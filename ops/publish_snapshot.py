"""Create a selective snapshot, restore a candidate, and cut it over.

Promotion renames openfactors_candidate to openfactors_serving and keeps the
previous generation. Cleanup of an expired or demoted generation is a separate
command. A publisher key can sign the manifest. Local restore does not require
that signature yet. This does not deploy a host. Public Beta v0.1 stays no-go.
"""

from __future__ import annotations

import argparse
import contextvars
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from contracts.check_dataset_contract import CONTRACT_PATH, load_json

from openfactors.quality.manifest import content_sha256
from ops.local_stack import ROOT, compose_base, require_environment, runtime_dir
from ops.migrate import sql_literal

DATASET_ID = "daily_external_factors_ph"
SIGNATURE_PRINCIPAL = "openfactors-publisher"
SIGNATURE_NAMESPACE = "openfactors-snapshot"
_SIGNATURE_TIMEOUT_SECONDS = 30
CANDIDATE_DATABASE = "openfactors_candidate"
SERVING_DATABASE = "openfactors_serving"
PREVIOUS_DATABASE = "openfactors_previous"
EXPIRED_DATABASE = "openfactors_expired"
CUTOVER_DATABASES = (
    CANDIDATE_DATABASE,
    SERVING_DATABASE,
    PREVIOUS_DATABASE,
    EXPIRED_DATABASE,
)
CUTOVER_CLUSTERS = frozenset({"test", "staging"})
CLEANUP_TARGETS = frozenset({CANDIDATE_DATABASE, EXPIRED_DATABASE})
_DROP_SQL = {
    EXPIRED_DATABASE: "DROP DATABASE openfactors_expired;",
    CANDIDATE_DATABASE: "DROP DATABASE openfactors_candidate;",
}
_DEMOTED_MARK = "openfactors-demoted"
_ALLOWED_AFTER = frozenset(
    {
        f"COMMENT ON DATABASE {CANDIDATE_DATABASE} IS {sql_literal(_DEMOTED_MARK)};",
        f"COMMENT ON DATABASE {SERVING_DATABASE} IS NULL;",
    }
)
_GENERATION_LOCK_CLASS = 481516234
_GENERATION_LOCK_OBJECT = 20261027
_LOST_CONNECTION = (
    "connection to server was lost",
    "server closed the connection unexpectedly",
    "could not receive data from server",
    "could not send data to server",
    "ssl connection has been closed unexpectedly",
)
_RENAME_EDGES = frozenset(
    {
        (CANDIDATE_DATABASE, SERVING_DATABASE),
        (SERVING_DATABASE, PREVIOUS_DATABASE),
        (PREVIOUS_DATABASE, EXPIRED_DATABASE),
        (SERVING_DATABASE, CANDIDATE_DATABASE),
        (PREVIOUS_DATABASE, SERVING_DATABASE),
        (EXPIRED_DATABASE, PREVIOUS_DATABASE),
    }
)
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
        with _generation_lock(environment):
            if _database_exists(environment, CANDIDATE_DATABASE):
                raise SnapshotError("The candidate database already exists.")
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
    with _generation_lock(environment):
        if _database_exists(environment, CANDIDATE_DATABASE):
            drop_database(environment, CANDIDATE_DATABASE)


def cleanup_generation(
    environment: str,
    target: str,
    confirm_release: str,
    *,
    dry_run: bool = False,
) -> dict[str, object]:
    """Drop one confirmed obsolete generation.

    The target must be openfactors_expired or a demoted openfactors_candidate.
    Promotion and rollback do not call this. A dry run does not drop anything.
    """
    _assert_cleanup_target(target)
    _confirm_release(confirm_release)
    if environment not in CUTOVER_CLUSTERS:
        raise SnapshotError("Cleanup runs only on the test or staging cluster.")
    with _generation_lock(environment):
        return _cleanup_generation_locked(
            environment,
            target,
            confirm_release,
            dry_run=dry_run,
        )


def _cleanup_generation_locked(
    environment: str,
    target: str,
    confirm_release: str,
    *,
    dry_run: bool,
) -> dict[str, object]:
    report = _cleanup_report(environment, target, confirm_release)
    if dry_run or not report["allowed"]:
        if not dry_run:
            raise SnapshotError(str(report["reason"]))
        return report
    confirmed = _cleanup_report(environment, target, confirm_release)
    if not confirmed["allowed"] or confirmed["release_id"] != report["release_id"]:
        raise SnapshotError(str(confirmed["reason"]))
    _drop_obsolete_generation(environment, target)
    report["dropped"] = True
    report["reason"] = "The obsolete generation was dropped."
    return report


def promote_candidate(
    environment: str,
    confirm_release: str,
    *,
    abort_after_rename: bool = False,
) -> dict[str, object]:
    """Rename a validated candidate into openfactors_serving.

    The confirmation must be the candidate release id. An existing serving
    database is preserved as openfactors_previous. This does not drop a
    previous or expired generation.
    """
    release_id = _confirm_release(confirm_release)
    _assert_cutover_cluster(environment)
    with _generation_lock(environment):
        return _promote_candidate_locked(
            environment,
            release_id,
            abort_after_rename=abort_after_rename,
        )


