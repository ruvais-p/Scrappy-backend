import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import pytest
from django.db import close_old_connections,transaction
from django.utils import timezone
from core.fixtures import synthetic_records
from core.models import *
from core.services.common import command,Conflict
from core.services import cash,warehouse,pickups,billing
from core.events import allowed
from core.jobs import safe_cell
from test_workflows import issue_pickup,post

@pytest.mark.django_db(transaction=True)
def test_concurrent_releases_preserve_stock():
    d=synthetic_records(policies=True);actor=d['users'][Role.WAREHOUSE]
    req=Requirement.objects.create(franchise=d['books'][0],product=d['products'][0],actor=d['users'][Role.HQ],quantity=10,due_date=timezone.localdate())
    StockBalance.objects.create(warehouse=d['warehouses'][0],product=d['products'][0],quantity=10)
    def send(_):
        close_old_connections()
        try:
            def run(u):
                r=Requirement.objects.select_for_update().get(pk=req.pk)
                warehouse.release(u,r,{'quantity':'7','version':1});return {'ok':True}
            return command(actor,str(uuid.uuid4()),'release',{},run)
        except Conflict:return {'conflict':True}
        finally:close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(send,range(2)))
    assert sum(bool(r.get('ok')) for r in results)==1
    assert StockBalance.objects.get().quantity==3 and StockMovement.objects.count()==1

@pytest.mark.django_db(transaction=True)
def test_duplicate_float_commands():
    d=synthetic_records(policies=True);actor=d['users'][Role.HQ];driver=d['users'][Role.COLLECTOR];key=str(uuid.uuid4())
    def send(_):
        close_old_connections()
        try:return command(actor,key,'float',{'amount':'100'},lambda u:{'id':str(cash.float_to(u,driver,{'amount':'100'}).pk)})
        finally:close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(send,range(2)))
    assert results[0]==results[1] and CashMovement.objects.count()==1
    assert DriverAccount.objects.get(driver=driver).balance==100

@pytest.mark.django_db
def test_paid_pdf_and_capability_redaction(data,client_for):
    from core.documents import render_bill
    bill=issue_pickup(data);u=data['users'][Role.COLLECTOR];c=client_for(u)
    key=str(uuid.uuid4());r=post(c,f'documents/{bill.pk}/share/',{},key);assert r.status_code==200,r.data
    url=r.data['url'];assert post(c,f'documents/{bill.pk}/share/',{},key).data['url']==url
    assert CustomerLink.objects.count()==1
    assert url.split('/')[-2] not in str(CommandResult.objects.get(key=key).result)
    before=c.get(url);assert before.status_code==200 and before['Content-Type']=='application/pdf'
    r=post(c,f'pickups/{bill.pk}/pay/',{'version':bill.version,'amount':'280','method':'UPI','reference':'PRIVATE-REFERENCE'})
    assert r.status_code==200,r.data
    bill.refresh_from_db();after=c.get(url);assert after.status_code==200 and before.content!=after.content
    assert b'PRIVATE-REFERENCE' not in after.content
    CustomerLink.objects.update(revoked=True);assert c.get(url).status_code==404
    assert not CashMovement.objects.exists()

@pytest.mark.django_db
def test_event_scope_and_ownership(data):
    c=data['users'][Role.COLLECTOR];other=User.objects.filter(franchise=data['books'][1],role=Role.COLLECTOR).first()
    event={'feature':'leads','franchise_id':str(c.franchise_id),'owner_id':c.pk,'warehouse_id':None}
    assert allowed(c,event) and not allowed(other,event)
    assert allowed(data['users'][Role.HQ],event)
    event['feature']='audit';assert not allowed(data['users'][Role.ADMIN],event)

@pytest.mark.django_db
def test_warehouse_zero_and_missing(data):
    bill=issue_pickup(data);lead=bill.lead;load=data['load'];load.unloaded_at=timezone.now();load.state='returned';load.returned_at=timezone.now();load.eligibility_reference='Synthetic';load.save();lead.refresh_from_db()
    from django.core.exceptions import ValidationError
    with transaction.atomic():
        with pytest.raises(ValidationError):warehouse.receive(data['users'][Role.WAREHOUSE],lead,{'version':lead.version,'lines':[]})
        result=warehouse.receive(data['users'][Role.WAREHOUSE],lead,{'version':lead.version,'lines':[{'product':str(data['products'][0].id),'actual':'0'}]})
    assert result.flagged and not StockMovement.objects.exists()

@pytest.mark.parametrize('text',['=1+1',' +SUM(A1)','@cmd','\t=2','-1+3'])
def test_csv_formula_defense(text):assert safe_cell(text).startswith("'")

@pytest.mark.django_db
def test_staff_customer_does_not_include_financial_history(data,client_for):
    c=client_for(data['users'][Role.DRIVER]);r=c.get('/api/v1/customers/')
    assert r.status_code==200
    assert all('bills' not in row and 'total' not in row for row in r.data['results'])

@pytest.mark.django_db
def test_policy_dependency_blocks_lock(data):
    lead=data['leads'][0];u=data['users'][Role.COLLECTOR]
    from core.policy import PolicyUnavailable
    with transaction.atomic():
        bill=pickups.start(u,lead,{'version':lead.version});bill=billing.save_draft(u,{'version':bill.version,'lines':[{'product':str(data['products'][0].pk),'quantity':'1'}]},bill)
        Policy.objects.filter(key='billing_issue').delete()
        with pytest.raises(PolicyUnavailable):billing.issue(u,bill,{'version':bill.version})
    bill.refresh_from_db();assert bill.state=='draft' and bill.number is None
