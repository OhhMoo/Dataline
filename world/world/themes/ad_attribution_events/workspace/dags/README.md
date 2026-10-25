# Deployed schedule

`ad_attribution_nightly` starts at 01:00 UTC after the 00:30 export. Its task
IDs match `sched_state`: `ingest_events`, `attribute_conversions`, and
`lock_partner_billing`. The repository DAG is documentation of the deployed
schedule; the scheduler runs the same commands through `run.sh`.
