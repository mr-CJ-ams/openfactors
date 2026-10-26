"""The public API reads through psql as of_api, without Compose exec."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from ops.api_read import (
    ApiDatabase,
    ApiReadError,
    bind_address,
    export_directory,
    load_database,
    read_sql,
    run_query,
)
from ops.local_stack import compose_base, dev_database_ready, runtime_dir
from ops.migrate import apply, psql

from openfactors.api.catalog import main
from openfactors.api.export_limits import MAX_EXPORT_BYTES, MAX_EXPORT_ROWS
from openfactors.api.factors import load_factor_page

ROOT = Path(__file__).resolve().parents[1]
_API_ENV = (
    "OPENFACTORS_API_PGHOST",
    "OPENFACTORS_API_PGPORT",
    "OPENFACTORS_API_PGDATABASE",
    "OPENFACTORS_API_PGUSER",
    "OPENFACTORS_API_PGPASSWORD",
    "OPENFACTORS_API_PGSSLMODE",
    "OPENFACTORS_API_PGSSLROOTCERT",
    "OPENFACTORS_API_PSQL",
    "OPENFACTORS_API_BIND_HOST",
    "OPENFACTORS_API_BIND_PORT",
    "OPENFACTORS_API_EXPORT_DIR",
)
_PASSWORD = "example-api-password"
_CATALOG_SQL = """
SELECT COALESCE((
  SELECT json_build_object(
    'release_id', active_release_id,
    'dataset_version', dataset_version
  )
  FROM publish.dataset_catalog
), 'null'::json)::text;
"""
_DENIED = (
    "SELECT * FROM control.raw_manifests;",
    "SELECT * FROM control.source_registry;",
    "SELECT * FROM staging.landed_payload;",
    "SELECT * FROM publish.daily_external_factors_ph;",
    "SELECT * FROM publish.dataset_releases;",
    "SELECT * FROM publish.active_dataset_release;",
    "SELECT * FROM quality.validation_runs;",
)


@pytest.fixture(autouse=True)
def _clear_api_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _API_ENV:
        monkeypatch.delenv(name, raising=False)


def test_default_bind_is_loopback_and_an_explicit_host_is_accepted() -> None:
    assert bind_address() == ("127.0.0.1", 8000)
    assert bind_address("0.0.0.0", 9) == ("0.0.0.0", 9)
    with pytest.raises(ValueError, match="empty"):
        bind_address("  ", 8000)


def test_bind_environment_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENFACTORS_API_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("OPENFACTORS_API_BIND_PORT", "8000")
    assert bind_address() == ("0.0.0.0", 8000)
    monkeypatch.setenv("OPENFACTORS_API_BIND_HOST", " ")
    with pytest.raises(ValueError, match="empty"):
        bind_address()


def test_catalog_command_rejects_arguments_and_keeps_the_local_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit, match="no arguments"):
        main(["--host", "0.0.0.0"])
    monkeypatch.setattr("ops.local_stack.dev_database_ready", lambda: False)
    with pytest.raises(SystemExit, match="not running"):
        main([])


def test_explicit_host_does_not_require_local_docker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert = tmp_path / "ca.crt"
    cert.write_text("not checked until a query\n", encoding="utf-8")
    monkeypatch.setenv("OPENFACTORS_API_PGHOST", "db.example")
    monkeypatch.setenv("OPENFACTORS_API_PGPORT", "5432")
    monkeypatch.setenv("OPENFACTORS_API_PGDATABASE", "openfactors")
    monkeypatch.setenv("OPENFACTORS_API_PGPASSWORD", _PASSWORD)
    monkeypatch.setenv("OPENFACTORS_API_PGSSLMODE", "verify-full")
    monkeypatch.setenv("OPENFACTORS_API_PGSSLROOTCERT", str(cert))

    def fail_if_docker_is_required() -> bool:
        raise AssertionError("docker should not be required")

    monkeypatch.setattr("ops.local_stack.dev_database_ready", fail_if_docker_is_required)
    seen: dict[str, object] = {}

    def serve(environment: str, host: str, port: int) -> None:
        seen["environment"] = environment
        seen["host"] = host
        seen["port"] = port

    monkeypatch.setattr("openfactors.api.catalog.serve_catalog", serve)
    assert main([]) == 0
    assert seen == {"environment": "dev", "host": "127.0.0.1", "port": 8000}
    assert _PASSWORD not in str(seen)


def test_api_user_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENFACTORS_API_PGUSER", "of_admin")
    with pytest.raises(ApiReadError, match="of_api") as caught:
        load_database("dev")
    assert "of_admin" not in str(caught.value)


def test_plaintext_and_weaker_tls_modes_do_not_open_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("psql should not start")

    monkeypatch.setattr("ops.api_read.subprocess.run", fail)
    for mode in ("disable", "allow", "prefer", "verify-ca", ""):
        if mode:
            monkeypatch.setenv("OPENFACTORS_API_PGSSLMODE", mode)
        else:
            monkeypatch.delenv("OPENFACTORS_API_PGSSLMODE", raising=False)
        if mode in {"", "require"}:
            continue
        with pytest.raises(ApiReadError, match="sslmode"):
            load_database("dev")


def test_hosted_connection_requires_verify_full(monkeypatch: pytest.MonkeyPatch) -> None:
    _hosted_env(monkeypatch, sslmode="require")
    with pytest.raises(ApiReadError, match="local-only") as caught:
        load_database("dev")
    assert _PASSWORD not in str(caught.value)
    monkeypatch.setenv("OPENFACTORS_API_PGSSLMODE", "disable")
    with pytest.raises(ApiReadError, match="verify-full") as caught:
        load_database("dev")
    assert _PASSWORD not in str(caught.value)


def test_verify_full_requires_a_ca_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _hosted_env(monkeypatch, sslmode="verify-full")
    with pytest.raises(ApiReadError, match="PGSSLROOTCERT"):
        load_database("dev")
    missing = tmp_path / "missing.crt"
    monkeypatch.setenv("OPENFACTORS_API_PGSSLROOTCERT", str(missing))
    with pytest.raises(ApiReadError, match="PGSSLROOTCERT"):
        load_database("dev")


def test_password_stays_out_of_the_psql_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    database = ApiDatabase(
        host="127.0.0.1",
        port=55430,
        database="openfactors_dev",
        user="of_api",
        password=_PASSWORD,
        sslmode="require",
        sslrootcert=None,
    )
    captured: dict[str, object] = {}

    class FakeProcess:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            env = kwargs["env"]
            assert isinstance(env, dict)
            password_file = Path(str(env["PGPASSFILE"]))
            captured["command"] = command
            captured["env"] = env
            captured["shell"] = kwargs["shell"]
            captured["pgpass"] = password_file.read_text(encoding="utf-8")
            self.returncode: int | None = None

        def communicate(
            self, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            captured["input"] = input
            captured["timeout"] = timeout
            self.returncode = 0
            return "", ""

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("ops.api_read.subprocess.Popen", FakeProcess)
    monkeypatch.setenv("PGPASSWORD", _PASSWORD)
    monkeypatch.setenv("PGSSLMODE", "disable")
    run_query(
        database,
        "SELECT :'location_id';",
        variables={"location_id": "ph-manila' OR '1'='1"},
        integers={"page_size": 100, "row_offset": 0},
        tuples_only=True,
    )
    command = captured["command"]
    assert isinstance(command, list)
    rendered = " ".join(str(part) for part in command)
    assert _PASSWORD not in rendered
    assert "PGPASSWORD=" not in rendered
    assert captured["shell"] is False
    env = captured["env"]
    assert isinstance(env, dict)
    assert env.get("PGPASSWORD") is None
    assert env.get("OPENFACTORS_API_PGPASSWORD") is None
    assert env["PGSSLMODE"] == "require"
    assert "PGPASSFILE" in env
    assert not Path(str(env["PGPASSFILE"])).exists()
    assert any(part == "location_id=ph-manila' OR '1'='1" for part in command)
    assert captured["timeout"] == 10
    assert "ph-manila" not in str(captured["input"])
    assert _PASSWORD in str(captured["pgpass"])
    assert "docker" not in rendered
    assert "compose" not in rendered


def test_factor_values_stay_in_psql_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_read(
        _environment: str,
        sql: str,
        *,
        variables: dict[str, str] | None = None,
        integers: dict[str, int] | None = None,
        tuples_only: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        captured["sql"] = sql
        captured["variables"] = variables
        captured["integers"] = integers
        captured["tuples_only"] = tuples_only
        return subprocess.CompletedProcess([], 0, stdout='{"total": 0, "rows": []}', stderr="")

    monkeypatch.setattr("openfactors.api.factors.read_sql", fake_read)
    load_factor_page(
        "dev",
        release_id="00000000-0000-4000-8000-000000000001",
        location_id="ph-manila' OR '1'='1",
        start_date="2018-01-01",
        end_date="2018-01-01",
        page=1,
        page_size=100,
    )
    sql = str(captured["sql"])
    variables = captured["variables"]
    assert isinstance(variables, dict)
    assert "ph-manila" not in sql
    assert variables["location_id"] == "ph-manila' OR '1'='1"
    assert ":'release_id'" in sql
    assert "LIMIT :page_size" in sql
    assert captured["integers"] == {"page_size": 100, "row_offset": 0}


def test_export_directory_is_configurable_and_caps_stay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = "00000000-0000-4000-8000-000000000001"
    assert export_directory("dev", release) == runtime_dir("dev") / "exports" / release
    monkeypatch.setenv("OPENFACTORS_API_EXPORT_DIR", str(tmp_path))
    assert export_directory("dev", release) == tmp_path / release
    with pytest.raises(ValueError):
        export_directory("dev", "../secrets")
    assert MAX_EXPORT_ROWS == 100_000
    assert MAX_EXPORT_BYTES == 50_000_000


def test_api_read_source_does_not_call_compose() -> None:
    text = (ROOT / "ops" / "api_read.py").read_text(encoding="utf-8")
    admin = (ROOT / "ops" / "migrate.py").read_text(encoding="utf-8")
    assert "docker" not in text
    assert "compose_base" not in text
    assert "compose_base" in admin
    assert '"exec"' in admin


def test_api_read_matches_the_compose_catalog_row() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required for the API read comparison.")
    apply("dev")
    direct = read_sql("dev", _CATALOG_SQL, tuples_only=True)
    compose = psql("dev", "of_api", _CATALOG_SQL, tuples_only=True)
    assert direct.returncode == 0, direct.stderr
    assert compose.returncode == 0, compose.stderr
    assert json.loads(direct.stdout) == json.loads(compose.stdout)
    probed = read_sql(
        "dev",
        "SELECT :'location_id', :page_size;",
        variables={"location_id": "ph-manila"},
        integers={"page_size": 100},
        tuples_only=True,
    )
    assert probed.returncode == 0, probed.stderr
    assert [part.strip() for part in probed.stdout.split("|")] == ["ph-manila", "100"]


def test_api_read_cannot_read_raw_or_admin_tables() -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required for the API permission check.")
    apply("dev")
    identity = read_sql("dev", "SELECT current_user;", tuples_only=True)
    assert identity.returncode == 0, identity.stderr
    assert identity.stdout.strip() == "of_api"
    allowed = read_sql("dev", "SELECT * FROM publish.dataset_catalog;", tuples_only=True)
    assert allowed.returncode == 0, allowed.stderr
    for sql in _DENIED:
        denied = read_sql("dev", sql, tuples_only=True)
        assert denied.returncode != 0, sql
        assert "permission denied" in denied.stderr


def test_api_read_verify_full_accepts_the_local_ca(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required for the verify-full check.")
    cert = _server_certificate(tmp_path)
    monkeypatch.setenv("OPENFACTORS_API_PGSSLMODE", "verify-full")
    monkeypatch.setenv("OPENFACTORS_API_PGSSLROOTCERT", str(cert))
    completed = read_sql("dev", "SELECT current_user;", tuples_only=True)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "of_api"


def test_api_read_verify_full_rejects_an_untrusted_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if not dev_database_ready():
        pytest.skip("Docker is required for the untrusted CA check.")
    bad = tmp_path / "bad.crt"
    bad.write_text("not a certificate\n", encoding="utf-8")
    monkeypatch.setenv("OPENFACTORS_API_PGSSLMODE", "verify-full")
    monkeypatch.setenv("OPENFACTORS_API_PGSSLROOTCERT", str(bad))
    completed = read_sql("dev", "SELECT current_user;", tuples_only=True)
    assert completed.returncode != 0


def _hosted_env(monkeypatch: pytest.MonkeyPatch, *, sslmode: str) -> None:
    monkeypatch.setenv("OPENFACTORS_API_PGHOST", "db.example")
    monkeypatch.setenv("OPENFACTORS_API_PGPORT", "5432")
    monkeypatch.setenv("OPENFACTORS_API_PGDATABASE", "openfactors")
    monkeypatch.setenv("OPENFACTORS_API_PGPASSWORD", _PASSWORD)
    monkeypatch.setenv("OPENFACTORS_API_PGSSLMODE", sslmode)


def _server_certificate(tmp_path: Path) -> Path:
    completed = subprocess.run(
        [*compose_base("dev"), "exec", "-T", "postgres", "cat", "/certs/server.crt"],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert b"BEGIN CERTIFICATE" in completed.stdout
    path = tmp_path / "server.crt"
    path.write_bytes(completed.stdout)
    return path
