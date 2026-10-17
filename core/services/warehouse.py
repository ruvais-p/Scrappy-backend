from decimal import Decimal
from django.utils import timezone
from django.db import transaction
from rest_framework.exceptions import ValidationError
from core.models import *
from core.policy import *
from .common import decimal,version,event,Conflict

def compare(billed,actual):
    difference=actual-billed
    pct=difference/billed*100 if billed else None
    flagged=abs(pct)>2 if pct is not None else actual>0
    return {'billed':str(billed),'actual':str(actual),'difference':str(difference),'percent':str(pct) if pct is not None else None,'flagged':flagged,'check':'Unbilled excess' if billed==0 and actual>0 else 'Not billed / no receipt' if billed==0 else 'Short' if flagged and difference<0 else 'Excess' if flagged else 'Matched' if difference==0 else 'Within tolerance'}

def receipt_context(actor, lead, data):
    require(actor, 'receipt.write')
    book_allowed(actor, lead.franchise)
    policy('warehouse_receipt_eligibility')
    version(lead, data.get('version'))
    if not lead.load_id:
        raise Conflict('A warehouse load association is required.')
    load = Load.objects.select_for_update().get(pk=lead.load_id)
    if ((actor.role==Role.WAREHOUSE and load.warehouse_id != actor.warehouse_id) or load.franchise_id != lead.franchise_id
            or load.state != 'returned' or not load.unloaded_at or not load.eligibility_reference):
        raise Conflict('Valid unloading evidence on a returned load in your warehouse is required.')
    receipt=Receipt.objects.filter(lead=lead).first()
    reweigh=load.reconciliation_state=='reweigh_requested'
    if receipt and receipt.submitted_at and (not reweigh or receipt.revision>=load.reweigh_version):
        raise Conflict('This lead has already been received for the current weighing.')
    try:
        bill = lead.bill
    except Bill.DoesNotExist:
        raise Conflict('A source bill is required.')
    if bill.state not in ('issued', 'completed', 'cancellation_requested'):
        raise Conflict('Source bill must be issued.')
    return bill,load,load.warehouse_id,reweigh


def update_load_reconciliation(actor,load):
    eligible=Lead.objects.filter(load=load,bill__state__in=['issued','completed','cancellation_requested'])
    total=eligible.count()
    if not total:return
    receipts=Receipt.objects.filter(lead__in=eligible,submitted_at__isnull=False)
    if receipts.count()!=total:return
    if load.reconciliation_state=='reweigh_requested' and receipts.filter(revision__lt=load.reweigh_version).exists():return
    state='flagged' if receipts.filter(flagged=True).exists() else 'reconciled'
    if load.reconciliation_state!=state:
        load.reconciliation_state=state
        load.version+=1
        load.save(update_fields=['reconciliation_state','version','updated_at'])
        event(actor,'loads',load,{'reconciliation_state':state},'load.reconciliation_completed')


def adjust_reweighed_stock(actor,receipt,archive,warehouse_id,new_lines):
    old={line['product']:Decimal(line['actual']) for line in receipt.lines}
    new={line['product']:Decimal(line['actual']) for line in new_lines}
    for product_id in sorted(set(old)|set(new)):
        delta=new.get(product_id,Decimal('0'))-old.get(product_id,Decimal('0'))
        if not delta:continue
        balance,_=StockBalance.objects.get_or_create(warehouse_id=warehouse_id,product_id=product_id)
        balance=StockBalance.objects.select_for_update().get(pk=balance.pk)
        if balance.quantity+delta<0:
            raise Conflict('Reweigh would reduce stock below zero because some received material has already been released.')
        balance.quantity+=delta;balance.version+=1;balance.save()
        StockMovement.objects.create(warehouse_id=warehouse_id,product_id=product_id,actor=actor,
                                     quantity=delta,kind='receipt_adjustment',receipt_revision=archive)


