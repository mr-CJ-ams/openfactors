SELECT failure
FROM (
    SELECT 'unpinned_source' AS failure
    FROM {{ ref('daily_external_factors_ph_candidate') }} AS mart
    LEFT JOIN {{ ref('location_source_map') }} AS pinned
        ON pinned.location_id = mart.location_id
    WHERE pinned.location_id IS NULL
        OR mart.weather_source_id IS DISTINCT FROM pinned.weather_source_id
        OR mart.holiday_source_id IS DISTINCT FROM pinned.holiday_source_id
        OR mart.dataset_version IS DISTINCT FROM '{{ var("dataset_version") }}'
        OR mart.country_code IS DISTINCT FROM 'PH'
    UNION ALL
    SELECT 'null_semantics' AS failure
    FROM {{ ref('daily_external_factors_ph_candidate') }}
    WHERE is_public_holiday IS NULL
        OR is_weekend IS NULL
        OR (is_public_holiday AND coalesce(btrim(holiday_name), '') = '')
        OR (NOT is_public_holiday AND holiday_name IS NOT NULL)
        OR (weather_data_status = 'missing' AND (
            temperature_mean_c IS NOT NULL OR precipitation_mm IS NOT NULL
        ))
        OR (weather_data_status = 'available' AND (
            temperature_mean_c IS NULL AND precipitation_mm IS NULL
        ))
        OR precipitation_mm < 0
        OR weather_data_status NOT IN ('available', 'missing', 'upstream_gap')
) AS failures
