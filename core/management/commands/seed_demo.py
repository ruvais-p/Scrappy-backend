import os
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from core.fixtures import synthetic_records
class Command(BaseCommand):
    help='Create isolated synthetic development records; never runs in production.'
    def add_arguments(self,p): p.add_argument('--test-policies',action='store_true')
    def handle(self,*args,**opts):
        if settings.ENVIRONMENT!='development':raise CommandError('Demo fixtures require development environment.')
        password=os.getenv('SCRAPPY_DEMO_PASSWORD')
        if not password: raise CommandError('Set SCRAPPY_DEMO_PASSWORD to a unique development-only password.')
        validate_password(password)
        with transaction.atomic(): data=synthetic_records(password,opts['test_policies'])
        self.stdout.write('Synthetic fixtures created. Account emails: '+', '.join(u.email for u in data['users'].values()))
