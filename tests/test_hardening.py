import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


def test_hardening_command_is_explicit_by_default(capsys):
    call_command('check_hardening')
    assert 'pass --production' in capsys.readouterr().out


@pytest.mark.django_db
def test_hardening_command_rejects_development_settings(settings):
    settings.DEBUG = True
    with pytest.raises(CommandError, match='DJANGO_DEBUG'):
        call_command('check_hardening', production=True)
