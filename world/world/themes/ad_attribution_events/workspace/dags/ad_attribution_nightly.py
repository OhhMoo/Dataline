"""Nightly ad-event ingestion, attribution, and partner billing close."""
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

WORKSPACE = "/workspace"

with DAG(
    dag_id="ad_attribution_nightly",
    description="ad event export -> raw -> attribution -> partner payout lock",
    schedule="0 1 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"owner": "attribution-eng", "retries": 1,
                  "retry_delay": timedelta(minutes=10)},
    tags=["attribution", "partner-billing"],
) as dag:
    ingest_events = BashOperator(
        task_id="ingest_events",
        bash_command=f"cd {WORKSPACE} && ./run.sh load",
        execution_timeout=timedelta(minutes=15),
    )
    attribute_conversions = BashOperator(
        task_id="attribute_conversions",
        bash_command=f"cd {WORKSPACE} && ./run.sh attribute",
        execution_timeout=timedelta(minutes=30),
    )
    lock_partner_billing = BashOperator(
        task_id="lock_partner_billing",
        bash_command=f"cd {WORKSPACE} && ./run.sh lock",
        execution_timeout=timedelta(minutes=20),
    )
    ingest_events >> attribute_conversions >> lock_partner_billing
