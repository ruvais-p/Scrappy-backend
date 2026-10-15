from django.utils import timezone
from rest_framework.exceptions import ValidationError
from core.models import *
from core.policy import require,book_allowed,policy
from .common import decimal,version,event,Conflict

def account(driver):
    DriverAccount.objects.get_or_create(driver=driver)
    return DriverAccount.objects.select_for_update().get(driver=driver)

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

def count(actor,driver,data):
    require(actor,'count.write');book_allowed(actor,driver.franchise);policy('route_eligibility')
    row=account(driver);version(row,data.get('version'))
    loads=list(Load.objects.select_for_update(of=('self',)).filter(driver=driver,franchise=driver.franchise,counted_in__isnull=True).order_by('id'))
    if not loads or any(l.state=='on_route' or not l.returned_at or not l.eligibility_reference for l in loads): raise Conflict('Driver still out or no eligible uncounted routes.')
    if set(map(str,data.get('routes',[])))!={str(l.id) for l in loads}: raise Conflict('Covered routes changed. Refresh and review.')
    received=decimal(data.get('received'),positive=False);difference=received-row.balance
    note=str(data.get('note','')).strip()
    if difference and not note: raise ValidationError('A note is required for any difference.')
    if len(note)>1000: raise ValidationError('Note is too long.')
    h=Handover.objects.create(driver=driver,franchise=driver.franchise,counter=actor,expected=row.balance,received=received,difference=difference,note=note,cutoff=timezone.now())
    HandoverRoute.objects.bulk_create([HandoverRoute(handover=h,load=l) for l in loads])
    move(actor,driver,-received,'handover',handover=h)
    event(actor,'cash',h,{'expected':str(h.expected),'received':str(received),'difference':str(difference)},'cash.counted')
    return h
