# dags/

Airflow DAGs for this project. One file per pipeline, `dag_id` equal to the
file stem.

## What actually runs these

**Not this checkout.** `dags/` is the source of the pipeline; the scheduler
runs a *deployed* copy of it, and it renders its own `profiles.yml` with an
absolute warehouse path rather than reading `config/profiles.yml`. So:

- editing a file here changes what gets deployed, not what runs tonight;
- the run history you can see — `var/sched/state.json`, the `sched_*` tools —
  is the record of the deployed pipeline, which is the thing to trust when
  the two disagree;
- `./run.sh dag-check` compares the two and tells you where they have drifted.

Task ids are the job names the scheduler records, deliberately: that is what
makes the comparison possible at all.

## The gap between tasks

The scheduler records a fixed offset between the start of one job and the
start of the next (five minutes here). The DAG does not encode that offset —
downstream tasks start as soon as upstream ones finish. If you are reading a
run record and the arithmetic looks off by a few minutes, that is why.

## Local

Airflow is not installed on the analysis boxes. `./run.sh dag-check` reads
these files with `ast` rather than importing them, so it works anywhere.
