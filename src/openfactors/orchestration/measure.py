"""Local load, coverage, and reliability report. It is not an HTTP route.

The timed query is one reader, one location, a 31-day window, and page size
100. That is the local shape. It does not claim the PRD p95 target, and it
does not set a date-span maximum or a request quota. Coverage uses the same
99 percent rule as publication. A prelaunch sample cannot prove the rolling
30-day reliability target.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from contracts.calendar import date_parts, manila_today

from openfactors.api.factors import handle_factors
from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.runs import RunRecord
from openfactors.orchestration.daily import STREAM as DAILY_STREAM
from openfactors.quality.coverage import (
    COVERAGE_DENOMINATOR,
    COVERAGE_NUMERATOR,
    evaluate_coverage,
)

logger = logging.getLogger("openfactors.orchestration")

_ROOT = Path(__file__).resolve().parents[3]
_LOCATIONS_PATH = _ROOT / "contracts" / "fixtures" / "pilot_locations.json"
READERS = 1
WINDOW_DAYS = 31
PAGE_SIZE = 100
LOCAL_SAMPLES = 20
LOCAL_WARMUP = 1
PRD_TARGET_SECONDS = 1
RELIABILITY_WINDOW_DAYS = 30
RELIABILITY_NUMERATOR = 99
RELIABILITY_DENOMINATOR = 100
OWNER = "Founder / Product Lead"
_SAMPLE_RELEASE = "00000000-0000-4000-8000-000000000042"
_DATASET_ID = "daily_external_factors_ph"


def build_measurement(
    durations_seconds: list[float],
    runs: Iterable[RunRecord],
    *,
    now: datetime,
    rows: list[dict],
    supported: set[tuple[str, str]],
    exclusions: list[dict],
    verified_dates: dict[str, dict],
    warmup_count: int,
    location_id: str,
    rows_returned: int,
) -> dict:
    """Build the report. The PRD latency and reliability targets stay unclaimed."""
    _require_aware(now, "A measurement report needs a timezone-aware clock.")
    if warmup_count < 0:
        raise ConnectorError(ErrorCategory.CONFIG, "Warmup count cannot be negative.")
    if location_id not in _location_ids():
        raise ConnectorError(ErrorCategory.CONFIG, "That location is not available.")
    if rows_returned < 0 or rows_returned > PAGE_SIZE:
        raise ConnectorError(
            ErrorCategory.CONFIG,
            "The timed page is larger than the local page size.",
        )
    coverage = evaluate_coverage(
        rows,
        supported=supported,
        exclusions=exclusions,
        verified_dates=verified_dates,
    )
    report = {
        "latency": _latency(durations_seconds, warmup_count, location_id, rows_returned),
        "coverage": _coverage(coverage),
        "reliability": _reliability(list(runs), now),
    }
    logger.info(
        "measurement environment=local prd_target_claimed=false coverage_met=%s "
        "reliability_claimed=false",
        str(report["coverage"]["meets_threshold"]).lower(),
    )
    return report


def sample_durations(now: datetime) -> tuple[list[float], int]:
    """Time the cached factors page. The first call is warmup and is discarded."""
    _require_aware(now, "A measurement report needs a timezone-aware clock.")
    end = manila_today(now)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    location_id = _location_ids()[0]
    page = _shape_page(location_id, start, end)
    target = _target(location_id, start, end)
    for _ignored in range(LOCAL_WARMUP):
        _once(target, page)
    return [_once(target, page) for _ignored in range(LOCAL_SAMPLES)], len(page["rows"])


def render_measurement(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def main(argv: list[str] | None = None, out=None) -> int:
    """Print the local report. This process has no published rows and no 30-day history."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        raise SystemExit("The measure command does not take arguments.")
    now = datetime.now(UTC)
    durations, rows_returned = sample_durations(now)
    locations = _location_ids()
    report = build_measurement(
        durations,
        [],
        now=now,
        rows=[],
        supported=_supported(locations, now),
        exclusions=[],
        verified_dates={},
        warmup_count=LOCAL_WARMUP,
        location_id=locations[0],
        rows_returned=rows_returned,
    )
    print(render_measurement(report), file=out or sys.stdout)
    return 0


def _latency(
    durations_seconds: list[float],
    warmup_count: int,
    location_id: str,
    rows_returned: int,
) -> dict:
    return {
        "environment": "local",
        "hosted_profile_signed_off": False,
        "readers": READERS,
        "location_id": location_id,
        "window_days": WINDOW_DAYS,
        "page_size": PAGE_SIZE,
        "route": "factors",
        "date_span_maximum": None,
        "request_quota": None,
        "exports_included": False,
        "timed_rows": rows_returned,
        "measures_are_null": True,
        "sample_release_published": False,
        "warmup_count": warmup_count,
        "sample_count": len(durations_seconds),
        "p95_seconds": _percentile_95(durations_seconds),
        "prd_target_seconds": PRD_TARGET_SECONDS,
        "prd_target_claimed": False,
        "confidence": None,
    }


