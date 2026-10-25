-- One row per landed ERA5 day, or one quarantine row when the payload cannot be expanded.
-- source_revision is the manifest SHA-256, so the same bytes always parse the same way.

WITH landed AS (
    SELECT
        payload.artifact_id,
        payload.source_id,
        payload.location_id,
        payload.payload,
        manifest.sha256_hex AS source_revision,
        manifest.retrieved_at,
        manifest.source_updated_at,
        registry.redistribution_status,
        payload.payload ->> 'timezone' AS timezone_name,
        payload.payload ->> 'utc_offset_seconds' AS utc_offset_seconds,
        payload.payload #>> '{daily_units,temperature_2m_mean}' AS temperature_unit,
        payload.payload #>> '{daily_units,precipitation_sum}' AS precipitation_unit,
        payload.payload -> 'daily' -> 'time' AS times,
        payload.payload -> 'daily' -> 'temperature_2m_mean' AS temperatures,
        payload.payload -> 'daily' -> 'precipitation_sum' AS precipitation
    FROM {{ source('openfactors_staging', 'landed_payload') }} AS payload
    INNER JOIN {{ source('openfactors_control', 'raw_manifests') }} AS manifest
        ON manifest.artifact_id = payload.artifact_id
        AND manifest.source_id = payload.source_id
    INNER JOIN {{ source('openfactors_control', 'source_registry') }} AS registry
        ON registry.source_id = payload.source_id
    WHERE payload.source_id = 'open-meteo-era5'
),
classified AS (
    SELECT
        landed.*,
        CASE
            WHEN landed.redistribution_status <> 'approved' THEN 'unapproved_source'
            WHEN landed.source_updated_at = landed.retrieved_at THEN 'retrieval_time_substituted'
            WHEN landed.timezone_name IS DISTINCT FROM 'Asia/Manila' THEN 'wrong_timezone'
            WHEN landed.utc_offset_seconds IS DISTINCT FROM '28800' THEN 'wrong_offset'
            WHEN landed.temperature_unit IS DISTINCT FROM (chr(176) || 'C') THEN 'wrong_temperature_unit'
            WHEN landed.precipitation_unit IS DISTINCT FROM 'mm' THEN 'wrong_precipitation_unit'
            WHEN jsonb_typeof(landed.times) IS DISTINCT FROM 'array'
                OR jsonb_typeof(landed.temperatures) IS DISTINCT FROM 'array'
                OR jsonb_typeof(landed.precipitation) IS DISTINCT FROM 'array'
                THEN 'array_length_mismatch'
            WHEN jsonb_array_length(landed.times)
                    IS DISTINCT FROM jsonb_array_length(landed.temperatures)
                OR jsonb_array_length(landed.times)
                    IS DISTINCT FROM jsonb_array_length(landed.precipitation)
                THEN 'array_length_mismatch'
        END AS artifact_reason
    FROM landed
),
blocked AS (
    SELECT
        classified.artifact_id,
        classified.source_id,
        classified.location_id,
        classified.source_revision,
        classified.retrieved_at,
        classified.source_updated_at,
        NULL::date AS observation_date,
        NULL::numeric AS temperature_mean_c,
        NULL::numeric AS precipitation_mm,
        NULL::text AS weather_data_status,
        classified.artifact_reason AS quarantine_reason
    FROM classified
    WHERE classified.artifact_reason IS NOT NULL
),
expanded AS (
    SELECT
        classified.artifact_id,
        classified.source_id,
        classified.location_id,
        classified.source_revision,
        classified.retrieved_at,
        classified.source_updated_at,
        time_item.value AS time_value,
        temperature_item.value AS temperature_value,
        precipitation_item.value AS precipitation_value
    FROM classified
    CROSS JOIN LATERAL jsonb_array_elements(classified.times)
        WITH ORDINALITY AS time_item (value, ordinality)
    INNER JOIN LATERAL jsonb_array_elements(classified.temperatures)
        WITH ORDINALITY AS temperature_item (value, ordinality)
        ON temperature_item.ordinality = time_item.ordinality
    INNER JOIN LATERAL jsonb_array_elements(classified.precipitation)
        WITH ORDINALITY AS precipitation_item (value, ordinality)
        ON precipitation_item.ordinality = time_item.ordinality
    WHERE classified.artifact_reason IS NULL
),
measured AS (
    SELECT
        expanded.*,
        CASE
            WHEN expanded.temperature_value = 'null'::jsonb THEN NULL
            WHEN jsonb_typeof(expanded.temperature_value) = 'number'
                THEN (expanded.temperature_value #>> '{}')::numeric
        END AS temperature_mean_c,
        CASE
            WHEN expanded.precipitation_value = 'null'::jsonb THEN NULL
            WHEN jsonb_typeof(expanded.precipitation_value) = 'number'
                THEN (expanded.precipitation_value #>> '{}')::numeric
        END AS precipitation_mm,
        expanded.time_value #>> '{}' AS time_text,
        jsonb_typeof(expanded.temperature_value) AS temperature_type,
        jsonb_typeof(expanded.precipitation_value) AS precipitation_type
    FROM expanded
),
parsed AS (
    SELECT
        measured.artifact_id,
        measured.source_id,
        measured.location_id,
        measured.source_revision,
        measured.retrieved_at,
        measured.source_updated_at,
        CASE
            WHEN measured.time_text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                THEN measured.time_text::date
        END AS observation_date,
        measured.temperature_mean_c,
        measured.precipitation_mm,
        CASE
            WHEN measured.temperature_mean_c IS NULL AND measured.precipitation_mm IS NULL
                THEN 'missing'
            ELSE 'available'
        END AS weather_data_status,
        CASE
            WHEN measured.time_text !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN 'unparseable_date'
            WHEN measured.temperature_value <> 'null'::jsonb
                AND measured.temperature_type IS DISTINCT FROM 'number'
                THEN 'non_numeric_measure'
            WHEN measured.precipitation_value <> 'null'::jsonb
                AND measured.precipitation_type IS DISTINCT FROM 'number'
                THEN 'non_numeric_measure'
            WHEN measured.precipitation_mm < 0 THEN 'negative_precipitation'
        END AS quarantine_reason
    FROM measured
)
SELECT * FROM blocked
UNION ALL
SELECT * FROM parsed
