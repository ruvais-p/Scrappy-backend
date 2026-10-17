import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


def backfill_load_reconciliation(apps, schema_editor):
    Load = apps.get_model('core', 'Load')
    Lead = apps.get_model('core', 'Lead')
    Receipt = apps.get_model('core', 'Receipt')
    for load in Load.objects.all().iterator():
        lead_ids = Lead.objects.filter(load_id=load.pk, bill__state__in=['issued', 'completed', 'cancellation_requested']).values_list('pk', flat=True)
        if not lead_ids.exists():
            continue
        receipts = Receipt.objects.filter(lead_id__in=lead_ids, submitted_at__isnull=False)
        if receipts.count() != lead_ids.count():
            continue
        load.reconciliation_state = 'flagged' if receipts.filter(flagged=True).exists() else 'reconciled'
        load.save(update_fields=['reconciliation_state'])


class Migration(migrations.Migration):
    dependencies = [('core', '0009_seed_digester_product')]

    operations = [
        migrations.AddField('load', 'reconciliation_state', models.CharField(default='pending', max_length=24)),
        migrations.AddField('load', 'reweigh_version', models.PositiveIntegerField(default=1)),
        migrations.AddField('load', 'resolution_action', models.CharField(blank=True, max_length=24)),
        migrations.AddField('load', 'resolution_note', models.CharField(blank=True, max_length=1000)),
        migrations.AddField('load', 'resolved_at', models.DateTimeField(blank=True, null=True)),
        migrations.AddField('load', 'resolved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='resolved_loads', to=settings.AUTH_USER_MODEL)),
        migrations.AddField('handover', 'reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='reviewed_handovers', to=settings.AUTH_USER_MODEL)),
        migrations.AddField('handover', 'reviewed_at', models.DateTimeField(blank=True, null=True)),
        migrations.AddField('handover', 'review_note', models.CharField(blank=True, max_length=1000)),
        migrations.AddField('receipt', 'revision', models.PositiveIntegerField(default=1)),
        migrations.CreateModel(
            name='ReceiptRevision',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('version', models.PositiveIntegerField(default=1)),
                ('revision', models.PositiveIntegerField()),
                ('lines', models.JSONField(default=list)),
                ('note', models.CharField(blank=True, max_length=1000)),
                ('submitted_at', models.DateTimeField(blank=True, null=True)),
                ('flagged', models.BooleanField(default=False)),
                ('superseded_at', models.DateTimeField(blank=True, null=True)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('receipt', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='revisions', to='core.receipt')),
            ],
            options={'abstract': False, 'ordering': ['-created_at']},
        ),
        migrations.AddField('stockmovement', 'receipt_revision', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='core.receiptrevision')),
        migrations.AddConstraint('stockmovement', models.UniqueConstraint(condition=Q(('receipt_revision__isnull', False)), fields=('receipt_revision', 'product'), name='once_per_reweigh_product')),
        migrations.RunPython(backfill_load_reconciliation, migrations.RunPython.noop),
    ]
