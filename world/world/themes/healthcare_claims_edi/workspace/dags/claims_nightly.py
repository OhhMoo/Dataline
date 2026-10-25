from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

WORKSPACE = "/workspace"

default_args = {
    "owner": "claims-data-platform",
    "retries": 1,
    "retry_delay": timedelta(minutes=10),
    "email_on_failure": True,
    "email": ["claims-data-oncall@example.com"],
}

dag = DAG(
    "claims_nightly",
    default_args=default_args,
    schedule="10 1 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["healthcare", "edi", "dbt"],
)

nightly_edi_load = BashOperator(
    task_id="nightly_edi_load",
    bash_command=f"cd {WORKSPACE} && ./run.sh load",
    execution_timeout=timedelta(minutes=15),
    dag=dag,
)
adjudication_build = BashOperator(
    task_id="adjudication_build",
    bash_command=(
        f"cd {WORKSPACE} && dbt build --project-dir dbt_project "
        "--select fct_adjudicated_claims dim_provider"
    ),
    execution_timeout=timedelta(minutes=20),
    dag=dag,
)
settlement_handoffs = BashOperator(
    task_id="settlement_handoffs",
    bash_command=(
        f"cd {WORKSPACE} && dbt build --project-dir dbt_project "
        "--select actuarial_reserve_handoff provider_capitation_settlement"
    ),
    execution_timeout=timedelta(minutes=20),
    dag=dag,
)
cms_window_close = BashOperator(
    task_id="cms_window_close",
    bash_command=(
        f"cd {WORKSPACE} && dbt build --project-dir dbt_project "
        "--select cms_submission_window"
    ),
    execution_timeout=timedelta(minutes=15),
    dag=dag,
)

nightly_edi_load >> adjudication_build >> settlement_handoffs >> cms_window_close
