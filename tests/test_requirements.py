import uuid
from unittest.mock import patch
from decimal import Decimal
import pytest
from django.utils import timezone
from core.models import Role, Requirement, StockBalance, StockMovement, Payment, Receipt, AuditEvent, Outbox
from test_workflows import post


def body(data):
    return {'franchise':str(data['books'][0].pk),'product':str(data['products'][0].pk),'quantity':'10','due_date':timezone.localdate().isoformat(),'note':'Material needed at HQ'}

@pytest.mark.django_db
def test_create_partial_release_fulfil_shared_progress(data,client_for):
    hq=client_for(data['users'][Role.HQ]);wm=client_for(data['users'][Role.WAREHOUSE]);key=str(uuid.uuid4())
    response=post(hq,'requirements/',body(data),key)
    assert response.status_code==201,response.data
    assert post(hq,'requirements/',body(data),key).data==response.data
    assert Requirement.objects.count()==1 and not StockMovement.objects.exists() and not StockBalance.objects.exists()
    assert not Payment.objects.exists() and not Receipt.objects.exists()
    pk=response.data['id'];url=f'requirements/{pk}/release/'
    StockBalance.objects.create(warehouse=data['warehouses'][0],product=data['products'][0],quantity=12)
    key=str(uuid.uuid4());payload={'version':1,'quantity':'4'}
    partial=post(wm,url,payload,key)
    assert partial.status_code==200 and Decimal(partial.data['remaining'])==6
    assert post(wm,url,payload,key).data==partial.data
    shared=hq.get(f'/api/v1/requirements/{pk}/').data
    assert shared['released']==partial.data['released'] and shared['remaining']==partial.data['remaining']
    assert post(hq,f'requirements/{pk}/cancel/',{'version':partial.data['version']}).status_code==409
    assert post(wm,url,{'version':1,'quantity':'1'}).status_code==409
    fulfilled=post(wm,url,{'version':partial.data['version'],'quantity':'6'})
    assert fulfilled.status_code==200 and fulfilled.data['state']=='fulfilled' and Decimal(fulfilled.data['remaining'])==0
    assert StockBalance.objects.get().quantity==2 and StockMovement.objects.count()==2
    assert not Payment.objects.exists() and not Receipt.objects.exists()

@pytest.mark.django_db
def test_cancel_unreleased_and_reject_warehouse_cancellation(data,client_for):
    hq=client_for(data['users'][Role.HQ]);wm=client_for(data['users'][Role.WAREHOUSE])
    result=post(hq,'requirements/',body(data)).data;pk=result['id']
    assert post(wm,f'requirements/{pk}/cancel/',{'version':1}).status_code==403
    assert post(hq,f'requirements/{pk}/cancel/',{'version':1}).status_code==200
    assert post(wm,f'requirements/{pk}/release/',{'version':2,'quantity':'1'}).status_code==409
    assert not StockMovement.objects.exists()

@pytest.mark.django_db
@pytest.mark.parametrize('field,value',[('franchise','bad'),('product',str(uuid.uuid4())),('quantity','0'),('quantity','-1'),('quantity','NaN'),('quantity','1.0001'),('due_date','bad'),('note','x'*1001)])
def test_invalid_requirements_do_not_create_records(data,client_for,field,value):
    payload=body(data);payload[field]=value
    assert post(client_for(data['users'][Role.HQ]),'requirements/',payload).status_code==400
    assert not Requirement.objects.exists()

@pytest.mark.django_db
def test_unassigned_material_and_wrong_role(data,client_for):
    data['products'][0].franchises.remove(data['books'][0])
    assert post(client_for(data['users'][Role.HQ]),'requirements/',body(data)).status_code==400
    assert post(client_for(data['users'][Role.WAREHOUSE]),'requirements/',body(data)).status_code==403

@pytest.mark.django_db
def test_release_audit_failure_rolls_back_all_effects(data,client_for):
    hq=client_for(data['users'][Role.HQ]);wm=client_for(data['users'][Role.WAREHOUSE])
    result=post(hq,'requirements/',body(data)).data
    StockBalance.objects.create(warehouse=data['warehouses'][0],product=data['products'][0],quantity=10)
    before=Outbox.objects.count()
    with patch('core.services.common.AuditEvent.objects.create',side_effect=RuntimeError('Injected audit failure')):
        with pytest.raises(RuntimeError):post(wm,f"requirements/{result['id']}/release/",{'version':1,'quantity':'4'})
    assert StockBalance.objects.get().quantity==10 and Requirement.objects.get().released==0
    assert not StockMovement.objects.exists() and Outbox.objects.count()==before

@pytest.mark.django_db
def test_warehouse_cannot_read_or_release_other_franchise_request(data,client_for):
    hq=client_for(data['users'][Role.HQ]);wm=client_for(data['users'][Role.WAREHOUSE])
    payload=body(data);payload['franchise']=str(data['books'][1].pk)
    result=post(hq,'requirements/',payload)
    assert result.status_code==201
    pk=result.data['id']
    assert wm.get(f'/api/v1/requirements/{pk}/').status_code==404
    assert post(wm,f'requirements/{pk}/release/',{'version':1,'quantity':'1'}).status_code==404
    assert wm.get('/api/v1/requirements/').data['count']==0
    assert not StockMovement.objects.exists()
