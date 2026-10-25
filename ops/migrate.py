"""Apply versioned Postgres migrations for the local stack.

OF-008 creates the control-plane tables and loads the approved source baseline.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from ops.local_stack import (
    compose_base,
    docker_available,
    load_secrets,
    require_environment,
)
from ops.source_registry_seed import (
    RETENTION_FIXTURE,
    ROOT,
    SOURCE_FIXTURE,
    load_json,
    seed_sql,
)

MIGRATIONS = ROOT / "ops" / "migrations"
ROLE_PASSWORDS = {
    "of_admin": "POSTGRES_PASSWORD",
    "of_migrate": "OF_MIGRATE_PASSWORD",
    "of_ingest": "OF_INGEST_PASSWORD",
    "of_api": "OF_API_PASSWORD",
}


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def psql(environment: str, user: str, sql: str, *, tuples_only: bool = False):
    require_environment(environment)
    if not docker_available():
        raise SystemExit(
            "Docker is not available. Install Docker Desktop, start it, and rerun this command."
        )
    secrets_map = load_secrets(environment)
    database = str(require_environment(environment)["database"])
    password = secrets_map[ROLE_PASSWORDS[user]]
    command = [
        *compose_base(environment),
        "exec",
        "-T",
        "--env",
        f"PGPASSWORD={password}",
        "postgres",
        "psql",
        f"host=127.0.0.1 user={user} dbname={database} sslmode=require",
        "--no-password",
        "-v",
        "ON_ERROR_STOP=1",
        "-f",
        "-",
    ]
    if tuples_only:
        flag_at = command.index("-f")
        command[flag_at:flag_at] = ["--tuples-only", "--no-align"]
    return subprocess.run(
        command,
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )


def _applied_versions(environment: str) -> set[str]:
    probe = psql(
        environment,
        "of_admin",
        "SELECT to_regclass('control.schema_migrations') IS NOT NULL;",
        tuples_only=True,
    )
    if probe.returncode != 0:
        raise SystemExit(probe.stderr.strip() or "Could not inspect schema_migrations.")
    if probe.stdout.strip() != "t":
        return set()
    listed = psql(
        environment,
        "of_admin",
        "SELECT version FROM control.schema_migrations ORDER BY version;",
        tuples_only=True,
    )
    if listed.returncode != 0:
        raise SystemExit(listed.stderr.strip() or "Could not read schema_migrations.")
    return {line for line in listed.stdout.splitlines() if line}


def _apply_sql(environment: str, version: str, body: str) -> None:
    wrapped = (
        "BEGIN;\n"
        f"{body.rstrip()}\n"
        "INSERT INTO control.schema_migrations (version) "
        f"VALUES ({sql_literal(version)});\n"
        "COMMIT;\n"
    )
    completed = psql(environment, "of_admin", wrapped)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"Migration {version} failed.\n{detail}")


def apply(environment: str) -> list[str]:
    require_environment(environment)
    extension = psql(environment, "of_admin", "CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    if extension.returncode != 0:
        detail = (extension.stderr or extension.stdout).strip()
        raise SystemExit(f"Could not enable pgcrypto.\n{detail}")
    applied = _applied_versions(environment)
    fresh: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in applied:
            continue
        _apply_sql(environment, path.name, path.read_text(encoding="utf-8"))
        fresh.append(path.name)
    seed_version = "0002_source_registry_seed.sql"
    if seed_version not in applied:
        registry = load_json(SOURCE_FIXTURE)
        retention = load_json(RETENTION_FIXTURE)
        _apply_sql(environment, seed_version, seed_sql(registry, retention))
        fresh.append(seed_version)
    from ops.dimensions import sync

    sync(environment)
    if fresh:
        print("Applied " + ", ".join(fresh))
    else:
        print(f"Migrations already applied on {environment}.")
    return fresh


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Apply local OpenFactors database migrations.")
    parser.add_argument("environment", choices=("dev", "test", "staging"))
    args = parser.parse_args(argv)
    apply(args.environment)


if __name__ == "__main__":
    main(sys.argv[1:])
