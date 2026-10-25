-- OF-017. Typed staging reads a landed JSON payload joined to its manifest.
-- The payload bytes stay in object storage. This table is the parse input.

CREATE TABLE staging.landed_payload (
  artifact_id UUID PRIMARY KEY REFERENCES control.raw_manifests(artifact_id),
  source_id TEXT NOT NULL REFERENCES control.source_registry(source_id),
  location_id TEXT,
  payload JSONB NOT NULL,
  CHECK (jsonb_typeof(payload) = 'object'),
  CHECK (
    (source_id = 'open-meteo-era5' AND location_id IS NOT NULL)
    OR (source_id = 'ph-official-gazette' AND location_id IS NULL)
  )
);

ALTER TABLE staging.landed_payload OWNER TO of_migrate;

REVOKE ALL ON TABLE staging.landed_payload FROM PUBLIC;

GRANT USAGE ON SCHEMA staging TO of_ingest;
GRANT SELECT, INSERT ON TABLE staging.landed_payload TO of_ingest;
GRANT SELECT ON TABLE staging.landed_payload TO of_transform;
GRANT USAGE ON SCHEMA control TO of_transform;
