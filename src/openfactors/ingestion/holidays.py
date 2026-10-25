"""Philippine national holiday calendar.

A published day is true or false only inside a complete year.
An incomplete year, a missing movable date, or a year that was never
transcribed is omitted. It is not stored as a non-holiday.
Local holidays and special working days do not set the public flag.
Several names on one date are joined with " | " in alphabetical order.
Reading the calendar does not move a checkpoint.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from openfactors.ingestion.errors import ConnectorError, ErrorCategory

SOURCE_ID = "ph-official-gazette"
GAZETTE_PREFIX = "https://www.officialgazette.gov.ph/"
IN_SCOPE = frozenset({"regular", "special_non_working"})
EXCLUDED = frozenset({"local", "special_working"})
NAME_SEPARATOR = " | "


@dataclass(frozen=True)
class Observance:
    name: str
    category: str
    scope: str
    date: date | None


@dataclass(frozen=True)
class CompiledYear:
    year: int
    declared_status: str
    effective_status: str
    observances: tuple[Observance, ...]


@dataclass(frozen=True)
class HolidayDay:
    date: date
    included_in_release: bool
    is_public_holiday: bool | None
    holiday_name: str | None
    holiday_source_id: str | None


def load_publication_calendar(payload: dict) -> dict[int, CompiledYear]:
    if payload.get("not_a_proclamation") is True:
        raise ConnectorError(ErrorCategory.SCHEMA, "A shape calendar cannot be published.")
    if payload.get("source_id") != SOURCE_ID:
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday source is not the Official Gazette.")
    years = payload.get("years")
    if not isinstance(years, list):
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday calendar has no years list.")
    return _index(compile_year(item, allow_shape=False) for item in years)


def compile_year(doc: dict, *, allow_shape: bool) -> CompiledYear:
    if not isinstance(doc, dict):
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday year is not an object.")
    year = doc.get("year")
    if not isinstance(year, int):
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday year is not an integer.")
    declared = doc.get("declared_status")
    if declared not in {"complete", "incomplete"}:
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday year status is not recognized.")
    if not allow_shape:
        _require_gazette_citation(doc)
    raw_rows = doc.get("observances")
    if not isinstance(raw_rows, list):
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday year has no observances.")
    observances = tuple(_observance(item, year) for item in raw_rows)
    return CompiledYear(
        year=year,
        declared_status=declared,
        effective_status=_effective_status(declared, observances),
        observances=observances,
    )


def resolve_day(years: dict[int, CompiledYear], day: date) -> HolidayDay:
    year = years.get(day.year)
    if year is None or year.effective_status != "complete":
        return HolidayDay(day, False, None, None, None)
    names = [item.name for item in year.observances if _qualifies(item) and item.date == day]
    if not names:
        return HolidayDay(day, True, False, None, SOURCE_ID)
    return HolidayDay(day, True, True, join_names(names), SOURCE_ID)


def join_names(names: list[str]) -> str:
    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        text = name.strip()
        if not text:
            raise ConnectorError(ErrorCategory.SCHEMA, "Holiday name is empty.")
        if text not in seen:
            seen.add(text)
            unique.append(text)
    return NAME_SEPARATOR.join(sorted(unique))


def _index(years: Iterable[CompiledYear]) -> dict[int, CompiledYear]:
    found: dict[int, CompiledYear] = {}
    for year in years:
        if year.year in found:
            raise ConnectorError(ErrorCategory.SCHEMA, "Holiday year is duplicated.")
        found[year.year] = year
    return found


def _require_gazette_citation(doc: dict) -> None:
    proclamation_id = doc.get("proclamation_id")
    source_url = doc.get("source_url")
    if not isinstance(proclamation_id, str) or not proclamation_id.strip():
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday year has no proclamation id.")
    if not isinstance(source_url, str) or not source_url.startswith(GAZETTE_PREFIX):
        raise ConnectorError(
            ErrorCategory.SCHEMA,
            "Holiday year is not cited to the Official Gazette.",
        )


def _observance(item: dict, year: int) -> Observance:
    if not isinstance(item, dict):
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday observance is not an object.")
    name = item.get("name")
    category = item.get("category")
    scope = item.get("scope")
    if not isinstance(name, str) or not name.strip():
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday name is empty.")
    if category not in IN_SCOPE | EXCLUDED:
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday category is not recognized.")
    if scope not in {"national", "local"}:
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday scope is not recognized.")
    return Observance(name.strip(), category, scope, _local_date(item.get("date"), year))


def _local_date(value: object, year: int) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday date is not a local date.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday date is not a local date.") from exc
    if parsed.year != year:
        raise ConnectorError(ErrorCategory.SCHEMA, "Holiday date is outside its calendar year.")
    return parsed


def _effective_status(declared: str, observances: tuple[Observance, ...]) -> str:
    in_scope = [item for item in observances if _qualifies(item)]
    if declared == "incomplete" or not in_scope or any(item.date is None for item in in_scope):
        return "incomplete"
    return "complete"


def _qualifies(item: Observance) -> bool:
    return item.scope == "national" and item.category in IN_SCOPE
