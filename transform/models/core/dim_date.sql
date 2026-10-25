-- Asia/Manila date spine. The stored row is core.dim_date.
SELECT
    date,
    day_of_week,
    is_weekend,
    calendar_year,
    calendar_month
FROM {{ source('openfactors_core', 'dim_date') }}
