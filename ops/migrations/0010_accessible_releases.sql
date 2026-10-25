-- OF-027. of_api can read published releases that still have approved sources.
-- Withdrawn and unapproved releases are absent, so a pin and a missing id look the same.

CREATE VIEW publish.accessible_releases
WITH (security_barrier = true, security_invoker = false) AS
SELECT
  releases.dataset_id,
  releases.release_id,
  releases.schema_version AS dataset_version,
  releases.coverage_start,
  releases.coverage_end,
  releases.published_at
FROM publish.dataset_releases AS releases
WHERE releases.dataset_id = 'daily_external_factors_ph'
  AND releases.status = 'published'
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

ALTER VIEW publish.accessible_releases OWNER TO of_publish;
REVOKE ALL ON TABLE publish.accessible_releases FROM PUBLIC;
GRANT SELECT ON TABLE publish.accessible_releases TO of_api;
