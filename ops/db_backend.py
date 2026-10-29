"""Choose the PostgreSQL command runner.

``compose`` is the default and talks to the local Docker cluster. ``native``
runs ``psql`` and ``pg_restore`` on the serving host. It is selected only by
``OPENFACTORS_DB_BACKEND=native``. A missing or unknown setting fails closed.
Native commands never fall back to Docker.

The restore program itself runs as Linux root so it can read the pinned
signers file. Peer authentication does not treat root as ``of_restore``.
Root starts the client through ``runuser`` as the dedicated OS user
``of_restore``. PostgreSQL then maps that OS user to the database role of
the same name. This module does not write ``pg_hba.conf`` and it does not
use trust authentication.
"""

from __future__ import annotations

import os
import re

OS_USER = "of_restore"
DB_ROLE = "of_restore"
NATIVE_DATABASES = frozenset(
    {
        "postgres",
        "openfactors_candidate",
        "openfactors_serving",
        "openfactors_previous",
        "openfactors_expired",
    }
)
_CONTAINER_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")


class BackendError(RuntimeError):
    """The database backend is missing or would weaken authentication."""


def backend_name() -> str:
    value = os.environ.get("OPENFACTORS_DB_BACKEND", "compose")
    if value not in {"compose", "native"}:
        raise BackendError("OPENFACTORS_DB_BACKEND must be compose or native.")
    return value


def identity_prefix(*, euid: int, target_uid: int) -> list[str]:
    """Return the argv prefix that selects the of_restore OS user.

    The same user needs no prefix. Root uses ``runuser``. Any other user is
    refused. The prefix never enables trust authentication.
    """
    if euid == target_uid:
        return []
    if euid != 0:
        raise BackendError(
            "Native PostgreSQL commands run as root and then as the of_restore OS user."
        )
    return ["runuser", "-u", OS_USER, "--"]


def connection_prefix() -> list[str]:
    """Build the process prefix for a native client."""
    if os.environ.get("OPENFACTORS_NATIVE_DOCKER_CONTAINER"):
        if os.environ.get("OPENFACTORS_NATIVE_TEST_HARNESS") != "1":
            raise BackendError("The disposable test harness is not enabled.")
        name = os.environ["OPENFACTORS_NATIVE_DOCKER_CONTAINER"]
        if _CONTAINER_NAME.fullmatch(name) is None:
            raise BackendError("The disposable test container name is not valid.")
        return ["docker", "exec", "-i", "-u", OS_USER, name]
    if os.environ.get("OPENFACTORS_NATIVE_TEST_HARNESS") == "1":
        raise BackendError("The disposable test harness has no container name.")
    if os.name != "posix":
        raise BackendError("Native PostgreSQL commands run on Linux.")
    import pwd

    try:
        record = pwd.getpwnam(OS_USER)
    except KeyError as exc:
        raise BackendError("The of_restore OS user does not exist.") from exc
    return identity_prefix(euid=os.geteuid(), target_uid=record.pw_uid)


def native_socket() -> str:
    socket = os.environ.get("OPENFACTORS_NATIVE_SOCKET", "/var/run/postgresql")
    parts = socket.split("/")
    if not socket.startswith("/") or ".." in parts or any(part == "" for part in parts[1:]):
        raise BackendError("The native socket path is not absolute.")
    return socket


def _database(database: str) -> str:
    if database not in NATIVE_DATABASES:
        raise BackendError("Native PostgreSQL refuses this database name.")
    return database


def _argv(tool: str, database: str, *, interactive: bool, tail: list[str]) -> list[str]:
    if tool not in {"psql", "pg_restore"}:
        raise BackendError("Native PostgreSQL refuses this client.")
    database = _database(database)
    prefix = connection_prefix()
    command = [tool, "--username", DB_ROLE, "--no-password"]
    if not os.environ.get("OPENFACTORS_NATIVE_DOCKER_CONTAINER"):
        command.extend(["--host", native_socket()])
    command.extend(["--dbname", database, *tail])
    if interactive:
        command = ["stdbuf", "-oL", "-eL", *command]
    if "trust" in command:
        raise BackendError("Native PostgreSQL refuses trust authentication.")
    return prefix + command


def native_psql_argv(database: str, *, interactive: bool) -> list[str]:
    return _argv(
        "psql",
        database,
        interactive=interactive,
        tail=["--no-align", "--tuples-only", "--no-psqlrc", "--set", "ON_ERROR_STOP=1"],
    )


def native_pg_restore_argv(database: str, dump_path: str) -> list[str]:
    if not dump_path.startswith("/") or "\n" in dump_path or "\x00" in dump_path:
        raise BackendError("The native restore path is not absolute.")
    if ".." in dump_path.split("/"):
        raise BackendError("The native restore path is not absolute.")
    return _argv(
        "pg_restore",
        database,
        interactive=False,
        tail=["--exit-on-error", "--no-owner", "--no-acl", dump_path],
    )
