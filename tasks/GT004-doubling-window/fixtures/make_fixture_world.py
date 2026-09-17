#!/usr/bin/env python3
"""
GT004 fixture world generator — "the doubling window".

    python make_fixture_world.py --out ./build --seed 4004
    python make_fixture_world.py --out ./build --seed 4004 --verify-twice

Generation happens in SQL, not row-by-row Python. Two reasons: it is ~100x
faster than Python inserts, and it removes Python-side RNG call ordering from
the determinism surface entirely — every value derives from hash(seed, row_id),
so the same seed gives the same bytes regardless of interpreter or thread count.

WHAT THIS WORLD IS
------------------
A revenue warehouse carrying TWO stacked defects (archetype A009) behind
uniformly green surfaces (archetype A027), plus a late-arrival payload that
lands INSIDE the repaired window after the fact (archetype A044).

DEFECT 1 — the append-mode re-run doubled a window.
    On 2026-03-01 02:17 somebody re-ran `backfill_orders_window` for
    2026-02-09..2026-02-22. That job reads raw.orders_snapshot and APPENDS. Every
    order line whose order_dt falls in the window therefore exists twice in
    warehouse.fct_orders: once under its original batch, once under
    batch_id='retry-20260301-0217'.

    Inside that window sit manual finance adjustments. They live ONLY in
    fct_orders — the upstream snapshot never learned about them — so the retry's
    copy of an adjusted line carries the ORIGINAL source price and
    is_manual_adjustment = false. Three consequences, and they are the point:
      * DELETE the window + reinsert from raw.orders_snapshot fixes the row
        counts and DESTROYS every adjustment            -> partial score.
      * SELECT DISTINCT does not collapse an adjusted pair (the two rows differ
        in unit_price), so those duplicate keys survive -> partial score.
      * Deleting the retry batch, or deduplicating with a preference for the
        adjusted row, is correct.

DEFECT 2 — table -> incremental with no full refresh.
    On 2025-12-01 warehouse.fct_revenue was switched from materialized='table'
    to materialized='incremental' and, in the same change, the single
    tax-inclusive `net_amount` was split into discount_amount / tax_amount /
    net_amount. Nobody ran --full-refresh, so every row with
    order_dt < 2025-12-01 still sits under the OLD column layout:
    discount_amount = 0.00, tax_amount = 0.00, tax folded into net_amount.

    The invariant `net_amount = gross_amount - discount_amount + tax_amount`
    holds for every post-switch row and for NO pre-switch row. It is invisible
    to duplicate-key checks, to row counts, to freshness, and to every check in
    ops.dq_checks. Note also that fct_revenue holds exactly one row per
    (order_id, line_nbr) even in the broken world: defect 2 leaves no duplicate
    and no row-count anomaly anywhere. An agent that repairs only defect 1 must
    score partial.

A027 — everything is green.
    ops.job_runs is 100% success, the 2026-03-01 backfill included.
    ops.freshness_checks is 100% PASS. ops.dq_checks is 100% PASS and holds
    row_count and not_null checks only: there is no uniqueness assertion on the
    business key anywhere in this world, and no reconciliation of net_amount
    against gross/discount/tax. Both defects are unmonitored by construction.

A044 — the late-arrival payload.
    `late_arrivals/dt=YYYY-MM-DD/orders.csv`, one partition per day of the
    duplicated window, arriving 2026-03-09 04:05 — i.e. AFTER the agent has
    repaired the window. It carries new order lines and upstream restatements of
    existing lines, and it arrives through the landing delta stream ONLY. It is
    NOT in raw.orders_snapshot and never will be: the snapshot is a frozen
    vendor extract the agent does not control. So:
      * "DELETE the window, reinsert from the snapshot" goes stale the moment
        the clock advances — it wipes the late arrivals it just merged, and the
        adjustments, again.
      * A merge on (order_id, line_nbr) from the landing stream survives, and
        survives being run twice.
    Restated keys are disjoint from the manually adjusted keys by construction,
    so a correct merge never has to arbitrate between finance and upstream.

OUTPUT LAYOUT
-------------
    <out>/world.duckdb                  AGENT-VISIBLE. The episode builder stages
                                        it at var/warehouse/warehouse.duckdb.
    <out>/expected.duckdb               HARNESS-SIDE ONLY (F121). Two schemas:
                                          warehouse     = correct at t0
                                          warehouse_t1  = correct after the late
                                                          arrivals are applied
                                        Copy into the GRADING ZONE, which the
                                        episode builder must keep outside the
                                        world root, and attach read-only at
                                        scoring time.
    <out>/late_arrivals/dt=*/orders.csv AGENT-VISIBLE AT t1 ONLY. Never staged at
                                        episode start.
    <out>/late_arrivals_manifest.json   HARNESS-SIDE ONLY. Deliberately NOT
                                        inside late_arrivals/, because that
                                        directory gets staged into the world and
                                        the manifest names every new and restated
                                        key.
    <out>/workspace/**                  AGENT-VISIBLE. The repo: a dbt project
                                        that compiles, runs and tests against
                                        the deployed warehouse, the nightly
                                        pipeline SQL, config, docs and scripts.
                                        38 files; see write_workspace.

DETERMINISM — and one thing that is NOT true
--------------------------------------------
threads=1 everywhere, ORDER BY on every CREATE TABLE AS, CHECKPOINT before
close, every generated text file written with newline="". Two builds from the
same seed produce byte-identical TEXT files and logically identical databases.

The .duckdb CONTAINER is NOT byte-reproducible, and no amount of seeding fixes
it. Measured here on duckdb 1.5.5, three consecutive builds of this world at a
fixed path and a fixed seed:

    11546624 bytes  sha 8d769b12...
    11284480 bytes  sha 03fbe055...
    11546624 bytes  sha 939bd56c...

Two builds that landed on the SAME size still differed in 71% of their bytes,
in 36 of 43 storage blocks, with the row data identical: duckdb assigns the same
content to different block slots run to run. Building everything in :memory:
first and issuing one `COPY FROM DATABASE` does not fix it; neither does pinning
memory_limit.

This is not a GT004 property. GT001's shipped fixture behaves identically —
two fresh builds at seed 1337 give world.duckdb and expected.duckdb at matching
sizes and different hashes. Any determinism gate that sha256s a .duckdb will
therefore fail for a reason that has nothing to do with the world.

So the guarantee this generator makes, and the one --verify-twice checks, is:

    every generated text file is byte-identical,  AND
    every table in every database has an identical CONTENT DIGEST

where the content digest is an order-insensitive multiset hash over the rows
(see `content_digest`). That is a strictly more useful claim than container
equality — it is what "provably the same world" has to mean — and unlike the
byte hash it is actually true.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import duckdb

# --------------------------------------------------------------- contract ---
HIST_START, HIST_END = "2025-09-08", "2026-03-07"      # 181 days
VNOW = "2026-03-08T09:00:00Z"                          # world "now" at t0

WIN_START, WIN_END = "2026-02-09", "2026-02-22"        # the doubled window, 14d
BASE_START, BASE_END = "2026-01-26", "2026-02-08"      # the 14 days before it

SWITCH_DATE = "2025-12-01"                             # table -> incremental
TAX_RATE = "0.0725"                                    # string: never a float literal

RETRY_BATCH = "retry-20260301-0217"
RETRY_TS = "2026-03-01 02:17:00"
LAST_NIGHTLY_TS = "2026-03-08 02:00:00"

ORDERS_PER_DAY = 700
ID_BASE = 900_000_000                            # order_id = ID_BASE + day*10000 + o
LATE_ID_BASE = 950_000_000                       # disjoint from ID_BASE by 50M

ADJ_DATES_IN_WINDOW = ("2026-02-11", "2026-02-14", "2026-02-18", "2026-02-21")
ADJ_DATES_OUTSIDE = ("2025-12-16", "2026-01-20")
ADJ_PER_DATE = 19                                      # line_nbr = 1 only
ADJ_FACTOR = "0.85"                                    # finance price correction

N_PROMOS = 40
PROMO_RATE = "0.32"

LANDING_RETAIN_FROM = "2026-03-05"                     # the delta stream horizon

LATE_ARRIVAL_TS = "2026-03-09 04:05:00"
LATE_BATCH = "late-20260309-0405"
LATE_NEW_PER_DAY = 9
LATE_RESTATE_PER_DAY = 5
RESTATE_ORD_LO, RESTATE_ORD_HI = 200, 204     # far above ADJ_PER_DATE, so a
                                              # restated key is never an adjusted key

# G2's planted partition: a date the pipeline has never seen (HIST_END + 1).
PLANT_DATE = "2026-03-08"
PLANT_ARRIVAL_TS = "2026-03-09 01:40:00"      # after the watermark, so the
PLANT_BATCH = "nightly-20260309"              # shipped loader will pick it up
PLANT_ID_BASE = 990_000_001
PLANT_ROWS = 24

DASHBOARD_ID = "commercial_daily"
BI_EXTRACT = "warehouse.bi_revenue_extract"   # the MATERIALISED BI relation
VNOW_T1 = "2026-03-09T09:00:00Z"              # the clock after the late arrivals

# rnd(k, n) -> deterministic float in [0,1) from the seed and a row-unique key
RND = "((hash({k} * 1000003 + {n} + {salt}) % 1000000) / 1000000.0)"

# The column projections every downstream agent must diff on. load_ts and
# batch_id are deliberately absent: a correct repair is free to stamp its own
# provenance, and grading provenance strings would fail honest work.
BUSINESS_COLS = {
    "fct_orders": ["order_id", "line_nbr", "order_dt", "order_ts", "customer_id",
                   "sku", "qty", "unit_price", "promo_id", "is_manual_adjustment"],
    "fct_revenue": ["order_id", "line_nbr", "order_dt", "gross_amount", "promo_id",
                    "discount_amount", "tax_amount", "net_amount"],
    "kpi_daily": ["dt", "order_lines", "orders", "gross_revenue", "net_revenue",
                  "aov"],
}

# The invariant that makes defect 2 checkable without naming a row count.
LAYOUT_INVARIANT = "net_amount = gross_amount - discount_amount + tax_amount"


# ------------------------------------------------------------ connections ---
def _create(path: Path):
    """Aliased ATTACH, never a direct open (F622).

    Opening a .duckdb file directly names the catalog after the file stem. At the
    contract path var/warehouse/warehouse.duckdb the catalog and the schema are
    then both `warehouse`, and every two-part reference raises
    "Binder Error: Ambiguous reference to catalog or schema". The fixture writes
    world.duckdb, whose stem does not collide, so a direct open here would work
    — and would still be wrong, because it would let table bodies through that
    only resolve under the convenient filename.
    """
    for f in (path, Path(str(path) + ".wal")):
        f.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{path.as_posix()}' AS wh")
    con.execute("USE wh")
    return con


def _readonly(path: Path, alias: str = "wh"):
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{path.as_posix()}' AS {alias} (READ_ONLY)")
    return con


def _close(con) -> None:
    con.execute("CHECKPOINT")
    con.close()


def _write_text(path: Path, text: str) -> None:
    """newline="" — no translation, ever.

    A CRLF that changes no row count has broken this repo's determinism
    guarantee four separate times. It is invisible to every length check, row
    count and parse test; only a byte-level cross-host diff finds it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


# -------------------------------------------------------------------- SQL ---
def _days_cte() -> str:
    return f"""
days AS (
  SELECT UNNEST(generate_series(DATE '{HIST_START}', DATE '{HIST_END}',
                                INTERVAL 1 DAY))::DATE AS d
),
indexed AS (SELECT d, (d - DATE '{HIST_START}')::BIGINT AS di FROM days)"""


def src_lines_sql(seed: int) -> str:
    """One row per order LINE, as the upstream source has it.

    Orders carry 1-3 lines, so (order_id, line_nbr) is a genuine composite
    business key rather than a column that is always 1. Everything else in the
    world — the fact table, the snapshot, the landing stream — is a projection
    of this, so they cannot disagree with each other.
    """
    ro = lambda n: RND.format(k="oseq", n=n, salt=seed)          # noqa: E731
    rl = lambda n: RND.format(k="rid", n=n, salt=seed)           # noqa: E731
    adj_dates = ", ".join(f"DATE '{x}'"
                          for x in ADJ_DATES_IN_WINDOW + ADJ_DATES_OUTSIDE)
    return f"""
WITH {_days_cte()},
orders AS (
  SELECT i.d, i.di, g.o, (i.di * 10000 + g.o) AS oseq
  FROM indexed i,
       LATERAL (SELECT UNNEST(generate_series(1, {ORDERS_PER_DAY})) AS o) g
),
sized AS (
  SELECT *, CASE WHEN {ro(11)} < 0.55 THEN 1
                 WHEN {ro(11)} < 0.85 THEN 2 ELSE 3 END AS nlines
  FROM orders
),
lines AS (
  SELECT s.d, s.di, s.o, s.oseq, g.ln AS line_nbr, (s.oseq * 10 + g.ln) AS rid
  FROM sized s,
       LATERAL (SELECT UNNEST(generate_series(1, s.nlines)) AS ln) g
),
attrs AS (
  SELECT d, di, o, line_nbr, rid,
         {rl(21)} AS r1, {rl(22)} AS r2, {rl(23)} AS r3,
         {rl(24)} AS r4, {rl(25)} AS r5, {rl(26)} AS r6,
         ({ID_BASE} + di * 10000 + o) AS order_id
  FROM lines
)
SELECT d, o, order_id, line_nbr,
       d                                                       AS order_dt,
       (d + INTERVAL ((r1*22)::INT + 1) HOUR
          + INTERVAL ((r2*59)::INT) MINUTE)::TIMESTAMP          AS order_ts,
       (1 + (r3*49999)::BIGINT)                                AS customer_id,
       'SKU-' || lpad(((r4*119)::INT + 1)::VARCHAR, 4, '0')    AS sku,
       (1 + (r5*3)::INT)                                       AS qty,
       ROUND(5 + r2*245, 2)::DECIMAL(10,2)                     AS src_price,
       CASE WHEN r6 < {PROMO_RATE}
            THEN 'PROMO-' || lpad(((r6*100000)::INT % {N_PROMOS} + 1)::VARCHAR,
                                  3, '0') END                  AS promo_id,
       (d IN ({adj_dates}) AND o <= {ADJ_PER_DATE} AND line_nbr = 1) AS is_adj
FROM attrs
ORDER BY d, order_id, line_nbr
"""


BASE_ORDERS_SQL = f"""
SELECT order_id, line_nbr, order_dt, order_ts, customer_id, sku, qty,
       CASE WHEN is_adj THEN ROUND(src_price * {ADJ_FACTOR}, 2)::DECIMAL(10,2)
            ELSE src_price END                                  AS unit_price,
       promo_id,
       is_adj                                                   AS is_manual_adjustment,
       CASE WHEN is_adj
            THEN 'finance restatement: unit price corrected' END AS adjustment_note,
       CASE WHEN is_adj
            THEN (order_dt + INTERVAL 5 DAY + INTERVAL 16 HOUR
                           + INTERVAL 30 MINUTE)::TIMESTAMP
            ELSE (order_dt + INTERVAL 1 DAY + INTERVAL 2 HOUR)::TIMESTAMP END
                                                                AS load_ts,
       CASE WHEN is_adj THEN 'adj-' || strftime(order_dt, '%Y%m%d')
            ELSE 'nightly-' || strftime(order_dt + INTERVAL 1 DAY, '%Y%m%d') END
                                                                AS batch_id
FROM src_lines
ORDER BY order_dt, order_id, line_nbr
"""

# THE DEFECT. The backfill re-read raw.orders_snapshot and APPENDED. The
# snapshot has never heard of the finance adjustments, so the second copy of an
# adjusted line carries the original source price and no adjustment flag —
# which is exactly why SELECT DISTINCT does not collapse it.
FCT_ORDERS_SQL = f"""
SELECT * FROM base_orders
UNION ALL
SELECT order_id, line_nbr, order_dt, order_ts, customer_id, sku, qty,
       CASE WHEN is_manual_adjustment
            THEN ROUND(unit_price / {ADJ_FACTOR}, 2)::DECIMAL(10,2)
            ELSE unit_price END,
       promo_id, FALSE, NULL,
       TIMESTAMP '{RETRY_TS}', '{RETRY_BATCH}'
FROM base_orders
WHERE order_dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}'
ORDER BY order_dt, order_id, line_nbr, batch_id
"""

