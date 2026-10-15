import getpass
from django.core.management.base import BaseCommand,CommandError
from django.contrib.auth.password_validation import validate_password
from django.db import transaction,connection
from core.models import User,Role
class Command(BaseCommand):
    help='Create the first Super Admin once, with a prompted password.'
    def add_arguments(self,p): p.add_argument('--email',required=True)
    def handle(self,*args,**opts):
        password=getpass.getpass('Password (at least 15 characters): ')
        validate_password(password)
        with transaction.atomic():
            with connection.cursor() as c: c.execute('SELECT pg_advisory_xact_lock(71856001)')
            if User.objects.filter(role=Role.SUPER).exists(): raise CommandError('Bootstrap is closed; a Super Admin already exists.')
            User.objects.create_user(username=opts['email'].lower(),email=opts['email'].lower(),password=password,role=Role.SUPER)
        self.stdout.write('Super Admin created. Enrol MFA at first sign-in.')
