from django.db import migrations


FORWARD = """
CREATE FUNCTION scrappy_invalidate_route_cash_review() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  prior_driver bigint;
  next_driver bigint;
  affected_driver bigint;
BEGIN
  IF TG_OP <> 'INSERT' THEN prior_driver := OLD.driver_id; END IF;
  IF TG_OP <> 'DELETE' THEN next_driver := NEW.driver_id; END IF;
  IF TG_OP = 'UPDATE' AND
     ROW(NEW.driver_id, NEW.franchise_id, NEW.warehouse_id, NEW.state,
         NEW.returned_at, NEW.eligibility_reference)
     IS NOT DISTINCT FROM
     ROW(OLD.driver_id, OLD.franchise_id, OLD.warehouse_id, OLD.state,
         OLD.returned_at, OLD.eligibility_reference) THEN
    RETURN NEW;
  END IF;
  FOR affected_driver IN
    SELECT DISTINCT value FROM unnest(ARRAY[prior_driver,next_driver]) AS value
    WHERE value IS NOT NULL ORDER BY value
  LOOP
    INSERT INTO core_driveraccount (driver_id,balance,version)
      VALUES (affected_driver,0,1) ON CONFLICT (driver_id) DO NOTHING;
    UPDATE core_driveraccount SET version=version+1 WHERE driver_id=affected_driver;
  END LOOP;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER route_cash_review BEFORE INSERT OR UPDATE OR DELETE ON core_load
FOR EACH ROW EXECUTE FUNCTION scrappy_invalidate_route_cash_review();
"""


class Migration(migrations.Migration):
    dependencies = [('core', '0006_handover_snapshot_guard')]
    operations = [migrations.RunSQL(FORWARD, """
        DROP TRIGGER route_cash_review ON core_load;
        DROP FUNCTION scrappy_invalidate_route_cash_review();
    """)]
