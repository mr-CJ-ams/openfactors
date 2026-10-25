"""Local recovery drill. The source database is not replaced."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from ops.local_stack import backup, dev_database_ready, restore_isolated
from ops.migrate import apply, psql
from ops.promote import promote_release, rollback_release
from ops.releases import persist_manifest
from tests.test_ingestion_runs import (
    KEEP,
    START,
    MemoryManifests,
    MemoryObjects,
    _batch,
    _payload,
)
from tests.test_release_manifest import _build, _row
from tests.test_release_promotion import (
    _active,
    _cleanup,
    _served_temperature,
    _stored_count,
)

from openfactors.ingestion.checkpoints import CheckpointKey, MemoryCheckpoints
from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.raw_store import (
    FileObjectStore,
    RetentionLabel,
    commit_raw_artifact,
    restore_raw_object,
)
from openfactors.ingestion.runs import MemoryRunLog, execute_run, recover_running_run
from openfactors.orchestration.daily import FailureAlert

NOW = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
RUN_ID = "00000000-0000-4000-8000-000000000040"


def test_a_missing_backup_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="Backup file is missing"):
        restore_isolated("dev", tmp_path / "missing.dump")
    empty = tmp_path / "empty.dump"
    empty.write_bytes(b"")
    with pytest.raises(SystemExit, match="Backup file is missing"):
        restore_isolated("dev", empty)


def test_a_raw_object_is_restored_only_when_the_digest_matches(tmp_path: Path) -> None:
    store = FileObjectStore(tmp_path)
    manifests = MemoryManifests()
    body = b'{"date":"2018-01-01"}'
    artifact = commit_raw_artifact(
        store,
        manifests,
        run_id=RUN_ID,
        source_id="open-meteo-era5",
        body=body,
        content_type="application/json",
        request_fingerprint="open-meteo-era5/ph-manila/2018-01-01",
        retrieved_at=NOW,
        source_updated_at=None,
        response_status=200,
        retention=RetentionLabel("until_source_withdrawn", None),
    )
    backup = store.get(artifact.object_key)
    stored = tmp_path / artifact.object_key
    stored.unlink()
    with pytest.raises(ConnectorError, match="Raw object is missing"):
        store.get(artifact.object_key)
    with pytest.raises(ConnectorError, match="recorded digest"):
        restore_raw_object(store, artifact.object_key, b"other", artifact.sha256_hex)
    assert not stored.is_file()
    restored = restore_raw_object(store, artifact.object_key, backup, artifact.sha256_hex)
    assert restored == body
    stored.write_bytes(b"tampered")
    with pytest.raises(ConnectorError, match="different bytes"):
        restore_raw_object(store, artifact.object_key, backup, artifact.sha256_hex)
    assert stored.read_bytes() == b"tampered"
    assert manifests.rows[(RUN_ID, artifact.request_fingerprint)].sha256_hex == artifact.sha256_hex


def test_a_failed_run_stays_failed_and_a_running_run_resumes() -> None:
    failed_runs = MemoryRunLog()

    def fail(_day: date) -> bytes:
        raise ConnectorError(ErrorCategory.TIMEOUT, "secret-query")

    failed = execute_run(
        failed_runs,
        MemoryCheckpoints(),
        MemoryObjects(),
        MemoryManifests(),
        _batch(START),
        fail,
        now=NOW,
        retention=KEEP,
        run_id_factory=lambda: RUN_ID,
    )
    assert failed.status == "failed"
    alert = FailureAlert(
        run_id=failed.run_id,
        manila_date=START,
        step="weather_fetch",
        category="timeout",
        at=NOW,
    )
    with pytest.raises(ConnectorError, match="stays failed"):
        recover_running_run(
            failed_runs,
            MemoryCheckpoints(),
            MemoryObjects(),
            MemoryManifests(),
            _batch(START),
            _payload,
            now=NOW,
            retention=KEEP,
            resume_run_id=RUN_ID,
        )
    assert failed_runs.get(RUN_ID).status == "failed"
    assert alert.run_id == RUN_ID
    assert "secret-query" not in str(alert)

    runs = MemoryRunLog()
    checkpoints = MemoryCheckpoints()
    objects = MemoryObjects()
    manifests = MemoryManifests()
    seen: list[date] = []

    def crash(day: date) -> bytes:
        seen.append(day)
        if len(seen) == 2:
            raise RuntimeError("injected crash")
        return _payload(day)

    with pytest.raises(RuntimeError, match="injected crash"):
        execute_run(
            runs,
            checkpoints,
            objects,
            manifests,
            _batch(START + timedelta(days=1)),
            crash,
            now=NOW,
            retention=KEEP,
            run_id_factory=lambda: RUN_ID,
        )
    assert runs.get(RUN_ID).status == "running"
    assert checkpoints.get(CheckpointKey("open-meteo-era5", "ph-manila", "daily")) == START

    def resume(day: date) -> bytes:
        seen.append(day)
        return _payload(day)

    finished = recover_running_run(
        runs,
        checkpoints,
        objects,
        manifests,
        _batch(START + timedelta(days=1)),
        resume,
        now=NOW,
        retention=KEEP,
        resume_run_id=RUN_ID,
    )
    assert finished.status == "succeeded"
    assert finished.run_id == RUN_ID
    assert seen == [START, START + timedelta(days=1), START + timedelta(days=1)]
    assert len(objects.bodies) == 2


def test_isolated_restore_and_release_rollback() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required for the restore drill.")
    apply("dev")
    dump_path = backup("dev")
    evidence = restore_isolated("dev", dump_path)
    assert evidence["source_database"] == "openfactors_dev"
    assert evidence["restored_database"] == "of_restore_drill"
    assert evidence["source_database_replaced"] is False
    assert evidence["drill_database_removed"] is True
    assert evidence["source_status"] == "approved"
    assert evidence["api_can_use_publish"] is True
    assert evidence["api_can_use_control"] is False
    assert evidence["rpo_target_seconds"] is None
    assert evidence["rto_target_seconds"] is None
    assert evidence["measured_restore_seconds"] >= 0
    assert evidence["fact_count"] == _fact_count()
    release_a = str(uuid.uuid4())
    release_b = str(uuid.uuid4())
    try:
        contract = _contract()
        first = _approved(contract, _row(), release_a)
        persist_manifest("dev", first)
        promote_release("dev", first, [_row()])
        second_row = _row()
        second_row["temperature_mean_c"] = "18.4"
        second = _approved(contract, second_row, release_b)
        persist_manifest("dev", second)
        promote_release("dev", second, [second_row])
        assert _active() == release_b
        rollback_release("dev", release_a)
        assert _active() == release_a
        assert _served_temperature() == Decimal("26.1")
        assert _stored_count(release_a) == "1"
        assert _stored_count(release_b) == "1"
    finally:
        _cleanup([release_a, release_b])
    assert _fact_count() == 0


def _approved(contract: dict, row: dict, release_id: str):
    manifest = _build(contract, [row], release_id=release_id)
    assert manifest.status == "approved"
    return manifest


def _contract() -> dict:
    from contracts.check_dataset_contract import CONTRACT_PATH, load_json

    return load_json(CONTRACT_PATH)


def _fact_count() -> int:
    counted = psql(
        "dev",
        "of_admin",
        "SELECT count(*)::text FROM publish.daily_external_factors_ph;",
        tuples_only=True,
    )
    assert counted.returncode == 0, counted.stderr
    lines = [line.strip() for line in counted.stdout.splitlines() if line.strip()]
    return int(lines[0])
