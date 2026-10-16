import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


@pytest.mark.django_db
def test_recovery_drill_requires_explicit_dry_run(capsys):
    call_command('recovery_drill', dry_run=True)
    assert 'completed without writes' in capsys.readouterr().out


def test_recovery_drill_rejects_mutating_mode():
    with pytest.raises(CommandError, match='--dry-run'):
        call_command('recovery_drill')
