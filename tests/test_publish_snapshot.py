"""Selective snapshot creation and isolated candidate validation."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from ops.migrate import apply, psql
from ops.promote import active_release_id, promote_release
from ops.publish_snapshot import (
    REQUIRED_ROLES,
    SnapshotError,
    _admin,
    _database_exists,
    abort_candidate,
    assert_cluster_roles,
    create_snapshot,
    drop_database,
    file_sha256,
    load_manifest,
    restore_candidate,
    verify_dump,
)
from ops.releases import persist_manifest
from tests.release_support import (
    ENVIRONMENT,
    cleanup_steps,
    discard_test_releases,
    ensure_isolated_database,
    restore_previous_release,
)
from tests.test_release_manifest import _row
from tests.test_release_promotion import _approved

_DEV_RELEASE = """
SELECT r.release_id::text || '|' || r.status || '|' || r.actual_row_count::text
       || '|' || r.content_sha256_hex
FROM publish.active_dataset_release AS a
JOIN publish.dataset_releases AS r ON r.release_id = a.release_id;
"""


def test_a_changed_dump_byte_fails_the_checksum(tmp_path: Path) -> None:
    dump = tmp_path / "release.dump"
    dump.write_bytes(b"snapshot-bytes")
    manifest = _manifest_file(tmp_path, dump)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["dump_sha256_hex"] = "ab" * 32
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotError, match="checksum"):
        verify_dump(dump, load_manifest(manifest))


@pytest.mark.parametrize(
    "database",
    [
        "openfactors_dev",
        "openfactors_test",
        "openfactors_staging",
        "openfactors_serving",
        "openfactors_previous",
        "postgres",
        "template0",
        "template1",
        "of_restore_drill",
    ],
)
def test_cleanup_refuses_a_protected_database(database: str) -> None:
    with pytest.raises(SnapshotError, match="allow-listed"):
        drop_database("dev", database)


def test_a_manifest_from_an_unexpected_database_is_rejected(tmp_path: Path) -> None:
    dump = tmp_path / "release.dump"
    dump.write_bytes(b"snapshot-bytes")
    manifest = _manifest_file(tmp_path, dump)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["source_database"] = "openfactors_serving"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotError, match="source database"):
        load_manifest(manifest)


def test_a_candidate_matches_the_release_and_development_data_stays(tmp_path: Path) -> None:
    dev_before = _development_release()
    with _published_release() as (release_id, manifest):
        dump = create_snapshot(ENVIRONMENT, tmp_path)
        evidence = restore_candidate(ENVIRONMENT, dump)
        assert evidence == {
            "candidate_database": "openfactors_candidate",
            "release_id": release_id,
            "dataset_version": "1.0.0",
            "row_count": 1,
            "content_sha256_hex": manifest.content_sha256_hex,
        }
        recorded = load_manifest(dump.with_suffix(".json"))
        assert recorded["dump_sha256_hex"] == file_sha256(dump)
        assert recorded["source_database"] == "openfactors_test"
        assert recorded["excluded_schemas"] == ["staging"]
        assert recorded["excluded_tables"] == [
            "core.fact_daily_weather",
            "core.fact_public_holidays",
            "core.holiday_calendar_coverage",
            "control.raw_manifests",
            "control.ingestion_runs",
            "control.checkpoints",
        ]
    assert _development_release() == dev_before


def test_a_checksum_mismatch_does_not_create_a_candidate(tmp_path: Path) -> None:
    with _published_release():
        dump = create_snapshot(ENVIRONMENT, tmp_path)
        manifest_path = dump.with_suffix(".json")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["dump_sha256_hex"] = "cd" * 32
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SnapshotError, match="checksum"):
            restore_candidate(ENVIRONMENT, dump)
        assert _candidate_absent()


def test_a_damaged_dump_with_a_matching_checksum_removes_the_candidate(tmp_path: Path) -> None:
    with _published_release():
        dump = create_snapshot(ENVIRONMENT, tmp_path)
        dump.write_bytes(b"this is not a postgres custom dump")
        manifest_path = dump.with_suffix(".json")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["dump_sha256_hex"] = hashlib.sha256(dump.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SnapshotError):
            restore_candidate(ENVIRONMENT, dump)
        assert _candidate_absent()


def test_a_missing_role_is_reported_before_a_candidate_exists() -> None:
    ensure_isolated_database()
    abort_candidate(ENVIRONMENT)
    with pytest.raises(SnapshotError, match="of_role_absent"):
        assert_cluster_roles(ENVIRONMENT, (*REQUIRED_ROLES, "of_role_absent"))
    assert _candidate_absent()


def test_an_existing_candidate_is_left_in_place(tmp_path: Path) -> None:
    ensure_isolated_database()
    apply(ENVIRONMENT)
    abort_candidate(ENVIRONMENT)
    _admin(ENVIRONMENT, "postgres", "CREATE DATABASE openfactors_candidate;")
    _admin(ENVIRONMENT, "openfactors_candidate", "CREATE TABLE kept_marker(id integer);")
    try:
        with pytest.raises(SnapshotError, match="already exists"):
            restore_candidate(ENVIRONMENT, tmp_path / "missing.dump")
        marker = _admin(
            ENVIRONMENT,
            "openfactors_candidate",
            "SELECT to_regclass('kept_marker')::text;",
        )
        assert "kept_marker" in marker
    finally:
        abort_candidate(ENVIRONMENT)
    assert _candidate_absent()


class _Published:
    def __init__(self) -> None:
        self.release_id = ""
        self.manifest = None
        self.previous: str | None = None
        self.started = False

    def __enter__(self):
        ensure_isolated_database()
        self.started = True
        abort_candidate(ENVIRONMENT)
        apply(ENVIRONMENT)
        self.previous = active_release_id(ENVIRONMENT)
        self.release_id = str(uuid.uuid4())
        contract = load_json(CONTRACT_PATH)
        self.manifest = _approved(contract, _row(), self.release_id)
        persist_manifest(ENVIRONMENT, self.manifest)
        promote_release(ENVIRONMENT, self.manifest, [_row()])
        return self.release_id, self.manifest

    def __exit__(self, exc_type, exc, traceback) -> None:
        if not self.started:
            return
        abort_candidate(ENVIRONMENT)
        cleanup_steps(
            lambda: restore_previous_release(ENVIRONMENT, self.previous),
            lambda: discard_test_releases(ENVIRONMENT, [self.release_id]),
        )


def _published_release() -> _Published:
    return _Published()


def _manifest_file(directory: Path, dump: Path) -> Path:
    payload = {
        "content_sha256_hex": "ab" * 32,
        "dataset_id": "daily_external_factors_ph",
        "dataset_version": "1.0.0",
        "dump_format": "custom",
        "dump_sha256_hex": file_sha256(dump),
        "excluded_schemas": ["staging"],
        "excluded_tables": [
            "core.fact_daily_weather",
            "core.fact_public_holidays",
            "core.holiday_calendar_coverage",
            "control.raw_manifests",
            "control.ingestion_runs",
            "control.checkpoints",
        ],
        "release_id": "66196674-ae81-4c18-a0d6-85e7e0b7c056",
        "row_count": 1,
        "source_database": "openfactors_test",
    }
    path = directory / "release.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _development_release() -> str:
    completed = psql("dev", "of_admin", _DEV_RELEASE, tuples_only=True)
    if completed.returncode != 0:
        return "unavailable"
    return completed.stdout.strip()


def _candidate_absent() -> bool:
    return not _database_exists(ENVIRONMENT, "openfactors_candidate")
