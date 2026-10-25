"""OF-005 checks that the reconciled contract strings stay in place."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED = {
    "C_OpenFactors_LLD_Data_Models_Contracts_Notion_v1.md": [
        "retention_mode TEXT NOT NULL",
        "until_source_withdrawn",
        "ph-manila",
        "Never substitute retrieval time",
    ],
    "D_OpenFactors_API_Specification_Notion_v1.md": [
        "ph-manila",
        "open-meteo-era5",
        "ph-official-gazette",
        "NUMERIC(7,3)",
        "NUMERIC(10,3)",
    ],
    "D1_OpenFactors_OpenAPI_v1.yaml": [
        "Unsupported method",
        "upstream update timestamp",
        "single location only",
    ],
    "A_OpenFactors_PRD_Notion_v1.md": [
        "never the retrieval time",
    ],
    "contracts/fixtures/retention_policy.json": [
        "until_source_withdrawn",
    ],
}

FORBIDDEN_OUTSIDE_HISTORY = "PH-PILOT-001"
HISTORY_FILES = {
    "docs/decisions/ADR-005-contract-reconciliation.md",
}


def main() -> None:
    missing = []
    for relative, snippets in REQUIRED.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        for snippet in snippets:
            if snippet not in text:
                missing.append(f"{relative} missing {snippet!r}")
    leftovers = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".md", ".yaml", ".yml", ".json"}:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in HISTORY_FILES:
            continue
        if FORBIDDEN_OUTSIDE_HISTORY in path.read_text(encoding="utf-8"):
            leftovers.append(relative)
    if missing or leftovers:
        for line in missing + [f"retired placeholder still in {name}" for name in leftovers]:
            print(line)
        raise SystemExit(1)
    print("OF-005 contract alignment ok")


if __name__ == "__main__":
    main()
