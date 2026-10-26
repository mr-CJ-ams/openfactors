"""Public dataset catalog and dictionary."""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ops.api_read import ApiReadError, bind_address, export_directory, read_sql

from openfactors.api.errors import Limiter, api_error, limit_response

CATALOG_PATH = "/api/v1/datasets"
DATASET_PATH = "/api/v1/datasets/daily-external-factors-ph"
CATALOG_PARAMETERS: frozenset[str] = frozenset()
DICTIONARY_PARAMETERS: frozenset[str] = frozenset({"release_id"})
_ROOT = Path(__file__).resolve().parents[3]
DICTIONARY_PATH = _ROOT / "contracts" / "fixtures" / "data_dictionary.json"
REGISTRY_PATH = _ROOT / "contracts" / "fixtures" / "source_registry.json"
_RELEASE_UNAVAILABLE = "That release is not available."
_COLUMN_KEYS = (
    "name",
    "logical_type",
    "physical_type",
    "nullable",
    "unit",
    "description",
    "source",
    "transformation",
    "examples",
)
_HEX = set("0123456789abcdefABCDEF")


class CatalogUnavailable(Exception):
    """The catalog view could not be read."""


def catalog_payload(rows: list[dict], dictionary: dict, generated_at: str) -> dict:
    datasets = []
    for row in rows:
        published_at = row.get("published_at")
        if not published_at or row.get("dataset_id") != dictionary["dataset_id"]:
            continue
        datasets.append(
            {
                "dataset_id": row["dataset_id"],
                "active_release_id": row["active_release_id"],
                "dataset_version": row["dataset_version"],
                "grain": dictionary["grain"],
                "coverage": {
                    "start": row["coverage_start"],
                    "end": row["coverage_end"],
                    "geographic": dictionary["geographic_coverage"],
                },
                "license": {
                    "summary": dictionary["license"],
                    "attribution": dictionary["attribution"],
                },
                "freshness": {
                    "data_as_of": published_at,
                    "last_successful_publication": published_at,
                },
            }
        )
    return {"generated_at": generated_at, "datasets": datasets}


def handle_catalog(
    method: str,
    target: str,
    *,
    load_rows: Callable[[], list[dict]],
    dictionary: dict | None = None,
    generated_at: str | None = None,
) -> tuple[int, dict[str, str], dict]:
    path, query = _split_target(target)
    if path != CATALOG_PATH:
        return 404, {}, _error("NOT_FOUND", "That path is not available.")
    if method != "GET":
        return 405, {"Allow": "GET"}, _error("METHOD_NOT_ALLOWED", "Only GET is available.")
    if _bad_escape(query):
        return 400, {}, _error("MALFORMED_QUERY", "The query string is malformed.")
    parameters = parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(parameters) - CATALOG_PARAMETERS)
    if unknown:
        name = unknown[0]
        return (
            422,
            {},
            _error(
                "UNKNOWN_QUERY_PARAMETER",
                "Unknown query parameter.",
                {"parameter": name},
            ),
        )
    try:
        rows = load_rows()
        document = (
            dictionary
            if dictionary is not None
            else json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
        )
        stamp = generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return 200, {}, catalog_payload(rows, document, stamp)
    except CatalogUnavailable:
        return 503, {}, _error("CATALOG_UNAVAILABLE", "The catalog is temporarily unavailable.")
    except Exception:
        return 500, {}, _error("INTERNAL", "The catalog request failed.")


def dictionary_payload(release: dict, dictionary: dict, registry: dict, generated_at: str) -> dict:
    holiday = next(
        source for source in registry["sources"] if source["source_id"] == "ph-official-gazette"
    )
    return {
        "release": {
            "dataset_id": release["dataset_id"],
            "dataset_version": release["dataset_version"],
            "release_id": release["release_id"],
            "generated_at": generated_at,
            "data_as_of": release["published_at"],
        },
        "grain": dictionary["grain"],
        "timezone": dictionary["timezone"],
        "columns": [{key: column[key] for key in _COLUMN_KEYS} for column in dictionary["columns"]],
        "sources": [_public_source(source) for source in registry["sources"]],
        "license": {
            "summary": dictionary["license"],
            "attribution": dictionary["attribution"],
        },
        "coverage": {
            "start": release["coverage_start"],
            "end": release["coverage_end"],
            "geographic": dictionary["geographic_coverage"],
            "temporal": dictionary["temporal_coverage"],
        },
        "limitations": [
            dictionary["known_gaps"],
            dictionary["provider_spatial_resolution"],
            dictionary["revision_policy"],
            dictionary["deprecation_policy"],
            holiday["unknown_rule"],
            *holiday["excluded"],
        ],
    }


