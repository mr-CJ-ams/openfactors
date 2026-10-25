"""HTML catalog page for the published dataset list.

The page shows geography, grain, coverage, the last successful publication,
terms, sources, and known gaps. It does not show a failed ingestion time.
The response clock is labeled separately from publication time.
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs

from openfactors.api.catalog import (
    DICTIONARY_PATH,
    REGISTRY_PATH,
    CatalogUnavailable,
    _bad_escape,
    _error,
    _split_target,
    catalog_payload,
    dictionary_payload,
)

PAGE_PATH = "/"
_UNAVAILABLE = "The catalog is temporarily unavailable."
_EMPTY = "No published dataset is available."
_CAVEAT = (
    "Weather and holidays are contextual factors, not proof of causation for business outcomes."
)


def handle_catalog_page(
    method: str,
    target: str,
    *,
    load_rows: Callable[[], list[dict]],
    dictionary: dict | None = None,
    registry: dict | None = None,
    generated_at: str | None = None,
) -> tuple[int, dict[str, str], dict | bytes]:
    path, query = _split_target(target)
    if path != PAGE_PATH:
        return 404, {}, _error("NOT_FOUND", "That path is not available.")
    if method != "GET":
        return 405, {"Allow": "GET"}, _error("METHOD_NOT_ALLOWED", "Only GET is available.")
    if _bad_escape(query):
        return 400, {}, _error("MALFORMED_QUERY", "The query string is malformed.")
    parameters = parse_qs(query, keep_blank_values=True)
    unknown = sorted(parameters)
    if unknown:
        return (
            422,
            {},
            _error(
                "UNKNOWN_QUERY_PARAMETER",
                "Unknown query parameter.",
                {"parameter": unknown[0]},
            ),
        )
    try:
        rows = load_rows()
        document = dictionary if dictionary is not None else _json(DICTIONARY_PATH)
        sources = registry if registry is not None else _json(REGISTRY_PATH)
        stamp = generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = catalog_payload(rows, document, stamp)
        described = _described(rows, document, sources, stamp)
        if len(described) != len(payload["datasets"]):
            raise RuntimeError("The catalog page and the catalog list diverged.")
        body = render_catalog_page(payload, described).encode("utf-8")
        return 200, {"Content-Type": "text/html; charset=utf-8"}, body
    except CatalogUnavailable:
        body = render_unavailable().encode("utf-8")
        return 503, {"Content-Type": "text/html; charset=utf-8"}, body
    except Exception:
        return 500, {}, _error("INTERNAL", "The catalog request failed.")


def render_catalog_page(payload: dict, described: list[dict]) -> str:
    articles = [
        _article(dataset, detail)
        for dataset, detail in zip(payload["datasets"], described, strict=True)
    ]
    if articles:
        listing = "\n".join(articles)
    else:
        listing = f"<p>{_EMPTY}</p>"
    generated = _text(payload["generated_at"])
    return _document(
        f"<h2>Published datasets</h2>\n{listing}\n"
        f'<p>Catalog response generated <time datetime="{generated}">{generated}</time>.</p>'
    )


def render_unavailable() -> str:
    return _document(f"<h2>Published datasets</h2>\n<p>{_UNAVAILABLE}</p>")


def _described(rows: list[dict], dictionary: dict, registry: dict, generated_at: str) -> list[dict]:
    pages = []
    for row in rows:
        published_at = row.get("published_at")
        if not published_at or row.get("dataset_id") != dictionary["dataset_id"]:
            continue
        release = {
            "dataset_id": row["dataset_id"],
            "dataset_version": row["dataset_version"],
            "release_id": row["active_release_id"],
            "published_at": published_at,
            "coverage_start": row["coverage_start"],
            "coverage_end": row["coverage_end"],
        }
        pages.append(dictionary_payload(release, dictionary, registry, generated_at))
    return pages


def _article(dataset: dict, detail: dict) -> str:
    published = _text(dataset["freshness"]["last_successful_publication"])
    version = _text(dataset["dataset_version"])
    release_id = _text(dataset["active_release_id"])
    coverage = dataset["coverage"]
    sources = "\n".join(_source(source) for source in detail["sources"])
    gaps = "\n".join(f"<li>{_text(item)}</li>" for item in detail["limitations"])
    if not gaps:
        gaps = "<li>No gaps are recorded for this release.</li>"
    return f"""<article>
<h3>{_text(dataset["dataset_id"])}</h3>
<p>Schema version {version}. Active release {release_id}.</p>
<h4>Grain</h4>
<p>{_text(dataset["grain"])}</p>
<h4>Coverage</h4>
<p>Geography: {_text(coverage["geographic"])}</p>
<p>Dates: {_text(coverage["start"])} through {_text(coverage["end"])}.</p>
<h4>Last successful publication</h4>
<p><time datetime="{published}">{published}</time></p>
<h4>Terms</h4>
<p>{_text(dataset["license"]["summary"])}</p>
<p>{_text(dataset["license"]["attribution"])}</p>
<h4>Sources</h4>
<ul>
{sources}
</ul>
<h4>Gaps</h4>
<ul>
{gaps}
</ul>
<p><a href="/datasets/daily-external-factors-ph">Dictionary and download</a></p>
</article>"""


def _source(source: dict) -> str:
    attribution = _text(source["attribution"])
    license_text = _text(source["license"])
    url = source.get("url")
    if _safe_https(url):
        href = _text(url)
        label = f'<a href="{href}">{attribution}</a>'
    else:
        label = attribution
    return f"<li>{label}. {license_text}.</li>"


def _document(main: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenFactors catalog</title>
<style>
body {{
  font-family: "Segoe UI", sans-serif;
  line-height: 1.5;
  max-width: 42rem;
  margin: 2rem auto;
  padding: 0 1rem;
  color: #1a1a1a;
  background: #fff;
}}
a:focus {{ outline: 3px solid #1a1a1a; outline-offset: 2px; }}
</style>
</head>
<body>
<a href="#catalog">Skip to catalog</a>
<header>
<h1>OpenFactors</h1>
<p>{_CAVEAT}</p>
<p><a href="/docs">API guide</a></p>
</header>
<main id="catalog">
{main}
</main>
</body>
</html>
"""


def _text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _safe_https(url: object) -> bool:
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    return not any(char in url for char in ' \t\r\n"<>')


def _json(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
