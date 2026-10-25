-- Sample orders are not an OpenFactors measurement.
-- The join does not show that weather or a holiday caused the orders.
-- The statement pins one release.

SELECT
    business.location_id,
    business.business_date::text AS date,
    business.orders,
    factors.temperature_mean_c::text AS temperature_mean_c,
    factors.precipitation_mm::text AS precipitation_mm,
    CASE
        WHEN factors.is_public_holiday THEN 'true'
        ELSE 'false'
    END AS is_public_holiday
FROM (
    VALUES
        ('ph-manila', DATE '2018-01-01', 4),
        ('ph-manila', DATE '2018-01-02', 5)
) AS business(location_id, business_date, orders)
JOIN publish.accessible_factors AS factors
    ON factors.location_id = business.location_id
    AND factors.date = business.business_date
WHERE factors.release_id = '__RELEASE_ID__'::uuid
ORDER BY business.business_date
