from datetime import datetime, timezone

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="subscription_finance",
    schedule="0 1 * * *",
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    tags=["finance", "revrec"],
) as dag:
    ingest_webhooks = BashOperator(
        task_id="ingest_webhooks", bash_command="./run.sh ingest"
    )
    fold_subscriptions = BashOperator(
        task_id="fold_subscriptions", bash_command="./run.sh fold"
    )
    revrec_window_close = BashOperator(
        task_id="revrec_window_close", bash_command="./run.sh close"
    )
    ingest_webhooks >> fold_subscriptions >> revrec_window_close
