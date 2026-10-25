-- OF-010. of_api may read the serving view only.
-- The view is not security_invoker: that setting would make of_api need
-- rights on the base table, which also holds release_id and weather_data_status.
-- The view owner is of_publish, and the view lists the 12 public columns.
-- Foreign keys to the warehouse tables are added in 0004_core_dimensions.sql.

CREATE TABLE publish.daily_external_factors_ph (
  release_id UUID NOT NULL,
  date DATE NOT NULL,
  location_id TEXT NOT NULL,
  country_code CHAR(2) NOT NULL CHECK (country_code = 'PH'),
  temperature_mean_c NUMERIC(7,3),
  precipitation_mm NUMERIC(10,3) CHECK (precipitation_mm >= 0),
  is_public_holiday BOOLEAN NOT NULL,
  holiday_name TEXT,
  is_weekend BOOLEAN NOT NULL,
  weather_source_id TEXT NOT NULL,
  holiday_source_id TEXT NOT NULL,
  source_updated_at TIMESTAMPTZ,
  dataset_version TEXT NOT NULL,
  weather_data_status TEXT NOT NULL CHECK (
    weather_data_status IN ('available', 'missing', 'upstream_gap')
  ),
  PRIMARY KEY (release_id, location_id, date),
  CHECK (
    (is_public_holiday AND holiday_name IS NOT NULL AND length(trim(holiday_name)) > 0)
    OR (NOT is_public_holiday AND holiday_name IS NULL)
  )
);

CREATE VIEW publish.daily_factors
WITH (security_barrier = true, security_invoker = false) AS
SELECT
  date,
  location_id,
  country_code,
  temperature_mean_c,
  precipitation_mm,
  is_public_holiday,
  holiday_name,
  is_weekend,
  weather_source_id,
  holiday_source_id,
  source_updated_at,
  dataset_version
FROM publish.daily_external_factors_ph;

ALTER TABLE publish.daily_external_factors_ph OWNER TO of_publish;
ALTER VIEW publish.daily_factors OWNER TO of_publish;

REVOKE ALL ON TABLE publish.daily_external_factors_ph FROM PUBLIC;
REVOKE ALL ON TABLE publish.daily_factors FROM PUBLIC;
GRANT SELECT ON TABLE publish.daily_factors TO of_api;

REVOKE ALL ON SCHEMA control, staging, core, quality FROM of_api;

ALTER DEFAULT PRIVILEGES FOR ROLE of_admin
  IN SCHEMA control, staging, core, quality, publish
  REVOKE ALL ON TABLES FROM PUBLIC, of_api;
ALTER DEFAULT PRIVILEGES FOR ROLE of_publish
  IN SCHEMA publish
  REVOKE ALL ON TABLES FROM PUBLIC, of_api;
