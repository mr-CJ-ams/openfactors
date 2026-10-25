SELECT
    location_id,
    observation_date AS date,
    source_revision,
    source_id,
    artifact_id,
    temperature_mean_c,
    precipitation_mm,
    weather_data_status,
    source_updated_at,
    retrieved_at
FROM {{ ref('stg_weather_parsed') }}
WHERE quarantine_reason IS NULL
