"""Holiday calendar rules. Shape dates are not proclamations."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from openfactors.ingestion.checkpoints import NATIONAL_LOCATION, CheckpointKey, MemoryCheckpoints
from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.ingestion.holidays import (
    SOURCE_ID,
    compile_year,
    load_publication_calendar,
    resolve_day,
)

ROOT = Path(__file__).resolve().parents[1]
CALENDAR = ROOT / "contracts" / "fixtures" / "holiday_calendar.json"
CASES = ROOT / "contracts" / "fixtures" / "holiday_calendar_cases.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_shape_cases_cover_scope_names_and_incomplete_years() -> None:
    payload = _load(CASES)
    assert payload["not_a_proclamation"] is True
    for case in payload["valid"]:
        compiled = compile_year(case, allow_shape=True)
        assert compiled.effective_status == case["expect_status"]
        years = {compiled.year: compiled}
        for expected in case["days"]:
            resolved = resolve_day(years, date.fromisoformat(expected["date"]))
            assert resolved.included_in_release is expected["included_in_release"]
            assert resolved.is_public_holiday is expected["is_public_holiday"]
            assert resolved.holiday_name == expected["holiday_name"]
            if resolved.included_in_release:
                assert resolved.holiday_source_id == SOURCE_ID
            else:
                assert resolved.is_public_holiday is not False
                assert resolved.holiday_source_id is None


@pytest.mark.parametrize("case", _load(CASES)["rejected"], ids=lambda case: case["case_id"])
def test_rejected_shape_rows_fail_schema(case: dict) -> None:
    with pytest.raises(ConnectorError) as caught:
        compile_year(case, allow_shape=True)
    assert caught.value.category is ErrorCategory.SCHEMA


def test_publication_calendar_transcribes_gazette_years() -> None:
    payload = _load(CALENDAR)
    assert "Alpha Day" not in CALENDAR.read_text(encoding="utf-8")
    years = load_publication_calendar(payload)
    assert list(years) == list(range(2018, 2027))
    assert all(year.effective_status == "complete" for year in years.values())
    checks = {
        date(2018, 1, 1): (True, "New Year's Day"),
        date(2018, 1, 2): (False, None),
        date(2018, 8, 21): (True, "Eid'l Adha | Ninoy Aquino Day"),
        date(2020, 4, 9): (True, "Araw ng Kagitingan | Maundy Thursday"),
        date(2021, 12, 31): (False, None),
        date(2023, 4, 9): (False, None),
        date(2023, 4, 10): (True, "Araw ng Kagitingan"),
        date(2023, 11, 30): (False, None),
        date(2024, 8, 21): (False, None),
        date(2024, 8, 23): (True, "Ninoy Aquino Day"),
        date(2025, 2, 25): (False, None),
        date(2025, 6, 6): (True, "Eidul Adha"),
        date(2026, 3, 20): (True, "Eid'l Fitr"),
    }
    for day, (flag, name) in checks.items():
        resolved = resolve_day(years, day)
        assert resolved.included_in_release is True
        assert resolved.is_public_holiday is flag
        assert resolved.holiday_name == name
        assert resolved.holiday_source_id == SOURCE_ID
    omitted = resolve_day(years, date(2017, 12, 25))
    assert omitted.included_in_release is False
    assert omitted.is_public_holiday is None


def test_shape_file_cannot_be_published() -> None:
    with pytest.raises(ConnectorError) as caught:
        load_publication_calendar(_load(CASES))
    assert caught.value.category is ErrorCategory.SCHEMA


def test_publication_year_must_cite_the_gazette() -> None:
    payload = {
        "source_id": SOURCE_ID,
        "years": [
            {
                "year": 2099,
                "declared_status": "complete",
                "proclamation_id": "shape",
                "source_url": "https://example.com/not-the-gazette",
                "observances": [
                    {
                        "name": "Alpha Day",
                        "category": "regular",
                        "scope": "national",
                        "date": "2099-01-04",
                    }
                ],
            }
        ],
    }
    with pytest.raises(ConnectorError) as caught:
        load_publication_calendar(payload)
    assert caught.value.category is ErrorCategory.SCHEMA


def test_compile_does_not_move_checkpoint() -> None:
    checkpoints = MemoryCheckpoints()
    key = CheckpointKey(SOURCE_ID, NATIONAL_LOCATION, "holiday")
    compile_year(_load(CASES)["valid"][0], allow_shape=True)
    assert checkpoints.get(key) is None
