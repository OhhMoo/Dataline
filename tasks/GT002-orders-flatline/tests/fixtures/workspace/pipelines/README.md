# The orders stream pipeline

`orders.v2` is the order event topic: one partition, thirty days of retention.
The `orders_fct_sink` consumer group owns loading it into `warehouse.fct_orders`.

## The DAG

Four tasks, every thirty minutes, in this order:

| # | task | file |
|---|---|---|
| 1 | `orders_stream_poll` | broker side, no file |
| 2 | `orders_stream_quarantine` | `pipelines/orders_quarantine.sql` |
| 3 | `orders_stream_sink` | `pipelines/orders_sink.sql` |
| 4 | `orders_stream_commit` | `pipelines/orders_commit.sql` |

The runner executes each `.sql` file **verbatim, as a single statement**, in the
order above, and abandons the rest of the run if one of them raises. The paths
are wired into the DAG definition: to change what a step does, change it inside
the file the step already points at. Renaming or deleting a file takes that step
out of the schedule with no warning and no error.

## Why a raised statement does not page anyone

`on_failure_policy: ignore`, set in 2025 so that a transient broker blip at 3am
would not wake the on-call. The task still reports success and the run summary
still says how many records it processed. Nobody has revisited it since.

## Replaying

There is no replay tool. Move `raw.consumer_offsets.committed_offset` back to
the offset you want to resume from and let the schedule catch up, or run the
files yourself in order until the lag reaches zero. `raw.topic_meta` carries
`log_start_offset`; resetting to that replays everything still retained, which
is thirty days, not the recent past.
