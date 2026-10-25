-- OF-028. of_api can read the 12 public columns of a published release
-- whose sources are still approved. release_id is the pin, not a response field.
-- The active serving view stays limited to the current pointer.

CREATE VIEW publish.accessible_factors
WITH (security_barrier = true, security_invoker = false) AS
SELECT
  factors.date,
  factors.location_id,
  btrim(factors.country_code) AS country_code,
  factors.temperature_mean_c,
  factors.precipitation_mm,
  factors.is_public_holiday,
  factors.holiday_name,
  factors.is_weekend,
  factors.weather_source_id,
  factors.holiday_source_id,
  factors.source_updated_at,
  factors.dataset_version,
  factors.release_id
FROM publish.daily_external_factors_ph AS factors
JOIN publish.accessible_releases AS releases
  ON releases.release_id = factors.release_id;

ALTER VIEW publish.accessible_factors OWNER TO of_publish;
REVOKE ALL ON TABLE publish.accessible_factors FROM PUBLIC;
GRANT SELECT ON TABLE publish.accessible_factors TO of_api;
