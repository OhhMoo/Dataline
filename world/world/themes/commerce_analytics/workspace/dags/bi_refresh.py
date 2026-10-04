# bi_refresh: refresh every dashboard at 06:00 UTC.
#
# Its own DAG, on its own clock. It does NOT wait for commerce_nightly: on a
# night the extract is late, the load and dbt finish on yesterday's data, and
# this refreshes the dashboards on yesterday's data at 06:00 and succeeds.
# Running it on dbt_build's success instead is ANA-219.
#
# task_id is the job name in sched_state - keep it that way.
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

WORKSPACE = "/workspace"

dag = DAG(
    "bi_refresh",
    default_args={
        "owner": "analytics-eng",
        "retries": 1,
        "retry_delay": timedelta(minutes=20),
        "email_on_failure": True,
        "email": ["analytics-oncall@example.com"],
    },
    # 06:00 UTC: three hours after the nightly build normally finishes, and
    # before anybody in EMEA opens a dashboard.
    schedule="0 6 * * *",
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["commerce", "bi"],
)

bi_refresh = BashOperator(
    task_id="bi_refresh",
    bash_command=f"cd {WORKSPACE} && ./run.sh refresh",
    execution_timeout=timedelta(minutes=30),
    dag=dag,
)
