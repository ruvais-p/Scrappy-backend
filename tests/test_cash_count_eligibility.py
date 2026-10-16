import uuid
from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from core.models import CashMovement, DriverAccount, Handover, Load, Policy, Role, Warehouse
from core.services import cash


@pytest.fixture
def count_setup(data, client_for):
    driver=data['users'][Role.COLLECTOR]
    with transaction.atomic():
        cash.float_to(data['users'][Role.HQ],driver,{'amount':'100'})
    load=data['load']
    load.state='returned';load.returned_at=timezone.now()
    load.eligibility_reference='Synthetic authorized return';load.save()
    payload={'action':'count','driver':driver.pk,'routes':[str(load.pk)],
             'version':DriverAccount.objects.get(driver=driver).version,'received':'100','note':''}
    return client_for(data['users'][Role.WAREHOUSE]),payload


def submit(setup, **changes):
    client,payload=setup
    return client.post('/api/v1/driver-cash/',{**payload,**changes},format='json',
                       HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))


def unchanged(data):
    assert not Handover.objects.exists()
    assert not CashMovement.objects.filter(kind='handover').exists()
    assert DriverAccount.objects.get(driver=data['users'][Role.COLLECTOR]).balance==100


@pytest.mark.django_db
@pytest.mark.parametrize('received',[None,'', '-1','1.001','NaN'])
def test_invalid_or_missing_received_is_not_zero(data,count_setup,received):
    assert submit(count_setup,received=received).status_code==400
    unchanged(data)


@pytest.mark.django_db
def test_omitted_received_is_rejected(data,count_setup):
    del count_setup[1]['received']
    assert submit(count_setup).status_code==400
    unchanged(data)


@pytest.mark.django_db
@pytest.mark.parametrize('note',[None,123,{},[], '', '  ', 'x'*1001])
def test_discrepancy_requires_valid_note(data,count_setup,note):
    assert submit(count_setup,received='0',note=note).status_code==400
    unchanged(data)


@pytest.mark.django_db
@pytest.mark.parametrize('received,note',[('0','No cash returned'),('100',''),('120','Extra cash received')])
def test_valid_counts_preserve_difference_and_await_supervisor(data,count_setup,received,note):
    response=submit(count_setup,received=received,note=note)
    assert response.status_code==200,response.data
    h=Handover.objects.get()
    assert h.received==int(received) and h.difference==int(received)-100
    assert h.state=='awaiting_supervisor' and h.note==note
    assert CashMovement.objects.get(handover=h).amount==-int(received)


@pytest.mark.django_db
@pytest.mark.parametrize('field,value',[
    ('state','on_route'),('state','unknown'),('returned_at',None),
    ('eligibility_reference',''),('eligibility_reference','   '),
])
def test_return_evidence_required(data,count_setup,field,value):
    Load.objects.filter(pk=data['load'].pk).update(**{field:value})
    count_setup[1]['version']=DriverAccount.objects.get(driver=data['users'][Role.COLLECTOR]).version
    assert submit(count_setup).status_code==409
    unchanged(data)


@pytest.mark.django_db
def test_future_return_is_not_evidence(data,count_setup):
    Load.objects.filter(pk=data['load'].pk).update(returned_at=timezone.now()+timedelta(days=1))
    count_setup[1]['version']=DriverAccount.objects.get(driver=data['users'][Role.COLLECTOR]).version
    assert submit(count_setup).status_code==409
    unchanged(data)


@pytest.mark.django_db
def test_another_active_route_blocks_count(data,count_setup):
    Load.objects.create(driver=data['users'][Role.COLLECTOR],franchise=data['books'][0],
                        warehouse=data['warehouses'][0])
    count_setup[1]['version']=DriverAccount.objects.get(driver=data['users'][Role.COLLECTOR]).version
    response=submit(count_setup)
    assert response.status_code==409 and 'Driver still out' in str(response.data)
    unchanged(data)


@pytest.mark.django_db
def test_other_warehouse_in_same_franchise_is_denied(data,count_setup):
    other=Warehouse.objects.create(franchise=data['books'][0],name='Other warehouse')
    Load.objects.filter(pk=data['load'].pk).update(warehouse=other)
    count_setup[1]['version']=DriverAccount.objects.get(driver=data['users'][Role.COLLECTOR]).version
    assert submit(count_setup).status_code==403
    unchanged(data)


@pytest.mark.django_db
def test_missing_warehouse_assignment_is_denied(data,count_setup):
    manager=data['users'][Role.WAREHOUSE]
    # Persisted accounts already have a database scope constraint; exercise
    # the service guard with an incomplete in-memory actor as well.
    manager.warehouse=None
    with pytest.raises(PermissionDenied):
        cash.count(manager,data['users'][Role.COLLECTOR],count_setup[1])
    unchanged(data)


@pytest.mark.django_db
def test_missing_route_policy_cannot_be_bypassed(data,count_setup):
    Policy.objects.filter(key='route_eligibility').delete()
    assert submit(count_setup).status_code==409
    unchanged(data)


@pytest.mark.django_db
@pytest.mark.parametrize('role',[Role.HQ,Role.APP,Role.COLLECTOR,Role.SUPER])
def test_count_authority_is_warehouse_only(data,count_setup,client_for,role):
    setup=(client_for(data['users'][role]),count_setup[1])
    assert submit(setup).status_code==403
    unchanged(data)
