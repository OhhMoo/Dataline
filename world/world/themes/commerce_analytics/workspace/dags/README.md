# dags/

Airflow DAGs for this project. One file per pipeline, `dag_id` equal to the
file stem.

| dag | schedule | tasks |
|---|---|---|
| `commerce_nightly` | 02:30 UTC | `daily_load` >> `dbt_build` |
| `bi_refresh` | 06:00 UTC | `bi_refresh` |

Two DAGs, two clocks. The dashboard refresh is not downstream of the build -
it runs at 06:00 whatever the build did. That is the gap ANA-219 is about, and
it is why a late extract can leave every job green and every dashboard a day
behind.

## What actually runs these

**Not this checkout.** `dags/` is the source of the pipeline; the scheduler
runs a *deployed* copy of it. So:

- editing a file here changes what gets deployed, not what runs tonight;
- the run history you can see - `var/sched/state.json`, the `sched_*` tools -
  is the record of the deployed pipeline, which is the thing to trust when
  the two disagree;
- `./run.sh dag-check` compares the two and tells you where they have drifted.

Task ids are the job names the scheduler records, deliberately: that is what
makes the comparison possible at all.

## The gap between tasks

The scheduler records a fixed offset between the start of `daily_load` and the
start of `dbt_build` (ten minutes). The DAG does not encode that offset -
downstream tasks start as soon as upstream ones finish. If a run record's
arithmetic looks off by a few minutes, that is why.

## Local

Airflow is not installed on the analysis boxes. `./run.sh dag-check` reads
these files with `ast` rather than importing them, so it works anywhere.
