"""Create a selective snapshot and restore it into an isolated candidate.

This command does not rename databases, close the API, or switch a live
serving database. Public Beta v0.1 stays no-go.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from contracts.check_dataset_contract import CONTRACT_PATH, load_json

from openfactors.quality.manifest import content_sha256
from ops.local_stack import ROOT, compose_base, require_environment, runtime_dir
from ops.migrate import sql_literal

DATASET_ID = "daily_external_factors_ph"
CANDIDATE_DATABASE = "openfactors_candidate"
SOURCE_DATABASES = frozenset({"openfactors_dev", "openfactors_test", "openfactors_staging"})
PROTECTED_DATABASES = frozenset(
    {
        "postgres",
        "template0",
        "template1",
        "openfactors_dev",
        "openfactors_test",
        "openfactors_staging",
        "openfactors_serving",
        "openfactors_previous",
        "openfactors_expired",
        "of_restore_drill",
    }
)
EXCLUDED_SCHEMAS = ("staging",)
EXCLUDED_TABLES = (
    "core.fact_daily_weather",
    "core.fact_public_holidays",
    "core.holiday_calendar_coverage",
    "control.raw_manifests",
    "control.ingestion_runs",
    "control.checkpoints",
)
REQUIRED_ROLES = ("of_migrate", "of_publish", "of_api")
LOGIN_ROLES = ("of_api",)
REQUIRED_OBJECTS = (
    "control.schema_migrations",
    "control.source_registry",
    "core.dim_date",
    "core.dim_location",
    "publish.active_dataset_release",
    "publish.accessible_factors",
    "publish.accessible_releases",
    "publish.daily_external_factors_ph",
    "publish.dataset_catalog",
    "publish.dataset_releases",
    "publish.daily_factors",
    "quality.validation_runs",
)
OWNERS = {
    "control.schema_migrations": "of_migrate",
    "control.source_registry": "of_migrate",
    "core.dim_date": "of_migrate",
    "core.dim_location": "of_migrate",
    "publish.active_dataset_release": "of_publish",
    "publish.accessible_factors": "of_publish",
    "publish.accessible_releases": "of_publish",
    "publish.daily_external_factors_ph": "of_publish",
    "publish.dataset_catalog": "of_publish",
    "publish.dataset_releases": "of_publish",
    "publish.daily_factors": "of_publish",
    "quality.validation_runs": "of_publish",
}
_ABSENT = (
    *EXCLUDED_TABLES,
    "staging.landed_payload",
)
_DENIED = (
    "SELECT 1 FROM control.source_registry;",
    "SELECT 1 FROM publish.daily_external_factors_ph;",
    "SELECT 1 FROM quality.validation_runs;",
    "SELECT 1 FROM core.dim_date;",
)


class SnapshotError(RuntimeError):
    """A snapshot could not be created or its candidate failed validation."""


def create_snapshot(environment: str, destination: Path) -> Path:
    """Dump the source database and write a manifest beside the dump."""
    source = _source_database(environment)
    destination.mkdir(parents=True, exist_ok=True)
    before = _published_identity(environment, source)
    dump_path = destination / f"{before['release_id']}.dump"
    manifest_path = dump_path.with_suffix(".json")
    if dump_path.exists() or manifest_path.exists():
        raise SnapshotError("The snapshot files already exist.")
    partial = dump_path.with_suffix(".dump.partial")
    container_dump = _container_dump()
    try:
        _dump(environment, container_dump)
        _copy_from_container(environment, container_dump, partial)
        after = _published_identity(environment, source)
        if after != before:
            raise SnapshotError("The published release changed while the snapshot was created.")
        digest = file_sha256(partial)
        partial.replace(dump_path)
        manifest = _manifest(environment, source, before, digest)
        manifest_path.write_text(_manifest_text(manifest), encoding="utf-8")
    except Exception:
        partial.unlink(missing_ok=True)
        dump_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise
    finally:
        _remove_container_dump(environment, container_dump)
    return dump_path


def restore_candidate(environment: str, dump_path: Path) -> dict[str, object]:
    """Restore a checked dump into openfactors_candidate and validate it."""
    _source_database(environment)
    _assert_candidate_target(CANDIDATE_DATABASE)
    if _database_exists(environment, CANDIDATE_DATABASE):
        raise SnapshotError("The candidate database already exists.")
    manifest = load_manifest(dump_path.with_suffix(".json"))
    verify_dump(dump_path, manifest)
    assert_cluster_roles(environment, REQUIRED_ROLES)
    container_dump = _container_dump()
    created = False
    try:
        _create_candidate(environment)
        created = True
        _copy_to_container(environment, dump_path, container_dump)
        restored = _pg_restore(environment, container_dump)
        if restored.returncode != 0:
            detail = (restored.stderr or restored.stdout or "").strip()
            raise SnapshotError(detail or "The candidate restore failed.")
        evidence = validate_candidate(environment, manifest)
    except Exception:
        if created:
            drop_database(environment, CANDIDATE_DATABASE)
        raise
    finally:
        _remove_container_dump(environment, container_dump)
    return evidence


def abort_candidate(environment: str) -> None:
    """Drop openfactors_candidate when it is present. No other name is accepted."""
    require_environment(environment)
    if _database_exists(environment, CANDIDATE_DATABASE):
        drop_database(environment, CANDIDATE_DATABASE)


def validate_candidate(environment: str, manifest: dict[str, object]) -> dict[str, object]:
    """Check the candidate copy. This does not write the source database."""
    _assert_candidate_target(CANDIDATE_DATABASE)
    current = _text(environment, CANDIDATE_DATABASE, "SELECT current_database();")
    if current != CANDIDATE_DATABASE:
        raise SnapshotError("Validation did not read the candidate database.")
    identity = _published_identity(environment, CANDIDATE_DATABASE)
    if identity["release_id"] != manifest["release_id"]:
        raise SnapshotError("The candidate release does not match the manifest.")
    if identity["dataset_version"] != manifest["dataset_version"]:
        raise SnapshotError("The candidate schema version does not match the manifest.")
    if identity["row_count"] != manifest["row_count"]:
        raise SnapshotError("The candidate row count does not match the manifest.")
    if identity["content_sha256_hex"] != manifest["content_sha256_hex"]:
        raise SnapshotError("The candidate content hash does not match the manifest.")
    if identity["dataset_version"] != _dataset_version():
        raise SnapshotError("The candidate schema version is not the public schema.")
    _require_objects(environment, CANDIDATE_DATABASE)
    _require_absent(environment, CANDIDATE_DATABASE)
    _require_owners(environment, CANDIDATE_DATABASE)
    _require_api_boundary(environment, CANDIDATE_DATABASE, identity["release_id"])
    return {
        "candidate_database": CANDIDATE_DATABASE,
        "release_id": identity["release_id"],
        "dataset_version": identity["dataset_version"],
        "row_count": identity["row_count"],
        "content_sha256_hex": identity["content_sha256_hex"],
    }


def load_manifest(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise SnapshotError("The snapshot manifest is missing.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SnapshotError("The snapshot manifest is not an object.")
    required = (
        "dataset_id",
        "release_id",
        "dataset_version",
        "row_count",
        "content_sha256_hex",
        "dump_sha256_hex",
        "dump_format",
        "source_database",
        "excluded_schemas",
        "excluded_tables",
    )
    for key in required:
        if key not in payload:
            raise SnapshotError("The snapshot manifest is missing a required field.")
    if payload["dataset_id"] != DATASET_ID or payload["dump_format"] != "custom":
        raise SnapshotError("The snapshot manifest does not describe this dataset.")
    if payload["dataset_version"] != _dataset_version():
        raise SnapshotError("The snapshot manifest schema version is not the public schema.")
    if payload["excluded_schemas"] != list(EXCLUDED_SCHEMAS):
        raise SnapshotError("The snapshot manifest exclude list does not match this command.")
    if payload["excluded_tables"] != list(EXCLUDED_TABLES):
        raise SnapshotError("The snapshot manifest exclude list does not match this command.")
    if payload["source_database"] not in SOURCE_DATABASES:
        raise SnapshotError("The snapshot manifest source database is not a known database.")
    if not isinstance(payload["row_count"], int) or isinstance(payload["row_count"], bool):
        raise SnapshotError("The snapshot manifest row count is not an integer.")
    if payload["row_count"] < 0:
        raise SnapshotError("The snapshot manifest row count is not an integer.")
    _hex64(str(payload["content_sha256_hex"]), "content hash")
    _hex64(str(payload["dump_sha256_hex"]), "dump checksum")
    try:
        uuid.UUID(str(payload["release_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SnapshotError("The snapshot manifest release id is not a UUID.") from exc
    return payload


def verify_dump(dump_path: Path, manifest: dict[str, object]) -> None:
    if not dump_path.is_file() or dump_path.stat().st_size == 0:
        raise SnapshotError("The snapshot dump is missing.")
    actual = file_sha256(dump_path)
    expected = str(manifest["dump_sha256_hex"])
    if not hmac.compare_digest(actual, expected):
        raise SnapshotError("The snapshot dump checksum does not match the manifest.")


def assert_cluster_roles(environment: str, required: tuple[str, ...]) -> None:
    found = _role_rows(environment)
    missing = [role for role in required if role not in found]
    if missing:
        raise SnapshotError("The cluster is missing a required role: " + ", ".join(missing))
    for role in LOGIN_ROLES:
        if role in required and found.get(role) != "login":
            raise SnapshotError("The API role cannot log in.")


def drop_database(environment: str, database: str) -> None:
    _assert_candidate_target(database)
    _admin(
        environment,
        "postgres",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = {sql_literal(database)} AND pid <> pg_backend_pid();",
    )
    _admin(
        environment,
        "postgres",
        f"DROP DATABASE IF EXISTS {database};",
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(
    environment: str,
    source: str,
    identity: dict[str, object],
    digest: str,
) -> dict[str, object]:
    version = _text(environment, source, "SHOW server_version;")
    return {
        "dataset_id": DATASET_ID,
        "release_id": identity["release_id"],
        "dataset_version": identity["dataset_version"],
        "row_count": identity["row_count"],
        "content_sha256_hex": identity["content_sha256_hex"],
        "dump_sha256_hex": digest,
        "dump_format": "custom",
        "source_database": source,
        "server_version": version,
        "excluded_schemas": list(EXCLUDED_SCHEMAS),
        "excluded_tables": list(EXCLUDED_TABLES),
    }


def _manifest_text(manifest: dict[str, object]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def _published_identity(environment: str, database: str) -> dict[str, object]:
    completed = _run(
        environment,
        database,
        """
        SELECT releases.release_id::text || '|' || releases.status || '|'
               || releases.actual_row_count::text || '|' || btrim(releases.content_sha256_hex)
               || '|' || btrim(releases.schema_version)
        FROM publish.active_dataset_release AS active
        JOIN publish.dataset_releases AS releases
          ON releases.release_id = active.release_id
        WHERE active.dataset_id = 'daily_external_factors_ph';
        """,
        user="of_admin",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise SnapshotError(detail or "The active release could not be read.")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise SnapshotError("There is no active published release.")
    if len(lines) != 1:
        raise SnapshotError("The snapshot query did not return one row.")
    release_id, status, row_count, content_hash, schema_version = lines[0].split("|")
    if status != "published":
        raise SnapshotError("The active release is not published.")
    rows = _fact_rows(environment, database, release_id)
    if len(rows) != int(row_count):
        raise SnapshotError("The fact row count does not match the release manifest.")
    recomputed = content_sha256(rows)
    if recomputed != content_hash:
        raise SnapshotError("The recomputed content hash does not match the stored hash.")
    if schema_version != _dataset_version():
        raise SnapshotError("The release schema version is not the public schema.")
    return {
        "release_id": release_id,
        "dataset_version": schema_version,
        "row_count": int(row_count),
        "content_sha256_hex": content_hash,
    }


def _fact_rows(environment: str, database: str, release_id: str) -> list[dict]:
    quoted = sql_literal(release_id)
    payload = _text(
        environment,
        database,
        f"""
        SELECT COALESCE(json_agg(row_to_json(candidate)), '[]'::json)::text
        FROM (
          SELECT date::text AS date,
                 location_id,
                 btrim(country_code) AS country_code,
                 temperature_mean_c::text AS temperature_mean_c,
                 precipitation_mm::text AS precipitation_mm,
                 is_public_holiday,
                 holiday_name,
                 is_weekend,
                 weather_source_id,
                 holiday_source_id,
                 source_updated_at::text AS source_updated_at,
                 dataset_version,
                 weather_data_status
          FROM publish.daily_external_factors_ph
          WHERE release_id = {quoted}::uuid
        ) AS candidate;
        """,
    )
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise SnapshotError("The fact rows could not be read.")
    return rows


def _require_objects(environment: str, database: str) -> None:
    for name in REQUIRED_OBJECTS:
        if _regclass(environment, database, name) != name:
            raise SnapshotError("The candidate is missing a required object.")


def _require_absent(environment: str, database: str) -> None:
    for name in _ABSENT:
        if _regclass(environment, database, name) != "absent":
            raise SnapshotError("The candidate contains an excluded object.")


def _require_owners(environment: str, database: str) -> None:
    for name, owner in OWNERS.items():
        found = _text(
            environment,
            database,
            "SELECT pg_get_userbyid(relowner) FROM pg_class "
            f"WHERE oid = {sql_literal(name)}::regclass;",
        )
        if found != owner:
            raise SnapshotError("The candidate object owner does not match the source role.")


def _require_api_boundary(environment: str, database: str, release_id: str) -> None:
    catalog = _text(
        environment,
        database,
        "SELECT active_release_id::text || '|' || dataset_version FROM publish.dataset_catalog;",
        user="of_api",
    )
    if catalog != f"{release_id}|{_dataset_version()}":
        raise SnapshotError("The API role cannot read the candidate catalog.")
    for sql in _DENIED:
        denied = _run(environment, database, sql, user="of_api")
        if denied.returncode == 0 or "permission denied" not in (denied.stderr or ""):
            raise SnapshotError("The API role can read a private relation.")


def _regclass(environment: str, database: str, name: str) -> str:
    return _text(
        environment,
        database,
        f"SELECT COALESCE(to_regclass({sql_literal(name)})::text, 'absent');",
    )


def _role_rows(environment: str) -> dict[str, str]:
    text = _admin(
        environment,
        "postgres",
        """
        SELECT rolname || '|' || CASE WHEN rolcanlogin THEN 'login' ELSE 'nologin' END
        FROM pg_roles
        WHERE rolname IN ('of_migrate', 'of_publish', 'of_api')
        ORDER BY 1;
        """,
    )
    found: dict[str, str] = {}
    for line in text.splitlines():
        if "|" not in line:
            continue
        name, login = line.split("|", 1)
        found[name] = login
    return found


def _database_exists(environment: str, database: str) -> bool:
    count = _admin(
        environment,
        "postgres",
        "SELECT count(*)::text FROM pg_database WHERE datname = " + sql_literal(database) + ";",
    ).strip()
    return count == "1"


def _create_candidate(environment: str) -> None:
    _assert_candidate_target(CANDIDATE_DATABASE)
    _admin(environment, "postgres", f"CREATE DATABASE {CANDIDATE_DATABASE};")


def _container_dump() -> str:
    return f"/tmp/openfactors-snapshot-{os.getpid()}.dump"


def _assert_candidate_target(database: str) -> None:
    if database != CANDIDATE_DATABASE or database in PROTECTED_DATABASES:
        raise SnapshotError("Restore and cleanup can use only the allow-listed candidate database.")


def _source_database(environment: str) -> str:
    database = _environment_database(environment)
    if database not in SOURCE_DATABASES or database in {CANDIDATE_DATABASE}:
        raise SnapshotError("The snapshot source is not a known database.")
    return database


def _dump(environment: str, container_dump: str) -> None:
    database = _source_database(environment)
    _remove_container_dump(environment, container_dump)
    command = [
        "exec",
        "-T",
        "postgres",
        "pg_dump",
        "--username",
        "of_admin",
        "--format",
        "custom",
        "--file",
        container_dump,
    ]
    for schema in EXCLUDED_SCHEMAS:
        command.append(f"--exclude-schema={schema}")
    for table in EXCLUDED_TABLES:
        command.append(f"--exclude-table={table}")
    command.append(database)
    completed = _run_compose(environment, *command)
    if completed.returncode != 0:
        raise SnapshotError((completed.stderr or "").strip() or "The snapshot dump failed.")


def _pg_restore(environment: str, container_dump: str) -> subprocess.CompletedProcess[str]:
    _assert_candidate_target(CANDIDATE_DATABASE)
    return _run_compose(
        environment,
        "exec",
        "-T",
        "postgres",
        "pg_restore",
        "--username",
        "of_admin",
        "--dbname",
        CANDIDATE_DATABASE,
        "--exit-on-error",
        container_dump,
    )


def _copy_from_container(environment: str, container_path: str, host_path: Path) -> None:
    completed = _run_compose(
        environment,
        "cp",
        f"postgres:{container_path}",
        str(host_path),
    )
    if completed.returncode != 0 or not host_path.is_file():
        raise SnapshotError("The snapshot dump was not copied out of the database container.")


def _copy_to_container(environment: str, host_path: Path, container_path: str) -> None:
    completed = _run_compose(
        environment,
        "cp",
        str(host_path),
        f"postgres:{container_path}",
    )
    if completed.returncode != 0:
        raise SnapshotError("The snapshot dump was not copied into the database container.")


def _remove_container_dump(environment: str, container_path: str) -> None:
    _run_compose(environment, "exec", "-T", "postgres", "rm", "-f", container_path)


def _run_compose(environment: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*compose_base(environment), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _dataset_version() -> str:
    version = str(load_json(CONTRACT_PATH)["dataset_version"])
    return version


def _environment_database(environment: str) -> str:
    return str(require_environment(environment)["database"])


def _hex64(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SnapshotError(f"The snapshot manifest {label} is not a SHA-256 hex digest.")


def _text(environment: str, database: str, sql: str, *, user: str = "of_admin") -> str:
    completed = _run(environment, database, sql, user=user)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise SnapshotError(detail or "The snapshot query failed.")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise SnapshotError("The snapshot query did not return one row.")
    return lines[0]


def _admin(environment: str, database: str, sql: str) -> str:
    completed = _run(environment, database, sql, user="of_admin")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise SnapshotError(detail or "The snapshot query failed.")
    return completed.stdout


def _run(
    environment: str,
    database: str,
    sql: str,
    *,
    user: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            *compose_base(environment),
            "exec",
            "-T",
            "postgres",
            "psql",
            "--username",
            user,
            "--dbname",
            database,
            "--no-align",
            "--tuples-only",
            "--set",
            "ON_ERROR_STOP=1",
            "--file",
            "-",
        ],
        cwd=ROOT,
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )


def _default_output(environment: str) -> Path:
    return runtime_dir(environment) / "publications"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Create a selective snapshot or restore it into the candidate database."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    create_cmd = sub.add_parser("create")
    create_cmd.add_argument("environment")
    create_cmd.add_argument("--output", type=Path)
    restore_cmd = sub.add_parser("restore-candidate")
    restore_cmd.add_argument("environment")
    restore_cmd.add_argument("dump", type=Path)
    abort_cmd = sub.add_parser("abort-candidate")
    abort_cmd.add_argument("environment")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            destination = args.output or _default_output(args.environment)
            print(create_snapshot(args.environment, destination))
        elif args.command == "restore-candidate":
            print(json.dumps(restore_candidate(args.environment, args.dump), indent=2))
        elif args.command == "abort-candidate":
            abort_candidate(args.environment)
            print("Candidate database removed.")
    except SnapshotError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main(sys.argv[1:])
