import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, transaction
from django.utils import timezone

from core.models import Bill, CashMovement, DriverAccount, Handover, Load, Payment, Role, User
from core.services import billing, cash
from core.services.common import Conflict, command


def setup_count(data):
    driver=data['users'][Role.COLLECTOR]
    load=data['load']
    load.state='returned';load.returned_at=timezone.now();load.eligibility_reference='Synthetic return';load.save()
    cash.float_to(data['users'][Role.HQ],driver,{'amount':'100'})
    return {'version':DriverAccount.objects.get(driver=driver).version,
            'routes':[str(load.pk)],'received':'80','note':'Synthetic discrepancy'}


def run_race(functions):
    barrier=Barrier(len(functions))
    def worker(fn):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return fn()
        except Conflict:
            return 'conflict'
        finally:
            close_old_connections()
    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        return list(pool.map(worker,functions))


def count_command(data,payload,actor=None):
    actor=actor or data['users'][Role.WAREHOUSE]
    return command(actor,str(uuid.uuid4()),'cash.count',payload,
                   lambda current: str(cash.count(current,data['users'][Role.COLLECTOR],payload).pk))


@pytest.mark.django_db(transaction=True)
def test_two_counters_commit_one_handover(data):
    payload=setup_count(data)
    other=User.objects.create(username='other-counter',role=Role.WAREHOUSE,
                              franchise=data['books'][0],warehouse=data['warehouses'][0])
    results=run_race([lambda:count_command(data,payload),lambda:count_command(data,payload,other)])
    assert results.count('conflict')==1
    assert Handover.objects.count()==1
    assert CashMovement.objects.filter(kind='handover').count()==1
    assert DriverAccount.objects.get(driver=data['users'][Role.COLLECTOR]).balance==20


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('activity',['float','payment','route'])
def test_count_races_preserve_reviewed_snapshot(data,activity):
    payload=setup_count(data)
    driver=data['users'][Role.COLLECTOR]
    lead=data['leads'][0]
    lead.agreed_at=timezone.now();lead.save()
    bill=Bill.objects.create(franchise=data['books'][0],creator=driver,customer=lead.customer,
                             lead=lead,kind='collection',state='issued',total=20)
    def compete():
        if activity=='float':
            return command(data['users'][Role.HQ],str(uuid.uuid4()),'cash.float',{},
                           lambda actor: str(cash.float_to(actor,driver,{'amount':'25'}).pk))
        if activity=='payment':
            return command(driver,str(uuid.uuid4()),'bill.pay',{},
                           lambda actor: str(billing.pay(actor,bill,{'version':bill.version,
                               'amount':'20','method':'Cash'}).pk))
        with transaction.atomic():
            Load.objects.filter(pk=data['load'].pk).update(state='on_route',returned_at=None)
        return 'route changed'
    results=run_race([lambda:count_command(data,payload),compete])
    assert results[1]!='conflict'
    counted=results[0]!='conflict'
    assert Handover.objects.count()==int(counted)
    assert CashMovement.objects.filter(kind='handover').count()==int(counted)
    if counted:
        assert Handover.objects.get().expected==100
    change={'float':25,'payment':-20,'route':0}[activity]
    assert DriverAccount.objects.get(driver=driver).balance==100+change-(80 if counted else 0)
    if activity=='payment': assert Payment.objects.filter(bill=bill).count()==1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('activity',['float','payment','route_round_trip','new_route','evidence'])
def test_completed_activity_invalidates_review(data,activity):
    payload=setup_count(data)
    driver=data['users'][Role.COLLECTOR]
    if activity=='float':
        cash.float_to(data['users'][Role.HQ],driver,{'amount':'25'})
    elif activity=='payment':
        cash.move(driver,driver,-20,'customer_payment')
    elif activity=='new_route':
        Load.objects.create(driver=driver,franchise=data['books'][0],warehouse=data['warehouses'][0])
    elif activity=='evidence':
        Load.objects.filter(pk=data['load'].pk).update(eligibility_reference='Revised evidence')
    else:
        Load.objects.filter(pk=data['load'].pk).update(state='on_route')
        Load.objects.filter(pk=data['load'].pk).update(state='returned')
    with pytest.raises(Conflict,match='changed'):
        count_command(data,payload)
    assert not Handover.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_rolled_back_route_change_preserves_review(data):
    payload=setup_count(data)
    with pytest.raises(RuntimeError),transaction.atomic():
        Load.objects.filter(pk=data['load'].pk).update(state='on_route')
        raise RuntimeError('abort')
    count_command(data,payload)
    assert Handover.objects.get().expected==100
