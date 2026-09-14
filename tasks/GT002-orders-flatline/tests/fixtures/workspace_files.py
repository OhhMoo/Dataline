#!/usr/bin/env python3
"""
GT002 agent-visible workspace, emitted rather than committed.

The pipeline SQL, the runbook and the scheduler history all describe the same
warehouse the generator builds. Committing them separately is how they drift, so
they are generated from one place and regenerated whenever the world is.

DEFECT B LIVES IN `SINK_SQL` BELOW. It is a plain INSERT..SELECT with no merge
key and no anti-join. Nothing in the warehouse records that fact, because nothing
has ever been redelivered to it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE_README = """# Commercial data platform

- `pipelines/` - the orders stream pipeline, executed by the SQL runner.
- `dbt_project/` - the reporting models built on top of `warehouse.fct_orders`.
- `docs/` - runbooks and the data dictionary. Freshness varies.
- `asset_platform/` - read-only mirror of the *other* orchestrator's run state.
  That platform belongs to the marketing data team; it is mirrored here only so
  lineage tooling can see across the two.

The warehouse is reachable through the SQL tools, not through the filesystem.
"""

PIPELINES_README = """# The orders stream pipeline

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
"""

QUARANTINE_SQL = """-- orders_stream_quarantine :: task 2 of 4, orders DAG, every 30 minutes
--
-- Routes records that break the schema contract into the dead-letter table so
-- that one bad producer message cannot hold the whole topic up. Safe to re-run:
-- a record already dead-lettered is not dead-lettered a second time.
INSERT INTO raw.orders_dlq
SELECT t.offset_id,
       t.event_ts,
       t.payload,
       'schema: required field order_id is null',
       now()
