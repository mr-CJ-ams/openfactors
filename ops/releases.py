"""Store a candidate release manifest and its quality evidence.

The insert does not copy rows into the public fact table and does not switch
the active release.
"""

from __future__ import annotations

import json

from openfactors.quality.manifest import ReleaseManifest
from ops.migrate import psql, sql_literal


def persist_manifest(environment: str, manifest: ReleaseManifest) -> None:
    if manifest.status not in {"approved", "rejected"}:
        raise ValueError("A candidate manifest is approved or rejected.")
    for name in ("expected_row_count", "actual_row_count"):
        value = getattr(manifest, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Release counts must be non-negative integers.")
    evidence = json.dumps(
        manifest.evidence(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    validation_status = "PASS" if manifest.status == "approved" else "FAIL"
    statement = f"""
    BEGIN;
    INSERT INTO publish.dataset_releases (
      release_id, dataset_id, schema_version, status,
      source_manifest_set_hash, content_sha256_hex,
      expected_row_count, actual_row_count,
      coverage_start, coverage_end, validated_at
    ) VALUES (
      {sql_literal(manifest.release_id)}::uuid,
      {sql_literal(manifest.dataset_id)},
      {sql_literal(manifest.schema_version)},
      {sql_literal(manifest.status)},
      {sql_literal(manifest.source_manifest_set_hash)},
      {sql_literal(manifest.content_sha256_hex)},
      {manifest.expected_row_count},
      {manifest.actual_row_count},
      {sql_literal(manifest.coverage_start)}::date,
      {sql_literal(manifest.coverage_end)}::date,
      {sql_literal(manifest.validated_at)}::timestamptz
    );
    INSERT INTO quality.validation_runs (
      release_id, status, evaluated_at,
      expected_location_dates, present_location_dates,
      weather_present_location_dates, documented_upstream_gaps,
      result_json
    ) VALUES (
      {sql_literal(manifest.release_id)}::uuid,
      {sql_literal(validation_status)},
      {sql_literal(manifest.validated_at)}::timestamptz,
      {manifest.coverage.expected},
      {manifest.coverage.present},
      {manifest.coverage.weather_present},
      {manifest.coverage.excluded},
      {sql_literal(evidence)}::jsonb
    );
    COMMIT;
    """
    completed = psql(environment, "of_admin", statement)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"Could not store the release manifest.\n{detail}")
