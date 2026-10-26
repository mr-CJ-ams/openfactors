"""Notebook and SQL join. Sample orders are not a measured result."""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from contracts.calendar import date_parts
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from examples.join_factors import (
    SAMPLE_BUSINESS,
    SAMPLE_FACTORS,
    SAMPLE_RELEASE_ID,
    join_outcomes,
    main,
    render_sql,
    run_join,
)
from ops.local_stack import dev_database_ready
from ops.migrate import apply
from ops.promote import active_release_id, promote_release, restore_active_release
from ops.releases import persist_manifest
from tests.test_factor_query import _set_source_status, _source_status
from tests.test_release_manifest import _build, _row
from tests.test_release_promotion import _approved, _cleanup

NOTEBOOK = Path("examples/join_factors.ipynb")


def test_notebook_joins_the_sample_and_leaves_the_missing_day_out() -> None:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    markdown = "\n".join(
        "".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "markdown"
    )
    assert "does not show a cause" in markdown
    namespace: dict = {}
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            exec("".join(cell["source"]), namespace)
    assert namespace["rows"] == [
        {
            "release_id": SAMPLE_RELEASE_ID,
            "location_id": "ph-manila",
            "date": "2018-01-01",
            "orders": 4,
            "temperature_mean_c": "26.1",
            "precipitation_mm": "3.4",
            "is_public_holiday": False,
        }
    ]
    assert len(SAMPLE_BUSINESS) == 2


def test_join_rejects_a_bad_pin_and_another_release() -> None:
    with pytest.raises(ValueError, match="release_id must be a UUID."):
        join_outcomes("not-a-release", SAMPLE_FACTORS, SAMPLE_BUSINESS)
    other = {**SAMPLE_FACTORS[0], "release_id": "00000000-0000-4000-8000-000000000099"}
    with pytest.raises(ValueError, match="another release"):
        join_outcomes(SAMPLE_RELEASE_ID, [other], SAMPLE_BUSINESS)
    with pytest.raises(ValueError, match="duplicated"):
        join_outcomes(SAMPLE_RELEASE_ID, [SAMPLE_FACTORS[0], SAMPLE_FACTORS[0]], SAMPLE_BUSINESS)
    rendered = render_sql(SAMPLE_RELEASE_ID)
    assert rendered.count(SAMPLE_RELEASE_ID) == 1
    assert "__RELEASE_ID__" not in rendered


def test_example_command_refuses_arguments_and_a_stopped_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit, match="no arguments"):
        main(["dev"])
    monkeypatch.setattr("ops.local_stack.dev_database_ready", lambda: False)
    with pytest.raises(SystemExit, match="not running"):
        main([])


def test_example_command_joins_the_active_local_release(capsys: pytest.CaptureFixture[str]) -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required to run the example SQL.")
    with published_history_fixture() as release_id:
        capsys.readouterr()
        assert main([]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].startswith("2018-01-01 ph-manila orders=4 ")
        assert lines[0].endswith("is_public_holiday=true")
        assert Decimal(lines[0].split("temperature_c=")[1].split()[0]) == Decimal("26.1")
        assert lines[1].startswith("2018-01-02 ph-manila orders=5 ")
        assert lines[1].endswith("is_public_holiday=false")
        assert lines[2] == f"Release {release_id}. Sample orders are not a measured result."


def test_sql_join_runs_on_the_pinned_release() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required to run the example SQL.")
    apply("dev")
    previous = active_release_id("dev")
    prior = _source_status()
    release_id = str(uuid.uuid4())
    try:
        if prior != "approved":
            _set_source_status("approved")
        contract = load_json(CONTRACT_PATH)
        manifest = _approved(contract, _row(), release_id)
        persist_manifest("dev", manifest)
        promote_release("dev", manifest, [_row()])
        rows = run_join("dev", release_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["location_id"] == "ph-manila"
        assert row["date"] == "2018-01-01"
        assert row["orders"] == 4
        assert Decimal(row["temperature_mean_c"]) == Decimal("26.1")
        assert Decimal(row["precipitation_mm"]) == Decimal("3.4")
        assert row["is_public_holiday"] is False
    finally:
        _cleanup([release_id])
        restore_active_release("dev", previous)
        if prior != "approved":
            _set_source_status(prior)


@contextmanager
def published_history_fixture() -> Iterator[str]:
    """Publish three fixture rows, then put the previous release back."""
    apply("dev")
    previous = active_release_id("dev")
    prior = _source_status()
    release_id = str(uuid.uuid4())
    if prior != "approved":
        _set_source_status("approved")
    try:
        rows = _history_rows()
        contract = load_json(CONTRACT_PATH)
        days = {row["date"] for row in rows}
        manifest = _build(
            contract,
            rows,
            release_id=release_id,
            dates=days,
            supported={("ph-manila", row["date"]) for row in rows},
            verified_dates={
                row["date"]: {
                    "is_public_holiday": row["is_public_holiday"],
                    "holiday_name": row["holiday_name"],
                }
                for row in rows
            },
            coverage_start=min(days),
            coverage_end=max(days),
        )
        assert manifest.status == "approved", (
            manifest.schema_gate.violations,
            manifest.coverage.violations,
        )
        persist_manifest("dev", manifest)
        promote_release("dev", manifest, rows)
        yield release_id
    finally:
        _cleanup([release_id])
        restore_active_release("dev", previous)
        if prior != "approved":
            _set_source_status(prior)
        assert active_release_id("dev") == previous


def _history_rows() -> list[dict]:
    specs = (
        ("2018-01-01", "26.1", "3.4", True, "New Year's Day", "available"),
        ("2018-01-02", "25.0", "0.0", False, None, "available"),
        ("2026-10-20", None, None, False, None, "missing"),
    )
    rows = []
    for day, temperature, precipitation, holiday, name, status in specs:
        rows.append(
            {
                "date": day,
                "location_id": "ph-manila",
                "country_code": "PH",
                "temperature_mean_c": temperature,
                "precipitation_mm": precipitation,
                "is_public_holiday": holiday,
                "holiday_name": name,
                "is_weekend": date_parts(date.fromisoformat(day))["is_weekend"],
                "weather_source_id": "open-meteo-era5",
                "holiday_source_id": "ph-official-gazette",
                "source_updated_at": None,
                "dataset_version": "1.0.0",
                "weather_data_status": status,
            }
        )
    return rows
