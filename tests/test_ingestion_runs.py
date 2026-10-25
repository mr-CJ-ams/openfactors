"""Restart and replay of an ingestion run."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from openfactors.ingestion.checkpoints import CheckpointKey, MemoryCheckpoints
from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.raw_store import RawArtifact, RetentionLabel
from openfactors.ingestion.runs import (
    MemoryRunLog,
    RunBatch,
    RunRecord,
    apply_transition,
    execute_run,
)

NOW = datetime(2026, 10, 24, 3, 0, tzinfo=UTC)
START = date(2018, 1, 1)
KEEP = RetentionLabel("until_source_withdrawn", None)
RUN_ID = "00000000-0000-4000-8000-000000000015"


class MemoryObjects:
    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}

    def put_if_absent(self, key: str, body: bytes) -> None:
        current = self.bodies.get(key)
        if current is None:
            self.bodies[key] = body
            return
        if current != body:
            raise ConnectorError(
                ErrorCategory.CHECKSUM, "Object key already holds different bytes."
            )

    def get(self, key: str) -> bytes:
        return self.bodies[key]


class MemoryManifests:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], RawArtifact] = {}

    def find(self, run_id: str, request_fingerprint: str) -> RawArtifact | None:
        return self.rows.get((run_id, request_fingerprint))

    def insert(self, artifact: RawArtifact) -> RawArtifact:
        self.rows[(artifact.run_id, artifact.request_fingerprint)] = artifact
        return artifact


def _batch(end: date = date(2018, 1, 3)) -> RunBatch:
    return RunBatch(
        source_id="open-meteo-era5",
        location_id="ph-manila",
        stream="daily",
        interval_start=START,
        interval_end=end,
        code_revision="of-015",
    )


def _payload(day: date) -> bytes:
    return f'{{"date":"{day.isoformat()}"}}'.encode()


def _statuses(log: MemoryRunLog, run_id: str) -> list[str]:
    return [
        event.status for event in log.events if event.run_id == run_id and event.kind == "status"
    ]


def test_crash_then_resume_does_not_store_the_finished_day_again() -> None:
    runs = MemoryRunLog()
    checkpoints = MemoryCheckpoints()
    objects = MemoryObjects()
    manifests = MemoryManifests()
    seen: list[date] = []

    def fetch(day: date) -> bytes:
        seen.append(day)
        if len(seen) == 2:
            raise RuntimeError("injected crash")
        return _payload(day)

    with pytest.raises(RuntimeError):
        execute_run(
            runs,
            checkpoints,
            objects,
            manifests,
            _batch(),
            fetch,
            now=NOW,
            retention=KEEP,
            run_id_factory=lambda: RUN_ID,
        )
    key = CheckpointKey("open-meteo-era5", "ph-manila", "daily")
    assert checkpoints.get(key) == START
    assert len(manifests.rows) == 1
    assert runs.get(RUN_ID).status == "running"

    def resume_fetch(day: date) -> bytes:
        seen.append(day)
        return _payload(day)

    finished = execute_run(
        runs,
        checkpoints,
        objects,
        manifests,
        _batch(),
        resume_fetch,
        now=NOW,
        retention=KEEP,
        resume_run_id=RUN_ID,
    )
    assert finished.status == "succeeded"
    assert finished.rows_accepted == 3
    assert checkpoints.get(key) == date(2018, 1, 3)
    assert len(objects.bodies) == 3
    assert len(manifests.rows) == 3
    assert seen == [
        START,
        START + timedelta(days=1),
        START + timedelta(days=1),
        START + timedelta(days=2),
    ]
    assert _statuses(runs, RUN_ID) == ["pending", "running", "succeeded"]


def test_crash_after_the_manifest_reuses_the_artifact() -> None:
    runs = MemoryRunLog()
    objects = MemoryObjects()
    manifests = MemoryManifests()

    class StopOnce(MemoryCheckpoints):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        def commit(self, key: CheckpointKey, durable_date: date) -> date:
            if self.fail:
                self.fail = False
                raise RuntimeError("injected crash")
            return super().commit(key, durable_date)

    flaky = StopOnce()
    with pytest.raises(RuntimeError):
        execute_run(
            runs,
            flaky,
            objects,
            manifests,
            _batch(START),
            _payload,
            now=NOW,
            retention=KEEP,
            run_id_factory=lambda: RUN_ID,
        )
    assert flaky.get(CheckpointKey("open-meteo-era5", "ph-manila", "daily")) is None
    assert len(manifests.rows) == 1
    finished = execute_run(
        runs,
        flaky,
        objects,
        manifests,
        _batch(START),
        _payload,
        now=NOW,
        retention=KEEP,
        resume_run_id=RUN_ID,
    )
    assert finished.status == "succeeded"
    assert finished.rows_accepted == 1
    assert len(objects.bodies) == 1
    assert len(manifests.rows) == 1


def test_replay_of_a_finished_interval_stores_nothing() -> None:
    runs = MemoryRunLog()
    checkpoints = MemoryCheckpoints()
    objects = MemoryObjects()
    manifests = MemoryManifests()
    calls: list[date] = []

    def fetch(day: date) -> bytes:
        calls.append(day)
        return _payload(day)

    first = execute_run(
        runs,
        checkpoints,
        objects,
        manifests,
        _batch(START),
        fetch,
        now=NOW,
        retention=KEEP,
        run_id_factory=lambda: RUN_ID,
    )
    replay = execute_run(
        runs,
        checkpoints,
        objects,
        manifests,
        _batch(START),
        fetch,
        now=NOW,
        retention=KEEP,
        run_id_factory=lambda: "00000000-0000-4000-8000-000000000016",
    )
    assert first.status == "succeeded"
    assert replay.status == "succeeded"
    assert replay.rows_accepted == 0
    assert len(manifests.rows) == 1
    assert calls == [START]
    advanced = [
        event
        for event in runs.events
        if event.run_id == replay.run_id and event.kind == "checkpoint_advanced"
    ]
    assert advanced == []


def test_fetch_failure_does_not_move_the_checkpoint() -> None:
    runs = MemoryRunLog()
    checkpoints = MemoryCheckpoints()

    def fetch(day: date) -> bytes:
        raise ConnectorError(ErrorCategory.TIMEOUT, "injected timeout")

    finished = execute_run(
        runs,
        checkpoints,
        MemoryObjects(),
        MemoryManifests(),
        _batch(START),
        fetch,
        now=NOW,
        retention=KEEP,
        run_id_factory=lambda: RUN_ID,
    )
    assert finished.status == "failed"
    assert finished.error_category == "timeout"
    assert finished.rows_accepted == 0
    assert checkpoints.get(CheckpointKey("open-meteo-era5", "ph-manila", "daily")) is None
    assert _statuses(runs, RUN_ID) == ["pending", "running", "failed"]


def test_pending_cannot_succeed_and_a_finished_run_cannot_resume() -> None:
    pending = RunRecord(
        run_id=RUN_ID,
        source_id="open-meteo-era5",
        stream="daily",
        location_id="ph-manila",
        interval_start=START,
        interval_end=START,
        code_revision="of-015",
        status="pending",
    )
    with pytest.raises(ConnectorError) as caught:
        apply_transition(pending, "succeeded", now=NOW)
    assert caught.value.category is ErrorCategory.CONFIG
    succeeded = apply_transition(
        apply_transition(pending, "running", now=NOW), "succeeded", now=NOW
    )
    with pytest.raises(ConnectorError):
        apply_transition(succeeded, "running", now=NOW)


def test_a_running_run_blocks_a_second_start_until_it_is_cancelled() -> None:
    runs = MemoryRunLog()
    checkpoints = MemoryCheckpoints()
    objects = MemoryObjects()
    manifests = MemoryManifests()

    def fetch(day: date) -> bytes:
        raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError):
        execute_run(
            runs,
            checkpoints,
            objects,
            manifests,
            _batch(START),
            fetch,
            now=NOW,
            retention=KEEP,
            run_id_factory=lambda: RUN_ID,
        )
    with pytest.raises(ConnectorError) as caught:
        execute_run(
            runs,
            checkpoints,
            objects,
            manifests,
            _batch(START),
            _payload,
            now=NOW,
            retention=KEEP,
        )
    assert caught.value.category is ErrorCategory.CONFIG
    runs.transition(RUN_ID, "cancelled", now=NOW)
    finished = execute_run(
        runs,
        checkpoints,
        objects,
        manifests,
        _batch(START),
        _payload,
        now=NOW,
        retention=KEEP,
        run_id_factory=lambda: "00000000-0000-4000-8000-000000000016",
    )
    assert finished.status == "succeeded"
    assert len(manifests.rows) == 1
