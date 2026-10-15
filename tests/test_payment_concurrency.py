import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections

from core.models import Bill, Customer, Payment, Role
from core.services import billing
from core.services.common import Conflict, command


@pytest.mark.django_db(transaction=True)
def test_concurrent_final_payments_allow_only_one_settlement(data):
    book = data['books'][0]
    customer = Customer.objects.filter(franchise=book).first()
    actor = data['users'][Role.APP]
    other = data['users'][Role.ADMIN]
    bill = Bill.objects.create(
        franchise=book, creator=actor, customer=customer, kind='sale',
        state='issued', subtotal=100, total=100, snapshot={'issuer': 'Synthetic'},
    )
    barrier = Barrier(2)

    def pay_one(user):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            current = Bill.objects.get(pk=bill.pk)
            payload = {'version': current.version, 'amount': '100', 'method': 'UPI', 'reference': str(uuid.uuid4())}
            return command(user, str(uuid.uuid4()), 'bill.pay', payload,
                           lambda current_user: {'id': str(billing.pay(current_user, current, payload).pk)})
        except Conflict:
            return None
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(pay_one, (actor, other)))
    bill.refresh_from_db()
    assert sum(result is not None for result in results) == 1
    assert bill.settled == 100 and bill.state == 'completed'
    assert Payment.objects.filter(bill=bill).count() == 1
