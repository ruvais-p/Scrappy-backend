import uuid
from decimal import Decimal
from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import Handover, HandoverRoute, Load, Policy, Role, Warehouse


def handover(data,state='awaiting_supervisor',warehouse=None):
    warehouse=warehouse or data['warehouses'][0]
    h=Handover.objects.create(driver=data['users'][Role.COLLECTOR],franchise=data['books'][0],
        counter=data['users'][Role.WAREHOUSE],expected=100,received=80,difference=-20,
        note='Synthetic history',state=state,cutoff=timezone.now())
    for _ in range(2):
        load=Load.objects.create(driver=h.driver,franchise=h.franchise,warehouse=warehouse,
            state='returned',returned_at=timezone.now(),eligibility_reference='Synthetic history')
        HandoverRoute.objects.create(handover=h,load=load)
    return h


@pytest.mark.django_db
def test_multiple_routes_link_one_canonical_history_row(data,client_for):
    h=handover(data)
    client=client_for(data['users'][Role.WAREHOUSE])
    history=client.get('/api/v1/handovers/')
    assert history.status_code==200
    assert history.data['count']==1
    assert Decimal(history.data['summary']['expected'])==100
    assert Decimal(history.data['summary']['received'])==80
    assert Decimal(history.data['summary']['difference'])==-20
    row=history.data['results'][0]
    assert len(row['included_routes'])==2 and row['decision_actions']==[]
    assert row['created_at'] and row['cutoff']
    for route in row['included_routes']:
        detail=client.get(f'/api/v1/loads/{route}/').data
        assert detail['handover']==str(h.pk)
        assert detail['settlement_state']=='awaiting_supervisor'
        assert detail['included_in_handover'] is True
    routes=client.get('/api/v1/loads/',{'settlement_state':'awaiting_supervisor'}).data
    assert routes['count']==2
    assert client.get('/api/v1/loads/',{'settlement_state':'invalid'}).status_code==400
    filtered=client.get('/api/v1/handovers/',{'state':'verified'}).data
    assert filtered['count']==0 and filtered['summary']['received']=='0'


@pytest.mark.django_db
@pytest.mark.parametrize('state',['awaiting_supervisor','verified','disputed'])
def test_saved_settlement_states_are_read_only(data,client_for,state):
    h=handover(data,state)
    for role in (Role.WAREHOUSE,Role.HQ,Role.APP,Role.COLLECTOR):
        client=client_for(data['users'][role])
        response=client.get(f'/api/v1/handovers/{h.pk}/')
        assert response.status_code==200 and response.data['state']==state
        for action in ('verify','dispute','reverse','approve'):
            response=client.post(f'/api/v1/handovers/{h.pk}/{action}/',{},format='json',
                                 HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
            assert response.status_code==403
        assert client.patch(f'/api/v1/handovers/{h.pk}/',{'state':'verified'}).status_code==405
    h.refresh_from_db()
    assert h.state==state


@pytest.mark.django_db
def test_warehouse_and_driver_history_boundaries(data,client_for):
    other=Warehouse.objects.create(franchise=data['books'][0],name='Other yard')
    foreign=handover(data,warehouse=other)
    local=handover(data)
    client=client_for(data['users'][Role.WAREHOUSE])
    result=client.get('/api/v1/handovers/').data
    assert result['count']==1 and result['results'][0]['id']==str(local.pk)
    assert client.get(f'/api/v1/handovers/{foreign.pk}/').status_code==404
    assert client_for(data['users'][Role.DRIVER]).get(f'/api/v1/handovers/{local.pk}/').status_code==404
    assert client_for(data['users'][Role.HQ]).get('/api/v1/handovers/').data['count']==2


@pytest.mark.django_db
def test_count_review_states_and_policy_gate(data,client_for):
    client=client_for(data['users'][Role.WAREHOUSE])
    response=client.get('/api/v1/driver-cash/').data['results'][0]
    assert not response['count_available'] and response['on_route']
    assert response['count_blocked_reason']=='Driver still out.'
    route=data['load']
    route.state='returned';route.returned_at=timezone.now();route.save()
    response=client.get('/api/v1/driver-cash/').data['results'][0]
    assert not response['count_available'] and 'evidence' in response['count_blocked_reason']
    route.eligibility_reference='Synthetic return';route.save()
    assert client.get('/api/v1/driver-cash/').data['results'][0]['count_available']
    detail=client.get(f'/api/v1/loads/{route.pk}/').data
    assert detail['settlement_state']=='count_pending' and detail['handover'] is None
    Policy.objects.filter(key='route_eligibility').delete()
    assert not client.get('/api/v1/driver-cash/').data['results'][0]['count_available']


@pytest.mark.django_db
def test_period_filter_is_saved_count_time_not_current_balance(data,client_for):
    handover(data)
    tomorrow=(timezone.localdate()+timedelta(days=1)).isoformat()
    response=client_for(data['users'][Role.HQ]).get('/api/v1/handovers/',{'from':tomorrow})
    assert response.data['count']==0 and response.data['summary']['received']=='0'
    assert response.data['date_basis']=='created_at'


@pytest.mark.django_db
def test_warehouse_review_does_not_disclose_foreign_route_ids(data,client_for):
    other=Warehouse.objects.create(franchise=data['books'][0],name='Other yard')
    foreign=Load.objects.create(driver=data['users'][Role.COLLECTOR],franchise=data['books'][0],warehouse=other)
    result=client_for(data['users'][Role.WAREHOUSE]).get('/api/v1/driver-cash/').data['results'][0]
    assert str(foreign.pk) not in result['routes']
    assert not result['count_available']
