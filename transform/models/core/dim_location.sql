-- Approved pilot locations. The stored row is core.dim_location.
SELECT
    location_id,
    display_name,
    country_code,
    admin_region,
    admin_province,
    latitude,
    longitude,
    timezone,
    provider_location_ref,
    weather_grid_description,
    valid_from,
    valid_to,
    approved_for_mvp
FROM {{ source('openfactors_core', 'dim_location') }}
WHERE approved_for_mvp
