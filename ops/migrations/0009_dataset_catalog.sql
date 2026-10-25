-- OF-026. of_api can read the active published catalog summary and nothing behind it.
-- A withdrawn release or a release whose source is not approved is omitted.

GRANT USAGE ON SCHEMA control TO of_publish;

CREATE VIEW publish.dataset_catalog
WITH (security_barrier = true, security_invoker = false) AS
SELECT
  releases.dataset_id,
  releases.release_id AS active_release_id,
  releases.schema_version AS dataset_version,
  releases.coverage_start,
  releases.coverage_end,
  releases.published_at
FROM publish.active_dataset_release AS active
JOIN publish.dataset_releases AS releases
  ON releases.release_id = active.release_id
 AND releases.dataset_id = active.dataset_id
 AND releases.status = 'published'
WHERE active.dataset_id = 'daily_external_factors_ph'
  AND NOT EXISTS (
    SELECT 1
    FROM publish.daily_external_factors_ph AS factors
    WHERE factors.release_id = releases.release_id
      AND (
        NOT EXISTS (
          SELECT 1 FROM control.source_registry AS registry
          WHERE registry.source_id = factors.weather_source_id
            AND registry.redistribution_status = 'approved'
        )
        OR NOT EXISTS (
          SELECT 1 FROM control.source_registry AS registry
          WHERE registry.source_id = factors.holiday_source_id
            AND registry.redistribution_status = 'approved'
        )
      )
  );

ALTER VIEW publish.dataset_catalog OWNER TO of_publish;
REVOKE ALL ON TABLE publish.dataset_catalog FROM PUBLIC;
GRANT SELECT ON TABLE publish.dataset_catalog TO of_api;
