from pathlib import Path

from ops.local_stack import COMPOSE_FILE, ENVIRONMENTS, ensure_secrets, runtime_dir

ROOT = Path(__file__).resolve().parents[1]


def test_environments_are_local_only() -> None:
    assert set(ENVIRONMENTS) == {"dev", "test", "staging"}
    ports = [settings["postgres_port"] for settings in ENVIRONMENTS.values()]
    assert len(set(ports)) == 3


def test_compose_requires_ssl_and_has_no_production_service() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    hba = (ROOT / "ops" / "compose" / "postgres" / "pg_hba.conf").read_text(encoding="utf-8")
    entrypoint = (ROOT / "ops" / "compose" / "postgres" / "entrypoint.sh").read_text(
        encoding="utf-8"
    )
    assert "ssl=on" in entrypoint
    assert "production" not in text
    assert "hostssl all all all scram-sha-256" in hba
    assert "hostnossl all all all reject" in hba


def test_init_creates_separate_roles_and_schemas() -> None:
    script = (ROOT / "ops" / "compose" / "postgres" / "init-roles.sh").read_text(encoding="utf-8")
    for schema in ("control", "staging", "core", "quality", "publish"):
        assert f"CREATE SCHEMA IF NOT EXISTS {schema};" in script
    for role in ("of_migrate", "of_ingest", "of_transform", "of_publish", "of_api"):
        assert f"CREATE ROLE {role} LOGIN" in script
    assert "GRANT USAGE ON SCHEMA publish TO of_api;" in script
    assert "GRANT USAGE, CREATE ON SCHEMA control TO of_ingest;" in script


def test_secrets_stay_under_the_gitignored_runtime_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("ops.local_stack.RUNTIME", tmp_path)
    path = ensure_secrets("dev")
    assert path.parent == runtime_dir("dev")
    assert "POSTGRES_PASSWORD=" in path.read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "ops/local/runtime/" in gitignore
