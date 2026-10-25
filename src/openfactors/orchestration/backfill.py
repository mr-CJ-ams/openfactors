"""Authorized historical backfill. This command is local, and it is not an HTTP route.

A request names a source, an approved location, an inclusive Asia/Manila interval,
the operator, and the reason. A dry-run records that plan and does not publish.
An actual run uses the daily step order. The same interval and code revision
does not publish a second time. This command does not rewrite or delete a
published release.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from contracts.calendar import manila_today

from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.http_client import Clock, RetryPolicy
from openfactors.ingestion.runs import MemoryRunLog, RunRecord
from openfactors.orchestration.daily import (
    PIPELINE_LOCK,
    STEP_ORDER,
    FailureAlert,
    attempt_step,
)

logger = logging.getLogger("openfactors.orchestration")

STREAM = "backfill"
DRY_RUN_STREAM = "backfill-dry-run"
_ROOT = Path(__file__).resolve().parents[3]
_LOCATIONS_PATH = _ROOT / "contracts" / "fixtures" / "pilot_locations.json"
_SOURCES_PATH = _ROOT / "contracts" / "fixtures" / "source_registry.json"
_ANONYMOUS = "anonymous"
Step = Callable[["BackfillScope"], None]


@dataclass(frozen=True)
class BackfillRequest:
    source_id: str
    location_id: str
    start_date: date
    end_date: date
    operator: str
    reason: str
    dry_run: bool = False
    code_revision: str = "local"


@dataclass(frozen=True)
class BackfillScope:
    source_id: str
    location_id: str
    start_date: date
    end_date: date
    operator: str
    reason: str
    code_revision: str


@dataclass(frozen=True)
class BackfillAudit:
    """Who asked for which interval. It is not an email or an HTTP response."""

    run_id: str
    operator: str
    reason: str
    source_id: str
    location_id: str
    start_date: date
    end_date: date
    dry_run: bool
    status: str
    at: datetime


def run_backfill(
    request: BackfillRequest,
    steps: Mapping[str, Step],
    *,
    runs: MemoryRunLog,
    audits: list[BackfillAudit],
    alerts: list[FailureAlert],
    now: datetime,
    policy: RetryPolicy | None = None,
    clock: Clock | None = None,
    locations: dict | None = None,
    sources: dict | None = None,
) -> RunRecord:
    """Validate one interval, then dry-run it or run it once."""
    _validate(request, now, locations=locations, sources=sources)
    if not request.dry_run and set(steps) != set(STEP_ORDER):
        raise ConnectorError(ErrorCategory.CONFIG, "The backfill steps do not match the pipeline.")
    if not PIPELINE_LOCK.acquire(blocking=False):
        raise ConnectorError(ErrorCategory.CONFIG, "A pipeline run is already in progress.")
    try:
        return _run_locked(
            request,
            steps,
            runs=runs,
            audits=audits,
            alerts=alerts,
            now=now,
            policy=policy or RetryPolicy(),
            clock=clock or Clock(),
        )
    finally:
        PIPELINE_LOCK.release()


def main(argv: list[str] | None = None) -> int:
    """Parse one maintainer request. A missing operator is not a run."""
    try:
        request = parse_backfill_args(list(sys.argv[1:] if argv is None else argv))
        record = run_backfill(
            request,
            _closed_steps(),
            runs=MemoryRunLog(),
            audits=[],
            alerts=[],
            now=datetime.now(UTC),
        )
    except ConnectorError as exc:
        raise SystemExit(str(exc)) from None
    if request.dry_run:
        return 0
    return 0 if record.status == "succeeded" else 1


def parse_backfill_args(argv: list[str]) -> BackfillRequest:
    parser = argparse.ArgumentParser(prog="python -m openfactors.orchestration.backfill")
    parser.add_argument("--source", required=True)
    parser.add_argument("--location-id", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--code-revision", default="local")
    parsed = parser.parse_args(argv)
    return BackfillRequest(
        source_id=parsed.source,
        location_id=parsed.location_id,
        start_date=_iso(parsed.start_date, "start_date"),
        end_date=_iso(parsed.end_date, "end_date"),
        operator=parsed.operator,
        reason=parsed.reason,
        dry_run=parsed.dry_run,
        code_revision=parsed.code_revision,
    )


def _run_locked(
    request: BackfillRequest,
    steps: Mapping[str, Step],
    *,
    runs: MemoryRunLog,
    audits: list[BackfillAudit],
    alerts: list[FailureAlert],
    now: datetime,
    policy: RetryPolicy,
    clock: Clock,
) -> RunRecord:
    if policy.max_attempts < 1:
        raise ConnectorError(ErrorCategory.CONFIG, "max_attempts must be at least 1.")
    if _running(runs):
        raise ConnectorError(ErrorCategory.CONFIG, "A backfill is already in progress.")
    if not request.dry_run:
        finished = _succeeded(runs, request)
        if finished is not None:
            _audit(audits, finished.run_id, request, "succeeded", now)
            return finished
    run = RunRecord(
        run_id=str(uuid.uuid4()),
        source_id=request.source_id,
        stream=DRY_RUN_STREAM if request.dry_run else STREAM,
        location_id=request.location_id,
        interval_start=request.start_date,
        interval_end=request.end_date,
        code_revision=request.code_revision,
        status="pending",
    )
    runs.create(run, now=now)
    runs.transition(run.run_id, "running", now=now)
    if request.dry_run:
        finished = runs.transition(run.run_id, "succeeded", now=now)
        _audit(audits, finished.run_id, request, "succeeded", now)
        logger.info(
            "backfill dry-run run_id=%s operator=%s source=%s location=%s "
            "start=%s end=%s reason=%s",
            finished.run_id,
            request.operator,
            request.source_id,
            request.location_id,
            request.start_date.isoformat(),
            request.end_date.isoformat(),
            request.reason,
        )
        return finished
    scope = BackfillScope(
        source_id=request.source_id,
        location_id=request.location_id,
        start_date=request.start_date,
        end_date=request.end_date,
        operator=request.operator,
        reason=request.reason,
        code_revision=request.code_revision,
    )
    for name in STEP_ORDER:
        try:
            attempt_step(steps[name], scope, policy, clock)
        except ConnectorError as exc:
            return _fail(runs, audits, alerts, request, run.run_id, name, exc.category, now)
        except Exception:
            return _fail(runs, audits, alerts, request, run.run_id, name, ErrorCategory.CONFIG, now)
    finished = runs.transition(run.run_id, "succeeded", now=now)
    _audit(audits, finished.run_id, request, "succeeded", now)
    logger.info(
        "backfill finished run_id=%s operator=%s source=%s location=%s start=%s end=%s",
        finished.run_id,
        request.operator,
        request.source_id,
        request.location_id,
        request.start_date.isoformat(),
        request.end_date.isoformat(),
    )
    return finished


def _validate(
    request: BackfillRequest,
    now: datetime,
    *,
    locations: dict | None,
    sources: dict | None,
) -> None:
    if request.operator.strip().casefold() == _ANONYMOUS or not request.operator.strip():
        raise ConnectorError(ErrorCategory.CONFIG, "A backfill needs a named operator.")
    if not request.reason.strip():
        raise ConnectorError(ErrorCategory.CONFIG, "A backfill needs a reason.")
    if not request.code_revision.strip():
        raise ConnectorError(ErrorCategory.CONFIG, "Code revision is empty.")
    try:
        today = manila_today(now)
    except ValueError:
        raise ConnectorError(
            ErrorCategory.CONFIG, "A backfill needs a timezone-aware clock."
        ) from None
    known_sources = _source_ids(sources)
    if request.source_id not in known_sources:
        raise ConnectorError(ErrorCategory.CONFIG, "That source is not available.")
    known_locations, supported_start = _locations(locations)
    if request.location_id != "all" and request.location_id not in known_locations:
        raise ConnectorError(ErrorCategory.CONFIG, "That location is not available.")
    if request.start_date < supported_start:
        raise ConnectorError(
            ErrorCategory.CONFIG,
            f"start_date is before {supported_start.isoformat()}.",
        )
    if request.end_date > today:
        raise ConnectorError(
            ErrorCategory.CONFIG, "end_date is after the current Asia/Manila date."
        )
    if request.start_date > request.end_date:
        raise ConnectorError(ErrorCategory.CONFIG, "start_date must be on or before end_date")


def _fail(
    runs: MemoryRunLog,
    audits: list[BackfillAudit],
    alerts: list[FailureAlert],
    request: BackfillRequest,
    run_id: str,
    step: str,
    category: ErrorCategory,
    now: datetime,
) -> RunRecord:
    failed = runs.transition(run_id, "failed", now=now, error_category=category)
    alerts.append(
        FailureAlert(
            run_id=run_id,
            manila_date=request.end_date,
            step=step,
            category=category.value,
            at=now,
        )
    )
    _audit(audits, run_id, request, "failed", now)
    logger.error(
        "backfill failed run_id=%s step=%s category=%s",
        run_id,
        step,
        category.value,
    )
    return failed


def _audit(
    audits: list[BackfillAudit],
    run_id: str,
    request: BackfillRequest,
    status: str,
    now: datetime,
) -> None:
    audits.append(
        BackfillAudit(
            run_id=run_id,
            operator=request.operator,
            reason=request.reason,
            source_id=request.source_id,
            location_id=request.location_id,
            start_date=request.start_date,
            end_date=request.end_date,
            dry_run=request.dry_run,
            status=status,
            at=now,
        )
    )


def _succeeded(runs: MemoryRunLog, request: BackfillRequest) -> RunRecord | None:
    matches = [
        run
        for run in runs.runs.values()
        if run.stream == STREAM
        and run.status == "succeeded"
        and run.source_id == request.source_id
        and run.location_id == request.location_id
        and run.interval_start == request.start_date
        and run.interval_end == request.end_date
        and run.code_revision == request.code_revision
    ]
    if len(matches) > 1:
        raise ConnectorError(
            ErrorCategory.CONFIG, "More than one backfill succeeded for this interval."
        )
    return matches[0] if matches else None


def _running(runs: MemoryRunLog) -> bool:
    return any(run.stream == STREAM and run.status == "running" for run in runs.runs.values())


def _source_ids(document: dict | None) -> set[str]:
    payload = document if document is not None else _json(_SOURCES_PATH)
    return {item["source_id"] for item in payload["sources"]}


def _locations(document: dict | None) -> tuple[set[str], date]:
    payload = document if document is not None else _json(_LOCATIONS_PATH)
    chosen = {
        item["location_id"] for item in payload["locations"] if item["approved_for_mvp"] is True
    }
    return chosen, date.fromisoformat(payload["supported_start_date"])


def _iso(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ConnectorError(ErrorCategory.CONFIG, f"{name} must be an ISO date.") from None


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _closed_steps() -> dict[str, Step]:
    """Fail closed. Starting the command does not fetch or publish."""

    def refuse(_scope: BackfillScope) -> None:
        raise ConnectorError(ErrorCategory.CONFIG, "Live backfill sources are not enabled.")

    return {name: refuse for name in STEP_ORDER}


if __name__ == "__main__":
    raise SystemExit(main())
