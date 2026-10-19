from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from core.models import Bill, Customer, Franchise, Quotation
from core.policy import require, book_allowed, PolicyUnavailable, policy
from .common import version, event, Conflict
from . import billing


def _pricing(actor, book, customer, kind, lines, requested_mode=None):
    if book.status != 'active' or not customer.active or customer.franchise_id != book.pk:
        raise ValidationError('Choose an active franchise and customer from the same franchise.')
    if kind not in ('collection', 'sale') or (book.kind == 'digester' and kind != 'sale') or book.kind == 'hq':
        raise ValidationError('Choose a transaction type supported by this franchise.')
    result = billing.lines_for(actor, book, kind, lines)
    mode = billing.billing_mode_for(result, book, requested_mode)
    if mode == 'company':
        require(actor, 'company.billing')
    config = policy('billing_issue')
    if config.get('tax_mode') not in ('none', 'exclusive') or config.get('rounding') != 'half_up_2':
        raise PolicyUnavailable('Quotation tax and rounding policy is not supported.')
    subtotal = sum((Decimal(line['amount']) for line in result), Decimal('0'))
    tax = Decimal('0')
    for line in result:
        amount = Decimal(line['amount'])
        line_tax = (amount * Decimal(line['tax_percent']) / Decimal('100')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) if config['tax_mode'] == 'exclusive' else Decimal('0')
        line['tax'] = str(line_tax)
        tax += line_tax
    return result, mode, subtotal, tax


@transaction.atomic
def save(actor, data, quotation=None):
    require(actor, 'quotation.write')
    if quotation:
        version(quotation, data.get('version'))
        if quotation.state != 'draft':
            raise Conflict('Only a draft quotation can be changed.')
    try:
        book = Franchise.objects.get(pk=data.get('franchise') or (quotation.franchise_id if quotation else actor.franchise_id))
    except (Franchise.DoesNotExist, ValueError):
        raise ValidationError('Choose a valid franchise.')
    book_allowed(actor, book)
    try:
        customer = Customer.objects.get(pk=data.get('customer') or (quotation.customer_id if quotation else None), franchise=book, active=True)
    except (Customer.DoesNotExist, ValueError):
        raise ValidationError('Choose an active customer in the selected franchise.')
    kind=data.get('kind',quotation.kind if quotation else 'sale')
    lines, mode, subtotal, tax = _pricing(actor,book,customer,kind,data.get('lines',[]),data.get('billing_mode'))
    if quotation is None:
        quotation=Quotation(franchise=book,creator=actor,customer=customer,kind=kind,number='QT-'+uuid4().hex[:10].upper())
    quotation.franchise=book;quotation.customer=customer;quotation.kind=kind;quotation.billing_mode=mode
    quotation.lines=lines;quotation.subtotal=subtotal;quotation.tax=tax;quotation.total=subtotal+tax
    quotation.notes=str(data.get('notes',''))[:1000];quotation.version+=1;quotation.save()
    event(actor,'quotations',quotation,{'number':quotation.number,'total':str(quotation.total)},'quotation.saved')
    return quotation


@transaction.atomic
def transition(actor, quotation, action, data):
    require(actor, 'quotation.write')
    quotation=Quotation.objects.select_for_update().get(pk=quotation.pk)
    version(quotation,data.get('version'))
    if action=='send':
        if quotation.state!='draft': raise Conflict('Only a draft quotation can be marked as sent.')
        quotation.state='sent';quotation.sent_at=timezone.now()
    elif action=='accept':
        if quotation.state!='sent': raise Conflict('Only a sent quotation can be accepted.')
        quotation.state='accepted';quotation.accepted_at=timezone.now()
    elif action=='reject':
        if quotation.state!='sent': raise Conflict('Only a sent quotation can be rejected.')
        quotation.state='rejected';quotation.rejected_at=timezone.now()
    else: raise ValidationError('Unknown quotation action.')
    quotation.version+=1;quotation.save()
    event_action={'send':'quotation.sent','accept':'quotation.accepted','reject':'quotation.rejected'}[action]
    event(actor,'quotations',quotation,{'number':quotation.number},event_action)
    return quotation


@transaction.atomic
def convert(actor, quotation, data):
    require(actor,'bill.write')
    quotation=Quotation.objects.select_for_update().select_related('franchise','customer').get(pk=quotation.pk)
    version(quotation,data.get('version'))
    if quotation.state!='accepted': raise Conflict('Record customer acceptance before converting this quotation.')
    if quotation.converted_bill_id: raise Conflict('This quotation has already been converted.')
    if quotation.franchise.status!='active' or not quotation.customer.active:
        raise ValidationError('The franchise or customer is no longer active.')
    bill=Bill.objects.create(franchise=quotation.franchise,creator=actor,customer=quotation.customer,
        kind=quotation.kind,mode='sales' if quotation.kind=='sale' else 'purchase',transaction_type='B2C',billing_mode=quotation.billing_mode,state='draft',lines=deepcopy(quotation.lines),
        subtotal=quotation.subtotal,tax=quotation.tax,total=quotation.total,notes=quotation.notes,
        snapshot={'quotation':{'id':str(quotation.pk),'number':quotation.number}})
    quotation.state='converted';quotation.converted_bill=bill;quotation.converted_at=timezone.now()
    quotation.version+=1;quotation.save()
    event(actor,'quotations',quotation,{'number':quotation.number,'bill_id':str(bill.pk)},'quotation.converted')
    return bill
