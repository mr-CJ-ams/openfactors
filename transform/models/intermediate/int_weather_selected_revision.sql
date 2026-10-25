-- One weather revision per location-day.
-- A meaningful source_updated_at beats a later retrieval.
-- The same timestamps keep the greater artifact_id.
WITH ranked AS (
    SELECT
        location_id,
        date,
        btrim(source_revision) AS source_revision,
        source_id,
        artifact_id,
        temperature_mean_c,
        precipitation_mm,
        weather_data_status,
        source_updated_at,
        retrieved_at,
        row_number() OVER (
            PARTITION BY location_id, date
            ORDER BY {{ revision_precedence() }}
        ) AS revision_rank
    FROM {{ ref('stg_weather') }}
)
SELECT
    location_id,
    date,
    source_revision,
    source_id,
    artifact_id,
    temperature_mean_c,
    precipitation_mm,
    weather_data_status,
    source_updated_at,
    retrieved_at
FROM ranked
WHERE revision_rank = 1
