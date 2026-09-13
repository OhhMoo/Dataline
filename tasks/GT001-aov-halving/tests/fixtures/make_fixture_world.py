#!/usr/bin/env python3
"""
GRADER-DEVELOPMENT FIXTURE ONLY — not the production world builder.

Track A owns the real `seed -> world` builder. This exists so grade.py can be
written and tested today without waiting for it. Both must satisfy the same
contract (SEED_SPEC.md); if they disagree, SEED_SPEC.md wins.

    python make_fixture_world.py --out ./build --seed 1337

Generation happens in SQL, not Python. Two reasons: it is ~100x faster than
row-by-row inserts, and it removes Python-side RNG call ordering from the
determinism surface entirely — every value derives from hash(seed, row_id),
so the same seed gives the same bytes regardless of interpreter or thread
count (clause D6).
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import duckdb

# --- contract constants, from SEED_SPEC.md ---------------------------------
HIST_START, HIST_END = "2025-09-05", "2026-03-04"
WIN_START, WIN_END = "2026-02-01", "2026-03-02"
FRAG_DATES = ("2026-03-03", "2026-03-04")
ADJ_DATES = ("2026-02-04", "2026-02-11", "2026-02-19", "2026-02-26")
ADJ_PER_DATE = 103                      # 4 * 103 = 412
DUP_BATCH = "retry-20260303-0412"
ORDERS_PER_DAY = 2000
TZ_BAND_PCT = 0.014
FANOUT_PROMOS = 7
FRAG_KEEP = 0.42

DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS warehouse;
"""

# rnd(k, n) -> deterministic float in [0,1) from the seed and a row-unique key
RND = "((hash({k} * 1000003 + {n} + {salt}) % 1000000) / 1000000.0)"


def orders_sql(seed: int, duplicated: bool) -> str:
    """fct_orders.

    `duplicated` controls ONLY the primary defect (the append-mode retry).
    The timezone cast, promo fan-out and fragment shortfall are permanent world
    features present in BOTH the broken and expected worlds: they are optional
    secondary findings, so `expected` must retain them or the row-level diff
    would penalise an agent for correctly repairing only what was asked.
    """
    tz_shift = "CASE WHEN late THEN d + INTERVAL 1 DAY ELSE d END"
    frag = (f"CASE WHEN d IN (DATE '{FRAG_DATES[0]}', DATE '{FRAG_DATES[1]}') "
            f"THEN {int(ORDERS_PER_DAY * FRAG_KEEP)} ELSE {ORDERS_PER_DAY} END")
    base = f"""
WITH days AS (
  SELECT UNNEST(generate_series(DATE '{HIST_START}', DATE '{HIST_END}',
                                INTERVAL 1 DAY))::DATE AS d
),
sized AS (SELECT d, {frag} AS n FROM days),
lines AS (
  SELECT s.d, g.i,
         ((s.d - DATE '2025-09-05')::BIGINT * 100000 + g.i) AS rid
  FROM sized s, LATERAL (SELECT UNNEST(generate_series(1, s.n)) AS i) g
),
f AS (
  SELECT d, i, rid,
         {RND.format(k='rid', n=1, salt=seed)} AS r1,
         {RND.format(k='rid', n=2, salt=seed)} AS r2,
         {RND.format(k='rid', n=3, salt=seed)} AS r3,
         {RND.format(k='rid', n=4, salt=seed)} AS r4,
         {RND.format(k='rid', n=5, salt=seed)} AS r5
  FROM lines
),
g AS (
  SELECT *, (r1 < {TZ_BAND_PCT}) AS late,
         (d IN (DATE '{ADJ_DATES[0]}', DATE '{ADJ_DATES[1]}',
                DATE '{ADJ_DATES[2]}', DATE '{ADJ_DATES[3]}')
          AND i <= {ADJ_PER_DATE}) AS is_adj
  FROM f
)
SELECT
  10000000 + rid                                   AS order_id,
  1                                                AS line_nbr,
  ({tz_shift})::DATE                               AS order_dt,
  (d + INTERVAL (CASE WHEN late THEN 23
                      ELSE (r2*23)::INT END) HOUR
     + INTERVAL ((r3*59)::INT) MINUTE)::TIMESTAMP  AS order_ts,
  (1 + (r4*49999)::BIGINT)                         AS customer_id,
  'SKU-' || lpad(((r5*119)::INT + 1)::VARCHAR, 4, '0') AS sku,
  (1 + (r2*3)::INT)                                AS qty,
  ROUND(5 + r3*245, 2)::DECIMAL(10,2)              AS unit_price,
  is_adj                                           AS is_manual_adjustment,
  CASE WHEN is_adj THEN 'finance correction: pricing error' END AS adjustment_note,
  TIMESTAMP '2026-03-04 02:00:00'                  AS load_ts,
  'nightly-20260304'                               AS batch_id
FROM g
"""
    if not duplicated:
        return base
    # THE PRIMARY DEFECT: the retry re-appended every row already in the window
    return f"""
{base}
UNION ALL
SELECT order_id, line_nbr, order_dt, order_ts, customer_id, sku, qty, unit_price,
       is_manual_adjustment, adjustment_note,
       TIMESTAMP '2026-03-03 04:12:00', '{DUP_BATCH}'
FROM ({base}) x
WHERE order_dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}'
"""


