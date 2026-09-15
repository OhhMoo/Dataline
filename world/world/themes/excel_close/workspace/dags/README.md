# dags/

`close_monthly` is the only pipeline. It is deployed to the scheduler from
this directory; the checkout you are reading does not run it, and the
scheduler supplies its own dbt profile rather than `config/profiles.yml`.

What this means in practice: a fix you make here is not live until it is
released. The run history in `sched_state` is the record of the deployed
pipeline, and when the two disagree the run history is the one that is true.
`./run.sh dag-check` reports where they disagree.

Task ids match the job names in that run history deliberately.

There is no sign-off task. Sign-off is the controller's, it is recorded in the
workbook, and putting a green tick in Airflow next to it would make it look
like a thing the pipeline did.
