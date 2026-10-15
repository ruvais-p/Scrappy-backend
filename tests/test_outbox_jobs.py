from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.db import transaction

from core.models import Job, Outbox, Role


@pytest.mark.django_db(transaction=True)
def test_transactional_outbox_discards_rolled_back_work_and_dispatches_committed_event(data):
    actor = data['users'][Role.SUPER]
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            Outbox.objects.create(actor=actor, feature='rolled_back', resource_id='x')
            raise RuntimeError('simulated command crash')
    assert not Outbox.objects.filter(feature='rolled_back').exists()

    record = Outbox.objects.create(actor=actor, feature='committed', resource_id='resource-1')
    redis_client = MagicMock()
    with patch('core.management.commands.dispatch_events.redis.Redis.from_url', return_value=redis_client):
        call_command('dispatch_events', once=True)
    record.refresh_from_db()
    assert record.published_at is not None
    assert redis_client.xadd.called


@pytest.mark.django_db(transaction=True)
def test_durable_job_is_created_only_after_transaction_commit(data):
    actor = data['users'][Role.SUPER]
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            Job.objects.create(actor=actor, kind='test_job', payload={'value': 1})
            raise RuntimeError('simulated command crash')
    assert not Job.objects.filter(kind='test_job').exists()

    with transaction.atomic():
        job = Job.objects.create(actor=actor, kind='test_job', payload={'value': 2})
    assert Job.objects.filter(pk=job.pk, state='pending').exists()
