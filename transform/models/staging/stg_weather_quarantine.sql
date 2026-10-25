SELECT
    artifact_id,
    source_id,
    location_id,
    source_revision,
    observation_date AS date,
    quarantine_reason,
    retrieved_at
FROM {{ ref('stg_weather_parsed') }}
WHERE quarantine_reason IS NOT NULL
