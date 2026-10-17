from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError,PermissionDenied
from core.models import *
from core.policy import *
from .common import decimal,version,event,Conflict

def resolved_rate(product,book,kind):
    r=Rate.objects.filter(product=product,kind=kind,franchise=book).order_by('-created_at').first() or Rate.objects.filter(product=product,kind=kind,franchise=None).order_by('-created_at').first()
    if not r: raise ValidationError(f'No applicable {kind} rate for {product.name}.')
    return r.amount

def cancellation_history(bill):
    return AuditEvent.objects.filter(resource=str(bill.pk),franchise_id=bill.franchise_id,
                                     action='bill.cancellation_requested').order_by('created_at','pk')

def request_cancellation(actor,bill,data):
    require(actor,'bill.write')
    bill=scoped_get(actor,Bill.objects.select_for_update(),'bills',bill.pk)
    version(bill,data.get('version'))
    if bill.lead_id:
        raise PolicyUnavailable('Pickup bill correction policy is not defined.')
    if bill.state not in ('issued','completed') or cancellation_history(bill).exists():
        raise Conflict('This bill cannot enter cancellation review.')
    reason=data.get('reason')
    if not isinstance(reason,str) or not reason.strip() or len(reason.strip())>500:
        raise ValidationError('Enter a cancellation reason of 1 to 500 characters.')
    previous_state=bill.state
    bill.state='cancellation_requested';bill.version+=1;bill.save()
    event(actor,'bills',bill,{'reason':reason.strip(),'previous_state':previous_state},'bill.cancellation_requested')
    return bill

def lines_for(actor,book,kind,lines,pickup=False):
    if not isinstance(lines,list) or not 1<=len(lines)<=100: raise ValidationError('Enter between 1 and 100 bill items.')
    output=[];seen=set()
    for line in lines:
        try: product=Product.objects.select_related('category').get(pk=line.get('product'))
        except (Product.DoesNotExist,ValueError): raise ValidationError('Invalid product.')
        if product.pk in seen or not eligible_product(actor,product,book): raise ValidationError('Product is unavailable or duplicated.')
        if pickup and (product.unit!='kg' or product.category.kind!='scrap'): raise ValidationError('Pickup items must be scrap measured in kg.')
        seen.add(product.pk);quantity=decimal(line.get('quantity'),3);rate=resolved_rate(product,book,kind)
        output.append({'product':str(product.pk),'name':product.name,'unit':product.unit,'quantity':str(quantity),'rate':str(rate),'amount':str((quantity*rate).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),'hsn':product.hsn,'tax_percent':str(product.tax_percent)})
    return output

def save_draft(actor,data,bill=None):
    require(actor,'pickup.write' if bill and bill.lead_id else 'bill.write')
    book=bill.franchise if bill else Franchise.objects.get(pk=data.get('franchise') or actor.franchise_id)
    book_allowed(actor,book)
    if book.status!='active': raise ValidationError('This franchise is not active for new billing.')
    customer=Customer.objects.filter(pk=data.get('customer') or (bill.customer_id if bill else None),franchise=book,active=True).first()
    if not customer: raise ValidationError('Select an active customer in this book.')
    kind=bill.kind if bill else data.get('kind','collection')
    if kind not in ('collection','sale','b2b') or (book.kind=='digester' and kind!='sale') or (book.kind=='hq' and kind!='collection'): raise ValidationError('Transaction type is incompatible with the book.')
    if bill:
        version(bill,data.get('version'))
        if bill.state!='draft': raise Conflict('Issued values cannot be edited.')
    lines=lines_for(actor,book,kind,data.get('lines',[]),bool(bill and bill.lead_id))
    if not bill: bill=Bill(franchise=book,creator=actor,customer=customer,kind=kind)
    bill.lines=lines;bill.customer=customer;bill.notes=str(data.get('notes',''))[:1000];bill.subtotal=sum(Decimal(l['amount']) for l in lines);bill.total=bill.subtotal;bill.version+=1;bill.save()
    event(actor,'pickups' if bill.lead_id else 'bills',bill,action='bill.draft_saved')
    return bill