FROM raw.orders_topic t
WHERE t.offset_id >= (SELECT committed_offset FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND t.offset_id <  (SELECT committed_offset + 2000 FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND json_extract_string(t.payload, '$.order_id') IS NULL
  AND NOT EXISTS (SELECT 1 FROM raw.orders_dlq d WHERE d.offset_id = t.offset_id);
"""

SINK_SQL = """-- orders_stream_sink :: task 3 of 4, orders DAG, every 30 minutes
--
-- Loads one poll window of orders.v2 into the orders fact table. Takes no
-- parameters: the window is read from raw.consumer_offsets, so this file is the
-- same on a scheduled run and on a manual catch-up.
INSERT INTO warehouse.fct_orders
SELECT CAST(json_extract_string(t.payload, '$.order_id')    AS BIGINT)        AS order_id,
       CAST(json_extract_string(t.payload, '$.revision')    AS INTEGER)       AS revision,
       CAST(json_extract_string(t.payload, '$.order_dt')    AS DATE)          AS order_dt,
       CAST(json_extract_string(t.payload, '$.event_ts')    AS TIMESTAMP)     AS event_ts,
       CAST(json_extract_string(t.payload, '$.customer_id') AS BIGINT)        AS customer_id,
            json_extract_string(t.payload, '$.channel_code')                  AS channel_code,
            json_extract_string(t.payload, '$.sku')                           AS sku,
       CAST(json_extract_string(t.payload, '$.qty')         AS INTEGER)       AS qty,
       CAST(json_extract_string(t.payload, '$.unit_price')  AS DECIMAL(10,2)) AS unit_price,
       ROUND(CAST(json_extract_string(t.payload, '$.qty')        AS INTEGER)
           * CAST(json_extract_string(t.payload, '$.unit_price') AS DECIMAL(10,2)), 2)
                                                            ::DECIMAL(12,2)   AS order_amount,
       now()                                                                  AS load_ts,
       'stream-' || strftime(t.event_ts, '%Y%m%d-%H')                         AS batch_id
FROM raw.orders_topic t
WHERE t.offset_id >= (SELECT committed_offset FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND t.offset_id <  (SELECT committed_offset + 2000 FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND json_extract_string(t.payload, '$.order_id') IS NOT NULL;
"""

COMMIT_SQL = """-- orders_stream_commit :: task 4 of 4, orders DAG, every 30 minutes
--
-- Advances the consumer group past the window the sink just processed.
UPDATE raw.consumer_offsets
SET committed_offset = LEAST(committed_offset + 2000,
                             (SELECT log_end_offset FROM raw.topic_meta
                               WHERE topic = 'orders.v2')),
    committed_at = now()
WHERE consumer_group = 'orders_fct_sink';
"""

RUNBOOK = """# Runbook - orders stream

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
"""

DATA_DICT = """# Data dictionary (extract)

## warehouse.fct_orders

One row per order **revision**. The producer re-emits an order with an
incremented `revision` when a correction is issued. Both the original and the
correction are kept; reporting reads the highest revision per order. The
business key is therefore `(order_id, revision)`, not `order_id` on its own.

| column | type | note |
|---|---|---|
| order_id | BIGINT | business key part 1 |
| revision | INTEGER | business key part 2, `1` on first emission |
| order_dt | DATE | date of the original order; stable across revisions |
| event_ts | TIMESTAMP | when the producer emitted this revision |
| customer_id | BIGINT | |
| channel_code | VARCHAR | joins `warehouse.dim_channel` |
| sku | VARCHAR | |
| qty | INTEGER | |
| unit_price | DECIMAL(10,2) | |
| order_amount | DECIMAL(12,2) | `qty * unit_price` |
| load_ts | TIMESTAMP | audit only, not part of the record |
| batch_id | VARCHAR | audit only, not part of the record |

`fct_orders` holds ninety days. The topic behind it holds thirty. The table is
the only copy of anything older than the retention horizon.

## warehouse.dim_channel

Refreshed by `channel_dim_refresh` on the asset platform, not from this repo.
Rows with `is_curated = true` are hand-maintained mappings that the refresh does
not produce and must not be dropped.
"""

KPI_MODEL = """{{ config(materialized='view') }}

-- Daily order counts and value by channel group. This is what the exec revenue
-- board reads.
select o.order_dt                            as dt,
       coalesce(c.channel_group, 'unmapped') as channel_group,
       count(*)                              as order_count,
       round(sum(o.order_amount), 2)         as order_value
from {{ source('warehouse', 'fct_orders') }} o
left join {{ source('warehouse', 'dim_channel') }} c
       on c.channel_code = o.channel_code
group by 1, 2
"""

ASSET_RUNS = {
    "platform": "asset_platform",
    "note": "read-only mirror of the marketing data team's orchestrator",
    "mirrored_at": "2026-03-13T08:55:00Z",
    "assets": [
        {
            "asset": "channel_dim_refresh",
            "target": "warehouse.dim_channel",
            "schedule": "daily 05:00",
            "last_success": "2026-03-07T05:00:00Z",
            "last_attempt": "2026-03-13T05:00:00Z",
            "status": "failed",
            "error": "AuthError: service account key expired 2026-03-07",
            "materialisation": "full replace of warehouse.dim_channel from the "
                               "channel master export",
        },
        {
            "asset": "marketing_spend_daily",
            "target": "warehouse.fct_spend",
            "schedule": "daily 05:30",
            "last_success": "2026-03-13T05:30:00Z",
            "last_attempt": "2026-03-13T05:30:00Z",
            "status": "success",
        },
    ],
}


def scheduler_state() -> dict:
    """Run history for /var/sched/state.json.

    Green everywhere. That is the whole point: `on_failure_policy: ignore` means a
    statement that raises is logged and the task still reports success, so four
    days of doing nothing look exactly like four days of working.

    The run summary says "0 records" and nothing else - F125's mitigation, which
    asked that the failing micro-batch log nothing useful. There is deliberately
    no failed run and no maintenance window here: an earlier build had one whose
    detail line said the sink "had already committed to the warehouse", which
    disclosed the dormant defect's trigger in plain English (F624).
    """
    stall = datetime(2026, 3, 9, 5, 30, 0)
    runs = []
    t = datetime(2026, 3, 3, 0, 0, 0)
    end = datetime(2026, 3, 13, 9, 0, 0)
    while t < end:
        if t >= stall:
            detail = "orders_stream_sink: 0 records"
        else:
            detail = "orders_stream_sink: %d records" % (
                80 + (t.hour * 37 + t.minute) % 40)
        runs.append({"job": "orders_stream",
                     "vts_start": t.isoformat() + "Z",
                     "vts_end": (t + timedelta(seconds=52)).isoformat() + "Z",
                     "status": "success", "detail": detail})
        t += timedelta(minutes=30)
    return {
        "vnow": "2026-03-13T09:00:00Z",
        "jobs": {
            "orders_stream": {"command": "run_pipeline_dir pipelines/",
                              "schedule": "*/30 * * * *", "duration_s": 52,
                              "on_failure_policy": "ignore"},
            "dbt_build": {"command": "dbt build --select warehouse",
                          "schedule": "15 2 * * *", "duration_s": 140,
                          "on_failure_policy": "alert"},
        },
        "runs": runs,
    }


FILES = {
    "README.md": WORKSPACE_README,
    "pipelines/README.md": PIPELINES_README,
    "pipelines/orders_quarantine.sql": QUARANTINE_SQL,
    "pipelines/orders_sink.sql": SINK_SQL,
    "pipelines/orders_commit.sql": COMMIT_SQL,
    "docs/runbooks/orders_stream.md": RUNBOOK,
    "docs/data_dictionary.md": DATA_DICT,
    "dbt_project/models/warehouse/kpi_orders_daily.sql": KPI_MODEL,
}

# The path the idempotency probe executes. Wired into the DAG, documented in
# pipelines/README.md, and named here so the grader and the world cannot drift.
SINK_PATH = "pipelines/orders_sink.sql"


def write_workspace(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    (root / "asset_platform").mkdir(parents=True, exist_ok=True)
    (root / "asset_platform" / "asset_runs.json").write_text(
        json.dumps(ASSET_RUNS, indent=2) + "\n", encoding="utf-8", newline="\n")


def write_scheduler(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scheduler_state(), indent=2) + "\n",
                    encoding="utf-8", newline="\n")
