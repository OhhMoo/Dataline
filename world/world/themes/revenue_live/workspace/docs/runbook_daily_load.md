# Runbook: daily_load

Scheduler job `daily_load` appends every landing partition newer than
`raw.load_checkpoint` to `raw.daily_revenue` and moves the checkpoint
(runs 01:00 UTC), followed by `dbt_build` at 01:05 UTC. Both should be
green in `sched_state`; investigate the first non-success run, not the last.
