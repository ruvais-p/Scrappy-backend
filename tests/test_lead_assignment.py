import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from core.models import Customer, Lead, Role, User
from core.services.common import Conflict
from core.services import pickups


def post(client, path, data):
    return client.post('/api/v1/'+path,data,format='json',HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))


@pytest.mark.django_db
def test_app_supervisor_assigns_only_same_franchise_driver(data,client_for):
    actor=data['users'][Role.APP];lead=data['leads'][0]
    response=post(client_for(actor),f'leads/{lead.pk}/assign/',{'version':lead.version,'driver':data['users'][Role.COLLECTOR].pk})
    assert response.status_code==200
    lead.refresh_from_db();assert lead.driver_id==data['users'][Role.COLLECTOR].pk and lead.state=='scheduled'
    foreign=User.objects.filter(role=Role.COLLECTOR,franchise=data['books'][1]).first()
    response=post(client_for(actor),f'leads/{lead.pk}/assign/',{'version':lead.version,'driver':foreign.pk})
    assert response.status_code==400


@pytest.mark.django_db
@pytest.mark.parametrize('action',["reschedule","cancel"])
def test_app_supervisor_can_change_only_unstarted_leads(data,client_for,action):
    actor=data['users'][Role.APP];lead=data['leads'][0]
    payload={'version':lead.version}
    if action=='reschedule':
        payload.update(pickup_date=(timezone.localdate()).isoformat(),pickup_time='14:30')
    response=post(client_for(actor),f'leads/{lead.pk}/{action}/',payload)
    assert response.status_code==200,response.data
    lead.refresh_from_db()
    if action=='cancel': assert lead.state=='cancelled' and lead.failure_reason
    else: assert str(lead.pickup_time)=='14:30:00'


@pytest.mark.django_db
def test_started_lead_cannot_be_rescheduled_or_cancelled(data,client_for):
    lead=data['leads'][0];lead.started_at=timezone.now();lead.state='weighing';lead.save()
    client=client_for(data['users'][Role.HQ])
    for action in ('reschedule','cancel'):
        payload={'version':lead.version}
        if action=='reschedule': payload.update(pickup_date=timezone.localdate().isoformat(),pickup_time='12:00')
        response=post(client,f'leads/{lead.pk}/{action}/',payload)
        assert response.status_code==409
    lead.refresh_from_db();assert lead.state=='weighing'


@pytest.mark.django_db
def test_inactive_franchise_cannot_receive_assignment(data,client_for):
    lead=data['leads'][0];lead.franchise.status='suspended';lead.franchise.save()
    response=post(client_for(data['users'][Role.HQ]),f'leads/{lead.pk}/assign/',
                  {'version':lead.version,'driver':data['users'][Role.COLLECTOR].pk})
    assert response.status_code==409
    lead.refresh_from_db();assert lead.driver_id==data['users'][Role.COLLECTOR].pk


@pytest.mark.django_db(transaction=True)
def test_assignment_and_start_race_has_one_winner(data):
    lead=data['leads'][0];hq=data['users'][Role.HQ];driver=data['users'][Role.COLLECTOR]
    other=User.objects.create(username='second-driver',email='second-driver@example.test',role=Role.DRIVER,franchise=lead.franchise)
    barrier=Barrier(2)
    def run(kind):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if kind=='assign':
                from core.services.common import command
                return command(hq,str(uuid.uuid4()),'lead.assign',{'version':lead.version},
                    lambda actor: str(pickups.assign(actor,Lead.objects.select_for_update().get(pk=lead.pk),{'version':lead.version,'driver':other.pk}).pk))
            from core.services.common import command
            return command(driver,str(uuid.uuid4()),'pickup.start',{'version':lead.version},
                lambda actor: str(pickups.start(actor,Lead.objects.select_for_update().get(pk=lead.pk),{'version':lead.version}).pk))
        except (Conflict,PermissionDenied):return 'conflict'
        finally: close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(run,['assign','start']))
    lead.refresh_from_db()
    assert sum(result!='conflict' for result in results)==1
    assert (lead.driver_id==other.pk and lead.state=='scheduled') or (lead.driver_id==driver.pk and lead.state=='weighing')


@pytest.mark.django_db
def test_assignment_replay_cannot_change_payload(data,client_for):
    lead=data['leads'][0];client=client_for(data['users'][Role.HQ]);key=str(uuid.uuid4())
    payload={'version':lead.version,'driver':data['users'][Role.COLLECTOR].pk}
    assert client.post(f'/api/v1/leads/{lead.pk}/assign/',payload,format='json',HTTP_IDEMPOTENCY_KEY=key).status_code==200
    payload['driver']=data['users'][Role.DRIVER].pk
    assert client.post(f'/api/v1/leads/{lead.pk}/assign/',payload,format='json',HTTP_IDEMPOTENCY_KEY=key).status_code==409
