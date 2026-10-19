from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0015_load_vehicle_full_alert')]

    operations = [
        migrations.AddField(
            model_name='receipt',
            name='explanation',
            field=models.CharField(blank=True, max_length=1000),
        ),
    ]
