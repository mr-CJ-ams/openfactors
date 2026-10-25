"""Local release checklist. It is not an HTTP route, and it does not sign.

A go decision needs every required record to be closed. This repository still
has unset limits and no hosted profile. The five cities are confirmed. Holiday
years 2018 through 2026 are transcribed. The ERA5 pull through 2026-10-25 is
stored. The command reports the remaining gaps. It does not write a signature,
a production version, or a passing QA status.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sys
from pathlib import Path

from openfactors.ingestion.errors import ConnectorError, ErrorCategory

logger = logging.getLogger("openfactors.orchestration")

_ROOT = Path(__file__).resolve().parents[3]
_PATHS = {
    "adr001": "docs/decisions/ADR-001-pilot-locations.md",
    "adr003": "docs/decisions/ADR-003-budget-and-ownership.md",
    "adr004": "docs/decisions/ADR-004-retention-and-withdrawal.md",
    "adr005": "docs/decisions/ADR-005-contract-reconciliation.md",
    "holiday": "contracts/fixtures/holiday_calendar.json",
    "dictionary": "contracts/fixtures/data_dictionary.json",
    "sources": "contracts/fixtures/source_registry.json",
    "qa": "F1OpenFactors_QA_Test_Cases_Notion_Import_v1.csv",
    "signature": "docs/decisions/go-no-go-signature.md",
}
_SIGNATURE_LINES = (
    "Product owner signed",
    "Technical owner signed",
    "Residual risks signed",
    "Rollback plan signed",
)
_VERSION_PREFIX = "Production version: "


def build_acceptance(documents: dict[str, str]) -> dict:
    """Judge the records that were passed in. Missing proof stays a no-go."""
    items = _items(documents)
    signatures = _signatures_present(documents.get("signature", ""))
    decision = "go" if signatures and all(item["proven"] for item in items) else "no-go"
    report = {
        "decision": decision,
        "signatures_present": signatures,
        "product_owner": _owner_name(documents.get("adr001", "")),
        "technical_owner": None,
        "production_version": _version(documents.get("signature", "")),
        "items": items,
    }
    logger.info(
        "acceptance decision=%s signatures_present=%s",
        decision,
        str(signatures).lower(),
    )
    return report


def load_documents() -> dict[str, str]:
    """Read the checklist inputs. A missing signature file is an empty record."""
    loaded = {}
    for key, relative in _PATHS.items():
        path = _ROOT / relative
        if key == "signature" and not path.is_file():
            loaded[key] = ""
            continue
        loaded[key] = path.read_text(encoding="utf-8")
    return loaded


def render_acceptance(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def main(argv: list[str] | None = None, out=None) -> int:
    """Print the checklist for this repository. Arguments are refused."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        raise SystemExit("The acceptance command does not take arguments.")
    print(render_acceptance(build_acceptance(load_documents())), file=out or sys.stdout)
    return 0


