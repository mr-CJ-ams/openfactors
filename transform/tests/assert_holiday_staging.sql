SELECT *
FROM {{ ref('stg_holidays') }}
WHERE scope <> 'national'
    OR holiday_category NOT IN ('regular', 'special_non_working')
    OR holiday_name IS NULL
    OR btrim(holiday_name) = ''
    OR source_id <> 'ph-official-gazette'
    OR source_updated_at IS NOT DISTINCT FROM retrieved_at
