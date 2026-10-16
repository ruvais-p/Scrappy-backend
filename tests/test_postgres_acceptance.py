import pytest
from django.db import connection, transaction

from core.models import Bill, Payment, Role
from core.services import billing, cash
from test_workflows import issue_pickup


@pytest.mark.django_db(transaction=True)
def test_concurrency_gate_runs_against_postgresql(data):
    assert connection.vendor == 'postgresql'
    assert connection.features.has_select_for_update


@pytest.mark.django_db
def test_pickup_payment_rolls_back_payment_and_cash_on_ledger_fault(data, monkeypatch):
    bill = issue_pickup(data)
    driver = data['users'][Role.COLLECTOR]
    cash.float_to(data['users'][Role.HQ], driver, {'amount': str(bill.total)})
    before = cash.account(driver).balance

    def fail(*args, **kwargs):
        raise RuntimeError('ledger unavailable')

    monkeypatch.setattr('core.services.cash.move', fail)
    with pytest.raises(RuntimeError, match='ledger unavailable'), transaction.atomic():
        billing.pay(driver, bill, {'version': bill.version, 'amount': str(bill.total), 'method': 'Cash'})

    bill.refresh_from_db()
    assert bill.state == 'issued'
    assert Payment.objects.filter(bill=bill).count() == 0
    assert cash.account(driver).balance == before
