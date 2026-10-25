"""Ingestion run state and the durable checkpoint.

A checkpoint moves only after the raw object and its manifest are both
committed. A crash leaves the run running so the same run can resume.
Days already at or behind the checkpoint are not stored again.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from openfactors.ingestion.checkpoints import CheckpointKey, MemoryCheckpoints, job_identity
from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.raw_store import (
    ManifestLog,
    ObjectStore,
    RetentionLabel,
    commit_raw_artifact,
)

_NEXT = {
    "pending": frozenset({"running"}),
    "running": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    source_id: str
    stream: str
    location_id: str
    interval_start: date
    interval_end: date
    code_revision: str
    status: str
    rows_received: int = 0
    rows_accepted: int = 0
    error_category: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class AuditEvent:
    run_id: str
    kind: str
    at: datetime
    status: str | None = None
    durable_date: date | None = None
    error_category: str | None = None


@dataclass(frozen=True)
class RunBatch:
    source_id: str
    location_id: str
    stream: str
    interval_start: date
    interval_end: date
    code_revision: str
    content_type: str = "application/json"


class MemoryRunLog:
    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}
        self.events: list[AuditEvent] = []

    def create(self, run: RunRecord, *, now: datetime) -> RunRecord:
        if run.status != "pending":
            raise ConnectorError(ErrorCategory.CONFIG, "A new run starts pending.")
        if run.run_id in self.runs:
            raise ConnectorError(ErrorCategory.CONFIG, "Run id already exists.")
        self.runs[run.run_id] = run
        self.events.append(AuditEvent(run.run_id, "status", now, status="pending"))
        return run

    def get(self, run_id: str) -> RunRecord:
        try:
            return self.runs[run_id]
        except KeyError as exc:
            raise ConnectorError(ErrorCategory.CONFIG, "Run id was not found.") from exc

    def transition(
        self,
        run_id: str,
        status: str,
        *,
        now: datetime,
        error_category: ErrorCategory | None = None,
    ) -> RunRecord:
        updated = apply_transition(self.get(run_id), status, now=now, error_category=error_category)
        self.runs[run_id] = updated
        self.events.append(
            AuditEvent(
                run_id,
                "status",
                now,
                status=status,
                error_category=updated.error_category,
            )
        )
        return updated

    def record_progress(self, run_id: str, *, rows_received: int, rows_accepted: int) -> RunRecord:
        if rows_accepted > rows_received or rows_received < 0:
            raise ConnectorError(ErrorCategory.CONFIG, "Run row counts are inconsistent.")
        updated = replace(
            self.get(run_id),
            rows_received=rows_received,
            rows_accepted=rows_accepted,
        )
        self.runs[run_id] = updated
        return updated

    def checkpoint_advanced(self, run_id: str, durable_date: date, *, now: datetime) -> None:
        self.events.append(
            AuditEvent(run_id, "checkpoint_advanced", now, durable_date=durable_date)
        )

    def active_run_id(self, source_id: str, location_id: str, stream: str) -> str | None:
        matches = [
            run.run_id
            for run in self.runs.values()
            if run.status == "running"
            and run.source_id == source_id
            and run.location_id == location_id
            and run.stream == stream
        ]
        if len(matches) > 1:
            raise ConnectorError(
                ErrorCategory.CONFIG, "More than one run is active for this stream."
            )
        if not matches:
            return None
        return matches[0]


def apply_transition(
    run: RunRecord,
    status: str,
    *,
    now: datetime,
    error_category: ErrorCategory | None = None,
) -> RunRecord:
    allowed = _NEXT.get(run.status)
    if allowed is None or status not in allowed:
        raise ConnectorError(
            ErrorCategory.CONFIG,
            f"Run cannot move from {run.status} to {status}.",
        )
    if now.tzinfo is None:
        raise ConnectorError(ErrorCategory.CONFIG, "Run timestamp must include a timezone.")
    if status == "failed":
        if error_category is None:
            raise ConnectorError(ErrorCategory.CONFIG, "A failed run needs an error category.")
        category: str | None = error_category.value
    elif error_category is not None:
        raise ConnectorError(ErrorCategory.CONFIG, "Only a failed run records an error category.")
    else:
        category = None
    started_at = now if status == "running" else run.started_at
    finished_at = now if status in _TERMINAL else None
    return replace(
        run,
        status=status,
        error_category=category,
        started_at=started_at,
        finished_at=finished_at,
    )


def execute_run(
    runs: MemoryRunLog,
    checkpoints: MemoryCheckpoints,
    objects: ObjectStore,
    manifests: ManifestLog,
    batch: RunBatch,
    fetch_day: Callable[[date], bytes],
    *,
    now: datetime,
    retention: RetentionLabel,
    resume_run_id: str | None = None,
    run_id_factory: Callable[[], str] | None = None,
) -> RunRecord:
    if batch.interval_start > batch.interval_end:
        raise ConnectorError(ErrorCategory.CONFIG, "Interval start is after interval end.")
    if not batch.code_revision.strip():
        raise ConnectorError(ErrorCategory.CONFIG, "Code revision is empty.")
    key = CheckpointKey(batch.source_id, batch.location_id, batch.stream)
    run = _open_run(
        runs, batch, now=now, resume_run_id=resume_run_id, run_id_factory=run_id_factory
    )
    received = run.rows_received
    accepted = run.rows_accepted
    for day in _days(batch.interval_start, batch.interval_end):
        watermark = checkpoints.get(key)
        if watermark is not None and day <= watermark:
            continue
        fingerprint = job_identity(
            batch.source_id,
            batch.location_id,
            batch.stream,
            day,
            day,
        )
        try:
            body = fetch_day(day)
            commit_raw_artifact(
                objects,
                manifests,
                run_id=run.run_id,
                source_id=batch.source_id,
                body=body,
                content_type=batch.content_type,
                request_fingerprint=fingerprint,
                retrieved_at=now,
                source_updated_at=None,
                response_status=200,
                retention=retention,
            )
        except ConnectorError as exc:
            runs.record_progress(run.run_id, rows_received=received + 1, rows_accepted=accepted)
            return runs.transition(run.run_id, "failed", now=now, error_category=exc.category)
        checkpoints.commit(key, day)
        received += 1
        accepted += 1
        runs.record_progress(run.run_id, rows_received=received, rows_accepted=accepted)
        runs.checkpoint_advanced(run.run_id, day, now=now)
    return runs.transition(run.run_id, "succeeded", now=now)


def recover_running_run(
    runs: MemoryRunLog,
    checkpoints: MemoryCheckpoints,
    objects: ObjectStore,
    manifests: ManifestLog,
    batch: RunBatch,
    fetch_day: Callable[[date], bytes],
    *,
    now: datetime,
    retention: RetentionLabel,
    resume_run_id: str,
    run_id_factory: Callable[[], str] | None = None,
) -> RunRecord:
    """Resume a run that is still running. A failed run stays failed."""
    run = runs.get(resume_run_id)
    if run.status == "failed":
        raise ConnectorError(
            ErrorCategory.CONFIG, "A failed run stays failed. Start a new attempt."
        )
    return execute_run(
        runs,
        checkpoints,
        objects,
        manifests,
        batch,
        fetch_day,
        now=now,
        retention=retention,
        resume_run_id=resume_run_id,
        run_id_factory=run_id_factory,
    )


def _open_run(
    runs: MemoryRunLog,
    batch: RunBatch,
    *,
    now: datetime,
    resume_run_id: str | None,
    run_id_factory: Callable[[], str] | None,
) -> RunRecord:
    if resume_run_id is not None:
        run = runs.get(resume_run_id)
        _require_same_batch(run, batch)
        if run.status != "running":
            raise ConnectorError(ErrorCategory.CONFIG, "Only a running run can resume.")
        return run
    active = runs.active_run_id(batch.source_id, batch.location_id, batch.stream)
    if active is not None:
        raise ConnectorError(ErrorCategory.CONFIG, "An ingestion run is already running.")
    created = runs.create(
        RunRecord(
            run_id=(run_id_factory or _new_id)(),
            source_id=batch.source_id,
            stream=batch.stream,
            location_id=batch.location_id,
            interval_start=batch.interval_start,
            interval_end=batch.interval_end,
            code_revision=batch.code_revision,
            status="pending",
        ),
        now=now,
    )
    return runs.transition(created.run_id, "running", now=now)


def _require_same_batch(run: RunRecord, batch: RunBatch) -> None:
    same = (
        run.source_id == batch.source_id
        and run.location_id == batch.location_id
        and run.stream == batch.stream
        and run.interval_start == batch.interval_start
        and run.interval_end == batch.interval_end
    )
    if not same:
        raise ConnectorError(ErrorCategory.CONFIG, "Resume batch does not match the run.")


def _days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _new_id() -> str:
    return str(uuid.uuid4())
