"""ADR-009 inclusive date span on the public factor and export routes."""

from __future__ import annotations

from datetime import date

from openfactors.api.csv_export import EXPORT_PATH, handle_export
from openfactors.api.dataset_page import PAGE_PATH, handle_dataset_page
from openfactors.api.factors import _SPAN_MESSAGE, FACTORS_PATH, handle_factors

PIN = "00000000-0000-4000-8000-000000000366"
_ORDER = "start_date must be on or before end_date"


def test_one_day_365_and_366_spans_stay_valid() -> None:
    one_day = date(2018, 1, 1), date(2018, 1, 1)
    ordinary_year = date(2023, 1, 1), date(2023, 12, 31)
    leap_year = date(2024, 1, 1), date(2024, 12, 31)
    anniversary = date(2023, 1, 1), date(2024, 1, 1)
    for start, end in (one_day, ordinary_year, leap_year, anniversary):
        assert (end - start).days + 1 <= 366
        status, _headers, body = _factors(start, end)
        assert status == 200
        assert body["pagination"]["total"] == 0
        csv_status, csv_headers, csv_body = _export(start, end, "csv")
        assert csv_status == 200
        assert csv_headers["Content-Type"].startswith("text/csv")
        assert csv_body.startswith(b"date,")
        parquet_status, parquet_headers, parquet_body = _export(start, end, "parquet")
        assert parquet_status == 200
        assert parquet_headers["Content-Type"] == "application/vnd.apache.parquet"
        assert parquet_body[:4] == b"PAR1"


def test_a_367_date_span_is_rejected_on_every_public_route() -> None:
    start, end = date(2024, 1, 1), date(2025, 1, 1)
    assert (end - start).days + 1 == 367
    called = {"factors": 0, "rows": 0}

    def load_factors(**_kwargs: object) -> dict:
        called["factors"] += 1
        return {"total": 0, "rows": []}

    def load_rows(**_kwargs: object) -> list[dict]:
        called["rows"] += 1
        return []

    status, _headers, body = _factors(start, end, load_factors=load_factors)
    assert status == 422
    assert body["error"]["code"] == "INVALID_DATE_RANGE"
    assert body["error"]["message"] == _SPAN_MESSAGE
    assert body["error"]["details"] == {"field": "start_date"}
    for export_format in ("csv", "parquet"):
        export_status, _export_headers, export_body = _export(
            start, end, export_format, load_rows=load_rows
        )
        assert export_status == 422
        assert export_body["error"]["code"] == "INVALID_DATE_RANGE"
        assert export_body["error"]["message"] == _SPAN_MESSAGE
    page_status, page_headers, page_body = handle_dataset_page(
        "GET",
        (
            f"{PAGE_PATH}?location_id=ph-manila&start_date={start.isoformat()}"
            f"&end_date={end.isoformat()}&format=csv"
        ),
        load_release=_release,
    )
    assert page_status == 200
    assert page_headers["Content-Type"].startswith("text/html")
    assert _SPAN_MESSAGE in page_body.decode("utf-8")
    assert EXPORT_PATH not in page_body.decode("utf-8")
    assert called == {"factors": 0, "rows": 0}


def test_inverted_and_malformed_dates_keep_their_existing_errors() -> None:
    inverted, _inverted_headers, inverted_body = _factors(date(2018, 1, 2), date(2018, 1, 1))
    assert inverted == 422
    assert inverted_body["error"]["code"] == "INVALID_DATE_RANGE"
    assert inverted_body["error"]["message"] == _ORDER
    malformed, _malformed_headers, malformed_body = handle_factors(
        "GET",
        f"{FACTORS_PATH}?location_id=ph-manila&start_date=2018-02-30&end_date=2018-03-01",
        load_release=_release,
        load_factors=lambda **_kwargs: {"total": 0, "rows": []},
        locations=frozenset({"ph-manila"}),
    )
    assert malformed == 422
    assert malformed_body["error"]["code"] == "INVALID_DATE"
    assert malformed_body["error"]["message"] == "start_date must be an ISO date."


def _factors(start: date, end: date, *, load_factors=None):
    return handle_factors(
        "GET",
        (
            f"{FACTORS_PATH}?location_id=ph-manila&start_date={start.isoformat()}"
            f"&end_date={end.isoformat()}"
        ),
        load_release=_release,
        load_factors=load_factors or (lambda **_kwargs: {"total": 0, "rows": []}),
        locations=frozenset({"ph-manila"}),
        columns=("date",),
        generated_at="2026-10-29T00:00:00Z",
    )


def _export(start: date, end: date, export_format: str, *, load_rows=None):
    return handle_export(
        "GET",
        (
            f"{EXPORT_PATH}?location_id=ph-manila&start_date={start.isoformat()}"
            f"&end_date={end.isoformat()}&format={export_format}"
        ),
        load_release=_release,
        load_rows=load_rows or (lambda **_kwargs: []),
        locations=frozenset({"ph-manila"}),
    )


def _release(release_id: str | None) -> dict:
    assert release_id is None
    return {
        "dataset_id": "daily_external_factors_ph",
        "release_id": PIN,
        "dataset_version": "1.0.0",
        "published_at": "2026-10-24T16:00:00+00:00",
    }