def handle_dictionary(
    method: str,
    target: str,
    *,
    load_release: Callable[[str | None], dict | None],
    dictionary: dict | None = None,
    registry: dict | None = None,
    generated_at: str | None = None,
) -> tuple[int, dict[str, str], dict]:
    path, query = _split_target(target)
    if path != DATASET_PATH:
        return 404, {}, _error("NOT_FOUND", "That path is not available.")
    if method != "GET":
        return 405, {"Allow": "GET"}, _error("METHOD_NOT_ALLOWED", "Only GET is available.")
    if _bad_escape(query):
        return 400, {}, _error("MALFORMED_QUERY", "The query string is malformed.")
    parameters = parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(parameters) - DICTIONARY_PARAMETERS)
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
    pin, invalid = _release_pin(parameters.get("release_id"))
    if invalid:
        return (
            422,
            {},
            _error("INVALID_RELEASE_ID", "release_id must be a UUID.", {"parameter": "release_id"}),
        )
    try:
        release = load_release(pin)
        if release is None:
            return 404, {}, _error("NOT_FOUND", _RELEASE_UNAVAILABLE)
        document = _json_file(DICTIONARY_PATH) if dictionary is None else dictionary
        recorded = _json_file(REGISTRY_PATH) if registry is None else registry
        stamp = generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return 200, {}, dictionary_payload(release, document, recorded, stamp)
    except CatalogUnavailable:
        return 503, {}, _error("CATALOG_UNAVAILABLE", "The catalog is temporarily unavailable.")
    except Exception:
        return 500, {}, _error("INTERNAL", "The catalog request failed.")


def load_accessible_release(environment: str, release_id: str | None) -> dict | None:
    if release_id is None:
        query = """
        SELECT COALESCE((
          SELECT json_build_object(
            'dataset_id', dataset_id,
            'release_id', active_release_id,
            'dataset_version', dataset_version,
            'coverage_start', coverage_start,
            'coverage_end', coverage_end,
            'published_at', published_at
          )
          FROM publish.dataset_catalog
        ), 'null'::json)::text;
        """
    else:
        pinned = str(uuid.UUID(release_id))
        query = """
        SELECT COALESCE((
          SELECT json_build_object(
            'dataset_id', dataset_id,
            'release_id', release_id,
            'dataset_version', dataset_version,
            'coverage_start', coverage_start,
            'coverage_end', coverage_end,
            'published_at', published_at
          )
          FROM publish.accessible_releases
          WHERE release_id = :'release_id'::uuid
        ), 'null'::json)::text;
        """
    try:
        completed = read_sql(
            environment,
            query,
            variables=None if release_id is None else {"release_id": pinned},
            tuples_only=True,
        )
    except ApiReadError as exc:
        raise CatalogUnavailable from exc
    if completed.returncode != 0:
        raise CatalogUnavailable
    text = completed.stdout.strip()
    if not text:
        raise CatalogUnavailable
    payload = json.loads(text)
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise CatalogUnavailable
    return payload


def load_catalog_rows(environment: str) -> list[dict]:
    try:
        completed = read_sql(
            environment,
            """
            SELECT COALESCE(json_agg(json_build_object(
              'dataset_id', dataset_id,
              'active_release_id', active_release_id,
              'dataset_version', dataset_version,
              'coverage_start', coverage_start,
              'coverage_end', coverage_end,
              'published_at', published_at
            )), '[]'::json)::text
            FROM publish.dataset_catalog;
            """,
            tuples_only=True,
        )
    except ApiReadError as exc:
        raise CatalogUnavailable from exc
    if completed.returncode != 0:
        raise CatalogUnavailable
    text = completed.stdout.strip()
    if not text:
        raise CatalogUnavailable
    payload = json.loads(text)
    if not isinstance(payload, list):
        raise CatalogUnavailable
    return payload


def open_catalog(
    environment: str,
    host: str | None = None,
    port: int | None = None,
) -> ThreadingHTTPServer:
    """Bind the catalog. The default is 127.0.0.1:8000."""
    bind_host, bind_port = bind_address(host, port)

    from openfactors.api.factors import load_export_rows, load_factor_page

    return catalog_server(
        lambda: load_catalog_rows(environment),
        bind_host,
        bind_port,
        load_release=lambda release_id: load_accessible_release(environment, release_id),
        load_factors=lambda **filters: load_factor_page(environment, **filters),
        load_export=lambda **filters: load_export_rows(environment, **filters),
        artifact_dir=lambda release_id: export_directory(environment, release_id),
    )


def serve_catalog(
    environment: str,
    host: str | None = None,
    port: int | None = None,
) -> None:
    open_catalog(environment, host, port).serve_forever()


