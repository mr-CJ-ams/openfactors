"""Local Docker environments for OF-007.

dev, test, and staging are separate Compose projects on this computer.
Production is intentionally absent while the cost cap is USD 0.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRILL_DATABASE = "of_restore_drill"
COMPOSE_FILE = ROOT / "ops" / "compose" / "docker-compose.yml"
RUNTIME = ROOT / "ops" / "local" / "runtime"

ENVIRONMENTS: dict[str, dict[str, int | str]] = {
    "dev": {
        # 55430 avoids the host PostgreSQL service, which already listens on 5432.
        "postgres_port": 55430,
        "minio_api_port": 9000,
        "minio_console_port": 9001,
        "database": "openfactors_dev",
        "bucket": "openfactors-dev-raw",
    },
    "test": {
        "postgres_port": 55432,
        "minio_api_port": 19000,
        "minio_console_port": 19001,
        "database": "openfactors_test",
        "bucket": "openfactors-test-raw",
    },
    "staging": {
        "postgres_port": 55433,
        "minio_api_port": 19002,
        "minio_console_port": 19003,
        "database": "openfactors_staging",
        "bucket": "openfactors-staging-raw",
    },
}

PASSWORD_KEYS = (
    "POSTGRES_PASSWORD",
    "OF_MIGRATE_PASSWORD",
    "OF_INGEST_PASSWORD",
    "OF_TRANSFORM_PASSWORD",
    "OF_PUBLISH_PASSWORD",
    "OF_API_PASSWORD",
    "MINIO_ROOT_PASSWORD",
)


def project_name(environment: str) -> str:
    require_environment(environment)
    return f"openfactors-{environment}"


def require_environment(environment: str) -> dict[str, int | str]:
    try:
        return ENVIRONMENTS[environment]
    except KeyError as exc:
        known = ", ".join(ENVIRONMENTS)
        raise SystemExit(f"Unknown environment {environment!r}. Choose one of: {known}.") from exc


def runtime_dir(environment: str) -> Path:
    return RUNTIME / environment


def secrets_file(environment: str) -> Path:
    return runtime_dir(environment) / "secrets.env"


def ensure_secrets(environment: str) -> Path:
    path = secrets_file(environment)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = require_environment(environment)
    lines = [
        f"COMPOSE_PROJECT_NAME={project_name(environment)}",
        "POSTGRES_USER=of_admin",
        f"POSTGRES_DB={settings['database']}",
        f"POSTGRES_PORT={settings['postgres_port']}",
        f"MINIO_API_PORT={settings['minio_api_port']}",
        f"MINIO_CONSOLE_PORT={settings['minio_console_port']}",
        "MINIO_ROOT_USER=ofminio",
        f"BUCKET={settings['bucket']}",
    ]
    lines.extend(f"{key}={secrets.token_hex(24)}" for key in PASSWORD_KEYS)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def load_secrets(environment: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ensure_secrets(environment).read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def docker_executable() -> str:
    found = shutil.which("docker")
    if found:
        return found
    candidates: list[Path] = []
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        candidates.append(
            Path(local_app) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
        )
    candidates.append(Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return "docker"


def docker_available() -> bool:
    try:
        result = subprocess.run(
            [docker_executable(), "info"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def compose_base(environment: str) -> list[str]:
    return [
        docker_executable(),
        "compose",
        "--project-name",
        project_name(environment),
        "--env-file",
        str(ensure_secrets(environment)),
        "--file",
        str(COMPOSE_FILE),
    ]


def run_compose(
    environment: str, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    if not docker_available():
        raise SystemExit(
            "Docker is not available. Install Docker Desktop, start it, and rerun this command. "
            "No database was started."
        )
    return subprocess.run(
        [*compose_base(environment), *args],
        cwd=ROOT,
        text=True,
        check=check,
    )


def up(environment: str) -> None:
    run_compose(environment, "up", "--detach", "--wait")
    settings = require_environment(environment)
    print(
        f"{environment} is up. Postgres localhost:{settings['postgres_port']} "
        f"database {settings['database']}. "
        f"Object storage https://localhost:{settings['minio_api_port']} "
        f"bucket {settings['bucket']}."
    )
    print(f"Passwords are in {secrets_file(environment)}")


def down(environment: str, *, volumes: bool) -> None:
    args = ["down"]
    if volumes:
        args.append("--volumes")
    run_compose(environment, *args)


def backup(environment: str) -> Path:
    destination = runtime_dir(environment) / "backups"
    destination.mkdir(parents=True, exist_ok=True)
    dump_path = destination / f"{environment}.dump"
    database = str(require_environment(environment)["database"])
    completed = subprocess.run(
        [
            *compose_base(environment),
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "--username",
            "of_admin",
            "--format",
            "custom",
            "--file",
            "/tmp/openfactors.dump",
            database,
        ],
        cwd=ROOT,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"pg_dump failed for {environment}.")
    copy = subprocess.run(
        [
            *compose_base(environment),
            "cp",
            "postgres:/tmp/openfactors.dump",
            str(dump_path),
        ],
        cwd=ROOT,
        text=True,
        check=False,
    )
    if copy.returncode != 0 or not dump_path.is_file() or dump_path.stat().st_size == 0:
        raise SystemExit(f"Backup file was not written for {environment}.")
    print(f"Wrote {dump_path}")
    return dump_path


def restore_isolated(environment: str, dump_path: Path) -> dict:
    """Load a backup into a throwaway database, check it, then remove that database.

    The source database is not replaced. The measured seconds are this drill's
    duration. They are not an approved recovery-time target.
    """
    source_database = str(require_environment(environment)["database"])
    if source_database == DRILL_DATABASE:
        raise SystemExit("Refusing to restore over the source database.")
    if not dump_path.is_file() or dump_path.stat().st_size == 0:
        raise SystemExit("Backup file is missing.")
    started = time.monotonic()
    try:
        copied = subprocess.run(
            [
                *compose_base(environment),
                "cp",
                str(dump_path),
                "postgres:/tmp/openfactors-restore.dump",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if copied.returncode != 0:
            raise SystemExit("Backup file was not copied into the database container.")
        loaded = _postgres(
            environment,
            "bash",
            "-lc",
            _load_drill_script(),
        )
        if loaded.returncode != 0:
            raise SystemExit(f"Isolated restore failed.\n{_safe_detail(loaded.stderr)}")
        evidence = _drill_evidence(environment, source_database)
    finally:
        _drop_drill(environment)
    evidence["measured_restore_seconds"] = round(time.monotonic() - started, 3)
    evidence["rpo_target_seconds"] = None
    evidence["rto_target_seconds"] = None
    evidence["drill_database_removed"] = _drill_removed(environment)
    return evidence


def _load_drill_script() -> str:
    return (
        "psql --username of_admin --dbname postgres --command "
        "\"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = 'of_restore_drill' AND pid <> pg_backend_pid();\" "
        "&& psql --username of_admin --dbname postgres --command "
        "'DROP DATABASE IF EXISTS of_restore_drill' "
        "&& psql --username of_admin --dbname postgres --command "
        "'CREATE DATABASE of_restore_drill' "
        "&& pg_restore --username of_admin --dbname of_restore_drill "
        "--no-owner --exit-on-error /tmp/openfactors-restore.dump"
    )


def _drill_evidence(environment: str, source_database: str) -> dict:
    source_count = _count_facts(environment)
    checked = _postgres(
        environment,
        "psql",
        "--username",
        "of_admin",
        "--dbname",
        DRILL_DATABASE,
        "--tuples-only",
        "--no-align",
        "--command",
        "SELECT current_database() || '|' || "
        "(SELECT count(*)::text FROM publish.daily_external_factors_ph) || '|' || "
        "(SELECT redistribution_status FROM control.source_registry "
        "WHERE source_id = 'open-meteo-era5') || '|' || "
        "CASE WHEN has_schema_privilege('of_api', 'publish', 'USAGE') "
        "THEN 'yes' ELSE 'no' END || '|' || "
        "CASE WHEN has_schema_privilege('of_api', 'control', 'USAGE') "
        "THEN 'yes' ELSE 'no' END;",
    )
    if checked.returncode != 0:
        raise SystemExit(f"Isolated restore could not be checked.\n{_safe_detail(checked.stderr)}")
    database, count, status, publish, control = _evidence_fields(checked.stdout)
    if database != DRILL_DATABASE or database == source_database:
        raise SystemExit("Isolated restore touched the source database.")
    if count != source_count:
        raise SystemExit("Isolated restore fact count does not match the backup.")
    if status != "approved" or publish != "yes" or control != "no":
        raise SystemExit("Isolated restore failed the schema privilege check.")
    return {
        "source_database": source_database,
        "restored_database": database,
        "source_database_replaced": False,
        "fact_count": int(count),
        "source_status": status,
        "api_can_use_publish": True,
        "api_can_use_control": False,
    }


def _count_facts(environment: str) -> str:
    from ops.migrate import psql

    counted = psql(
        environment,
        "of_admin",
        "SELECT count(*)::text FROM publish.daily_external_factors_ph;",
        tuples_only=True,
    )
    if counted.returncode != 0:
        raise SystemExit("Source fact count could not be read.")
    lines = [line.strip() for line in counted.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise SystemExit("Source fact count could not be read.")
    return lines[0]


def _drill_removed(environment: str) -> bool:
    from ops.migrate import psql

    counted = psql(
        environment,
        "of_admin",
        "SELECT count(*)::text FROM pg_database WHERE datname = 'of_restore_drill';",
        tuples_only=True,
    )
    lines = [line.strip() for line in counted.stdout.splitlines() if line.strip()]
    return counted.returncode == 0 and lines == ["0"]


def _drop_drill(environment: str) -> None:
    _postgres(
        environment,
        "bash",
        "-lc",
        "psql --username of_admin --dbname postgres --command "
        "\"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = 'of_restore_drill' AND pid <> pg_backend_pid();\" "
        "&& psql --username of_admin --dbname postgres --command "
        "'DROP DATABASE IF EXISTS of_restore_drill'",
    )


def _evidence_fields(stdout: str) -> tuple[str, str, str, str, str]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip() and "|" in line]
    if len(lines) != 1:
        raise SystemExit("Isolated restore check did not return one row.")
    fields = lines[0].split("|")
    if len(fields) != 5:
        raise SystemExit("Isolated restore check did not return one row.")
    return fields[0], fields[1], fields[2], fields[3], fields[4]


def _postgres(environment: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*compose_base(environment), "exec", "-T", "postgres", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _safe_detail(text: str) -> str:
    lines = [line for line in text.splitlines() if "PASSWORD" not in line.upper()]
    return "\n".join(lines)[-500:]


def _psql(
    environment: str, *, user: str, database: str, sslmode: str, sql: str
) -> subprocess.CompletedProcess[str]:
    secrets_map = load_secrets(environment)
    password_key = {
        "of_admin": "POSTGRES_PASSWORD",
        "of_api": "OF_API_PASSWORD",
    }[user]
    return subprocess.run(
        [
            *compose_base(environment),
            "exec",
            "-T",
            "--env",
            f"PGPASSWORD={secrets_map[password_key]}",
            "postgres",
            "psql",
            f"host=127.0.0.1 user={user} dbname={database} sslmode={sslmode}",
            "--no-password",
            "--tuples-only",
            "--command",
            sql,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def smoke() -> None:
    environment = "test"
    settings = require_environment(environment)
    database = str(settings["database"])
    try:
        up(environment)
        publish = _psql(
            environment,
            user="of_api",
            database=database,
            sslmode="require",
            sql="SELECT has_schema_privilege(current_user, 'publish', 'USAGE')",
        )
        control = _psql(
            environment,
            user="of_api",
            database=database,
            sslmode="require",
            sql="SELECT has_schema_privilege(current_user, 'control', 'USAGE')",
        )
        plaintext = _psql(
            environment,
            user="of_api",
            database=database,
            sslmode="disable",
            sql="SELECT 1",
        )
        if publish.returncode != 0 or "t" not in publish.stdout:
            raise SystemExit(f"API role cannot read the publish schema.\n{publish.stderr}")
        if control.returncode != 0 or "f" not in control.stdout:
            raise SystemExit(
                f"API role can use the control schema.\n{control.stdout}{control.stderr}"
            )
        if plaintext.returncode == 0:
            raise SystemExit("Postgres accepted a connection without SSL.")
        dump_path = backup(environment)
        restore = subprocess.run(
            [
                *compose_base(environment),
                "exec",
                "-T",
                "postgres",
                "bash",
                "-lc",
                "psql --username of_admin --dbname postgres "
                "--command 'CREATE DATABASE of_restore_check' "
                "&& pg_restore --username of_admin --dbname of_restore_check "
                "--no-owner --exit-on-error /tmp/openfactors.dump "
                "&& psql --username of_admin --dbname of_restore_check "
                "--tuples-only --command "
                '"SELECT schema_name FROM information_schema.schemata '
                "WHERE schema_name = 'publish'\"",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if restore.returncode != 0 or "publish" not in restore.stdout:
            raise SystemExit(
                f"pg_restore did not accept the backup.\n{restore.stdout}\n{restore.stderr}"
            )
        if dump_path.stat().st_size == 0:
            raise SystemExit("Backup file is empty.")
        print("Provisioning smoke passed for the test environment.")
    finally:
        if docker_available():
            down(environment, volumes=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local OpenFactors Postgres and object storage.")
    sub = parser.add_subparsers(dest="command", required=True)
    up_cmd = sub.add_parser("up")
    up_cmd.add_argument("environment", choices=tuple(ENVIRONMENTS))
    down_cmd = sub.add_parser("down")
    down_cmd.add_argument("environment", choices=tuple(ENVIRONMENTS))
    down_cmd.add_argument("--volumes", action="store_true")
    backup_cmd = sub.add_parser("backup")
    backup_cmd.add_argument("environment", choices=tuple(ENVIRONMENTS))
    restore_cmd = sub.add_parser("restore-check")
    restore_cmd.add_argument("environment", choices=tuple(ENVIRONMENTS))
    restore_cmd.add_argument("dump")
    sub.add_parser("smoke")
    args = parser.parse_args(argv)
    if args.command == "up":
        up(args.environment)
    elif args.command == "down":
        down(args.environment, volumes=args.volumes)
    elif args.command == "backup":
        backup(args.environment)
    elif args.command == "restore-check":
        print(json.dumps(restore_isolated(args.environment, Path(args.dump)), indent=2))
    elif args.command == "smoke":
        smoke()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    main(sys.argv[1:])
