"""Run the production daily command from the learning track.

The learning track does not define its own steps. It calls the production
module and returns that process's exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DAILY_MODULE = "openfactors.orchestration.daily"


def production_daily_args() -> list[str]:
    return ["-m", PRODUCTION_DAILY_MODULE]


def production_daily_command() -> list[str]:
    return [sys.executable, *production_daily_args()]


def run_production_daily() -> int:
    completed = subprocess.run(
        production_daily_command(),
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    prefix = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = prefix if not current else prefix + os.pathsep + current
    return env
