"""Notebook and SQL join. Sample orders are not a measured result."""

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, load_json
from examples.join_factors import (
    SAMPLE_BUSINESS,
    SAMPLE_FACTORS,
    SAMPLE_RELEASE_ID,
    join_outcomes,
    render_sql,
    run_join,
)
from ops.local_stack import docker_available
from ops.migrate import apply
from ops.promote import promote_release
from ops.releases import persist_manifest
from tests.test_factor_query import _set_source_status, _source_status
from tests.test_release_manifest import _row
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


def test_sql_join_runs_on_the_pinned_release() -> None:
    if not docker_available():
        pytest.skip("Docker is required to run the example SQL.")
    apply("dev")
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
        if prior != "approved":
            _set_source_status(prior)
