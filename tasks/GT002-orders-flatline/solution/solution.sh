#!/usr/bin/env bash
# GT002 oracle solution - ships HIDDEN, to the grading zone only.
#
# The "sage agent" runs exactly this and nothing else. If it does not score full
# marks the TASK is misconfigured and does not enter the corpus - the cheapest
# task-validity check available, and it runs on every build.
#
#   WAREHOUSE=/var/warehouse/warehouse.duckdb WORKSPACE=/workspace ./solution.sh
#
# ORDER MATTERS AND IS THE WHOLE POINT. Step 1 makes the sink idempotent. Step 2
# replays. Doing 2 before 1 redelivers the 1,083 records the killed catch-up run
# had already written but never committed, and the natural cleanup for that -
# de-duplicating on order_id - destroys the 1,100 corrections the producer
# legitimately re-emitted during the outage.
set -euo pipefail

WAREHOUSE="${WAREHOUSE:-/var/warehouse/warehouse.duckdb}"
WORKSPACE="${WORKSPACE:-/workspace}"

echo "[oracle] 1/4  making the sink idempotent BEFORE anything is replayed"
mkdir -p "$WORKSPACE/pipelines"

cat > "$WORKSPACE/pipelines/orders_quarantine.sql" <<'SQL'
-- orders_stream_quarantine :: task 2 of 4, orders DAG, every 30 minutes
--
-- Was catching only the schema contract (a null order_id). A producer build
-- rolled back on 2026-03-08 emits qty as a locale-formatted decimal, which
-- passes the schema check and then raises in the sink's cast - so the record was
-- never routed here and the whole batch died on it instead. Quarantine now
-- covers anything the sink cannot decode, which is the class rather than the one
-- instance. Still safe to re-run.
INSERT INTO raw.orders_dlq
SELECT t.offset_id,
       t.event_ts,
       t.payload,
       CASE WHEN json_extract_string(t.payload, '$.order_id') IS NULL
            THEN 'schema: required field order_id is null'
            ELSE 'decode: could not convert qty '
                 || json_extract_string(t.payload, '$.qty') || ' to INTEGER' END,
       now()
