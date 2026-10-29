"""Provisional export ceilings, the attribution manifest, and a finished file.

100,000 rows and 50,000,000 bytes are the ceilings already stated in ADR-005.
A span over 366 inclusive dates is refused by the shared date check before a file
is built. No separate request-quota number is chosen here. A file is
renamed into place only after its attribution manifest is complete. A failed
publish deletes both.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path

MAX_EXPORT_ROWS = 100_000
MAX_EXPORT_BYTES = 50_000_000
MANIFEST_MEDIA_TYPE = "application/vnd.openfactors.export-manifest+json"


def etag(payload: bytes) -> str:
    return f'"{hashlib.sha256(payload).hexdigest()}"'


def limit_message(unit: str, limit: int) -> str:
    if unit == "rows":
        counted = "rows"
    else:
        counted = "bytes"
    return (
        f"The export exceeds {limit} {counted}. "
        "Split the date interval and request each part separately."
    )


def wants_manifest(headers: object) -> bool:
    if headers is None:
        return False
    getter = getattr(headers, "get", None)
    if getter is None:
        return False
    accept = getter("Accept")
    if accept is None:
        accept = getter("accept")
    return isinstance(accept, str) and MANIFEST_MEDIA_TYPE in accept


def load_registry(registry: dict | None) -> dict:
    if registry is not None:
        return registry
    from openfactors.api.catalog import REGISTRY_PATH

    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def export_sources(registry: dict) -> list[dict]:
    from openfactors.api.catalog import _public_source

    chosen = []
    for source in registry.get("sources", []):
        if source.get("csv_parquet_allowed") is not True:
            continue
        public = _public_source(source)
        if not _complete(public):
            raise ValueError("An export source is missing attribution.")
        chosen.append(public)
    roles = {item["role"] for item in chosen}
    if not {"weather", "holiday"} <= roles:
        raise ValueError("An export source is missing attribution.")
    return chosen


def export_link(release_id: str, sources: list[dict]) -> str:
    from openfactors.api.catalog import DATASET_PATH

    links = [f'<{DATASET_PATH}?release_id={release_id}>; rel="describedby"']
    links.extend(f'<{source["url"]}>; rel="license"' for source in sources)
    return ", ".join(links)


def attribution_manifest(
    *,
    dataset_id: str,
    dataset_version: str,
    release_id: str,
    location_id: str,
    start_date: str,
    end_date: str,
    export_format: str,
    row_count: int,
    body: bytes,
    sources: list[dict],
) -> dict:
    return {
        "dataset_id": dataset_id,
        "dataset_version": dataset_version,
        "release_id": release_id,
        "location_id": location_id,
        "start_date": start_date,
        "end_date": end_date,
        "format": export_format,
        "row_count": row_count,
        "byte_count": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "sources": sources,
    }


def manifest_bytes(document: dict) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def commit_export(
    directory: Path,
    filename: str,
    body: bytes,
    manifest: bytes,
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    partial = directory / f".{filename}.partial"
    final = directory / filename
    manifest_partial = directory / f".{filename}.manifest.partial"
    manifest_final = directory / f"{filename}.manifest.json"
    try:
        partial.write_bytes(body)
        manifest_partial.write_bytes(manifest)
        _checkpoint(checkpoint, "partial")
        os.replace(manifest_partial, manifest_final)
        _checkpoint(checkpoint, "manifest")
        os.replace(partial, final)
    except Exception:
        for path in (partial, final, manifest_partial, manifest_final):
            path.unlink(missing_ok=True)
        raise


_UNFINISHED = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]*\.partial$")


def sweep_unfinished_exports(root: Path) -> None:
    """Delete recognized partial files inside UUID export folders.

    Completed exports, other names, and files outside those folders stay.
    Symbolic links stay, including a link whose target is a partial file.
    """
    if not root.is_dir() or root.is_symlink():
        return
    root_resolved = root.resolve()
    for child in root.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            parsed = uuid.UUID(child.name)
        except ValueError:
            continue
        if str(parsed) != child.name:
            continue
        child_resolved = child.resolve()
        if not _within(child_resolved, root_resolved):
            continue
        for item in child.iterdir():
            if item.is_symlink() or not item.is_file():
                continue
            if _UNFINISHED.fullmatch(item.name) is None:
                continue
            if not _within(item.resolve(), child_resolved):
                continue
            item.unlink()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _checkpoint(checkpoint: Callable[[str], None] | None, name: str) -> None:
    if checkpoint is not None:
        checkpoint(name)


def _complete(source: dict) -> bool:
    url = source.get("url")
    return bool(source.get("attribution") and source.get("license") and _safe_https(url))


def _safe_https(url: object) -> bool:
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    forbidden = set(' \t\r\n"<> ,')
    return not any(char in forbidden for char in url)
