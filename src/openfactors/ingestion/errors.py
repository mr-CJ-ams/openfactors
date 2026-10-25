"""Structured failures from the shared ingestion connector."""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_HTTP = "upstream_http"
    CLIENT_HTTP = "client_http"
    NETWORK = "network"
    PARTIAL_RESPONSE = "partial_response"
    SCHEMA = "schema"
    CHECKSUM = "checksum"
    CHECKPOINT_REGRESSION = "checkpoint_regression"
    CONFIG = "config"


class ConnectorError(Exception):
    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        status_code: int | None = None,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.attempts = attempts


class UpstreamTimeout(Exception):
    """Raised by a transport when the bounded request timeout elapses."""
