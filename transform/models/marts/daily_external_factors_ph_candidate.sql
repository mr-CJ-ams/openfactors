-- Candidate gold row. The first 12 columns are the public contract, in that order.
-- weather_data_status and release_id stay internal. release_id is empty until a release is pinned.
SELECT
    days.date,
    days.location_id,
    days.country_code::char(2) AS country_code,
    days.temperature_mean_c::numeric(7, 3) AS temperature_mean_c,
    days.precipitation_mm::numeric(10, 3) AS precipitation_mm,
    days.is_public_holiday,
    days.holiday_name,
    days.is_weekend,
    CASE
        WHEN days.weather_artifact_id IS NULL THEN pinned.weather_source_id
        ELSE days.weather_source_id
    END AS weather_source_id,
    days.holiday_source_id,
    CASE
        WHEN days.weather_source_updated_at IS NULL THEN days.holiday_source_updated_at
        WHEN days.holiday_source_updated_at IS NULL THEN days.weather_source_updated_at
        ELSE greatest(days.weather_source_updated_at, days.holiday_source_updated_at)
    END AS source_updated_at,
    '{{ var("dataset_version") }}'::text AS dataset_version,
    CASE
        WHEN days.weather_artifact_id IS NULL THEN 'missing'
        ELSE days.weather_data_status
    END AS weather_data_status,
    NULL::uuid AS release_id
FROM {{ ref('int_location_day') }} AS days
INNER JOIN {{ ref('location_source_map') }} AS pinned
    ON pinned.location_id = days.location_id
WHERE days.holiday_source_id = pinned.holiday_source_id
    AND (
        days.weather_artifact_id IS NULL
        OR days.weather_source_id = pinned.weather_source_id
    )
