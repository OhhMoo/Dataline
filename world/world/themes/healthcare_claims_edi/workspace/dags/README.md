# Deployed schedule

`claims_nightly.py` is the source representation of the deployed nightly DAG.
Task IDs match scheduler job names. The platform scheduler supplies virtual
execution dates and dependency slots; Airflow is not required for local dbt
development. Keep task IDs and the dependency chain aligned with `sched_list`.
