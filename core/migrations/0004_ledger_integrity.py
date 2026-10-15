from django.db import migrations

TABLES=('auditevent','payment','cashmovement','earningsentry','stockmovement','receipt','rate','handoverroute')
FORWARD='''
CREATE FUNCTION scrappy_reject_history_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Historical records are append-only' USING ERRCODE = '23514';
END;
$$;
'''+''.join(f'CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON core_{table} FOR EACH ROW EXECUTE FUNCTION scrappy_reject_history_change();\n' for table in TABLES)+'''
CREATE FUNCTION scrappy_protect_issued_bill() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state <> 'draft' THEN
    IF TG_OP = 'DELETE' THEN
      RAISE EXCEPTION 'Issued bills cannot be deleted' USING ERRCODE = '23514';
    END IF;
    IF ROW(NEW.franchise_id, NEW.creator_id, NEW.customer_id, NEW.lead_id,
           NEW.kind, NEW.number, NEW.lines, NEW.snapshot, NEW.subtotal,
           NEW.tax, NEW.total, NEW.share_percent, NEW.issued_at, NEW.notes)
       IS DISTINCT FROM
       ROW(OLD.franchise_id, OLD.creator_id, OLD.customer_id, OLD.lead_id,
           OLD.kind, OLD.number, OLD.lines, OLD.snapshot, OLD.subtotal,
           OLD.tax, OLD.total, OLD.share_percent, OLD.issued_at, OLD.notes)
       OR NEW.state = 'draft' THEN
      RAISE EXCEPTION 'Issued bill values are immutable' USING ERRCODE = '23514';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER immutable_issued_bill BEFORE UPDATE OR DELETE ON core_bill
FOR EACH ROW EXECUTE FUNCTION scrappy_protect_issued_bill();
'''
REVERSE='DROP TRIGGER immutable_issued_bill ON core_bill; DROP FUNCTION scrappy_protect_issued_bill();'+''.join(f'DROP TRIGGER immutable_history ON core_{table};' for table in TABLES)+'DROP FUNCTION scrappy_reject_history_change();'

class Migration(migrations.Migration):
    dependencies=[('core','0003_datarevision')]
    operations=[migrations.RunSQL(FORWARD,REVERSE)]