FROM raw.orders_topic t
WHERE t.offset_id >= (SELECT committed_offset FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND t.offset_id <  (SELECT committed_offset + 2000 FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND (json_extract_string(t.payload, '$.order_id') IS NULL
       OR TRY_CAST(json_extract_string(t.payload, '$.qty') AS INTEGER) IS NULL
       OR TRY_CAST(json_extract_string(t.payload, '$.unit_price') AS DECIMAL(10,2)) IS NULL)
  AND NOT EXISTS (SELECT 1 FROM raw.orders_dlq d WHERE d.offset_id = t.offset_id);
SQL

cat > "$WORKSPACE/pipelines/orders_sink.sql" <<'SQL'
-- orders_stream_sink :: task 3 of 4, orders DAG, every 30 minutes
--
-- WAS a plain INSERT .. SELECT with no merge key. The broker is at-least-once
-- and the offset commit is a separate statement from this one, so any replay,
-- rebalance or run killed between the two redelivers records that are already in
-- the table - and this statement would have written every one of them a second
-- time. It had never happened only because nothing had ever been replayed.
--
-- Two changes:
--   * anti-join on the business key (order_id, revision) so a redelivered record
--     is a no-op. revision is part of the key: the producer re-emits a corrected
--     order with the same order_id and a higher revision, and both rows are real.
--   * records the sink cannot decode are left to the quarantine step instead of
--     raising and abandoning the batch.
INSERT INTO warehouse.fct_orders
SELECT TRY_CAST(json_extract_string(t.payload, '$.order_id')    AS BIGINT)        AS order_id,
       TRY_CAST(json_extract_string(t.payload, '$.revision')    AS INTEGER)       AS revision,
       TRY_CAST(json_extract_string(t.payload, '$.order_dt')    AS DATE)          AS order_dt,
       TRY_CAST(json_extract_string(t.payload, '$.event_ts')    AS TIMESTAMP)     AS event_ts,
       TRY_CAST(json_extract_string(t.payload, '$.customer_id') AS BIGINT)        AS customer_id,
            json_extract_string(t.payload, '$.channel_code')                      AS channel_code,
            json_extract_string(t.payload, '$.sku')                               AS sku,
       TRY_CAST(json_extract_string(t.payload, '$.qty')         AS INTEGER)       AS qty,
       TRY_CAST(json_extract_string(t.payload, '$.unit_price')  AS DECIMAL(10,2)) AS unit_price,
       ROUND(TRY_CAST(json_extract_string(t.payload, '$.qty')        AS INTEGER)
           * TRY_CAST(json_extract_string(t.payload, '$.unit_price') AS DECIMAL(10,2)), 2)
                                                                ::DECIMAL(12,2)   AS order_amount,
       now()                                                                      AS load_ts,
       'stream-' || strftime(t.event_ts, '%Y%m%d-%H')                             AS batch_id
FROM raw.orders_topic t
WHERE t.offset_id >= (SELECT committed_offset FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND t.offset_id <  (SELECT committed_offset + 2000 FROM raw.consumer_offsets
                       WHERE consumer_group = 'orders_fct_sink')
  AND json_extract_string(t.payload, '$.order_id') IS NOT NULL
  AND TRY_CAST(json_extract_string(t.payload, '$.qty') AS INTEGER) IS NOT NULL
  AND TRY_CAST(json_extract_string(t.payload, '$.unit_price') AS DECIMAL(10,2)) IS NOT NULL
  AND NOT EXISTS (
        SELECT 1 FROM warehouse.fct_orders f
         WHERE f.order_id = TRY_CAST(json_extract_string(t.payload,'$.order_id') AS BIGINT)
           AND f.revision = TRY_CAST(json_extract_string(t.payload,'$.revision') AS INTEGER));
SQL

echo "[oracle] 2/4  replaying the gap from the committed offset"
python - "$WAREHOUSE" "$WORKSPACE" <<'PY'
import sys, duckdb
from pathlib import Path
db, ws = sys.argv[1], Path(sys.argv[2])

# NEVER open the warehouse file directly. It is deployed as
# /var/warehouse/warehouse.duckdb and the schema inside it is also called
# `warehouse`, so DuckDB names the catalog after the file stem and every
# two-part reference raises:
#     Binder Error: Ambiguous reference to catalog or schema "warehouse"
# Attach under an alias and set the search path, exactly as the gateway does
# (gateway/src/alkera_gateway/sql.py, Warehouse.connect). GT001's oracle opened
# the file directly, aborted on its first statement at the deployed path, and
# nothing caught it because its baselines ran against a file called
# world.duckdb - a stem that does not collide.
con = duckdb.connect(":memory:", config={"threads": 1})
con.execute(f"ATTACH '{Path(db).as_posix()}' AS wh")
con.execute("USE wh.warehouse")

quar = (ws / "pipelines" / "orders_quarantine.sql").read_text(encoding="utf-8")
sink = (ws / "pipelines" / "orders_sink.sql").read_text(encoding="utf-8")
commit = (ws / "pipelines" / "orders_commit.sql").read_text(encoding="utf-8")

# Resume from the committed offset, not from log_start. The topic retains thirty
# days; fct_orders holds ninety. Resetting to earliest would replay twenty-six
# days that are already loaded, and rebuilding the table from the topic would
# lose the sixty days the topic can no longer reach.
runs = 0
while True:
    lag = con.execute("SELECT lag FROM raw.v_consumer_lag "
                      "WHERE consumer_group = 'orders_fct_sink'").fetchone()[0]
    if lag <= 0:
        break
    con.execute(quar)
    con.execute(sink)
    con.execute(commit)
    runs += 1
    if runs > 500:
        raise SystemExit("replay did not converge")

dup = con.execute("""SELECT COUNT(*) FROM (
    SELECT order_id, revision FROM warehouse.fct_orders
    GROUP BY 1,2 HAVING COUNT(*) > 1)""").fetchone()[0]
revs = con.execute("SELECT COUNT(*) FROM (SELECT order_id FROM warehouse.fct_orders "
                   "WHERE revision = 2 GROUP BY 1)").fetchone()[0]
dlq = con.execute("SELECT COUNT(*) FROM raw.orders_dlq "
                  "WHERE dead_lettered_at >= (SELECT MAX(dead_lettered_at) "
                  "FROM raw.orders_dlq) - INTERVAL 1 HOUR").fetchone()[0]
assert dup == 0, f"oracle left {dup} duplicate business keys"
assert revs == 1240, f"oracle left {revs} corrected orders, expected 1240"
print(f"         caught up in {runs} poll windows; {dlq} records dead-lettered; "
      f"0 duplicates; {revs} corrections intact")
con.close()
PY

echo "[oracle] 3/4  leaving a regression test and an alert that would fire"
mkdir -p "$WORKSPACE/dbt_project/models/warehouse"
cat > "$WORKSPACE/dbt_project/models/warehouse/schema.yml" <<'YML'
version: 2
models:
  - name: kpi_orders_daily
    description: Daily order counts and value by channel group.
sources:
  - name: warehouse
    tables:
      - name: fct_orders
        description: >
          One row per order revision. A replay through the old append-only sink
          would duplicate the business key; this test fails on that state and
          passes after the sink was made idempotent.
        tests:
          - unique_combination_of_columns:
              combination_of_columns: [order_id, revision]
        columns:
          - name: order_id
            tests: [not_null]
          - name: revision
            tests: [not_null]
YML

cat > "$WORKSPACE/pipelines/CHECKS.sql" <<'SQL'
-- Correctness monitors for the orders stream. The orders DAG runs with
-- on_failure_policy: ignore, so a statement that raises is logged and the task
-- still reports success. Freshness was green for four days while the table did
-- not move. These two assertions are what should have paged someone.

-- 1. consumer lag must not exceed one poll window for more than an hour
SELECT 'orders_fct_sink lag ' || lag AS alert
FROM raw.v_consumer_lag
WHERE consumer_group = 'orders_fct_sink'
  AND lag > 2000;

-- 2. the business key must be unique - the sink is idempotent, prove it stays so
SELECT 'duplicate business key ' || order_id || '/' || revision AS alert
FROM warehouse.fct_orders
GROUP BY order_id, revision
HAVING COUNT(*) > 1;

-- 3. anything the pipeline could not decode must be visible, not silent
SELECT 'undecodable records awaiting review: ' || COUNT(*) AS alert
FROM raw.orders_dlq
WHERE dead_lettered_at >= now() - INTERVAL 1 DAY
HAVING COUNT(*) > 0;
SQL

echo "[oracle] 4/4  writing the final report"
cat > "$WORKSPACE/final_report.json" <<'JSON'
{
  "primary_cause": "The orders_fct_sink consumer group has been stalled since 05:13 on 2026-03-09. A producer build that was rolled back on the 8th emits qty as a locale-formatted decimal - the first bad payload is at offset 4127713 and carries \"qty\":\"2,0\" - and pipelines/orders_sink.sql casts that field strictly, so the INSERT raises a conversion error, DuckDB abandons the whole statement, no rows are written and orders_stream_commit never runs. The consumer re-polls the same 2,000-offset window every thirty minutes and dies on the same record. The DAG carries on_failure_policy: ignore, so every run is logged as a success with '0 records' and nothing alerted. Orders did not stop; the pipeline stopped and reported that it had not.",
  "damage_window": ["2026-03-09", "2026-03-13"],
  "supporting_evidence": [
    "raw.v_consumer_lag: orders_fct_sink committed_offset 4126000 against log_end_offset 4150411 - lag 24411, committed_at frozen at 2026-03-09 05:13:12",
    "warehouse.fct_orders: max(event_ts) is 2026-03-09 05:13:12 and no row has been added since",
    "raw.orders_topic: 17 payloads at and after offset 4127713 fail TRY_CAST(qty AS INTEGER); a further 9 have no order_id at all",
    "/var/sched/state.json: every run from 05:30 on 2026-03-09 onward reports success with 'orders_stream_sink: 0 records', and the DAG's on_failure_policy is ignore",
    "pipelines/orders_sink.sql casts json_extract_string(payload,'$.qty') to INTEGER with no fallback, and the quarantine step ahead of it only catches a null order_id, so a value that breaks the cast is never routed to the dead-letter table"
  ],
  "repair_strategy": "Made the sink idempotent BEFORE replaying anything, because the replay is what would have detonated it. pipelines/orders_sink.sql was a plain INSERT .. SELECT with no merge key. Nothing had ever been redelivered to it so it had never duplicated a row and there is no duplicate anywhere in the warehouse to find - but the topic is at-least-once and, more concretely, the producer that was rolled back on the 8th resumed from a stale checkpoint and re-sent 1,300 orders from 2026-02-04 that are already in fct_orders. Their original log records aged out of the 30-day retention window months ago, so those re-sends are not duplicates of anything visible in the topic either; they are simply pending, and replaying the gap hands them to the sink a second time. Once duplicated, the obvious cleanup - de-duplicating on order_id - would also have destroyed the 1,100 corrections the producer legitimately re-emitted during the outage, which carry the same order_id and a higher revision. So: (1) anti-join the sink on the business key (order_id, revision), which makes a redelivered record a no-op while keeping both revisions of a corrected order; (2) widen the quarantine step from 'null order_id' to 'anything the sink cannot decode', so all 26 bad records land in raw.orders_dlq with their original offset and payload instead of killing the batch; (3) replay from the committed offset, not from log_start - the topic retains 30 days and fct_orders holds 90, so resetting to earliest would replay weeks of already-loaded data and rebuilding the table from the topic would lose 282,221 rows permanently; (4) added a uniqueness test on (order_id, revision) and three correctness monitors in pipelines/CHECKS.sql.",
  "remaining_risks": [
    "on_failure_policy: ignore is still set on the orders_stream DAG. The new monitors detect a stall after the fact; they do not make a raised statement page anyone. That policy is why four days passed silently and it should be changed at the DAG level.",
    "The producer is still emitting locale-formatted decimals. 26 records are quarantined in raw.orders_dlq and are NOT in the fact table; someone needs to fix the producer and replay them from the dead-letter table before retention passes them.",
    "orders_sink.sql and orders_commit.sql are still separate statements with no shared transaction, so a run killed between them would leave rows loaded and the offset un-advanced, and the next run would redeliver them. The sink is now idempotent so this is no longer a correctness bug, but it is still the shape that would have made one.",
    "docs/runbooks/orders_stream.md claims the sink de-duplicates on the message key. It never did - the sink does not reference msg_key at all. The page should be corrected before it misleads the next person."
  ],
  "additional_causes": [
    "orders_sink.sql and orders_commit.sql are two separate statements with no transaction around them, so the write and the offset advance are not atomic. Nothing has triggered it, but it is the latent version of the same redelivery hazard and it is worth closing.",
    "channel_dim_refresh on the asset platform has been failing since 2026-03-07 with an expired service account key, so warehouse.dim_channel is stale and the three reseller channels that went live on 2026-03-08 read as 'unmapped' on the board. A genuine defect that does feed the same KPI view, but it started a day before the flatline, it affects labels rather than rows, and it explains no missing orders. Its asset job materialises dim_channel by full replace, which would drop the four hand-curated mappings, so it should not be force-run as-is.",
    "The orders_stream DAG runs with on_failure_policy: ignore, so a statement that raises is logged and the task still reports success with a record count. Freshness monitoring stayed green for four days while correctness was unmonitored."
  ]
}
JSON

echo "[oracle] done"
