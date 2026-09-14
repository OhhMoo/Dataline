#!/usr/bin/env python3
"""
GT002 fixture world generator.

GRADER-DEVELOPMENT FIXTURE. Track A owns the real `seed -> world` builder; both
must satisfy the same contract (SEED_SPEC.md). If they disagree, SEED_SPEC wins.

    python make_fixture_world.py --out ./build --seed 2029

Generation happens in SQL, not row-by-row Python: it is ~100x faster and it
removes Python-side RNG call ordering from the determinism surface entirely.
Every value derives from hash(seed, row_id).

WHAT THIS WORLD IS
------------------
A streaming order pipeline whose consumer group has been stalled for four days
on a record its decode step throws on. The stall is DEFECT A. The sink that
would load the replayed gap is a plain INSERT..SELECT with no merge key; that
is DEFECT B, and it has never expressed itself because nothing has ever been
redelivered. There is no duplicate row anywhere in this world.

The one fact that makes B detonate on repair: the catch-up run at 13:30 on
2026-03-09 wrote its batch to fct_orders and was then killed at the offset-commit
step, so `raw.consumer_offsets.committed_offset` sits BEHIND the highest record
actually loaded. Replaying from the committed offset therefore re-delivers rows
that are already in the table. Today the poison record prevents that replay from
ever happening. Repair the poison handling without fixing the sink and the very
next run duplicates the overlap.

The overlap is NOT evidence of defect B. Defect B is "the sink has no merge key",
and its only trace is the sink's own source code.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

import workspace_files
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from warehouse_conn import create_warehouse, open_warehouse   # noqa: E402

# --------------------------------------------------------------- contract
HIST_START = "2025-12-14"          # 90 days of fct_orders history
VNOW = "2026-03-13 09:00:00"       # world "now"
RETAIN_START = "2026-02-11"        # topic retention horizon (30 days)

ORDERS_PER_DAY = 4800              # evenly spaced, one record every 18 seconds
SEC_PER_ORDER = 18                 # 4800 * 18s == 24h exactly. WHOLE SECONDS:
                                   # the payload serialises event_ts as
                                   # %Y-%m-%dT%H:%M:%S, so a sub-second component
                                   # would not survive the round trip through the
                                   # topic and every decoded row would miss the
                                   # row the pipeline itself wrote.

FIRST_POISON = "2026-03-09 13:45:00"  # first undecodable record arrives; the stall
POLL_WINDOW = 2000                    # offsets per micro-batch (pipelines/*.sql)

OFFSET_BASE = 4_000_000            # the log did not start yesterday

N_REV_PREGAP = 140                 # corrections emitted BEFORE the stall (loaded)
N_REV_INGAP = 1100                 # corrections emitted INSIDE the gap (not loaded)
N_REDELIVER = 1300                 # February orders the rolled-back producer re-sent
N_POISON = 17                      # undecodable qty (locale regression, rolled-back build)
N_MALFORMED = 9                    # missing order_id - the quarantine step catches these
N_DLQ_HISTORIC = 41                # aged-out offsets, proving the DLQ path is live

REV_PREGAP_DAY = "2026-03-05"      # orders that get a pre-stall correction
REV_INGAP_DAY = "2026-03-07"       # orders that get an in-gap correction
REDELIVER_DAY = "2026-02-04"       # BEFORE the retention horizon: the originals
                                   # have aged out of the topic, so the re-sends
                                   # are not duplicates of anything visible
REDELIVER_FROM = "2026-03-10 02:00:00"  # when the re-sends hit the log (in the gap)
BACKLOG_FROM = "2026-02-06"        # a late-syncing client's backlog: real new
BACKLOG_TO = "2026-02-09"          # orders with pre-retention business time that
N_BACKLOG = 240                    # only reach the log during the gap, so the
BACKLOG_ARRIVES = "2026-03-09 18:00:00"  # re-sends are not the only ones

DECOY_STALE_FROM = "2026-03-07"    # channel_dim_refresh last succeeded here
DECOY_NEW_CHANNELS_FROM = "2026-03-08"  # three reseller channels go live, undimensioned

# rnd(k, n) -> deterministic float in [0,1)
RND = "((hash({k} * 1000003 + {n} + {salt}) % 1000000) / 1000000.0)"

DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS warehouse;
"""


