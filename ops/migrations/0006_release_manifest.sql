-- OF-023. A candidate release freezes provenance, content checksum, and quality evidence.
-- release_id is the content id. schema_version is the contract version. They are different columns.
-- Promotion of an approved release is OF-024. This trigger rejects every update and delete.

CREATE TABLE publish.dataset_releases (
  release_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dataset_id TEXT NOT NULL CHECK (dataset_id = 'daily_external_factors_ph'),
  schema_version TEXT NOT NULL CHECK (schema_version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
  status TEXT NOT NULL CHECK (status IN (
    'building', 'validating', 'rejected', 'approved', 'published', 'withdrawn'
  )),
  source_manifest_set_hash CHAR(64) NOT NULL
    CHECK (source_manifest_set_hash ~ '^[0-9a-f]{64}$'),
  content_sha256_hex CHAR(64)
    CHECK (content_sha256_hex IS NULL OR content_sha256_hex ~ '^[0-9a-f]{64}$'),
  expected_row_count BIGINT CHECK (expected_row_count IS NULL OR expected_row_count >= 0),
  actual_row_count BIGINT CHECK (actual_row_count IS NULL OR actual_row_count >= 0),
  coverage_start DATE NOT NULL,
  coverage_end DATE NOT NULL,
  validated_at TIMESTAMPTZ,
  published_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (coverage_start <= coverage_end),
  CHECK (schema_version <> release_id::text),
  CHECK (status NOT IN ('approved', 'rejected') OR (
    content_sha256_hex IS NOT NULL
    AND expected_row_count IS NOT NULL
    AND actual_row_count IS NOT NULL
    AND validated_at IS NOT NULL
  )),
  CHECK (status <> 'published' OR published_at IS NOT NULL),
  CHECK (published_at IS NULL OR status IN ('published', 'withdrawn'))
);

CREATE TABLE quality.validation_runs (
  validation_run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  release_id UUID NOT NULL REFERENCES publish.dataset_releases(release_id),
  status TEXT NOT NULL CHECK (status IN ('running', 'PASS', 'FAIL', 'REVIEW_REQUIRED')),
  evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expected_location_dates BIGINT,
  present_location_dates BIGINT,
  weather_present_location_dates BIGINT,
  documented_upstream_gaps BIGINT NOT NULL DEFAULT 0,
  result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  reviewer_id TEXT,
  reviewed_at TIMESTAMPTZ
);

CREATE INDEX idx_validation_runs_release ON quality.validation_runs(release_id);

ALTER TABLE publish.daily_external_factors_ph
  ADD CONSTRAINT daily_external_factors_ph_release_fkey
  FOREIGN KEY (release_id) REFERENCES publish.dataset_releases(release_id);

CREATE FUNCTION publish.reject_release_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'dataset_releases is immutable';
END;
$$;

CREATE TRIGGER dataset_releases_immutable
BEFORE UPDATE OR DELETE ON publish.dataset_releases
FOR EACH ROW
EXECUTE FUNCTION publish.reject_release_mutation();

CREATE FUNCTION quality.reject_validation_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'validation_runs is immutable';
END;
$$;

CREATE TRIGGER validation_runs_immutable
BEFORE UPDATE OR DELETE ON quality.validation_runs
FOR EACH ROW
EXECUTE FUNCTION quality.reject_validation_mutation();

ALTER TABLE publish.dataset_releases OWNER TO of_publish;
ALTER TABLE quality.validation_runs OWNER TO of_publish;
ALTER FUNCTION publish.reject_release_mutation() OWNER TO of_publish;
ALTER FUNCTION quality.reject_validation_mutation() OWNER TO of_publish;

REVOKE ALL ON TABLE publish.dataset_releases, quality.validation_runs FROM PUBLIC, of_api;
