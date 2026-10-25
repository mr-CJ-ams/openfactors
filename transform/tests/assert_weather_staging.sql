SELECT *
FROM {{ ref('stg_weather') }}
WHERE precipitation_mm < 0
    OR (
        weather_data_status = 'available'
        AND temperature_mean_c IS NULL
        AND precipitation_mm IS NULL
    )
    OR (
        weather_data_status = 'missing'
        AND (temperature_mean_c IS NOT NULL OR precipitation_mm IS NOT NULL)
    )
    OR source_updated_at IS NOT DISTINCT FROM retrieved_at
    OR source_id <> 'open-meteo-era5'
    OR source_revision IS NULL
