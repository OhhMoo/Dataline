# dags

One DAG, `claims_nightly`, deployed to the scheduler from this directory by
the release pipeline. **The copy in this checkout does not run anything.** The
scheduler runs the deployed copy and supplies its own dbt profile; a change
here takes effect at the next release, not tonight.

Task ids are the job names in the recorded run history on purpose. That is
what lets `./run.sh dag-check` compare the two and report a task that has been
renamed on one side only.

`settle_window` is a sensor, not a job. It has no run record, and dag-check
knows not to look for one.

Airflow is not installed on the operator workstations. `./run.sh dag-check`
reads the DAG source with `ast` rather than importing it.
