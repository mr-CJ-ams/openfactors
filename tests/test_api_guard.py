"""Provisional API admission: window, proxy chain, process cap, and export sweep."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import ops.api_read as api_read
import pytest
from ops.api_read import (
    API_PROCESS_SLOTS,
    API_STATEMENT_TIMEOUT,
    API_SUBPROCESS_DEADLINE_SECONDS,
    ApiDatabase,
    ApiReadError,
    read_sql,
)

from openfactors.api.catalog import CATALOG_PATH, catalog_server, open_catalog
from openfactors.api.export_limits import sweep_unfinished_exports
from openfactors.api.factors import FACTORS_PATH, load_factor_page
from openfactors.api.guard import (
    SOCKET_TIMEOUT_SECONDS,
    ClientAddressError,
    RequestWindow,
    admission,
    apply_socket_timeout,
    resolve_client,
)

_RELEASE = "00000000-0000-4000-8000-000000000503"
_PUBLIC = "203.0.113.8"
_SPOOF = "198.51.100.9"
_ROOT = Path(__file__).resolve().parents[1]


class _Headers:
    def __init__(self, values: dict[str, list[str]]) -> None:
        self.values = values

    def get_all(self, name: str) -> list[str] | None:
        return self.values.get(name)


def test_rolling_window_refuses_the_61st_and_ignores_rejected_requests() -> None:
    window = RequestWindow()
    for _ in range(60):
        assert window.decide(_PUBLIC, 1_000.0) is None
    retry = window.decide(_PUBLIC, 1_000.0)
    assert retry is not None and retry >= 1
    assert window.decide(_PUBLIC, 1_000.0) == retry
    assert window.decide(_PUBLIC, 1_060.0) is None
    narrow = RequestWindow(limit=1, interval=10)
    assert narrow.decide("203.0.113.4", 0.0) is None
    assert narrow.decide("203.0.113.4", 1.0) == 9
    assert narrow.decide("203.0.113.4", 10.0) is None


def test_window_memory_stays_bounded() -> None:
    window = RequestWindow(limit=1, interval=60, max_clients=2)
    assert window.decide("203.0.113.1", 0.0) is None
    assert window.decide("203.0.113.2", 0.0) is None
    assert window.decide("203.0.113.3", 1.0) == 60
    assert window.decide("203.0.113.1", 1.0) == 59


def test_concurrent_window_admits_only_the_limit() -> None:
    window = RequestWindow(limit=60, interval=60, max_clients=8)
    results: list[int | None] = []
    lock = threading.Lock()

    def work() -> None:
        local: list[int | None] = []
        for _ in range(20):
            local.append(window.decide(_PUBLIC, 50.0))
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(item is None for item in results) == 60
    assert all(item is None or item >= 1 for item in results)


def test_loopback_is_unlimited_and_ignores_a_spoofed_forwarding_header() -> None:
    window = RequestWindow(limit=1, interval=60)
    headers = _Headers({"X-Forwarded-For": [_SPOOF], "Forwarded": [f"for={_SPOOF}"]})
    for _ in range(5):
        assert admission("127.0.0.1", headers, window=window, now=1.0) is None
        assert admission("::1", headers, window=window, now=1.0) is None
    assert window.decide(_SPOOF, 1.0) is None
    assert window.decide("127.0.0.1", 1.0) is None


def test_an_untrusted_peer_is_limited_and_the_spoofed_address_is_not() -> None:
    window = RequestWindow(limit=1, interval=60)
    headers = _Headers({"X-Forwarded-For": [_SPOOF]})
    assert admission(_PUBLIC, headers, window=window, now=1.0) is None
    assert admission(_PUBLIC, headers, window=window, now=1.0) == 60
    assert window.decide(_SPOOF, 1.0) is None


def test_a_trusted_proxy_uses_the_nearest_untrusted_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENFACTORS_API_TRUSTED_PROXIES", "127.0.0.1, 192.0.2.10")
    headers = _Headers({"X-Forwarded-For": [f"{_SPOOF}, {_PUBLIC}, 192.0.2.10"]})
    assert resolve_client("127.0.0.1", headers) == (_PUBLIC, True)
    direct = _Headers({"X-Forwarded-For": [f"{_SPOOF}, {_PUBLIC}"]})
    assert resolve_client("203.0.113.50", direct) == ("203.0.113.50", False)


def test_a_bad_proxy_chain_is_refused_without_using_the_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENFACTORS_API_TRUSTED_PROXIES", "127.0.0.1")
    both = _Headers({"Forwarded": [f"for={_SPOOF}"], "X-Forwarded-For": [_PUBLIC]})
    with pytest.raises(ClientAddressError):
        resolve_client("127.0.0.1", both)
    for value in ("for=unknown", 'for="example.com"', "for=192.0.2.1:443", "for=_hidden"):
        with pytest.raises(ClientAddressError):
            resolve_client("127.0.0.1", _Headers({"Forwarded": [value]}))
    with pytest.raises(ClientAddressError):
        resolve_client("127.0.0.1", _Headers({"X-Forwarded-For": ["example.com"]}))
    bracket = _Headers({"Forwarded": ['for="[2001:db8::1]"']})
    assert resolve_client("127.0.0.1", bracket) == ("2001:db8::1", True)


def test_loopback_catalog_stays_available_past_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENFACTORS_API_TRUSTED_PROXIES", raising=False)
    server = _serve(catalog_server(lambda: []))
    try:
        for _ in range(61):
            status, headers, _body = _request(server, CATALOG_PATH)
            assert status == 200
            assert "Retry-After" not in headers
    finally:
        _close(server)


def test_trusted_proxy_limit_keeps_the_429_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENFACTORS_API_TRUSTED_PROXIES", "127.0.0.1")
    monkeypatch.setattr(
        "openfactors.api.guard.SHARED_WINDOW",
        RequestWindow(limit=2, interval=60),
    )
    server = _serve(catalog_server(lambda: []))
    try:
        outcomes = [
            _request(server, CATALOG_PATH, headers={"X-Forwarded-For": _PUBLIC}) for _ in range(3)
        ]
    finally:
        _close(server)
    assert [item[0] for item in outcomes] == [200, 200, 429]
    retry = outcomes[2][1]["Retry-After"]
    assert int(retry) >= 1
    body = outcomes[2][2]
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["message"] == "Too many requests."
    encoded = json.dumps(body)
    assert _PUBLIC not in encoded
    assert "X-Forwarded-For" not in encoded


def test_a_spoofed_or_broken_chain_does_not_change_the_loopback_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENFACTORS_API_TRUSTED_PROXIES", raising=False)
    server = _serve(catalog_server(lambda: []))
    try:
        status, _headers, body = _request(
            server,
            CATALOG_PATH,
            headers={"X-Forwarded-For": _SPOOF, "Forwarded": f"for={_SPOOF}"},
        )
    finally:
        _close(server)
    assert status == 200
    assert _SPOOF not in json.dumps(body)

    monkeypatch.setenv("OPENFACTORS_API_TRUSTED_PROXIES", "127.0.0.1")
    limited = _serve(catalog_server(lambda: []))
    try:
        status, _headers, body = _request(
            limited,
            CATALOG_PATH,
            headers={"X-Forwarded-For": _SPOOF, "Forwarded": "for=unknown"},
        )
    finally:
        _close(limited)
    assert status == 400
    assert body["error"]["code"] == "MALFORMED_REQUEST"
    assert body["error"]["message"] == "The request is malformed."
    encoded = json.dumps(body)
    assert _SPOOF not in encoded
    assert "unknown" not in encoded


def test_a_bad_proxy_list_refuses_startup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENFACTORS_API_TRUSTED_PROXIES", "example.com")
    monkeypatch.setenv("OPENFACTORS_API_EXPORT_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="OPENFACTORS_API_TRUSTED_PROXIES"):
        open_catalog("dev", "127.0.0.1", 0)


def test_the_fifth_psql_returns_immediately_and_does_not_start() -> None:
    held = 0
    try:
        while held < API_PROCESS_SLOTS and api_read._SLOTS.acquire(blocking=False):
            held += 1
        assert held == API_PROCESS_SLOTS
        started = time.monotonic()
        with pytest.raises(ApiReadError, match="The API is busy."):
            read_sql("dev", "SELECT 1;")
        assert time.monotonic() - started < 0.5
    finally:
        for _ in range(held):
            api_read._SLOTS.release()


def test_concurrent_reads_do_not_pass_the_process_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    inflight = 0
    peak = 0
    errors: list[ApiReadError] = []
    lock = threading.Lock()
    release = threading.Event()
    entered = threading.Event()

    def slow(_database: object, _sql: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
            if inflight >= API_PROCESS_SLOTS:
                entered.set()
        release.wait(timeout=2)
        with lock:
            inflight -= 1
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr("ops.api_read.load_database", lambda _environment: object())
    monkeypatch.setattr("ops.api_read.run_query", slow)

    def call() -> None:
        try:
            read_sql("dev", "SELECT 1;")
        except ApiReadError as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(API_PROCESS_SLOTS + 2)]
    for thread in threads:
        thread.start()
    assert entered.wait(timeout=2)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not errors:
        time.sleep(0.01)
    release.set()
    for thread in threads:
        thread.join()
    assert peak <= API_PROCESS_SLOTS
    assert errors
    assert all(isinstance(item, ApiReadError) and "busy" in str(item) for item in errors)


def test_a_timed_out_psql_is_reaped_before_its_slot_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class Hung:
        def __init__(self, command: list[str], **_kwargs: object) -> None:
            self.command = command
            self.returncode: int | None = None
            Hung.last = self

        def communicate(
            self, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            if not hasattr(Hung, "timeout"):
                Hung.script = input
                Hung.timeout = timeout
            if self.returncode is not None:
                return "", ""
            raise subprocess.TimeoutExpired(self.command, timeout or 0)

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            order.append("kill")
            self.returncode = -9

    original_release = api_read._SLOTS.release

    def release() -> None:
        order.append("release")
        original_release()

    monkeypatch.setattr(api_read._SLOTS, "release", release)
    monkeypatch.setattr("ops.api_read.load_database", lambda _environment: _database())
    monkeypatch.setattr("ops.api_read.psql_executable", lambda: "psql")
    monkeypatch.setattr("ops.api_read.subprocess.Popen", Hung)
    with pytest.raises(ApiReadError, match="timed out"):
        read_sql("dev", "SELECT 1;")
    assert Hung.timeout == API_SUBPROCESS_DEADLINE_SECONDS
    assert str(Hung.script).startswith(f"SET statement_timeout = '{API_STATEMENT_TIMEOUT}';\n")
    assert order[0] == "kill"
    assert "release" in order
    assert order.index("kill") < order.index("release")
    _assert_all_slots_free()


def test_api_statement_timeout_is_not_applied_to_admin_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def capture(_database: object, sql: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["sql"] = sql
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr("ops.api_read.load_database", lambda _environment: object())
    monkeypatch.setattr("ops.api_read.run_query", capture)
    read_sql("dev", "SELECT 1;")
    assert seen["sql"].startswith(f"SET statement_timeout = '{API_STATEMENT_TIMEOUT}';\n")
    for relative in (
        "ops/migrate.py",
        "ops/promote.py",
        "ops/withdraw.py",
        "ops/load_stored.py",
        "ops/local_stack.py",
    ):
        assert "statement_timeout" not in (_ROOT / relative).read_text(encoding="utf-8")


def test_a_busy_factor_query_uses_the_existing_503(monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise ApiReadError("The API is busy.")

    monkeypatch.setattr("openfactors.api.factors.read_sql", busy)
    server = _serve(
        catalog_server(
            lambda: [],
            load_release=lambda _release_id: {
                "dataset_id": "daily_external_factors_ph",
                "release_id": _RELEASE,
                "dataset_version": "1.0.0",
                "published_at": "2026-10-24T16:00:00+00:00",
            },
            load_factors=lambda **filters: load_factor_page("dev", **filters),
        )
    )
    try:
        status, headers, body = _request(
            server,
            f"{FACTORS_PATH}?location_id=ph-manila&start_date=2018-01-01&end_date=2018-01-01",
        )
    finally:
        _close(server)
    assert status == 503
    assert "Retry-After" not in headers
    assert body["error"]["code"] == "FACTORS_UNAVAILABLE"
    assert body["error"]["message"] == "The factor query is temporarily unavailable."
    assert "busy" not in json.dumps(body).lower()


def test_startup_sweep_keeps_finished_exports_and_unrelated_files(tmp_path: Path) -> None:
    release = tmp_path / "00000000-0000-4000-8000-000000000099"
    release.mkdir()
    (release / "daily.csv").write_text("date\n", encoding="utf-8")
    (release / "daily.csv.manifest.json").write_text("{}\n", encoding="utf-8")
    (release / ".daily.csv.partial").write_text("partial", encoding="utf-8")
    (release / ".daily.csv.manifest.partial").write_text("manifest", encoding="utf-8")
    (release / ".gitkeep").write_text("", encoding="utf-8")
    (release / "notes.partial").write_text("keep", encoding="utf-8")
    (release / ".partial").write_text("keep", encoding="utf-8")
    other = tmp_path / "notes"
    other.mkdir()
    (other / ".daily.csv.partial").write_text("keep", encoding="utf-8")
    outside = tmp_path.parent / "openfactors-unrelated.partial"
    outside.write_text("keep", encoding="utf-8")
    upper = tmp_path / "00000000-0000-4000-8000-0000000000AB"
    upper.mkdir()
    (upper / ".daily.csv.partial").write_text("keep", encoding="utf-8")
    try:
        link = release / ".linked.csv.partial"
        link.symlink_to(release / "daily.csv")
    except OSError:
        link = None
    try:
        sweep_unfinished_exports(tmp_path)
        assert not (release / ".daily.csv.partial").exists()
        assert not (release / ".daily.csv.manifest.partial").exists()
        assert (release / "daily.csv").read_text(encoding="utf-8") == "date\n"
        assert (release / "daily.csv.manifest.json").read_text(encoding="utf-8") == "{}\n"
        assert (release / ".gitkeep").is_file()
        assert (release / "notes.partial").is_file()
        assert (release / ".partial").is_file()
        assert (other / ".daily.csv.partial").is_file()
        assert outside.read_text(encoding="utf-8") == "keep"
        assert (upper / ".daily.csv.partial").is_file()
        if link is not None:
            assert link.is_symlink()
            assert (release / "daily.csv").is_file()
    finally:
        outside.unlink(missing_ok=True)


def test_open_catalog_sweeps_only_its_export_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENFACTORS_API_TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("OPENFACTORS_API_EXPORT_DIR", str(tmp_path))
    release = tmp_path / "00000000-0000-4000-8000-000000000098"
    release.mkdir()
    (release / ".daily.csv.partial").write_text("partial", encoding="utf-8")
    (release / "daily.csv").write_text("ok", encoding="utf-8")
    server = open_catalog("dev", "127.0.0.1", 0)
    try:
        assert not (release / ".daily.csv.partial").exists()
        assert (release / "daily.csv").read_text(encoding="utf-8") == "ok"
    finally:
        server.server_close()


def test_accepted_connection_timeout_is_30_seconds() -> None:
    left, right = socket.socketpair()
    try:
        apply_socket_timeout(left)
        assert left.gettimeout() == SOCKET_TIMEOUT_SECONDS == 30
    finally:
        left.close()
        right.close()


def _database() -> ApiDatabase:
    return ApiDatabase(
        host="127.0.0.1",
        port=1,
        database="openfactors_dev",
        user="of_api",
        password="example-api-password",
        sslmode="require",
        sslrootcert=None,
    )


def _assert_all_slots_free() -> None:
    held = 0
    try:
        while held < API_PROCESS_SLOTS and api_read._SLOTS.acquire(blocking=False):
            held += 1
        assert held == API_PROCESS_SLOTS
    finally:
        for _ in range(held):
            api_read._SLOTS.release()


def _serve(server: ThreadingHTTPServer) -> ThreadingHTTPServer:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _close(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def _request(
    server: ThreadingHTTPServer,
    path: str,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict]:
    port = server.server_address[1]
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as error:
        payload = json.loads(error.read().decode("utf-8"))
        return error.code, dict(error.headers), payload
