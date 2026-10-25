-- OF-016. Conformed location and date dimensions, plus source facts.
-- A second source_revision for the same location-day is a new row.
-- The winner is source_updated_at, otherwise retrieved_at, otherwise artifact_id.
-- Location coordinates and the ERA5 cell are not updated in place.
-- Foreign keys from the publish table point at these dimensions and the source registry.

CREATE TABLE core.dim_location (
  location_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  country_code CHAR(2) NOT NULL CHECK (country_code = 'PH'),
  admin_region TEXT,
  admin_province TEXT,
  latitude NUMERIC(9,6) NOT NULL CHECK (latitude BETWEEN -90 AND 90),
  longitude NUMERIC(9,6) NOT NULL CHECK (longitude BETWEEN -180 AND 180),
  timezone TEXT NOT NULL DEFAULT 'Asia/Manila' CHECK (timezone = 'Asia/Manila'),
  provider_location_ref TEXT,
  weather_grid_description TEXT,
  valid_from DATE NOT NULL,
  valid_to DATE,
  approved_for_mvp BOOLEAN NOT NULL DEFAULT FALSE,
  CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE core.dim_date (
  date DATE PRIMARY KEY,
  day_of_week SMALLINT NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
  is_weekend BOOLEAN NOT NULL,
  calendar_year SMALLINT NOT NULL,
  calendar_month SMALLINT NOT NULL CHECK (calendar_month BETWEEN 1 AND 12),
  CHECK (is_weekend = (day_of_week IN (6, 7))),
  CHECK (day_of_week = EXTRACT(ISODOW FROM date)::integer),
  CHECK (calendar_year = EXTRACT(YEAR FROM date)::integer),
  CHECK (calendar_month = EXTRACT(MONTH FROM date)::integer)
);

CREATE TABLE core.fact_daily_weather (
  location_id TEXT NOT NULL REFERENCES core.dim_location(location_id),
  date DATE NOT NULL REFERENCES core.dim_date(date),
  source_revision TEXT NOT NULL CHECK (length(btrim(source_revision)) > 0),
  source_id TEXT NOT NULL REFERENCES control.source_registry(source_id),
  artifact_id UUID NOT NULL REFERENCES control.raw_manifests(artifact_id),
  temperature_mean_c NUMERIC(7,3),
  precipitation_mm NUMERIC(10,3) CHECK (precipitation_mm >= 0),
  weather_data_status TEXT NOT NULL
    CHECK (weather_data_status IN ('available', 'missing', 'upstream_gap')),
  source_updated_at TIMESTAMPTZ,
  retrieved_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (location_id, date, source_revision),
  CHECK (
    weather_data_status <> 'available'
    OR (temperature_mean_c IS NOT NULL OR precipitation_mm IS NOT NULL)
  )
);

CREATE TABLE core.fact_public_holidays (
  date DATE NOT NULL REFERENCES core.dim_date(date),
  holiday_id TEXT NOT NULL CHECK (length(btrim(holiday_id)) > 0),
  source_revision TEXT NOT NULL CHECK (length(btrim(source_revision)) > 0),
  source_id TEXT NOT NULL REFERENCES control.source_registry(source_id),
  artifact_id UUID NOT NULL REFERENCES control.raw_manifests(artifact_id),
  holiday_name TEXT NOT NULL CHECK (length(btrim(holiday_name)) > 0),
  holiday_category TEXT NOT NULL
    CHECK (holiday_category IN ('regular', 'special_non_working')),
  scope TEXT NOT NULL CHECK (scope = 'national'),
  source_updated_at TIMESTAMPTZ,
  retrieved_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (date, holiday_id, source_revision)
);

CREATE TABLE core.holiday_calendar_coverage (
  date DATE NOT NULL REFERENCES core.dim_date(date),
  source_id TEXT NOT NULL REFERENCES control.source_registry(source_id),
  source_revision TEXT NOT NULL CHECK (length(btrim(source_revision)) > 0),
  verified_complete BOOLEAN NOT NULL DEFAULT FALSE,
  verified_at TIMESTAMPTZ,
  PRIMARY KEY (date, source_id, source_revision),
  CHECK (NOT verified_complete OR verified_at IS NOT NULL)
);

CREATE INDEX idx_weather_source_date ON core.fact_daily_weather(source_id, date);
CREATE INDEX idx_holidays_date ON core.fact_public_holidays(date);

COMMENT ON COLUMN core.fact_daily_weather.source_revision IS
  'Deterministic fingerprint. Winner is source_updated_at, else retrieved_at, else artifact_id.';
COMMENT ON COLUMN core.fact_public_holidays.source_revision IS
  'One calendar snapshot for the covered interval. Do not mix per-date snapshots.';

CREATE FUNCTION core.reject_location_geometry_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.latitude IS DISTINCT FROM OLD.latitude
     OR NEW.longitude IS DISTINCT FROM OLD.longitude
     OR NEW.timezone IS DISTINCT FROM OLD.timezone
     OR NEW.country_code IS DISTINCT FROM OLD.country_code
     OR NEW.provider_location_ref IS DISTINCT FROM OLD.provider_location_ref
     OR NEW.weather_grid_description IS DISTINCT FROM OLD.weather_grid_description
  THEN
    RAISE EXCEPTION 'location geometry is immutable';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER dim_location_geometry_immutable
BEFORE UPDATE ON core.dim_location
FOR EACH ROW
EXECUTE FUNCTION core.reject_location_geometry_change();

ALTER TABLE publish.daily_external_factors_ph
  ADD CONSTRAINT daily_external_factors_ph_date_fkey
    FOREIGN KEY (date) REFERENCES core.dim_date(date),
  ADD CONSTRAINT daily_external_factors_ph_location_fkey
    FOREIGN KEY (location_id) REFERENCES core.dim_location(location_id),
  ADD CONSTRAINT daily_external_factors_ph_weather_source_fkey
    FOREIGN KEY (weather_source_id) REFERENCES control.source_registry(source_id),
  ADD CONSTRAINT daily_external_factors_ph_holiday_source_fkey
    FOREIGN KEY (holiday_source_id) REFERENCES control.source_registry(source_id);

ALTER TABLE core.dim_location OWNER TO of_migrate;
ALTER TABLE core.dim_date OWNER TO of_migrate;
ALTER TABLE core.fact_daily_weather OWNER TO of_migrate;
ALTER TABLE core.fact_public_holidays OWNER TO of_migrate;
ALTER TABLE core.holiday_calendar_coverage OWNER TO of_migrate;
ALTER FUNCTION core.reject_location_geometry_change() OWNER TO of_migrate;

REVOKE ALL ON TABLE
  core.dim_location,
  core.dim_date,
  core.fact_daily_weather,
  core.fact_public_holidays,
  core.holiday_calendar_coverage
FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
  core.dim_location,
  core.dim_date,
  core.fact_daily_weather,
  core.fact_public_holidays,
  core.holiday_calendar_coverage
TO of_transform;

GRANT SELECT ON TABLE control.source_registry, control.raw_manifests TO of_transform;
GRANT SELECT ON TABLE core.dim_location, core.dim_date TO of_publish;
GRANT SELECT ON TABLE control.source_registry TO of_publish;
