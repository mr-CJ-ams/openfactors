"""One local daily attempt. Airflow is not installed, and this is not an HTTP route.

The attempt runs weather, holidays, transform, validation, and publish, in that
order. A failed step is not followed by publish. Transient failures retry up
to the connector attempt limit. A failed attempt leaves a log line and a failed
run record. It does not send email or SMS.
"""

from __future__ import annotations

import logging
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from contracts.calendar import manila_today

from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.http_client import Clock, RetryPolicy
from openfactors.ingestion.runs import MemoryRunLog, RunRecord

logger = logging.getLogger("openfactors.orchestration")

STEP_ORDER = (
    "weather_fetch",
    "holiday_fetch",
    "transform",
    "validate",
    "publish",
)
SOURCE_ID = "daily-external-factors-ph"
STREAM = "daily"
LOCATION_ID = "all"
RETRYABLE = frozenset(
    {
        ErrorCategory.TIMEOUT,
        ErrorCategory.RATE_LIMITED,
        ErrorCategory.UPSTREAM_HTTP,
        ErrorCategory.NETWORK,
    }
)
PIPELINE_LOCK = threading.Lock()
Step = Callable[[date], None]


@dataclass(frozen=True)
class FailureAlert:
    """Local failure record. It is not an email, a page, or an HTTP response."""

    run_id: str
    manila_date: date
    step: str
    category: str
    at: datetime


def run_daily(
    steps: Mapping[str, Step],
    *,
    runs: MemoryRunLog,
    alerts: list[FailureAlert],
    now: datetime,
    policy: RetryPolicy | None = None,
    clock: Clock | None = None,
    code_revision: str = "local",
) -> RunRecord:
    """Run one Asia/Manila date. A second success for that date does not repeat it."""
    if not PIPELINE_LOCK.acquire(blocking=False):
        raise ConnectorError(ErrorCategory.CONFIG, "A pipeline run is already in progress.")
    try:
        return _run_locked(
            steps,
            runs=runs,
            alerts=alerts,
            now=now,
            policy=policy or RetryPolicy(),
            clock=clock or Clock(),
            code_revision=code_revision,
        )
    finally:
        PIPELINE_LOCK.release()


def main(argv: list[str] | None = None) -> int:
    """Run one attempt. Date intervals belong to the backfill command, not here."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        raise SystemExit("The daily command does not take arguments.")
    alerts: list[FailureAlert] = []
    record = run_daily(
        _closed_steps(),
        runs=MemoryRunLog(),
        alerts=alerts,
        now=datetime.now(UTC),
    )
    return 0 if record.status == "succeeded" else 1


def _run_locked(
    steps: Mapping[str, Step],
    *,
    runs: MemoryRunLog,
    alerts: list[FailureAlert],
    now: datetime,
    policy: RetryPolicy,
    clock: Clock,
    code_revision: str,
) -> RunRecord:
    if set(steps) != set(STEP_ORDER):
        raise ConnectorError(
            ErrorCategory.CONFIG, "The daily run steps do not match the pipeline."
        )
    if not code_revision.strip():
        raise ConnectorError(ErrorCategory.CONFIG, "Code revision is empty.")
    if policy.max_attempts < 1:
        raise ConnectorError(ErrorCategory.CONFIG, "max_attempts must be at least 1.")
    try:
        day = manila_today(now)
    except ValueError:
        raise ConnectorError(
            ErrorCategory.CONFIG, "A daily run needs a timezone-aware clock."
        ) from None
    finished = _succeeded(runs, day)
    if finished is not None:
        return finished
    if _running(runs, day) is not None:
        raise ConnectorError(ErrorCategory.CONFIG, "A daily run is already in progress.")
    run = RunRecord(
        run_id=str(uuid.uuid4()),
        source_id=SOURCE_ID,
        stream=STREAM,
        location_id=LOCATION_ID,
        interval_start=day,
        interval_end=day,
        code_revision=code_revision,
        status="pending",
    )
    runs.create(run, now=now)
    runs.transition(run.run_id, "running", now=now)
    for name in STEP_ORDER:
        try:
            attempt_step(steps[name], day, policy, clock)
        except ConnectorError as exc:
            return _fail(runs, alerts, run.run_id, day, name, exc.category, now)
        except Exception:
            return _fail(runs, alerts, run.run_id, day, name, ErrorCategory.CONFIG, now)
    return runs.transition(run.run_id, "succeeded", now=now)


def attempt_step(step: Callable, argument: object, policy: RetryPolicy, clock: Clock) -> None:
    """Retry one transient failure up to the connector attempt limit."""
    for attempt in range(1, policy.max_attempts + 1):
        try:
            step(argument)
            return
        except ConnectorError as exc:
            if exc.category not in RETRYABLE or attempt >= policy.max_attempts:
                raise
            delay = policy.base_delay_seconds * (2 ** (attempt - 1))
            clock.sleep(min(delay, policy.max_delay_seconds))


def _fail(
    runs: MemoryRunLog,
    alerts: list[FailureAlert],
    run_id: str,
    day: date,
    step: str,
    category: ErrorCategory,
    now: datetime,
) -> RunRecord:
    failed = runs.transition(run_id, "failed", now=now, error_category=category)
    alerts.append(
        FailureAlert(
            run_id=run_id,
            manila_date=day,
            step=step,
            category=category.value,
            at=now,
        )
    )
    logger.error(
        "daily run failed run_id=%s step=%s category=%s",
        run_id,
        step,
        category.value,
    )
    return failed


def _succeeded(runs: MemoryRunLog, day: date) -> RunRecord | None:
    matches = [
        run
        for run in runs.runs.values()
        if run.stream == STREAM and run.interval_start == day and run.status == "succeeded"
    ]
    if len(matches) > 1:
        raise ConnectorError(
            ErrorCategory.CONFIG, "More than one daily run succeeded for this date."
        )
    return matches[0] if matches else None


def _running(runs: MemoryRunLog, day: date) -> RunRecord | None:
    matches = [
        run
        for run in runs.runs.values()
        if run.stream == STREAM and run.interval_start == day and run.status == "running"
    ]
    if len(matches) > 1:
        raise ConnectorError(
            ErrorCategory.CONFIG, "More than one daily run is active for this date."
        )
    return matches[0] if matches else None


def _closed_steps() -> dict[str, Step]:
    """Fail closed. A live pull and a publish are not implied by starting the command."""

    def refuse(_day: date) -> None:
        raise ConnectorError(ErrorCategory.CONFIG, "Live daily sources are not enabled.")

    return {name: refuse for name in STEP_ORDER}


if __name__ == "__main__":
    raise SystemExit(main())
