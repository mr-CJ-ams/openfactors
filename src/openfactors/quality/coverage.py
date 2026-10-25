"""Coverage, precipitation, and calendar gates for a candidate.

A FAIL result is not written to the publish table. Weather nulls stay in the
row-coverage numerator and are reported separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from contracts.calendar import date_parts

COVERAGE_NUMERATOR = 99
COVERAGE_DENOMINATOR = 100
_VIOLATION_ORDER = (
    "negative_precipitation",
    "unknown_holiday",
    "holiday_mismatch",
    "unverified_calendar",
    "weekend_mismatch",
    "coverage",
)


@dataclass(frozen=True)
class CoverageResult:
    status: str
    violations: tuple[str, ...]
    expected: int
    present: int
    excluded: int
    weather_present: int


def evaluate_coverage(
    rows: list[dict],
    *,
    supported: set[tuple[str, str]],
    exclusions: list[dict],
    verified_dates: dict[str, dict],
) -> CoverageResult:
    violations: set[str] = set()
    excluded = _approved_exclusions(supported, exclusions)
    expected = supported - excluded
    present: set[tuple[str, str]] = set()
    weather_present = 0
    for row in rows:
        _row_violations(row, verified_dates, violations)
        key = (row.get("location_id"), row.get("date"))
        if key not in expected or key in present:
            continue
        present.add(key)
        if _has_weather(row):
            weather_present += 1
    if not expected or len(present) * COVERAGE_DENOMINATOR < len(expected) * COVERAGE_NUMERATOR:
        violations.add("coverage")
    status = "FAIL" if violations else "PASS"
    ordered = tuple(code for code in _VIOLATION_ORDER if code in violations)
    return CoverageResult(
        status,
        ordered,
        len(expected),
        len(present),
        len(excluded),
        weather_present,
    )


def _approved_exclusions(
    supported: set[tuple[str, str]],
    exclusions: list[dict],
) -> set[tuple[str, str]]:
    approved: set[tuple[str, str]] = set()
    for item in exclusions:
        reason = item.get("reason")
        key = (item.get("location_id"), item.get("date"))
        documented = item.get("public") is True and isinstance(reason, str) and reason.strip()
        if documented and key in supported:
            approved.add(key)
    return approved


def _row_violations(row: dict, verified_dates: dict[str, dict], violations: set[str]) -> None:
    if _negative(row.get("precipitation_mm")):
        violations.add("negative_precipitation")
    local_date = row.get("date")
    flag, name = _holiday_pair(row.get("is_public_holiday"), row.get("holiday_name"))
    if _inconsistent_holiday(flag, name):
        violations.add("unknown_holiday")
    if not isinstance(local_date, str) or local_date not in verified_dates:
        violations.add("unverified_calendar")
    else:
        verified_flag, verified_name = _holiday_pair(
            verified_dates[local_date].get("is_public_holiday"),
            verified_dates[local_date].get("holiday_name"),
        )
        if (flag, name) != (verified_flag, verified_name):
            violations.add("holiday_mismatch")
    if isinstance(local_date, str):
        try:
            parsed = date.fromisoformat(local_date)
        except ValueError:
            parsed = None
        if parsed is not None and row.get("is_weekend") is not date_parts(parsed)["is_weekend"]:
            violations.add("weekend_mismatch")


def _holiday_pair(flag: object, name: object) -> tuple[object, str | None]:
    if isinstance(name, str):
        cleaned = name.strip() or None
    else:
        cleaned = None
    return flag, cleaned


def _inconsistent_holiday(flag: object, name: str | None) -> bool:
    if not isinstance(flag, bool):
        return True
    if flag and name is None:
        return True
    return not flag and name is not None


def _negative(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)) < 0
    except InvalidOperation:
        return False


def _has_weather(row: dict) -> bool:
    return row.get("temperature_mean_c") is not None or row.get("precipitation_mm") is not None
