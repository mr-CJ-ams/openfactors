-- OF-025. published -> withdrawn is the only further release-status change.
-- The serving view hides a release that is no longer published.
-- Raw manifests stay. Local export files are removed by the withdrawal command.

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
  IF OLD.status = 'published'
     AND NEW.status = 'withdrawn'
     AND OLD.published_at IS NOT NULL
     AND NEW.published_at IS NOT DISTINCT FROM OLD.published_at
  THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'dataset_releases is immutable';
END;
$$;

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
 AND active.release_id = factors.release_id
JOIN publish.dataset_releases AS releases
  ON releases.release_id = factors.release_id
 AND releases.status = 'published';

ALTER VIEW publish.daily_factors OWNER TO of_publish;
ALTER FUNCTION publish.reject_release_mutation() OWNER TO of_publish;

REVOKE ALL ON TABLE publish.daily_factors FROM PUBLIC;
GRANT SELECT ON TABLE publish.daily_factors TO of_api;
