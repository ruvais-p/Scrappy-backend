import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, transaction

from core.models import Bill, CashMovement, DriverAccount, Payment, Role, StockMovement
from core.policy import PolicyUnavailable
from core.services import billing, cash
from core.services.common import Conflict, command
from test_workflows import issue_pickup


@pytest.mark.django_db
def test_pickup_upi_closes_after_agreement_without_cash_or_stock(data):
    bill = issue_pickup(data)
    driver = data['users'][Role.COLLECTOR]
    payload = {'version': bill.version, 'amount': str(bill.total), 'method': 'UPI', 'reference': 'upi-verified-by-collector'}

    with transaction.atomic():
        billing.pay(driver, bill, payload)

    bill.refresh_from_db()
    assert bill.state == 'completed'
    assert bill.lead.state == 'closed'
    assert Payment.objects.filter(bill=bill, method='UPI').count() == 1
    assert not CashMovement.objects.filter(payment__bill=bill).exists()
    assert not StockMovement.objects.exists()


@pytest.mark.django_db
def test_pickup_cash_requires_balance_and_is_atomic(data):
    bill = issue_pickup(data)
    driver = data['users'][Role.COLLECTOR]
    with transaction.atomic():
        cash.float_to(data['users'][Role.HQ], driver, {'amount': str(bill.total - 1)})

    with pytest.raises(Conflict, match='Insufficient cash'):
        billing.pay(driver, bill, {'version': bill.version, 'amount': str(bill.total), 'method': 'Cash'})

    bill.refresh_from_db()
    assert bill.state == 'issued'
    assert Payment.objects.filter(bill=bill).count() == 0
    assert DriverAccount.objects.get(driver=driver).balance == bill.total - 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_pickup_cash_settlement_closes_once(data):
    bill = issue_pickup(data)
    driver = data['users'][Role.COLLECTOR]
    cash.float_to(data['users'][Role.HQ], driver, {'amount': str(bill.total)})
    barrier = Barrier(2)

    def settle():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            current = Bill.objects.get(pk=bill.pk)
            payload = {'version': current.version, 'amount': str(current.total), 'method': 'Cash'}
            return command(driver, str(uuid.uuid4()), 'bill.pay', payload,
                           lambda actor: str(billing.pay(actor, current, payload).pk))
        except (Conflict, PolicyUnavailable):
            return None
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: settle(), range(2)))

    bill.refresh_from_db()
    assert sum(result is not None for result in results) == 1
    assert bill.state == 'completed'
    assert Payment.objects.filter(bill=bill).count() == 1
    assert CashMovement.objects.filter(payment__bill=bill).count() == 1
    assert not StockMovement.objects.exists()
