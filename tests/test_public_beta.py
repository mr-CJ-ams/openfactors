"""Public beta gate. The checklist refusal stays in place, and nothing is deployed."""

import io
import json
import subprocess

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
    assert report["ownership"]["cost_cap_usd"] == 0
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
