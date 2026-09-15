# vendor feeds: poll the drop folders, load what fully landed, rebuild dbt.
#
# task_ids here are the job names in sched_state. keep them that way or the
# on-call runbook stops matching the alerts.
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

WORKSPACE = "/workspace"

default_args = {
    "owner": "kesh",
    "retries": 2,
    "retry_delay": timedelta(minutes=7),   # acme's sftp drops the connection a lot
    "email_on_failure": True,
    "email": ["data-oncall@example.invalid"],
}

dag = DAG(
    "vendor_feeds",
    default_args=default_args,
    # 05:30 UTC. acme cut the file on their midnight and they are ~2h behind,
    # so anything earlier finds an empty folder half the week.
    schedule_interval="30 5 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["vendor", "sftp"],
)

# fails if no vendor has written a .done sentinel. a csv without a sentinel is
# a transfer in flight, not a delivery.
sftp_poll = BashOperator(
    task_id="sftp_poll",
    bash_command=f"cd {WORKSPACE} && ./run.sh poll",
    dag=dag,
)

# loader is overwrite-by-filename, so re-running it is safe. it is NOT safe if
# somebody flips --on-redelivery to append; see docs/runbook_vendor_feeds.md.
vendor_load = BashOperator(
    task_id="vendor_load",
    bash_command=f"cd {WORKSPACE} && ./run.sh load",
    execution_timeout=timedelta(minutes=20),
    dag=dag,
)

dbt_build = BashOperator(
    task_id="dbt_build",
    bash_command=f"cd {WORKSPACE} && ./run.sh build",
    execution_timeout=timedelta(minutes=30),
    dag=dag,
)

sftp_poll >> vendor_load >> dbt_build
