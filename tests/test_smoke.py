from pathlib import Path

from openfactors import __version__

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (
    "ingestion",
    "transform",
    "serving/api",
    "serving/web",
    "ops",
)


def test_package_imports() -> None:
    assert __version__ == "0.1.0"


def test_monorepo_packages_exist() -> None:
    missing = [name for name in PACKAGES if not (ROOT / name).is_dir()]
    assert missing == []


def test_contract_alignment() -> None:
    from contracts.check_contract_alignment import main

    assert main() is None
