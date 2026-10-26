"""Shared cleanup for release drills.

Published fact rows stay immutable in the product. A drill that has to remove
its own rows withdraws that release first, so a later source suspension cannot
treat the empty release as a servable replacement.
"""

from __future__ import annotations

import socket
import sys
import uuid
from collections.abc import Callable

import pytest
from ops.local_stack import ENVIRONMENTS, docker_available, up
from ops.migrate import psql
from ops.promote import restore_active_release

ENVIRONMENT = "test"


def ensure_isolated_database() -> None:
    """Start the disposable test database when Docker is available."""
    if not docker_available():
        pytest.skip("Docker is required for the isolated test database.")
    if _port_open():
        return
    up(ENVIRONMENT)
    if not _port_open():
        pytest.fail("The isolated test database did not start.")


def discard_test_releases(environment: str, release_ids: list[str]) -> None:
    """Withdraw drill releases, then remove their rows and pointer."""
    if not release_ids:
        return
    quoted = ", ".join(f"'{uuid.UUID(release_id)}'::uuid" for release_id in release_ids)
    completed = psql(
        environment,
        "of_admin",
        f"""
        BEGIN;
        DELETE FROM publish.active_dataset_release WHERE release_id IN ({quoted});
        UPDATE publish.dataset_releases
        SET status = 'withdrawn'
        WHERE release_id IN ({quoted})
          AND status = 'published';
        ALTER TABLE publish.daily_external_factors_ph DISABLE TRIGGER published_rows_immutable;
        DELETE FROM publish.daily_external_factors_ph WHERE release_id IN ({quoted});
        ALTER TABLE publish.daily_external_factors_ph ENABLE TRIGGER published_rows_immutable;
        COMMIT;
        """,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or "Test release cleanup failed.")


def restore_previous_release(environment: str, previous: str | None) -> None:
    restore_active_release(environment, previous)


def cleanup_steps(*actions: Callable[[], None]) -> None:
    """Run every cleanup step. A failed test keeps its original exception."""
    in_flight = sys.exception()
    errors: list[Exception] = []
    for action in actions:
        try:
            action()
        except Exception as exc:
            errors.append(exc)
    if not errors:
        return
    if in_flight is not None:
        for exc in errors:
            in_flight.add_note(f"{type(exc).__name__}: {exc}")
        return
    raise errors[0]


def _port_open() -> bool:
    port = int(ENVIRONMENTS[ENVIRONMENT]["postgres_port"])
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False