def measurement_lines(lead, bill, supplied, complete=True):
    if not isinstance(supplied, list) or len(supplied) > 100 or (complete and not supplied):
        raise ValidationError('Enter measured material weights (at most 100 lines).')
    actuals = {}
    for line in supplied:
        if not isinstance(line, dict):
            raise ValidationError('Each measurement must identify a material and actual weight.')
        key = str(line.get('product'))
        if key in actuals:
            raise ValidationError('Duplicate material.')
        actuals[key] = decimal(line.get('actual'), 3, False)
    billed = {line['product']: line for line in bill.lines}
    if complete and not set(billed).issubset(actuals):
        raise ValidationError('Measure every billed material; missing is not zero.')
    output = []
    for pk, actual in actuals.items():
        product = Product.objects.filter(pk=pk, franchises=lead.franchise, unit='kg', tracks_stock=True, category__kind='scrap').first()
        if not product or (pk not in billed and (not product.active or not product.category.active)):
            raise ValidationError('Invalid material for this warehouse receipt.')
        original = billed.get(pk)
        output.append({'product': pk, 'name': original['name'] if original else product.name,
                       **compare(Decimal(original['quantity']) if original else Decimal(0), actual)})
    return output


def save_measurements(actor, lead, data, submit=False):
    bill,load,warehouse_id,reweigh = receipt_context(actor, lead, data)
    output = measurement_lines(lead, bill, data.get('lines', []), complete=submit)
    note = str(data.get('note', ''))
    if len(note) > 1000:
        raise ValidationError('Warehouse note must be at most 1,000 characters.')
    receipt, _ = Receipt.objects.get_or_create(lead=lead, defaults={'warehouse_id': warehouse_id, 'actor': actor})
    if receipt.warehouse_id != warehouse_id:
        raise Conflict('The saved reconciliation belongs to a different warehouse.')
    if reweigh and receipt.submitted_at:
        if not submit:
            draft,_=ReceiptRevision.objects.get_or_create(receipt=receipt,revision=load.reweigh_version,
                submitted_at__isnull=True,superseded_at__isnull=True,
                defaults={'actor':actor,'lines':output,'note':note,'flagged':any(line['flagged'] for line in output)})
            draft.actor=actor;draft.lines=output;draft.note=note;draft.flagged=any(line['flagged'] for line in output)
            draft.version+=1;draft.save()
            lead.version+=1;lead.save(update_fields=['version','updated_at'])
            event(actor,'receipts',receipt,{'flagged':draft.flagged,'revision':load.reweigh_version},'receipt.reweigh_draft_saved')
            receipt.lines=output;receipt.note=note;receipt.pending_reweigh=True
            return receipt
        archive=ReceiptRevision.objects.create(receipt=receipt,actor=receipt.actor,revision=receipt.revision,
            lines=receipt.lines,note=receipt.note,submitted_at=receipt.submitted_at,flagged=receipt.flagged,
            superseded_at=timezone.now())
        adjust_reweighed_stock(actor,receipt,archive,warehouse_id,output)
        ReceiptRevision.objects.filter(receipt=receipt,revision=load.reweigh_version,
            submitted_at__isnull=True,superseded_at__isnull=True).delete()
        receipt.revision=load.reweigh_version
        receipt.lines=output;receipt.note=note;receipt.actor=actor;receipt.flagged=any(line['flagged'] for line in output)
        receipt.submitted_at=timezone.now();receipt.version+=1;receipt.save()
        lead.version+=1;lead.save(update_fields=['version','updated_at'])
        event(actor,'receipts',receipt,{'flagged':receipt.flagged,'revision':receipt.revision},'receipt.reweighed')
        update_load_reconciliation(actor,load)
        return receipt
    receipt.lines = output
    receipt.note = note
    receipt.actor = actor
    receipt.flagged = any(line['flagged'] for line in output)
    receipt.version += 1
    if submit:
        receipt.submitted_at = timezone.now()
        receipt.revision=load.reweigh_version
    receipt.save()
    if submit:
        for line in sorted(output, key=lambda item: item['product']):
            qty = Decimal(line['actual'])
            if qty > 0:
                balance, _ = StockBalance.objects.get_or_create(warehouse_id=warehouse_id, product_id=line['product'])
                balance = StockBalance.objects.select_for_update().get(pk=balance.pk)
                balance.quantity += qty
                balance.version += 1
                balance.save()
                StockMovement.objects.create(warehouse_id=warehouse_id, product_id=line['product'], actor=actor,
                                             quantity=qty, kind='receipt', receipt=receipt)
    lead.version += 1
    lead.save(update_fields=['version'])
    event(actor, 'receipts', receipt, {'flagged': receipt.flagged}, 'receipt.submitted' if submit else 'receipt.draft_saved')
    if submit:update_load_reconciliation(actor,load)
    return receipt


