import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from decimal import Decimal

import pytest
from django.db import close_old_connections
from django.utils import timezone
from core.models import Bill, Product, Rate, Role
from core.services import billing, pickups
from core.services.common import Conflict


def post(client,path,data,key=None):
    return client.post('/api/v1/'+path,data,format='json',HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))


def start_payload(lead): return {'version':lead.version}


@pytest.mark.django_db
def test_assigned_start_creates_shared_draft_and_resume_is_idempotent(data,client_for):
    lead=data['leads'][0];collector=data['users'][Role.COLLECTOR];client=client_for(collector)
    first=post(client,f'leads/{lead.pk}/start/',start_payload(lead))
    assert first.status_code==200,first.data
    lead.refresh_from_db();assert lead.state=='weighing' and lead.started_at
    bill_id=first.data['id'];assert Bill.objects.filter(lead=lead).count()==1
    resumed=post(client,f'leads/{lead.pk}/start/',{'version':lead.version})
    assert resumed.status_code==200 and resumed.data['id']==bill_id
    assert Bill.objects.filter(lead=lead).count()==1


@pytest.mark.django_db
def test_unassigned_or_foreign_driver_cannot_start(data,client_for):
    lead=data['leads'][0];lead.driver=None;lead.state='unassigned';lead.save()
    assert post(client_for(data['users'][Role.COLLECTOR]),f'leads/{lead.pk}/start/',{'version':lead.version}).status_code in (403,404)
    foreign=data['users'][Role.DIGESTER]
    lead.driver=foreign;lead.state='scheduled';lead.save()
    assert post(client_for(data['users'][Role.COLLECTOR]),f'leads/{lead.pk}/start/',{'version':lead.version}).status_code in (403,404)


@pytest.mark.django_db
@pytest.mark.parametrize('lines',[[],[{'product':'not-a-uuid','quantity':'1'}],
                                  [{'product':None,'quantity':'1'}]])
def test_empty_or_invalid_pickup_weights_are_rejected(data,client_for,lines):
    lead=data['leads'][0];collector=data['users'][Role.COLLECTOR]
    bill=pickups.start(collector,lead,{'version':lead.version})
    response=post(client_for(collector),f'pickups/{bill.pk}/save/',
                  {'version':bill.version,'lines':lines})
    assert response.status_code==400
    bill.refresh_from_db();assert bill.state=='draft' and bill.lines==[]


@pytest.mark.django_db
def test_pickup_rejects_non_kg_and_zero_weights(data,client_for):
    lead=data['leads'][0];collector=data['users'][Role.COLLECTOR]
    bill=pickups.start(collector,lead,{'version':lead.version})
    product=data['products'][0];product.unit='piece';product.save()
    response=post(client_for(collector),f'pickups/{bill.pk}/save/',
                  {'version':bill.version,'lines':[{'product':product.pk,'quantity':'1'}]})
    assert response.status_code==400
    product.unit='kg';product.save()
    response=post(client_for(collector),f'pickups/{bill.pk}/save/',
                  {'version':bill.version,'lines':[{'product':product.pk,'quantity':'0'}]})
    assert response.status_code==400


@pytest.mark.django_db
def test_rate_change_requires_fresh_pickup_review(data,client_for):
    lead=data['leads'][0];collector=data['users'][Role.COLLECTOR];product=data['products'][0]
    bill=pickups.start(collector,lead,{'version':lead.version})
    saved=post(client_for(collector),f'pickups/{bill.pk}/save/',
               {'version':bill.version,'lines':[{'product':product.pk,'quantity':'2'}]})
    assert saved.status_code==200
    bill.refresh_from_db();Rate.objects.create(product=product,kind='collection',amount=99,actor=data['users'][Role.HQ])
    issued=post(client_for(collector),f'pickups/{bill.pk}/issue/',{'version':bill.version})
    assert issued.status_code==409
    bill.refresh_from_db();assert bill.state=='draft' and bill.number is None


@pytest.mark.django_db(transaction=True)
def test_concurrent_start_creates_one_bill(data):
    lead=data['leads'][0];collector=data['users'][Role.COLLECTOR];barrier=Barrier(2)
    def run(_):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            from core.models import Lead
            from core.services.common import command
            return command(collector,str(uuid.uuid4()),'pickup.start',{'lead':str(lead.pk)},lambda actor: str(
                pickups.start(actor,Lead.objects.select_for_update().get(pk=lead.pk),
                              {'version':Lead.objects.get(pk=lead.pk).version}).pk))
        except Conflict:return 'conflict'
        finally:close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,[1,2]))
    assert Bill.objects.filter(lead=lead).count()==1
    assert len(set(str(r) for r in results))==1


@pytest.mark.django_db
def test_inconsistent_resume_state_is_rejected(data):
    lead=data['leads'][0];lead.state='weighing';lead.started_at=None;lead.save()
    with pytest.raises(Conflict): pickups.start(data['users'][Role.COLLECTOR],lead,{'version':lead.version})