def _coverage(result) -> dict:
    other = [code for code in result.violations if code != "coverage"]
    return {
        "numerator": COVERAGE_NUMERATOR,
        "denominator": COVERAGE_DENOMINATOR,
        "expected": result.expected,
        "present": result.present,
        "excluded": result.excluded,
        "missing": result.expected - result.present,
        "weather_present": result.weather_present,
        "weather_absent": result.present - result.weather_present,
        "other_violations": other,
        "meets_threshold": "coverage" not in result.violations,
    }


def _reliability(runs: list[RunRecord], now: datetime) -> dict:
    window_start = now - timedelta(days=RELIABILITY_WINDOW_DAYS)
    attempts = 0
    successes = 0
    unplaced = 0
    for run in runs:
        if run.stream != DAILY_STREAM:
            continue
        started = run.started_at
        if started is None or started.tzinfo is None:
            unplaced += 1
            continue
        if started < window_start or started > now:
            continue
        attempts += 1
        if run.status == "succeeded":
            successes += 1
    return {
        "numerator": RELIABILITY_NUMERATOR,
        "denominator": RELIABILITY_DENOMINATOR,
        "window_days": RELIABILITY_WINDOW_DAYS,
        "owner": OWNER,
        "rollback_threshold": None,
        "prelaunch_sufficient": False,
        "slo_claimed": False,
        "observed_attempts": attempts,
        "observed_successes": successes,
        "unplaced_runs": unplaced,
        "sample_meets_threshold": _ratio_met(successes, attempts),
        "missing_telemetry_counts_as_success": False,
    }


def _percentile_95(samples: list[float]) -> float:
    if not samples:
        raise ConnectorError(ErrorCategory.CONFIG, "A latency sample is required.")
    if any(sample < 0 for sample in samples):
        raise ConnectorError(ErrorCategory.CONFIG, "A latency sample must be zero or greater.")
    ordered = sorted(samples)
    index = math.ceil(95 * len(ordered) / 100) - 1
    return ordered[index]


def _ratio_met(successes: int, attempts: int) -> bool:
    if attempts <= 0:
        return False
    return successes * RELIABILITY_DENOMINATOR >= attempts * RELIABILITY_NUMERATOR


def _once(target: str, page: dict) -> float:
    started = time.perf_counter()
    status, _headers, body = handle_factors(
        "GET",
        target,
        load_release=lambda _release_id: _sample_release(),
        load_factors=lambda **_kwargs: page,
    )
    encoded = json.dumps(body)
    elapsed = time.perf_counter() - started
    if status != 200 or not encoded:
        raise ConnectorError(ErrorCategory.CONFIG, "The cached query sample did not succeed.")
    return elapsed


def _shape_page(location_id: str, start: date, end: date) -> dict:
    rows = []
    day = start
    while day <= end:
        parts = date_parts(day)
        rows.append(
            {
                "date": day.isoformat(),
                "location_id": location_id,
                "country_code": "PH",
                "temperature_mean_c": None,
                "precipitation_mm": None,
                "is_public_holiday": False,
                "holiday_name": None,
                "is_weekend": parts["is_weekend"],
                "weather_source_id": "open-meteo-era5",
                "holiday_source_id": "ph-official-gazette",
                "source_updated_at": None,
                "dataset_version": "1.0.0",
            }
        )
        day += timedelta(days=1)
    return {"total": len(rows), "rows": rows}


def _sample_release() -> dict:
    return {
        "dataset_id": _DATASET_ID,
        "release_id": _SAMPLE_RELEASE,
        "dataset_version": "1.0.0",
        "published_at": "2026-10-25T00:00:00Z",
    }


def _target(location_id: str, start: date, end: date) -> str:
    return (
        "/api/v1/factors/daily"
        f"?location_id={location_id}&start_date={start.isoformat()}"
        f"&end_date={end.isoformat()}&page=1&page_size={PAGE_SIZE}"
    )


def _supported(locations: list[str], now: datetime) -> set[tuple[str, str]]:
    document = _locations_document()
    start = date.fromisoformat(document["supported_start_date"])
    end = manila_today(now)
    keys: set[tuple[str, str]] = set()
    day = start
    while day <= end:
        stamp = day.isoformat()
        for location_id in locations:
            keys.add((location_id, stamp))
        day += timedelta(days=1)
    return keys


def _location_ids() -> list[str]:
    return [item["location_id"] for item in _locations_document()["locations"]]


def _locations_document() -> dict:
    return json.loads(_LOCATIONS_PATH.read_text(encoding="utf-8"))


def _require_aware(value: datetime, message: str) -> None:
    if value.tzinfo is None:
        raise ConnectorError(ErrorCategory.CONFIG, message)


if __name__ == "__main__":
    raise SystemExit(main())
