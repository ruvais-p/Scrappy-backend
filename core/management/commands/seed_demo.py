import os
from decimal import Decimal
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from core.fixtures import synthetic_records
from core.models import District, LSGD, Role
class Command(BaseCommand):
    help='Create isolated synthetic development records; never runs in production.'
    def add_arguments(self,p): p.add_argument('--test-policies',action='store_true')
    def handle(self,*args,**opts):
        if settings.ENVIRONMENT!='development':raise CommandError('Demo fixtures require development environment.')
        password=os.getenv('SCRAPPY_DEMO_PASSWORD')
        if not password: raise CommandError('Set SCRAPPY_DEMO_PASSWORD to a unique development-only password.')
        validate_password(password)
        with transaction.atomic():
            data=synthetic_records(password,opts['test_policies'])
            manager=data['users'][Role.DIGESTER_MANAGER]
            samples=[
                ('Demo North Grama Panchayat','grama_panchayat','Ernakulam','Synthetic Digester CSV demo record',Decimal('10.1200000'),Decimal('76.3000000')),
                ('Demo South Grama Panchayat','grama_panchayat','Ernakulam','Synthetic Digester CSV demo record',Decimal('9.9300000'),Decimal('76.2700000')),
                ('Demo Central Municipality','municipality','Thrissur','Synthetic Digester CSV demo record',Decimal('10.5200000'),Decimal('76.2100000')),
                ('Demo Coastal Block Panchayat','block_panchayat','Alappuzha','Synthetic Digester CSV demo record',Decimal('9.5000000'),Decimal('76.3300000')),
            ]
            for name,kind,district_name,address,latitude,longitude in samples:
                district,_=District.objects.get_or_create(name=district_name)
                LSGD.objects.get_or_create(name=name,district=district,defaults={'kind':kind,'address':address,'latitude':latitude,'longitude':longitude,'created_by':manager})
        self.stdout.write('Synthetic fixtures created. Digester HQ demo account: '+manager.email+'; LSGD test records: '+str(len(samples)))
