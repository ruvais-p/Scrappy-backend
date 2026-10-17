import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import close_old_connections, transaction
from django.utils import timezone

from core.fixtures import synthetic_records
from core.models import AuditEvent, Bill, Lead, Outbox, Policy, Receipt, Role, StockBalance, StockMovement, User, Warehouse
from core.services import billing, pickups, warehouse
from core.services.common import command, Conflict
from test_workflows import post


def prepared(data, index=0, quantities=('10', '2', '5', '5')):
    actor=data['users'][Role.COLLECTOR];lead=data['leads'][index]
    with transaction.atomic():
        bill=pickups.start(actor,lead,{'version':lead.version})
        bill=billing.save_draft(actor,{'version':bill.version,'lines':[{'product':str(p.pk),'quantity':q} for p,q in zip(data['products'],quantities)]},bill)
        bill=billing.issue(actor,bill,{'version':bill.version})
    load=data['load'];load.state='returned';load.returned_at=timezone.now();load.unloaded_at=timezone.now();load.eligibility_reference='Synthetic authorised unload';load.save()
    lead.refresh_from_db()
    return lead,bill


def payload(data,lead,actual=('9','12','11','5')):
    return {'version':lead.version,'lines':[{'product':str(p.pk),'actual':q} for p,q in zip(data['products'],actual)],'note':'Measured at unloading'}


@pytest.mark.django_db
def test_draft_resume_preview_and_submit_reference(data,client_for):
    lead,bill=prepared(data);client=client_for(data['users'][Role.WAREHOUSE])
    original=bill.lines
    partial=payload(data,lead,('9',));key=str(uuid.uuid4())
    response=post(client,f'leads/{lead.pk}/save-reconciliation/',partial,key)
    assert response.status_code==200,response.data
    assert post(client,f'leads/{lead.pk}/save-reconciliation/',partial,key).data==response.data
    assert not StockMovement.objects.exists() and not StockBalance.objects.exists()
    resumed=client.get(f'/api/v1/reconciliation/{lead.pk}/').data
    assert resumed['receipt']['state']=='draft' and resumed['receipt']['lines'][0]['actual']=='9'
    assert resumed['version']==response.data['lead_version']
    lead.refresh_from_db();full=payload(data,lead)
    preview=post(client,f'reconciliation/{lead.pk}/',full)
    assert preview.status_code==200 and preview.data['complete']
    assert Decimal(preview.data['billed_total'])==22 and Decimal(preview.data['actual_total'])==37
    assert sum(line['flagged'] for line in preview.data['lines'])==3
    assert not StockMovement.objects.exists()
    key=str(uuid.uuid4());result=post(client,f'leads/{lead.pk}/receive/',full,key)
    assert result.status_code==200,result.data
    assert post(client,f'leads/{lead.pk}/receive/',full,key).data==result.data
    assert sum(StockBalance.objects.values_list('quantity',flat=True))==37
    assert StockMovement.objects.count()==4 and Receipt.objects.count()==1
    bill.refresh_from_db();assert bill.lines==original
    assert not client.get(f'/api/v1/reconciliation/{lead.pk}/').data['can_edit']


@pytest.mark.django_db
def test_incomplete_preview_and_blank_submit(data,client_for):
    lead,_=prepared(data);client=client_for(data['users'][Role.WAREHOUSE])
    partial=payload(data,lead,('0',))
    preview=post(client,f'reconciliation/{lead.pk}/',partial)
    assert preview.status_code==200 and not preview.data['complete']
    assert preview.data['lines'][0]['actual']=='0'
    assert post(client,f'leads/{lead.pk}/receive/',partial).status_code==400
    assert not Receipt.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('actual',['','-1','NaN','Infinity','1.0001'])
def test_invalid_measurement_is_not_saved(data,client_for,actual):
    lead,_=prepared(data);client=client_for(data['users'][Role.WAREHOUSE])
    assert post(client,f'leads/{lead.pk}/save-reconciliation/',payload(data,lead,(actual,))).status_code==400
    assert not Receipt.objects.exists() and not StockMovement.objects.exists()


@pytest.mark.django_db
def test_unbilled_excess_and_historical_material_name(data,client_for):
    lead,bill=prepared(data,quantities=('10',));product=data['products'][0]
    product.name='Renamed after issue';product.save()
    client=client_for(data['users'][Role.WAREHOUSE]);result=post(client,f'leads/{lead.pk}/receive/',payload(data,lead,('10','3')))
    assert result.status_code==200,result.data
    first,extra=result.data['lines'];assert first['name']=='Iron'
    assert extra['billed']=='0' and extra['percent'] is None and extra['flagged']
    assert extra['check']=='Unbilled excess'
    assert sum(StockBalance.objects.values_list('quantity',flat=True))==13


@pytest.mark.django_db
def test_on_route_foreign_warehouse_and_missing_policy(data,client_for):
    lead,_=prepared(data);client=client_for(data['users'][Role.WAREHOUSE])
    load=data['load'];load.state='on_route';load.save()
    assert post(client,f'leads/{lead.pk}/save-reconciliation/',payload(data,lead)).status_code==409
    load.state='returned';load.save()
    other=Warehouse.objects.create(franchise=data['books'][0],name='Other warehouse')
    user=User.objects.create(username='other-warehouse',email='other-warehouse@example.test',role=Role.WAREHOUSE,franchise=data['books'][0],warehouse=other)
    assert client_for(user).get(f'/api/v1/reconciliation/{lead.pk}/').status_code==404
    assert post(client_for(user),f'leads/{lead.pk}/receive/',payload(data,lead)).status_code==409
    Policy.objects.filter(key='warehouse_receipt_eligibility').delete()
    assert not client.get(f'/api/v1/reconciliation/{lead.pk}/').data['can_edit']
    assert post(client,f'leads/{lead.pk}/receive/',payload(data,lead)).status_code==409
    assert not StockMovement.objects.exists()


