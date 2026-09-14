# Runbook - orders stream

*Owner: Data Platform. Last reviewed 2025-11-18.*

## Topology

`orders.v2` (1 partition, 30d retention) -> `orders_fct_sink` consumer group ->
`warehouse.fct_orders`. The step definitions live in `pipelines/`.

## Is it healthy?

```sql
SELECT * FROM raw.v_consumer_lag;
```

`lag` is `log_end_offset - committed_offset`. Under normal load it sits in the
hundreds. `orders_archive_legacy` is a decommissioned group left over from the
2025 archive migration - its lag grows forever and means nothing.

## Bad records

Records that break the schema contract are written to `raw.orders_dlq` with
their original offset and payload and then skipped. Check it after any producer
release.

## Redelivery

The broker is at-least-once, so the same record can arrive more than once after
a rebalance or a replay. That is handled at the sink, which de-duplicates on the
message key before writing.

> NOTE 2026-01-09: the pipeline was moved off the old Python consumer onto the
> SQL runner during the Q4 warehouse migration. This page has not been
> re-reviewed since. Read `pipelines/` for what actually runs today.

## Restarting after an outage

Put `committed_offset` back to where you want to resume and let the schedule
catch up. Do not reset to `log_start_offset` unless you intend to reprocess the
entire retained topic.
