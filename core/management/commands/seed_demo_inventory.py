from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Product, Role, StockBalance, StockMovement, User, Warehouse


class Command(BaseCommand):
    help = 'Add synthetic stock receipts and releases to the local demo warehouse.'

    def handle(self, *args, **options):
        if settings.ENVIRONMENT != 'development':
            raise CommandError('Demo inventory requires development environment.')

        warehouse = Warehouse.objects.filter(franchise__code='NTH', name='North Yard Warehouse').first()
        actor = User.objects.filter(role=Role.WAREHOUSE, warehouse=warehouse).first() if warehouse else None
        if not warehouse or not actor:
            raise CommandError('Run seed_demo first to create the North Yard warehouse and warehouse manager.')

        samples = [
            ('Iron', Decimal('125.000'), Decimal('35.000')),
            ('Paper', Decimal('70.000'), Decimal('22.000')),
            ('Aluminium', Decimal('12.000'), Decimal('4.000')),
            ('Plastic', Decimal('45.000'), Decimal('0.000')),
        ]
        created = 0
        with transaction.atomic():
            for name, received, released in samples:
                product = Product.objects.filter(name=name, franchises=warehouse.franchise).first()
                if not product:
                    raise CommandError(f'Demo product {name!r} was not found. Run seed_demo first.')
                if StockMovement.objects.filter(warehouse=warehouse, product=product).exists():
                    continue

                balance, _ = StockBalance.objects.get_or_create(
                    warehouse=warehouse, product=product, defaults={'quantity': Decimal('0.000')}
                )
                balance = StockBalance.objects.select_for_update().get(pk=balance.pk)
                StockMovement.objects.create(
                    warehouse=warehouse, product=product, actor=actor,
                    quantity=received, kind='receipt',
                )
                balance.quantity += received
                if released:
                    StockMovement.objects.create(
                        warehouse=warehouse, product=product, actor=actor,
                        quantity=-released, kind='release',
                    )
                    balance.quantity -= released
                balance.version += 1
                balance.save(update_fields=['quantity', 'version'])
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f'Created sample movements for {created} products in {warehouse.name}. '
            'Run again safely; existing product movements are left unchanged.'
        ))
