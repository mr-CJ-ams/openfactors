-- Approved locations crossed with each Manila date inside their validity window.
SELECT
    locations.location_id,
    btrim(locations.country_code) AS country_code,
    dates.date,
    dates.is_weekend,
    dates.day_of_week
FROM {{ ref('dim_location') }} AS locations
INNER JOIN {{ ref('dim_date') }} AS dates
    ON dates.date >= locations.valid_from
    AND (locations.valid_to IS NULL OR dates.date <= locations.valid_to)
