# Deployment DAG

`subscription_finance.py` documents the production dependency order. Airflow is not required for local development; `./run.sh ingest`, `./run.sh fold`, and `./run.sh close` invoke the same loader and dbt selectors directly. The world scheduler renders its own dbt profile and virtual execution dates.
