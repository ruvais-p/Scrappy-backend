from decimal import Decimal

from django.db import migrations


def add_digester_product(apps, schema_editor):
    Category = apps.get_model('core', 'Category')
    Franchise = apps.get_model('core', 'Franchise')
    Product = apps.get_model('core', 'Product')
    Rate = apps.get_model('core', 'Rate')
    User = apps.get_model('core', 'User')

    franchises = list(Franchise.objects.filter(kind='digester', status='active'))
    if not franchises:
        return

    actor = User.objects.filter(role='super_admin').order_by('id').first()
    if actor is None:
        # The rate requires an actor for its audit trail. Any existing account
        # is preferable to inventing a service identity during a migration.
        actor = User.objects.order_by('id').first()
    if actor is None:
        return

    category, _ = Category.objects.get_or_create(name='Digester', kind='digester')
    product, _ = Product.objects.get_or_create(
        name='Digester',
        category=category,
        defaults={'unit': 'piece'},
    )
    if product.unit != 'piece' or not product.active:
        Product.objects.filter(pk=product.pk).update(unit='piece', active=True)
    product.franchises.add(*franchises)

    for franchise in franchises:
        latest_rate = Rate.objects.filter(
            product=product,
            franchise=franchise,
            kind='sale',
        ).order_by('-created_at', '-pk').first()
        if latest_rate is None or latest_rate.amount != Decimal('500.00'):
            Rate.objects.create(
                product=product,
                franchise=franchise,
                kind='sale',
                amount=Decimal('500.00'),
                actor=actor,
                reason='Digester product price per piece',
            )


class Migration(migrations.Migration):
    dependencies = [('core', '0008_roledefinition_user_custom_role')]

    operations = [migrations.RunPython(add_digester_product, migrations.RunPython.noop)]
