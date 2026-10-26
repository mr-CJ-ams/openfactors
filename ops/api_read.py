"""Read Postgres for the public API through the psql client.

The public process connects as of_api. It does not use Docker Compose exec.
Migrations, ingestion, dbt, and promotion keep their own roles and the Compose
path in ops.migrate.

TLS modes:
- require is local only. The session is encrypted. The server certificate is
  not verified. The local Compose certificate is self-signed for 127.0.0.1,
  and its CA stays in the Compose volume.
- verify-full is required when OPENFACTORS_API_PGHOST is set. It also requires
  OPENFACTORS_API_PGSSLROOTCERT, a trusted CA file, and the host name must
  match that certificate.

The password is written to a temporary client password file and passed with
PGPASSFILE. It is not placed in the psql argument list, and that file is
removed when the command finishes. Request values are psql variables, not
shell text.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from ops.local_stack import load_secrets, require_environment, runtime_dir

API_ROLE = "of_api"
LOCAL_SSLMODE = "require"
HOSTED_SSLMODE = "verify-full"
_ALLOWED_SSLMODES = frozenset({LOCAL_SSLMODE, HOSTED_SSLMODE})
_HOST = re.compile(r"^[A-Za-z0-9._-]+$")
_DATABASE = re.compile(r"^[A-Za-z0-9_-]+$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COLUMN = re.compile(r"^[a-z_][a-z0-9_]*$")
_SAFE_CONN = re.compile(r"^[A-Za-z0-9_.:/@+-]+$")


class ApiReadError(RuntimeError):
    """The API could not open its read connection."""


class ApiDatabase:
    """One of_api connection. The password is omitted from repr."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        sslmode: str,
        sslrootcert: Path | None,
    ) -> None:
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.sslmode = sslmode
        self.sslrootcert = sslrootcert

    def __repr__(self) -> str:
        return (
            "ApiDatabase("
            f"host={self.host!r}, port={self.port!r}, database={self.database!r}, "
            f"user={self.user!r}, sslmode={self.sslmode!r})"
        )


def bind_address(host: str | None = None, port: int | None = None) -> tuple[str, int]:
    """Return the HTTP bind address. The default is 127.0.0.1:8000."""
    if host is None:
        configured = os.environ.get("OPENFACTORS_API_BIND_HOST")
        host = "127.0.0.1" if configured is None else configured
    if port is None:
        configured_port = os.environ.get("OPENFACTORS_API_BIND_PORT")
        port = 8000 if configured_port is None else _bind_port(configured_port)
    cleaned = host.strip()
    if not cleaned or any(character.isspace() or ord(character) < 32 for character in cleaned):
        raise ValueError("The catalog bind host is empty.")
    if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
        raise ValueError("The catalog bind port is not a port.")
    return cleaned, port


def export_directory(environment: str, release_id: str) -> Path:
    """Return the directory for one release export.

    OPENFACTORS_API_EXPORT_DIR replaces the local runtime directory. The row
    and byte caps are unchanged.
    """
    release = str(uuid.UUID(release_id))
    configured = os.environ.get("OPENFACTORS_API_EXPORT_DIR", "").strip()
    if configured:
        if any(ord(character) < 32 for character in configured):
            raise ValueError("The export directory is not a usable path.")
        root = Path(configured)
    else:
        root = runtime_dir(environment) / "exports"
    return root / release


def sql_identifiers(names: tuple[str, ...]) -> str:
    """Return a comma-separated list of public column names."""
    selected: list[str] = []
    for name in names:
        if _COLUMN.fullmatch(name) is None:
            raise ApiReadError("The column name is not a public identifier.")
        selected.append(name)
    if not selected:
        raise ApiReadError("The column list is empty.")
    return ", ".join(selected)


def load_database(environment: str) -> ApiDatabase:
    """Load the of_api connection. A configured host cannot use local TLS."""
    _reject_wrong_user()
    if os.environ.get("OPENFACTORS_API_PGHOST", "").strip():
        return _hosted_database()
    return _local_database(environment)


