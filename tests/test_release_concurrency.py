import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from django.db import close_old_connections
from django.utils import timezone
from core.fixtures import synthetic_records
from core.models import Role, User, Requirement, StockBalance, StockMovement, Receipt, Payment, AuditEvent
from core.services import warehouse
from core.services.common import command, Conflict
from test_workflows import post


def prepare():
    data=synthetic_records(policies=True)
    first=data['users'][Role.WAREHOUSE]
    second=User.objects.create(username='second-releaser',email='second-releaser@example.test',role=Role.WAREHOUSE,franchise=first.franchise,warehouse=first.warehouse)
    req=Requirement.objects.create(franchise=first.franchise,product=data['products'][0],actor=data['users'][Role.HQ],quantity=10,due_date=timezone.localdate())
    StockBalance.objects.create(warehouse=first.warehouse,product=req.product,quantity=10)
    return data,first,second,req


def compete(operations):
    barrier=Barrier(len(operations))
    def run(operation):
        actor,request,action,quantity=operation
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            body={'version':1,'quantity':quantity}
            def apply(current):
                result=(warehouse.release if action=='release' else warehouse.cancel_requirement)(current,request,body)
                return {'id':str(result.pk),'state':result.state}
            return command(actor,str(uuid.uuid4()),action,body,apply)
        except Conflict:return None
        finally:close_old_connections()
    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(run,operations))

@pytest.mark.django_db(transaction=True)
def test_different_operators_cannot_overfill_same_request():
    data,first,second,req=prepare()
    StockBalance.objects.update(quantity=20)
    results=compete([(first,req,'release','7'),(second,req,'release','7')])
    assert sum(r is not None for r in results)==1
    req.refresh_from_db()
    assert req.released==7 and StockBalance.objects.get().quantity==13
    assert StockMovement.objects.count()==1
    assert not Payment.objects.exists() and not Receipt.objects.exists()

@pytest.mark.django_db(transaction=True)
def test_two_requests_cannot_overdraw_shared_stock():
    data,first,second,req=prepare()
    other=Requirement.objects.create(franchise=req.franchise,product=req.product,actor=data['users'][Role.HQ],quantity=10,due_date=timezone.localdate())
    results=compete([(first,req,'release','7'),(second,other,'release','7')])
    assert sum(r is not None for r in results)==1
    assert StockBalance.objects.get().quantity==3 and StockMovement.objects.count()==1
    assert sorted(Requirement.objects.values_list('released',flat=True))==[0,7]
    assert AuditEvent.objects.filter(action='stock.released').count()==1

@pytest.mark.django_db(transaction=True)
def test_release_races_hq_cancellation():
    data,first,second,req=prepare()
    results=compete([(first,req,'release','4'),(data['users'][Role.HQ],req,'cancel','0')])
    assert sum(r is not None for r in results)==1
    req.refresh_from_db()
    if req.state=='cancelled':
        assert req.released==0 and StockBalance.objects.get().quantity==10
        assert not StockMovement.objects.exists()
    else:
        assert req.state=='active' and req.released==4 and StockBalance.objects.get().quantity==6
        assert StockMovement.objects.count()==1
    assert not Payment.objects.exists() and not Receipt.objects.exists()

@pytest.mark.django_db
@pytest.mark.parametrize('action',['transfer','adjust','correct','reverse','receive','pay'])
def test_undefined_stock_commands_remain_denied(data,client_for,action):
    actor=data['users'][Role.WAREHOUSE]
    req=Requirement.objects.create(franchise=actor.franchise,product=data['products'][0],actor=data['users'][Role.HQ],quantity=10,due_date=timezone.localdate())
    balance=StockBalance.objects.create(warehouse=actor.warehouse,product=req.product,quantity=10)
    client=client_for(actor)
    assert post(client,f'requirements/{req.pk}/{action}/',{'version':1,'quantity':'1'}).status_code==403
    assert post(client,f'inventory/{balance.pk}/{action}/',{'version':1,'quantity':'1'}).status_code==404
    assert client.patch(f'/api/v1/requirements/{req.pk}/',{'released':100},format='json').status_code==405
    assert StockBalance.objects.get().quantity==10
    assert not StockMovement.objects.exists() and not Payment.objects.exists() and not Receipt.objects.exists()
