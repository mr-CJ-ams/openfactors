"""GET /api/v1/factors/daily. One location, inclusive dates, one pinned release."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

from ops.api_read import ApiReadError, read_sql, sql_identifiers

from openfactors.api.catalog import (
    CatalogUnavailable,
    _bad_escape,
    _error,
    _json_file,
    _release_pin,
    _split_target,
)

FACTORS_PATH = "/api/v1/factors/daily"
_ROOT = Path(__file__).resolve().parents[3]
_LOCATIONS_PATH = _ROOT / "contracts" / "fixtures" / "pilot_locations.json"
_DICTIONARY_PATH = _ROOT / "contracts" / "fixtures" / "data_dictionary.json"
_LOCATION_UNAVAILABLE = "That location is not available."
_RELEASE_UNAVAILABLE = "That release is not available."
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INTEGER = re.compile(r"^(0|[1-9][0-9]*)$")
_MAX_OFFSET = 2**63 - 1
_REQUIRED = ("location_id", "start_date", "end_date")
_ALLOWED = {"location_id", "start_date", "end_date", "release_id", "page", "page_size"}
FactorLoader = Callable[..., dict]


def handle_factors(
    method: str,
    target: str,
    *,
    load_release: Callable[[str | None], dict | None],
    load_factors: FactorLoader,
    locations: frozenset[str] | None = None,
    columns: tuple[str, ...] | None = None,
    generated_at: str | None = None,
) -> tuple[int, dict[str, str], dict]:
    path, query = _split_target(target)
    if path != FACTORS_PATH:
        return 404, {}, _error("NOT_FOUND", "That path is not available.")
    if method != "GET":
        return 405, {"Allow": "GET"}, _error("METHOD_NOT_ALLOWED", "Only GET is available.")
    if _bad_escape(query):
        return 400, {}, _error("MALFORMED_QUERY", "The query string is malformed.")
    parameters = _parameters(query)
    if isinstance(parameters, dict) and "error" in parameters:
        return 422, {}, parameters
    assert isinstance(parameters, dict)
    parsed = _filters(parameters)
    if "error" in parsed:
        return 422, {}, parsed
    approved = _approved_locations() if locations is None else locations
    if parsed["location_id"] not in approved:
        return 404, {}, _error("NOT_FOUND", _LOCATION_UNAVAILABLE)
    try:
        release = load_release(parsed["release_id"])
        if release is None or release.get("dataset_id") != "daily_external_factors_ph":
            return 404, {}, _error("NOT_FOUND", _RELEASE_UNAVAILABLE)
        page = load_factors(
            release_id=release["release_id"],
            location_id=parsed["location_id"],
            start_date=parsed["start_date"],
            end_date=parsed["end_date"],
            page=parsed["page"],
            page_size=parsed["page_size"],
        )
        names = _column_names() if columns is None else columns
        stamp = generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = _page(release, page, names, parsed["page"], parsed["page_size"], stamp)
        headers = {
            "X-Release-Id": payload["release_id"],
            "X-Dataset-Version": payload["dataset_version"],
            "X-Data-As-Of": payload["data_as_of"],
        }
        return 200, headers, payload
    except CatalogUnavailable:
        return (
            503,
            {},
            _error("FACTORS_UNAVAILABLE", "The factor query is temporarily unavailable."),
        )
    except Exception:
        return 500, {}, _error("INTERNAL", "The factor query failed.")


def load_factor_page(
    environment: str,
    *,
    release_id: str,
    location_id: str,
    start_date: str,
    end_date: str,
    page: int,
    page_size: int,
) -> dict:
    names = _column_names()
    selected = sql_identifiers(names)
    offset = (page - 1) * page_size
    query = f"""
    WITH matched AS (
      SELECT {selected}
      FROM publish.accessible_factors
      WHERE release_id = :'release_id'::uuid
        AND location_id = :'location_id'
        AND date >= :'start_date'::date
        AND date <= :'end_date'::date
    ),
    paged AS (
      SELECT * FROM matched
      ORDER BY date ASC, location_id ASC
      LIMIT :page_size OFFSET :row_offset
    )
    SELECT json_build_object(
      'total', (SELECT count(*) FROM matched),
      'rows', COALESCE((
        SELECT json_agg(to_jsonb(paged) ORDER BY paged.date, paged.location_id)
        FROM paged
      ), '[]'::json)
    )::text;
    """
    try:
        completed = read_sql(
            environment,
            query,
            variables={
                "release_id": str(uuid.UUID(release_id)),
                "location_id": location_id,
                "start_date": start_date,
                "end_date": end_date,
            },
            integers={"page_size": page_size, "row_offset": offset},
            tuples_only=True,
        )
    except ApiReadError as exc:
        raise CatalogUnavailable from exc
    if completed.returncode != 0:
        raise CatalogUnavailable
    payload = _json_line(completed.stdout)
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise CatalogUnavailable
    total = payload.get("total")
    if isinstance(total, bool) or not isinstance(total, int):
        raise CatalogUnavailable
    return {"total": total, "rows": payload["rows"]}


def load_export_rows(
    environment: str,
    *,
    release_id: str,
    location_id: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    names = _column_names()
    selected = sql_identifiers(names)
    query = f"""
    WITH matched AS (
      SELECT {selected}
      FROM publish.accessible_factors
      WHERE release_id = :'release_id'::uuid
        AND location_id = :'location_id'
        AND date >= :'start_date'::date
        AND date <= :'end_date'::date
    )
    SELECT COALESCE((
      SELECT json_agg(to_jsonb(matched) ORDER BY matched.date, matched.location_id)
      FROM matched
    ), '[]'::json)::text;
    """
    try:
        completed = read_sql(
            environment,
            query,
            variables={
                "release_id": str(uuid.UUID(release_id)),
                "location_id": location_id,
                "start_date": start_date,
                "end_date": end_date,
            },
            tuples_only=True,
        )
    except ApiReadError as exc:
        raise CatalogUnavailable from exc
    if completed.returncode != 0:
        raise CatalogUnavailable
    payload = _json_value(completed.stdout)
    if not isinstance(payload, list):
        raise CatalogUnavailable
    return payload


def _page(
    release: dict,
    page: dict,
    columns: tuple[str, ...],
    page_number: int,
    page_size: int,
    generated_at: str,
) -> dict:
    return {
        "dataset_id": release["dataset_id"],
        "dataset_version": release["dataset_version"],
        "release_id": release["release_id"],
        "generated_at": generated_at,
        "data_as_of": release["published_at"],
        "pagination": {"page": page_number, "page_size": page_size, "total": page["total"]},
        "data": [{name: row.get(name) for name in columns} for row in page["rows"]],
    }


def _parameters(query: str) -> dict:
    from urllib.parse import parse_qs

    parameters = parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(parameters) - _ALLOWED)
    if unknown:
        return _error(
            "UNKNOWN_QUERY_PARAMETER",
            "Unknown query parameter.",
            {"parameter": unknown[0]},
        )
    return parameters


def _filters(parameters: dict) -> dict:
    values: dict[str, str] = {}
    for name in _REQUIRED:
        text, problem = _single(parameters.get(name))
        if problem == "missing":
            return _error(
                "MISSING_QUERY_PARAMETER",
                f"{name} is required.",
                {"parameter": name},
            )
        if problem:
            return _error(
                "INVALID_QUERY_PARAMETER",
                f"{name} must be a single value.",
                {"parameter": name},
            )
        values[name] = text or ""
    start = _iso_date(values["start_date"])
    end = _iso_date(values["end_date"])
    if start is None:
        return _error("INVALID_DATE", "start_date must be an ISO date.", {"field": "start_date"})
    if end is None:
        return _error("INVALID_DATE", "end_date must be an ISO date.", {"field": "end_date"})
    if start > end:
        return _error(
            "INVALID_DATE_RANGE",
            "start_date must be on or before end_date",
            {"field": "start_date"},
        )
    pin, invalid_pin = _release_pin(parameters.get("release_id"))
    if invalid_pin:
        return _error(
            "INVALID_RELEASE_ID",
            "release_id must be a UUID.",
            {"parameter": "release_id"},
        )
    page, invalid_page = _whole_number(parameters.get("page"), maximum=_MAX_OFFSET)
    if invalid_page or page == 0:
        return _error(
            "INVALID_PAGE",
            "page must be an integer greater than or equal to 1.",
            {"field": "page"},
        )
    page_size, invalid_size = _whole_number(parameters.get("page_size"), maximum=1000)
    if invalid_size or page_size == 0:
        return _error(
            "INVALID_PAGE_SIZE",
            "page_size must be an integer from 1 to 1000.",
            {"field": "page_size"},
        )
    resolved_page = 1 if page is None else page
    resolved_size = 100 if page_size is None else page_size
    if (resolved_page - 1) * resolved_size > _MAX_OFFSET:
        return _error(
            "INVALID_PAGE",
            "page must be an integer greater than or equal to 1.",
            {"field": "page"},
        )
    return {
        "location_id": values["location_id"],
        "start_date": values["start_date"],
        "end_date": values["end_date"],
        "release_id": pin,
        "page": resolved_page,
        "page_size": resolved_size,
    }


def _single(values: list[str] | None) -> tuple[str | None, str]:
    if values is None:
        return None, "missing"
    if len(values) != 1 or not values[0]:
        return None, "invalid"
    return values[0], ""


def _iso_date(value: str) -> date | None:
    if _DATE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _whole_number(values: list[str] | None, *, maximum: int) -> tuple[int | None, bool]:
    if values is None:
        return None, False
    if len(values) != 1 or _INTEGER.fullmatch(values[0]) is None:
        return None, True
    number = int(values[0])
    if number > maximum:
        return None, True
    return number, False


def _approved_locations() -> frozenset[str]:
    document = _json_file(_LOCATIONS_PATH)
    return frozenset(
        item["location_id"] for item in document["locations"] if item["approved_for_mvp"] is True
    )


def _column_names() -> tuple[str, ...]:
    document = _json_file(_DICTIONARY_PATH)
    return tuple(column["name"] for column in document["columns"])


def _json_line(text: str) -> dict:
    payload = _json_value(text)
    if not isinstance(payload, dict):
        raise CatalogUnavailable
    return payload


def _json_value(text: str) -> dict | list:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            payload = json.loads(stripped)
            if isinstance(payload, dict | list):
                return payload
    raise CatalogUnavailable
