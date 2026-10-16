import uuid
from decimal import Decimal
import pytest
from django.db import transaction
from django.utils import timezone
from core.models import *
from core.policy import PolicyUnavailable,policy
from core.services.common import command
from core.services import billing,cash,pickups,warehouse

def post(client,url,data=None,key=None):return client.post('/api/v1/'+url,data or {},format='json',HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))

def issue_pickup(data):
    user=data['users'][Role.COLLECTOR];lead=data['leads'][0]
    with transaction.atomic():
        bill=pickups.start(user,lead,{'version':lead.version})
        bill=billing.save_draft(user,{'version':bill.version,'lines':[{'product':str(data['products'][0].pk),'quantity':'10'}]},bill)
        bill=billing.issue(user,bill,{'version':bill.version})
        lead.refresh_from_db();pickups.advance(user,lead,'sent',{'version':lead.version});pickups.advance(user,lead,'agree',{'version':lead.version})
    return bill

@pytest.mark.django_db
def test_nine_roles_and_scope(data,client_for):
    for role,user in data['users'].items():
        client=client_for(user)
        assert client.get('/api/v1/auth/me/').status_code==200
        assert client.get('/api/v1/audit/').status_code==(200 if role in (Role.SUPER,Role.HQ) else 403)
        assert client.get('/api/v1/settings/').status_code==(200 if role==Role.SUPER else 403)
    user=data['users'][Role.COLLECTOR];c=client_for(user)
    assert c.get('/api/v1/bills/').status_code==403
    other=Lead.objects.create(franchise=data['books'][1],customer=Customer.objects.create(franchise=data['books'][1],name='Other',phone='9000000999'),pickup_date=timezone.localdate(),pickup_time='09:00',state='unassigned')
    assert c.get(f'/api/v1/leads/{other.id}/').status_code==404

@pytest.mark.django_db
def test_fixture_constraints(data):
    assert len(data['users'])==9
    bad=User(username='invalid',email='invalid@example.test',role=Role.STAFF,franchise=data['books'][0])
    with pytest.raises(Exception):bad.full_clean()
    assert all(not u.has_usable_password() for u in data['users'].values())

@pytest.mark.django_db
def test_draft_issue_snapshot_payment_and_receipt(data,client_for):
    bill=issue_pickup(data);u=data['users'][Role.COLLECTOR];hq=data['users'][Role.HQ];wm=data['users'][Role.WAREHOUSE]
    assert bill.total==280
    assert not StockMovement.objects.exists()
    Rate.objects.create(product=data['products'][0],kind='collection',amount=99,actor=hq)
    with transaction.atomic():cash.float_to(hq,u,{'amount':500})
    c=client_for(u);key=str(uuid.uuid4());payload={'amount':'280','method':'Cash','version':bill.version}
    r=post(c,f'pickups/{bill.id}/pay/',payload,key);assert r.status_code==200,r.data
    retry=post(c,f'pickups/{bill.id}/pay/',payload,key);assert retry.status_code==200
    bill.refresh_from_db();assert bill.state=='completed' and bill.lines[0]['rate']=='28.00'
    assert Payment.objects.count()==1 and DriverAccount.objects.get(driver=u).balance==220
    assert not StockMovement.objects.exists()
    load=data['load'];load.unloaded_at=timezone.now();load.returned_at=timezone.now();load.state='returned';load.eligibility_reference='Synthetic test evidence';load.save()
    lead=bill.lead;lead.refresh_from_db()
    w=client_for(wm);key=str(uuid.uuid4());payload={'version':lead.version,'lines':[{'product':str(data['products'][0].id),'actual':'9'}]}
    r=post(w,f'leads/{lead.id}/receive/',payload,key);assert r.status_code==200,r.data
    assert post(w,f'leads/{lead.id}/receive/',payload,key).status_code==200
    assert StockBalance.objects.get(product=data['products'][0]).quantity==9 and StockMovement.objects.count()==1
    bill.refresh_from_db();assert bill.total==280 and bill.state=='completed'

@pytest.mark.django_db
def test_policy_gate_in_production(data,settings):
    settings.ENVIRONMENT='production'
    with pytest.raises(PolicyUnavailable):policy('billing_issue')

