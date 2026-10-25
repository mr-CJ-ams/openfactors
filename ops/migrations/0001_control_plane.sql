-- OF-008 control plane. Column definitions follow the LLD reference DDL.
-- A stored raw manifest cannot be updated or deleted.

CREATE TABLE IF NOT EXISTS control.schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE control.source_registry (
  source_id TEXT PRIMARY KEY,
  provider_name TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_grain TEXT NOT NULL,
  license_reference_url TEXT,
  redistribution_status TEXT NOT NULL DEFAULT 'unreviewed'
    CHECK (redistribution_status IN ('unreviewed', 'approved', 'rejected', 'suspended')),
  attribution_text TEXT,
  commercial_use_allowed BOOLEAN,
  retention_mode TEXT NOT NULL DEFAULT 'until_source_withdrawn'
    CHECK (retention_mode IN ('until_source_withdrawn', 'days')),
  raw_retention_days INTEGER,
  public_export_allowed BOOLEAN NOT NULL DEFAULT FALSE,
  approved_by TEXT,
  approved_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (retention_mode <> 'days' OR raw_retention_days > 0),
  CHECK (retention_mode <> 'until_source_withdrawn' OR raw_retention_days IS NULL),
  CONSTRAINT source_approval_complete CHECK (
    redistribution_status <> 'approved' OR
    (license_reference_url IS NOT NULL AND approved_by IS NOT NULL
     AND approved_at IS NOT NULL)
  )
);

CREATE TABLE control.ingestion_runs (
  run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id TEXT NOT NULL REFERENCES control.source_registry(source_id),
  stream TEXT NOT NULL,
  location_id TEXT,
  interval_start DATE NOT NULL,
  interval_end DATE NOT NULL,
  code_revision TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
  rows_received BIGINT NOT NULL DEFAULT 0 CHECK (rows_received >= 0),
  rows_accepted BIGINT NOT NULL DEFAULT 0 CHECK (rows_accepted >= 0),
  error_category TEXT,
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (interval_start <= interval_end),
  CHECK (rows_accepted <= rows_received),
  CHECK (finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE control.raw_manifests (
  artifact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID NOT NULL REFERENCES control.ingestion_runs(run_id),
  source_id TEXT NOT NULL REFERENCES control.source_registry(source_id),
  object_key TEXT NOT NULL UNIQUE,
  sha256_hex CHAR(64) NOT NULL CHECK (sha256_hex ~ '^[0-9a-f]{64}$'),
  content_type TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  retrieved_at TIMESTAMPTZ NOT NULL,
  source_updated_at TIMESTAMPTZ,
  response_status INTEGER,
  bytes_stored BIGINT NOT NULL CHECK (bytes_stored >= 0),
  retention_until TIMESTAMPTZ,
  UNIQUE (run_id, request_fingerprint)
);

CREATE INDEX idx_manifests_source_retrieved
  ON control.raw_manifests(source_id, retrieved_at DESC);

CREATE TABLE control.checkpoints (
  source_id TEXT NOT NULL REFERENCES control.source_registry(source_id),
  location_id TEXT NOT NULL,
  stream TEXT NOT NULL,
  last_durable_date DATE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, location_id, stream)
);

CREATE FUNCTION control.reject_manifest_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'raw_manifests is immutable';
END;
$$;

CREATE TRIGGER raw_manifests_immutable
BEFORE UPDATE OR DELETE ON control.raw_manifests
FOR EACH ROW
EXECUTE FUNCTION control.reject_manifest_mutation();

ALTER TABLE control.schema_migrations OWNER TO of_migrate;
ALTER TABLE control.source_registry OWNER TO of_migrate;
ALTER TABLE control.ingestion_runs OWNER TO of_migrate;
ALTER TABLE control.raw_manifests OWNER TO of_migrate;
ALTER TABLE control.checkpoints OWNER TO of_migrate;
ALTER FUNCTION control.reject_manifest_mutation() OWNER TO of_migrate;

REVOKE ALL ON TABLE
  control.schema_migrations,
  control.source_registry,
  control.ingestion_runs,
  control.raw_manifests,
  control.checkpoints
FROM PUBLIC;

GRANT SELECT ON TABLE control.source_registry TO of_ingest;
GRANT SELECT, INSERT, UPDATE ON TABLE control.ingestion_runs TO of_ingest;
GRANT SELECT, INSERT ON TABLE control.raw_manifests TO of_ingest;
GRANT SELECT, INSERT, UPDATE ON TABLE control.checkpoints TO of_ingest;
