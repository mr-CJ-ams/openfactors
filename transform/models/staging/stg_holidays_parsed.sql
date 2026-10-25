-- One accepted or quarantined holiday observance.
-- An incomplete year and a shape fixture produce no accepted row.

WITH landed AS (
    SELECT
        payload.artifact_id,
        payload.source_id,
        payload.payload,
        manifest.sha256_hex AS source_revision,
        manifest.retrieved_at,
        manifest.source_updated_at,
        registry.redistribution_status,
        payload.payload ->> 'declared_status' AS declared_status,
        COALESCE(payload.payload -> 'not_a_proclamation' = 'true'::jsonb, FALSE) AS not_a_proclamation,
        payload.payload -> 'observances' AS observances
    FROM {{ source('openfactors_staging', 'landed_payload') }} AS payload
    INNER JOIN {{ source('openfactors_control', 'raw_manifests') }} AS manifest
        ON manifest.artifact_id = payload.artifact_id
        AND manifest.source_id = payload.source_id
    INNER JOIN {{ source('openfactors_control', 'source_registry') }} AS registry
        ON registry.source_id = payload.source_id
    WHERE payload.source_id = 'ph-official-gazette'
),
classified AS (
    SELECT
        landed.*,
        CASE
            WHEN landed.redistribution_status <> 'approved' THEN 'unapproved_source'
            WHEN landed.source_updated_at = landed.retrieved_at THEN 'retrieval_time_substituted'
            WHEN landed.not_a_proclamation THEN 'not_a_proclamation'
            WHEN jsonb_typeof(landed.observances) IS DISTINCT FROM 'array' THEN 'observances_missing'
            WHEN jsonb_array_length(landed.observances) = 0 THEN 'observances_missing'
        END AS artifact_reason
    FROM landed
),
observance AS (
    SELECT
        classified.artifact_id,
        classified.source_id,
        classified.source_revision,
        classified.retrieved_at,
        classified.source_updated_at,
        classified.declared_status,
        classified.artifact_reason,
        btrim(item.value ->> 'name') AS holiday_name,
        item.value ->> 'category' AS holiday_category,
        item.value ->> 'scope' AS scope,
        item.value ->> 'date' AS date_text
    FROM classified
    CROSS JOIN LATERAL jsonb_array_elements(classified.observances) AS item (value)
    WHERE classified.artifact_reason IS NULL
),
year_state AS (
    SELECT
        observance.artifact_id,
        bool_or(
            observance.scope = 'national'
            AND observance.holiday_category IN ('regular', 'special_non_working')
            AND (
                observance.date_text IS NULL
                OR observance.date_text !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
            )
        ) AS has_unresolved_national,
        bool_or(observance.declared_status IS DISTINCT FROM 'complete') AS declared_incomplete
    FROM observance
    GROUP BY observance.artifact_id
),
parsed AS (
    SELECT
        observance.artifact_id,
        observance.source_id,
        observance.source_revision,
        observance.retrieved_at,
        observance.source_updated_at,
        CASE
            WHEN observance.date_text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                THEN observance.date_text::date
        END AS observation_date,
        observance.holiday_name,
        observance.holiday_category,
        observance.scope,
        CASE
            WHEN year_state.declared_incomplete OR year_state.has_unresolved_national
                THEN 'incomplete_year'
            WHEN observance.holiday_name IS NULL OR observance.holiday_name = '' THEN 'empty_name'
            WHEN observance.scope IS DISTINCT FROM 'national'
                OR observance.holiday_category NOT IN ('regular', 'special_non_working')
                THEN 'out_of_scope'
            WHEN observance.date_text !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN 'unparseable_date'
        END AS quarantine_reason
    FROM observance
    INNER JOIN year_state ON year_state.artifact_id = observance.artifact_id
),
blocked AS (
    SELECT
        classified.artifact_id,
        classified.source_id,
        classified.source_revision,
        classified.retrieved_at,
        classified.source_updated_at,
        NULL::date AS observation_date,
        NULL::text AS holiday_name,
        NULL::text AS holiday_category,
        NULL::text AS scope,
        classified.artifact_reason AS quarantine_reason
    FROM classified
    WHERE classified.artifact_reason IS NOT NULL
)
SELECT * FROM blocked
UNION ALL
SELECT * FROM parsed