def main(argv: list[str] | None = None) -> int:
    """Listen for the published release. The default bind is 127.0.0.1:8000."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        raise SystemExit("The catalog listens on 127.0.0.1 by default and takes no arguments.")
    from ops.api_read import load_database

    try:
        load_database("dev")
    except ApiReadError as exc:
        raise SystemExit(str(exc)) from exc
    if not os.environ.get("OPENFACTORS_API_PGHOST", "").strip():
        from ops.local_stack import dev_database_ready

        if not dev_database_ready():
            raise SystemExit(
                "The local dev database is not running. Start it, then run this command again."
            )
    try:
        host, port = bind_address()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"http://{host}:{port}", flush=True)
    serve_catalog("dev", host, port)
    return 0


def catalog_server(
    load_rows: Callable[[], list[dict]],
    host: str = "127.0.0.1",
    port: int = 0,
    load_release: Callable[[str | None], dict | None] | None = None,
    load_factors: Callable[..., dict] | None = None,
    load_export: Callable[..., list[dict]] | None = None,
    limiter: Limiter | None = None,
    artifact_dir: Callable[[str], Path] | None = None,
    observe: Callable[[str, int], None] | None = None,
) -> ThreadingHTTPServer:
    resolve = load_release or (lambda _release_id: None)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._respond("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._respond("PUT")

        def do_PATCH(self) -> None:  # noqa: N802
            self._respond("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            self._respond("DELETE")

        def _respond(self, method: str) -> None:
            status, headers, payload = _dispatch(
                method,
                self.path,
                load_rows=load_rows,
                load_release=resolve,
                load_factors=load_factors,
                load_export=load_export,
                limiter=limiter,
                request_headers=self.headers,
                artifact_dir=artifact_dir,
            )
            if observe is not None:
                observe(self.path, status)
            if isinstance(payload, bytes):
                body = payload
                content_type = headers.pop("Content-Type", "application/octet-stream")
            else:
                body = json.dumps(payload).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def _dispatch(
    method: str,
    target: str,
    *,
    load_rows: Callable[[], list[dict]],
    load_release: Callable[[str | None], dict | None],
    load_factors: Callable[..., dict] | None = None,
    load_export: Callable[..., list[dict]] | None = None,
    limiter: Limiter | None = None,
    request_headers: object | None = None,
    artifact_dir: Callable[[str], Path] | None = None,
) -> tuple[int, dict[str, str], dict | bytes]:
    path, _query = _split_target(target)
    limited = limit_response(method, path, limiter)
    if limited is not None:
        return limited
    if path == "/openapi.json":
        from openfactors.api.openapi import handle_openapi

        return handle_openapi(method, target)
    if path == "/":
        from openfactors.api.catalog_page import handle_catalog_page

        return handle_catalog_page(method, target, load_rows=load_rows)
    if path == "/docs":
        from openfactors.api.api_guide import handle_api_guide

        return handle_api_guide(method, target)
    if path == "/datasets/daily-external-factors-ph":
        from openfactors.api.dataset_page import handle_dataset_page

        return handle_dataset_page(method, target, load_release=load_release)
    if path == DATASET_PATH:
        return handle_dictionary(method, target, load_release=load_release)
    if path == "/api/v1/factors/daily":
        if load_factors is None:
            return 404, {}, _error("NOT_FOUND", "That path is not available.")
        from openfactors.api.factors import handle_factors

        return handle_factors(
            method,
            target,
            load_release=load_release,
            load_factors=load_factors,
        )
    if path == "/api/v1/exports/daily-external-factors-ph":
        if load_export is None:
            return 404, {}, _error("NOT_FOUND", "That path is not available.")
        from openfactors.api.csv_export import handle_export

        return handle_export(
            method,
            target,
            load_release=load_release,
            load_rows=load_export,
            request_headers=request_headers,
            artifact_dir=artifact_dir,
        )
    return handle_catalog(method, target, load_rows=load_rows)


def _public_source(source: dict) -> dict:
    return {
        "source_id": source["source_id"],
        "role": source["role"],
        "attribution": source["attribution"],
        "license": source["license"],
        "url": source.get("attribution_url") or source["terms_url"],
    }


def _release_pin(values: list[str] | None) -> tuple[str | None, bool]:
    if values is None:
        return None, False
    if len(values) != 1 or not values[0]:
        return None, True
    try:
        return str(uuid.UUID(values[0])), False
    except ValueError:
        return None, True


def _json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _split_target(target: str) -> tuple[str, str]:
    parts = urlsplit(target)
    return parts.path, parts.query


def _bad_escape(query: str) -> bool:
    index = 0
    while index < len(query):
        if query[index] != "%":
            index += 1
            continue
        chunk = query[index + 1 : index + 3]
        if len(chunk) < 2 or any(character not in _HEX for character in chunk):
            return True
        index += 3
    return False


def _error(code: str, message: str, details: dict | None = None) -> dict:
    return api_error(code, message, details)


if __name__ == "__main__":
    raise SystemExit(main())