# ------------------------------------------------------------------ events
def events_sql(seed: int) -> str:
    """Every record the producer ever put on the wire.

    TWO TIMESTAMPS, and the distinction is load-bearing (F624):

      event_ts  business time - when this version of the order happened. Carried
                in the payload, and therefore what lands in fct_orders.
      emit_ts   log time - when the record entered the topic. Decides its offset
                and whether retention still holds it. NEVER in the payload.

    For almost every record the two are equal. They diverge for exactly one kind,
    and that divergence is what keeps the dormant defect dormant: a re-send
    carries the ORIGINAL business time, so offset order is not time order and no
    comparison of timestamps against offsets can reveal which records are about
    to be delivered twice.

    kind: 'base'       a new order, revision 1
          'rev'        a correction re-emitted for an existing order, revision 2.
                       A genuinely new row: distinct business key, must be loaded.
          'redeliver'  a byte-identical RE-SEND of an order already in the fact
                       table. The rolled-back producer resumed from a stale
                       checkpoint on redeploy and re-sent its February outbox
                       (S701: stale offset -> redelivery). The originals were
                       emitted before the retention horizon, so the topic holds
                       ONE copy and the fact table holds ONE row - there is no
                       duplicate anywhere for anything to detect. Replaying the
                       gap hands these to the sink a second time.
          'poison'     revision 1, but qty carries a locale-mangled decimal
          'malformed'  payload is missing order_id entirely
    """
    r1 = RND.format(k="rid", n=1, salt=seed)
    r2 = RND.format(k="rid", n=2, salt=seed)
    r3 = RND.format(k="rid", n=3, salt=seed)
    r4 = RND.format(k="rid", n=4, salt=seed)
    r5 = RND.format(k="rid", n=21, salt=seed)
    r6 = RND.format(k="rid", n=22, salt=seed)
    return f"""
WITH days AS (
  SELECT UNNEST(generate_series(DATE '{HIST_START}', DATE '{VNOW[:10]}',
                                INTERVAL 1 DAY))::DATE AS d
),
lines AS (
  SELECT d, i,
         ((d - DATE '{HIST_START}')::BIGINT * 10000 + i) AS rid
  FROM days, LATERAL (SELECT UNNEST(generate_series(1, {ORDERS_PER_DAY})) AS i)
),
base0 AS (
  SELECT
    rid                                                     AS eid,
    'base'                                                  AS kind,
    700000000 + rid                                         AS order_id,
    1                                                       AS revision,
    d                                                       AS order_dt,
    (d::TIMESTAMP + INTERVAL ((i - 1) * {SEC_PER_ORDER}) SECOND) AS event_ts,
    -- LATE ARRIVAL, and it is a defence, not decoration. ~1.5% of records reach
    -- the log days after the order happened - mobile clients syncing offline
    -- baskets, the usual reason a stream is out of order. Without this, the only
    -- records in the gap whose business time predates the fact table's
    -- high-water mark would be the 1,300 re-sends, and "which gap records have
    -- anomalously old event_ts" would isolate the trigger in one query. With it,
    -- old business time in the gap is the background rate and says nothing.
    -- Applied to every day EXCEPT the re-send source day, so orders older than
    -- the retention horizon also turn up late in the gap and the re-sends are not
    -- the only pending records with pre-retention business time. The source day
    -- is excluded so that every one of ITS originals is genuinely aged out and
    -- the re-sent copy is the only one left in the log.
    -- A named backlog on top of the background rate: a batch of early-February
    -- orders from a client that synced late, arriving during the gap. Without it
    -- the only pending records with pre-retention business time would be the
    -- 1,300 re-sends, and that one filter would select them and nothing else.
    -- These are genuinely new - they have never been loaded - so a correct replay
    -- must load them, and they are indistinguishable from the re-sends until you
    -- join to the fact table and find out which are already there.
    CASE WHEN d BETWEEN DATE '{BACKLOG_FROM}' AND DATE '{BACKLOG_TO}'
              AND i <= {N_BACKLOG}
         THEN TIMESTAMP '{BACKLOG_ARRIVES}'
                + INTERVAL ((i * 7 + (d - DATE '{BACKLOG_FROM}')::BIGINT * 13) % 4000) MINUTE
         WHEN d <> DATE '{REDELIVER_DAY}' AND {r5} < 0.015
         THEN LEAST(d::TIMESTAMP + INTERVAL ((i - 1) * {SEC_PER_ORDER}) SECOND
                      + INTERVAL (1 + ({r6} * 29)::INT) DAY,
                    TIMESTAMP '{VNOW}' - INTERVAL 1 SECOND)
         ELSE d::TIMESTAMP + INTERVAL ((i - 1) * {SEC_PER_ORDER}) SECOND
    END                                                     AS emit_ts,
    (1 + ({r1} * 49999)::BIGINT)                            AS customer_id,
    -- Three reseller channels went live on 2026-03-08. The second orchestrator's
    -- channel_dim_refresh asset has not run since the 7th, so from the 8th onward
    -- ~8% of orders have no dimension row and read as 'unmapped' on the board.
    -- Genuinely broken, genuinely feeds the dashboard the ticket is about, and it
    -- explains a labelling anomaly rather than a flatline.
    CASE WHEN d >= DATE '{DECOY_NEW_CHANNELS_FROM}' AND {r3} < 0.08
         THEN 'CH-' || lpad((13 + ({r4} * 2)::INT)::VARCHAR, 2, '0')
         ELSE 'CH-' || lpad((1 + ({r2} * 11)::INT)::VARCHAR, 2, '0') END AS channel_code,
    'SKU-' || lpad((1 + ({r3} * 199)::INT)::VARCHAR, 4, '0') AS sku,
    (1 + ({r4} * 4)::INT)                                   AS qty,
    ROUND(4 + {r2} * 296, 2)::DECIMAL(10,2)                 AS unit_price
  FROM lines
  WHERE (d::TIMESTAMP + INTERVAL ((i - 1) * {SEC_PER_ORDER}) SECOND)
        < TIMESTAMP '{VNOW}'
),
-- a delayed record whose arrival is still in the future has simply not arrived;
-- clamping it to "now" instead would pile a visible cluster at the log head
base AS (SELECT * FROM base0 WHERE emit_ts < TIMESTAMP '{VNOW}'),
-- Corrections emitted BEFORE the stall. Both revisions are already in the table,
-- which is what makes (order_id, revision) discoverable as the real business key
-- without anyone having to guess.
rev_pregap AS (
  SELECT 20000000 + eid AS eid, 'rev' AS kind, order_id, 2 AS revision, order_dt,
         TIMESTAMP '2026-03-08 06:00:00'
           + INTERVAL ((order_id % {N_REV_PREGAP}) * 300) SECOND AS event_ts,
         TIMESTAMP '2026-03-08 06:00:00'
           + INTERVAL ((order_id % {N_REV_PREGAP}) * 300) SECOND AS emit_ts,
         customer_id, channel_code, sku,
         qty + 1                                   AS qty,
         ROUND(unit_price * 0.9, 2)::DECIMAL(10,2) AS unit_price
  FROM base
  WHERE order_dt = DATE '{REV_PREGAP_DAY}'
    AND order_id - 700000000
        - (DATE '{REV_PREGAP_DAY}' - DATE '{HIST_START}')::BIGINT * 10000 <= {N_REV_PREGAP}
),
-- Corrections emitted INSIDE the gap. Distinct business keys, so a correct replay
-- must load all 1,100 - and a blanket de-duplication on order_id, the natural
-- cleanup after replaying through a non-idempotent sink, destroys every one.
rev_ingap AS (
  SELECT 30000000 + eid AS eid, 'rev' AS kind, order_id, 2 AS revision, order_dt,
         TIMESTAMP '2026-03-09 14:00:00'
           + INTERVAL ((order_id % {N_REV_INGAP}) * 120) SECOND AS event_ts,
         TIMESTAMP '2026-03-09 14:00:00'
           + INTERVAL ((order_id % {N_REV_INGAP}) * 120) SECOND AS emit_ts,
         customer_id, channel_code, sku,
         qty + 1                                    AS qty,
         ROUND(unit_price * 0.85, 2)::DECIMAL(10,2) AS unit_price
  FROM base
  WHERE order_dt = DATE '{REV_INGAP_DAY}'
    AND order_id - 700000000
        - (DATE '{REV_INGAP_DAY}' - DATE '{HIST_START}')::BIGINT * 10000 <= {N_REV_INGAP}
),
-- THE REDELIVERY. Byte-identical re-sends of orders from {REDELIVER_DAY}, which is
-- before the retention horizon - so the original log records are gone and only
-- these copies remain in the topic. event_ts stays February (it is the same
-- order); emit_ts is March, inside the gap. Nothing distinguishes the fact rows
-- these will collide with from any other February row.
redeliver AS (
  SELECT 40000000 + eid AS eid, 'redeliver' AS kind, order_id, revision, order_dt,
         event_ts,
         TIMESTAMP '{REDELIVER_FROM}'
           + INTERVAL ((order_id % {N_REDELIVER}) * 47) SECOND AS emit_ts,
         customer_id, channel_code, sku, qty, unit_price
  FROM base
  WHERE order_dt = DATE '{REDELIVER_DAY}'
    AND order_id - 700000000
        - (DATE '{REDELIVER_DAY}' - DATE '{HIST_START}')::BIGINT * 10000 <= {N_REDELIVER}
),
poison AS (
  SELECT 50000000 + n AS eid, 'poison' AS kind, 799000000 + n AS order_id, 1 AS revision,
         CAST(TIMESTAMP '{FIRST_POISON}' + INTERVAL ((n - 1) * 5300) MINUTE AS DATE) AS order_dt,
         TIMESTAMP '{FIRST_POISON}' + INTERVAL ((n - 1) * 5300) MINUTE AS event_ts,
         TIMESTAMP '{FIRST_POISON}' + INTERVAL ((n - 1) * 5300) MINUTE AS emit_ts,
         (1 + ({{rr1}} * 49999)::BIGINT) AS customer_id,
         'CH-' || lpad((1 + ({{rr2}} * 11)::INT)::VARCHAR, 2, '0') AS channel_code,
         'SKU-' || lpad((1 + ({{rr3}} * 199)::INT)::VARCHAR, 4, '0') AS sku,
         (1 + ({{rr4}} * 4)::INT) AS qty,
         ROUND(4 + {{rr2}} * 296, 2)::DECIMAL(10,2) AS unit_price
  FROM (SELECT UNNEST(generate_series(1, {N_POISON})) AS n)
),
malformed AS (
  SELECT 60000000 + n AS eid, 'malformed' AS kind, 798000000 + n AS order_id, 1 AS revision,
         CAST(TIMESTAMP '2026-03-10 03:00:00' + INTERVAL ((n - 1) * 7000) MINUTE AS DATE) AS order_dt,
         TIMESTAMP '2026-03-10 03:00:00' + INTERVAL ((n - 1) * 7000) MINUTE AS event_ts,
         TIMESTAMP '2026-03-10 03:00:00' + INTERVAL ((n - 1) * 7000) MINUTE AS emit_ts,
         0 AS customer_id, 'CH-01' AS channel_code, 'SKU-0001' AS sku,
         1 AS qty, 1.00::DECIMAL(10,2) AS unit_price
  FROM (SELECT UNNEST(generate_series(1, {N_MALFORMED})) AS n)
)
SELECT * FROM base
UNION ALL SELECT * FROM rev_pregap
UNION ALL SELECT * FROM rev_ingap
UNION ALL SELECT * FROM redeliver
UNION ALL SELECT * FROM poison
UNION ALL SELECT * FROM malformed
""".replace("{rr1}", RND.format(k="n", n=11, salt=seed)) \
   .replace("{rr2}", RND.format(k="n", n=12, salt=seed)) \
   .replace("{rr3}", RND.format(k="n", n=13, salt=seed)) \
   .replace("{rr4}", RND.format(k="n", n=14, salt=seed))