def _promote_candidate_locked(
    environment: str,
    release_id: str,
    *,
    abort_after_rename: bool,
) -> dict[str, object]:
    assert_cluster_roles(environment, REQUIRED_ROLES)
    catalog = _cutover_catalog(environment)
    if catalog[CANDIDATE_DATABASE] is None:
        return _finish_promoted_serving(environment, release_id, catalog)
    _refuse_demoted_candidate(environment)
    plan = _promotion_plan(catalog)
    evidence = _revalidate_database(environment, CANDIDATE_DATABASE, release_id)
    if catalog[SERVING_DATABASE] is not None:
        if catalog[SERVING_DATABASE] == "open":
            _set_connections(environment, SERVING_DATABASE, allow=False)
        _terminate_database(environment, SERVING_DATABASE)
    _rename_transaction(environment, plan)
    try:
        if abort_after_rename:
            raise SnapshotError("Post-cutover validation failed.")
        _set_connections(environment, SERVING_DATABASE, allow=True)
        promoted = _revalidate_database(environment, SERVING_DATABASE, release_id)
        after = _cutover_catalog(environment)
        if after[SERVING_DATABASE] != "open":
            raise SnapshotError("The promoted serving database is not accepting connections.")
        if after[PREVIOUS_DATABASE] not in {None, "closed"}:
            raise SnapshotError("The previous serving database is accepting connections.")
        if promoted["content_sha256_hex"] != evidence["content_sha256_hex"]:
            raise SnapshotError("The promoted content hash does not match the candidate.")
    except Exception as exc:
        _undo_promotion(environment, plan, exc)
        raise
    return _evidence(SERVING_DATABASE, promoted)


def rollback_serving(
    environment: str,
    confirm_release: str,
    *,
    abort_after_rename: bool = False,
) -> dict[str, object]:
    """Restore openfactors_previous to openfactors_serving.

    The confirmation must be the previous generation's release id. The current
    serving generation is preserved as openfactors_candidate. Expired generations
    are left in place.
    """
    release_id = _confirm_release(confirm_release)
    _assert_cutover_cluster(environment)
    with _generation_lock(environment):
        return _rollback_serving_locked(
            environment,
            release_id,
            abort_after_rename=abort_after_rename,
        )


def _rollback_serving_locked(
    environment: str,
    release_id: str,
    *,
    abort_after_rename: bool,
) -> dict[str, object]:
    assert_cluster_roles(environment, REQUIRED_ROLES)
    catalog = _cutover_catalog(environment)
    if catalog[PREVIOUS_DATABASE] is None:
        return _finish_restored_serving(environment, release_id, catalog)
    if catalog[SERVING_DATABASE] is None or catalog[CANDIDATE_DATABASE] is not None:
        raise SnapshotError("The cutover databases are in an unexpected state.")
    if catalog[PREVIOUS_DATABASE] == "open":
        _set_connections(environment, PREVIOUS_DATABASE, allow=False)
    _revalidate_previous(environment, release_id)
    if catalog[SERVING_DATABASE] == "open":
        _set_connections(environment, SERVING_DATABASE, allow=False)
    _terminate_database(environment, SERVING_DATABASE)
    plan = [
        (SERVING_DATABASE, CANDIDATE_DATABASE),
        (PREVIOUS_DATABASE, SERVING_DATABASE),
    ]
    _rename_transaction(
        environment,
        plan,
        after=(f"COMMENT ON DATABASE {CANDIDATE_DATABASE} IS {sql_literal(_DEMOTED_MARK)};",),
    )
    try:
        if abort_after_rename:
            raise SnapshotError("Post-cutover validation failed.")
        _set_connections(environment, SERVING_DATABASE, allow=True)
        _set_connections(environment, CANDIDATE_DATABASE, allow=True)
        restored = _revalidate_database(environment, SERVING_DATABASE, release_id)
        after = _cutover_catalog(environment)
        if after[SERVING_DATABASE] != "open" or after[CANDIDATE_DATABASE] != "open":
            raise SnapshotError("Rollback left a generation closed.")
        if after[PREVIOUS_DATABASE] is not None:
            raise SnapshotError("Rollback left a previous generation in place.")
    except Exception as exc:
        _undo_rollback(environment, plan, exc)
        raise
    return _evidence(SERVING_DATABASE, restored)


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


def signature_path_for(manifest_path: Path) -> Path:
    """Return the sibling signature path. ssh-keygen appends .sig to the file name."""
    return manifest_path.parent / f"{manifest_path.name}.sig"


