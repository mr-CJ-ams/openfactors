"""Learning DAG. It is paused, unscheduled, and calls the production command."""

from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="openfactors_daily_learning",
    schedule=None,
    start_date=datetime(2026, 10, 26),
    catchup=False,
    is_paused_upon_creation=True,
) as dag:
    BashOperator(
        task_id="daily",
        bash_command="python -m openfactors.orchestration.daily",
    )
