-- National holidays attach once per location-day. Two observances do not add a second row.
SELECT
    spine.location_id,
    spine.country_code,
    spine.date,
    spine.is_weekend,
    weather.temperature_mean_c,
    weather.precipitation_mm,
    weather.weather_data_status,
    weather.source_id AS weather_source_id,
    weather.source_revision AS weather_source_revision,
    weather.artifact_id AS weather_artifact_id,
    weather.source_updated_at AS weather_source_updated_at,
    holidays.is_public_holiday,
    holidays.holiday_name,
    holidays.holiday_count,
    holidays.source_id AS holiday_source_id,
    holidays.source_revision AS holiday_source_revision,
    holidays.artifact_id AS holiday_artifact_id,
    holidays.source_updated_at AS holiday_source_updated_at
FROM {{ ref('int_calendar_location_spine') }} AS spine
INNER JOIN {{ ref('int_holiday_calendar_snapshot') }} AS holidays
    ON holidays.date = spine.date
LEFT JOIN {{ ref('int_weather_selected_revision') }} AS weather
    ON weather.location_id = spine.location_id
    AND weather.date = spine.date
