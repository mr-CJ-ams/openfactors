"""Release checklist. Missing proof stays a no-go, and this command does not sign."""

import io
import json

import pytest

from openfactors.ingestion.errors import ConnectorError
from openfactors.orchestration.accept import build_acceptance, load_documents, main

_SIGNATURE = "\n".join(
    (
        "Product owner signed",
        "Technical owner signed",
        "Residual risks signed",
        "Rollback plan signed",
        "Production version: test-only",
    )
)


def test_repository_checklist_is_no_go() -> None:
    report = build_acceptance(load_documents())
    assert report["decision"] == "no-go"
    assert report["signatures_present"] is False
    assert report["product_owner"] == "Founder / Product Lead"
    assert report["technical_owner"] is None
    assert report["production_version"] is None
    items = {item["id"]: item for item in report["items"]}
    assert items["source_redistribution"]["proven"] is True
    assert items["source_redistribution"]["note"] == "The free Open-Meteo API stays non-commercial."
    assert items["export_caps"]["proven"] is True
    assert items["product_owner_locations"]["proven"] is True
    assert items["product_owner_locations"]["gap"] is None
    assert items["holiday_years"]["proven"] is True
    assert items["holiday_years"]["gap"] is None
    assert items["weather_history"]["proven"] is True
    assert items["weather_history"]["gap"] is None
    for item_id in (
        "query_limits",
        "hosted_recovery",
        "hosted_load",
        "public_hosting",
        "paging",
        "qa_release_blockers",
    ):
        assert items[item_id]["proven"] is False
        assert items[item_id]["gap"]
    assert all(item["required"] for item in report["items"])


def test_one_open_item_keeps_the_decision_no_go() -> None:
    documents = _closed_documents()
    documents["holiday"] = '{"years": []}'
    report = build_acceptance(documents)
    assert report["decision"] == "no-go"
    assert report["signatures_present"] is True
    holiday = next(item for item in report["items"] if item["id"] == "holiday_years")
    assert holiday["proven"] is False


def test_closed_records_can_go_without_this_command_signing() -> None:
    report = build_acceptance(_closed_documents())
    assert report["decision"] == "go"
    assert report["signatures_present"] is True
    assert report["product_owner"] == "test-signature"
    assert report["technical_owner"] is None
    assert report["production_version"] == "test-only"
    assert all(item["proven"] for item in report["items"])


def test_invalid_fixture_fails_closed() -> None:
    documents = _closed_documents()
    documents["holiday"] = "not-json"
    with pytest.raises(ConnectorError, match="holiday calendar fixture"):
        build_acceptance(documents)


def test_acceptance_command_prints_no_go() -> None:
    out = io.StringIO()
    assert main([], out) == 0
    report = json.loads(out.getvalue())
    assert report["decision"] == "no-go"
    assert report["signatures_present"] is False
    with pytest.raises(SystemExit, match="The acceptance command does not take arguments."):
        main(["--sign"])


def _closed_documents() -> dict[str, str]:
    return {
        "adr001": "| Product owner | test-signature | 2026-10-25 | Confirmed |\n",
        "adr003": "\n".join(
            (
                "The hosted load profile is signed off.",
                "Public hosting is approved.",
                "Paging is approved.",
            )
        ),
        "adr004": "Hosted recovery-time targets are approved.\n",
        "adr005": "100,000 rows and 50 MB\nDate span and rate numbers are approved.\n",
        "holiday": '{"years": ["recorded-in-test"]}',
        "dictionary": '{"known_gaps": "none in this test"}',
        "sources": json.dumps(
            {
                "sources": [
                    {
                        "license_status": "approved_noncommercial",
                        "attribution": "Weather data by Open-Meteo.com",
                        "public_api_allowed": True,
                        "csv_parquet_allowed": True,
                    }
                ]
            }
        ),
        "qa": "Release_Blocker,Status\nYes,Passed\n",
        "signature": _SIGNATURE,
    }
