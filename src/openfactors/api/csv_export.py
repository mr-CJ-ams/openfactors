"""GET CSV export for one location and one pinned release.

A text cell that starts with a spreadsheet formula character is prefixed with
one apostrophe. That apostrophe is not part of the value. `restore_formula`
removes it again. Numbers, booleans, dates, and timestamps are not prefixed.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from openfactors.api.catalog import _bad_escape, _error, _split_target
from openfactors.api.export_limits import (
    MANIFEST_MEDIA_TYPE,
    MAX_EXPORT_BYTES,
    MAX_EXPORT_ROWS,
    attribution_manifest,
    commit_export,
    etag,
    export_link,
    export_sources,
    limit_message,
    load_registry,
    manifest_bytes,
    wants_manifest,
)
from openfactors.api.factors import (
    _approved_locations,
    _column_names,
    _filters,
    _single,
)

EXPORT_PATH = "/api/v1/exports/daily-external-factors-ph"
EXPORT_QUERY = {
    "location_id": True,
    "start_date": True,
    "end_date": True,
    "release_id": False,
    "format": True,
}
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_NULLABLE = frozenset(
    {"temperature_mean_c", "precipitation_mm", "holiday_name", "source_updated_at"}
)
_BOOLEANS = frozenset({"is_public_holiday", "is_weekend"})
_NUMBERS = frozenset({"temperature_mean_c", "precipitation_mm"})
RowLoader = Callable[..., list[dict]]


def handle_export(
    method: str,
    target: str,
    *,
    load_release: Callable[[str | None], dict | None],
    load_rows: RowLoader,
    locations: frozenset[str] | None = None,
    columns: tuple[str, ...] | None = None,
    request_headers: object | None = None,
    artifact_dir: Callable[[str], Path] | None = None,
    registry: dict | None = None,
    max_rows: int = MAX_EXPORT_ROWS,
    max_bytes: int = MAX_EXPORT_BYTES,
) -> tuple[int, dict[str, str], dict | bytes]:
    from openfactors.api.catalog import CatalogUnavailable

    path, query = _split_target(target)
    if path != EXPORT_PATH:
        return 404, {}, _error("NOT_FOUND", "That path is not available.")
    if method != "GET":
        return 405, {"Allow": "GET"}, _error("METHOD_NOT_ALLOWED", "Only GET is available.")
    if _bad_escape(query):
        return 400, {}, _error("MALFORMED_QUERY", "The query string is malformed.")
    from urllib.parse import parse_qs

    parameters = parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(parameters) - set(EXPORT_QUERY))
    if unknown:
        return (
            422,
            {},
            _error(
                "UNKNOWN_QUERY_PARAMETER",
                "Unknown query parameter.",
                {"parameter": unknown[0]},
            ),
        )
    text, problem = _single(parameters.get("format"))
    if problem == "missing":
        return (
            422,
            {},
            _error("MISSING_QUERY_PARAMETER", "format is required.", {"parameter": "format"}),
        )
    if problem:
        return (
            422,
            {},
            _error(
                "INVALID_QUERY_PARAMETER",
                "format must be a single value.",
                {"parameter": "format"},
            ),
        )
    if text not in {"csv", "parquet"}:
        return (
            422,
            {},
            _error("INVALID_FORMAT", "format must be csv or parquet.", {"parameter": "format"}),
        )
    parsed = _filters(parameters)
    if "error" in parsed:
        return 422, {}, parsed
    approved = _approved_locations() if locations is None else locations
    if parsed["location_id"] not in approved:
        return 404, {}, _error("NOT_FOUND", "That location is not available.")
    try:
        release = load_release(parsed["release_id"])
        if release is None or release.get("dataset_id") != "daily_external_factors_ph":
            return 404, {}, _error("NOT_FOUND", "That release is not available.")
        sources = export_sources(load_registry(registry))
        rows = load_rows(
            release_id=release["release_id"],
            location_id=parsed["location_id"],
            start_date=parsed["start_date"],
            end_date=parsed["end_date"],
        )
        if len(rows) > max_rows:
            return 422, {}, _limit_error("rows", max_rows)
        names = _column_names() if columns is None else columns
        if text == "parquet":
            from openfactors.api.parquet_export import PARQUET_MEDIA_TYPE, parquet_bytes

            file_body = parquet_bytes(
                rows,
                names,
                release_id=release["release_id"],
                dataset_version=release["dataset_version"],
            )
            media_type = PARQUET_MEDIA_TYPE
            suffix = ".parquet"
        else:
            file_body = csv_bytes(rows, names)
            media_type = "text/csv; charset=utf-8"
            suffix = ".csv"
        if len(file_body) > max_bytes:
            return 422, {}, _limit_error("bytes", max_bytes)
        filename = _filename(
            release["release_id"],
            parsed["start_date"],
            parsed["end_date"],
            suffix,
        )
        sidecar = manifest_bytes(
            attribution_manifest(
                dataset_id=release["dataset_id"],
                dataset_version=release["dataset_version"],
                release_id=release["release_id"],
                location_id=parsed["location_id"],
                start_date=parsed["start_date"],
                end_date=parsed["end_date"],
                export_format=text,
                row_count=len(rows),
                body=file_body,
                sources=sources,
            )
        )
        if artifact_dir is not None:
            commit_export(artifact_dir(release["release_id"]), filename, file_body, sidecar)
        if wants_manifest(request_headers):
            body = sidecar
            media_type = MANIFEST_MEDIA_TYPE
            filename = _filename(
                release["release_id"],
                parsed["start_date"],
                parsed["end_date"],
                ".manifest.json",
            )
        else:
            body = file_body
        headers = {
            "Content-Type": media_type,
            "X-Release-Id": release["release_id"],
            "X-Dataset-Version": release["dataset_version"],
            "X-Data-As-Of": release["published_at"],
            "Content-Disposition": f'attachment; filename="{filename}"',
            "ETag": etag(body),
            "Link": export_link(release["release_id"], sources),
        }
        return 200, headers, body
    except CatalogUnavailable:
        return 503, {}, _error("EXPORT_UNAVAILABLE", "The export is temporarily unavailable.")
    except Exception:
        return 500, {}, _error("INTERNAL", "The export failed.")


def _limit_error(unit: str, limit: int) -> dict:
    return _error("EXPORT_LIMIT", limit_message(unit, limit), {"limit": limit, "unit": unit})


def csv_bytes(rows: list[dict], columns: tuple[str, ...]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(name, row.get(name)) for name in columns])
    return buffer.getvalue().encode("utf-8")


def parse_csv(payload: bytes, columns: tuple[str, ...]) -> list[dict]:
    text = payload.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(columns):
        raise ValueError("CSV headers do not match the 12 public fields.")
    parsed = []
    for raw in reader:
        parsed.append({name: _parse_cell(name, raw[name]) for name in columns})
    return parsed


def guard_formula(value: str) -> str:
    if value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def restore_formula(value: str) -> str:
    if len(value) > 1 and value[0] == "'" and value[1:].startswith(FORMULA_PREFIXES):
        return value[1:]
    return value


def _cell(name: str, value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if name in _NUMBERS and not isinstance(value, str):
        return _number(value)
    text = str(value)
    if name == "holiday_name":
        return guard_formula(text)
    return text


def _parse_cell(name: str, value: str) -> object:
    if value == "":
        return None if name in _NULLABLE else ""
    if name in _BOOLEANS:
        if value == "true":
            return True
        if value == "false":
            return False
        return value
    if name in _NUMBERS:
        return Decimal(value)
    if name == "holiday_name":
        return restore_formula(value)
    return value


def _number(value: object) -> str:
    text = format(Decimal(str(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _filename(release_id: str, start: str, end: str, suffix: str) -> str:
    parts = ["daily-external-factors-ph", release_id, start, end]
    cleaned = [
        "".join(char if char.isalnum() or char in ".-" else "_" for char in part) for part in parts
    ]
    return "_".join(cleaned) + suffix
