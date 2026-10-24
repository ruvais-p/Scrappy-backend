from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0027_kwdstockmovement_kwdstockbalance')]
    operations = [
        migrations.AddField(
            model_name='bill', name='delivery_status',
            field=models.CharField(choices=[('pending', 'To deliver'), ('delivered', 'Delivered'), ('cancelled', 'Cancelled')], default='pending', max_length=16),
        ),
        migrations.AddField(
            model_name='bill', name='installation_status',
            field=models.CharField(choices=[('pending', 'To install'), ('installed', 'Installed'), ('not_required', 'Not required')], default='pending', max_length=16),
        ),
    ]
