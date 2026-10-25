from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

WORKSPACE = "/workspace"

default_args = {
    "owner": "metering-platform",
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
    "email_on_failure": True,
    "email": ["metering-oncall@example.invalid"],
}

dag = DAG(
    "iot_billing",
    default_args=default_args,
    schedule_interval="0 1 * * *",
    start_date=datetime(2026, 1, 2),
    catchup=False,
    max_active_runs=1,
    tags=["iot", "metering", "billing"],
)

load_usage = BashOperator(
    task_id="load_usage",
    bash_command=f"cd {WORKSPACE} && ./run.sh load",
    execution_timeout=timedelta(minutes=20),
    dag=dag,
)

finalize_billing = BashOperator(
    task_id="finalize_billing",
    bash_command=f"cd {WORKSPACE} && ./run.sh build",
    execution_timeout=timedelta(minutes=30),
    dag=dag,
)

load_usage >> finalize_billing
