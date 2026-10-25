"""Local operations report. It is not an HTTP route, and Grafana is not installed.

Last attempt, last successful run, and last publication are separate times.
A dry-run is an attempt, not a success and not a publication. No email or SMS
is sent. No freshness threshold and no request quota are set.
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.runs import MemoryRunLog, RunRecord
from openfactors.orchestration.backfill import DRY_RUN_STREAM
from openfactors.orchestration.daily import FailureAlert
from openfactors.quality.gates import GateResult

logger = logging.getLogger("openfactors.orchestration")

_ROUTES = {
    "/": "catalog_page",
    "/docs": "guide",
    "/datasets/daily-external-factors-ph": "dataset_page",
    "/openapi.json": "openapi",
    "/api/v1/datasets": "catalog",
    "/api/v1/datasets/daily-external-factors-ph": "dictionary",
    "/api/v1/factors/daily": "factors",
    "/api/v1/exports/daily-external-factors-ph": "export",
}


@dataclass(frozen=True)
class ApiObservation:
    route: str
    status_class: str
    rate_limited: bool


@dataclass(frozen=True)
class QualityObservation:
    rule: str | None
    passed: bool


def note_api(observations: list[ApiObservation], target: str, status: int) -> None:
    """Count one response by route and status class. The trace id is not a label."""
    path = target.split("?", 1)[0]
    observations.append(
        ApiObservation(
            route=_ROUTES.get(path, "other"),
            status_class=_status_class(status),
            rate_limited=status == 429,
        )
    )


def note_gate(observations: list[QualityObservation], result: GateResult) -> None:
    """Record a gate outcome. A failure does not change the publication time."""
    if result.status == "pass":
        observations.append(QualityObservation(rule=None, passed=True))
        return
    if not result.violations:
        observations.append(QualityObservation(rule=None, passed=False))
        return
    for rule in result.violations:
        observations.append(QualityObservation(rule=rule, passed=False))


def build_status(
    runs: MemoryRunLog,
    alerts: Iterable[FailureAlert],
    quality: Iterable[QualityObservation],
    api: Iterable[ApiObservation],
    *,
    publication_at: datetime | None,
    now: datetime,
) -> dict:
    """Build the report. Missing times stay missing."""
    _require_aware(now, "A status report needs a timezone-aware clock.")
    if publication_at is not None:
        _require_aware(publication_at, "A publication time needs a timezone.")
    recorded = list(runs.runs.values())
    alert_rows = list(alerts)
    quality_rows = list(quality)
    api_rows = list(api)
    attempt = _latest(recorded, lambda run: _aware(run.started_at))
    success = _latest(
        recorded,
        lambda run: (
            _aware(run.finished_at)
            if run.status == "succeeded" and run.stream != DRY_RUN_STREAM
            else None
        ),
    )
    report = {
        "freshness": {
            "last_attempt_at": _stamp(attempt),
            "last_success_at": _stamp(success),
            "last_publication_at": _stamp(publication_at),
            "publication_age_seconds": _age(publication_at, now),
        },
        "pipeline": {
            "attempt_count": len(recorded),
            "success_count": sum(
                1 for run in recorded if run.status == "succeeded" and run.stream != DRY_RUN_STREAM
            ),
            "failure_count": sum(1 for run in recorded if run.status == "failed"),
            "runs": [_run_row(run) for run in _ordered(recorded)],
        },
        "quality": _quality(quality_rows),
        "api": _api(api_rows),
        "alerts": [_alert_row(alert) for alert in alert_rows],
    }
    logger.info(
        "operations status last_attempt=%s last_success=%s last_publication=%s",
        _label(attempt),
        _label(success),
        _label(publication_at),
    )
    return report


def render_status(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def main(argv: list[str] | None = None, out=None) -> int:
    """Print an empty report. This process has no pipeline history of its own."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        raise SystemExit("The status command does not take arguments.")
    report = build_status(
        MemoryRunLog(),
        [],
        [],
        [],
        publication_at=None,
        now=datetime.now(UTC),
    )
    print(render_status(report), file=out or sys.stdout)
    return 0


def _run_row(run: RunRecord) -> dict:
    return {
        "run_id": run.run_id,
        "source_id": run.source_id,
        "stream": run.stream,
        "state": run.status,
        "rows_received": run.rows_received,
        "rows_accepted": run.rows_accepted,
        "duration_ms": _duration(run),
        "error_category": run.error_category,
    }


def _alert_row(alert: FailureAlert) -> dict:
    return {
        "run_id": alert.run_id,
        "step": alert.step,
        "category": alert.category,
        "manila_date": alert.manila_date.isoformat(),
        "at": _stamp(alert.at),
    }


def _quality(rows: list[QualityObservation]) -> dict:
    failures: Counter[str] = Counter()
    for row in rows:
        if not row.passed and row.rule:
            failures[row.rule] += 1
    latest = None
    if rows:
        last = rows[-1]
        latest = {"rule": last.rule, "passed": last.passed}
    return {"failures": dict(sorted(failures.items())), "latest": latest}


def _api(rows: list[ApiObservation]) -> dict:
    counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        counts[(row.route, row.status_class)] += 1
    requests = [
        {"route": route, "status_class": status_class, "count": count}
        for (route, status_class), count in sorted(counts.items())
    ]
    return {"requests": requests, "rate_limited": sum(1 for row in rows if row.rate_limited)}


def _ordered(runs: list[RunRecord]) -> list[RunRecord]:
    epoch = datetime.min.replace(tzinfo=UTC)
    return sorted(runs, key=lambda run: (_aware(run.started_at) or epoch, run.run_id))


def _latest(runs: list[RunRecord], choose) -> datetime | None:
    stamps = [stamp for run in runs if (stamp := choose(run)) is not None]
    if not stamps:
        return None
    return max(stamps)


def _duration(run: RunRecord) -> int | None:
    started = _aware(run.started_at)
    finished = _aware(run.finished_at)
    if started is None or finished is None or finished < started:
        return None
    return int((finished - started).total_seconds() * 1000)


def _age(publication_at: datetime | None, now: datetime) -> int | None:
    if publication_at is None or publication_at > now:
        return None
    return int((now - publication_at).total_seconds())


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return None
    return value


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _label(value: datetime | None) -> str:
    if value is None:
        return "missing"
    return value.isoformat()


def _require_aware(value: datetime, message: str) -> None:
    if value.tzinfo is None:
        raise ConnectorError(ErrorCategory.CONFIG, message)


def _status_class(status: int) -> str:
    if 200 <= status <= 299:
        return "2xx"
    if 400 <= status <= 499:
        return "4xx"
    if 500 <= status <= 599:
        return "5xx"
    return "other"


if __name__ == "__main__":
    raise SystemExit(main())
