from django.utils import timezone
from django.db import transaction
from django.db.models import Sum
from decimal import Decimal
from rest_framework.exceptions import ValidationError, PermissionDenied
from core.models import *
from core.policy import require,book_allowed,policy
from .common import decimal,version,event,Conflict

def account(driver):
    DriverAccount.objects.get_or_create(driver=driver)
    return DriverAccount.objects.select_for_update().get(driver=driver)

@transaction.atomic
def move(actor,driver,amount,kind,payment=None,handover=None):
    row=account(driver)
    if kind=='customer_payment' and row.balance+amount<0: raise Conflict('Insufficient cash. Refresh the current balance.')
    row.balance+=amount;row.version+=1;row.save()
    movement=CashMovement.objects.create(actor=actor,driver=driver,franchise=driver.franchise,amount=amount,kind=kind,payment=payment,handover=handover)
    event(actor,'cash',movement,{'amount':str(amount),'kind':kind})
    return movement

def float_to(actor,driver,data):
    require(actor,'float.write');book_allowed(actor,driver.franchise)
    if not driver.is_active or driver.role not in (Role.DRIVER,Role.COLLECTOR): raise ValidationError('Select an active driver or Collector.')
    return move(actor,driver,decimal(data.get('amount')),'float')

@transaction.atomic
def count(actor,driver,data):
    require(actor,'count.write');book_allowed(actor,driver.franchise);policy('route_eligibility')
    if not driver.is_active or driver.role not in (Role.DRIVER,Role.COLLECTOR):
        raise ValidationError('Select an active driver or Collector.')
    if not actor.warehouse_id or actor.warehouse.franchise_id!=driver.franchise_id:
        raise PermissionDenied('An assigned warehouse in this franchise is required for counting.')
    row=account(driver);version(row,data.get('version'))
    # The account lock serializes every cash movement. Capture the ledger
    # boundary before posting this handover, including cash carried from all
    # prior routes rather than summing just the routes being counted today.
    cutoff=timezone.now()
    expected=CashMovement.objects.filter(driver=driver,created_at__lte=cutoff).aggregate(total=Sum('amount'))['total'] or Decimal('0')
    if expected!=row.balance:
        raise Conflict('Cash balance does not match its ledger. Reconcile before counting.')
    # Route writes acquire this same account lock in the database trigger.
    # Do not lock loads here: an updater may already hold a load row while
    # waiting for our account lock, which would invert the lock order.
    loads=list(Load.objects.filter(driver=driver,counted_in__isnull=True).order_by('id'))
    if not loads: raise Conflict('No eligible uncounted routes.')
    if any(l.state=='on_route' for l in loads): raise Conflict('Driver still out.')
    if any(l.franchise_id!=driver.franchise_id or l.warehouse_id!=actor.warehouse_id for l in loads):
        raise PermissionDenied('All covered routes must belong to your assigned warehouse.')
    if any(l.state!='returned' or not l.returned_at or l.returned_at>cutoff or not l.eligibility_reference.strip() for l in loads):
        raise Conflict('Valid route-return evidence is required before counting.')
    routes=data.get('routes')
    if not isinstance(routes,list) or len(routes)!=len(loads) or set(map(str,routes))!={str(l.id) for l in loads}: raise Conflict('Covered routes changed. Refresh and review.')
    received=decimal(data.get('received'),positive=False);difference=received-expected
    note=data.get('note','')
    if not isinstance(note,str): raise ValidationError('Enter a text note.')
    note=note.strip()
    if difference and not note: raise ValidationError('A note is required for any difference.')
    if len(note)>1000: raise ValidationError('Note is too long.')
    h=Handover.objects.create(driver=driver,franchise=driver.franchise,counter=actor,expected=expected,received=received,difference=difference,note=note,cutoff=cutoff)
    HandoverRoute.objects.bulk_create([HandoverRoute(handover=h,load=l) for l in loads])
    move(actor,driver,-received,'handover',handover=h)
    event(actor,'cash',h,{'expected':str(h.expected),'received':str(received),'difference':str(difference)},'cash.counted')
    return h
