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
    if (load.warehouse_id != actor.warehouse_id or load.franchise_id != lead.franchise_id
            or load.state != 'returned' or not load.unloaded_at or not load.eligibility_reference):
        raise Conflict('Valid unloading evidence on a returned load in your warehouse is required.')
    if Receipt.objects.filter(lead=lead, submitted_at__isnull=False).exists():
        raise Conflict('This lead has already been received.')
    try:
        bill = lead.bill
    except Bill.DoesNotExist:
        raise Conflict('A source bill is required.')
    if bill.state not in ('issued', 'completed', 'cancellation_requested'):
        raise Conflict('Source bill must be issued.')
    return bill


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
    bill = receipt_context(actor, lead, data)
    output = measurement_lines(lead, bill, data.get('lines', []), complete=submit)
    note = str(data.get('note', ''))
    if len(note) > 1000:
        raise ValidationError('Warehouse note must be at most 1,000 characters.')
    receipt, _ = Receipt.objects.get_or_create(lead=lead, defaults={'warehouse_id': actor.warehouse_id, 'actor': actor})
    if receipt.warehouse_id != actor.warehouse_id:
        raise Conflict('The saved reconciliation belongs to a different warehouse.')
    receipt.lines = output
    receipt.note = note
    receipt.actor = actor
    receipt.flagged = any(line['flagged'] for line in output)
    receipt.version += 1
    if submit:
        receipt.submitted_at = timezone.now()
    receipt.save()
    if submit:
        for line in sorted(output, key=lambda item: item['product']):
            qty = Decimal(line['actual'])
            if qty > 0:
                balance, _ = StockBalance.objects.get_or_create(warehouse_id=actor.warehouse_id, product_id=line['product'])
                balance = StockBalance.objects.select_for_update().get(pk=balance.pk)
                balance.quantity += qty
                balance.version += 1
                balance.save()
                StockMovement.objects.create(warehouse_id=actor.warehouse_id, product_id=line['product'], actor=actor,
                                             quantity=qty, kind='receipt', receipt=receipt)
    lead.version += 1
    lead.save(update_fields=['version'])
    event(actor, 'receipts', receipt, {'flagged': receipt.flagged}, 'receipt.submitted' if submit else 'receipt.draft_saved')
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
    balance=StockBalance.objects.select_for_update().filter(warehouse_id=actor.warehouse_id,product=request.product).first()
    if not balance or qty>balance.quantity or qty>request.quantity-request.released: raise Conflict('Release exceeds available stock or remaining demand.')
    balance.quantity-=qty;balance.version+=1;balance.save()
    movement=StockMovement.objects.create(warehouse_id=actor.warehouse_id,product=request.product,actor=actor,quantity=-qty,kind='release',requirement=request)
    request.released+=qty;request.version+=1
    if request.released==request.quantity: request.state='fulfilled'
    request.save();event(actor,'inventory',movement,action='stock.released');event(actor,'requirements',request,action='requirement.progress')
    return request
