"""Local public-beta gate. It is not an HTTP route, and it does not deploy.

The release checklist is still no-go, and this command has no public address
to publish. It does not create a release tag, start telemetry, or run a
post-deploy smoke. Cost and incident ownership stay the local records.
"""

from __future__ import annotations

import json
import logging
import sys

from openfactors.ingestion.errors import ConnectorError, ErrorCategory
from openfactors.orchestration.accept import build_acceptance, load_documents

logger = logging.getLogger("openfactors.orchestration")

_CHECKLIST = "A release report needs a checklist decision."

_OPERATOR = "Founder / Product Lead"


def build_release(acceptance: dict) -> dict:
    """Record a refused deployment. A go checklist still does not deploy here."""
    decision = _decision(acceptance)
    if decision == "go" and acceptance.get("signatures_present") is not True:
        raise ConnectorError(ErrorCategory.CONFIG, "A go decision needs signatures.")
    report = {
        "deployed": False,
        "public_url": None,
        "release_tag": None,
        "staged_rollout": "not_started",
        "acceptance_decision": decision,
        "open_items": _open_items(acceptance),
        "blocked_reason": _blocked_reason(decision),
        "rollback": {
            "local_procedure": "docs/decisions/recovery-drill.md",
            "public_rollback": False,
        },
        "ownership": {
            "approved_recurring_ceiling_usd": 10,
            "actual_recurring_spend_usd": 0,
            "operator": _OPERATOR,
            "paging": False,
            "alert": "A failed local run leaves a log line and a failed run record.",
        },
        "monitoring": {
            "operations_report": "python -m openfactors.orchestration.status",
            "grafana": False,
            "email": False,
        },
        "telemetry": {"started": False},
        "smoke": {"status": "not_run"},
    }
    logger.info(
        "release deployed=false acceptance=%s smoke=not_run",
        decision,
    )
    return report


def render_release(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def main(argv: list[str] | None = None, out=None) -> int:
    """Print the refusal for this repository. Arguments are refused."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        raise SystemExit("The release command does not take arguments.")
    acceptance = build_acceptance(load_documents())
    print(render_release(build_release(acceptance)), file=out or sys.stdout)
    return 0


def _decision(acceptance: dict) -> str:
    decision = acceptance.get("decision")
    if decision not in {"go", "no-go"} or not isinstance(acceptance.get("items"), list):
        raise ConnectorError(ErrorCategory.CONFIG, _CHECKLIST)
    return decision


def _open_items(acceptance: dict) -> list[str]:
    open_items = []
    for item in acceptance["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ConnectorError(ErrorCategory.CONFIG, _CHECKLIST)
        if item.get("proven") is not True:
            open_items.append(item["id"])
    if acceptance.get("signatures_present") is not True:
        open_items.append("signatures")
    return open_items


def _blocked_reason(decision: str) -> str:
    if decision == "no-go":
        return "The release checklist is no-go."
    return "This command does not start a public deployment."


if __name__ == "__main__":
    raise SystemExit(main())