def revenue_sql(seed: int, _unused: bool = True) -> str:
    """One row per order line — revenue is never duplicated. Decoy 2 lives here."""
    # decoy 2 is a permanent world feature, present in both worlds
    inflate = (f"CASE WHEN promo_id IS NOT NULL "
               f"AND CAST(substr(promo_id, 7) AS INT) <= {FANOUT_PROMOS} "
               f"THEN gross * (1.02 + rr*0.01) ELSE gross END")
    return f"""
WITH o AS (SELECT DISTINCT order_id, line_nbr, order_dt, qty, unit_price
           FROM warehouse.fct_orders),
p AS (
  SELECT *, ROUND(qty * unit_price, 2) AS gross,
         {RND.format(k='order_id', n=7, salt=seed)} AS rp,
         {RND.format(k='order_id', n=8, salt=seed)} AS rr
  FROM o
),
q AS (
  SELECT *, CASE WHEN rp < 0.30
              THEN 'PROMO-' || lpad(((rp*130)::INT % 40 + 1)::VARCHAR, 3, '0')
            END AS promo_id
  FROM p
)
SELECT order_id, line_nbr, order_dt,
       gross::DECIMAL(12,2)                AS gross_amount,
       promo_id,
       ROUND({inflate}, 2)::DECIMAL(12,2)  AS net_amount,
       TIMESTAMP '2026-03-04 02:05:00'     AS load_ts,
       'nightly-20260304'                  AS batch_id
FROM q
"""


# NOTE the UNQUALIFIED table names in the body. SEED_SPEC deploys this file as
# /var/warehouse/warehouse.duckdb, so at runtime the DuckDB catalog is also
# called "warehouse" and a stored 2-part reference like warehouse.fct_orders is
# ambiguous ("Ambiguous reference to catalog or schema"). Unqualified names
# resolve against the view's own schema and work under both filenames.
# This only reproduces when the file is named warehouse.duckdb - the fixture
# builds world.duckdb, so it surfaced solely at Track A/B/C integration.
KPI_VIEW = """
CREATE OR REPLACE VIEW warehouse.kpi_daily AS
SELECT o.order_dt AS dt, COUNT(*) AS order_count,
       COALESCE(r.revenue, 0) AS revenue,
       ROUND(COALESCE(r.revenue,0) / NULLIF(COUNT(*),0), 2) AS aov
FROM fct_orders o
LEFT JOIN (SELECT order_dt, SUM(net_amount) AS revenue
           FROM fct_revenue GROUP BY order_dt) r ON r.order_dt = o.order_dt
GROUP BY o.order_dt, r.revenue ORDER BY 1;
"""


def build_db(path: Path, seed: int, duplicated: bool):
    for p in (path, Path(str(path) + ".wal")):
        p.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    con.execute("SET threads TO 1;")          # clause D6
    con.execute(DDL)
    con.execute(f"CREATE TABLE warehouse.fct_orders AS {orders_sql(seed, duplicated)}")
    con.execute(f"CREATE TABLE warehouse.fct_revenue AS {revenue_sql(seed)}")

    dup_promos = (f"""
    UNION ALL
    SELECT promo_id, promo_name || ' (legacy)', discount_pct, valid_from, valid_to
    FROM base WHERE CAST(substr(promo_id, 7) AS INT) <= {FANOUT_PROMOS}""")
    con.execute(f"""
    CREATE TABLE warehouse.dim_promotions AS
    WITH base AS (
      SELECT 'PROMO-' || lpad(i::VARCHAR,3,'0') AS promo_id,
             'Promotion ' || i::VARCHAR         AS promo_name,
             10.0::DECIMAL(5,2)                 AS discount_pct,
             DATE '{HIST_START}' AS valid_from, DATE '{HIST_END}' AS valid_to
      FROM (SELECT UNNEST(generate_series(1,40)) AS i))
    SELECT * FROM base {dup_promos}""")

    con.execute("""CREATE TABLE warehouse.pipeline_watermark AS
      SELECT 'daily_orders' AS job, DATE '2026-03-04' AS last_loaded_dt,
             TIMESTAMP '2026-03-04 02:00:00' AS updated_at
      UNION ALL SELECT 'daily_revenue', DATE '2026-03-04',
             TIMESTAMP '2026-03-04 02:05:00'""")
    con.execute(KPI_VIEW)
    con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./build")
    ap.add_argument("--seed", type=int, default=1337)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)

    for duplicated, name in ((True, "world.duckdb"), (False, "expected.duckdb")):
        build_db(out / name, a.seed, duplicated)
        con = duckdb.connect(str(out / name))
        n = con.execute("SELECT COUNT(*) FROM warehouse.fct_orders").fetchone()[0]
        con.close()
        print(f"  {name:<16} {n:>8,} order rows")

    con = duckdb.connect(str(out / "world.duckdb"))
    dup = con.execute("SELECT COUNT(*) FROM (SELECT order_id,line_nbr "
                      "FROM warehouse.fct_orders GROUP BY 1,2 HAVING COUNT(*)>1)").fetchone()[0]
    adj = con.execute("SELECT COUNT(*) FROM warehouse.fct_orders "
                      "WHERE is_manual_adjustment").fetchone()[0]
    inw, before = con.execute(f"""
      SELECT ROUND(AVG(CASE WHEN dt BETWEEN DATE '{WIN_START}' AND DATE '{WIN_END}'
                            THEN aov END),2),
             ROUND(AVG(CASE WHEN dt < DATE '{WIN_START}' THEN aov END),2)
      FROM warehouse.kpi_daily""").fetchone()
    con.close()
    print(f"\n  duplicated business keys : {dup:,}")
    print(f"  manual-adjustment rows   : {adj}   (contract: 412)")
    print(f"  AOV before window        : {before}")
    print(f"  AOV inside window        : {inw}")
    print(f"  ratio                    : {round(float(inw)/float(before),3)}  <- must be ~0.5")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