def read_sql(
    environment: str,
    sql: str,
    *,
    variables: dict[str, str] | None = None,
    integers: dict[str, int] | None = None,
    tuples_only: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one read statement as of_api. SQL is stdin, not a shell command."""
    return run_query(
        load_database(environment),
        sql,
        variables=variables or {},
        integers=integers or {},
        tuples_only=tuples_only,
    )


def run_query(
    database: ApiDatabase,
    sql: str,
    *,
    variables: dict[str, str],
    integers: dict[str, int],
    tuples_only: bool,
) -> subprocess.CompletedProcess[str]:
    command = _command(database, variables, integers, tuples_only=tuples_only)
    password_file = _write_pgpass(database)
    try:
        env = os.environ.copy()
        env.pop("PGPASSWORD", None)
        env.pop("OPENFACTORS_API_PGPASSWORD", None)
        env.pop("PGSERVICE", None)
        env["PGPASSFILE"] = str(password_file)
        env["PGSSLMODE"] = database.sslmode
        if database.sslrootcert is None:
            env.pop("PGSSLROOTCERT", None)
        else:
            env["PGSSLROOTCERT"] = str(database.sslrootcert)
        return subprocess.run(
            command,
            input=sql,
            text=True,
            capture_output=True,
            check=False,
            env=env,
            shell=False,
        )
    finally:
        password_file.unlink(missing_ok=True)


def psql_executable() -> str:
    configured = os.environ.get("OPENFACTORS_API_PSQL", "").strip()
    if configured:
        if not Path(configured).is_file():
            raise ApiReadError("OPENFACTORS_API_PSQL is not a psql program.")
        return configured
    found = shutil.which("psql")
    if found:
        return found
    if sys.platform == "win32":
        root = Path(r"C:\Program Files\PostgreSQL")
        candidates = [path for path in root.glob("*/bin/psql.exe") if path.is_file()]
        if candidates:
            candidates.sort(key=lambda path: _version_key(path.parents[1].name), reverse=True)
            return str(candidates[0])
    raise ApiReadError(
        "The psql client is not installed. Install the PostgreSQL client and rerun this command."
    )


def _hosted_database() -> ApiDatabase:
    mode, cert = _sslmode(hosted=True)
    return ApiDatabase(
        host=_token("OPENFACTORS_API_PGHOST", _HOST),
        port=_port("OPENFACTORS_API_PGPORT"),
        database=_token("OPENFACTORS_API_PGDATABASE", _DATABASE),
        user=API_ROLE,
        password=_password("OPENFACTORS_API_PGPASSWORD"),
        sslmode=mode,
        sslrootcert=cert,
    )


def _local_database(environment: str) -> ApiDatabase:
    mode, cert = _sslmode(hosted=False)
    try:
        settings = require_environment(environment)
    except SystemExit as exc:
        raise ApiReadError(str(exc)) from exc
    secrets = load_secrets(environment)
    password = secrets.get("OF_API_PASSWORD", "")
    if not password or any(ord(character) < 32 for character in password):
        raise ApiReadError("The local API password is not in the runtime secrets file.")
    return ApiDatabase(
        host="127.0.0.1",
        port=int(settings["postgres_port"]),
        database=str(settings["database"]),
        user=API_ROLE,
        password=password,
        sslmode=mode,
        sslrootcert=cert,
    )


def _reject_wrong_user() -> None:
    configured = os.environ.get("OPENFACTORS_API_PGUSER")
    if configured is None:
        return
    if configured.strip() != API_ROLE:
        raise ApiReadError("The API database user must be of_api.")


def _sslmode(*, hosted: bool) -> tuple[str, Path | None]:
    raw = os.environ.get("OPENFACTORS_API_PGSSLMODE", "").strip()
    if hosted:
        if raw == LOCAL_SSLMODE:
            raise ApiReadError(
                "sslmode=require is the local-only mode. "
                "A configured API host must use sslmode=verify-full."
            )
        if raw != HOSTED_SSLMODE:
            raise ApiReadError("A configured API host must use sslmode=verify-full.")
        return HOSTED_SSLMODE, _ca_file()
    mode = raw or LOCAL_SSLMODE
    if mode not in _ALLOWED_SSLMODES:
        raise ApiReadError("The API connection must use sslmode=require or sslmode=verify-full.")
    if mode == HOSTED_SSLMODE:
        return mode, _ca_file()
    return mode, None


def _ca_file() -> Path:
    raw = os.environ.get("OPENFACTORS_API_PGSSLROOTCERT", "").strip()
    if not raw or any(ord(character) < 32 for character in raw):
        raise ApiReadError(
            "OPENFACTORS_API_PGSSLROOTCERT must be a trusted CA file for sslmode=verify-full."
        )
    path = Path(raw)
    if not path.is_file():
        raise ApiReadError(
            "OPENFACTORS_API_PGSSLROOTCERT must be a trusted CA file for sslmode=verify-full."
        )
    return path


def _token(name: str, pattern: re.Pattern[str]) -> str:
    value = os.environ.get(name, "").strip()
    if pattern.fullmatch(value) is None:
        raise ApiReadError(f"{name} is not a usable value.")
    return value


def _port(name: str) -> int:
    text = os.environ.get(name, "").strip()
    if not text.isdigit():
        raise ApiReadError(f"{name} is not a port.")
    number = int(text)
    if number < 1 or number > 65535:
        raise ApiReadError(f"{name} is not a port.")
    return number


def _bind_port(text: str) -> int:
    stripped = text.strip()
    if not stripped.isdigit():
        raise ValueError("The catalog bind port is not a port.")
    number = int(stripped)
    if number > 65535:
        raise ValueError("The catalog bind port is not a port.")
    return number


def _password(name: str) -> str:
    value = os.environ.get(name, "")
    if value == "" or any(ord(character) < 32 for character in value):
        raise ApiReadError(f"{name} is required.")
    return value


def _command(
    database: ApiDatabase,
    variables: dict[str, str],
    integers: dict[str, int],
    *,
    tuples_only: bool,
) -> list[str]:
    command = [
        psql_executable(),
        _conninfo(database),
        "--no-password",
        "--no-psqlrc",
        "--quiet",
        "-v",
        "ON_ERROR_STOP=1",
    ]
    if tuples_only:
        command.extend(["--tuples-only", "--no-align"])
    for name, value in variables.items():
        _string_variable(name, value)
        command.extend(["-v", f"{name}={value}"])
    for name, value in integers.items():
        _integer_variable(name, value)
        command.extend(["-v", f"{name}={value}"])
    command.extend(["-f", "-"])
    return command


def _string_variable(name: str, value: str) -> None:
    if _IDENTIFIER.fullmatch(name) is None:
        raise ApiReadError("The SQL variable name is not an identifier.")
    if any(ord(character) < 32 for character in value):
        raise ApiReadError("The SQL variable contains a control character.")


def _integer_variable(name: str, value: int) -> None:
    if _IDENTIFIER.fullmatch(name) is None:
        raise ApiReadError("The SQL variable name is not an identifier.")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiReadError("The SQL integer variable is not a whole number.")


def _conninfo(database: ApiDatabase) -> str:
    parts = [
        f"host={database.host}",
        f"port={database.port}",
        f"user={database.user}",
        f"dbname={database.database}",
        f"sslmode={database.sslmode}",
    ]
    if database.sslrootcert is not None:
        parts.append("sslrootcert=" + _conn_value(database.sslrootcert.resolve().as_posix()))
    return " ".join(parts)


def _conn_value(value: str) -> str:
    if _SAFE_CONN.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return "'" + escaped + "'"


def _write_pgpass(database: ApiDatabase) -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix="openfactors-api-",
        suffix=".pgpass",
        delete=False,
    )
    path = Path(handle.name)
    handle.close()
    try:
        os.chmod(path, 0o600)
        fields = (
            database.host,
            str(database.port),
            database.database,
            database.user,
            database.password,
        )
        line = ":".join(_pgpass_field(field) for field in fields)
        path.write_text(line + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _pgpass_field(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _version_key(name: str) -> tuple[int, ...]:
    numbers: list[int] = []
    for piece in name.split("."):
        digits = "".join(character for character in piece if character.isdigit())
        numbers.append(int(digits) if digits else 0)
    return tuple(numbers)
