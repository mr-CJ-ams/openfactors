"""Provisional public API admission limits.

60 requests per rolling 60 seconds apply to a resolved non-loopback client.
Loopback is unlimited. These figures are not staging measurements.

Forwarded and X-Forwarded-For are ignored unless the TCP peer is listed in
OPENFACTORS_API_TRUSTED_PROXIES. A trusted peer must send one chain. The
client is the nearest address in that chain that is not itself a trusted proxy.
"""

from __future__ import annotations

import ipaddress
import math
import os
import threading
import time
from collections import deque

REQUESTS_PER_WINDOW = 60
WINDOW_SECONDS = 60
MAX_TRACKED_CLIENTS = 4096
SOCKET_TIMEOUT_SECONDS = 30


def apply_socket_timeout(connection: object) -> None:
    """Bound one accepted connection. Query work still has its own deadline."""
    setter = getattr(connection, "settimeout", None)
    if setter is not None:
        setter(SOCKET_TIMEOUT_SECONDS)


_MAX_CHAIN = 8
_MAX_HEADER = 512


class ClientAddressError(Exception):
    """The peer or proxy chain does not identify one client."""


class RequestWindow:
    """A rolling window with a fixed number of client buckets."""

    def __init__(
        self,
        *,
        limit: int = REQUESTS_PER_WINDOW,
        interval: float = WINDOW_SECONDS,
        max_clients: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        self._limit = limit
        self._interval = interval
        self._max_clients = max_clients
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def decide(self, client: str, now: float) -> int | None:
        with self._lock:
            self._purge(now)
            hits = self._hits.get(client)
            if hits is None:
                if len(self._hits) >= self._max_clients and not self._drop_idle(now):
                    return math.ceil(self._interval)
                hits = deque()
                self._hits[client] = hits
            if len(hits) >= self._limit:
                retry = math.ceil(hits[0] + self._interval - now)
                return retry if retry > 0 else 1
            hits.append(now)
            return None

    def _purge(self, now: float) -> None:
        cutoff = now - self._interval
        empty: list[str] = []
        for client, hits in self._hits.items():
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if not hits:
                empty.append(client)
        for client in empty:
            del self._hits[client]

    def _drop_idle(self, now: float) -> bool:
        cutoff = now - self._interval
        idle = [client for client, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        if not idle:
            return False
        del self._hits[idle[0]]
        return True


SHARED_WINDOW = RequestWindow()


def trusted_proxies() -> frozenset[str]:
    raw = os.environ.get("OPENFACTORS_API_TRUSTED_PROXIES", "")
    found: list[str] = []
    for part in raw.split(","):
        text = part.strip()
        if not text:
            continue
        found.append(_canonical(text))
    return frozenset(found)


def resolve_client(peer: str, headers: object) -> tuple[str, bool]:
    """Return the client address and whether a trusted proxy supplied it."""
    peer_ip = _canonical(peer)
    trusted = trusted_proxies()
    if peer_ip not in trusted:
        return peer_ip, False
    forwarded = _header_values(headers, "Forwarded")
    via = _header_values(headers, "X-Forwarded-For")
    if forwarded and via:
        raise ClientAddressError
    if forwarded:
        chain = _forwarded_chain(",".join(forwarded))
    elif via:
        chain = _xff_chain(",".join(via))
    else:
        raise ClientAddressError
    for address in reversed(chain):
        if address not in trusted:
            return address, True
    raise ClientAddressError


def admission(
    peer: str,
    headers: object,
    *,
    window: RequestWindow | None = None,
    now: float | None = None,
) -> int | None:
    """Return Retry-After seconds, or None when the request may proceed."""
    client, via_proxy = resolve_client(peer, headers)
    if ipaddress.ip_address(client).is_loopback and not via_proxy:
        return None
    clock = time.monotonic() if now is None else now
    return (window or SHARED_WINDOW).decide(client, clock)


def _canonical(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError as exc:
        raise ClientAddressError from exc


def _header_values(headers: object, name: str) -> list[str]:
    getter = getattr(headers, "get_all", None)
    if getter is not None:
        values = getter(name) or []
    else:
        one = getattr(headers, "get", lambda _name: None)(name)
        values = [] if one is None else [one]
    cleaned = []
    for value in values:
        if not isinstance(value, str) or len(value) > _MAX_HEADER:
            raise ClientAddressError
        text = value.strip()
        if text:
            cleaned.append(text)
    return cleaned


def _xff_chain(value: str) -> list[str]:
    if len(value) > _MAX_HEADER or any(ord(character) < 32 for character in value):
        raise ClientAddressError
    parts = [part.strip() for part in value.split(",")]
    if not parts or len(parts) > _MAX_CHAIN or any(not part for part in parts):
        raise ClientAddressError
    return [_canonical(part) for part in parts]


def _forwarded_chain(value: str) -> list[str]:
    if len(value) > _MAX_HEADER or any(ord(character) < 32 for character in value):
        raise ClientAddressError
    elements = _split_outside_quotes(value, ",")
    if not elements or len(elements) > _MAX_CHAIN:
        raise ClientAddressError
    chain: list[str] = []
    for element in elements:
        fors = []
        for parameter in _split_outside_quotes(element, ";"):
            name, separator, raw = parameter.strip().partition("=")
            if separator != "=" or name.lower() != "for":
                continue
            fors.append(_unquote(raw.strip()))
        if len(fors) != 1:
            raise ClientAddressError
        chain.append(_one_forwarded_address(fors[0]))
    return chain


def _one_forwarded_address(value: str) -> str:
    if value.startswith("["):
        if "]" not in value:
            raise ClientAddressError
        bracket, rest = value[1:].split("]", 1)
        if rest != "":
            raise ClientAddressError
        return _canonical(bracket)
    return _canonical(value)


def _unquote(value: str) -> str:
    if not value.startswith('"'):
        if any(character in value for character in ' "\\'):
            raise ClientAddressError
        return value
    if len(value) < 2 or not value.endswith('"'):
        raise ClientAddressError
    inner = value[1:-1]
    return inner.replace('\\"', '"').replace("\\\\", "\\")


def _split_outside_quotes(value: str, separator: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if quoted and character == "\\":
            current.append(character)
            escaped = True
            continue
        if character == '"':
            quoted = not quoted
            current.append(character)
            continue
        if character == separator and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    if quoted or escaped:
        raise ClientAddressError
    parts.append("".join(current))
    return parts
