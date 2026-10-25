SELECT
    artifact_id,
    source_id,
    source_revision,
    observation_date AS date,
    holiday_name,
    quarantine_reason,
    retrieved_at
FROM {{ ref('stg_holidays_parsed') }}
WHERE quarantine_reason IS NOT NULL
