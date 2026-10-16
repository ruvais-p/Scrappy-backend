from django.db import migrations


FORWARD = """
CREATE FUNCTION scrappy_protect_handover_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Cash handovers cannot be deleted' USING ERRCODE = '23514';
  END IF;
  IF ROW(NEW.driver_id, NEW.franchise_id, NEW.counter_id, NEW.expected,
         NEW.received, NEW.difference, NEW.note, NEW.cutoff, NEW.created_at)
     IS DISTINCT FROM
     ROW(OLD.driver_id, OLD.franchise_id, OLD.counter_id, OLD.expected,
         OLD.received, OLD.difference, OLD.note, OLD.cutoff, OLD.created_at) THEN
    RAISE EXCEPTION 'Cash handover snapshots are immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER immutable_handover_snapshot BEFORE UPDATE OR DELETE ON core_handover
FOR EACH ROW EXECUTE FUNCTION scrappy_protect_handover_snapshot();
"""


class Migration(migrations.Migration):
    dependencies = [('core', '0005_receipt_draft_guard')]
    operations = [migrations.RunSQL(FORWARD, """
        DROP TRIGGER immutable_handover_snapshot ON core_handover;
        DROP FUNCTION scrappy_protect_handover_snapshot();
    """)]
