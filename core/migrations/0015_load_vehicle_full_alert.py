from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0014_add_segment_tracking_roles')]

    operations = [
        migrations.AddField(
            model_name='load',
            name='vehicle_full_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
