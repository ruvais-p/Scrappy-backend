from unittest.mock import patch
import pytest
from django.db import transaction,IntegrityError
from core.models import AuditEvent,CashMovement,Rate,Bill,Role,Receipt
from core.services import cash
from core.services.common import command
from test_workflows import issue_pickup

@pytest.mark.django_db
def test_audit_and_rates_cannot_be_rewritten(data):
    event=AuditEvent.objects.create(actor=data['users'][Role.HQ],action='original',resource='example')
    for operation in [lambda:AuditEvent.objects.filter(pk=event.pk).update(action='changed'),lambda:AuditEvent.objects.filter(pk=event.pk).delete(),lambda:Rate.objects.all().update(amount=1)]:
        with pytest.raises(IntegrityError),transaction.atomic():operation()
    event.refresh_from_db();assert event.action=='original'

@pytest.mark.django_db
def test_issued_values_are_database_protected(data):
    bill=issue_pickup(data)
    for operation in [lambda:Bill.objects.filter(pk=bill.pk).update(total=999),lambda:Bill.objects.filter(pk=bill.pk).update(state='draft'),lambda:Bill.objects.filter(pk=bill.pk).delete()]:
        with pytest.raises(IntegrityError),transaction.atomic():operation()
    bill.refresh_from_db();assert bill.total==280

@pytest.mark.django_db
def test_audit_failure_rolls_back_financial_command(data):
    actor=data['users'][Role.HQ];driver=data['users'][Role.COLLECTOR]
    with patch('core.services.common.AuditEvent.objects.create',side_effect=RuntimeError('injected audit failure')):
        with pytest.raises(RuntimeError):
            command(actor,'audit-rollback','float',{'amount':'20'},lambda u:{'id':str(cash.float_to(u,driver,{'amount':'20'}).pk)})
    assert not CashMovement.objects.exists()

@pytest.mark.django_db
def test_receipt_draft_can_submit_but_posted_values_cannot_change(data):
    from django.utils import timezone
    receipt=Receipt.objects.create(lead=data['leads'][0],warehouse=data['warehouses'][0],actor=data['users'][Role.WAREHOUSE])
    receipt.submitted_at=timezone.now();receipt.save()
    with pytest.raises(IntegrityError),transaction.atomic():Receipt.objects.filter(pk=receipt.pk).update(lines=[])