def sign_manifest(manifest_path: Path, key_path: Path) -> Path:
    """Sign the manifest bytes with the publisher key. The key is not read here."""
    manifest_path = Path(manifest_path)
    key_path = Path(key_path)
    if not manifest_path.is_file() or manifest_path.stat().st_size == 0:
        raise SnapshotError("The snapshot manifest is missing.")
    if not key_path.is_file() or key_path.stat().st_size == 0:
        raise SnapshotError("The publisher signing key is missing.")
    _assert_signing_key_location(key_path)
    _assert_signing_key_file(key_path)
    destination = signature_path_for(manifest_path)
    if destination.exists():
        raise SnapshotError("The snapshot signature already exists.")
    manifest_bytes = manifest_path.read_bytes()
    with tempfile.TemporaryDirectory(prefix="openfactors-sign-") as temporary:
        copied = Path(temporary) / manifest_path.name
        copied.write_bytes(manifest_bytes)
        completed = _run_keygen(
            [
                _ssh_keygen(),
                "-Y",
                "sign",
                "-f",
                str(key_path),
                "-n",
                SIGNATURE_NAMESPACE,
                str(copied),
            ],
            stdin=None,
            failure="The snapshot signature could not be written.",
        )
        produced = signature_path_for(copied)
        if completed.returncode != 0 or not produced.is_file():
            raise SnapshotError("The publisher signing key could not be used.")
        payload = produced.read_bytes()
    if (
        b"-----BEGIN SSH SIGNATURE-----" not in payload
        or b"-----END SSH SIGNATURE-----" not in payload
    ):
        raise SnapshotError("The snapshot signature could not be written.")
    _publish_new_signature(destination, payload)
    return destination


def verify_manifest(
    manifest_path: Path,
    signature_path: Path,
    allowed_signers_path: Path,
) -> None:
    """Verify the manifest bytes. Identity and the signers file are local pins.

    The principal, namespace, and allowed-signers path are not read from the
    manifest. A failed check does not create a database.
    """
    manifest_path = Path(manifest_path)
    signature_path = Path(signature_path)
    allowed_signers_path = Path(allowed_signers_path)
    if not manifest_path.is_file() or manifest_path.stat().st_size == 0:
        raise SnapshotError("The snapshot manifest is missing.")
    _require_signature_file(signature_path)
    _require_allowed_signers(allowed_signers_path)
    manifest_bytes = manifest_path.read_bytes()
    completed = _run_keygen(
        [
            _ssh_keygen(),
            "-Y",
            "verify",
            "-f",
            str(allowed_signers_path),
            "-I",
            SIGNATURE_PRINCIPAL,
            "-n",
            SIGNATURE_NAMESPACE,
            "-s",
            str(signature_path),
        ],
        stdin=manifest_bytes,
        failure="Signature verification did not finish.",
    )
    if completed.returncode != 0:
        raise SnapshotError("The snapshot signature was rejected.")
    stdout = completed.stdout.decode("utf-8", errors="replace").replace("\r", "")
    expected = f'Good "{SIGNATURE_NAMESPACE}" signature for {SIGNATURE_PRINCIPAL} with ED25519 key '
    if expected not in stdout:
        raise SnapshotError("The snapshot signature was rejected.")


def _assert_signing_key_file(key_path: Path) -> None:
    """Refuse a symlink or, on POSIX, a key other accounts can read.

    Windows does not expose Unix mode bits, so the mode check runs on POSIX.
    An unencrypted key is still readable by the development account.
    """
    if key_path.is_symlink():
        raise SnapshotError("The publisher signing key must be a regular file.")
    if not _private_key_mode_applies():
        return
    if key_path.stat().st_mode & 0o077:
        raise SnapshotError("The publisher signing key is readable by other accounts.")


def _private_key_mode_applies() -> bool:
    return os.name == "posix"


def _publish_new_signature(destination: Path, payload: bytes) -> None:
    """Publish a complete signature without replacing an existing file.

    The temporary file is in the destination directory. A crash can leave that
    partial name. It is not the signature. The final name appears only after
    the complete bytes are in place.
    """
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.link(temporary, destination)
        else:
            os.rename(temporary, destination)
    except FileExistsError as exc:
        raise SnapshotError("The snapshot signature already exists.") from exc
    except OSError as exc:
        raise SnapshotError("The snapshot signature could not be written.") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _assert_signing_key_location(key_path: Path) -> None:
    resolved = key_path.resolve()
    root = ROOT.resolve()
    runtime = (root / "ops" / "local" / "runtime").resolve()
    try:
        resolved.relative_to(runtime)
        return
    except ValueError:
        pass
    try:
        resolved.relative_to(root)
    except ValueError:
        return
    raise SnapshotError("The publisher signing key must stay outside Git.")


def _require_signature_file(signature_path: Path) -> None:
    if not signature_path.is_file() or signature_path.stat().st_size == 0:
        raise SnapshotError("The snapshot signature is missing.")
    try:
        payload = signature_path.read_bytes()
    except OSError as exc:
        raise SnapshotError("The snapshot signature is malformed.") from exc
    if (
        b"-----BEGIN SSH SIGNATURE-----" not in payload
        or b"-----END SSH SIGNATURE-----" not in payload
    ):
        raise SnapshotError("The snapshot signature is malformed.")


