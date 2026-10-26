"""The learning Airflow track calls the production daily command."""

from pathlib import Path

from learn.pipeline import production_daily_args, run_production_daily
from ops.local_stack import COMPOSE_FILE

from openfactors.orchestration.daily import main

ROOT = Path(__file__).resolve().parents[1]
LEARN_COMPOSE = ROOT / "ops" / "compose" / "learn" / "docker-compose.yml"
LEARN_DAG = ROOT / "ops" / "compose" / "learn" / "dags" / "daily_pipeline.py"


def test_the_learning_task_is_the_production_daily_module() -> None:
    assert production_daily_args() == ["-m", "openfactors.orchestration.daily"]
    dag = LEARN_DAG.read_text(encoding="utf-8")
    assert "python -m openfactors.orchestration.daily" in dag
    assert "schedule=None" in dag
    assert "is_paused_upon_creation=True" in dag
    assert "promote" not in dag


def test_the_learning_run_returns_the_production_exit_code() -> None:
    assert main([]) == run_production_daily() == 1


def test_learning_airflow_stays_off_the_production_compose() -> None:
    production = COMPOSE_FILE.read_text(encoding="utf-8")
    learning = LEARN_COMPOSE.read_text(encoding="utf-8")
    assert "airflow" not in production.lower()
    assert "name: openfactors-learn" in learning
    assert "127.0.0.1:18080:8080" in learning
    assert "0.0.0.0:" not in learning
    assert "55430" not in learning
    assert "secrets.env" not in learning
    assert "/src:" in learning
