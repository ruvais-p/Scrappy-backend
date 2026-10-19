from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0010_supervisor_hq_operations')]

    operations = [
        migrations.AddField(
            model_name='category',
            name='default_billing_mode',
            field=models.CharField(choices=[('franchise', 'Franchise Billing'), ('company', 'Company Billing')], default='franchise', max_length=16),
        ),
        migrations.AddField(
            model_name='bill',
            name='billing_mode',
            field=models.CharField(choices=[('franchise', 'Franchise Billing'), ('company', 'Company Billing')], default='franchise', max_length=16),
        ),
    ]