def receive(actor, lead, data):
    return save_measurements(actor, lead, data, submit=True)


def create_requirement(actor, data):
    from datetime import date
    from uuid import UUID
    require(actor, 'requirement.write')
    try:
        book_id=UUID(str(data.get('franchise','')))
        product_id=UUID(str(data.get('product','')))
        due=date.fromisoformat(str(data.get('due_date','')))
    except ValueError:
        raise ValidationError('Choose a valid franchise, material and due date.')
    book=Franchise.objects.filter(pk=book_id,kind='scrap',status='active').first()
    product=Product.objects.select_related('category').filter(pk=product_id).first()
    if not book or not product or not eligible_product(actor,product,book) or not product.tracks_stock or product.category.kind!='scrap':
        raise ValidationError('Choose an active scrap franchise and an assigned stock material.')
    note=str(data.get('note',''))
    if len(note)>1000: raise ValidationError('Note must be at most 1,000 characters.')
    obj=Requirement.objects.create(franchise=book,product=product,actor=actor,quantity=decimal(data.get('quantity'),3),due_date=due,note=note)
    event(actor,'requirements',obj,action='requirement.created')
    return obj


@transaction.atomic
def cancel_requirement(actor, request, data):
    request=Requirement.objects.select_for_update().get(pk=request.pk)
    require(actor,'requirement.write');book_allowed(actor,request.franchise)
    version(request,data.get('version'))
    if request.released or request.state!='active':
        raise Conflict('Only wholly unfulfilled active requests can be cancelled.')
    request.state='cancelled';request.version+=1;request.save()
    event(actor,'requirements',request,action='requirement.cancelled')
    return request


@transaction.atomic
def release(actor,request,data):
    request=Requirement.objects.select_for_update().get(pk=request.pk)
    require(actor,'release.write');book_allowed(actor,request.franchise);version(request,data.get('version'))
    if request.state!='active' or request.released>=request.quantity: raise Conflict('This requirement is not open.')
    qty=decimal(data.get('quantity'),3)
    warehouse_id=data.get('warehouse')
    warehouse=Warehouse.objects.filter(pk=warehouse_id,franchise=request.franchise).first()
    if not warehouse: raise ValidationError('Choose a warehouse in the requirement franchise.')
    balance=StockBalance.objects.select_for_update().filter(warehouse=warehouse,product=request.product).first()
    if not balance or qty>balance.quantity or qty>request.quantity-request.released: raise Conflict('Release exceeds available stock or remaining demand.')
    balance.quantity-=qty;balance.version+=1;balance.save()
    movement=StockMovement.objects.create(warehouse=warehouse,product=request.product,actor=actor,quantity=-qty,kind='release',requirement=request)
    request.released+=qty;request.version+=1
    if request.released==request.quantity: request.state='fulfilled'
    request.save();event(actor,'inventory',movement,action='stock.released');event(actor,'requirements',request,action='requirement.progress')
    return request


@transaction.atomic
def resolve_flagged_load(actor,load,data):
    require(actor,'load.resolve')
    load=Load.objects.select_for_update().get(pk=load.pk)
    version(load,data.get('version'))
    if load.reconciliation_state!='flagged':
        raise Conflict('Only a completed flagged load can be resolved.')
    decision=data.get('decision')
    if decision not in ('accept','reweigh'):
        raise ValidationError('Choose accept or request a reweigh.')
    note=data.get('note','')
    if not isinstance(note,str) or not note.strip():
        raise ValidationError('Enter a resolution note.')
    note=note.strip()
    if len(note)>1000: raise ValidationError('Resolution note must be at most 1,000 characters.')
    if decision=='reweigh':
        load.reweigh_version+=1
        load.reconciliation_state='reweigh_requested'
    else:
        load.reconciliation_state='reconciled'
    load.resolution_action='accepted' if decision=='accept' else 'reweigh'
    load.resolution_note=note
    load.resolved_by=actor
    load.resolved_at=timezone.now()
    load.version+=1
    load.save(update_fields=['reconciliation_state','reweigh_version','resolution_action','resolution_note',
                             'resolved_by','resolved_at','version','updated_at'])
    event(actor,'loads',load,{'decision':decision,'note':note,'reweigh_version':load.reweigh_version},'load.flagged_resolved')
    return load
