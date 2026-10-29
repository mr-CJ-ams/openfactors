"""Public beta gate. The checklist refusal stays in place, and nothing is deployed."""

import io
import json
import subprocess
from pathlib import Path

import pytest

from openfactors.ingestion.errors import ConnectorError
from openfactors.orchestration.accept import build_acceptance, load_documents
from openfactors.orchestration.release import build_release, main


def test_repository_release_is_not_deployed() -> None:
    before = _tags()
    report = build_release(build_acceptance(load_documents()))
    assert report["deployed"] is False
    assert report["public_url"] is None
    assert report["release_tag"] is None
    assert report["staged_rollout"] == "not_started"
    assert report["acceptance_decision"] == "no-go"
    assert report["blocked_reason"] == "The release checklist is no-go."
    assert "product_owner_locations" not in report["open_items"]
    assert "holiday_years" not in report["open_items"]
    assert "weather_history" not in report["open_items"]
    assert "query_limits" in report["open_items"]
    assert "signatures" in report["open_items"]
    assert report["rollback"]["public_rollback"] is False
    assert report["rollback"]["local_procedure"] == "docs/decisions/recovery-drill.md"
    assert report["ownership"]["approved_recurring_ceiling_usd"] == 10
    assert report["ownership"]["actual_recurring_spend_usd"] == 0
    assert "cost_cap_usd" not in report["ownership"]
    assert report["ownership"]["operator"] == "Founder / Product Lead"
    assert report["ownership"]["paging"] is False
    assert report["monitoring"]["grafana"] is False
    assert report["monitoring"]["email"] is False
    assert report["telemetry"]["started"] is False
    assert report["smoke"]["status"] == "not_run"
    assert _tags() == before


def test_go_checklist_still_does_not_deploy() -> None:
    report = build_release(
        {
            "decision": "go",
            "signatures_present": True,
            "items": [{"id": "product_owner_locations", "proven": True}],
        }
    )
    assert report["deployed"] is False
    assert report["release_tag"] is None
    assert report["public_url"] is None
    assert report["smoke"]["status"] == "not_run"
    assert report["telemetry"]["started"] is False
    assert report["open_items"] == []
    assert report["blocked_reason"] == "This command does not start a public deployment."


def test_release_report_fails_closed() -> None:
    with pytest.raises(ConnectorError, match="checklist decision"):
        build_release({})
    with pytest.raises(ConnectorError, match="needs signatures"):
        build_release({"decision": "go", "signatures_present": False, "items": []})


def test_hosting_proposal_does_not_approve_a_public_release() -> None:
    text = (
        Path(__file__).resolve().parents[1] / "docs/decisions/ADR-006-zero-cost-public-hosting.md"
    ).read_text(encoding="utf-8")
    assert "Not approved" in text
    assert "No host is selected" in text
    for phrase in (
        "Date span and rate numbers are approved.",
        "Hosted recovery-time targets are approved.",
        "The hosted load profile is signed off.",
        "Public hosting is approved.",
        "Paging is approved.",
        "Product owner signed",
        "Technical owner signed",
        "Residual risks signed",
        "Rollback plan signed",
        "Production version:",
    ):
        assert phrase not in text


def test_limits_proposal_does_not_approve_or_enforce() -> None:
    text = (
        Path(__file__).resolve().parents[1] / "docs/decisions/ADR-008-public-beta-query-limits.md"
    ).read_text(encoding="utf-8")
    history = (
        Path(__file__).resolve().parents[1] / "docs/decisions/ADR-005-contract-reconciliation.md"
    ).read_text(encoding="utf-8")
    assert "Reconsidered" in text
    assert "Not approved" in text
    assert "Not enforced" in text
    assert "not measured benchmarks" in text
    assert "The 366-day proposal is reconsidered" in text
    assert "60 requests per minute per client IP" in text
    assert "provisional until staging measurements" in text
    assert "Loopback stays unlimited." in text
    assert "100,000 rows" in text
    assert "1,000" in text
    assert "3,221" in text
    assert "286,098" in text
    assert "No date-span maximum and no rate-limit numbers." in history
    for phrase in (
        "Date span and rate numbers are approved.",
        "Hosted recovery-time targets are approved.",
        "The hosted load profile is signed off.",
        "Public hosting is approved.",
        "Paging is approved.",
        "Product owner signed",
        "Technical owner signed",
        "Residual risks signed",
        "Rollback plan signed",
        "Production version:",
    ):
        assert phrase not in text
    from openfactors.api.export_limits import MAX_EXPORT_BYTES, MAX_EXPORT_ROWS
    from openfactors.api.factors import _filters

    assert MAX_EXPORT_ROWS == 100_000
    assert MAX_EXPORT_BYTES == 50_000_000
    long_span = _filters(
        {
            "location_id": ["ph-manila"],
            "start_date": ["2024-01-01"],
            "end_date": ["2025-01-01"],
        }
    )
    assert long_span["error"]["code"] == "INVALID_DATE_RANGE"
    allowed = _filters(
        {
            "location_id": ["ph-manila"],
            "start_date": ["2024-01-01"],
            "end_date": ["2024-12-31"],
        }
    )
    assert "error" not in allowed
    assert allowed["page_size"] == 100


def test_release_command_prints_the_refusal() -> None:
    out = io.StringIO()
    assert main([], out) == 0
    report = json.loads(out.getvalue())
    assert report["deployed"] is False
    assert report["smoke"]["status"] == "not_run"
    with pytest.raises(SystemExit, match="The release command does not take arguments."):
        main(["--deploy"])


def _tags() -> list[str]:
    completed = subprocess.run(
        ["git", "tag", "--list"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.splitlines()