# raw.orders_snapshot — the frozen upstream nightly extract. Source values only:
# no adjustments, because finance edits fct_orders in place and the upstream
# system never learns about it. This table is what makes the destructive repair
# available and plausible, and exactly why the destructive repair loses points.
SNAPSHOT_SQL = f"""
SELECT order_id, line_nbr, order_dt, order_ts, customer_id, sku, qty,
       src_price AS unit_price, promo_id,
       TIMESTAMP '{LAST_NIGHTLY_TS}' AS snapshot_ts,
       'snapshot-20260308'           AS snapshot_id
FROM src_lines
ORDER BY order_dt, order_id, line_nbr
"""

# raw.orders_landing — the delta stream, retained three days. These rows are
# already in fct_orders. They are the LIVENESS reference: they show, in the world
# itself, how a day's orders normally arrive and what the merge target looks
# like. February cannot be rebuilt from here — the stream does not retain that
# far — which is why an in-place repair is the only non-destructive option.
#
# arrival_ts is order_dt + 1 day + 00:40, i.e. FORTY MINUTES BEFORE
# load_orders_delta starts at 01:20 on the same date (ops.job_runs, and the
# runbook on the wiki). It used to be 01:40 — twenty minutes AFTER the job that
# claims to have loaded it — which is the sort of thing a careful agent notices
# and reports as a cause. It was never a cause; it was a fixture bug, and an
# incoherent clock in the world costs a correct answer a false-cause penalty.
LANDING_SQL = f"""
SELECT (order_dt + INTERVAL 1 DAY + INTERVAL 40 MINUTE)::TIMESTAMP  AS arrival_ts,
       'nightly-' || strftime(order_dt + INTERVAL 1 DAY, '%Y%m%d')  AS batch_id,
       order_id, line_nbr, order_dt, order_ts, customer_id, sku, qty,
       src_price AS unit_price, promo_id,
       'landing/dt=' || strftime(order_dt, '%Y-%m-%d') || '/orders.csv'
                                                                    AS source_file
FROM src_lines
WHERE order_dt >= DATE '{LANDING_RETAIN_FROM}'
ORDER BY order_dt, order_id, line_nbr
"""

PROMO_SQL = f"""
SELECT 'PROMO-' || lpad(i::VARCHAR, 3, '0')  AS promo_id,
       'Promotion ' || i::VARCHAR            AS promo_name,
       (5 + (i % 5) * 5)::DECIMAL(5,2)       AS discount_pct,
       DATE '{HIST_START}'                   AS valid_from,
       DATE '{HIST_END}'                     AS valid_to
FROM (SELECT UNNEST(generate_series(1, {N_PROMOS})) AS i)
ORDER BY 1
"""


def revenue_sql(src_table: str, promo_dim: str, *, stale: bool) -> str:
    """warehouse.fct_revenue.

    stale=True plants defect 2: every row with order_dt < SWITCH_DATE keeps the
    pre-switch layout — discount_amount 0.00, tax_amount 0.00, tax folded into
    net_amount. stale=False is the same world with the full refresh that was
    never run, i.e. the reference.

    Exactly one row per (order_id, line_nbr) either way. The mart is never
    duplicated, which is what makes the doubled window surface as order_lines
    doubling while net_revenue does not.
    """
    disc = "COALESCE(ROUND(gross * discount_pct / 100.0, 2), 0.00)"
    if stale:
        old = f"order_dt < DATE '{SWITCH_DATE}'"
        discount = f"CASE WHEN {old} THEN 0.00 ELSE disc END"
        tax = (f"CASE WHEN {old} THEN 0.00 "
               f"ELSE ROUND((gross - disc) * {TAX_RATE}, 2) END")
        net = (f"CASE WHEN {old} THEN gross + ROUND(gross * {TAX_RATE}, 2) "
               f"ELSE gross - disc + ROUND((gross - disc) * {TAX_RATE}, 2) END")
        load = (f"CASE WHEN {old} "
                f"THEN (order_dt + INTERVAL 1 DAY + INTERVAL 2 HOUR)::TIMESTAMP "
                f"ELSE (order_dt + INTERVAL 1 DAY + INTERVAL 2 HOUR "
                f"     + INTERVAL 20 MINUTE)::TIMESTAMP END")
    else:
        discount = "disc"
        tax = f"ROUND((gross - disc) * {TAX_RATE}, 2)"
        net = f"gross - disc + ROUND((gross - disc) * {TAX_RATE}, 2)"
        load = ("(order_dt + INTERVAL 1 DAY + INTERVAL 2 HOUR "
                "  + INTERVAL 20 MINUTE)::TIMESTAMP")
    return f"""
WITH o AS (
  SELECT order_id, line_nbr, order_dt, qty, unit_price, promo_id
  FROM {src_table}
),
p AS (SELECT *, ROUND(qty * unit_price, 2) AS gross FROM o),
j AS (
  SELECT p.*, {disc} AS disc
  FROM p LEFT JOIN {promo_dim} d ON d.promo_id = p.promo_id
)
SELECT order_id, line_nbr, order_dt,
       gross::DECIMAL(12,2)     AS gross_amount,
       promo_id,
       ({discount})::DECIMAL(12,2) AS discount_amount,
       ({tax})::DECIMAL(12,2)      AS tax_amount,
       ({net})::DECIMAL(12,2)      AS net_amount,
       {load}                      AS load_ts,
       'rev-' || strftime(order_dt + INTERVAL 1 DAY, '%Y%m%d') AS batch_id
FROM j
ORDER BY order_dt, order_id, line_nbr
"""


def kpi_sql(orders_tbl: str, revenue_tbl: str) -> str:
    """warehouse.kpi_daily — a MATERIALISED table, not a view.

    Deliberate. A mart that is right while the reporting table is stale is
    exactly the G4 partial. If kpi_daily were a view, repairing fct_orders would
    silently repair the reporting surface too and G4 would be free.
    """
    return f"""
WITH o AS (
  SELECT order_dt AS dt, COUNT(*) AS order_lines,
         COUNT(DISTINCT order_id) AS orders,
         ROUND(SUM(qty * unit_price), 2)::DECIMAL(14,2) AS gross_revenue
  FROM {orders_tbl} GROUP BY 1
),
r AS (
  SELECT order_dt AS dt, ROUND(SUM(net_amount), 2)::DECIMAL(14,2) AS net_revenue
  FROM {revenue_tbl} GROUP BY 1
)
SELECT o.dt, o.order_lines, o.orders, o.gross_revenue,
       COALESCE(r.net_revenue, 0)::DECIMAL(14,2) AS net_revenue,
       ROUND(COALESCE(r.net_revenue, 0) / NULLIF(o.orders, 0), 2)::DECIMAL(12,2) AS aov
FROM o LEFT JOIN r USING (dt)
ORDER BY 1
"""


# ---------------------------------------------------------- green surfaces ---
# A027. Everything a monitor looks at says the pipeline is healthy. dq_checks
# carries row_count and not_null checks and NOTHING ELSE: no uniqueness
# assertion on (order_id, line_nbr), no reconciliation of net_amount against
# gross/discount/tax. Both defects are unmonitored by construction, not by
# omission — which is the archetype.
JOB_RUNS_SQL = f"""
WITH {_days_cte()},
j AS (
  SELECT d, x.job, x.ord FROM indexed,
    (VALUES ('load_orders_delta', 1), ('load_revenue', 2), ('dbt_build', 3))
      AS x(job, ord)
)
SELECT 'run-' || strftime(d + INTERVAL 1 DAY, '%Y%m%d') || '-' || ord::VARCHAR AS run_id,
       job,
       (d + INTERVAL 1 DAY + INTERVAL 1 HOUR
          + INTERVAL (ord * 20) MINUTE)::TIMESTAMP AS started_at,
       (d + INTERVAL 1 DAY + INTERVAL 1 HOUR + INTERVAL (ord * 20) MINUTE
          + INTERVAL 90 SECOND)::TIMESTAMP         AS ended_at,
       'success'                                   AS status,
       'dt=' || strftime(d, '%Y-%m-%d')            AS detail
FROM j
UNION ALL
SELECT 'run-20260301-backfill', 'backfill_orders_window',
       TIMESTAMP '{RETRY_TS}', TIMESTAMP '2026-03-01 02:21:38', 'success',
       '--from {WIN_START} --to {WIN_END} --mode append'
ORDER BY 3, 1
"""

FRESHNESS_SQL = f"""
WITH {_days_cte()},
c AS (
  SELECT d, x.dataset FROM indexed,
    (VALUES ('warehouse.fct_orders'), ('warehouse.fct_revenue'),
            ('warehouse.kpi_daily')) AS x(dataset)
  WHERE d >= DATE '2026-02-06'
)
SELECT (d + INTERVAL 1 DAY + INTERVAL 3 HOUR)::TIMESTAMP AS checked_at,
       dataset, d AS max_partition_dt, 24 AS threshold_hours, 'PASS' AS status
FROM c ORDER BY 1, 2
"""

DQ_SQL = f"""
WITH {_days_cte()},
c AS (
  SELECT d, x.check_name, x.dataset FROM indexed,
    (VALUES ('row_count_gt_zero',     'warehouse.fct_orders'),
            ('not_null_order_id',     'warehouse.fct_orders'),
            ('not_null_order_dt',     'warehouse.fct_orders'),
            ('row_count_gt_zero',     'warehouse.fct_revenue'),
            ('not_null_gross_amount', 'warehouse.fct_revenue'),
            ('row_count_gt_zero',     'warehouse.kpi_daily'))
      AS x(check_name, dataset)
  WHERE d >= DATE '2026-02-06'
)
SELECT (d + INTERVAL 1 DAY + INTERVAL 3 HOUR + INTERVAL 5 MINUTE)::TIMESTAMP
         AS checked_at,
       dataset, check_name, 'PASS' AS status, 0 AS failed_rows
FROM c ORDER BY 1, 2, 3
"""

# The BI-facing relation. A MATERIALISED extract, not a view over the mart, and
# that is the whole design: gate G4 asks whether the downstream exposure shows the
# CORRECTED number, which is only a question if repairing the mart does not repair
# the exposure as a side effect. If the dashboard read warehouse.kpi_daily
# directly, every agent that passed G1 would collect G4 for free and the gate
# would degenerate into "did you break the yaml".
#
# It lives in the `warehouse` schema on purpose: world/fingerprint.py hashes only
# raw/staging/warehouse, so a `bi` schema inside the database would be invisible
# to the G5 section fingerprint AND to the repo's own determinism gate.
BI_EXTRACT_SQL = """
SELECT dt, order_lines, orders, gross_revenue, net_revenue, aov
FROM warehouse.kpi_daily ORDER BY dt
"""

WATERMARK_SQL = f"""
SELECT * FROM (VALUES
  ('load_orders_delta', DATE '{HIST_END}', TIMESTAMP '{LAST_NIGHTLY_TS}'),
  ('load_revenue',      DATE '{HIST_END}', TIMESTAMP '2026-03-08 02:20:00'),
  ('dbt_build',         DATE '{HIST_END}', TIMESTAMP '2026-03-08 02:40:00')
) AS t(job, last_loaded_dt, updated_at) ORDER BY 1
"""


# ----------------------------------------------------------- world builder ---
def build_world(path: Path, seed: int) -> None:
    con = _create(path)
    con.execute("CREATE SCHEMA raw")
    con.execute("CREATE SCHEMA warehouse")
    con.execute("CREATE SCHEMA ops")

    con.execute(f"CREATE TEMP TABLE src_lines AS {src_lines_sql(seed)}")
    con.execute(f"CREATE TEMP TABLE base_orders AS {BASE_ORDERS_SQL}")

    con.execute(f"CREATE TABLE warehouse.dim_promotions AS {PROMO_SQL}")
    con.execute(f"CREATE TABLE warehouse.fct_orders AS {FCT_ORDERS_SQL}")
    con.execute(f"CREATE TABLE raw.orders_snapshot AS {SNAPSHOT_SQL}")
    con.execute(f"CREATE TABLE raw.orders_landing AS {LANDING_SQL}")

    # The mart is built from the TRUE business state (base_orders, i.e. the
    # non-retry rows), which is why the doubling surfaces as order_lines doubling
    # while net_revenue does not.
    con.execute("CREATE TABLE warehouse.fct_revenue AS "
                + revenue_sql("base_orders", "warehouse.dim_promotions", stale=True))
    con.execute("CREATE TABLE warehouse.kpi_daily AS "
                + kpi_sql("warehouse.fct_orders", "warehouse.fct_revenue"))
    con.execute(f"CREATE TABLE {BI_EXTRACT} AS {BI_EXTRACT_SQL}")

    # models/reporting. A separate schema on purpose: these are read by people,
    # a few times a month, and nothing in the nightly chain depends on them.
    # They are built from the world AS IT IS, duplicates and all, because that
    # is what the last `dbt build` in this environment would have produced.
    con.execute("CREATE SCHEMA reporting")
    for name, body in REPORTING_MODELS.items():
        con.execute(f"CREATE TABLE reporting.{name} AS {derender(body)}")

    con.execute(f"CREATE TABLE ops.job_runs AS {JOB_RUNS_SQL}")
    con.execute(f"CREATE TABLE ops.freshness_checks AS {FRESHNESS_SQL}")
    con.execute(f"CREATE TABLE ops.dq_checks AS {DQ_SQL}")
    con.execute(f"CREATE TABLE warehouse.pipeline_watermark AS {WATERMARK_SQL}")
    _close(con)


# ------------------------------------------- reference + late-arrival payload ---
# ---------------------------------------------------------------------------
# ONE definition of an arriving row, used THREE ways, which is the only reason
# there is one truth about the payload rather than three:
#
#   1. `late_rows_sql` materialises it to write late_arrivals/dt=*/orders.csv and
#      to build the `warehouse_t1` reference;
#   2. `apply_late_arrivals.py` replays those CSVs into raw.orders_landing;
#   3. CONTRACT["probes"]["late_arrival"]["payload"]["sql"] is the SAME
#      expression with {id} and {date} as placeholders, so the G3 probe — which
#      synthesises its payload from a row template rather than reading my files —
#      plants exactly the rows the reference was built from.
#
# fixture_contract.py::_payload_equivalence_check executes route 3 against a
# scratch copy and compares it to route 2 row for row. Two mechanisms that are
# supposed to agree and are never compared is how a reference goes stale.
#
# Every field is a pure function of (order_id, order_dt). That is what lets a
# `{id}`/`{date}` template reproduce it; it is also why a restated line comes
# back with different qty / unit_price / sku than the line it restates.
LATE_ROW_FIELDS = """\
{id}, 1, DATE '{date}',
(DATE '{date}' + INTERVAL (({id} % 22) + 1) HOUR
               + INTERVAL ({id} % 59) MINUTE)::TIMESTAMP,
(1 + ({id} % 49999))::BIGINT,
'SKU-' || lpad((({id} % 119) + 1)::VARCHAR, 4, '0'),
(1 + ({id} % 3))::INTEGER,
ROUND(5 + ({id} % 24000) / 100.0, 2)::DECIMAL(10,2),
CASE WHEN ({id} % 100) < 32
     THEN 'PROMO-' || lpad((({id} % 40) + 1)::VARCHAR, 3, '0') END"""

LANDING_INSERT_COLS = ("arrival_ts, batch_id, order_id, line_nbr, order_dt, "
                       "order_ts, customer_id, sku, qty, unit_price, promo_id, "
                       "source_file")


def landing_insert_template(arrival_ts: str, batch: str) -> str:
    """A one-row INSERT with {id} and {date} left as format placeholders.

    This is what goes in the contract for the G2 plant and the G3 payload. The
    probe calls .format(n=, date=, id=, amount=) on it; {n} and {amount} are
    unused here because every field is derived from the id, which is what makes
    the probe's rows and the fixture's rows the same rows.
    """
    return (f"INSERT INTO raw.orders_landing ({LANDING_INSERT_COLS}) SELECT "
            f"TIMESTAMP '{arrival_ts}', '{batch}', {LATE_ROW_FIELDS}, "
            f"'landing/orders/dt={{date}}/orders.csv'").replace("\n", " ")


