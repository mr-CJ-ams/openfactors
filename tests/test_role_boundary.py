from pathlib import Path

import pytest
from contracts.check_dataset_contract import CONTRACT_PATH, field_names, load_json
from ops.local_stack import COMPOSE_FILE, docker_available
from ops.migrate import MIGRATIONS, apply, psql

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "D1_OpenFactors_OpenAPI_v1.yaml"
WRITE_METHODS = ("post", "put", "patch", "delete")


def test_compose_binds_databases_to_localhost_and_keeps_the_bucket_private() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "127.0.0.1:${POSTGRES_PORT}:5432" in text
    assert "127.0.0.1:${MINIO_API_PORT}:9000" in text
    assert "127.0.0.1:${MINIO_CONSOLE_PORT}:9001" in text
    assert 'anonymous set none "local/$$BUCKET"' in text


def test_public_contract_has_no_write_route() -> None:
    methods = _openapi_methods(OPENAPI.read_text(encoding="utf-8"))
    assert methods
    assert set(methods) == {"get"}


def test_migration_grants_the_serving_view_and_nothing_else() -> None:
    text = (MIGRATIONS / "0003_api_role_boundary.sql").read_text(encoding="utf-8")
    assert "GRANT SELECT ON TABLE publish.daily_factors TO of_api;" in text
    assert "REVOKE ALL ON SCHEMA control, staging, core, quality FROM of_api;" in text
    assert "security_barrier = true" in text
    for name in field_names(load_json(CONTRACT_PATH)):
        assert name in text


def test_api_role_can_read_only_the_serving_view() -> None:
    if not docker_available():
        pytest.skip("Docker is required for the OF-010 permission check.")
    apply("dev")
    allowed = psql("dev", "of_api", "SELECT * FROM publish.daily_factors;")
    assert allowed.returncode == 0, allowed.stderr
    catalog = psql("dev", "of_api", "SELECT * FROM publish.dataset_catalog;")
    assert catalog.returncode == 0, catalog.stderr
    releases = psql("dev", "of_api", "SELECT * FROM publish.accessible_releases;")
    assert releases.returncode == 0, releases.stderr
    factors = psql("dev", "of_api", "SELECT * FROM publish.accessible_factors;")
    assert factors.returncode == 0, factors.stderr
    denied_targets = (
        "SELECT * FROM control.raw_manifests;",
        "SELECT * FROM control.source_registry;",
        "SELECT * FROM publish.daily_external_factors_ph;",
        "SELECT * FROM publish.dataset_releases;",
        "SELECT * FROM publish.active_dataset_release;",
        "SELECT * FROM quality.validation_runs;",
        "INSERT INTO publish.daily_factors DEFAULT VALUES;",
        "CREATE TABLE publish.api_should_not_create (id integer);",
    )
    for sql in denied_targets:
        denied = psql("dev", "of_api", sql)
        assert denied.returncode != 0, sql
        if sql.startswith("INSERT"):
            writable = "permission denied" in denied.stderr
            writable = writable or "cannot insert into view" in denied.stderr
            assert writable, denied.stderr
        else:
            assert "permission denied" in denied.stderr
    privileges = psql(
        "dev",
        "of_admin",
        """
        SELECT has_table_privilege('of_api', 'publish.daily_factors', 'SELECT'),
               has_table_privilege('of_api', 'publish.daily_factors', 'INSERT'),
               has_table_privilege('of_api', 'publish.daily_factors', 'UPDATE'),
               has_table_privilege('of_api', 'publish.daily_factors', 'DELETE'),
               has_schema_privilege('of_api', 'control', 'USAGE'),
               has_schema_privilege('of_api', 'staging', 'USAGE'),
               has_schema_privilege('of_api', 'core', 'USAGE'),
               has_schema_privilege('of_api', 'quality', 'USAGE'),
               has_schema_privilege('of_api', 'publish', 'USAGE');
        """,
        tuples_only=True,
    )
    assert privileges.returncode == 0, privileges.stderr
    assert privileges.stdout.strip() == "t|f|f|f|f|f|f|f|t"


def _openapi_methods(text: str) -> list[str]:
    methods: list[str] = []
    in_paths = False
    for line in text.splitlines():
        if line.startswith("paths:"):
            in_paths = True
            continue
        if in_paths and line.startswith("components:"):
            break
        if not in_paths or not line.startswith("    ") or line.startswith("      "):
            continue
        stripped = line.strip()
        if stripped.endswith(":"):
            methods.append(stripped[:-1])
    return methods
