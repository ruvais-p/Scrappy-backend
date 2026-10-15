from datetime import datetime
from decimal import Decimal
from unittest.mock import patch
import pytest
from django.utils import timezone
from core.models import Role, StockMovement, StockBalance, Warehouse
from test_reconciliation import prepared, payload
from test_workflows import post

@pytest.mark.django_db
def test_period_balances_current_and_scoped_sources(data,client_for):
    lead,_=prepared(data,quantities=('10',))
    manager=client_for(data['users'][Role.WAREHOUSE])
    result=post(manager,f'leads/{lead.pk}/receive/',payload(data,lead,('9',)))
    assert result.status_code==200
    receipt=result.data['id'];product=data['products'][0];warehouse=data['load'].warehouse
    # Historical ledger events precede the current receipt; signs remain explicit.
    for amount,kind,day in [('5','receipt',1),('-2','release',3)]:
        with patch('django.utils.timezone.now',return_value=timezone.make_aware(datetime(2020,1,day))):
            StockMovement.objects.create(warehouse=warehouse,product=product,actor=data['users'][Role.WAREHOUSE],quantity=amount,kind=kind)
    StockBalance.objects.filter(warehouse=warehouse,product=product).update(quantity=12)
    response=manager.get('/api/v1/inventory-report/?from=2020-01-02&to=2020-01-04')
    assert response.status_code==200,response.data
    row=response.data['results'][0]
    assert [Decimal(row[k]) for k in ('opening','receipts','releases','closing','current')]==[5,0,2,3,12]
    assert row['unit']=='kg'
    assert manager.get('/api/v1/movements/?receipt='+receipt).data['count']==1
    flags=manager.get('/api/v1/receipts/?flagged=1').data
    assert flags['count']==1 and str(flags['results'][0]['lead'])==str(lead.pk)
    foreign=Warehouse.objects.create(franchise=data['books'][0],name='Different yard')
    StockMovement.objects.create(warehouse=foreign,product=product,actor=data['users'][Role.HQ],quantity=100,kind='receipt')
    assert manager.get('/api/v1/inventory-report/').data['count']==1
    assert manager.get('/api/v1/inventory-report/?warehouse='+str(foreign.pk)).data['count']==0
    assert client_for(data['users'][Role.HQ]).get('/api/v1/inventory-report/').data['count']==2

@pytest.mark.django_db
@pytest.mark.parametrize('query',['from=bad','from=2026-02-02&to=2026-01-01','warehouse=bad','page=oops'])
def test_inventory_report_rejects_invalid_filters(data,client_for,query):
    assert client_for(data['users'][Role.HQ]).get('/api/v1/inventory-report/?'+query).status_code==400

@pytest.mark.django_db
@pytest.mark.parametrize('role',Role.values)
def test_inventory_report_permissions(data,client_for,role):
    expected=200 if role in (Role.SUPER,Role.ADMIN,Role.HQ,Role.APP,Role.WAREHOUSE) else 403
    assert client_for(data['users'][role]).get('/api/v1/inventory-report/').status_code==expected

@pytest.mark.django_db
def test_units_and_other_franchise_never_merge(data,client_for):
    from core.models import Product
    actor=data['users'][Role.HQ]
    pieces=Product.objects.create(name='Recovered units',category=data['products'][0].category,unit='pcs')
    for warehouse,product in [(data['warehouses'][0],data['products'][0]),(data['warehouses'][0],pieces),(data['warehouses'][1],pieces)]:
        StockMovement.objects.create(warehouse=warehouse,product=product,actor=actor,quantity=3,kind='receipt')
        StockBalance.objects.create(warehouse=warehouse,product=product,quantity=3)
    client=client_for(data['users'][Role.APP])
    result=client.get('/api/v1/inventory-report/').data
    assert result['count']==2
    assert {r['unit'] for r in result['results']}=={'kg','pcs'}
    assert all(Decimal(r['current'])==3 for r in result['results'])
    assert client.get('/api/v1/inventory-report/?franchise='+str(data['books'][1].pk)).data['count']==0
    assert client.get('/api/v1/movements/?warehouse='+str(data['warehouses'][1].pk)).data['count']==0
