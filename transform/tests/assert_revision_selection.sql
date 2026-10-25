SELECT failure
FROM (
    SELECT 'duplicate_weather' AS failure
    FROM {{ ref('int_weather_selected_revision') }}
    GROUP BY location_id, date
    HAVING count(*) > 1
    UNION ALL
    SELECT 'duplicate_location_day' AS failure
    FROM {{ ref('int_location_day') }}
    GROUP BY location_id, date
    HAVING count(*) > 1
    UNION ALL
    SELECT 'holiday_flag' AS failure
    FROM {{ ref('int_location_day') }}
    WHERE is_public_holiday IS NULL
        OR (is_public_holiday AND coalesce(btrim(holiday_name), '') = '')
        OR (NOT is_public_holiday AND holiday_name IS NOT NULL)
        OR (holiday_count = 0) IS DISTINCT FROM (NOT is_public_holiday)
    UNION ALL
    SELECT 'holiday_broadcast' AS failure
    FROM (
        SELECT date
        FROM {{ ref('int_location_day') }}
        GROUP BY date
        HAVING count(DISTINCT holiday_name) > 1
            OR count(DISTINCT holiday_source_revision) > 1
            OR (count(holiday_name) <> 0 AND count(holiday_name) <> count(*))
    ) AS mixed
) AS failures