def _require_allowed_signers(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise SnapshotError("The allowed signers file is missing.")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotError("The allowed signers file cannot be read.") from exc
    except UnicodeError as exc:
        raise SnapshotError("The allowed signers file is invalid.") from exc
    principal = SIGNATURE_PRINCIPAL
    namespace = f'namespaces="{SIGNATURE_NAMESPACE}"'
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if not fields or fields[0] != principal:
            continue
        if namespace in line and "ssh-ed25519 " in line:
            return
    raise SnapshotError("The allowed signers file is invalid.")


def _ssh_keygen() -> str:
    found = shutil.which("ssh-keygen")
    if not found:
        raise SnapshotError("ssh-keygen is not available.")
    return found


def _run_keygen(
    args: list[str],
    *,
    stdin: bytes | None,
    failure: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        if stdin is None:
            return subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_SIGNATURE_TIMEOUT_SECONDS,
                check=False,
            )
        return subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            timeout=_SIGNATURE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SnapshotError(failure) from exc
    except FileNotFoundError as exc:
        raise SnapshotError("ssh-keygen is not available.") from exc
    except OSError as exc:
        raise SnapshotError("ssh-keygen is not available.") from exc


def assert_cluster_roles(environment: str, required: tuple[str, ...]) -> None:
    found = _role_rows(environment)
    missing = [role for role in required if role not in found]
    if missing:
        raise SnapshotError("The cluster is missing a required role: " + ", ".join(missing))
    for role in LOGIN_ROLES:
        if role in required and found.get(role) != "login":
            raise SnapshotError("The API role cannot log in.")


def drop_database(environment: str, database: str) -> None:
    _guard_generation_lock(environment)
    _assert_candidate_target(database)
    if _database_comment(environment, database) == _DEMOTED_MARK:
        raise SnapshotError("The candidate is a preserved serving generation and is not dropped.")
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
    _guard_generation_lock(environment)
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


_active_generation_lock: contextvars.ContextVar[_generation_lock | None] = contextvars.ContextVar(
    "_active_generation_lock",
    default=None,
)


class _generation_lock:
    """Hold the cluster generation lock until this command finishes.

    The lock lives in its own session. That session stays open through
    DROP DATABASE and the renames. Closing it is what releases the lock.
    """

    def __init__(self, environment: str) -> None:
        self.environment = environment
        self._process: subprocess.Popen[str] | None = None
        self._pid: int | None = None
        self._token: contextvars.Token[_generation_lock | None] | None = None

    def __enter__(self) -> _generation_lock:
        self._process = subprocess.Popen(
            [
                *compose_base(self.environment),
                "exec",
                "-T",
                "postgres",
                "stdbuf",
                "-oL",
                "-eL",
                "psql",
                "--username",
                "of_admin",
                "--dbname",
                "postgres",
                "--no-align",
                "--tuples-only",
                "--set",
                "ON_ERROR_STOP=1",
            ],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert self._process.stdin is not None
        self._process.stdin.write(
            "SELECT CASE WHEN pg_try_advisory_lock("
            f"{_GENERATION_LOCK_CLASS}, {_GENERATION_LOCK_OBJECT}"
            ") THEN 'locked' ELSE 'busy' END;\n"
        )
        self._process.stdin.flush()
        line = _read_process_line(self._process)
        if line.strip() != "locked":
            busy = line.strip() == "busy"
            self._close()
            if busy:
                raise SnapshotError("Another generation command is running.")
            raise SnapshotError("The generation lock could not be acquired.")
        assert self._process.stdin is not None
        self._process.stdin.write("SELECT pg_backend_pid();\n")
        self._process.stdin.flush()
        pid_line = _read_process_line(self._process).strip()
        if not pid_line.isdigit():
            self._close()
            raise SnapshotError("The generation lock could not be acquired.")
        self._pid = int(pid_line)
        self._token = _active_generation_lock.set(self)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._token is not None:
            _active_generation_lock.reset(self._token)
            self._token = None
        self._close()

    def lose_session(self) -> None:
        """Drop the lock-holding backend. Tests use this to simulate a disconnect."""
        if self._pid is not None:
            _admin(
                self.environment,
                "postgres",
                f"SELECT pg_terminate_backend({self._pid});",
            )
        self._close()

    def session_holds_lock(self) -> bool:
        if self._process is None or self._pid is None or self._process.poll() is not None:
            return False
        count = _admin(
            self.environment,
            "postgres",
            "SELECT count(*)::text FROM pg_locks "
            "WHERE locktype = 'advisory' AND granted "
            f"AND classid = {_GENERATION_LOCK_CLASS} "
            f"AND objid = {_GENERATION_LOCK_OBJECT} "
            f"AND pid = {self._pid};",
        ).strip()
        return count == "1"

    def run_while_held(self, sql: str, *, marker: str) -> None:
        """Run one statement on the lock session, then require a marker row.

        A disconnected session cannot execute the statement. The marker is a
        following SELECT, so a command tag is not treated as success.
        """
        if not marker.isascii() or not marker.isalnum():
            raise SnapshotError("Cleanup refuses this database statement.")
        if (
            self._process is None
            or self._process.stdin is None
            or self._process.poll() is not None
            or not self.session_holds_lock()
        ):
            raise SnapshotError(
                "The generation lock was lost. Inspect pg_database before retrying."
            )
        statement = sql if sql.endswith("\n") else f"{sql}\n"
        try:
            self._process.stdin.write(f"{statement}SELECT '{marker}';\n")
            self._process.stdin.flush()
        except OSError as exc:
            raise SnapshotError(
                "The generation lock was lost. Inspect pg_database before retrying."
            ) from exc
        for _line_number in range(5):
            line = _read_process_line(
                self._process,
                timeout=30,
                failure="The drop was not confirmed. Inspect pg_database before retrying.",
            ).strip()
            if line == marker:
                return
            if self._process.poll() is not None:
                break
        raise SnapshotError("The drop was not confirmed. Inspect pg_database before retrying.")

    def _close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _guard_generation_lock(environment: str, *, required: bool = False) -> None:
    """Stop a generation command whose lock session has disconnected.

    Calls made outside a generation command are unchanged unless the caller
    requires the lock. Manual SQL does not hold this lock.
    """
    lock = _active_generation_lock.get()
    if lock is None:
        if required:
            raise SnapshotError(
                "The generation lock was lost. Inspect pg_database before retrying."
            )
        return
    if lock.environment != environment or not lock.session_holds_lock():
        raise SnapshotError("The generation lock was lost. Inspect pg_database before retrying.")


def _read_process_line(
    process: subprocess.Popen[str],
    timeout: float = 10,
    failure: str = "The generation lock could not be acquired.",
) -> str:
    if process.stdout is None:
        return ""
    box: dict[str, str] = {}

    def read() -> None:
        box["line"] = process.stdout.readline() if process.stdout is not None else ""

    thread = threading.Thread(target=read)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        process.kill()
        raise SnapshotError(failure)
    return box.get("line", "")


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


def _promotion_plan(catalog: dict[str, str | None]) -> list[tuple[str, str]]:
    if catalog[CANDIDATE_DATABASE] is None:
        raise SnapshotError("The candidate database is missing.")
    if catalog[EXPIRED_DATABASE] is not None:
        raise SnapshotError("An expired generation is still present.")
    if catalog[SERVING_DATABASE] is None and catalog[PREVIOUS_DATABASE] is not None:
        raise SnapshotError("The cutover databases are in an unexpected state.")
    if catalog[CANDIDATE_DATABASE] != "open":
        raise SnapshotError("The candidate database is not accepting connections.")
    if catalog[PREVIOUS_DATABASE] not in {None, "closed"}:
        raise SnapshotError("The previous serving database is accepting connections.")
    plan: list[tuple[str, str]] = []
    if catalog[PREVIOUS_DATABASE] is not None:
        plan.append((PREVIOUS_DATABASE, EXPIRED_DATABASE))
    if catalog[SERVING_DATABASE] is not None:
        plan.append((SERVING_DATABASE, PREVIOUS_DATABASE))
    plan.append((CANDIDATE_DATABASE, SERVING_DATABASE))
    return plan


def _confirm_release(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SnapshotError("The confirmation is not a release id.") from exc


def _assert_cutover_cluster(environment: str) -> None:
    if environment not in CUTOVER_CLUSTERS:
        raise SnapshotError("Cutover runs only on the test or staging cluster.")
    source = _source_database(environment)
    if source not in {"openfactors_test", "openfactors_staging"} or source in CUTOVER_DATABASES:
        raise SnapshotError("Cutover refuses this source database.")


def _assert_cutover_name(database: str) -> None:
    if database not in CUTOVER_DATABASES:
        raise SnapshotError("Cutover refuses this database name.")


def _cutover_catalog(environment: str) -> dict[str, str | None]:
    listed = ", ".join(sql_literal(name) for name in CUTOVER_DATABASES)
    text = _admin(
        environment,
        "postgres",
        "SELECT datname || '|' || CASE WHEN datallowconn THEN 'open' ELSE 'closed' END "
        f"FROM pg_database WHERE datname IN ({listed}) ORDER BY 1;",
    )
    found: dict[str, str | None] = {name: None for name in CUTOVER_DATABASES}
    for line in text.splitlines():
        if "|" not in line:
            continue
        name, state = line.strip().split("|", 1)
        if name in found:
            found[name] = state
    return found


def _revalidate_database(
    environment: str,
    database: str,
    release_id: str,
) -> dict[str, object]:
    if database not in {CANDIDATE_DATABASE, SERVING_DATABASE, PREVIOUS_DATABASE}:
        raise SnapshotError("Validation can read only a candidate or serving generation.")
    current = _text(environment, database, "SELECT current_database();")
    if current != database:
        raise SnapshotError("Validation did not read the intended database.")
    identity = _published_identity(environment, database)
    if identity["release_id"] != release_id:
        raise SnapshotError("The confirmation does not match the database release.")
    _require_objects(environment, database)
    _require_absent(environment, database)
    _require_owners(environment, database)
    _require_api_boundary(environment, database, str(identity["release_id"]))
    return identity


def _revalidate_previous(environment: str, release_id: str) -> None:
    _set_connections(environment, PREVIOUS_DATABASE, allow=True)
    try:
        _revalidate_database(environment, PREVIOUS_DATABASE, release_id)
    except Exception:
        _set_connections(environment, PREVIOUS_DATABASE, allow=False)
        raise
    _set_connections(environment, PREVIOUS_DATABASE, allow=False)


def _finish_promoted_serving(
    environment: str,
    release_id: str,
    catalog: dict[str, str | None],
) -> dict[str, object]:
    if catalog[SERVING_DATABASE] is None or catalog[CANDIDATE_DATABASE] is not None:
        raise SnapshotError("The candidate database is missing.")
    if catalog[PREVIOUS_DATABASE] not in {None, "closed"}:
        raise SnapshotError("The previous serving database is accepting connections.")
    opened = False
    if catalog[SERVING_DATABASE] != "open":
        _set_connections(environment, SERVING_DATABASE, allow=True)
        opened = True
    try:
        identity = _revalidate_database(environment, SERVING_DATABASE, release_id)
    except Exception:
        if opened:
            _set_connections(environment, SERVING_DATABASE, allow=False)
        raise
    return _evidence(SERVING_DATABASE, identity)


def _finish_restored_serving(
    environment: str,
    release_id: str,
    catalog: dict[str, str | None],
) -> dict[str, object]:
    if (
        catalog[SERVING_DATABASE] is None
        or catalog[CANDIDATE_DATABASE] is None
        or catalog[PREVIOUS_DATABASE] is not None
    ):
        raise SnapshotError("There is no previous serving database to restore.")
    if _database_comment(environment, CANDIDATE_DATABASE) != _DEMOTED_MARK:
        raise SnapshotError("There is no previous serving database to restore.")
    opened_serving = catalog[SERVING_DATABASE] != "open"
    opened_candidate = catalog[CANDIDATE_DATABASE] != "open"
    if opened_serving:
        _set_connections(environment, SERVING_DATABASE, allow=True)
    if opened_candidate:
        _set_connections(environment, CANDIDATE_DATABASE, allow=True)
    try:
        identity = _revalidate_database(environment, SERVING_DATABASE, release_id)
        preserved = _published_identity(environment, CANDIDATE_DATABASE)
        if preserved["release_id"] == identity["release_id"]:
            raise SnapshotError("Rollback did not preserve a distinct serving generation.")
    except Exception:
        if opened_serving:
            _set_connections(environment, SERVING_DATABASE, allow=False)
        if opened_candidate:
            _set_connections(environment, CANDIDATE_DATABASE, allow=False)
        raise
    return _evidence(SERVING_DATABASE, identity)


def _set_connections(environment: str, database: str, *, allow: bool) -> None:
    _guard_generation_lock(environment)
    _assert_cutover_name(database)
    flag = "true" if allow else "false"
    _admin(
        environment,
        "postgres",
        f"ALTER DATABASE {database} WITH ALLOW_CONNECTIONS {flag};",
    )


def _terminate_database(environment: str, database: str) -> None:
    _guard_generation_lock(environment)
    _assert_cutover_name(database)
    for _attempt in range(3):
        _admin(
            environment,
            "postgres",
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = {sql_literal(database)} AND pid <> pg_backend_pid();",
        )
        count = _admin(
            environment,
            "postgres",
            "SELECT count(*)::text FROM pg_stat_activity "
            f"WHERE datname = {sql_literal(database)} AND pid <> pg_backend_pid();",
        ).strip()
        if count == "0":
            return
    raise SnapshotError("A session is still using the database.")


def _rename_transaction(
    environment: str,
    plan: list[tuple[str, str]],
    *,
    after: tuple[str, ...] = (),
    fail_before_commit: bool = False,
) -> None:
    """Rename in one transaction. fail_before_commit is a test seam, not a command."""
    if not plan:
        raise SnapshotError("The cutover databases are in an unexpected state.")
    for statement in after:
        if statement not in _ALLOWED_AFTER:
            raise SnapshotError("Cutover refuses this database statement.")
    statements = ["SET lock_timeout = '3s';", "BEGIN;"]
    for source, destination in plan:
        if (source, destination) not in _RENAME_EDGES:
            raise SnapshotError("Cutover refuses this database name.")
        statements.append(f"ALTER DATABASE {source} RENAME TO {destination};")
    statements.extend(after)
    if fail_before_commit:
        statements.append("SELECT 1/0;")
    statements.append("COMMIT;")
    _guard_generation_lock(environment)
    _admin(environment, "postgres", "\n".join(statements))


def _reverse_rename(
    environment: str,
    plan: list[tuple[str, str]],
    *,
    after: tuple[str, ...] = (),
    fail_before_commit: bool = False,
) -> None:
    for _source, destination in plan:
        _terminate_database(environment, destination)
    reverse = [(destination, source) for source, destination in reversed(plan)]
    _rename_transaction(
        environment,
        reverse,
        after=after,
        fail_before_commit=fail_before_commit,
    )


def _undo_promotion(
    environment: str,
    plan: list[tuple[str, str]],
    exc: Exception,
    *,
    fail_before_commit: bool = False,
) -> None:
    """Compensate a rename that has already committed. This is a new transaction."""
    try:
        _reverse_rename(environment, plan, fail_before_commit=fail_before_commit)
        if any(source == SERVING_DATABASE for source, _destination in plan):
            _set_connections(environment, SERVING_DATABASE, allow=True)
        else:
            _set_connections(environment, CANDIDATE_DATABASE, allow=True)
    except Exception as undo_error:
        raise SnapshotError(_compensation_message(undo_error)) from exc


def _undo_rollback(environment: str, plan: list[tuple[str, str]], exc: Exception) -> None:
    """Compensate a rollback rename that has already committed."""
    try:
        _reverse_rename(
            environment,
            plan,
            after=(f"COMMENT ON DATABASE {SERVING_DATABASE} IS NULL;",),
        )
        _set_connections(environment, SERVING_DATABASE, allow=True)
        _set_connections(environment, PREVIOUS_DATABASE, allow=False)
    except Exception as undo_error:
        raise SnapshotError(_compensation_message(undo_error)) from exc


def _compensation_message(undo_error: Exception) -> str:
    """Separate a rolled-back statement error from an unconfirmed commit."""
    detail = str(undo_error).strip()
    lowered = detail.lower()
    lost = any(phrase in lowered for phrase in _LOST_CONNECTION)
    if lost or "error:" not in lowered:
        return (
            "The compensating transaction commit was not confirmed. "
            "Inspect pg_database before retrying. " + detail
        )
    return (
        "The compensating transaction failed before commit and was rolled back. "
        "Inspect pg_database before retrying. " + detail
    )


def _database_comment(environment: str, database: str) -> str:
    _assert_cutover_name(database)
    return _admin(
        environment,
        "postgres",
        "SELECT COALESCE(shobj_description(oid, 'pg_database'), '') "
        f"FROM pg_database WHERE datname = {sql_literal(database)};",
    ).strip()


def _refuse_demoted_candidate(environment: str) -> None:
    comment = _database_comment(environment, CANDIDATE_DATABASE)
    if comment == _DEMOTED_MARK:
        raise SnapshotError(
            "The candidate is a preserved serving generation, not a new publication."
        )
    if comment:
        raise SnapshotError("The candidate database has an unexpected catalog comment.")


def _evidence(database: str, identity: dict[str, object]) -> dict[str, object]:
    return {
        "database": database,
        "release_id": identity["release_id"],
        "dataset_version": identity["dataset_version"],
        "row_count": identity["row_count"],
        "content_sha256_hex": identity["content_sha256_hex"],
    }


def _cleanup_report(environment: str, target: str, confirm_release: str) -> dict[str, object]:
    """Decide whether one generation may be dropped. This does not drop it."""
    _assert_cleanup_target(target)
    release_id = _confirm_release(confirm_release)
    if environment not in CUTOVER_CLUSTERS:
        raise SnapshotError("Cleanup runs only on the test or staging cluster.")
    catalog = _cutover_catalog(environment)
    state = catalog[target]
    connections = _target_connections(environment, target) if state is not None else 0
    report: dict[str, object] = {
        "target": target,
        "release_id": None,
        "state": state or "absent",
        "connections": connections,
        "serving_state": catalog[SERVING_DATABASE] or "absent",
        "previous_state": catalog[PREVIOUS_DATABASE] or "absent",
        "allowed": False,
        "dropped": False,
        "reason": "",
    }
    if state is None:
        report["reason"] = "The target database is absent."
        return report
    if connections != 0:
        report["reason"] = "A session is using the target database."
        return report
    if target == EXPIRED_DATABASE and state != "closed":
        report["reason"] = "The expired generation is accepting connections."
        return report
    if target == CANDIDATE_DATABASE and _database_comment(environment, target) != _DEMOTED_MARK:
        report["reason"] = "The candidate is not a demoted serving generation."
        return report
    if target == CANDIDATE_DATABASE and catalog[PREVIOUS_DATABASE] is None:
        report["reason"] = "The demoted candidate is the only remaining copy of its release."
        return report
    if target == EXPIRED_DATABASE and _database_comment(environment, target):
        report["reason"] = "The expired generation has an unexpected catalog comment."
        return report
    if catalog[SERVING_DATABASE] is None:
        report["reason"] = "The serving database is missing."
        return report
    if catalog[SERVING_DATABASE] != "open":
        report["reason"] = "The serving database is not accepting connections."
        return report
    if catalog[PREVIOUS_DATABASE] == "open":
        report["reason"] = "The previous serving database is accepting connections."
        return report
    try:
        serving = _published_identity(environment, SERVING_DATABASE)
    except SnapshotError as exc:
        report["reason"] = str(exc)
        return report
    if _run(environment, SERVING_DATABASE, "SELECT 1;", user="of_api").returncode != 0:
        report["reason"] = "The serving database is not accepting API connections."
        return report
    try:
        identity = _read_cleanup_identity(environment, target, state)
    except SnapshotError as exc:
        report["reason"] = str(exc)
        return report
    report["release_id"] = identity["release_id"]
    if identity["release_id"] != release_id:
        report["reason"] = "The confirmation does not match the database release."
        return report
    if identity["release_id"] == serving["release_id"]:
        report["reason"] = "The target release is still the serving release."
        return report
    if _target_connections(environment, target) != 0:
        report["reason"] = "A session is using the target database."
        return report
    if target == EXPIRED_DATABASE:
        report["reason"] = (
            "The expired generation is closed, its release matches, "
            "and the serving database is a different published release."
        )
    else:
        report["reason"] = (
            "The candidate is a demoted generation, its release matches, "
            "and the serving database is a different published release."
        )
    report["allowed"] = True
    return report


def _read_cleanup_identity(
    environment: str,
    database: str,
    state: str,
    *,
    fail_reclose: bool = False,
) -> dict[str, object]:
    """Read a closed target, then restore its closed state.

    fail_reclose is a test seam. It leaves the target open and reports how to close it.
    """
    _assert_cleanup_target(database)
    opened = state == "closed"
    if opened:
        _set_connections(environment, database, allow=True)
    try:
        return _published_identity(environment, database)
    finally:
        if opened:
            _reclose_cleanup_target(environment, database, fail=fail_reclose)


def _reclose_cleanup_target(environment: str, database: str, *, fail: bool) -> None:
    if fail:
        raise SnapshotError(
            "The target was opened for inspection and could not be closed. "
            "Inspect pg_database and set ALLOW_CONNECTIONS false before retrying."
        )
    try:
        _set_connections(environment, database, allow=False)
    except SnapshotError as exc:
        raise SnapshotError(
            "The target was opened for inspection and could not be closed. "
            "Inspect pg_database and set ALLOW_CONNECTIONS false before retrying. " + str(exc)
        ) from exc


def _drop_obsolete_generation(environment: str, target: str) -> None:
    """Drop one allow-listed generation. Sessions are not terminated."""
    _assert_cleanup_target(target)
    if _target_connections(environment, target) != 0:
        raise SnapshotError("A session is using the target database.")
    catalog = _cutover_catalog(environment)
    if catalog[target] is None or catalog[SERVING_DATABASE] != "open":
        raise SnapshotError("The cutover databases are in an unexpected state.")
    if catalog[PREVIOUS_DATABASE] == "open":
        raise SnapshotError("The previous serving database is accepting connections.")
    if target == EXPIRED_DATABASE and catalog[target] != "closed":
        raise SnapshotError("The expired generation is accepting connections.")
    if target == CANDIDATE_DATABASE and catalog[PREVIOUS_DATABASE] is None:
        raise SnapshotError("The demoted candidate is the only remaining copy of its release.")
    if target == CANDIDATE_DATABASE and _database_comment(environment, target) != _DEMOTED_MARK:
        raise SnapshotError("The candidate is not a demoted serving generation.")
    statement = _DROP_SQL[target]
    if "CASCADE" in statement or "IF EXISTS" in statement:
        raise SnapshotError("Cleanup refuses this database statement.")
    _guard_generation_lock(environment, required=True)
    lock = _active_generation_lock.get()
    if lock is None:
        raise SnapshotError("The generation lock was lost. Inspect pg_database before retrying.")
    lock.run_while_held(statement, marker="dropped")


def _assert_cleanup_target(database: str) -> None:
    if database not in CLEANUP_TARGETS:
        raise SnapshotError("Cleanup refuses this database name.")


def _target_connections(environment: str, database: str) -> int:
    _assert_cleanup_target(database)
    count = _admin(
        environment,
        "postgres",
        "SELECT count(*)::text FROM pg_stat_activity "
        f"WHERE datname = {sql_literal(database)} AND pid <> pg_backend_pid();",
    ).strip()
    return int(count or "0")


def _default_output(environment: str) -> Path:
    return runtime_dir(environment) / "publications"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create a selective snapshot, restore a candidate, cut it over, "
            "clean a generation, or sign its manifest."
        )
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
    promote_cmd = sub.add_parser("promote-candidate")
    promote_cmd.add_argument("environment")
    promote_cmd.add_argument("--confirm-release", required=True)
    rollback_cmd = sub.add_parser("rollback-serving")
    rollback_cmd.add_argument("environment")
    rollback_cmd.add_argument("--confirm-release", required=True)
    cleanup_cmd = sub.add_parser("cleanup-generation")
    cleanup_cmd.add_argument("environment")
    cleanup_cmd.add_argument("--target", required=True)
    cleanup_cmd.add_argument("--confirm-release", required=True)
    cleanup_cmd.add_argument("--dry-run", action="store_true")
    sign_cmd = sub.add_parser("sign-manifest")
    sign_cmd.add_argument("manifest", type=Path)
    sign_cmd.add_argument("--key", type=Path, required=True)
    verify_cmd = sub.add_parser("verify-manifest")
    verify_cmd.add_argument("manifest", type=Path)
    verify_cmd.add_argument("--signature", type=Path)
    verify_cmd.add_argument("--allowed-signers", type=Path, required=True)
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
        elif args.command == "promote-candidate":
            print(json.dumps(promote_candidate(args.environment, args.confirm_release), indent=2))
        elif args.command == "rollback-serving":
            print(json.dumps(rollback_serving(args.environment, args.confirm_release), indent=2))
        elif args.command == "cleanup-generation":
            print(
                json.dumps(
                    cleanup_generation(
                        args.environment,
                        args.target,
                        args.confirm_release,
                        dry_run=args.dry_run,
                    ),
                    indent=2,
                )
            )
        elif args.command == "sign-manifest":
            print(sign_manifest(args.manifest, args.key))
        elif args.command == "verify-manifest":
            signature = args.signature or signature_path_for(args.manifest)
            verify_manifest(args.manifest, signature, args.allowed_signers)
            print("Snapshot signature accepted.")
    except SnapshotError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main(sys.argv[1:])
