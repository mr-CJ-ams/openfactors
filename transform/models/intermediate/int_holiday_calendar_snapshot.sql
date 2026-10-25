-- One holiday snapshot for every covered year, including days with no observance.
-- Observances from a losing revision are not mixed into the winner.
WITH revision_keys AS (
    SELECT
        btrim(source_revision) AS source_revision,
        source_id,
        artifact_id,
        source_updated_at,
        retrieved_at
    FROM {{ ref('stg_holidays') }}
    GROUP BY 1, 2, 3, 4, 5
),
ranked AS (
    SELECT
        revision_keys.*,
        row_number() OVER (
            ORDER BY {{ revision_precedence() }}
        ) AS revision_rank
    FROM revision_keys
),
winner AS (
    SELECT
        source_revision,
        source_id,
        artifact_id,
        source_updated_at,
        retrieved_at
    FROM ranked
    WHERE revision_rank = 1
),
covered_years AS (
    SELECT DISTINCT extract(YEAR FROM holidays.date)::integer AS calendar_year
    FROM {{ ref('stg_holidays') }} AS holidays
    INNER JOIN winner ON winner.source_revision = btrim(holidays.source_revision)
),
verified_dates AS (
    SELECT dates.date
    FROM {{ ref('dim_date') }} AS dates
    INNER JOIN covered_years ON covered_years.calendar_year = dates.calendar_year
),
names AS (
    SELECT
        holidays.date,
        count(DISTINCT holidays.holiday_name)::integer AS holiday_count,
        string_agg(DISTINCT holidays.holiday_name, ' | ' ORDER BY holidays.holiday_name)
            AS holiday_name
    FROM {{ ref('stg_holidays') }} AS holidays
    INNER JOIN winner ON winner.source_revision = btrim(holidays.source_revision)
    GROUP BY holidays.date
)
SELECT
    verified_dates.date,
    coalesce(names.holiday_count, 0) AS holiday_count,
    CASE
        WHEN coalesce(names.holiday_count, 0) = 0 THEN NULL
        ELSE names.holiday_name
    END AS holiday_name,
    coalesce(names.holiday_count, 0) > 0 AS is_public_holiday,
    winner.source_revision,
    winner.source_id,
    winner.artifact_id,
    winner.source_updated_at,
    winner.retrieved_at
FROM verified_dates
CROSS JOIN winner
LEFT JOIN names ON names.date = verified_dates.date
