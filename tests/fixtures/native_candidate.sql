-- Stand-in candidate used by the disposable PostgreSQL 16 integration test.
-- Placeholders: __RELEASE_ID__, __CONTENT_HASH__, __SCHEMA_VERSION__

CREATE SCHEMA control;
CREATE SCHEMA core;
CREATE SCHEMA publish;
CREATE SCHEMA quality;

CREATE TABLE control.schema_migrations (
  version TEXT PRIMARY KEY
);
CREATE TABLE control.source_registry (
  source_id TEXT PRIMARY KEY
);
CREATE TABLE core.dim_date (
  date_key DATE PRIMARY KEY
);
CREATE TABLE core.dim_location (
  location_id TEXT PRIMARY KEY
);
CREATE TABLE publish.dataset_releases (
  release_id UUID PRIMARY KEY,
  dataset_id TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  status TEXT NOT NULL,
  actual_row_count BIGINT NOT NULL,
  content_sha256_hex TEXT NOT NULL
);
CREATE TABLE publish.active_dataset_release (
  dataset_id TEXT PRIMARY KEY,
  release_id UUID NOT NULL
);
CREATE TABLE publish.daily_external_factors_ph (
  release_id UUID,
  date DATE,
  location_id TEXT,
  country_code TEXT,
  temperature_mean_c TEXT,
  precipitation_mm TEXT,
  is_public_holiday BOOLEAN,
  holiday_name TEXT,
  is_weekend BOOLEAN,
  weather_source_id TEXT,
  holiday_source_id TEXT,
  source_updated_at TEXT,
  dataset_version TEXT,
  weather_data_status TEXT
);
CREATE TABLE quality.validation_runs (
  validation_run_id UUID PRIMARY KEY
);

CREATE VIEW publish.dataset_catalog AS
SELECT
  releases.release_id AS active_release_id,
  releases.schema_version AS dataset_version
FROM publish.active_dataset_release AS active
JOIN publish.dataset_releases AS releases
  ON releases.release_id = active.release_id
WHERE active.dataset_id = 'daily_external_factors_ph'
  AND releases.status = 'published';

CREATE VIEW publish.daily_factors AS
SELECT NULL::date AS date WHERE false;
CREATE VIEW publish.accessible_factors AS
SELECT NULL::date AS date WHERE false;
CREATE VIEW publish.accessible_releases AS
SELECT NULL::uuid AS release_id WHERE false;

ALTER SCHEMA control OWNER TO of_migrate;
ALTER SCHEMA core OWNER TO of_migrate;
ALTER SCHEMA publish OWNER TO of_publish;
ALTER SCHEMA quality OWNER TO of_publish;
ALTER TABLE control.schema_migrations OWNER TO of_migrate;
ALTER TABLE control.source_registry OWNER TO of_migrate;
ALTER TABLE core.dim_date OWNER TO of_migrate;
ALTER TABLE core.dim_location OWNER TO of_migrate;
ALTER TABLE publish.dataset_releases OWNER TO of_publish;
ALTER TABLE publish.active_dataset_release OWNER TO of_publish;
ALTER TABLE publish.daily_external_factors_ph OWNER TO of_publish;
ALTER TABLE quality.validation_runs OWNER TO of_publish;
ALTER VIEW publish.dataset_catalog OWNER TO of_publish;
ALTER VIEW publish.daily_factors OWNER TO of_publish;
ALTER VIEW publish.accessible_factors OWNER TO of_publish;
ALTER VIEW publish.accessible_releases OWNER TO of_publish;

INSERT INTO publish.dataset_releases (
  release_id, dataset_id, schema_version, status, actual_row_count, content_sha256_hex
) VALUES (
  '__RELEASE_ID__',
  'daily_external_factors_ph',
  '__SCHEMA_VERSION__',
  'published',
  0,
  '__CONTENT_HASH__'
);
INSERT INTO publish.active_dataset_release (dataset_id, release_id)
VALUES ('daily_external_factors_ph', '__RELEASE_ID__');
