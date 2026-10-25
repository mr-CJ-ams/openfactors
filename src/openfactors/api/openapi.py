"""GET /openapi.json and the diff against the routes this server implements.

The approved YAML remains the contract. A path or status that the server does
not serve must be listed in contracts/fixtures/openapi_diffs.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from openfactors.api.catalog import (
    CATALOG_PARAMETERS,
    CATALOG_PATH,
    DATASET_PATH,
    DICTIONARY_PARAMETERS,
    DICTIONARY_PATH,
    REGISTRY_PATH,
    _bad_escape,
    _error,
    _split_target,
    catalog_payload,
    dictionary_payload,
)
from openfactors.api.csv_export import EXPORT_PATH, EXPORT_QUERY
from openfactors.api.factors import _ALLOWED, _REQUIRED, FACTORS_PATH, _page

OPENAPI_ROUTE = "/openapi.json"
_ROOT = Path(__file__).resolve().parents[3]
APPROVED_PATH = _ROOT / "D1_OpenFactors_OpenAPI_v1.yaml"
DIFFS_PATH = _ROOT / "contracts" / "fixtures" / "openapi_diffs.json"
CONTRACT_PATH = _ROOT / "contracts" / "fixtures" / "daily_factor_contract.json"
_STAMP = "2026-10-25T00:00:00Z"
_DATA_RESPONSES = frozenset({"200", "400", "404", "405", "422", "429", "500", "503"})
_RESPONSES = {
    CATALOG_PATH: frozenset({"200", "400", "405", "422", "429", "500", "503"}),
    DATASET_PATH: _DATA_RESPONSES,
    FACTORS_PATH: _DATA_RESPONSES,
    EXPORT_PATH: _DATA_RESPONSES,
}


def handle_openapi(
    method: str,
    target: str,
    *,
    load_document=None,
) -> tuple[int, dict[str, str], dict]:
    loader = load_approved if load_document is None else load_document
    path, query = _split_target(target)
    if path != OPENAPI_ROUTE:
        return 404, {}, _error("NOT_FOUND", "That path is not available.")
    if method != "GET":
        return 405, {"Allow": "GET"}, _error("METHOD_NOT_ALLOWED", "Only GET is available.")
    if _bad_escape(query):
        return 400, {}, _error("MALFORMED_QUERY", "The query string is malformed.")
    from urllib.parse import parse_qs

    parameters = parse_qs(query, keep_blank_values=True)
    if parameters:
        return (
            422,
            {},
            _error(
                "UNKNOWN_QUERY_PARAMETER",
                "Unknown query parameter.",
                {"parameter": sorted(parameters)[0]},
            ),
        )
    try:
        document = loader()
        if lint(document):
            return 500, {}, _error("INTERNAL", "The OpenAPI document is unavailable.")
        if not isinstance(document, dict):
            return 500, {}, _error("INTERNAL", "The OpenAPI document is unavailable.")
        return 200, {}, document
    except Exception:
        return 500, {}, _error("INTERNAL", "The OpenAPI document is unavailable.")


def load_approved() -> dict:
    document = yaml.safe_load(APPROVED_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("The OpenAPI document must be an object.")
    return document


def load_diffs() -> dict:
    document = json.loads(DIFFS_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("The OpenAPI diff record must be an object.")
    return document


def implementation_schema() -> dict:
    dictionary = json.loads(DICTIONARY_PATH.read_text(encoding="utf-8"))
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    release = {
        "dataset_id": "daily_external_factors_ph",
        "dataset_version": "1.0.0",
        "release_id": "00000000-0000-4000-8000-000000000300",
        "published_at": _STAMP,
        "coverage_start": "2018-01-01",
        "coverage_end": "2018-01-01",
    }
    catalog = catalog_payload([], dictionary, _STAMP)
    described = dictionary_payload(release, dictionary, registry, _STAMP)
    factors = _page(release, {"total": 0, "rows": []}, (), 1, 100, _STAMP)
    parameters = {
        CATALOG_PATH: {name: False for name in CATALOG_PARAMETERS},
        DATASET_PATH: {name: False for name in DICTIONARY_PARAMETERS},
        FACTORS_PATH: {name: name in _REQUIRED for name in _ALLOWED},
        EXPORT_PATH: dict(EXPORT_QUERY),
    }
    required = {
        CATALOG_PATH: set(catalog),
        DATASET_PATH: set(described),
        FACTORS_PATH: set(factors),
    }
    paths = {
        path: {
            "parameters": parameters[path],
            "responses": set(_RESPONSES[path]),
            "ok_required": required[path],
        }
        for path in (CATALOG_PATH, DATASET_PATH, FACTORS_PATH)
    }
    paths[EXPORT_PATH] = {
        "parameters": parameters[EXPORT_PATH],
        "responses": set(_RESPONSES[EXPORT_PATH]),
        "content_types": {"text/csv", "application/vnd.apache.parquet"},
    }
    return {"openapi": "3.1.0", "paths": paths}


def lint(document: dict) -> list[str]:
    problems: list[str] = []
    if document.get("openapi") != "3.1.0":
        problems.append("openapi_version")
    info = document.get("info")
    if not isinstance(info, dict) or not info.get("title") or not info.get("version"):
        problems.append("info")
    if not isinstance(document.get("servers"), list) or not document["servers"]:
        problems.append("servers")
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        return [*problems, "paths"]
    if OPENAPI_ROUTE in paths:
        problems.append("discovery_listed_as_data")
    seen: set[str] = set()
    for path, operations in paths.items():
        if not isinstance(path, str) or not path.startswith("/"):
            problems.append(f"path:{path}")
            continue
        if not isinstance(operations, dict) or set(operations) != {"get"}:
            problems.append(f"method:{path}")
            continue
        operation = operations["get"]
        operation_id = operation.get("operationId") if isinstance(operation, dict) else None
        if not isinstance(operation_id, str) or operation_id in seen:
            problems.append(f"operation_id:{path}")
        else:
            seen.add(operation_id)
        responses = operation.get("responses") if isinstance(operation, dict) else None
        if not isinstance(responses, dict) or not responses:
            problems.append(f"responses:{path}")
    problems.extend(_refs(document))
    problems.extend(_schema_required(document))
    return problems


def compare(implemented: dict, approved: dict, diffs: dict) -> list[str]:
    problems: list[str] = []
    missing = _records(diffs, "unimplemented_operations", ("method", "path"))
    gaps = _records(diffs, "response_gaps", ("method", "path", "status"))
    content_gaps = _records(diffs, "content_gaps", ("method", "path", "content_type"))
    approved_paths = approved.get("paths")
    if not isinstance(approved_paths, dict):
        return ["paths"]
    implemented_paths = implemented["paths"]
    for path, operation in approved_paths.items():
        if not isinstance(operation, dict) or "get" not in operation:
            problems.append(f"method:{path}")
            continue
        if path not in implemented_paths:
            if ("get", path) not in missing:
                problems.append(f"unimplemented:{path}")
            continue
        if ("get", path) in missing:
            problems.append(f"stale_unimplemented:{path}")
        problems.extend(
            _compare_path(
                path,
                implemented_paths[path],
                operation["get"],
                approved,
                gaps,
                content_gaps,
            )
        )
    for path in implemented_paths:
        if path not in approved_paths:
            problems.append(f"undocumented:{path}")
    for method, path in missing:
        if path not in approved_paths:
            problems.append(f"stale_unimplemented:{path}")
    for method, path, status in gaps:
        if path not in approved_paths:
            problems.append(f"stale_gap:{path}:{status}")
    for method, path, content_type in content_gaps:
        if path not in approved_paths:
            problems.append(f"stale_content:{path}:{content_type}")
    return problems


def check() -> list[str]:
    approved = load_approved()
    problems = lint(approved)
    problems.extend(compare(implementation_schema(), approved, load_diffs()))
    return problems


def main() -> int:
    problems = check()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("OF-030 OpenAPI contract ok")
    return 0


def _compare_path(
    path: str,
    implemented: dict,
    operation: dict,
    approved: dict,
    gaps: set,
    content_gaps: set,
) -> list[str]:
    problems: list[str] = []
    declared = _yaml_parameters(operation)
    if declared != implemented["parameters"]:
        problems.append(f"parameters:{path}")
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return [*problems, f"responses:{path}"]
    yaml_statuses = {str(status) for status in responses}
    allowed_gaps = {code for method, gap_path, code in gaps if gap_path == path and method == "get"}
    expected = yaml_statuses - allowed_gaps
    if expected != implemented["responses"]:
        problems.append(f"responses:{path}")
    for method, gap_path, status in gaps:
        if gap_path == path and method == "get" and status not in yaml_statuses:
            problems.append(f"stale_gap:{path}:{status}")
        if gap_path == path and method == "get" and status in implemented["responses"]:
            problems.append(f"resolved_gap:{path}:{status}")
    if "content_types" in implemented:
        problems.extend(_compare_content(path, implemented, responses.get("200"), content_gaps))
    else:
        schema = _json_schema(responses.get("200"))
        if schema is None or _required_keys(approved, schema) != implemented["ok_required"]:
            problems.append(f"schema:{path}")
    return problems


def _compare_content(
    path: str,
    implemented: dict,
    response: object,
    content_gaps: set,
) -> list[str]:
    problems: list[str] = []
    content = response.get("content") if isinstance(response, dict) else None
    yaml_types = set(content) if isinstance(content, dict) else set()
    allowed = {
        kind for method, gap_path, kind in content_gaps if gap_path == path and method == "get"
    }
    if yaml_types - allowed != implemented["content_types"]:
        problems.append(f"content:{path}")
    for method, gap_path, kind in content_gaps:
        if gap_path != path or method != "get":
            continue
        if kind not in yaml_types:
            problems.append(f"stale_content:{path}:{kind}")
        if kind in implemented["content_types"]:
            problems.append(f"resolved_content:{path}:{kind}")
    return problems


def _yaml_parameters(operation: dict) -> dict[str, bool]:
    parameters = operation.get("parameters") or []
    if not isinstance(parameters, list):
        return {}
    return {
        item["name"]: bool(item.get("required", False))
        for item in parameters
        if isinstance(item, dict) and "name" in item
    }


def _json_schema(response: object) -> dict | None:
    if not isinstance(response, dict):
        return None
    content = response.get("content")
    if not isinstance(content, dict):
        return None
    body = content.get("application/json")
    if not isinstance(body, dict) or not isinstance(body.get("schema"), dict):
        return None
    return body["schema"]


def _required_keys(document: dict, schema: dict) -> set[str]:
    resolved = _lookup(document, schema["$ref"]) if "$ref" in schema else schema
    if not isinstance(resolved, dict):
        return set()
    if "allOf" in resolved:
        required: set[str] = set()
        for part in resolved["allOf"]:
            if isinstance(part, dict):
                required |= _required_keys(document, part)
        return required
    return set(resolved.get("required") or [])


def _schema_required(document: dict) -> list[str]:
    schemas = document.get("components", {}).get("schemas", {})
    if not isinstance(schemas, dict):
        return ["components"]
    problems: list[str] = []
    for name in ("Error", "DailyFactor", "FactorPage", "ReleaseMetadata"):
        if name not in schemas:
            problems.append(f"schema_missing:{name}")
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    names = [field["name"] for field in contract["fields"]]
    factor = schemas.get("DailyFactor")
    if isinstance(factor, dict) and factor.get("required") != names:
        problems.append("daily_factor_fields")
    error = schemas.get("Error")
    if isinstance(error, dict):
        if error.get("required") != ["error"]:
            problems.append("error_envelope")
        inner = error.get("properties", {}).get("error", {})
        if inner.get("required") != ["code", "message", "trace_id"]:
            problems.append("error_trace")
    return problems


def _refs(document: dict) -> list[str]:
    problems: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and _lookup(document, ref) is None:
                problems.append(f"ref:{ref}")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)
    return problems


def _lookup(document: dict, ref: str) -> object | None:
    if not ref.startswith("#/"):
        return None
    cursor: object = document
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(cursor, dict) or part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def _records(diffs: dict, name: str, fields: tuple[str, ...]) -> set[tuple[str, ...]]:
    rows = diffs.get(name) or []
    if not isinstance(rows, list):
        return set()
    records = set()
    for row in rows:
        if isinstance(row, dict) and all(isinstance(row.get(field), str) for field in fields):
            records.add(tuple(row[field] for field in fields))
    return records


if __name__ == "__main__":
    raise SystemExit(main())
