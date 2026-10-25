-- OF-024. One transaction inserts the candidate and switches the active pointer.
-- Content stays immutable. approved -> published is the only release-row change.
-- Rollback moves the pointer. Published rows stay stored. Withdrawal is OF-025.

CREATE OR REPLACE FUNCTION publish.reject_release_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'dataset_releases is immutable';
  END IF;
  IF OLD.release_id IS DISTINCT FROM NEW.release_id
     OR OLD.dataset_id IS DISTINCT FROM NEW.dataset_id
     OR OLD.schema_version IS DISTINCT FROM NEW.schema_version
     OR OLD.source_manifest_set_hash IS DISTINCT FROM NEW.source_manifest_set_hash
     OR OLD.content_sha256_hex IS DISTINCT FROM NEW.content_sha256_hex
     OR OLD.expected_row_count IS DISTINCT FROM NEW.expected_row_count
     OR OLD.actual_row_count IS DISTINCT FROM NEW.actual_row_count
     OR OLD.coverage_start IS DISTINCT FROM NEW.coverage_start
     OR OLD.coverage_end IS DISTINCT FROM NEW.coverage_end
     OR OLD.validated_at IS DISTINCT FROM NEW.validated_at
     OR OLD.created_at IS DISTINCT FROM NEW.created_at
  THEN
    RAISE EXCEPTION 'dataset_releases content is immutable';
  END IF;
  IF OLD.status = 'approved'
     AND NEW.status = 'published'
     AND OLD.published_at IS NULL
     AND NEW.published_at IS NOT NULL
  THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'dataset_releases is immutable';
END;
$$;

CREATE TABLE publish.active_dataset_release (
  dataset_id TEXT PRIMARY KEY CHECK (dataset_id = 'daily_external_factors_ph'),
  release_id UUID NOT NULL UNIQUE REFERENCES publish.dataset_releases(release_id),
  activated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION publish.reject_unpublished_pointer()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  found_status TEXT;
BEGIN
  SELECT status INTO found_status
  FROM publish.dataset_releases
  WHERE release_id = NEW.release_id;
  IF found_status IS DISTINCT FROM 'published' THEN
    RAISE EXCEPTION 'active release must be published';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER active_release_must_be_published
BEFORE INSERT OR UPDATE ON publish.active_dataset_release
FOR EACH ROW
EXECUTE FUNCTION publish.reject_unpublished_pointer();

CREATE FUNCTION publish.reject_published_row_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'published rows are immutable';
END;
$$;

CREATE TRIGGER published_rows_immutable
BEFORE UPDATE OR DELETE ON publish.daily_external_factors_ph
FOR EACH ROW
EXECUTE FUNCTION publish.reject_published_row_mutation();

DROP VIEW publish.daily_factors;

CREATE VIEW publish.daily_factors
WITH (security_barrier = true, security_invoker = false) AS
SELECT
  factors.date,
  factors.location_id,
  factors.country_code,
  factors.temperature_mean_c,
  factors.precipitation_mm,
  factors.is_public_holiday,
  factors.holiday_name,
  factors.is_weekend,
  factors.weather_source_id,
  factors.holiday_source_id,
  factors.source_updated_at,
  factors.dataset_version
FROM publish.daily_external_factors_ph AS factors
JOIN publish.active_dataset_release AS active
  ON active.dataset_id = 'daily_external_factors_ph'
 AND active.release_id = factors.release_id;

ALTER VIEW publish.daily_factors OWNER TO of_publish;
ALTER TABLE publish.active_dataset_release OWNER TO of_publish;
ALTER FUNCTION publish.reject_release_mutation() OWNER TO of_publish;
ALTER FUNCTION publish.reject_unpublished_pointer() OWNER TO of_publish;
ALTER FUNCTION publish.reject_published_row_mutation() OWNER TO of_publish;

REVOKE ALL ON TABLE publish.daily_factors FROM PUBLIC;
GRANT SELECT ON TABLE publish.daily_factors TO of_api;
REVOKE ALL ON TABLE publish.active_dataset_release FROM PUBLIC, of_api;