@pytest.mark.django_db
def test_warehouse_manager_records_unload_before_reconciliation(data,client_for):
    lead,bill=prepared(data)
    load=data['load'];load.state='on_route';load.returned_at=None;load.unloaded_at=None;load.eligibility_reference='';load.save()
    client=client_for(data['users'][Role.WAREHOUSE])
    assert not client.get(f'/api/v1/reconciliation/{lead.pk}/').data['can_edit']
    response=post(client,f'loads/{load.pk}/unload/',{'version':load.version,'reference':'Gate pass 481'},str(uuid.uuid4()))
    assert response.status_code==200,response.data
    load.refresh_from_db()
    assert load.state=='returned' and load.returned_at and load.unloaded_at
    assert load.eligibility_reference=='Gate pass 481'
    assert client.get(f'/api/v1/reconciliation/{lead.pk}/').data['can_edit']
    assert AuditEvent.objects.filter(resource=str(load.pk),action='load.unloaded').exists()


@pytest.mark.django_db
def test_paid_closed_lead_still_receivable(data,client_for):
    lead,bill=prepared(data)
    with transaction.atomic():
        actor=data['users'][Role.COLLECTOR]
        pickups.advance(actor,lead,'sent',{'version':lead.version})
        pickups.advance(actor,lead,'agree',{'version':lead.version})
        billing.pay(actor,bill,{'version':bill.version,'method':'UPI','amount':str(bill.total),'reference':'Synthetic external payment'})
    lead.refresh_from_db();assert lead.state=='closed'
    client=client_for(data['users'][Role.WAREHOUSE])
    queue=client.get('/api/v1/reconciliation-queue/')
    assert queue.status_code==200 and queue.data['count']==1
    assert queue.data['results'][0]['id']==str(lead.pk)
    assert client.get(f'/api/v1/reconciliation/{lead.pk}/').data['can_edit']
    assert post(client,f'leads/{lead.pk}/receive/',payload(data,lead)).status_code==200
    lead.refresh_from_db();assert lead.state=='closed'
    assert client.get('/api/v1/reconciliation-queue/').data['count']==0


@pytest.mark.django_db
def test_receipt_failure_rolls_back_draft_and_stock(data,client_for):
    lead,_=prepared(data);client=client_for(data['users'][Role.WAREHOUSE])
    post(client,f'leads/{lead.pk}/save-reconciliation/',payload(data,lead,('9',)))
    lead.refresh_from_db();before_audit=AuditEvent.objects.count();before_outbox=Outbox.objects.count()
    original=StockMovement.objects.create;calls=0
    def fail_second(**kwargs):
        nonlocal calls
        calls+=1
        if calls==2:raise RuntimeError('Injected movement failure')
        return original(**kwargs)
    with patch('core.services.warehouse.StockMovement.objects.create',side_effect=fail_second):
        with pytest.raises(RuntimeError):post(client,f'leads/{lead.pk}/receive/',payload(data,lead))
    receipt=Receipt.objects.get();assert receipt.submitted_at is None and len(receipt.lines)==1
    assert not StockMovement.objects.exists() and not StockBalance.objects.exists()
    assert AuditEvent.objects.count()==before_audit and Outbox.objects.count()==before_outbox


@pytest.mark.django_db
def test_opposing_lead_discrepancies_remain_flagged(data,client_for):
    client=client_for(data['users'][Role.WAREHOUSE])
    for i,actual in enumerate(('9','11')):
        lead,_=prepared(data,index=i,quantities=('10',))
        assert post(client,f'leads/{lead.pk}/receive/',payload(data,lead,(actual,))).status_code==200
    assert Receipt.objects.filter(flagged=True).count()==2
    assert sum(Decimal(receipt.lines[0]['difference']) for receipt in Receipt.objects.all())==0


@pytest.mark.django_db(transaction=True)
def test_two_warehouse_operators_cannot_receive_same_lead_twice():
    data=synthetic_records(policies=True);lead,_=prepared(data,quantities=('10',))
    first=data['users'][Role.WAREHOUSE]
    second=User.objects.create(username='second-counter',email='second-counter@example.test',role=Role.WAREHOUSE,franchise=first.franchise,warehouse=first.warehouse)
    body=payload(data,lead,('9',))
    def submit(actor):
        close_old_connections()
        try:
            def run(current):
                locked=Lead.objects.select_for_update().get(pk=lead.pk)
                receipt=warehouse.receive(current,locked,body)
                return {'id':str(receipt.pk)}
            return command(actor,str(uuid.uuid4()),'receipt.submit',body,run)
        except Conflict:return None
        finally:close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(submit,[first,second]))
    assert sum(result is not None for result in results)==1
    assert StockMovement.objects.count()==1 and Receipt.objects.count()==1
    assert StockBalance.objects.get().quantity==9
