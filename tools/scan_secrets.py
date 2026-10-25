"""Fail if tracked text looks like a credential.

Patterns are assembled in code so this file does not contain a literal secret.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
}
MAX_BYTES = 1_000_000

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile("-----BEGIN " + r"(?:RSA |OPENSSH |EC )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile("AK" + r"IA[0-9A-Z]{16}")),
    ("github-token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github-fine-grained-token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("openai-style-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
)


def scan_text(text: str) -> list[str]:
    return [name for name, pattern in RULES if pattern.search(text)]


def _skip_path(path: Path, root: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return relative.parts[:3] == ("ops", "local", "runtime")


def find_secrets(root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or _skip_path(path, root):
            continue
        if path.stat().st_size > MAX_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for rule in scan_text(text):
            findings.append(f"{rule}: {path.relative_to(root).as_posix()}")
    return findings


def main() -> None:
    findings = find_secrets()
    if findings:
        print("\n".join(findings))
        raise SystemExit(1)
    print("secret scan ok")


if __name__ == "__main__":
    main()
