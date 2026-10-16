import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import CashMovement, DriverAccount, Handover, HandoverRoute, Load, Role
from core.services import cash


def returned(data, load=None):
    load = load or Load.objects.create(driver=data['users'][Role.COLLECTOR],
                                      franchise=data['books'][0], warehouse=data['warehouses'][0])
    load.state='returned'
    load.returned_at=timezone.now()
    load.eligibility_reference='Synthetic return evidence'
    load.save()
    return load


def float_cash(data, amount):
    with transaction.atomic():
        cash.float_to(data['users'][Role.HQ], data['users'][Role.COLLECTOR], {'amount':amount})


def count(client, data, routes, received, key=None):
    driver=data['users'][Role.COLLECTOR]
    return client.post('/api/v1/driver-cash/', {
        'action':'count','driver':driver.pk,'routes':[str(route.pk) for route in routes],
        'version':DriverAccount.objects.get(driver=driver).version,
        'received':received,'note':'Synthetic cash discrepancy',
    }, format='json', HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))


@pytest.mark.django_db
def test_carried_cash_multiple_routes_and_cutoff(data, client_for):
    client=client_for(data['users'][Role.WAREHOUSE])
    driver=data['users'][Role.COLLECTOR]
    first=returned(data,data['load'])
    float_cash(data,'500')
    response=count(client,data,[first],'300')
    assert response.status_code==200, response.data
    old=Handover.objects.get(pk=response.data['id'])
    assert old.expected==500 and old.difference==-200

    second,third=returned(data),returned(data)
    float_cash(data,'100')
    response=count(client,data,[second,third],'250')
    assert response.status_code==200, response.data
    handover=Handover.objects.get(pk=response.data['id'])
    assert handover.expected==300 and handover.received==250 and handover.difference==-50
    assert set(response.data['included_routes'])=={str(second.pk),str(third.pk)}
    assert HandoverRoute.objects.filter(handover=handover).count()==2
    assert CashMovement.objects.filter(handover=handover).count()==1
    assert DriverAccount.objects.get(driver=driver).balance==50
    assert CashMovement.objects.filter(driver=driver,created_at__lte=handover.cutoff).aggregate(
        total=Sum('amount'))['total']==handover.expected
    assert CashMovement.objects.get(handover=handover).created_at>handover.cutoff
    assert count(client,data,[second,third],'250').status_code==409
    assert CashMovement.objects.filter(kind='handover').aggregate(total=Sum('amount'))['total']==-550
    float_cash(data,'75')
    handover.refresh_from_db();old.refresh_from_db()
    assert handover.expected==300 and old.expected==500
    assert DriverAccount.objects.get(driver=driver).balance==125


@pytest.mark.django_db
@pytest.mark.parametrize('invalid_routes',['duplicate','missing','foreign'])
def test_exact_route_membership_required(data,client_for,invalid_routes):
    first=returned(data,data['load']);second=returned(data)
    float_cash(data,'100')
    routes={'duplicate':[first,first,second], 'missing':[first],
            'foreign':[first,Load(id=uuid.uuid4())]}[invalid_routes]
    response=count(client_for(data['users'][Role.WAREHOUSE]),data,routes,'100')
    assert response.status_code==409
    assert not Handover.objects.exists()
    assert not CashMovement.objects.filter(kind='handover').exists()


@pytest.mark.django_db
def test_projection_mismatch_blocks_count(data,client_for):
    route=returned(data,data['load']);float_cash(data,'100')
    DriverAccount.objects.filter(driver=data['users'][Role.COLLECTOR]).update(balance=120)
    response=count(client_for(data['users'][Role.WAREHOUSE]),data,[route],'120')
    assert response.status_code==409
    assert not Handover.objects.exists()


@pytest.mark.django_db
def test_snapshot_and_route_membership_cannot_be_rewritten(data,client_for):
    route=returned(data,data['load']);float_cash(data,'100')
    response=count(client_for(data['users'][Role.WAREHOUSE]),data,[route],'80')
    assert response.status_code==200
    handover=Handover.objects.get()
    for changes in ({'expected':Decimal('200')},{'received':Decimal('90')},
                    {'difference':Decimal('0')},{'cutoff':timezone.now()},{'note':'Rewritten'}):
        with pytest.raises(IntegrityError),transaction.atomic():
            Handover.objects.filter(pk=handover.pk).update(**changes)
    with pytest.raises(IntegrityError),transaction.atomic():
        HandoverRoute.objects.filter(handover=handover).delete()
    with pytest.raises(IntegrityError),transaction.atomic():
        HandoverRoute.objects.create(handover=handover,load=route)


@pytest.mark.django_db
def test_count_audit_failure_rolls_back_every_effect(data,client_for):
    route=returned(data,data['load']);float_cash(data,'100')
    client=client_for(data['users'][Role.WAREHOUSE])
    with patch('core.services.cash.event',side_effect=RuntimeError('audit unavailable')):
        with pytest.raises(RuntimeError,match='audit unavailable'):
            count(client,data,[route],'100')
    assert not Handover.objects.exists() and not HandoverRoute.objects.exists()
    assert not CashMovement.objects.filter(kind='handover').exists()
    assert DriverAccount.objects.get(driver=data['users'][Role.COLLECTOR]).balance==100
