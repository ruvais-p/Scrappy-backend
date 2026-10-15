from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import uuid
import time
import pytest
from django.db import close_old_connections
from django.db.models import Sum
from rest_framework.test import APIClient
from core.models import EarningsEntry,Role

@pytest.mark.django_db
def test_digester_statement_is_own_franchise_and_staff_is_denied(data,client_for):
    digester=data['users'][Role.DIGESTER]
    other=data['books'][3]
    EarningsEntry.objects.create(franchise=digester.franchise,actor=digester,amount=125,kind='sale')
    EarningsEntry.objects.create(franchise=other,actor=digester,amount=999,kind='sale')
    response=client_for(digester).get('/api/v1/payouts/')
    assert response.status_code==200
    assert Decimal(response.data['balance'])==125
    assert len(response.data['results'])==1
    assert response.data['can_payout'] is False
    assert client_for(data['users'][Role.STAFF]).get('/api/v1/payouts/').status_code==403

@pytest.mark.django_db
def test_head_office_can_record_payout_but_cannot_exceed_statement(data,client_for):
    admin=data['users'][Role.ADMIN];book=data['books'][2]
    EarningsEntry.objects.create(franchise=book,actor=admin,amount=100,kind='sale')
    response=client_for(admin).get('/api/v1/payouts/',{'franchise':str(book.pk)})
    assert response.status_code==200 and response.data['can_payout'] is True
    assert Decimal(response.data['balance'])==100
    from uuid import uuid4
    payload={'franchise':str(book.pk),'amount':'101','reference':'too much'}
    assert client_for(admin).post('/api/v1/payouts/',payload,format='json',HTTP_IDEMPOTENCY_KEY=str(uuid4())).status_code==409

@pytest.mark.django_db(transaction=True)
def test_concurrent_payouts_cannot_overdraw_earnings(data):
    book=data['books'][2];admin=data['users'][Role.ADMIN];super_admin=data['users'][Role.SUPER]
    EarningsEntry.objects.create(franchise=book,actor=admin,amount=100,kind='sale')

    def submit(actor):
        close_old_connections()
        try:
            client=APIClient();client.force_login(actor)
            session=client.session;session.update({'permission_version':actor.permission_version,'signed_in_at':time.time(),'last_activity':time.time()});session.save()
            return client.post('/api/v1/payouts/', {'franchise':str(book.pk),'amount':'80','reference':'race'}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())).status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(submit,(admin,super_admin)))
    assert sorted(statuses)==[200,409]
    assert EarningsEntry.objects.filter(franchise=book,kind='payout').count()==1
    assert EarningsEntry.objects.filter(franchise=book).aggregate(total=Sum('amount'))['total']==20