@pytest.mark.django_db
def test_cash_handover_once(data,client_for):
    u=data['users'][Role.COLLECTOR];hq=data['users'][Role.HQ];wm=data['users'][Role.WAREHOUSE]
    with transaction.atomic():cash.float_to(hq,u,{'amount':'500'})
    c=client_for(wm);row=DriverAccount.objects.get(driver=u);load=data['load']
    payload={'action':'count','driver':u.id,'received':'0','note':'No cash returned','routes':[str(load.id)],'version':row.version}
    assert post(c,'driver-cash/',payload).status_code==409
    load.state='returned';load.returned_at=timezone.now();load.eligibility_reference='Synthetic';load.save()
    payload['version']=DriverAccount.objects.get(driver=u).version
    key=str(uuid.uuid4());r=post(c,'driver-cash/',payload,key);assert r.status_code==200,r.data
    assert post(c,'driver-cash/',payload,key).status_code==200
    h=Handover.objects.get();assert h.received==0 and h.expected==500 and h.state=='awaiting_supervisor'
    assert CashMovement.objects.filter(kind='handover').count()==1
    assert DriverAccount.objects.get(driver=u).balance==500

@pytest.mark.django_db
def test_assignment_boundary_and_replay_conflict(data,client_for):
    hq=data['users'][Role.HQ];c=client_for(hq);lead=data['leads'][0]
    key=str(uuid.uuid4());payload={'driver':data['users'][Role.DRIVER].id,'version':lead.version}
    assert post(c,f'leads/{lead.id}/assign/',payload,key).status_code==200
    payload['driver']=data['users'][Role.COLLECTOR].id
    assert post(c,f'leads/{lead.id}/assign/',payload,key).status_code==409
    assert post(client_for(data['users'][Role.WAREHOUSE]),f'leads/{lead.id}/assign/',payload).status_code==403

@pytest.mark.parametrize('b,a,flag,pct',[(100,102,False,2),(100,98,False,-2),(100,102.001,True,None),(100,97.999,True,None),(100,0,True,-100),(0,1,True,None),(0,0,False,None)])
def test_weight_thresholds(b,a,flag,pct):
    result=warehouse.compare(Decimal(str(b)),Decimal(str(a)))
    assert result['flagged']==flag
    if pct is not None:assert Decimal(result['percent'])==pct
    if not b:assert result['percent'] is None

@pytest.mark.django_db
def test_request_release_bounds(data,client_for):
    hq=data['users'][Role.HQ];wm=data['users'][Role.WAREHOUSE];p=data['products'][0];book=data['books'][0]
    req=Requirement.objects.create(franchise=book,product=p,actor=hq,quantity=10,due_date=timezone.localdate())
    assert not StockMovement.objects.exists()
    StockBalance.objects.create(warehouse=data['warehouses'][0],product=p,quantity=5)
    c=client_for(wm)
    assert post(c,f'requirements/{req.id}/release/',{'version':1,'quantity':6}).status_code==409
    assert post(c,f'requirements/{req.id}/release/',{'version':1,'quantity':3}).status_code==200
    req.refresh_from_db();assert req.released==3 and StockBalance.objects.get(product=p).quantity==2
    assert StockMovement.objects.count()==1

@pytest.mark.django_db
def test_revoke_session(data,client_for):
    user=data['users'][Role.COLLECTOR];c=client_for(user)
    user.permission_version+=1;user.save()
    assert c.get('/api/v1/leads/').status_code==401

@pytest.mark.django_db
def test_login_csrf_required(data):
    from rest_framework.test import APIClient
    c=APIClient(enforce_csrf_checks=True)
    assert c.post('/api/v1/auth/login/',{'email':'a','password':'b'}).status_code==403

@pytest.mark.django_db
def test_financial_rollback(data):
    user=data['users'][Role.COLLECTOR]
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            cash.float_to(data['users'][Role.HQ],user,{'amount':'200'});raise RuntimeError('injected')
    assert not CashMovement.objects.exists() and not Outbox.objects.exists()