def _items(documents: dict[str, str]) -> list[dict]:
    blockers = _unpassed_blockers(documents.get("qa", ""))
    holiday_years = _holiday_years(documents.get("holiday", ""))
    weather_open = "not stored yet" in _known_gaps(documents.get("dictionary", ""))
    return [
        _item(
            "product_owner_locations",
            _owner_signed(documents.get("adr001", "")),
            _PATHS["adr001"],
            "Product owner confirmation is still pending.",
        ),
        _item(
            "holiday_years",
            holiday_years > 0,
            _PATHS["holiday"],
            "No Official Gazette year is transcribed yet.",
        ),
        _item(
            "weather_history",
            not weather_open,
            _PATHS["dictionary"],
            "The multi-year weather pull is not stored yet.",
        ),
        _item(
            "query_limits",
            "Date span and rate numbers are approved." in documents.get("adr005", ""),
            _PATHS["adr005"],
            "Date span and rate numbers remain unset.",
        ),
        _item(
            "hosted_recovery",
            "Hosted recovery-time targets are approved." in documents.get("adr004", ""),
            _PATHS["adr004"],
            "Hosted recovery-time targets remain unset.",
        ),
        _item(
            "hosted_load",
            "The hosted load profile is signed off." in documents.get("adr003", ""),
            _PATHS["adr003"],
            "The hosted load profile is not signed off.",
        ),
        _item(
            "public_hosting",
            "Public hosting is approved." in documents.get("adr003", ""),
            _PATHS["adr003"],
            "Public hosting is not approved.",
        ),
        _item(
            "paging",
            "Paging is approved." in documents.get("adr003", ""),
            _PATHS["adr003"],
            "There is no paging service.",
        ),
        _item(
            "qa_release_blockers",
            blockers == 0,
            _PATHS["qa"],
            "Release-blocking QA cases are still not run.",
        ),
        _item(
            "source_redistribution",
            _sources_approved(documents.get("sources", "")),
            _PATHS["sources"],
            "A source is not approved for redistribution.",
            "The free Open-Meteo API stays non-commercial.",
        ),
        _item(
            "export_caps",
            "100,000 rows and 50 MB" in documents.get("adr005", ""),
            _PATHS["adr005"],
            "Export caps are not recorded.",
            "The caps are 100,000 rows and 50 MB.",
        ),
    ]


def _item(item_id: str, proven: bool, evidence: str, gap: str, note: str | None = None) -> dict:
    return {
        "id": item_id,
        "required": True,
        "proven": proven,
        "evidence": evidence,
        "gap": None if proven else gap,
        "note": note if proven else None,
    }


def _signatures_present(text: str) -> bool:
    if _version(text) is None:
        return False
    return all(line in text for line in _SIGNATURE_LINES)


def _version(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(_VERSION_PREFIX) and line.removeprefix(_VERSION_PREFIX).strip():
            return line.removeprefix(_VERSION_PREFIX).strip()
    return None


def _owner_signed(text: str) -> bool:
    name = _owner_name(text)
    if not name:
        return False
    for line in text.splitlines():
        if line.startswith("| Product owner |"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            return len(cells) >= 4 and "Pending" not in cells[3]
    return False


def _owner_name(text: str) -> str | None:
    for line in text.splitlines():
        if not line.startswith("| Product owner |"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[1]:
            return cells[1]
    return None


def _holiday_years(text: str) -> int:
    payload = _json_object(text, "The holiday calendar fixture is not valid JSON.")
    years = payload.get("years")
    if not isinstance(years, list):
        return 0
    return len(years)


def _known_gaps(text: str) -> str:
    payload = _json_object(text, "The data dictionary fixture is not valid JSON.")
    gaps = payload.get("known_gaps")
    if not isinstance(gaps, str):
        return "not stored yet"
    return gaps


def _sources_approved(text: str) -> bool:
    payload = _json_object(text, "The source registry fixture is not valid JSON.")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        return False
    for item in sources:
        if not isinstance(item, dict):
            return False
        status = item.get("license_status")
        attribution = item.get("attribution")
        if not isinstance(status, str) or not status.startswith("approved"):
            return False
        if not isinstance(attribution, str) or not attribution.strip():
            return False
        public = item.get("public_api_allowed") is True
        exported = item.get("csv_parquet_allowed") is True
        if not public or not exported:
            return False
    return True


def _unpassed_blockers(text: str) -> int | None:
    if not text.strip():
        return None
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    if "Release_Blocker" not in fields or "Status" not in fields:
        return None
    count = 0
    for row in reader:
        if row.get("Release_Blocker") == "Yes" and row.get("Status") != "Passed":
            count += 1
    return count


def _json_object(text: str, message: str) -> dict:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise ConnectorError(ErrorCategory.CONFIG, message) from None
    if not isinstance(payload, dict):
        raise ConnectorError(ErrorCategory.CONFIG, message)
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
