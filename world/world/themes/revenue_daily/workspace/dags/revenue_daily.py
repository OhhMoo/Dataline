"""Daily revenue pipeline.

Lands the previous day's order partition into `raw.daily_revenue`, then
rebuilds staging and the warehouse with dbt.

Owner: data-platform. Runbook: docs/runbook_daily_load.md
"""
from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator

WORKSPACE = "/workspace"

DEFAULT_ARGS = {
    "owner": "data-platform",
    "retries": 1,
    "retry_delay": timedelta(minutes=10),
    "email_on_failure": False,
    "depends_on_past": False,
}

with DAG(
    dag_id="revenue_daily",
    description="daily order landing -> raw -> staging -> warehouse",
    schedule="0 1 * * *",
    start_date=pendulum.datetime(2025, 9, 5, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["revenue", "daily", "tier-1"],
) as dag:

    # Full reload of raw from the landing zone. The landing zone is immutable,
    # so re-running this task is safe at any point in the day; that is the
    # reason it is a reload rather than an append.
    daily_load = BashOperator(
        task_id="daily_load",
        bash_command=f"cd {WORKSPACE} && ./run.sh load",
        execution_timeout=timedelta(minutes=15),
    )

    # `build`, not `run`: the tests in models/*/schema.yml are the only thing
    # standing between a bad partition and the dashboard.
    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command=f"cd {WORKSPACE} && ./run.sh build",
        execution_timeout=timedelta(minutes=30),
    )

    daily_load >> dbt_build