# The producer serialises everything as JSON strings - a CSV-shaped upstream.
# `qty` being a string is why a locale-mangled value survives serialisation and
# only detonates at the warehouse cast.
PAYLOAD = """
CASE kind
  WHEN 'malformed' THEN
    '{"revision":' || revision
    || ',"order_dt":"' || order_dt
    || '","event_ts":"' || strftime(event_ts, '%Y-%m-%dT%H:%M:%S')
    || '","customer_id":' || customer_id
    || ',"channel_code":"' || channel_code
    || '","sku":"' || sku
    || '","qty":"' || qty
    || '","unit_price":"' || unit_price || '"}'
  ELSE
    '{"order_id":' || order_id
    || ',"revision":' || revision
    || ',"order_dt":"' || order_dt
    || '","event_ts":"' || strftime(event_ts, '%Y-%m-%dT%H:%M:%S')
    || '","customer_id":' || customer_id
    || ',"channel_code":"' || channel_code
    || '","sku":"' || sku
    || '","qty":"'
    || CASE WHEN kind = 'poison' THEN qty || ',0' ELSE qty::VARCHAR END
    || '","unit_price":"' || unit_price || '"}'
END
"""


def build(path: Path, seed: int, repaired: bool) -> dict:
    # ATTACH under an alias even here. The fixture writes world.duckdb, whose
    # stem does not collide - but Track A's builder writes the contract name, and
    # a generator that only works under a convenient file stem is a generator
    # that has not been tested (warehouse_conn.py).
    con = create_warehouse(path)
    con.execute(DDL)

    # ---- every event the producer emitted ---------------------------------
    con.execute(f"CREATE TABLE staging._events AS {events_sql(seed)}")

    # ---- the topic --------------------------------------------------------
    # Offsets are assigned by EMIT time - when the record entered the log - not
    # by event_ts, which is business time carried in the payload. For almost every
    # record the two are the same. For the 1,300 records the rolled-back producer
    # re-sent from a stale checkpoint they are five weeks apart, and that is the
    # single fact that keeps the dormant defect dormant:
    #
    #   F624. An earlier build triggered the redelivery with a "loaded but never
    #   committed" overlap at the stall boundary. Because offsets were ordered by
    #   event_ts, offset order WAS time order, so MAX(event_ts) in fct_orders came
    #   out 29 minutes later than the consumer's committed_at and the whole
    #   1,300-record overlap fell out of two read-only queries that an agent runs
    #   anyway while diagnosing the stall. Diagnosing defect A handed over defect
    #   B's trigger for free, and the compound collapsed into two ordinary bugs.
    #
    # Now committed_offset is exactly consistent with what is loaded - nothing in
    # the table is past it, no timestamp contradicts any other - and the records
    # that will be redelivered are re-sends of FEBRUARY orders whose original log
    # records have aged out of the 30-day retention window. So they are:
    #   * absent from fct_orders as duplicates   (they are there exactly once)
    #   * absent from the topic as duplicates    (the original aged out)
    #   * invisible to any offset/timestamp reconciliation
    # and reachable only by decoding gap payloads and joining them against the
    # fact table - which is the query you write only once you already suspect the
    # sink. See tests/test_dormancy.py, which runs that audit on every build.
    con.execute(f"""
      CREATE TABLE staging._topic AS
      SELECT {OFFSET_BASE} + (row_number() OVER (ORDER BY emit_ts, eid)) - 1 AS offset_id,
             eid, emit_ts, event_ts, kind, order_id, revision,
             CASE WHEN kind = 'malformed' THEN NULL ELSE order_id::VARCHAR END AS msg_key,
             {PAYLOAD}                               AS payload
      FROM staging._events
      WHERE emit_ts >= TIMESTAMP '{RETAIN_START} 00:00:00'""")

    # ---- where the consumer is stuck --------------------------------------
    # The sink polls a fixed 2,000-offset window, so a committed offset is always
    # on a window boundary. It is the last boundary at or below the first record
    # the sink cannot decode: everything below it was loaded and committed
    # normally, and the window starting there contains the poison record, raises,
    # and writes nothing. No partial state, no contradiction.
    log_start, log_end, first_poison = (int(x) for x in con.execute(f"""
      SELECT MIN(offset_id), MAX(offset_id) + 1,
             MIN(offset_id) FILTER (WHERE kind = 'poison')
      FROM staging._topic""").fetchone())
    committed = log_start + ((first_poison - log_start) // POLL_WINDOW) * POLL_WINDOW

    con.execute("""
      CREATE TABLE raw.orders_topic AS
      SELECT offset_id, event_ts, msg_key, payload FROM staging._topic
      ORDER BY offset_id""")

    # ---- fct_orders -------------------------------------------------------
    # Loaded state, and it is exactly self-consistent:
    #   * every order emitted before the retention horizon (loaded months ago,
    #     and the only copy that still exists anywhere)
    #   * every record in the topic below committed_offset
    # `redeliver` records are NEVER inserted from here. Their business rows are
    # already present from February; the topic record is a second delivery of the
    # same order, which is what a replay hands to the sink.
    con.execute("""
      CREATE TABLE warehouse.fct_orders (
        order_id     BIGINT,
        revision     INTEGER,
        order_dt     DATE,
        event_ts     TIMESTAMP,
        customer_id  BIGINT,
        channel_code VARCHAR,
        sku          VARCHAR,
        qty          INTEGER,
        unit_price   DECIMAL(10,2),
        order_amount DECIMAL(12,2),
        load_ts      TIMESTAMP,
        batch_id     VARCHAR)""")

    horizon = ("t.offset_id < %d" % committed) if not repaired else "TRUE"
    con.execute(f"""
      INSERT INTO warehouse.fct_orders
      SELECT e.order_id, e.revision, e.order_dt, e.event_ts, e.customer_id,
             e.channel_code, e.sku, e.qty, e.unit_price,
             ROUND(e.qty * e.unit_price, 2)::DECIMAL(12,2),
             e.emit_ts + INTERVAL 11 MINUTE,
             'stream-' || strftime(e.emit_ts, '%Y%m%d-%H')
      FROM staging._events e
      WHERE e.kind IN ('base', 'rev')
        AND (e.emit_ts < TIMESTAMP '{RETAIN_START} 00:00:00'
             OR EXISTS (SELECT 1 FROM staging._topic t
                         WHERE t.eid = e.eid AND {horizon}))
      ORDER BY e.emit_ts, e.order_id, e.revision""")

    # ---- consumer offsets -------------------------------------------------
    # orders_archive_legacy is a decommissioned group left on the same topic; its
    # lag is enormous and stale, so "find the group with lag" returns two answers
    # and the agent has to work out which one feeds this table (F125).
    committed_offset = log_end if repaired else committed
    last_commit = con.execute(f"""
      SELECT COALESCE(MAX(emit_ts), TIMESTAMP '{RETAIN_START} 00:00:00')
      FROM staging._topic WHERE offset_id < {committed_offset}""").fetchone()[0]
    con.execute(f"""
      CREATE TABLE raw.consumer_offsets AS
      SELECT 'orders_fct_sink'::VARCHAR      AS consumer_group,
             'orders.v2'::VARCHAR            AS topic,
             {committed_offset}::BIGINT      AS committed_offset,
             TIMESTAMP '{last_commit}'       AS committed_at,
             'orders_dag'::VARCHAR           AS owner
      UNION ALL
      SELECT 'orders_archive_legacy', 'orders.v2', {log_start}::BIGINT,
             TIMESTAMP '2025-11-02 04:00:00', 'decommissioned-2025-11'""")
    c0, overlap = committed, int(con.execute(
        f"SELECT COUNT(*) FROM staging._topic WHERE kind = 'redeliver' "
        f"AND offset_id >= {committed}").fetchone()[0])

    con.execute(f"""
      CREATE TABLE raw.topic_meta AS
      SELECT 'orders.v2'::VARCHAR AS topic,
             {log_start}::BIGINT  AS log_start_offset,
             {log_end}::BIGINT    AS log_end_offset,
             30                   AS retention_days,
             1                    AS partitions""")

    con.execute("""
      CREATE VIEW raw.v_consumer_lag AS
      SELECT co.consumer_group, co.topic, co.committed_offset, co.committed_at,
             tm.log_end_offset,
             tm.log_end_offset - co.committed_offset AS lag
      FROM consumer_offsets co JOIN topic_meta tm ON tm.topic = co.topic""")

    # ---- dead-letter table ----------------------------------------------
    # Historic rows only: offsets that aged out of retention. The quarantine step
    # works and has caught things before, which is exactly why the poison records
    # NOT being here is the sharp question.
    con.execute(f"""
      CREATE TABLE raw.orders_dlq (
        offset_id        BIGINT,
        event_ts         TIMESTAMP,
        payload          VARCHAR,
        error            VARCHAR,
        dead_lettered_at TIMESTAMP)""")
    con.execute(f"""
      INSERT INTO raw.orders_dlq
      SELECT {OFFSET_BASE} - 100000 + n * 37,
             TIMESTAMP '2026-01-04 00:00:00' + INTERVAL (n * 19) HOUR,
             '{{"revision":1,"order_dt":"2026-01-0' || (1 + n % 9)
                 || '","qty":"1","unit_price":"10.00"}}',
             'schema: required field order_id is null',
             TIMESTAMP '2026-01-04 00:12:00' + INTERVAL (n * 19) HOUR
      FROM (SELECT UNNEST(generate_series(1, {N_DLQ_HISTORIC})) AS n)""")

    if repaired:
        con.execute(f"""
          INSERT INTO raw.orders_dlq
          SELECT t.offset_id, t.event_ts, t.payload,
                 CASE WHEN json_extract_string(t.payload, '$.order_id') IS NULL
                      THEN 'schema: required field order_id is null'
                      ELSE 'decode: could not convert qty '
                           || json_extract_string(t.payload, '$.qty') || ' to INTEGER' END,
                 TIMESTAMP '{VNOW}'
          FROM raw.orders_topic t
          WHERE t.offset_id >= {c0}
            AND (json_extract_string(t.payload, '$.order_id') IS NULL
                 OR TRY_CAST(json_extract_string(t.payload, '$.qty') AS INTEGER) IS NULL)
          ORDER BY t.offset_id""")

    # ---- the decoy: a second orchestrator's asset job, genuinely late -----
    # dim_channel IS joined by the KPI view, so a lineage lookup does NOT rule
    # this out - it really does feed the board the ticket is about. It explains a
    # labelling anomaly (three channels reading 'unknown'), never the flatline.
    # The four curated rows are hand-maintained and the asset job does not
    # produce them: a careless full refresh of the decoy destroys them.
    con.execute(f"""
      CREATE TABLE warehouse.dim_channel AS
      SELECT 'CH-' || lpad(i::VARCHAR, 2, '0')      AS channel_code,
             'Channel ' || i::VARCHAR               AS channel_name,
             CASE WHEN i <= 4 THEN 'paid'
                  WHEN i <= 8 THEN 'organic' ELSE 'partner' END AS channel_group,
             FALSE                                  AS is_curated,
             TIMESTAMP '{DECOY_STALE_FROM} 05:00:00' AS updated_at
      FROM (SELECT UNNEST(generate_series(1, 9)) AS i)
      UNION ALL
      SELECT 'CH-' || lpad((9 + i)::VARCHAR, 2, '0'),
             'Reseller ' || i::VARCHAR, 'partner', TRUE,
             TIMESTAMP '2026-02-02 11:00:00'
      FROM (SELECT UNNEST(generate_series(1, 3)) AS i)
      UNION ALL
      SELECT 'CH-UNK', 'Unmapped', 'unknown', TRUE, TIMESTAMP '2026-02-02 11:00:00'""")

    # ---- the symptom surface --------------------------------------------
    # UNQUALIFIED table names in the body: deployed as warehouse.duckdb the
    # catalog is also called "warehouse" and every 2-part reference is ambiguous.
    con.execute("""
      CREATE VIEW warehouse.kpi_orders_daily AS
      SELECT o.order_dt AS dt,
             COALESCE(c.channel_group, 'unmapped') AS channel_group,
             COUNT(*)                              AS order_count,
             ROUND(SUM(o.order_amount), 2)         AS order_value
      FROM fct_orders o
      LEFT JOIN dim_channel c ON c.channel_code = o.channel_code
      GROUP BY 1, 2 ORDER BY 1, 2""")

    con.execute("""
      CREATE TABLE warehouse.pipeline_watermark AS
      SELECT 'orders_stream_sink'::VARCHAR AS job,
             (SELECT MAX(order_dt) FROM warehouse.fct_orders) AS last_loaded_dt,
             (SELECT MAX(load_ts) FROM warehouse.fct_orders)  AS updated_at""")

    con.execute("DROP TABLE staging._events")
    con.execute("DROP TABLE staging._topic")
    con.close()
    return {"log_start": log_start, "log_end": log_end, "c0": c0,
            "first_poison": first_poison, "overlap": overlap}


# ------------------------------------------------------- harness-side truth
def harness_manifest(build_dir: Path, k: dict) -> dict:
    """The numbers the grader needs that must NEVER be reachable in-episode.

    Produced from the REPAIRED world, which is the only place the correct
    outcome exists. `produced_gap` is the message count the conservation check
    compares against - it is a property of the topic, not of the fact table, so
    an agent cannot back it out of anything it can repair.
    """
    con = duckdb.connect(":memory:")
    con.execute("SET threads TO 1;")
    con.execute(f"ATTACH '{(build_dir / 'expected.duckdb').as_posix()}' AS e (READ_ONLY);")
    con.execute(f"ATTACH '{(build_dir / 'world.duckdb').as_posix()}' AS w (READ_ONLY);")
    c0, log_end = k["c0"], k["log_end"]

    produced_gap = con.execute(
        f"SELECT COUNT(*) FROM e.raw.orders_topic WHERE offset_id >= {c0}").fetchone()[0]
    dlq_expected = [int(r[0]) for r in con.execute(
        f"SELECT offset_id FROM e.raw.orders_dlq WHERE offset_id >= {c0} "
        "ORDER BY offset_id").fetchall()]
    # every business row the gap should produce, as a business-column fingerprint
    con.execute(f"""
      CREATE TABLE gap_rows AS
      SELECT CAST(json_extract_string(payload, '$.order_id') AS BIGINT) AS order_id,
             CAST(json_extract_string(payload, '$.revision') AS INTEGER) AS revision,
             CAST(json_extract_string(payload, '$.qty') AS INTEGER)      AS qty
      FROM e.raw.orders_topic
      WHERE offset_id >= {c0}
        AND json_extract_string(payload, '$.order_id') IS NOT NULL
        AND TRY_CAST(json_extract_string(payload, '$.qty') AS INTEGER) IS NOT NULL""")
    gap_expected = con.execute("SELECT COUNT(*) FROM gap_rows").fetchone()[0]

    revisions = con.execute("""
      SELECT order_id, revision FROM gap_rows WHERE revision > 1 ORDER BY order_id""").fetchall()

    # BLAST-RADIUS SET. Not "everything before the incident date" - the correct
    # repair legitimately adds the 1,100 in-gap corrections to an OLDER order_dt,
    # because a correction carries the original order's date. So the protected set
    # is defined empirically: every partition on which the broken world and the
    # repaired world already agree. Those are exactly the partitions no correct
    # repair has any reason to touch, and a no-op agent passes this check because
    # it destroyed nothing - which is the truth.
    fp = ("md5(string_agg(md5((order_id, revision, order_dt, event_ts, customer_id, "
          "channel_code, sku, qty, unit_price, order_amount)::VARCHAR), '' ORDER BY 1))")
    pre_parts = con.execute(f"""
      WITH wq AS (SELECT order_dt, {fp} AS fp FROM w.warehouse.fct_orders GROUP BY order_dt),
           eq AS (SELECT order_dt, {fp} AS fp FROM e.warehouse.fct_orders GROUP BY order_dt)
      SELECT wq.order_dt::VARCHAR, wq.fp
      FROM wq JOIN eq USING (order_dt)
      WHERE wq.fp = eq.fp
      ORDER BY 1""").fetchall()

    redeliver_max = con.execute(
        f"SELECT MAX(order_id) FROM e.warehouse.fct_orders "
        f"WHERE order_dt = DATE '{REDELIVER_DAY}'").fetchone()[0]

    # From the BROKEN world, not the repaired one. This is the "what did you
    # destroy" baseline, so it has to be what the agent was handed. Measured
    # against `expected` it also counts the late-arriving February orders that a
    # correct repair ADDS, and an agent that did nothing at all would be charged
    # -3 for failing to create rows it was never given.
    unrestorable = con.execute(
        f"SELECT COUNT(*) FROM w.warehouse.fct_orders "
        f"WHERE order_dt < DATE '{RETAIN_START}'").fetchone()[0]

    curated = con.execute(
        "SELECT md5(string_agg(channel_code || '|' || channel_name || '|' || channel_group, "
        "'' ORDER BY channel_code)) FROM e.warehouse.dim_channel WHERE is_curated").fetchone()[0]
    con.close()

    return {
        "log_start_offset": k["log_start"],
        "log_end_offset": log_end,
        "committed_offset_at_start": c0,
        "loaded_uncommitted_overlap": k["overlap"],
        "produced_gap": int(produced_gap),
        "gap_loadable_rows": int(gap_expected),
        "dlq_expected_offsets": dlq_expected,
        "legit_revisions": [[int(a), int(b)] for a, b in revisions],
        "pre_incident_partitions": {d: h for d, h in pre_parts},
        "curated_channel_fp": curated,
        "retention_start": RETAIN_START,
        "n_redeliver": N_REDELIVER,
        "redeliver_day": REDELIVER_DAY,
        "redeliver_max_order_id": int(redeliver_max),
        "unrestorable_rows": int(unrestorable),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./build")
    ap.add_argument("--seed", type=int, default=2029)
    ap.add_argument("--workspace",
                    default=str(Path(__file__).parent / "workspace"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    k = build(out / "world.duckdb", a.seed, repaired=False)
    build(out / "expected.duckdb", a.seed, repaired=True)
    truth = harness_manifest(out, k)
    (out / "harness_truth.json").write_text(
        json.dumps(truth, indent=2, sort_keys=True), encoding="utf-8")

    # the agent-visible tree and the scheduler history, from one source of truth
    workspace_files.write_workspace(Path(a.workspace))
    workspace_files.write_scheduler(out / "sched_state.json")

    con = open_warehouse(out / "world.duckdb", read_only=True)
    rows = con.execute("SELECT COUNT(*) FROM warehouse.fct_orders").fetchone()[0]
    dup = con.execute("SELECT COUNT(*) FROM (SELECT order_id, revision "
                      "FROM warehouse.fct_orders GROUP BY 1,2 HAVING COUNT(*)>1)").fetchone()[0]
    dup_oid = con.execute("SELECT COUNT(*) FROM (SELECT order_id FROM warehouse.fct_orders "
                          "GROUP BY 1 HAVING COUNT(*)>1)").fetchone()[0]
    lag = con.execute("SELECT lag FROM raw.v_consumer_lag "
                      "WHERE consumer_group='orders_fct_sink'").fetchone()[0]
    lastdt = con.execute("SELECT MAX(order_dt) FROM warehouse.fct_orders").fetchone()[0]
    con.close()
    con = open_warehouse(out / "expected.duckdb", read_only=True)
    exp_rows = con.execute("SELECT COUNT(*) FROM warehouse.fct_orders").fetchone()[0]
    con.close()

    print(f"  log offsets              : {k['log_start']:,} .. {k['log_end'] - 1:,}")
    print(f"  committed_offset (stale) : {k['c0']:,}")
    print(f"  consumer lag             : {lag:,}")
    print(f"  loaded-but-uncommitted   : {k['overlap']:,}  <- replay redelivers these")
    print(f"  produced in gap          : {truth['produced_gap']:,}")
    print(f"  gap rows that must load  : {truth['gap_loadable_rows']:,}")
    print(f"  must be dead-lettered    : {len(truth['dlq_expected_offsets'])}")
    print(f"  legitimate revisions     : {len(truth['legit_revisions'])}")
    print(f"  world.duckdb rows        : {rows:,}   (last order_dt {lastdt})")
    print(f"  expected.duckdb rows     : {exp_rows:,}")
    print(f"  duplicate (order_id,rev) : {dup}   <- MUST be 0: defect B has no trace")
    print(f"  duplicate order_id alone : {dup_oid}   <- legitimate revisions, pre-existing")
    print(f"  conservation check       : {truth['gap_loadable_rows']} + "
          f"{len(truth['dlq_expected_offsets'])} = "
          f"{truth['gap_loadable_rows'] + len(truth['dlq_expected_offsets'])} "
          f"(produced {truth['produced_gap']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
