import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction

from core.models import AuditEvent, Bill, CashMovement, EarningsEntry, Payment, Policy, Role, StockMovement


def post(client, bill, action, data, key=None):
    return client.post(f'/api/v1/bills/{bill.pk}/{action}/', data, format='json',
                       HTTP_IDEMPOTENCY_KEY=key or str(uuid.uuid4()))


@pytest.fixture
def issued(data):
    return Bill.objects.create(franchise=data['books'][0], creator=data['users'][Role.APP],
                               customer=data['leads'][0].customer, kind='collection', state='issued',
                               number='CANCEL-TEST', subtotal=100, total=100,
                               lines=[{'name':'Paper','quantity':'10','rate':'10','amount':'100'}])


@pytest.mark.django_db
def test_request_retains_finances_and_survives_final_payment(data, client_for, issued):
    client=client_for(data['users'][Role.APP])
    original=(issued.lines, issued.snapshot, issued.number, issued.total)
    counts=[model.objects.count() for model in (Payment,CashMovement,EarningsEntry,StockMovement)]
    payload={'version':issued.version,'reason':'Please review this duplicate bill'}
    key=str(uuid.uuid4())
    result=post(client,issued,'request-cancellation',payload,key)
    assert result.status_code==200
    assert post(client,issued,'request-cancellation',payload,key).data==result.data
    issued.refresh_from_db()
    assert issued.state=='cancellation_requested'
    assert (issued.lines,issued.snapshot,issued.number,issued.total)==original
    assert [model.objects.count() for model in (Payment,CashMovement,EarningsEntry,StockMovement)]==counts
    history=result.data['cancellation_requests']
    assert len(history)==1 and history[0]['reason']==payload['reason']
    assert history[0]['status']=='pending'
    paid=post(client,issued,'pay',{'version':issued.version,'amount':'100','method':'Cash'})
    assert paid.status_code==200
    assert paid.data['state']=='cancellation_requested'
    assert Decimal(paid.data['outstanding'])==0
    assert paid.data['cancellation_requests']==history
    summary=client.get('/api/v1/bills/').data['summary']
    assert Decimal(summary['outgoing_total'])==100
    assert Decimal(summary['outgoing_settled'])==100
    issued.refresh_from_db()
    duplicate=post(client,issued,'request-cancellation',{'version':issued.version,'reason':'Again'})
    assert duplicate.status_code==409


@pytest.mark.django_db
@pytest.mark.parametrize('reason',[None,'', '   ',123,{},'x'*501])
def test_invalid_reason_is_rejected(data,client_for,issued,reason):
    result=post(client_for(data['users'][Role.APP]),issued,'request-cancellation',
                {'version':issued.version,'reason':reason})
    assert result.status_code==400
    issued.refresh_from_db()
    assert issued.state=='issued'
    assert not AuditEvent.objects.filter(action='bill.cancellation_requested').exists()


@pytest.mark.django_db
def test_scope_and_undefined_actions_remain_denied(data,client_for,issued):
    foreign=client_for(data['users'][Role.DIGESTER])
    assert foreign.get(f'/api/v1/bills/{issued.pk}/').status_code==404
    assert post(foreign,issued,'request-cancellation',{'version':issued.version,'reason':'Hidden'}).status_code==404
    client=client_for(data['users'][Role.SUPER])
    for action in ('approve-cancellation','reject-cancellation','refund','reverse','correct'):
        assert post(client,issued,action,{'version':issued.version}).status_code==403
    assert post(client,issued,'save',{'version':issued.version,'lines':[]}).status_code==409
    assert client.delete(f'/api/v1/bills/{issued.pk}/').status_code==405
    with pytest.raises(IntegrityError), transaction.atomic():
        Bill.objects.filter(pk=issued.pk).update(lines=[])


@pytest.mark.django_db
def test_request_audit_failure_rolls_back(data,client_for,issued):
    client=client_for(data['users'][Role.APP])
    with patch('core.services.common.AuditEvent.objects.create',side_effect=RuntimeError('audit failed')):
        with pytest.raises(RuntimeError,match='audit failed'):
            post(client,issued,'request-cancellation',{'version':issued.version,'reason':'Review'})
    issued.refresh_from_db()
    assert issued.state=='issued'


@pytest.mark.django_db
def test_b2b_policy_record_cannot_enable_undefined_posting(data,client_for):
    Policy.objects.update_or_create(key='b2b_payment_authority',defaults={
        'decision_reference':'Synthetic test','test_only':True,'config':{}})
    client=client_for(data['users'][Role.APP])
    for state,action in [('draft','issue'),('issued','pay')]:
        bill=Bill.objects.create(franchise=data['books'][0],creator=data['users'][Role.APP],
                                 customer=data['leads'][0].customer,kind='b2b',state=state,total=100)
        result=post(client,bill,action,{'version':bill.version,'amount':'100','method':'Cash'})
        assert result.status_code==409
        bill.refresh_from_db()
        assert bill.state==state and bill.settled==0
