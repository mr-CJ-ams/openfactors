"""One public error body. No request quota is configured.

A limiter may refuse one request and supply its own Retry-After seconds.
This module does not choose that number.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

Limiter = Callable[[str, str], int | None]


def api_error(code: str, message: str, details: dict | None = None) -> dict:
    error = {"code": code, "message": message, "trace_id": uuid.uuid4().hex}
    if details:
        error["details"] = dict(details)
    return {"error": error}


def limit_response(
    method: str,
    path: str,
    limiter: Limiter | None,
) -> tuple[int, dict[str, str], dict] | None:
    if limiter is None:
        return None
    try:
        decision = limiter(method, path)
    except Exception:
        return 500, {}, api_error("INTERNAL", "The request failed.")
    if decision is None:
        return None
    if isinstance(decision, bool) or not isinstance(decision, int) or decision < 0:
        return 500, {}, api_error("INTERNAL", "The request failed.")
    return (
        429,
        {"Retry-After": str(decision)},
        api_error("RATE_LIMITED", "Too many requests."),
    )