def late_rows_sql() -> str:
    """The late-arrival payload, derived FROM THE WORLD so it cannot drift.

    NEW lines use a disjoint id range (LATE_ID_BASE), so they collide with
    nothing. RESTATED lines are existing keys whose within-day order ordinal sits
    in [RESTATE_ORD_LO, RESTATE_ORD_HI] — far above ADJ_PER_DATE, so a restated
    key is never a manually adjusted key and a correct merge never has to choose
    between finance and upstream.
    """
    fields = LATE_ROW_FIELDS.replace("{id}", "order_id").replace(
        "DATE '{date}'", "d")
    return f"""
WITH wdays AS (
  SELECT UNNEST(generate_series(DATE '{WIN_START}', DATE '{WIN_END}',
                                INTERVAL 1 DAY))::DATE AS d
),
wi AS (SELECT d, (d - DATE '{WIN_START}')::BIGINT AS wdi,
               (d - DATE '{HIST_START}')::BIGINT AS hdi FROM wdays),
ids AS (
  SELECT d, ({LATE_ID_BASE} + wdi * 100 + g.k) AS order_id, 'new' AS change_type
  FROM wi, LATERAL (SELECT UNNEST(generate_series(1, {LATE_NEW_PER_DAY})) AS k) g
  UNION ALL
  SELECT d, ({ID_BASE} + hdi * 10000 + {RESTATE_ORD_LO} - 1 + g.k),
         'restated'
  FROM wi, LATERAL (SELECT UNNEST(generate_series(1, {LATE_RESTATE_PER_DAY})) AS k) g
)
SELECT TIMESTAMP '{LATE_ARRIVAL_TS}' AS arrival_ts,
       '{LATE_BATCH}'                AS batch_id,
       t.* , change_type
FROM ids, LATERAL (SELECT {fields}) AS t(order_id, line_nbr, order_dt, order_ts,
                                         customer_id, sku, qty, unit_price,
                                         promo_id)
ORDER BY order_dt, change_type, order_id, line_nbr
"""


def late_events() -> list[dict]:
    """`probes.late_arrival.payload.events` — 2 events per window day.

    One in the disjoint id range (genuinely new lines) and one in the existing
    range (upstream restatements of lines already in the fact table). The
    restatement events are what separate "merge" from "append": a new line that
    is appended twice shows up as a duplicate key, but so does a restatement that
    is appended once.

    `adds` is how many rows the event puts INTO the fact table, which is not
    `rows` for a restatement: a merge replaces the row it already holds, so the
    day's count does not move and `payload.predicate` - an id-range predicate -
    cannot see it either. The probe defaults `adds` to `rows`, so a payload of
    genuinely new lines needs no such key; this one does, and without it the
    probe expects 196 arrivals where a CORRECT merge produces 126 and the gate
    zeroes for every agent including the oracle (measured 2026-09-14).
    `rows` is unchanged, so all 196 rows are still planted and the CSV-versus-
    template equivalence check still compares the same payload.
    """
    d0 = dt.date.fromisoformat(WIN_START)
    dh = (d0 - dt.date.fromisoformat(HIST_START)).days
    out = []
    for i in range((dt.date.fromisoformat(WIN_END) - d0).days + 1):
        day = (d0 + dt.timedelta(days=i)).isoformat()
        out.append({"date": day, "rows": LATE_NEW_PER_DAY,
                    "adds": LATE_NEW_PER_DAY,
                    "id_base": LATE_ID_BASE + i * 100 + 1, "kind": "new"})
        out.append({"date": day, "rows": LATE_RESTATE_PER_DAY,
                    "adds": 0,
                    "id_base": ID_BASE + (dh + i) * 10000 + RESTATE_ORD_LO,
                    "kind": "restated"})
    return out


LATE_CSV_HEADER = ["arrival_ts", "batch_id", "order_id", "line_nbr", "order_dt",
                   "order_ts", "customer_id", "sku", "qty", "unit_price", "promo_id"]


def build_expected_and_payload(out: Path, seed: int) -> dict:
    """expected.duckdb (both reference states) + the late-arrival payload.

    expected is DERIVED from world.duckdb rather than regenerated. That is the
    whole reason it cannot drift from it: ref.warehouse.fct_orders is literally
    "world minus the retry batch", which is the repair stated as a query. A
    second independent generator would be a second thing to keep in sync, and
    keeping two generators in sync is how a stale reference got written under a
    validated run before.
    """
    world, exp = out / "world.duckdb", out / "expected.duckdb"
    for f in (exp, Path(str(exp) + ".wal")):
        f.unlink(missing_ok=True)

    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{world.as_posix()}' AS src (READ_ONLY)")
    con.execute(f"ATTACH '{exp.as_posix()}' AS ref")
    con.execute("USE src")          # so unqualified raw.* / warehouse.* read world
    con.execute("CREATE SCHEMA ref.warehouse")
    con.execute("CREATE SCHEMA ref.warehouse_t1")

    ocols = ", ".join(BUSINESS_COLS["fct_orders"])
    rcols = ", ".join(BUSINESS_COLS["fct_revenue"])
    kcols = ", ".join(BUSINESS_COLS["kpi_daily"])

    # ---- t0 reference: both defects repaired --------------------------------
    con.execute(f"""
      CREATE TEMP TABLE t0_orders AS
      SELECT {ocols} FROM src.warehouse.fct_orders
      WHERE batch_id <> '{RETRY_BATCH}'
      ORDER BY order_dt, order_id, line_nbr""")
    con.execute("CREATE TABLE ref.warehouse.fct_orders AS "
                "SELECT * FROM t0_orders ORDER BY order_dt, order_id, line_nbr")
    con.execute(
        f"CREATE TABLE ref.warehouse.fct_revenue AS SELECT {rcols} FROM ("
        + revenue_sql("t0_orders", "src.warehouse.dim_promotions", stale=False)
        + ") ORDER BY order_dt, order_id, line_nbr")
    con.execute(
        f"CREATE TABLE ref.warehouse.kpi_daily AS SELECT {kcols} FROM ("
        + kpi_sql("t0_orders", "ref.warehouse.fct_revenue") + ") ORDER BY dt")

    # ---- the late-arrival payload -------------------------------------------
    con.execute(f"CREATE TEMP TABLE late_rows AS {late_rows_sql()}")

    # ---- t1 reference: t0 + the late arrivals merged EXACTLY ONCE ------------
    con.execute(f"""
      CREATE TEMP TABLE t1_orders AS
      SELECT {ocols} FROM (
        SELECT o.order_id, o.line_nbr, o.order_dt,
               COALESCE(l.order_ts, o.order_ts)         AS order_ts,
               COALESCE(l.customer_id, o.customer_id)   AS customer_id,
               COALESCE(l.sku, o.sku)                   AS sku,
               COALESCE(l.qty, o.qty)                   AS qty,
               COALESCE(l.unit_price, o.unit_price)     AS unit_price,
               COALESCE(l.promo_id, o.promo_id)         AS promo_id,
               o.is_manual_adjustment
        FROM t0_orders o
        LEFT JOIN late_rows l
               ON l.order_id = o.order_id AND l.line_nbr = o.line_nbr
        UNION ALL
        SELECT order_id, line_nbr, order_dt, order_ts, customer_id, sku, qty,
               unit_price, promo_id, FALSE
        FROM late_rows WHERE change_type = 'new')
      ORDER BY order_dt, order_id, line_nbr""")
    con.execute("CREATE TABLE ref.warehouse_t1.fct_orders AS "
                "SELECT * FROM t1_orders ORDER BY order_dt, order_id, line_nbr")
    con.execute(
        f"CREATE TABLE ref.warehouse_t1.fct_revenue AS SELECT {rcols} FROM ("
        + revenue_sql("t1_orders", "src.warehouse.dim_promotions", stale=False)
        + ") ORDER BY order_dt, order_id, line_nbr")
    con.execute(
        f"CREATE TABLE ref.warehouse_t1.kpi_daily AS SELECT {kcols} FROM ("
        + kpi_sql("t1_orders", "ref.warehouse_t1.fct_revenue") + ") ORDER BY dt")

    rows = con.execute(
        "SELECT arrival_ts, batch_id, order_id, line_nbr, order_dt, order_ts, "
        "customer_id, sku, qty, unit_price, promo_id, change_type FROM late_rows "
        "ORDER BY order_dt, change_type, order_id, line_nbr").fetchall()
    con.execute("CHECKPOINT")
    con.close()
    return _write_payload(out, rows)


