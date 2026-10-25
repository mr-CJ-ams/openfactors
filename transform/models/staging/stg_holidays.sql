SELECT
    observation_date AS date,
    source_revision || ':' || holiday_category || ':' || holiday_name AS holiday_id,
    source_revision,
    source_id,
    artifact_id,
    holiday_name,
    holiday_category,
    scope,
    source_updated_at,
    retrieved_at
FROM {{ ref('stg_holidays_parsed') }}
WHERE quarantine_reason IS NULL
