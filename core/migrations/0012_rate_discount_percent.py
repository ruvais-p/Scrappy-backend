from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0011_company_billing_mode')]

    operations = [
        migrations.AddField(
            model_name='rate',
            name='discount_percent',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddConstraint(
            model_name='rate',
            constraint=models.CheckConstraint(condition=models.Q(('discount_percent__gte', 0), ('discount_percent__lte', 100)), name='rate_discount_range'),
        ),
    ]
