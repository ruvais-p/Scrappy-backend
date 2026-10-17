from datetime import time, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import Customer, Franchise, Lead


class Command(BaseCommand):
    help = 'Add idempotent synthetic leads for development HQ review.'

    def handle(self, *args, **options):
        if settings.ENVIRONMENT != 'development':
            raise CommandError('Demo leads can only be added in development.')

        today = timezone.localdate()
        created = 0
        with transaction.atomic():
            for code, prefix in [('NTH', 'North'), ('STH', 'South')]:
                franchise = Franchise.objects.filter(code=code, kind='scrap').first()
                if not franchise:
                    raise CommandError(f'{code} demo franchise is missing; initialize demo data first.')
                for number in range(1, 5):
                    phone = f'910000{0 if code == "NTH" else 1}{number:04d}'
                    customer, _ = Customer.objects.get_or_create(
                        franchise=franchise,
                        phone=phone,
                        defaults={
                            'name': f'HQ Demo {prefix} Lead {number}',
                            'address': f'{prefix} district, Demo City',
                            'area': f'{prefix} district',
                        },
                    )
                    lead, was_created = Lead.objects.get_or_create(
                        customer=customer,
                        pickup_date=today + timedelta(days=(number - 1) % 3),
                        defaults={
                            'franchise': franchise,
                            'pickup_time': time(10 + number, 0),
                            'items': ['Paper, cardboard', 'Iron, aluminium', 'Plastic, paper', 'Mixed recyclable materials'][number - 1],
                            'state': 'unassigned',
                        },
                    )
                    created += int(was_created)

        self.stdout.write(f'Added {created} new demo leads for HQ. Existing demo leads were left unchanged.')
