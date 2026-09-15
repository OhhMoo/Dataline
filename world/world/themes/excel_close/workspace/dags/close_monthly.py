"""Monthly close import.

Re-imports every close workbook into `raw`, then rebuilds `fct_gl_monthly`.

Runs daily, not monthly. The close happens on day 1 of the following month but
workbooks are posted late often enough that waiting for a monthly trigger
meant the controller was the one who noticed. A daily full re-import is cheap
and it means a late correction to a prior period appears the next morning
without anyone re-running anything.

Owner: data, jointly with the GL team.
Sign-off is a human step and is deliberately not in this DAG — see
docs/signoff_checklist.md.
"""
from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator

WORKSPACE = "/workspace"

DEFAULT_ARGS = {
    "owner": "data",
    "email": ["gl-team@example.invalid"],
    "email_on_failure": True,
    # One retry, well after the hour. If the import failed because a workbook
    # was open in Excel, retrying two minutes later fails the same way.
    "retries": 1,
    "retry_delay": timedelta(minutes=30),
    "depends_on_past": False,
}

with DAG(
    dag_id="close_monthly",
    description="close workbooks -> raw -> staging -> fct_gl_monthly",
    schedule="0 8 * * *",
    start_date=pendulum.datetime(2025, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["finance", "close"],
) as dag:

    # Full re-import of every workbook. raw must equal the workbooks exactly;
    # there is no incremental path on purpose, because an incremental import
    # would need to decide what "changed" means for a spreadsheet.
    close_import = BashOperator(
        task_id="close_import",
        bash_command=f"cd {WORKSPACE} && ./run.sh import",
        execution_timeout=timedelta(minutes=20),
    )

    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command=f"cd {WORKSPACE} && ./run.sh build",
        execution_timeout=timedelta(minutes=30),
    )

    close_import >> dbt_build
