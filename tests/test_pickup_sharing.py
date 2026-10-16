import uuid
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from core.models import AuditEvent, Bill, CashMovement, Payment, Policy, Role
from core.policy import PolicyUnavailable
from core.services import billing, pickups
from core.services.common import Conflict
from test_workflows import post


def issued_pickup(data):
    collector=data['users'][Role.COLLECTOR];lead=data['leads'][0]
    with transaction.atomic():
        bill=pickups.start(collector,lead,{'version':lead.version})
        bill=billing.save_draft(collector,{'version':bill.version,
            'lines':[{'product':str(data['products'][0].pk),'quantity':'1'}]},bill)
        return billing.issue(collector,bill,{'version':bill.version})


@pytest.mark.django_db
def test_whatsapp_open_is_only_an_attempt(data):
    bill=issued_pickup(data);lead=bill.lead;collector=data['users'][Role.COLLECTOR]
    version=lead.version
    pickups.advance(collector,lead,'whatsapp-opened',{'version':version})
    lead.refresh_from_db()
    assert lead.state=='weighing' and lead.sent_at is None and lead.agreed_at is None and lead.version==version
    audit=AuditEvent.objects.filter(resource=str(lead.pk),action='share.whatsapp_opened').latest('created_at')
    assert audit.details['channel']=='whatsapp'


@pytest.mark.django_db
def test_link_opening_does_not_create_agreement(data,client_for):
    bill=issued_pickup(data);collector=data['users'][Role.COLLECTOR]
    response=post(client_for(collector),f'documents/{bill.pk}/share/',{},'sharing-attempt')
    assert response.status_code==200
    lead=bill.lead;lead.refresh_from_db()
    assert lead.sent_at is None and lead.agreed_at is None
    public=client_for(collector)
    public.get(response.data['url'])
    lead.refresh_from_db();assert lead.agreed_at is None


@pytest.mark.django_db
def test_sent_confirmation_requires_policy_and_is_one_time(data):
    bill=issued_pickup(data);lead=bill.lead;collector=data['users'][Role.COLLECTOR]
    Policy.objects.filter(key='customer_sharing').delete()
    with pytest.raises(PolicyUnavailable): pickups.advance(collector,lead,'sent',{'version':lead.version})
    lead.refresh_from_db();assert lead.sent_at is None and lead.agreed_at is None
    Policy.objects.create(key='customer_sharing',config={'confirmation':'link_opened'},decision_reference='Unsupported test')
    with pytest.raises(PolicyUnavailable): pickups.advance(collector,lead,'sent',{'version':lead.version})
    lead.refresh_from_db();assert lead.sent_at is None
    Policy.objects.filter(key='customer_sharing').update(config={'confirmation':'collector_assertion'})
    sent=pickups.advance(collector,lead,'sent',{'version':lead.version});assert sent.sent_at
    with pytest.raises(Conflict): pickups.advance(collector,sent,'sent',{'version':sent.version})


@pytest.mark.django_db
def test_agreement_requires_sent_and_is_explicit(data):
    bill=issued_pickup(data);lead=bill.lead;collector=data['users'][Role.COLLECTOR]
    with pytest.raises(Conflict): pickups.advance(collector,lead,'agree',{'version':lead.version})
    lead.refresh_from_db();assert lead.agreed_at is None
    sent=pickups.advance(collector,lead,'sent',{'version':lead.version})
    agreed=pickups.advance(collector,sent,'agree',{'version':sent.version})
    assert agreed.agreed_at and agreed.state=='agreed'
    with pytest.raises(Conflict): pickups.advance(collector,agreed,'agree',{'version':agreed.version})


@pytest.mark.django_db
def test_payment_cannot_precede_agreement(data,client_for):
    user=data['users'][Role.COLLECTOR];lead=data['leads'][0]
    with transaction.atomic():
        bill=pickups.start(user,lead,{'version':lead.version})
        bill=billing.save_draft(user,{'version':bill.version,'lines':[{'product':str(data['products'][0].pk),'quantity':'1'}]},bill)
        bill=billing.issue(user,bill,{'version':bill.version})
    response=post(client_for(user),f'pickups/{bill.pk}/pay/',{'version':bill.version,'amount':str(bill.total),'method':'UPI','reference':'UTR'})
    assert response.status_code==403
    assert Payment.objects.count()==0 and CashMovement.objects.count()==0


@pytest.mark.django_db
def test_sharing_and_agreement_retries_are_idempotent(data,client_for):
    bill=issued_pickup(data);lead=bill.lead;collector=data['users'][Role.COLLECTOR]
    client=client_for(collector)
    key=str(uuid.uuid4());payload={'version':lead.version}
    first=post(client,f'leads/{lead.pk}/sent/',payload,key)
    retry=post(client,f'leads/{lead.pk}/sent/',payload,key)
    assert first.status_code==200 and retry.status_code==200
    lead.refresh_from_db();key=str(uuid.uuid4());payload={'version':lead.version}
    first=post(client,f'leads/{lead.pk}/agree/',payload,key)
    retry=post(client,f'leads/{lead.pk}/agree/',payload,key)
    assert first.status_code==200 and retry.status_code==200
    assert AuditEvent.objects.filter(resource=str(lead.pk),action='pickup.agree').count()==1
