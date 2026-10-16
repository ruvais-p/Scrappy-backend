from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from core.models import AuditEvent, Job, User


class Command(BaseCommand):
    help = 'Run a non-destructive recovery continuity drill against the configured services.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Required safety flag; never mutates production data.')

    def handle(self, *args, **options):
        if not options['dry_run']:
            raise CommandError('Pass --dry-run; this command never mutates application data.')
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        root=Path(settings.PRIVATE_ROOT)
        files=sum(1 for item in root.iterdir() if item.is_file()) if root.exists() else 0
        self.stdout.write(f'Database: {connection.vendor} reachable')
        self.stdout.write(f'Audit events retained: {AuditEvent.objects.count()}')
        self.stdout.write(f'Users requiring session revalidation: {User.objects.filter(is_active=False).count()}')
        self.stdout.write(f'Private artifacts present: {files}')
        self.stdout.write(f'Pending jobs retained: {Job.objects.filter(state="pending").count()}')
        self.stdout.write(self.style.SUCCESS('Recovery drill completed without writes.'))
