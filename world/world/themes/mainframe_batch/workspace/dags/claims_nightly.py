"""CLAIMS NIGHTLY BATCH.

Verifies the transmitted extract, waits out the settle window, loads it, and
rebuilds the warehouse.

Task ids are the job names recorded by the scheduler. They are referenced by
name in docs/runbook_batch_window.md and in the operations alert routing.
Renaming one is a change, not a refactor.

Change control: CR-2025-203 (settle window 30 -> 45 minutes).
"""
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.sensors.time_delta import TimeDeltaSensor

WORKSPACE = "/workspace"

# Settle window. Held in conf/claims.cfg as [landing] settle_minutes; repeated
# here because the DAG is deployed separately from the repository and cannot
# read that file at parse time. If you change one, change both.
SETTLE_MINUTES = 45

DEFAULT_ARGS = {
    "owner": "batch-ops",
    "email": ["batch-ops@example.invalid"],
    "email_on_failure": True,
    "email_on_retry": False,
    # The extract is cut once. A retry cannot make a missing transmission
    # appear, so retrying is only useful for the load and the build.
    "retries": 1,
    "retry_delay": timedelta(minutes=15),
    "depends_on_past": True,
}

with DAG(
    dag_id="claims_nightly",
    description="POLICYMASTER claims extract -> raw -> staging -> warehouse",
    default_args=DEFAULT_ARGS,
    # The batch window opens at 23:00 on the day the extract is cut.
    schedule_interval="0 23 * * *",
    start_date=datetime(2025, 9, 1),
    catchup=False,
    max_active_runs=1,
    tags=["claims", "batch", "tier-1"],
) as dag:

    # Presence and record-length check over the landing partitions. A short
    # read is rejected here; the copybook record is 31 bytes fixed block with
    # no delimiters, so a truncated file is indistinguishable from a small one
    # once it has been parsed.
    nightly_extract = BashOperator(
        task_id="nightly_extract",
        bash_command=f"cd {WORKSPACE} && ./run.sh verify",
        execution_timeout=timedelta(minutes=20),
    )

    # Connect:Direct reports complete before the file is closed on the
    # receiving side. The settle window is the difference.
    settle_window = TimeDeltaSensor(
        task_id="settle_window",
        delta=timedelta(minutes=SETTLE_MINUTES),
        mode="reschedule",
    )

    batch_load = BashOperator(
        task_id="batch_load",
        bash_command=f"cd {WORKSPACE} && ./run.sh load",
        execution_timeout=timedelta(minutes=60),
    )

    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command=f"cd {WORKSPACE} && ./run.sh build",
        execution_timeout=timedelta(minutes=45),
    )

    nightly_extract >> settle_window >> batch_load >> dbt_build
