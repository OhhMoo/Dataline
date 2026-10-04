# commerce nightly: load the shop extract, rebuild the marts.
#
# task_ids are the job names in sched_state - keep them that way or the
# on-call runbook stops matching what the scheduler shows.
#
# The 06:00 dashboard refresh is NOT in this DAG: it is dags/bi_refresh.py, on
# its own clock, and it does not wait for dbt_build. Making it wait is ANA-219.
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

WORKSPACE = "/workspace"

default_args = {
    "owner": "analytics-eng",
    "retries": 1,
    "retry_delay": timedelta(minutes=10),
    "email_on_failure": True,
    "email": ["analytics-oncall@example.com"],
}

dag = DAG(
    "commerce_nightly",
    default_args=default_args,
    # 02:30 UTC: the platform's export lands at 02:00.
    schedule="30 2 * * *",
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["commerce", "dbt"],
)

# full reload of every partition on disk - re-running is safe
daily_load = BashOperator(
    task_id="daily_load",
    bash_command=f"cd {WORKSPACE} && ./run.sh load",
    execution_timeout=timedelta(minutes=20),
    dag=dag,
)

# starts ten minutes after the load is scheduled, not after it finishes - the
# platform scheduler runs it on its own slot
dbt_build = BashOperator(
    task_id="dbt_build",
    bash_command=f"cd {WORKSPACE} && ./run.sh build",
    execution_timeout=timedelta(minutes=30),
    dag=dag,
)

daily_load >> dbt_build
