from django.db import migrations

class Migration(migrations.Migration):
    dependencies=[('core','0004_ledger_integrity')]
    operations=[migrations.RunSQL(
        'DROP TRIGGER immutable_history ON core_receipt; CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON core_receipt FOR EACH ROW WHEN (OLD.submitted_at IS NOT NULL) EXECUTE FUNCTION scrappy_reject_history_change();',
        'DROP TRIGGER immutable_history ON core_receipt; CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON core_receipt FOR EACH ROW EXECUTE FUNCTION scrappy_reject_history_change();'
    )]