def _write_payload(out: Path, rows: list) -> dict:
    """One CSV per day of the window + a HARNESS-SIDE manifest.

    The CSV carries NO change_type column. A landing file from an upstream system
    does not annotate which of its records are restatements — and if it did, the
    G3 merge would be handed its own answer. The new/restated accounting lives in
    late_arrivals_manifest.json, written OUTSIDE late_arrivals/ so that staging
    the payload directory into the world cannot leak it.
    """
    payload = out / "late_arrivals"
    if payload.exists():
        shutil.rmtree(payload)
    by_day: dict[str, list] = {}
    for r in rows:
        by_day.setdefault(str(r[4]), []).append(r)

    partitions = []
    for dt in sorted(by_day):
        drows = by_day[dt]
        p = payload / f"dt={dt}" / "orders.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(LATE_CSV_HEADER)
            for r in drows:
                w.writerow([str(r[0]), r[1], r[2], r[3], str(r[4]), str(r[5]),
                            r[6], r[7], r[8], f"{r[9]:.2f}",
                            "" if r[10] is None else r[10]])
        partitions.append({
            "dt": dt,
            "file": f"dt={dt}/orders.csv",
            "rows": len(drows),
            "new_keys": sum(1 for x in drows if x[11] == "new"),
            "restated_keys": sum(1 for x in drows if x[11] == "restated"),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        })

    manifest = {
        "arrival_ts": LATE_ARRIVAL_TS,
        "batch_id": LATE_BATCH,
        "window_start": WIN_START,
        "window_end": WIN_END,
        "csv_header": LATE_CSV_HEADER,
        "total_rows": len(rows),
        "total_new_keys": sum(1 for r in rows if r[11] == "new"),
        "total_restated_keys": sum(1 for r in rows if r[11] == "restated"),
        "new_keys": [[r[2], r[3]] for r in rows if r[11] == "new"],
        "restated_keys": [[r[2], r[3]] for r in rows if r[11] == "restated"],
        "partitions": partitions,
        "apply_with": "fixtures/apply_late_arrivals.py",
        "note": ("HARNESS-SIDE. Never stage this file into the world root: it "
                 "names every new and restated key. The dt=* partitions under "
                 "late_arrivals/ are the only agent-visible part, and only at t1."),
    }
    _write_text(out / "late_arrivals_manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


# ---------------------------------------------------------------- workspace ---
DBT_FCT_ORDERS = """\
{{ config(materialized='incremental', unique_key=['order_id', 'line_nbr']) }}

-- Nightly delta load. raw.orders_landing retains three days; anything older has
-- to come from the snapshot reconciliation.

select
    order_id,
    line_nbr,
    order_dt,
    order_ts,
    customer_id,
    sku,
    qty,
    unit_price,
    promo_id,
    false as is_manual_adjustment,
    cast(null as varchar) as adjustment_note,
    arrival_ts as load_ts,
    batch_id
from {{ source('raw', 'orders_landing') }}
{% if is_incremental() %}
where arrival_ts > (select coalesce(max(load_ts), '1900-01-01'::timestamp)
                    from {{ this }})
{% endif %}
"""

DBT_FCT_REVENUE = """\
{{ config(materialized='incremental', unique_key=['order_id', 'line_nbr']) }}

-- 2025-12-01: switched from materialized='table' to 'incremental' so the nightly
-- build stops rewriting six months of history. Same change split the old
-- tax-inclusive net_amount into discount_amount / tax_amount / net_amount.
-- See pipelines/README.md.

with o as (
    select order_id, line_nbr, order_dt, qty, unit_price, promo_id,
           load_ts, batch_id
    from {{ ref('fct_orders') }}
),

p as (
    select o.*, round(o.qty * o.unit_price, 2) as gross_amount
    from o
),

j as (
    select p.*,
           coalesce(round(p.gross_amount * d.discount_pct / 100.0, 2), 0.00)
               as discount_amount
    from p
    left join {{ source('warehouse', 'dim_promotions') }} d
        on d.promo_id = p.promo_id
)

select
    order_id,
    line_nbr,
    order_dt,
    gross_amount,
    promo_id,
    discount_amount,
    round((gross_amount - discount_amount) * {{ var('tax_rate') }}, 2)
        as tax_amount,
    gross_amount - discount_amount
        + round((gross_amount - discount_amount) * {{ var('tax_rate') }}, 2)
        as net_amount,
    load_ts,
    batch_id
from j
{% if is_incremental() %}
where order_dt > (select coalesce(max(order_dt), '1900-01-01'::date) from {{ this }})
{% endif %}
"""

PIPE_BACKFILL = f"""\
-- backfill_orders_window
-- usage: --from <date> --to <date> [--mode append]
--
-- Re-reads the frozen upstream snapshot for a date range and writes it into the
-- fact table. `append` is the default and the only mode that is implemented.

INSERT INTO warehouse.fct_orders
SELECT order_id, line_nbr, order_dt, order_ts, customer_id, sku, qty, unit_price,
       promo_id, FALSE, NULL, now()::TIMESTAMP, :batch_id
FROM raw.orders_snapshot
WHERE order_dt BETWEEN :from_dt AND :to_dt;
"""

PIPE_LOAD_DELTA = """\
-- load_orders_delta
-- Nightly 01:20. Appends the newest partition in the landing stream.

INSERT INTO warehouse.fct_orders
SELECT order_id, line_nbr, order_dt, order_ts, customer_id, sku, qty, unit_price,
       promo_id, FALSE, NULL, arrival_ts, batch_id
FROM raw.orders_landing
WHERE order_dt = (SELECT MAX(order_dt) FROM raw.orders_landing);

UPDATE warehouse.pipeline_watermark
   SET last_loaded_dt = (SELECT MAX(order_dt) FROM raw.orders_landing),
       updated_at     = (SELECT MAX(arrival_ts) FROM raw.orders_landing)
 WHERE job = 'load_orders_delta';
"""

PIPE_BUILD_MARTS = f"""\
-- build_marts
-- Nightly 01:40, after load_orders_delta. fct_revenue has been incremental
-- since 2025-12-01; kpi_daily is small enough to rebuild whole.

INSERT INTO warehouse.fct_revenue
SELECT o.order_id, o.line_nbr, o.order_dt,
       ROUND(o.qty * o.unit_price, 2)::DECIMAL(12,2) AS gross_amount,
       o.promo_id,
       COALESCE(ROUND(ROUND(o.qty * o.unit_price, 2) * d.discount_pct / 100.0, 2),
                0.00)::DECIMAL(12,2) AS discount_amount,
       ROUND((ROUND(o.qty * o.unit_price, 2)
              - COALESCE(ROUND(ROUND(o.qty * o.unit_price, 2)
                               * d.discount_pct / 100.0, 2), 0.00))
             * {TAX_RATE}, 2)::DECIMAL(12,2) AS tax_amount,
       (ROUND(o.qty * o.unit_price, 2)
        - COALESCE(ROUND(ROUND(o.qty * o.unit_price, 2)
                         * d.discount_pct / 100.0, 2), 0.00)
        + ROUND((ROUND(o.qty * o.unit_price, 2)
                 - COALESCE(ROUND(ROUND(o.qty * o.unit_price, 2)
                                  * d.discount_pct / 100.0, 2), 0.00))
                * {TAX_RATE}, 2))::DECIMAL(12,2) AS net_amount,
       o.load_ts, o.batch_id
FROM warehouse.fct_orders o
LEFT JOIN warehouse.dim_promotions d ON d.promo_id = o.promo_id
WHERE o.order_dt > (SELECT MAX(order_dt) FROM warehouse.fct_revenue);

CREATE OR REPLACE TABLE warehouse.kpi_daily AS
WITH o AS (
  SELECT order_dt AS dt, COUNT(*) AS order_lines,
         COUNT(DISTINCT order_id) AS orders,
         ROUND(SUM(qty * unit_price), 2)::DECIMAL(14,2) AS gross_revenue
  FROM warehouse.fct_orders GROUP BY 1
),
r AS (
  SELECT order_dt AS dt, ROUND(SUM(net_amount), 2)::DECIMAL(14,2) AS net_revenue
  FROM warehouse.fct_revenue GROUP BY 1
)
SELECT o.dt, o.order_lines, o.orders, o.gross_revenue,
       COALESCE(r.net_revenue, 0)::DECIMAL(14,2) AS net_revenue,
       ROUND(COALESCE(r.net_revenue, 0) / NULLIF(o.orders, 0), 2)::DECIMAL(12,2) AS aov
FROM o LEFT JOIN r USING (dt)
ORDER BY 1;
"""

PIPE_REFRESH_BI = f"""\
-- refresh_bi_extract
-- Nightly 02:00, after build_marts. {DASHBOARD_ID} reads this table, NOT
-- warehouse.kpi_daily: the dashboard has to serve at 07:00 without holding a
-- lock on the mart while it rebuilds.

CREATE OR REPLACE TABLE {BI_EXTRACT} AS
SELECT dt, order_lines, orders, gross_revenue, net_revenue, aov
FROM warehouse.kpi_daily ORDER BY dt;
"""

PIPE_README = f"""\
# pipelines

| file | schedule | reads | writes |
|---|---|---|---|
| `load_orders_delta.sql` | nightly 01:20 | `raw.orders_landing` | `warehouse.fct_orders` |
| `build_marts.sql` | nightly 01:40 | `warehouse.fct_orders` | `warehouse.fct_revenue`, `warehouse.kpi_daily` |
| `refresh_bi_extract.sql` | nightly 02:00 | `warehouse.kpi_daily` | `{BI_EXTRACT}` |
| `backfill_orders_window.sql` | manual | `raw.orders_snapshot` | `warehouse.fct_orders` |

`raw.orders_landing` retains three days. `raw.orders_snapshot` is a full upstream
extract refreshed by the vendor's nightly job; we do not control it, and it does
not carry anything finance has edited by hand.

The `{DASHBOARD_ID}` dashboard reads `{BI_EXTRACT}`, not `warehouse.kpi_daily`.
Rebuilding the mart does not move the dashboard until `refresh_bi_extract` runs.

## changelog

- 2025-12-01 — `fct_revenue` moved from `materialized='table'` to
  `materialized='incremental'` so the nightly build stops rewriting six months of
  history. Same change splits the old tax-inclusive `net_amount` into
  `discount_amount` / `tax_amount` / `net_amount`.
- 2026-03-01 — ran `backfill_orders_window --from {WIN_START} --to {WIN_END}`
  after the 02:00 load looked short. Completed in 4m38s, no errors.
"""

DASHBOARD_YML = f"""\
dashboard: {DASHBOARD_ID}
panels:
  - name: revenue_daily
    sql: "SELECT dt, order_lines, orders, gross_revenue, net_revenue, aov
      FROM {BI_EXTRACT} ORDER BY dt"
  - name: window_summary
    sql: "SELECT SUM(order_lines) AS order_lines, SUM(orders) AS orders,
      ROUND(SUM(gross_revenue), 2) AS gross_revenue,
      ROUND(SUM(net_revenue), 2) AS net_revenue
      FROM {BI_EXTRACT}
      WHERE dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}'"
"""

# ------------------------------------------------------------ the repo shell ---
# Everything below is what a shop this size actually has in the repo and this
# world did not: a README that predates the incident, a changelog, pinned
# requirements, CI, a task runner, committed dbt targets, decision records, a
# data dictionary, a runbook, operational scripts, and one archived pipeline
# nobody has deleted.
#
# NOTHING here states either defect. The two facts that read closest to a hint —
# that the dashboard serves a materialised extract rather than the mart, and
# that `backfill_orders_window` is run by hand — are both already in
# pipelines/README.md, which shipped with the first version of this world.

README = f"""\
# commercial

Order lines in, marts out, one dashboard on the end of it. The delta stream
lands in `raw`, `warehouse` holds the facts and the marts, and
`warehouse.bi_revenue_extract` is what `commercial_daily` serves.

## Layout

- `pipelines/` — the SQL the nightly scheduler runs, one file per job.
  `pipelines/README.md` is the table of what runs when and carries the
  pipeline's own changelog.
- `dbt_project/` — dbt Core project: staging views over `raw`, the two
  warehouse facts, and the reporting views on top of them.
- `config/` — dbt targets, warehouse settings, the nightly check definitions
- `docs/` — data dictionary, runbook, decision records
- `scripts/` — operational helpers, runnable from this directory
- `run.sh` — every command in this README is a target

`dbt_project/models/warehouse/` is the definition of record for `fct_orders`
and `fct_revenue`. `pipelines/*.sql` is what the scheduler actually executes.
The two carry the same arithmetic and are kept in step by hand; see
`docs/adr/0002-incremental-marts.md`.

## Getting started

```sh
./run.sh env              # print what it resolved to
./run.sh check            # dbt parse + pipeline check, same as CI
./run.sh build            # dbt build against the dev target
```

`run.sh` sources `.envrc` and exports `DBT_PROFILES_DIR=config`, so it uses the
committed profile rather than your `~/.dbt/profiles.yml`. A bare `dbt` will
not. Personal overrides go in `.env`, which `.envrc` sources on top of itself
and which is gitignored.

## Where the warehouse is

`../var/warehouse/warehouse.duckdb`, outside the repo. Override with
`DBT_WAREHOUSE_PATH`.

The file is called `warehouse.duckdb` and one of the schemas inside it is also
called `warehouse`. Those collide, and it has cost people an afternoon more
than once — read `docs/adr/0001-warehouse-file-and-attach-alias.md` before you
open the file from anything that is not dbt. `scripts/warehouse.py` does it the
right way and is three lines you can copy.

## Schedule

The partition for a given `order_dt` lands at 00:40 the following morning.
Then, nightly:

| time | file | writes |
|---|---|---|
| 01:20 | `pipelines/load_orders_delta.sql` | `warehouse.fct_orders` |
| 01:40 | `pipelines/build_marts.sql` | `warehouse.fct_revenue`, `warehouse.kpi_daily` |
| 02:00 | `pipelines/refresh_bi_extract.sql` | `warehouse.bi_revenue_extract` |

`backfill_orders_window` is not scheduled. It is run by hand.

Two things record those runs and they do not use the same names. The deployed
schedule is `var/sched/state.json` and the `sched_*` tools, keyed by the
pipeline file. `ops.job_runs` is the platform's own log and keys by ITS job
names: `load_orders_delta` at 01:20, `load_revenue` at 01:40 and `dbt_build` at
02:00 — so `build_marts.sql` shows up there as two rows and
`refresh_bi_extract` has no row of its own at all. Nobody has reconciled them.
It is on the list.

`./run.sh pipeline-check` compares `pipelines/` with `var/sched/state.json`.

## Checks

`config/checks.yml` is the written-down copy of what the platform checks every
night. Results land in `ops.freshness_checks` and `ops.dq_checks`.

## Contacts

`#data-eng` for the pipeline, `#commercial-reporting` for the pack. The period
pack is Commercial's workbook, not ours: we own everything up to and including
`warehouse.bi_revenue_extract`.
"""

CHANGELOG = f"""\
# Changelog

Newest first, repo-level. Changes to the nightly SQL are in
`pipelines/README.md`, which has its own changelog.

## 2026-02-27
- `scripts/check_pipeline.py` reads `var/sched/state.json` directly instead of
  the copy that used to be checked in under `docs/`. The checked-in copy is
  gone; it had been stale since December and two people had believed it.

## 2026-02-03
- `config/checks.yml` written down. The nightly checks existed only in the
  platform's own config before this, so nobody in this repo could see what was
  being watched without asking for it.

## 2026-01-14
- CI runs `./run.sh check` on every push: `dbt parse` plus the pipeline check.
  Two pull requests in a row renamed a model and not the pipeline SQL.

## {SWITCH_DATE}
- `fct_revenue` moved from `materialized='table'` to `materialized='incremental'`
  so the nightly build stops rewriting six months of history. The same change
  split the old tax-inclusive `net_amount` into `discount_amount`, `tax_amount`
  and `net_amount`. See `docs/adr/0002-incremental-marts.md`.

## 2025-11-09
- Postmortem actions from the November pack: `refresh_bi_extract` split out of
  `build_marts`, and `warehouse.bi_revenue_extract` became the relation the
  dashboard reads. See `docs/adr/0003-bi-extract-is-a-table.md`.

## 2025-10-06
- Reporting views moved into the dbt project. They had been three saved queries
  in three people's editors.

## {HIST_START}
- First green nightly: `raw.orders_landing` -> `warehouse.fct_orders` ->
  `warehouse.fct_revenue` -> `warehouse.kpi_daily`.
"""

GITIGNORE = """\
# dbt
dbt_project/target/
dbt_project/dbt_packages/
dbt_project/logs/
logs/

# Personal overrides. `.envrc` is committed and holds no secrets; `.env` is
# yours, is sourced on top of it, and never goes in here.
.env
.env.local

# Local scratch warehouses. The real one lives outside the repo - see README.
*.duckdb
*.duckdb.wal

# Working notes from an incident. Useful, not part of the pipeline.
evidence/

# python
__pycache__/
*.pyc
.venv/
"""

ENVRC = """\
# direnv. Committed on purpose: nothing in here is a secret, and the whole
# point is that a fresh checkout resolves to something that works.
#
# Anything that IS a secret comes from the platform vault and is injected by
# the deployment. The scheduled runs do not read this file at all.
#
# Not using direnv? `./run.sh` sources this file itself.

# Which config/profiles.yml target run.sh uses: dev | ci | prod
export DBT_TARGET="${DBT_TARGET:-dev}"

# dev target: the DuckDB file this checkout reads and writes. Left unset so
# run.sh resolves it to ../var/warehouse/warehouse.duckdb, which is where the
# warehouse actually is. Set it only if yours is somewhere else.
# export DBT_WAREHOUSE_PATH=

# prod. Not reachable from a developer checkout - the path is on a mount only
# the scheduler host has. Set here so that `dbt debug --target prod` fails with
# a path you can read rather than quietly falling back to dev.
export DBT_PROD_WAREHOUSE_PATH="${DBT_PROD_WAREHOUSE_PATH:-/mnt/warehouse/prod/warehouse.duckdb}"

# The scheduler's run history, relative to the repo root.
export SCHED_STATE="${SCHED_STATE:-../var/sched/state.json}"

# Personal overrides go in .env, which is gitignored. Keep anything that looks
# like a credential there and nowhere else in this tree.
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
"""

REQUIREMENTS = """\
# Runtime for the dbt project and for scripts/. Pinned: the warehouse file
# format is version-sensitive and a minor duckdb bump has rewritten pages
# under us before.
dbt-core==1.12.4
dbt-duckdb==1.11.0
duckdb==1.5.5
PyYAML==6.0.2
"""

RUN_SH = """\
#!/usr/bin/env bash
# Task runner for the commercial marts.
#
# Everything CI runs, and everything the runbook tells you to run, is a target
# here. A step that only exists in someone's shell history will be wrong by the
# time you need it.
set -euo pipefail

cd "$(dirname "$0")"
REPO="$PWD"

# .envrc is the committed environment; it sources .env on top of itself if you
# have one. The scheduled runs read neither.
if [ -f .envrc ]; then
  # shellcheck disable=SC1091
  . ./.envrc
fi

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

export DBT_PROFILES_DIR="$REPO/config"
export DBT_TARGET="${DBT_TARGET:-dev}"
if [ -z "${DBT_WAREHOUSE_PATH:-}" ]; then
  DBT_WAREHOUSE_PATH="$(cd "$REPO/.." && pwd)/var/warehouse/warehouse.duckdb"
fi
export DBT_WAREHOUSE_PATH
SCHED_STATE="${SCHED_STATE:-$REPO/../var/sched/state.json}"

DBT=(dbt --no-use-colors)
DBT_ARGS=(--project-dir "$REPO/dbt_project" --target "$DBT_TARGET"
          --target-path "$REPO/dbt_project/target"
          --log-path "$REPO/dbt_project/logs")

usage() {
  cat <<'USAGE'
usage: ./run.sh <target>

  env             print the resolved configuration (no secrets)
  parse           dbt parse
  compile         dbt compile
  run             dbt run
  test            dbt test
  build           dbt build (run + test)
  inventory       list warehouse relations and row counts
  pipeline-check  compare pipelines/ with the scheduler's run history
  check           dbt parse + pipeline-check   (what CI runs on every push)
USAGE
}

case "${1:-}" in
  env)
    echo "repo              $REPO"
    echo "dbt target        $DBT_TARGET"
    echo "dbt profiles dir  $DBT_PROFILES_DIR"
    echo "warehouse path    $DBT_WAREHOUSE_PATH"
    echo "scheduler state   $SCHED_STATE"
    ;;
  parse)     "${DBT[@]}" parse   "${DBT_ARGS[@]}" ;;
  compile)   "${DBT[@]}" compile "${DBT_ARGS[@]}" ;;
  run)       "${DBT[@]}" run     "${DBT_ARGS[@]}" ;;
  test)      "${DBT[@]}" test    "${DBT_ARGS[@]}" ;;
  build)     "${DBT[@]}" build   "${DBT_ARGS[@]}" ;;
  inventory) "$PY" scripts/warehouse.py ;;
  pipeline-check) "$PY" scripts/check_pipeline.py --state "$SCHED_STATE" ;;
  check)
    "${DBT[@]}" parse "${DBT_ARGS[@]}"
    "$PY" scripts/check_pipeline.py --state "$SCHED_STATE"
    ;;
  ""|-h|--help|help) usage ;;
  *) echo "unknown target: $1" >&2; usage >&2; exit 2 ;;
esac
"""

CI_YML = """\
name: ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: install
        run: pip install -r requirements.txt
      - name: check
        # parse the dbt project, then compare pipelines/ with what the
        # scheduler has actually been running
        run: ./run.sh check
        env:
          DBT_TARGET: ci
"""

# --------------------------------------------------------------- config/ ---
# No credential, no hostname that resolves, no key path that exists. Anything
# that would be a secret is named as a vault entry and nothing more.

CONFIG_PROFILES = """\
# dbt profiles for this project.
#
# run.sh exports DBT_PROFILES_DIR=config, so anything run through run.sh uses
# this file. A bare `dbt` picks up ~/.dbt/profiles.yml instead - that is the
# usual reason two people get two different answers out of the same model.
#
# The scheduled runs use neither: the platform renders its own profiles.yml
# with an absolute warehouse path immediately before it invokes dbt.
commercial:
  target: "{{ env_var('DBT_TARGET', 'dev') }}"
  outputs:

    # Developer checkout. The warehouse file lives outside the repo, one level
    # up from it, so that a `git clean -xfd` cannot delete it.
    dev:
      type: duckdb
      path: "{{ env_var('DBT_WAREHOUSE_PATH', '../var/warehouse/warehouse.duckdb') }}"
      threads: 1

    # CI builds against a throwaway file so a red build cannot corrupt a real one.
    ci:
      type: duckdb
      path: "{{ env_var('DBT_WAREHOUSE_PATH', '/tmp/ci_warehouse.duckdb') }}"
      threads: 1

    # Production. Not reachable from a developer checkout - the path is on a
    # mount only the scheduler host has. Kept here so `dbt debug --target prod`
    # fails with a path you can read rather than falling back to dev.
    prod:
      type: duckdb
      path: "{{ env_var('DBT_PROD_WAREHOUSE_PATH', '/mnt/warehouse/prod/warehouse.duckdb') }}"
      threads: 4
"""

CONFIG_WAREHOUSE = f"""\
# Warehouse settings this repo depends on and does not own.
#
# Nothing reads this file at runtime; the platform holds the authoritative
# copy. It is written down so that "how long does the landing stream keep a
# partition" has an answer in the repo rather than in someone's memory.

warehouse:
  backend: duckdb
  # Outside the repo. The catalog DuckDB derives from the filename collides
  # with the `warehouse` schema inside it - see docs/adr/0001.
  path: ../var/warehouse/warehouse.duckdb
  attach_alias: wh
  schemas:
    raw: landed upstream data, written by the platform ingestion jobs
    warehouse: facts, dimensions, marts and the BI extract
    ops: job runs, freshness checks, data quality checks

sources:

  orders_landing:
    relation: raw.orders_landing
    kind: delta stream
    grain: one row per order line per arrival
    retention_days: 3
    retained_from: "{LANDING_RETAIN_FROM}"
    # The partition for order_dt = D lands at 00:40 on D+1, forty minutes
    # before load_orders_delta starts. If it has not landed by then the job
    # runs and moves nothing; see docs/runbook_nightly_load.md.
    arrives_local_time: "00:40"
    swept_by: platform partition sweeper
    note: >-
      Three days. Anything older than that is gone from here and has to come
      from orders_snapshot.

  orders_snapshot:
    relation: raw.orders_snapshot
    kind: full vendor extract
    grain: one row per order line
    refreshed: nightly, by the vendor
    owner: upstream vendor, via the platform ingestion job
    note: >-
      Source values only. We do not control its schedule or its contents, and
      it does not carry anything that was edited by hand in this warehouse.

  dim_promotions:
    relation: warehouse.dim_promotions
    kind: dimension sync
    owner: Marketing campaign tool, via the platform promotions job
    vault: platform/promotions/api
    note: >-
      Replaced wholesale on each sync. Not a dbt model - this project only
      joins to it, which is why it is declared as a source.

bi:
  dashboard: {DASHBOARD_ID}
  reads: {BI_EXTRACT}
  definition: ../var/bi/dashboards/{DASHBOARD_ID}.yml
  refreshed_by: pipelines/refresh_bi_extract.sql
"""

CONFIG_CHECKS = """\
# The nightly checks, as the platform runs them.
#
# This file is documentation: the scheduler reads its own copy. Results land in
# ops.freshness_checks and ops.dq_checks, which is where to look when somebody
# asks whether the pipeline was healthy on a given night.

freshness:
  threshold_hours: 24
  datasets:
    - warehouse.fct_orders
    - warehouse.fct_revenue
    - warehouse.kpi_daily

data_quality:
  - relation: warehouse.fct_orders
    check: row_count_gt_zero
  - relation: warehouse.fct_orders
    check: not_null_order_id
  - relation: warehouse.fct_orders
    check: not_null_order_dt
  - relation: warehouse.fct_revenue
    check: row_count_gt_zero
  - relation: warehouse.fct_revenue
    check: not_null_gross_amount
  - relation: warehouse.kpi_daily
    check: row_count_gt_zero
"""

# ----------------------------------------------------------- dbt project ---
# `profile: commercial` must match the profile name the episode builder renders
# into <world_root>/var/dbt/profiles.yml, which is what the gateway passes to
# `dbt --profiles-dir`. If these two names ever diverge, every dbt tool in the
# episode fails with "Could not find profile named 'commercial'" and dbt is a
# dead surface again.
DBT_PROJECT_YML = """\
name: commercial
version: "1.4.0"
config-version: 2

profile: commercial

model-paths: ["models"]
macro-paths: ["macros"]
analysis-paths: ["analyses"]
test-paths: ["tests"]
clean-targets: ["target", "dbt_packages", "logs"]

vars:
  # The rate build_marts.sql applies. Declared here so a model and the pipeline
  # SQL cannot drift apart silently on the one number a reader would not notice.
  tax_rate: 0.0725

models:
  commercial:
    # Staging is a thin typed layer over raw; nothing there is worth storing.
    staging:
      +materialized: view
      +schema: staging
    # The facts. Both of these carry their own +materialized in the model file.
    warehouse:
      +materialized: table
      +schema: warehouse
    # Read a handful of times a month, by people, not by jobs. Tables, not
    # views: a view body that reads the `warehouse` schema will not bind here.
    # See macros/relations.sql.
    reporting:
      +materialized: table
      +schema: reporting
"""

# Copied from the platform's other dbt projects, where it is load-bearing for
# exactly the reason the comment gives. Do not "simplify" it.
DBT_MACRO_RELATIONS = """\
{#
  Render every ref() and source() WITHOUT the database qualifier - except
  inside a test, which renders it WITH one. Two different failures, one file.

  1. STORED BODIES MUST CARRY NO CATALOG NAME.

  dbt bakes the reference it renders into the stored body of every view it
  creates. dbt opens var/warehouse/warehouse.duckdb directly, so at creation
  time the catalog is named `warehouse` (DuckDB names the catalog after the
  file stem) and a three-part reference reads `warehouse.raw.orders_landing`.
  Every other reader ATTACHes the same file under an alias instead, and under
  that alias the baked `warehouse.` no longer resolves:

      Binder Error: Catalog "warehouse" does not exist!

  A two-part `raw.<table>` body binds against the view's own catalog, whatever
  that catalog happens to be called. So the fix is to make the reference carry
  no catalog name at all, rather than to make the writer and the reader agree
  on one: agreement is a convention, and the next tool to run dbt against this
  project brings its own profiles.yml.

  `this` is deliberately untouched: the CREATE statement's target still renders
  three-part and resolves fine, because dbt is connected to the file it is
  writing to.

  2. TWO-PART IS AMBIGUOUS WHEN THE SCHEMA SHARES THE CATALOG'S NAME.

  One of the schemas in warehouse.duckdb is itself called `warehouse`. Against
  a catalog that is also called `warehouse`, a two-part `warehouse.fct_orders`
  is not merely unqualified, it is ambiguous:

      Binder Error: Ambiguous reference to catalog or schema "warehouse"
      - use a fully qualified path like "warehouse.warehouse"

  So the rule is not "always strip" - it is "strip unless stripping would make
  the name ambiguous", i.e. unless `relation.schema == relation.database`.
  Everything that reads a `warehouse.` relation is a TABLE or an incremental
  model (or a test), whose body dbt executes and never stores, so the retained
  catalog name costs nothing. Everything whose body IS stored - the staging
  views - reads `raw.`, where stripping is both safe and necessary.

  The corollary is worth writing down because it is not obvious: in this
  warehouse a VIEW may not read a relation in the `warehouse` schema. Two-part
  will not bind at CREATE time and three-part would be baked into the stored
  body and break every aliased reader. A model that needs `fct_orders` is a
  table. That is why models/reporting is materialized as tables.

  3. TESTS ARE NOT STORED EITHER.

  A test is a SELECT dbt executes immediately against the file it opened
  directly. Nothing about it is persisted, so rule 1 does not apply and the
  database qualifier goes back on unconditionally - generic or singular,
  `model.resource_type` is `test` for both.
#}
{% macro _in_test() %}
  {% do return(model is defined and model is mapping
                and model.get('resource_type') == 'test') %}
{% endmacro %}

{% macro _qualify(relation) %}
  {% if _in_test() %}
    {% do return(relation) %}
  {% endif %}
  {% if relation.schema == relation.database %}
    {% do return(relation) %}
  {% endif %}
  {% do return(relation.include(database=false)) %}
{% endmacro %}

{% macro ref() %}
  {% do return(_qualify(builtins.ref(*varargs, **kwargs))) %}
{% endmacro %}

{% macro source(source_name, table_name) %}
  {% do return(_qualify(builtins.source(source_name, table_name))) %}
{% endmacro %}
"""

DBT_MACRO_SCHEMA = """\
{#
  Use the schema a model asks for, verbatim. dbt's default prefixes the
  target's schema onto a custom one, which would put the facts in
  `main_warehouse` and nothing downstream would find them.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
{{ custom_schema_name if custom_schema_name else target.schema }}
{%- endmacro %}
"""

DBT_SOURCES_YML = """\
version: 2

sources:

  - name: raw
    description: >
      Landed upstream data. Nothing in this project writes here - the platform
      ingestion jobs do.
    schema: raw
    tables:

      - name: orders_landing
        description: >
          The nightly delta stream, one partition per order date, retained for
          three days. Older partitions are swept by the platform, so a lookback
          further back than that finds nothing here.
        columns:
          - name: arrival_ts
            description: When the platform landed the partition.
          - name: batch_id
            description: The load that brought the row in.
          - name: order_id
            description: Order identifier assigned upstream.
          - name: line_nbr
            description: Line number within the order.
          - name: order_dt
            description: Business date of the order, and the partition key.

      - name: orders_snapshot
        description: >
          The vendor's full nightly extract. Source values only: it is produced
          upstream, we do not control it, and it carries nothing that was
          edited by hand in this warehouse.
        columns:
          - name: snapshot_id
            description: Which nightly extract the row came from.
          - name: snapshot_ts
            description: When that extract was cut.

  - name: warehouse
    description: >
      Relations in the `warehouse` schema that this project reads and does not
      build. Declared as sources rather than left as bare table references so
      that `dbt docs` and the lineage graph are not lying about the edges.
    schema: warehouse
    tables:
      - name: dim_promotions
        description: >
          Promotion dimension, synced wholesale from Marketing's campaign tool
          by the platform promotions job. One row per promotion, with the
          percentage the discount is computed from.
"""

# ------------------------------------------------------------ dbt models ---
DBT_STG_ORDERS_LANDING = """\
-- The delta stream, typed and named the way the marts want it.
--
-- A view: raw.orders_landing holds three days, so there is nothing here worth
-- storing. Anything older than the retention window is not in this relation and
-- never will be.

select
    arrival_ts,
    batch_id,
    order_id,
    line_nbr,
    order_dt,
    order_ts,
    customer_id,
    sku,
    qty,
    unit_price,
    round(qty * unit_price, 2) as line_amount,
    promo_id,
    source_file
from {{ source('raw', 'orders_landing') }}
"""

DBT_STG_ORDERS_SNAPSHOT = """\
-- The vendor's full nightly extract, typed.
--
-- Note what is not here: `is_manual_adjustment`. The snapshot is produced
-- upstream and has no notion of it, and defaulting it to false in this view
-- would read as a fact about the order rather than as an absence of one.

select
    snapshot_id,
    snapshot_ts,
    order_id,
    line_nbr,
    order_dt,
    order_ts,
    customer_id,
    sku,
    qty,
    unit_price,
    round(qty * unit_price, 2) as line_amount,
    promo_id
from {{ source('raw', 'orders_snapshot') }}
"""

DBT_STAGING_SCHEMA_YML = """\
version: 2

models:

  - name: stg_orders_landing
    description: >
      One row per order line per arrival, as landed. Three days of retention;
      the platform sweeps older partitions.
    columns:
      - name: order_id
        description: Order identifier assigned upstream.
        tests: [not_null]
      - name: line_nbr
        description: Line number within the order, 1-based.
        tests: [not_null]
      - name: order_dt
        description: Business date of the order, and the partition key.
        tests: [not_null]
      - name: arrival_ts
        description: When the platform landed the partition this row came in on.
        tests: [not_null]
      - name: line_amount
        description: qty * unit_price, rounded to the cent.

  - name: stg_orders_snapshot
    description: >
      One row per order line in the vendor's frozen nightly extract.
    columns:
      - name: order_id
        tests: [not_null]
      - name: line_nbr
        tests: [not_null]
      - name: order_dt
        tests: [not_null]
      - name: snapshot_id
        description: Which nightly extract this row came from.
        tests: [not_null]
"""

DBT_WAREHOUSE_SCHEMA_YML = """\
version: 2

models:

  - name: fct_orders
    description: >
      Order lines. Loaded nightly from the delta stream, and the one relation
      in this warehouse that finance edits in place: a price restatement is
      applied here and nowhere upstream, which is what `is_manual_adjustment`
      and `adjustment_note` are for.
    columns:
      - name: order_id
        description: Order identifier assigned upstream.
        tests: [not_null]
      - name: line_nbr
        description: Line number within the order, 1-based.
        tests: [not_null]
      - name: order_dt
        description: Business date. Every downstream lookback is keyed on it.
        tests: [not_null]
      - name: customer_id
        tests: [not_null]
      - name: qty
        tests: [not_null]
      - name: unit_price
        description: >
          Price per unit as loaded, or as restated by finance when
          `is_manual_adjustment` is true.
        tests: [not_null]
      - name: is_manual_adjustment
        description: >
          True when this row's `unit_price` was set by hand here and does not
          match what the upstream extract carries for the same line.
        tests: [not_null]
      - name: adjustment_note
        description: Free text from finance. Null unless the row was restated.
      - name: promo_id
        description: Promotion applied to the line; null when there was none.
        tests:
          - relationships:
              arguments:
                to: source('warehouse', 'dim_promotions')
                field: promo_id
      - name: load_ts
        description: When this row was written here.
      - name: batch_id
        description: The load that wrote it.

  - name: fct_revenue
    description: >
      Revenue per order line. Incremental since 2025-12-01; the reasoning is in
      docs/adr/0002-incremental-marts.md and the nightly implementation is
      pipelines/build_marts.sql.
    columns:
      - name: order_id
        tests: [not_null]
      - name: line_nbr
        tests: [not_null]
      - name: order_dt
        description: Business date, carried through from fct_orders.
        tests: [not_null]
      - name: gross_amount
        description: qty * unit_price, rounded to the cent.
        tests: [not_null]
      - name: promo_id
        description: Promotion applied to the line; null when there was none.
      - name: discount_amount
        description: Promotional discount on the line.
        tests: [not_null]
      - name: tax_amount
        description: Sales tax on the line.
        tests: [not_null]
      - name: net_amount
        description: >
          The figure the period pack reports. `pipelines/build_marts.sql` is
          the implementation of record.
        tests: [not_null]
"""

DBT_RPT_PROMO_PERFORMANCE = """\
{{ config(materialized='table') }}

-- Promotion-level gross, discount and net by month, for Marketing's review.
--
-- A table, like everything under models/reporting. A view whose body reads the
-- `warehouse` schema cannot be created in this warehouse at all; the reasoning
-- is in macros/relations.sql and it is not a style choice.

with r as (

    select promo_id, order_dt, gross_amount, discount_amount, net_amount
    from {{ ref('fct_revenue') }}
    where promo_id is not null

)

select
    p.promo_id,
    p.promo_name,
    p.discount_pct,
    date_trunc('month', r.order_dt) as month,
    count(*) as order_lines,
    round(sum(r.gross_amount), 2) as gross_amount,
    round(sum(r.discount_amount), 2) as discount_amount,
    round(sum(r.net_amount), 2) as net_amount
from r
join {{ source('warehouse', 'dim_promotions') }} p on p.promo_id = r.promo_id
group by 1, 2, 3, 4
order by 4, 1
"""

DBT_RPT_SKU_DAILY = """\
{{ config(materialized='table') }}

-- Lines, units and gross per SKU per day.
--
-- Built for the November pack, kept because #commercial-reporting still links
-- to it. Reads fct_orders rather than fct_revenue on purpose: the question it
-- answers is a volume question and the discount split is noise in it.

select
    order_dt,
    sku,
    count(*) as order_lines,
    sum(qty) as units,
    round(sum(qty * unit_price), 2) as gross_amount
from {{ ref('fct_orders') }}
group by 1, 2
order by 1, 2
"""

DBT_RPT_CUSTOMER_MONTHLY = """\
{{ config(materialized='table') }}

-- One row per customer per month.
--
-- Written for a single question during the November certification and never
-- asked for again. Nothing reads it. It is still here because deleting a model
-- somebody might have bookmarked costs more than leaving it, which is the same
-- reasoning that left eleven legacy_ views in the catalogue until DATA-4290.

select
    customer_id,
    date_trunc('month', order_dt) as month,
    count(distinct order_id) as orders,
    count(*) as order_lines,
    round(sum(qty * unit_price), 2) as gross_amount
from {{ ref('fct_orders') }}
group by 1, 2
order by 2, 1
"""

DBT_REPORTING_SCHEMA_YML = """\
version: 2

models:

  - name: rpt_promo_performance
    description: >
      Gross, discount and net by promotion and month. Marketing's monthly
      review reads this; nothing automated does.
    columns:
      - name: promo_id
        tests: [not_null]
      - name: month
        tests: [not_null]

  - name: rpt_sku_daily
    description: >
      Lines, units and gross per SKU per day. Volume, not revenue - it reads
      fct_orders and knows nothing about discount or tax.
    columns:
      - name: order_dt
        tests: [not_null]
      - name: sku
        tests: [not_null]

  - name: rpt_customer_orders_monthly
    description: >
      One row per customer per month. Unowned: written for the November
      certification and not read since.
    columns:
      - name: customer_id
        tests: [not_null]
      - name: month
        tests: [not_null]
"""

# The three reporting marts exist TWICE - as dbt models the agent can read, and
# as relations in the shipped world - and there is exactly one text for each.
# `derender` renders the model body into plain SQL for the build rather than
# keeping a second copy of the same query in this module, because two copies of
# a query that are supposed to agree and are never compared is how a fixture
# starts lying.
REPORTING_MODELS = {
    "rpt_promo_performance": DBT_RPT_PROMO_PERFORMANCE,
    "rpt_sku_daily": DBT_RPT_SKU_DAILY,
    "rpt_customer_orders_monthly": DBT_RPT_CUSTOMER_MONTHLY,
}

JINJA_RELATIONS = {
    "{{ ref('fct_orders') }}": "warehouse.fct_orders",
    "{{ ref('fct_revenue') }}": "warehouse.fct_revenue",
    "{{ source('warehouse', 'dim_promotions') }}": "warehouse.dim_promotions",
}


def derender(body: str) -> str:
    """dbt model body -> plain SQL, by substitution and nothing else."""
    out = []
    for line in body.splitlines(keepends=True):
        if line.startswith("{{ config("):
            continue
        for token, relation in JINJA_RELATIONS.items():
            line = line.replace(token, relation)
        out.append(line)
    sql = "".join(out)
    if "{{" in sql or "{%" in sql:
        raise SystemExit("derender left jinja behind:\n" + sql)
    return sql


DBT_ANALYSIS_PROMO_MIX = """\
-- Saved out of an ad-hoc in #commercial-reporting: what share of order lines
-- carried a promotion, by month. `dbt compile` renders it; nothing runs it,
-- which is what analyses/ is for.

select
    date_trunc('month', order_dt) as month,
    count(*) filter (where promo_id is not null) as promo_lines,
    count(*) as order_lines,
    round(100.0 * count(*) filter (where promo_id is not null) / count(*), 1)
        as promo_share_pct
from {{ ref('fct_revenue') }}
group by 1
order by 1
"""

# --------------------------------------------------------------- archive ---
PIPE_ARCHIVE_PACK_EXPORT = """\
-- period_pack_export   ARCHIVED 2025-11-09, not scheduled
--
-- The pre-dbt export. It wrote the period pack straight out of the warehouse
-- to a CSV on the share and Commercial built the workbook on top of that file.
-- Superseded by the commercial_daily dashboard.
--
-- It does not run as written: warehouse.revenue_rollup went with the legacy_
-- views in DATA-4290. Kept only until the 2025 packs come off the share, at
-- which point this file and the share folder go together.

COPY (
  SELECT period_month, gross_revenue, net_revenue, order_count
  FROM warehouse.revenue_rollup
  WHERE period_month >= DATE '2025-01-01'
  ORDER BY period_month
) TO '/mnt/share/commercial/period_pack.csv' (HEADER, DELIMITER ',');
"""

# ------------------------------------------------------------------ docs ---
ADR_0001 = """\
# ADR 0001 — one warehouse file per environment, and none of them in the repo

Status: accepted (2025-10-06)

## Context

The warehouse is a single DuckDB file. It started inside the repo, next to the
dbt project, because that was the shortest path to a working `dbt build`.

Three things went wrong with that.

1. `git clean -xfd` deleted it. Twice.
2. Everyone's `~/.dbt/profiles.yml` pointed somewhere slightly different, so
   "it works for me" was true and useless.
3. The file is named `warehouse.duckdb` and one of the schemas inside it is
   also called `warehouse`. DuckDB names the catalog after the file stem, so
   opening the file directly makes `warehouse.fct_orders` ambiguous — catalog
   or schema? Every two-part reference then fails to bind.

## Decision

- The warehouse file lives outside the repo, at
  `../var/warehouse/warehouse.duckdb` relative to the repo root. One file per
  environment, never shared.
- The path comes from `DBT_WAREHOUSE_PATH`, with that relative path as the
  fallback, so a fresh checkout works with no configuration at all.
- `config/profiles.yml` is committed and `run.sh` exports `DBT_PROFILES_DIR`,
  so that is the profile anything run through `run.sh` uses.
- Anything that opens the file for reading **attaches it under an alias**
  rather than opening it directly:

  ```sql
  ATTACH '<path>' AS wh (READ_ONLY);
  USE wh;
  SELECT COUNT(*) FROM wh.warehouse.fct_orders;
  ```

  dbt is the exception: it opens the file directly because it is the writer,
  and `dbt_project/macros/relations.sql` strips the database qualifier off
  every `ref()` and `source()` so the view bodies dbt stores do not bake in a
  catalog name. Read the comment in that file before changing it.

## Consequences

- A developer checkout and the scheduler reach the same file by two different
  routes: the checkout through `config/profiles.yml`, the scheduler through a
  `profiles.yml` it renders itself. They agree today. Nothing enforces that
  they keep agreeing, which is the loose end in this decision.
- `dbt --target prod` from a developer checkout fails on a missing mount. That
  is intended: it should not quietly fall back to dev.
"""

ADR_0002 = f"""\
# ADR 0002 — fct_revenue is incremental

Status: accepted ({SWITCH_DATE})

## Context

`warehouse.fct_revenue` was `materialized='table'`. Every nightly build rewrote
six months of history in order to add one day to it. At current volume that is
a couple of hundred thousand rows a night and about four minutes of a window
that does not grow, over a history that does.

The same review changed what the table holds. `net_amount` had been a single
tax-inclusive figure. Commercial needed the promotional discount and the tax
as columns of their own so the pack could show them separately.

## Decision

- `fct_revenue` is `materialized='incremental'`, with `unique_key` on
  `(order_id, line_nbr)`.
- The incremental filter is on `order_dt`, not on a load timestamp: the mart's
  grain is the business date and the pack is cut on the business date.
- `net_amount` is split into `discount_amount`, `tax_amount` and `net_amount`.
  The tax rate is `vars.tax_rate` in `dbt_project/dbt_project.yml`.
- `kpi_daily` stays a full rebuild. It is one row per day and rebuilding it
  costs nothing.

## Consequences

- Nothing rebuilds history on its own any more. A full refresh is now a
  decision somebody makes, with a cost: the four minutes a night this change
  bought back, paid back in one go.
- `pipelines/build_marts.sql` is what the scheduler executes and it carries its
  own copy of the same arithmetic. The two are kept in step by hand. That is a
  loose end and it has not bitten us yet.
"""

ADR_0003 = f"""\
# ADR 0003 — the dashboard reads a table, not the mart

Status: accepted (2025-11-09)

## Context

`{DASHBOARD_ID}` served at 07:00 straight off `warehouse.kpi_daily`. The
November pack was cut while `build_marts` was still running, and for eleven
minutes the dashboard showed a half-built mart to the people cutting the pack.

## Decision

- `{BI_EXTRACT}` is a materialised copy of `warehouse.kpi_daily`.
- `pipelines/refresh_bi_extract.sql` rebuilds it at 02:00, after `build_marts`.
- `var/bi/dashboards/{DASHBOARD_ID}.yml` reads the extract. It does not read
  the mart, and it must not: the point is that the dashboard is a deliberate
  step behind the mart rather than a window onto a table being written.

## Consequences

- Rebuilding `warehouse.kpi_daily` does not move the dashboard. The extract has
  to be refreshed too, and until it is, the two disagree by design. This is
  already the second most common question in `#commercial-reporting`.
- Two relations now hold the same numbers, and nothing checks that they agree.
"""

DATA_DICTIONARY = f"""\
# Data dictionary

What is in the warehouse, at what grain, and who writes it. Column-level
descriptions live with the models, in `dbt_project/models/**/schema.yml`; this
page is the map.

## raw

| relation | grain | written by | retention |
|---|---|---|---|
| `raw.orders_landing` | one order line per arrival | platform ingestion | {LANDING_RETAIN_FROM} onward, 3 days rolling |
| `raw.orders_snapshot` | one row per order line | the vendor's nightly extract | full, replaced nightly |

`raw.orders_snapshot` is a frozen upstream extract. It carries source values
only — nothing that was edited by hand in this warehouse is in it, and nothing
we do here reaches it.

## warehouse

| relation | grain | written by |
|---|---|---|
| `warehouse.fct_orders` | one row per `(order_id, line_nbr)` | `pipelines/load_orders_delta.sql`, nightly; `pipelines/backfill_orders_window.sql`, by hand |
| `warehouse.fct_revenue` | one row per `(order_id, line_nbr)` | `pipelines/build_marts.sql`, nightly |
| `warehouse.kpi_daily` | one row per `order_dt` | `pipelines/build_marts.sql`, nightly |
| `warehouse.dim_promotions` | one row per `promo_id` | the platform promotions sync |
| `{BI_EXTRACT}` | one row per `dt` | `pipelines/refresh_bi_extract.sql`, nightly |
| `warehouse.pipeline_watermark` | one row per job | each job, as it finishes |

`fct_orders` is the only relation here that is edited outside the pipeline:
finance restates a unit price directly on the row and sets
`is_manual_adjustment`. Those edits exist here and nowhere else.

## ops

| relation | grain | written by |
|---|---|---|
| `ops.job_runs` | one row per job run | the scheduler |
| `ops.freshness_checks` | one row per dataset per night | the nightly checks |
| `ops.dq_checks` | one row per check per night | the nightly checks |

`config/checks.yml` is the written-down copy of what those checks are.

## Dates

`order_dt` is the business date and the partition key, and every lookback in
this repo is keyed on it. `arrival_ts` is when a partition landed, `load_ts` is
when a row was written into the warehouse, and the three are not
interchangeable: a row can arrive days after its `order_dt`, and the delta
stream's retention is measured in arrivals.
"""

RUNBOOK_NIGHTLY = f"""\
# Runbook: the nightly load

## What runs

The partition for `order_dt = D` lands in `raw.orders_landing` at 00:40 on
D+1. Everything below is the same morning.

| time | file | `ops.job_runs` calls it | writes |
|---|---|---|---|
| 01:20 | `pipelines/load_orders_delta.sql` | `load_orders_delta` | `warehouse.fct_orders` |
| 01:40 | `pipelines/build_marts.sql` | `load_revenue`, then `dbt_build` at 02:00 | `warehouse.fct_revenue`, `warehouse.kpi_daily` |
| 02:00 | `pipelines/refresh_bi_extract.sql` | nothing — it has no job row | `{BI_EXTRACT}` |

The third column is not a typo. `ops.job_runs` predates the split of
`build_marts` and `refresh_bi_extract` into separate files and still logs the
mart work under the two older job names. Reconciling them is on the list and
has been for a while; in the meantime, `ops.job_runs` is the record of whether
a night ran, and `pipelines/` is the record of what it ran.

`{DASHBOARD_ID}` serves from `{BI_EXTRACT}` at 07:00.

## Is it healthy

```sh
./run.sh pipeline-check     # does pipelines/ still match what has been running
./run.sh inventory          # relations and row counts
```

and in the warehouse:

```sql
SELECT job, MAX(started_at) AS last_start, COUNT(*) AS runs,
       SUM(CASE WHEN status <> 'success' THEN 1 ELSE 0 END) AS not_success
FROM wh.ops.job_runs GROUP BY job ORDER BY job;

SELECT * FROM wh.warehouse.pipeline_watermark ORDER BY job;
```

Investigate the first non-success run, not the last. The watermark is what the
next run reads, so a job that failed and left the watermark advanced is worse
than one that failed and left it alone.

## The delta stream is late

`raw.orders_landing` retains three days. If the partition for last night has
not landed by 01:20, `load_orders_delta` runs and moves nothing, and the
freshness check fires at 03:00 the following day.

Do not reach for the snapshot to fill a gap of a day or two — wait for the
partition. `raw.orders_snapshot` is the vendor's own extract and it carries
source values only, so anything finance has restated in `warehouse.fct_orders`
is not in it.

## Rebuilding a window

`pipelines/backfill_orders_window.sql` reads `raw.orders_snapshot` for a date
range. It is not scheduled and it is not part of the nightly chain. Usage is at
the top of the file.

## Do not full-refresh fct_orders

`dbt run --select fct_orders --full-refresh` rebuilds the model from
`raw.orders_landing`, and `raw.orders_landing` retains three days. It completes,
it reports success, and it leaves three days of orders where six months were.

The delta stream is not a source of history and never has been. Anything older
than the retention window exists in `warehouse.fct_orders` itself and in
`raw.orders_snapshot`, and nowhere else.

## Who to call

`#data-eng`. Commercial cut the period pack on the 5th working day; if the
marts are in doubt on that day say so in `#commercial-reporting` rather than
letting the pack go out and be restated later — see the certification policy on
the wiki.
"""

# ---------------------------------------------------------------- scripts ---
SCRIPT_WAREHOUSE = '''\
"""Open the warehouse the way everything except dbt has to open it.

DuckDB names the catalog after the file stem. The file is `warehouse.duckdb`
and one of the schemas inside it is `warehouse`, so opening it directly makes
every two-part reference ambiguous:

    Binder Error: Ambiguous reference to catalog or schema "warehouse"

ATTACHing it under an alias costs one line and removes the whole class of
failure. See docs/adr/0001-warehouse-file-and-attach-alias.md.

    python scripts/warehouse.py                        # relations + row counts
    python scripts/warehouse.py --sql "SELECT 1"
    python scripts/warehouse.py --db /path/to/other.duckdb
"""
import argparse
import os
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO.parent / "var" / "warehouse" / "warehouse.duckdb"


def warehouse_path() -> Path:
    return Path(os.environ.get("DBT_WAREHOUSE_PATH") or DEFAULT_DB)


def connect(db=None, *, read_only: bool = True, alias: str = "wh"):
    """ATTACH under an alias, never a direct open. This is the three lines."""
    db = Path(db or warehouse_path())
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{db.as_posix()}' AS {alias}"
                + (" (READ_ONLY)" if read_only else ""))
    con.execute(f"USE {alias}")
    return con


def inventory(con, alias: str = "wh") -> list:
    rows = con.execute(
        "SELECT schema_name, table_name FROM duckdb_tables() "
        "WHERE database_name = ? ORDER BY 1, 2", [alias]).fetchall()
    out = []
    for schema, table in rows:
        n = con.execute(
            f'SELECT COUNT(*) FROM {alias}."{schema}"."{table}"').fetchone()[0]
        out.append((f"{schema}.{table}", n))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="warehouse")
    ap.add_argument("--db", default=None, help="default: $DBT_WAREHOUSE_PATH")
    ap.add_argument("--sql", default=None, help="run one statement and print it")
    ap.add_argument("--write", action="store_true",
                    help="attach read-write (default is read-only)")
    a = ap.parse_args(argv)

    db = Path(a.db or warehouse_path())
    if not db.is_file():
        print(f"no warehouse at {db}")
        return 2
    con = connect(db, read_only=not a.write)
    try:
        if a.sql:
            cur = con.execute(a.sql)
            names = [d[0] for d in (cur.description or [])]
            if names:
                print("\\t".join(names))
            for row in cur.fetchall():
                print("\\t".join("" if v is None else str(v) for v in row))
            return 0
        print(f"{db}")
        for name, n in inventory(con):
            print(f"  {name:<34} {n:>9,}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

SCRIPT_CHECK_PIPELINE = '''\
"""Compare pipelines/ with the runs the scheduler actually recorded.

Two questions are worth asking of a pipeline you did not write:

    what is supposed to run, and in what order?
    does that still match what the platform has been running?

`pipelines/README.md` answers the first - it is the table of record. The second
is answered against `var/sched/state.json`, the run history the scheduler
writes, so a file that was renamed here and not in the deployment shows up in
this output instead of at 01:20 on a Sunday.

Exit status: 0 clean, 1 drift, 2 could not read the inputs.

    python scripts/check_pipeline.py
    python scripts/check_pipeline.py --state ../var/sched/state.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Jobs that exist on purpose without a nightly schedule: each is run by hand,
# and each is named as such in pipelines/README.md's schedule column.
MANUAL = {"backfill_orders_window"}


def defined(pipelines: Path) -> dict:
    """{job -> filename}. One job per pipelines/*.sql, named by the file stem."""
    return {p.stem: p.name for p in sorted(pipelines.glob("*.sql"))}


def documented(readme: Path) -> set:
    if not readme.is_file():
        return set()
    return set(re.findall(r"`([a-z0-9_]+)\\.sql`",
                          readme.read_text(encoding="utf-8")))


def from_state(state_path: Path):
    """(scheduled jobs, jobs with at least one recorded run), or (None, None)."""
    if not Path(state_path).is_file():
        return None, None
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    jobs = set(state.get("jobs") or {})
    ran = {r["job"] for r in (state.get("runs") or []) if "job" in r}
    return jobs, ran


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="check_pipeline")
    ap.add_argument("--pipelines", type=Path, default=REPO / "pipelines")
    ap.add_argument("--state", type=Path,
                    default=REPO.parent / "var" / "sched" / "state.json")
    a = ap.parse_args(argv)

    if not a.pipelines.is_dir():
        print(f"no pipeline directory at {a.pipelines}", file=sys.stderr)
        return 2
    files = defined(a.pipelines)
    if not files:
        print(f"no .sql files under {a.pipelines}", file=sys.stderr)
        return 2
    docs = documented(a.pipelines / "README.md")
    jobs, ran = from_state(a.state)

    print(f"{len(files)} pipeline file(s) in {a.pipelines}")
    for job, name in files.items():
        if jobs is not None and job in jobs:
            sched = "scheduled"
        elif job in MANUAL:
            sched = "manual"
        else:
            sched = "UNSCHEDULED"
        doc = "documented" if job in docs else "UNDOCUMENTED"
        runs = "" if ran is None else ("has run" if job in ran else "never run")
        print(f"  {name:<30} {sched:<12} {doc:<13} {runs}")

    if jobs is None:
        print(f"\\nno run history at {a.state}; skipped the drift check")
        return 0

    drift = 0
    for job in sorted(jobs - set(files)):
        print(f"  DRIFT: the scheduler runs {job!r} and no file here defines it")
        drift += 1
    for job in sorted(set(files) - jobs - MANUAL):
        print(f"  DRIFT: {job!r} is defined here, is not scheduled, and is not "
              f"listed as a manual job")
        drift += 1
    for job in sorted(set(files) - docs):
        print(f"  DRIFT: {job!r} is not in pipelines/README.md")
        drift += 1
    if drift:
        return 1
    print("  clean: every scheduled job has a file, every file is documented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

# The agent-visible repo, in one place. 35 files, which is the register the
# four shipped world themes use (23-27 each) rather than the eight this task
# shipped with: a world that is eight files is not a computer anybody works on,
# and an agent that cannot tell a thin fixture from a real repo cannot be
# measured on whether it read the repo.
WORKSPACE_FILES = {
    # -- the repo shell ------------------------------------------------------
    "README.md": README,
    "CHANGELOG.md": CHANGELOG,
    ".gitignore": GITIGNORE,
    ".envrc": ENVRC,
    "requirements.txt": REQUIREMENTS,
    "run.sh": RUN_SH,
    ".github/workflows/ci.yml": CI_YML,

    # -- configuration -------------------------------------------------------
    "config/profiles.yml": CONFIG_PROFILES,
    "config/warehouse.yml": CONFIG_WAREHOUSE,
    "config/checks.yml": CONFIG_CHECKS,

    # -- the dbt project: this is what dbt_compile / dbt_run / dbt_test need --
    "dbt_project/dbt_project.yml": DBT_PROJECT_YML,
    "dbt_project/macros/relations.sql": DBT_MACRO_RELATIONS,
    "dbt_project/macros/schema.sql": DBT_MACRO_SCHEMA,
    "dbt_project/models/sources.yml": DBT_SOURCES_YML,
    "dbt_project/models/staging/stg_orders_landing.sql": DBT_STG_ORDERS_LANDING,
    "dbt_project/models/staging/stg_orders_snapshot.sql": DBT_STG_ORDERS_SNAPSHOT,
    "dbt_project/models/staging/schema.yml": DBT_STAGING_SCHEMA_YML,
    "dbt_project/models/warehouse/fct_orders.sql": DBT_FCT_ORDERS,
    "dbt_project/models/warehouse/fct_revenue.sql": DBT_FCT_REVENUE,
    "dbt_project/models/warehouse/schema.yml": DBT_WAREHOUSE_SCHEMA_YML,
    "dbt_project/models/reporting/rpt_promo_performance.sql":
        DBT_RPT_PROMO_PERFORMANCE,
    "dbt_project/models/reporting/rpt_sku_daily.sql": DBT_RPT_SKU_DAILY,
    "dbt_project/models/reporting/rpt_customer_orders_monthly.sql":
        DBT_RPT_CUSTOMER_MONTHLY,
    "dbt_project/models/reporting/schema.yml": DBT_REPORTING_SCHEMA_YML,
    "dbt_project/analyses/promo_mix_by_month.sql": DBT_ANALYSIS_PROMO_MIX,

    # -- the nightly SQL the scheduler runs, unchanged from the first build ---
    "pipelines/backfill_orders_window.sql": PIPE_BACKFILL,
    "pipelines/load_orders_delta.sql": PIPE_LOAD_DELTA,
    "pipelines/build_marts.sql": PIPE_BUILD_MARTS,
    "pipelines/refresh_bi_extract.sql": PIPE_REFRESH_BI,
    "pipelines/README.md": PIPE_README,
    "pipelines/archive/period_pack_export.sql": PIPE_ARCHIVE_PACK_EXPORT,

    # -- docs ----------------------------------------------------------------
    "docs/data_dictionary.md": DATA_DICTIONARY,
    "docs/runbook_nightly_load.md": RUNBOOK_NIGHTLY,
    "docs/adr/0001-warehouse-file-and-attach-alias.md": ADR_0001,
    "docs/adr/0002-incremental-marts.md": ADR_0002,
    "docs/adr/0003-bi-extract-is-a-table.md": ADR_0003,

    # -- scripts -------------------------------------------------------------
    "scripts/warehouse.py": SCRIPT_WAREHOUSE,
    "scripts/check_pipeline.py": SCRIPT_CHECK_PIPELINE,
}

BI_FILES = {f"dashboards/{DASHBOARD_ID}.yml": DASHBOARD_YML}


def write_workspace(out: Path) -> dict:
    """The agent-visible repo. A shop, not a paper trail.

    Neither defect is *stated* anywhere in here, and that is the only editorial
    rule this directory has. What IS here is what a team of five actually
    carries: a dbt project that compiles, runs and tests against the deployed
    warehouse; a README that predates the incident; pinned requirements; CI; a
    task runner; committed dbt targets; three decision records; a data
    dictionary; a runbook; two scripts; and one archived pipeline nobody has
    got round to deleting.

    The first rollouts measured 8 files and 11,439 bytes of starting context,
    against 23-27 files in each of the four shipped world themes, and reported
    `dbt` as a dead surface: there were two model files and no dbt_project.yml,
    so `dbt_compile` and `dbt_test` could not run at all and were called three
    times each before the agent gave up on them. Both of those are fixed here.

    What is deliberately ABSENT, and must stay absent: any uniqueness assertion
    on (order_id, line_nbr), in schema.yml, in a singular test, in config/
    checks.yml or anywhere else. CONTRACT["green"]["uniqueness_checks_present"]
    is 0 and DATA-4355 - the ticket that asks for exactly that test, open since
    November - is load-bearing narrative. A `unique` test here would monitor
    defect 1 and A027 would stop holding.
    """
    hashes = {}
    for sub, files in (("workspace", WORKSPACE_FILES), ("bi", BI_FILES)):
        d = out / sub
        if d.exists():
            shutil.rmtree(d)
        for rel, text in files.items():
            _write_text(d / rel, text)
            hashes[f"{sub}/{rel}"] = hashlib.sha256(
                (d / rel).read_bytes()).hexdigest()
    return hashes


# ------------------------------------------------- staging: world + grading ---
# The episode builder (tests/build_world.py) is not mine and does not exist yet,
# so the two things the probes need from a BUILT world are provided here as
# functions it can call. Both are pure staging: they add no world content that
# make_fixture_world did not already generate.

# ---------------------------------------------------------------------------
# LAYER 4 - the publication ledger (world/README.md#publication-ledger, frozen
# 2026-09-16). A job may declare `publishes: [{target, kind, consumer}]`, and
# `WorldScheduler._simulate_job` appends one receipt per entry at the three
# sites where the job's command actually ran. GT004's consumer-facing jobs are
# the last two of the nightly chain: `build_marts` writes the mart Commercial's
# dashboard aggregates from, and `refresh_bi_extract` writes the MATERIALISED
# extract the dashboard actually renders.
#
# OFF BY DEFAULT, and that is not timidity. `publishes:` lives inside
# var/world/live.json and the schedule the clock needs to execute lives inside
# var/sched/state.json, and BOTH are sections of `world.fingerprint.semantic()`
# - the contract says so in as many words: "adding one would move
# var/world/live.json, which IS fingerprinted, and every recorded world hash
# with it". A world staged with `ledger=False` is byte-identical to the world
# every recorded GT004 rollout ran in, so the default arm's semantic_hash does
# not move and no baseline's score can. `ledger=True` is what tests/
# ledger_check.py stages, because a ledger is only evidence if something
# advanced the clock over it, and nothing in GT004's grading path does:
# `trigger()` writes no receipts by contract, and the G3 probe moves `vnow` by
# editing the file rather than by running `world.scheduler.advance`.
PUBLISHES = {
    "build_marts": [
        {"target": "warehouse.kpi_daily", "kind": "table",
         "consumer": "Commercial"},
    ],
    "refresh_bi_extract": [
        {"target": BI_EXTRACT, "kind": "table", "consumer": "Commercial"},
    ],
}

# What `world.scheduler.advance` needs beyond an `exec` block, and nothing more.
# `_plan_jobs` reads live["vtime_start"] and `job_starts` reads each job's
# `time` and `day_offset`; `_simulate_job` reads `duration_s` and `detail`.
# Without them an advance raises KeyError before a single job runs, which is why
# this is staged rather than asserted.
#
# `day_offset: 1` is the shipped schedule read back out of scheduler_state()'s
# own run history: `load_orders_delta` ran at 2026-03-08T01:20 for dt=2026-03-07.
# The nightly processes YESTERDAY, so a night's occurrence sits one day after
# the day it is responsible for, and `advance` lands on exactly the slots the
# history already used.
ADVANCE_VTIME_START = VNOW[:10] + "T00:00:00Z"   # midnight of the episode day
ADVANCE_PLANE = {
    "load_orders_delta": {"time": "01:20", "day_offset": 1, "duration_s": 90},
    "build_marts": {"time": "01:40", "day_offset": 1, "duration_s": 90},
    "refresh_bi_extract": {"time": "02:00", "day_offset": 1, "duration_s": 40},
}


def scheduler_state(vnow: str = VNOW, ledger: bool = False) -> dict:
    """A minimal var/sched/state.json.

    G3 advances `vnow` in this file: that is what makes an arrival LATE rather
    than merely additional. If the episode builder prefers Track A's
    `world.scheduler.initial_state`, use it — but the file MUST carry `vnow`, or
    late_arrival._advance_clock silently declines to advance and G3 measures an
    ordinary insert.

    Every job carries an `exec` block. Without one `world.executor.execute`
    returns `error_kind="unsupported"`, so an episode running the executing
    `WorldScheduler` would refuse all three jobs and the agent could not re-run
    its own pipeline through the scheduler at all - which is the single thing a
    data engineer does most. The blocks name the SAME pipeline files the
    probes execute (`CONTRACT["probes"]["pipeline"]["steps"]`), so triggering a
    job and running the file by hand do the same work; if they diverged, the
    grader and the agent would be looking at two different pipelines.

    `ledger=True` additionally declares each consumer-facing job's `publishes:`
    block here as well as in live_state(), so a world with no live plane still
    carries it (world/world/ledger.py `publishes` falls through live -> exec ->
    state). It moves the `scheduler` fingerprint section; see PUBLISHES.
    """
    state = {
        "vnow": vnow,
        "jobs": {
            "load_orders_delta": {
                "command": "load_orders_delta", "duration_s": 90, "time": "01:20",
                "exec": {"kind": "sql", "script": "pipelines/load_orders_delta.sql",
                         "detail": "load_orders_delta"}},
            "build_marts": {
                "command": "dbt_build", "duration_s": 90, "time": "01:40",
                "exec": {"kind": "sql", "script": "pipelines/build_marts.sql",
                         "detail": "build_marts"}},
            "refresh_bi_extract": {
                "command": "refresh_bi_extract", "duration_s": 40, "time": "02:00",
                "exec": {"kind": "sql", "script": "pipelines/refresh_bi_extract.sql",
                         "detail": "refresh_bi_extract"}},
        },
        "runs": [
            {"job": "load_orders_delta", "vts_start": "2026-03-08T01:20:00Z",
             "vts_end": "2026-03-08T01:21:30Z", "status": "success",
             "detail": f"dt={HIST_END}"},
            {"job": "build_marts", "vts_start": "2026-03-08T01:40:00Z",
             "vts_end": "2026-03-08T01:41:30Z", "status": "success",
             "detail": f"dt={HIST_END}"},
            {"job": "refresh_bi_extract", "vts_start": "2026-03-08T02:00:00Z",
             "vts_end": "2026-03-08T02:00:40Z", "status": "success",
             "detail": DASHBOARD_ID},
            {"job": "load_orders_delta", "vts_start": RETRY_TS.replace(" ", "T") + "Z",
             "vts_end": "2026-03-01T02:21:38Z", "status": "success",
             "detail": f"backfill_orders_window --from {WIN_START} --to {WIN_END}"},
        ],
    }
    if ledger:
        for job, spec in PUBLISHES.items():
            state["jobs"][job]["publishes"] = [dict(e) for e in spec]
    return state


def live_state(ledger: bool = False) -> dict:
    """var/world/live.json - where `exec` specs actually live.

    `scheduler._execute` reads a job's work from `live["jobs"][name]["exec"]`,
    NOT from `state["jobs"]`, which carries only the schedule. Without this file
    every trigger came back `declares no executable work`, so an agent could not
    re-run its own pipeline through the scheduler - the single most common thing
    a data engineer does.

    No `theme`, no `vtime_start`, no `days_landed`: this world's data is built by
    the fixture generator, not landed day by day, so there is no stream to
    generate. The scripts are the SAME files the probes execute, so triggering a
    job and running the file by hand cannot diverge.

    `ledger=True` adds the two things `world.scheduler.advance` needs to execute
    a night for real - `vtime_start`, and each job's `time`/`duration_s`/`detail`
    - plus the `publishes:` blocks that turn a completed occurrence into a
    receipt. It is off by default because this file IS a fingerprint section;
    see PUBLISHES for the whole argument.
    """
    live = {
        "jobs": {
            "load_orders_delta": {
                "exec": {"kind": "sql", "script": "pipelines/load_orders_delta.sql",
                         "detail": "load_orders_delta"}},
            "build_marts": {
                "exec": {"kind": "sql", "script": "pipelines/build_marts.sql",
                         "detail": "build_marts"}},
            "refresh_bi_extract": {
                "exec": {"kind": "sql", "script": "pipelines/refresh_bi_extract.sql",
                         "detail": "refresh_bi_extract"}},
        },
    }
    if ledger:
        live["vtime_start"] = ADVANCE_VTIME_START
        for job, spec in live["jobs"].items():
            spec.update(ADVANCE_PLANE[job])
            spec["detail"] = job + " dt={day}"
            if job in PUBLISHES:
                spec["publishes"] = [dict(e) for e in PUBLISHES[job]]
    return live


def stage_world_root(build: Path, root: Path, ledger: bool = False) -> dict:
    """Lay the built fixture out as a §9.2.1 world root.

    The warehouse lands at var/warehouse/warehouse.duckdb — the CONTRACT
    filename, where the duckdb catalog and the `warehouse` schema collide — so
    the agent's own two-part references run under exactly the condition the
    deployed world has. Staging it anywhere else is how GT001 once shipped
    TASK VALID with an oracle that aborted on step 1 of 4.

    `ledger=False` is the shipped world, byte for byte: every recorded GT004
    rollout ran in it and its `world.fingerprint.semantic_hash` must not move.
    `ledger=True` stages the publication plane instead - see PUBLISHES - and is
    used by tests/ledger_check.py alone.
    """
    build, root = Path(build), Path(root)
    (root / "var" / "warehouse").mkdir(parents=True, exist_ok=True)
    (root / "var" / "sched").mkdir(parents=True, exist_ok=True)
    (root / "build").mkdir(parents=True, exist_ok=True)
    wh = root / "var" / "warehouse" / "warehouse.duckdb"
    shutil.copy(build / "world.duckdb", wh)
    Path(str(wh) + ".wal").unlink(missing_ok=True)
    for sub in ("workspace", "bi"):
        dst = root / ("workspace" if sub == "workspace" else "var/bi")
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(build / sub, dst)
    _write_text(root / "var" / "sched" / "state.json",
                json.dumps(scheduler_state(ledger=ledger), indent=2,
                           sort_keys=True) + "\n")
    # var/world/live.json is where scheduler._execute reads a job's `exec`
    # spec. state.json carries the SCHEDULE; live.json carries the WORK.
    (root / "var" / "world").mkdir(parents=True, exist_ok=True)
    _write_text(root / "var" / "world" / "live.json",
                json.dumps(live_state(ledger=ledger), indent=2,
                           sort_keys=True) + "\n")
    return {"world_root": str(root), "warehouse": str(wh),
            "workspace": str(root / "workspace"),
            "dashboards": str(root / "var" / "bi" / "dashboards"),
            "scheduler": str(root / "var" / "sched" / "state.json"),
            "virtual_time": VNOW}


def write_grading_zone(build: Path, world_root: Path, grading: Path,
                       snapshot_fn=None) -> dict:
    """Everything the probes read, in a directory the episode cannot reach.

    F121. `grading` MUST NOT be inside `world_root`; this refuses otherwise
    rather than trusting the caller, because an agent that can read the reference
    copies instead of diagnosing and the task is dead.

    Produces:
      expected.duckdb            G1's reference (schemas warehouse, warehouse_t1)
      pre_repair_world/          G6 route 1: the PRISTINE pre-episode world root.
                                 A copy, not a reconstruction — re-injuring from
                                 contract SQL proves a test catches the defect as
                                 the CONTRACT describes it, which is not the same
                                 claim as catching the defect as it was planted.
      world_sections.json        G5's baseline, from probes.collateral.snapshot()
      late_arrivals_manifest.json  the new/restated key lists
      late_arrivals/             the payload, for the oracle and for humans
    """
    build, world_root, grading = Path(build), Path(world_root), Path(grading)
    if grading.resolve() == world_root.resolve() or \
            grading.resolve().is_relative_to(world_root.resolve()):
        raise SystemExit(
            "F121: the grading zone must not live inside the world root "
            f"({grading} is inside {world_root})")
    grading.mkdir(parents=True, exist_ok=True)
    shutil.copy(build / "expected.duckdb", grading / "expected.duckdb")
    shutil.copy(build / "late_arrivals_manifest.json",
                grading / "late_arrivals_manifest.json")
    for name in ("late_arrivals", "pre_repair_world"):
        dst = grading / name
        if dst.exists():
            shutil.rmtree(dst)
    shutil.copytree(build / "late_arrivals", grading / "late_arrivals")
    shutil.copytree(world_root, grading / "pre_repair_world")

    if snapshot_fn is None:
        snapshot_fn = _import_collateral_snapshot()
    sections = snapshot_fn(world_root)
    _write_text(grading / "world_sections.json",
                json.dumps(sections, indent=2, sort_keys=True) + "\n")
    return {
        "grading": str(grading),
        "expected": str(grading / "expected.duckdb"),
        "pre_repair_world": str(grading / "pre_repair_world"),
        "collateral_baseline": str(grading / "world_sections.json"),
        "late_arrivals": str(grading / "late_arrivals"),
        "sections": sorted(k for k, v in sections["sections"].items()
                           if v is not None),
    }


def _import_collateral_snapshot():
    """tests/probes is PROBES' package, not mine — import it, never vendor it.

    Passing `snapshot_fn` explicitly is the supported route for a caller that
    already has it imported; this is the convenience path.
    """
    import sys
    tests = Path(__file__).resolve().parent.parent / "tests"
    if str(tests) not in sys.path:
        sys.path.insert(0, str(tests))
    from probes.collateral import snapshot        # noqa: E402
    return snapshot


# --------------------------------------------------------------- measuring ---
def measure(out: Path) -> dict:
    """Read every number the contract publishes. Measured, never asserted."""
    con = _readonly(out / "world.duckdb", "wh")
    con.execute(f"ATTACH '{(out / 'expected.duckdb').as_posix()}' AS ref (READ_ONLY)")

    def q(sql: str):
        return con.execute(sql).fetchone()

    world = {
        "fct_orders_rows": q("SELECT COUNT(*) FROM wh.warehouse.fct_orders")[0],
        "fct_revenue_rows": q("SELECT COUNT(*) FROM wh.warehouse.fct_revenue")[0],
        "kpi_daily_rows": q("SELECT COUNT(*) FROM wh.warehouse.kpi_daily")[0],
        "snapshot_rows": q("SELECT COUNT(*) FROM wh.raw.orders_snapshot")[0],
        "landing_rows": q("SELECT COUNT(*) FROM wh.raw.orders_landing")[0],
        "promo_rows": q("SELECT COUNT(*) FROM wh.warehouse.dim_promotions")[0],
        "dup_keys": q("SELECT COUNT(*) FROM (SELECT order_id, line_nbr "
                      "FROM wh.warehouse.fct_orders GROUP BY 1,2 HAVING COUNT(*)>1)")[0],
        "retry_batch_rows": q("SELECT COUNT(*) FROM wh.warehouse.fct_orders "
                              f"WHERE batch_id = '{RETRY_BATCH}'")[0],
        "manual_adj_total": q("SELECT COUNT(*) FROM wh.warehouse.fct_orders "
                              "WHERE is_manual_adjustment")[0],
        "manual_adj_in_window": q(
            "SELECT COUNT(*) FROM wh.warehouse.fct_orders WHERE is_manual_adjustment "
            f"AND order_dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}'")[0],
        "stale_layout_rows": q("SELECT COUNT(*) FROM wh.warehouse.fct_revenue "
                               f"WHERE NOT ({LAYOUT_INVARIANT})")[0],
        "preswitch_revenue_rows": q("SELECT COUNT(*) FROM wh.warehouse.fct_revenue "
                                    f"WHERE order_dt < DATE '{SWITCH_DATE}'")[0],
        "revenue_dup_keys": q("SELECT COUNT(*) FROM (SELECT order_id, line_nbr "
                              "FROM wh.warehouse.fct_revenue GROUP BY 1,2 "
                              "HAVING COUNT(*)>1)")[0],
        "job_runs": q("SELECT COUNT(*) FROM wh.ops.job_runs")[0],
        "job_runs_not_success": q("SELECT COUNT(*) FROM wh.ops.job_runs "
                                  "WHERE status <> 'success'")[0],
        "freshness_not_pass": q("SELECT COUNT(*) FROM wh.ops.freshness_checks "
                                "WHERE status <> 'PASS'")[0],
        "dq_not_pass": q("SELECT COUNT(*) FROM wh.ops.dq_checks "
                         "WHERE status <> 'PASS'")[0],
        "max_order_dt": str(q("SELECT MAX(order_dt) FROM wh.warehouse.fct_orders")[0]),
    }
    exp = {
        "fct_orders_rows": q("SELECT COUNT(*) FROM ref.warehouse.fct_orders")[0],
        "fct_revenue_rows": q("SELECT COUNT(*) FROM ref.warehouse.fct_revenue")[0],
        "kpi_daily_rows": q("SELECT COUNT(*) FROM ref.warehouse.kpi_daily")[0],
        "dup_keys": q("SELECT COUNT(*) FROM (SELECT order_id, line_nbr "
                      "FROM ref.warehouse.fct_orders GROUP BY 1,2 HAVING COUNT(*)>1)")[0],
        "manual_adj_total": q("SELECT COUNT(*) FROM ref.warehouse.fct_orders "
                              "WHERE is_manual_adjustment")[0],
        "stale_layout_rows": q("SELECT COUNT(*) FROM ref.warehouse.fct_revenue "
                               f"WHERE NOT ({LAYOUT_INVARIANT})")[0],
    }
    exp_t1 = {
        "fct_orders_rows": q("SELECT COUNT(*) FROM ref.warehouse_t1.fct_orders")[0],
        "fct_revenue_rows": q("SELECT COUNT(*) FROM ref.warehouse_t1.fct_revenue")[0],
        "kpi_daily_rows": q("SELECT COUNT(*) FROM ref.warehouse_t1.kpi_daily")[0],
        "dup_keys": q("SELECT COUNT(*) FROM (SELECT order_id, line_nbr "
                      "FROM ref.warehouse_t1.fct_orders GROUP BY 1,2 HAVING COUNT(*)>1)")[0],
        "manual_adj_total": q("SELECT COUNT(*) FROM ref.warehouse_t1.fct_orders "
                              "WHERE is_manual_adjustment")[0],
    }
    win, base = q(f"""
        SELECT ROUND(AVG(CASE WHEN dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}'
                              THEN order_lines END), 2),
               ROUND(AVG(CASE WHEN dt BETWEEN DATE '{BASE_START}' AND DATE '{BASE_END}'
                              THEN order_lines END), 2)
        FROM wh.warehouse.kpi_daily""")
    # Every adjusted row must actually DIFFER from the snapshot, or "a full
    # refresh destroys the adjustments" is a claim with no teeth: reloading a
    # row with the value it already had destroys nothing. 114 here, of which the
    # 76 inside the window are the ones a window-scoped refresh wipes.
    differ = q("""
        SELECT COUNT(*) FROM wh.warehouse.fct_orders o
        JOIN wh.raw.orders_snapshot s USING (order_id, line_nbr)
        WHERE o.is_manual_adjustment AND o.unit_price <> s.unit_price""")[0]
    window_rows = q(
        "SELECT COUNT(*) FROM wh.warehouse.fct_orders "
        f"WHERE order_dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}'")[0]
    max_mult = q("SELECT MAX(c) FROM (SELECT COUNT(*) c FROM wh.warehouse.fct_orders "
                 "GROUP BY order_id, line_nbr)")[0]
    # G4's expectations: what the dashboard shows now (stale) and what it must
    # show once the extract is refreshed from a repaired mart (corrected).
    PRE_SWITCH_DAY = "2025-10-15"

    def panel(src_kpi: str) -> dict:
        w = q(f"SELECT SUM(order_lines), ROUND(SUM(gross_revenue),2), "
              f"ROUND(SUM(net_revenue),2) FROM {src_kpi} "
              f"WHERE dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}'")
        d = q(f"SELECT net_revenue, order_lines FROM {src_kpi} "
              f"WHERE dt = DATE '{PRE_SWITCH_DAY}'")
        return {"window_order_lines": int(w[0]),
                "window_gross_revenue": float(w[1]),
                "window_net_revenue": float(w[2]),
                "preswitch_net_revenue": float(d[0]),
                "preswitch_order_lines": int(d[1])}

    exposure = {
        "pre_switch_day": PRE_SWITCH_DAY,
        "stale": panel(f"wh.{BI_EXTRACT}"),
        "corrected": panel("ref.warehouse.kpi_daily"),
        "corrected_t1": panel("ref.warehouse_t1.kpi_daily"),
        "extract_equals_kpi_now": q(
            f"SELECT COUNT(*) FROM (SELECT * FROM wh.{BI_EXTRACT} "
            f"EXCEPT ALL SELECT dt, order_lines, orders, gross_revenue, "
            f"net_revenue, aov FROM wh.warehouse.kpi_daily)")[0] == 0,
    }
    con.close()
    world["window_rows"] = window_rows
    world["max_key_multiplicity"] = max_mult
    world["adjusted_rows_differing_from_snapshot"] = differ
    return {
        "world": world, "expected": exp, "expected_t1": exp_t1,
        "window_lines_per_day": float(win), "baseline_lines_per_day": float(base),
        "symptom_ratio": round(float(win) / float(base), 3),
        "exposure": exposure,
    }


# -------------------------------------------------------------------- main ---
def generate(out: Path, seed: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    build_world(out / "world.duckdb", seed)
    manifest = build_expected_and_payload(out, seed)
    ws = write_workspace(out)
    stats = measure(out)
    stats["late_arrivals"] = {
        "arrival_ts": manifest["arrival_ts"],
        "batch_id": manifest["batch_id"],
        "partitions": len(manifest["partitions"]),
        "rows": manifest["total_rows"],
        "new_keys": manifest["total_new_keys"],
        "restated_keys": manifest["total_restated_keys"],
    }
    stats["workspace_sha256"] = ws
    return stats


def tree_hashes(root: Path, text_only: bool = True) -> dict:
    """sha256 of every generated file. text_only skips the .duckdb containers,
    which are not byte-reproducible — see the DETERMINISM note in the module
    docstring for the measurement."""
    h = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not (text_only and p.suffix == ".duckdb"):
            h[p.relative_to(root).as_posix()] = hashlib.sha256(
                p.read_bytes()).hexdigest()
    return h


def content_digest(db: Path) -> dict:
    """Order-insensitive multiset hash of every table in a database.

    md5 per row, then md5 over the row hashes sorted — so it is invariant to
    physical row order (a repair is free to rewrite a table in any order) but
    NOT to multiplicity, which matters: a doubled window must not digest the
    same as a repaired one. Aliased ATTACH, READ_ONLY (F622).
    """
    con = _readonly(db, "d")
    try:
        tables = [r[0] for r in con.execute(
            "SELECT schema_name || '.' || table_name FROM duckdb_tables() "
            "WHERE database_name = 'd' ORDER BY 1").fetchall()]
        return {t: con.execute(
            f"SELECT md5(COALESCE(string_agg(h, '' ORDER BY h), '')) FROM "
            f"(SELECT md5(CAST(x AS VARCHAR)) AS h FROM d.{t} x)").fetchone()[0]
                for t in tables}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./build")
    ap.add_argument("--seed", type=int, default=4004)
    ap.add_argument("--verify-twice", action="store_true",
                    help="build a second copy into a temp dir and diff every byte")
    ap.add_argument("--json", action="store_true", help="dump measured stats")
    a = ap.parse_args()

    out = Path(a.out).resolve()
    stats = generate(out, a.seed)
    if a.json:
        print(json.dumps(stats, indent=2, sort_keys=True))
        return 0
    w, e, t1 = stats["world"], stats["expected"], stats["expected_t1"]

    print(f"  world.duckdb    fct_orders {w['fct_orders_rows']:>8,}  "
          f"fct_revenue {w['fct_revenue_rows']:>8,}  kpi_daily {w['kpi_daily_rows']:>4,}")
    print(f"  expected @t0    fct_orders {e['fct_orders_rows']:>8,}  "
          f"fct_revenue {e['fct_revenue_rows']:>8,}  kpi_daily {e['kpi_daily_rows']:>4,}")
    print(f"  expected @t1    fct_orders {t1['fct_orders_rows']:>8,}  "
          f"fct_revenue {t1['fct_revenue_rows']:>8,}  kpi_daily {t1['kpi_daily_rows']:>4,}")
    print()
    print(f"  defect 1  window {WIN_START}..{WIN_END}  (batch {RETRY_BATCH})")
    print(f"            duplicated business keys       : {w['dup_keys']:,}")
    print(f"            rows carrying the retry batch  : {w['retry_batch_rows']:,}")
    print(f"            manual adjustments in window   : {w['manual_adj_in_window']}"
          f"  of {w['manual_adj_total']}; all "
          f"{w['adjusted_rows_differing_from_snapshot']} differ from the snapshot")
    print(f"  defect 2  switch {SWITCH_DATE}")
    print(f"            rows violating {LAYOUT_INVARIANT}")
    print(f"                                           : {w['stale_layout_rows']:,}")
    print(f"            duplicate keys in fct_revenue  : {w['revenue_dup_keys']}"
          f"   <- defect 2 leaves NO structural trace")
    print(f"  A027      job runs {w['job_runs']:,}, not-success {w['job_runs_not_success']}"
          f" | freshness not-PASS {w['freshness_not_pass']}"
          f" | dq not-PASS {w['dq_not_pass']}")
    la = stats["late_arrivals"]
    print(f"  A044      {la['partitions']} partitions, {la['rows']} rows "
          f"({la['new_keys']} new + {la['restated_keys']} restated), "
          f"arriving {la['arrival_ts']}")
    print()
    print(f"  symptom   window {stats['window_lines_per_day']:,} lines/day vs "
          f"baseline {stats['baseline_lines_per_day']:,}  -> ratio "
          f"{stats['symptom_ratio']}")

    if a.verify_twice:
        tmp = Path(tempfile.mkdtemp(prefix="gt004-det-"))
        try:
            generate(tmp, a.seed)
            ok = True
            h1, h2 = tree_hashes(out), tree_hashes(tmp)
            print(f"\n  determinism, build A = {out}")
            print(f"                build B = {tmp}")
            print(f"    text files : {len(h1)} hashed, byte-identical: {h1 == h2}")
            for k in sorted(set(h1) | set(h2)):
                if h1.get(k) != h2.get(k):
                    ok = False
                    print(f"      DIFFERS {k}\n        A {h1.get(k)}\n        B {h2.get(k)}")
            for db in ("world.duckdb", "expected.duckdb"):
                d1, d2 = content_digest(out / db), content_digest(tmp / db)
                print(f"    {db:<16}: {len(d1)} tables, content-identical: {d1 == d2}")
                for t in sorted(set(d1) | set(d2)):
                    if d1.get(t) != d2.get(t):
                        ok = False
                        print(f"      DIFFERS {t}\n        A {d1.get(t)}\n        B {d2.get(t)}")
            # Stated, not hidden: the containers themselves do not match, and
            # that is a duckdb storage property, not a defect in this world.
            for db in ("world.duckdb", "expected.duckdb"):
                b1 = hashlib.sha256((out / db).read_bytes()).hexdigest()[:16]
                b2 = hashlib.sha256((tmp / db).read_bytes()).hexdigest()[:16]
                print(f"    {db:<16}: container bytes {b1} vs {b2} "
                      f"({'match' if b1 == b2 else 'DIFFER, expected'}) "
                      f"- not part of the guarantee, see the module docstring")
            if not ok:
                return 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
