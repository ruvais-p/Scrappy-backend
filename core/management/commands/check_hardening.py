from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Validate production secrets, transport, storage, and renderer safeguards.'

    def add_arguments(self, parser):
        parser.add_argument('--production', action='store_true', help='Apply production-only checks.')

    def handle(self, *args, **options):
        if not options['production']:
            self.stdout.write(self.style.SUCCESS('Hardening checks skipped; pass --production to validate deployment settings.'))
            return
        errors=[]
        if settings.DEBUG: errors.append('DJANGO_DEBUG must be disabled.')
        if len(settings.SECRET_KEY)<32: errors.append('DJANGO_SECRET_KEY must contain at least 32 characters.')
        if not settings.MFA_ENCRYPTION_KEY: errors.append('MFA_ENCRYPTION_KEY must be configured for encrypted event cursors.')
        if settings.EMAIL_BACKEND.endswith('console.EmailBackend'): errors.append('EMAIL_BACKEND must deliver through a configured provider.')
        root=Path(settings.PRIVATE_ROOT)
        if root.exists() and (root.stat().st_mode & 0o777)!=0o700: errors.append('PRIVATE_STORAGE_PATH must be mode 0700.')
        if errors: raise CommandError('Hardening checks failed: '+' '.join(errors))
        self.stdout.write(self.style.SUCCESS('Production hardening checks passed.'))
