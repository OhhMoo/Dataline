# Runbook: daily_load

Scheduler job `daily_load` lands one day partition into `raw.daily_revenue`
(runs 01:00 UTC), followed by `dbt_build` at 01:05 UTC. Both should be
green in `sched_state`; investigate the first non-success run, not the last.