def issue(actor,bill,data):
    version(bill,data.get('version'))
    if bill.state!='draft': raise Conflict('This bill is already issued.')
    if bill.kind=='b2b': raise PolicyUnavailable('B2B posting authority is not defined.')
    config=policy('billing_issue')
    required=('issuer_by_book','tax_mode','rounding','number_prefix')
    if any(k not in config for k in required): raise PolicyUnavailable('Billing policy is incomplete.')
    if config['rounding']!='half_up_2' or config['tax_mode'] not in ('none','exclusive'): raise PolicyUnavailable('This tax/rounding policy is not supported.')
    if bill.franchise.status!='active' or not bill.customer.active: raise ValidationError('The book or customer is inactive.')
    refreshed=lines_for(actor,bill.franchise,bill.kind,bill.lines,bool(bill.lead_id))
    if refreshed!=bill.lines: raise Conflict('Rates or products changed. Refresh and review your draft.')
    issuer=config['issuer_by_book'].get(str(bill.franchise_id))
    if not issuer: raise PolicyUnavailable('An approved issuer is required for this book.')
    key=config['number_prefix']
    seq,_=InvoiceSequence.objects.get_or_create(key=key)
    seq=InvoiceSequence.objects.select_for_update().get(pk=key)
    bill.number=f'{key}-{seq.next_number:08d}';seq.next_number+=1;seq.save()
    bill.subtotal=sum(Decimal(l['amount']) for l in bill.lines)
    bill.tax=Decimal('0')
    if config['tax_mode']=='exclusive':
        for line in bill.lines:
            tax=(Decimal(line['amount'])*Decimal(line['tax_percent'])/100).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
            line['tax']=str(tax);bill.tax+=tax
    bill.total=bill.subtotal+bill.tax
    bill.snapshot={'issuer':issuer,'customer':{'name':bill.customer.name,'address':bill.customer.address,'phone':bill.customer.phone,'gstin':bill.customer.gstin},'book':bill.franchise.name,'creator':actor.get_full_name() or actor.email,'terms':config.get('terms',''),'policy':config}
    if bill.lead_id:
        bill.snapshot['pickup']={'lead':str(bill.lead_id),'load':str(bill.lead.load_id) if bill.lead.load_id else '', 'collector':actor.get_full_name(),'vehicle':actor.vehicle,'area':actor.area}
    bill.state='issued';bill.issued_at=timezone.now();bill.version+=1
    if bill.franchise.kind=='digester':
        policy('digester_earnings');bill.share_percent=bill.franchise.share_percent
        EarningsEntry.objects.create(franchise=bill.franchise,bill=bill,actor=actor,kind='earning',amount=(bill.subtotal*bill.share_percent/100).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
    bill.save();event(actor,'pickups' if bill.lead_id else 'bills',bill,{'number':bill.number},'bill.issued')
    return bill

@transaction.atomic
def pay(actor,bill,data):
    # Payment settlement is serialized on the bill row so two operators cannot
    # both observe the same outstanding balance and overpay it.
    locked_bill=Bill.objects.select_for_update(of=('self',)).select_related('franchise','customer').get(pk=bill.pk)
    # Keep a caller-held instance current for service users that chain commands
    # in one transaction (the API serializes the returned locked instance).
    bill.__dict__.update(locked_bill.__dict__)
    version(bill,data.get('version'))
    if bill.state not in ('issued','cancellation_requested'): raise Conflict('This bill is not payable.')
    if bill.franchise.status!='active': policy('suspended_settlement')
    if bill.kind=='b2b': raise PolicyUnavailable('B2B posting authority is not defined.')
    amount=decimal(data.get('amount'));method=data.get('method');reference=str(data.get('reference','')).strip()
    if method not in ('Cash','UPI','Bank transfer','Card','Cheque'): raise ValidationError('Invalid payment method.')
    if method=='Cheque': policy('cheque_clearance')
    if method=='UPI' and not reference: raise ValidationError('Enter the external transaction reference.')
    if len(reference)>160 or amount>bill.total-bill.settled: raise ValidationError('Payment exceeds the outstanding amount or reference is too long.')
    lead=None
    if bill.lead_id:
        require(actor,'pickup.write')
        lead=Lead.objects.select_for_update().get(pk=bill.lead_id)
        if lead.driver_id!=actor.id or not lead.agreed_at: raise PermissionDenied('Customer agreement on your assigned pickup is required.')
        if amount!=bill.total-bill.settled: raise PolicyUnavailable('Partial pickup payment policy is not defined.')
        if method not in ('Cash','UPI'): raise ValidationError('Pickup payment must be Cash or UPI.')
        # Older pickups may predate route linking. Ensure the settled pickup is
        # included in the collector's current load before closing it.
        from .pickups import ensure_load
        ensure_load(actor,lead)
    payment=Payment.objects.create(bill=bill,actor=actor,amount=amount,method=method,reference=reference,paid_at=timezone.now())
    if lead and method=='Cash':
        from .cash import move
        move(actor,actor,-amount,'customer_payment',payment=payment)
    bill.settled+=amount;bill.version+=1
    if bill.settled==bill.total:
        bill.state='cancellation_requested' if bill.state=='cancellation_requested' or cancellation_history(bill).exists() else 'completed'
        if lead: lead.state='closed';lead.closed_at=timezone.now();lead.version+=1;lead.save();event(actor,'leads',lead,action='pickup.closed')
    bill.save();event(actor,'pickups' if lead else 'bills',bill,{'payment':str(payment.id),'amount':str(amount),'method':method},'payment.recorded')
    return bill
