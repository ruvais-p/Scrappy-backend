import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0012_rate_discount_percent'),
    ]

    operations = [
        migrations.CreateModel(
            name='Quotation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('version', models.PositiveIntegerField(default=1)),
                ('kind', models.CharField(choices=[('collection', 'Collection'), ('sale', 'Sale'), ('b2b', 'B2B')], max_length=20)),
                ('billing_mode', models.CharField(choices=[('franchise', 'Franchise Billing'), ('company', 'Company Billing')], default='franchise', max_length=16)),
                ('number', models.CharField(max_length=32, unique=True)),
                ('state', models.CharField(choices=[('draft', 'Draft'), ('sent', 'Sent'), ('accepted', 'Accepted'), ('rejected', 'Rejected'), ('converted', 'Converted')], default='draft', max_length=16)),
                ('lines', models.JSONField(default=list)),
                ('subtotal', models.DecimalField(decimal_places=2, default=0, max_digits=16)),
                ('tax', models.DecimalField(decimal_places=2, default=0, max_digits=16)),
                ('total', models.DecimalField(decimal_places=2, default=0, max_digits=16)),
                ('notes', models.CharField(blank=True, max_length=1000)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('accepted_at', models.DateTimeField(blank=True, null=True)),
                ('rejected_at', models.DateTimeField(blank=True, null=True)),
                ('converted_at', models.DateTimeField(blank=True, null=True)),
                ('converted_bill', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='source_quotation', to='core.bill')),
                ('creator', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('customer', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='core.customer')),
                ('franchise', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='core.franchise')),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
