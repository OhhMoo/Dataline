# Runbook: batch window

The PolicyMaster claims batch sends one extract per night. Scheduler job
`nightly_extract` lands `data/landing/claims_extract/dt=YYYY-MM-DD/CLAIMS.DAT`
at 23:00 UTC; the file must be on the landing zone before the batch cutoff,
or the window opens without input.

Inside the window, `batch_load` runs `loader/load_extract.py` against the
landing partition (starts 23:45 UTC), then `dbt_build` rebuilds staging and
the warehouse at 23:55 UTC. All three should be green in `sched_state`;
investigate the first non-success run, not the last.

If `CLAIMS.DAT` is missing or its byte length is not a multiple of the
31-byte record length in `copybooks/CLAIMS.CPY`, the loader rejects the
partition — check the extract transmission before rerunning `batch_load`.
