#!/usr/bin/env python3
"""
GRADER-DEVELOPMENT FIXTURE ONLY - not the production world builder.

Track A owns the real `seed -> world` builder. This exists so grade.py can be
written and tested today without waiting for it. Both must satisfy the same
contract (SEED_SPEC.md); if they disagree, SEED_SPEC.md wins.

    python make_fixture_world.py --out ./build --seed 4211

Generation happens in SQL, not Python. Two reasons, both learned on GT001: it
is ~100x faster than row-by-row inserts (row-by-row timed out at 400k rows),
and it removes Python-side RNG call ordering from the determinism surface
entirely - every value derives from hash(seed, row_id), so the same seed gives
the same bytes regardless of interpreter or thread count.

NOTHING RACES HERE. The interleave this task is built around is staged as
committed artifacts: a leftover __dbt_backup relation, a scheduler run log with
explicit start/end stamps, and one snapshot row whose fact-derived and
dim-derived columns come from different builds. The only thing that actually
runs concurrently is the scoring probe in grade.py, at fixed offsets.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

# --- contract constants, from SEED_SPEC.md ---------------------------------
HIST_START, HIST_END = "2025-11-03", "2026-03-04"
VIRTUAL_NOW = "2026-03-05 09:00:00"

SNAP_START = "2026-02-10 00:00:00"          # rev_snapshot retention window
SNAP_END = "2026-03-05 09:00:00"

TORN_TS = "2026-03-03 08:15:00"             # the one torn read
NEXT_TS = "2026-03-03 08:30:00"             # the first clean read after it
FILTER_LIVE_TS = "2026-03-03 17:15:00"      # Ravi's predicate reaches the snapshot
RESELLER_ONBOARD_TS = "2026-02-25 00:15:00"  # new resellers appear in the dim

TOTAL_CUSTOMERS = 10180                     # ids 500001..510180, ONE id space
BASE_ID0 = 500000
NEW_RESELLERS = 1180                        # 1,180 of them, chosen by hash
NEW_BATCH = "cm-20260225-0001"              # the ONLY thing that marks the cohort
NEW_RESELLER_FIRST_DT = "2026-03-02"        # channel launch

LINES_PER_DAY = 1300
RESELLER_LINES = {"2026-03-02": 600, "2026-03-03": 620, "2026-03-04": 650}

FX_GAP_START, FX_GAP_END = "2026-02-16", "2026-03-04"   # JPY rows missing here
JPY_BEFORE, JPY_AFTER = 0.00712, 0.00655

VDUP_DT = "2026-02-24"                      # decoy: vendor re-sent one chunk
VDUP_LINES = 31

DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS warehouse;
"""

# The launch cohort, addressed by what the world says about it rather than by
# an id range. Every downstream reference goes through this.
NEW_PRED = "source_batch = '" + NEW_BATCH + "'"
NEW_IDS = "SELECT customer_id FROM raw.customer_master WHERE " + NEW_PRED

# The accounts the PREVIOUS build's dimension carried twice. Deliberately a
# PROPER SUBSET of the launch cohort, and of nothing else.
#
# Ablation, 2026-09-13: when the duplicated set and the launch cohort were the
# same 1,180 accounts, the tick's excess equalled the launch cohort's
# launch-day revenue exactly - so `source_batch = ...`, `valid_from = ...` and
# "customers whose first order was 2026-03-02" each reproduced the excess from
# LIVE data, and an agent could identify the affected accounts without ever
# opening the leftover relation. The duplication was an artefact of the vendor
# packaging one chunk of the onboarding file twice, which has no reason to
# cover the whole batch, so it no longer does. No live predicate isolates this
# set; only warehouse.mart_revenue_dim__dbt_backup does.
DUP_PRED = (NEW_PRED + " AND (hash(customer_id * 1000003 + 19 + {salt}) % 100) < 76")


def dup_ids(seed: int) -> str:
    return ("SELECT customer_id FROM raw.customer_master WHERE "
            + DUP_PRED.format(salt=seed))

# rnd(k, n) -> deterministic float in [0,1) from the seed and a row-unique key
RND = "((hash({k} * 1000003 + {n} + {salt}) % 1000000) / 1000000.0)"



# --------------------------------------------------------------- connection
def attach(path: Path, read_only: bool = False):
    """NEVER open a warehouse file directly.

    Deployed at its contract name /var/warehouse/warehouse.duckdb, DuckDB names
    the catalog after the file stem, so catalog == schema == "warehouse" and
    every warehouse.<table> reference raises "Ambiguous reference to catalog or
    schema". The fixture builds world.duckdb, where the collision cannot happen,
    which is exactly why it went unnoticed in GT001's oracle until the world was
    deployed under its contract name. One idiom everywhere, so the fixture path
    and the deployed path cannot diverge.
    """
    con = duckdb.connect(":memory:", config={"threads": 1})
    ro = " (READ_ONLY)" if read_only else ""
    con.execute("ATTACH '" + str(path).replace("'", "''") + "' AS wh" + ro)
    con.execute("USE wh")
    return con


# --------------------------------------------------------------- customers
def customers_sql(seed: int) -> str:
    """10,180 customers in ONE contiguous id space.

    The 1,180 accounts of the reseller channel onboarded on 2026-02-25 are
    chosen by hash and scattered through that space. An earlier version gave
    them their own id block above 900000, which was a fixture artefact and a
    giveaway: `ORDER BY customer_id DESC` separated the cohort at a glance,
    with nothing in the world to justify it. The only thing that marks them now
    is source_batch / valid_from, which is ordinary customer-master texture.
    """
    r1 = RND.format(k="cid", n=11, salt=seed)
    r2 = RND.format(k="cid", n=12, salt=seed)
    r3 = RND.format(k="cid", n=13, salt=seed)
    return f"""
WITH ids AS (
  SELECT {BASE_ID0} + UNNEST(generate_series(1, {TOTAL_CUSTOMERS}))::BIGINT AS cid
),
b AS (
  SELECT cid, {r1} AS a, {r2} AS b, {r3} AS c FROM ids
),
marked AS (
  SELECT *, row_number() OVER (ORDER BY c, cid) <= {NEW_RESELLERS} AS is_new
  FROM b
)
SELECT cid AS customer_id,
       CASE WHEN is_new THEN (CASE WHEN a < 0.60 THEN 'AMER' ELSE 'EMEA' END)
            WHEN a < 0.40 THEN 'AMER' WHEN a < 0.70 THEN 'EMEA'
            WHEN a < 0.90 THEN 'APAC' ELSE 'LATAM' END          AS region,
       CASE WHEN is_new THEN 'RESELLER'
            WHEN b < 0.35 THEN 'ENTERPRISE' WHEN b < 0.89 THEN 'MIDMARKET'
            ELSE 'RESELLER' END                                 AS customer_segment,
       CASE WHEN is_new THEN '{NEW_BATCH}' ELSE 'cm-20251103-0001' END AS source_batch,
       CASE WHEN is_new THEN DATE '2026-02-25' ELSE DATE '{HIST_START}' END AS valid_from
FROM marked
"""


# ---------------------------------------------------------------- fx rates
def fx_sql(seed: int, with_gap: bool) -> str:
    """USD/EUR/JPY/BRL daily rates. JPY steps down on 2026-02-16.

    `with_gap` omits the JPY rows from FX_GAP_START..FX_GAP_END: the vendor's
    APAC file stopped arriving and nobody noticed, because stg_revenue_lines
    resolves the rate with an AS OF join that silently forward-fills."""
    gap = (f"AND NOT (cur = 'JPY' AND d BETWEEN DATE '{FX_GAP_START}' "
           f"AND DATE '{FX_GAP_END}')") if with_gap else ""
    w = RND.format(k="(epoch(d)::BIGINT/86400)", n="len(cur)", salt=seed)
    return f"""
WITH days AS (
  SELECT UNNEST(generate_series(DATE '2025-11-01', DATE '{HIST_END}',
                                INTERVAL 1 DAY))::DATE AS d),
cur AS (SELECT UNNEST(['USD','EUR','JPY','BRL']) AS cur),
x AS (SELECT d, cur, {w} AS w FROM days, cur)
SELECT d AS rate_date, cur AS currency,
       (CASE cur
          WHEN 'USD' THEN 1.0
          WHEN 'EUR' THEN ROUND(1.08 + (w - 0.5) * 0.012, 8)
          WHEN 'BRL' THEN ROUND(0.185 + (w - 0.5) * 0.004, 8)
          ELSE ROUND(CASE WHEN d < DATE '{FX_GAP_START}' THEN {JPY_BEFORE}
                          ELSE {JPY_AFTER} END + (w - 0.5) * 0.00004, 8)
        END)::DECIMAL(12,8) AS rate_to_usd,
       'fx_daily_' || strftime(d, '%Y%m%d') || '.csv' AS source_file
FROM x
WHERE TRUE {gap}
"""


FX_INBOX_SQL = f"""
-- The loader's landing table for files dropped in data/landing/fx/.
-- The last seven daily drops (already merged) plus the vendor's redelivery of
-- the APAC series, received 2026-03-05 and not yet merged.
SELECT rate_date, currency, rate_to_usd, source_file,
       (rate_date + INTERVAL 1 DAY + INTERVAL 4 HOUR)::TIMESTAMP AS received_at
FROM _fx_gapped WHERE rate_date >= DATE '2026-02-26'
UNION ALL
SELECT rate_date, currency, rate_to_usd,
       'fx_jpy_20260216_20260304.csv',
       TIMESTAMP '2026-03-05 04:10:00'
FROM _fx_full WHERE currency = 'JPY'
  AND rate_date BETWEEN DATE '{FX_GAP_START}' AND DATE '{FX_GAP_END}'
"""


# ------------------------------------------------------------ revenue lines
def lines_sql(seed: int) -> str:
    """~165k order lines. Two things are planted here and nothing else:

    - the reseller channel's launch volume on 2026-03-02..03-04, which is the
      first revenue the 1,180 new customers ever have; and
    - VDUP_LINES genuinely duplicated lines on VDUP_DT, where the vendor re-sent
      one chunk of a file. A real, small, findable defect on the wrong date."""
    return f"""
WITH days AS (
  SELECT UNNEST(generate_series(DATE '{HIST_START}', DATE '{HIST_END}',
                                INTERVAL 1 DAY))::DATE AS d),
base AS (
  SELECT d, i, ((d - DATE '{HIST_START}')::BIGINT * 100000 + i) AS rid
  FROM days, LATERAL (SELECT UNNEST(generate_series(1, {LINES_PER_DAY})) AS i)),
res AS (
  SELECT d, i, 90000000 + ((d - DATE '{HIST_START}')::BIGINT * 100000 + i) AS rid
  FROM (SELECT UNNEST(['{NEW_RESELLER_FIRST_DT}','2026-03-03','2026-03-04'])::DATE AS d,
               UNNEST([{RESELLER_LINES['2026-03-02']},
                       {RESELLER_LINES['2026-03-03']},
                       {RESELLER_LINES['2026-03-04']}]) AS n),
       LATERAL (SELECT UNNEST(generate_series(1, n)) AS i)),
allrows AS (
  SELECT d, rid, FALSE AS is_new_reseller FROM base
  UNION ALL SELECT d, rid, TRUE FROM res),
cr AS (
  SELECT customer_id, TRUE AS is_new,
         row_number() OVER (ORDER BY customer_id) AS rn
  FROM raw.customer_master WHERE {NEW_PRED}
  UNION ALL
  SELECT customer_id, FALSE,
         row_number() OVER (ORDER BY customer_id)
  FROM raw.customer_master WHERE NOT ({NEW_PRED})),
sizes AS (
  SELECT COUNT(*) FILTER (WHERE is_new)     AS n_new,
         COUNT(*) FILTER (WHERE NOT is_new) AS n_base
  FROM cr),
f AS (
  SELECT d, rid, is_new_reseller,
         {RND.format(k='rid', n=1, salt=seed)} AS r1,
         {RND.format(k='rid', n=2, salt=seed)} AS r2,
         {RND.format(k='rid', n=3, salt=seed)} AS r3
  FROM allrows),
pick AS (
  SELECT f.*,
         CASE WHEN f.is_new_reseller
              THEN 1 + (f.r1 * (s.n_new - 1))::BIGINT
              ELSE 1 + (f.r1 * (s.n_base - 1))::BIGINT END AS pick_rn
  FROM f CROSS JOIN sizes s),
g AS (
  SELECT p.d, p.rid, p.is_new_reseller, p.r2, p.r3, cr.customer_id
  FROM pick p
  JOIN cr ON cr.is_new = p.is_new_reseller AND cr.rn = p.pick_rn),
j AS (
  SELECT g.*, c.region
  FROM g JOIN raw.customer_master c USING (customer_id))
SELECT
  10000000 + rid                              AS order_id,
  1                                           AS line_nbr,
  d                                           AS order_dt,
  customer_id,
  CASE region WHEN 'AMER' THEN 'USD' WHEN 'EMEA' THEN 'EUR'
              WHEN 'APAC' THEN 'JPY' ELSE 'BRL' END AS currency,
  (CASE region
     WHEN 'AMER' THEN ROUND(200 + r2 * 7800, 2)
     WHEN 'EMEA' THEN ROUND(185 + r2 * 7215, 2)
     WHEN 'APAC' THEN ROUND(30000 + r2 * 1170000, 2)
     ELSE ROUND(1080 + r2 * 41920, 2) END)::DECIMAL(14,2) AS amount_local,
  'rev-' || strftime(d + INTERVAL 1 DAY, '%Y%m%d') || '-0500' AS load_batch,
  (CASE WHEN d = DATE '2026-03-02'
        THEN TIMESTAMP '2026-03-03 08:14:49'
        ELSE (d + INTERVAL 1 DAY + INTERVAL 5 HOUR + INTERVAL 4 MINUTE)::TIMESTAMP
   END)                                       AS loaded_at
FROM j
UNION ALL
-- decoy: the vendor re-sent one chunk of the 2026-02-24 file. Same business
-- keys, different load_batch. Real, small, and on the wrong date.
SELECT order_id, line_nbr, order_dt, customer_id, currency, amount_local,
       'rev-20260225-0500-resend', loaded_at + INTERVAL 22 MINUTE
FROM (
  SELECT 10000000 + rid AS order_id, 1 AS line_nbr, d AS order_dt, customer_id,
         CASE region WHEN 'AMER' THEN 'USD' WHEN 'EMEA' THEN 'EUR'
                     WHEN 'APAC' THEN 'JPY' ELSE 'BRL' END AS currency,
         (CASE region
            WHEN 'AMER' THEN ROUND(200 + r2 * 7800, 2)
            WHEN 'EMEA' THEN ROUND(185 + r2 * 7215, 2)
            WHEN 'APAC' THEN ROUND(30000 + r2 * 1170000, 2)
            ELSE ROUND(1080 + r2 * 41920, 2) END)::DECIMAL(14,2) AS amount_local,
         (d + INTERVAL 1 DAY + INTERVAL 5 HOUR + INTERVAL 4 MINUTE)::TIMESTAMP AS loaded_at,
         row_number() OVER (ORDER BY rid) AS rn
  FROM j WHERE d = DATE '{VDUP_DT}' AND region = 'AMER')
WHERE rn <= {VDUP_LINES}
"""


# ------------------------------------------------------------------ staging
STG_LINES_SQL = """
-- AS OF join: the rate in force on or before the order date. This is what
-- silently forward-fills across the missing APAC window.
SELECT l.order_id, l.line_nbr, l.order_dt, l.customer_id,
       c.customer_segment, l.currency, l.amount_local,
       f.rate_to_usd AS fx_rate,
       ROUND(l.amount_local * f.rate_to_usd, 2)::DECIMAL(16,2) AS amount_usd,
       l.loaded_at
FROM raw.revenue_lines l
JOIN raw.customer_master c USING (customer_id)
ASOF JOIN raw.fx_rates_daily f
  ON l.currency = f.currency AND l.order_dt >= f.rate_date
"""

STG_CUSTOMER_SQL = """
-- No deduplication. Harmless for eight months: the only customers that ever
-- had more than one row had no revenue.
SELECT customer_id, region, customer_segment FROM raw.customer_master
"""


# --------------------------------------------------------------------- mart
def mart_fact_sql(filtered: bool) -> str:
    where = "WHERE l.customer_segment <> 'RESELLER'" if filtered else ""
    return f"""
SELECT l.order_dt, l.customer_id, l.customer_segment,
       ROUND(SUM(l.amount_usd), 2)::DECIMAL(16,2) AS revenue_usd,
       COUNT(*)::BIGINT AS order_count
FROM staging.stg_revenue_lines l
{where}
GROUP BY 1, 2, 3
"""


MART_DIM_SQL = "SELECT customer_id, region, customer_segment FROM staging.stg_customer"

# Build A's dim, left behind by the swap whose final DROP never ran. It is the
# ONLY physical evidence that the reseller cohort was ever duplicated: the
# correction that removed the duplicate rows from raw.customer_master arrived in
# the same bundle, and a delete leaves nothing behind.
def backup_dim_sql(seed: int) -> str:
    return f"""
SELECT customer_id, region, customer_segment FROM staging.stg_customer
UNION ALL
SELECT customer_id, region, customer_segment FROM staging.stg_customer
WHERE customer_id IN ({dup_ids(seed)})
"""


# ----------------------------------------------------------------- snapshot
def snapshot_sql(seed: int, stg: str = "staging.stg_revenue_lines") -> str:
    """One row per region per 15-minute tick, append-only, never rewritten.

    Everything about a tick is a pure function of the tick timestamp:
      L  - the latest order_dt the mart held at that tick
      M  - the month the MTD figure covers
      torn      - the single tick that read a half-swapped mart
      filtered  - ticks after the analyst's predicate went live
      dim_dup   - ticks whose dim still carried the duplicated reseller rows

    `stg` is ALWAYS the as-reported staging - the one with the forward-filled
    APAC rate. rev_snapshot is a record of what the mart said at the time, not a
    cache of what it says now, so the reference world carries exactly the same
    history including the currency error that was really there.
    """
    return f"""
WITH ticks AS (
  SELECT UNNEST(generate_series(TIMESTAMP '{SNAP_START}', TIMESTAMP '{SNAP_END}',
                                INTERVAL 15 MINUTE)) AS ts),
t AS (
  SELECT ts,
    CASE
      WHEN ts::DATE = DATE '2026-03-03' AND ts >= TIMESTAMP '{TORN_TS}' THEN DATE '2026-03-02'
      WHEN ts::DATE = DATE '2026-03-03' THEN DATE '2026-03-01'
      WHEN ts >= ts::DATE + INTERVAL 5 HOUR + INTERVAL 15 MINUTE THEN ts::DATE - 1
      ELSE ts::DATE - 2 END                                   AS l_dt,
    date_trunc('month', ts::DATE)::DATE                       AS m_dt,
    (ts >= TIMESTAMP '{FILTER_LIVE_TS}')                      AS filtered,
    (ts = TIMESTAMP '{TORN_TS}')                              AS torn,
    (ts >= TIMESTAMP '{RESELLER_ONBOARD_TS}'
     AND ts <= TIMESTAMP '{TORN_TS}')                         AS dim_dup
  FROM ticks),
dayreg AS (
  SELECT l.order_dt, c.region, l.customer_segment,
         SUM(l.amount_usd) AS rev, COUNT(*)::BIGINT AS cnt,
         SUM(CASE WHEN l.customer_id IN ({dup_ids(seed)}) THEN l.amount_usd ELSE 0 END) AS rev_new,
         SUM(CASE WHEN l.customer_id IN ({dup_ids(seed)}) THEN 1 ELSE 0 END)::BIGINT AS cnt_new
  FROM {stg} l
  JOIN staging.stg_customer c USING (customer_id)
  GROUP BY 1, 2, 3),
acct AS (
  SELECT region, COUNT(*)::BIGINT AS n,
         SUM(CASE WHEN customer_id IN ({dup_ids(seed)}) THEN 1 ELSE 0 END)::BIGINT AS n_new
  FROM staging.stg_customer GROUP BY 1),
agg AS (
  SELECT t.ts, d.region,
         SUM(CASE WHEN t.filtered AND d.customer_segment = 'RESELLER'
                  THEN 0 ELSE d.rev END)
           + SUM(CASE WHEN t.torn THEN d.rev_new ELSE 0 END)      AS rev,
         SUM(CASE WHEN t.filtered AND d.customer_segment = 'RESELLER'
                  THEN 0 ELSE d.cnt END)
           + SUM(CASE WHEN t.torn THEN d.cnt_new ELSE 0 END)      AS cnt
  FROM t JOIN dayreg d ON d.order_dt BETWEEN t.m_dt AND t.l_dt
  GROUP BY 1, 2)
SELECT t.ts                                        AS snapshot_ts,
       a.region                                    AS region,
       ROUND(COALESCE(g.rev, 0), 2)::DECIMAL(16,2) AS mtd_revenue_usd,
       COALESCE(g.cnt, 0)::BIGINT                  AS mtd_order_count,
       (a.n + CASE WHEN t.dim_dup THEN a.n_new ELSE 0 END)::BIGINT AS active_accounts
FROM t CROSS JOIN acct a
LEFT JOIN agg g ON g.ts = t.ts AND g.region = a.region
ORDER BY 1, 2
"""


# ---------------------------------------------------------------- scheduler
def scheduler_state() -> dict:
    """Seven days of run history. Every ordering that matters is explicit and
    committed; nothing is timed at build time."""
    runs = []

    def add(job, s, e, status="success", detail=None, trigger="cron"):
        r = {"job": job, "vts_start": s, "vts_end": e, "status": status,
             "trigger": trigger}
        if detail:
            r["detail"] = detail
        runs.append(r)

    days = ["2026-02-27", "2026-02-28", "2026-03-01", "2026-03-02",
            "2026-03-03", "2026-03-04", "2026-03-05"]
    for d in days:
        # daily_revenue_load - 05:00, except the late Tuesday
        if d == "2026-03-03":
            add("daily_revenue_load", f"{d}T05:00:00Z", f"{d}T05:00:11Z", "failed",
                "source file revenue_20260302.csv.gz not present in landing")
            add("daily_revenue_load", f"{d}T06:00:00Z", f"{d}T06:00:09Z", "failed",
                "source file revenue_20260302.csv.gz not present in landing",
                trigger="retry")
            add("daily_revenue_load", f"{d}T07:00:00Z", f"{d}T07:00:10Z", "failed",
                "source file revenue_20260302.csv.gz not present in landing",
                trigger="retry")
            add("daily_revenue_load", f"{d}T08:00:12Z", f"{d}T08:14:49Z", "success",
                "dt=2026-03-02 + customer_master bundle cm-20260303-0800", trigger="retry")
        else:
            prev = (duckdb.sql(f"SELECT (DATE '{d}' - 1)::VARCHAR").fetchone()[0])
            add("daily_revenue_load", f"{d}T05:00:00Z", f"{d}T05:04:{18:02d}Z",
                "success", f"dt={prev}")
        # mart_hourly_rebuild - :07 every hour
        for h in range(24):
            if d == "2026-03-05" and h > 8:
                break
            s = f"{d}T{h:02d}:07:00Z"
            broke = (d > "2026-03-03") or (d == "2026-03-03" and h >= 9)
            if broke:
                add("mart_hourly_rebuild", s, f"{d}T{h:02d}:07:38Z", "failed",
                    'Catalog Error: Table with name "mart_revenue_dim__dbt_backup" '
                    "already exists!")
            else:
                add("mart_hourly_rebuild", s, f"{d}T{h:02d}:07:41Z", "success")
        if d == "2026-03-03":
            # the event-driven rebuild that follows the load, and the one that
            # never finished its final DROP
            add("mart_hourly_rebuild", f"{d}T08:14:58Z", f"{d}T08:15:39Z", "failed",
                'Catalog Error: Table with name "mart_revenue_dim__dbt_backup" '
                "already exists!", trigger="downstream_of:daily_revenue_load")
        # bi_revenue_extract - every 15 minutes
        for h in range(24):
            for m in (0, 15, 30, 45):
                if d == "2026-03-05" and (h > 9 or (h == 9 and m > 0)):
                    continue
                add("bi_revenue_extract", f"{d}T{h:02d}:{m:02d}:00Z",
                    f"{d}T{h:02d}:{m:02d}:06Z", "success")
    runs.sort(key=lambda r: (r["vts_start"], r["job"]))
    return {
        "vnow": VIRTUAL_NOW.replace(" ", "T") + "Z",
        "jobs": {
            "daily_revenue_load": {"command": "load_daily_revenue", "cron": "0 5 * * *",
                                   "duration_s": 258, "retries": 3},
            "mart_hourly_rebuild": {"command": "duckdb -f pipelines/mart_rebuild.sql",
                                    "cron": "7 * * * *", "duration_s": 41,
                                    "also_triggered_by": ["daily_revenue_load"]},
            "bi_revenue_extract": {"command": "duckdb -f pipelines/bi_extract.sql",
                                   "cron": "0,15,30,45 * * * *", "duration_s": 6},
        },
        "runs": runs,
    }


# -------------------------------------------------------------------- build
def build_db(path: Path, seed: int, broken: bool):
    for p in (path, Path(str(path) + ".wal")):
        p.unlink(missing_ok=True)
    con = attach(path)
    con.execute(DDL)

    con.execute(f"CREATE TABLE raw.customer_master AS {customers_sql(seed)}")
    # _fx_full is the truth; raw.fx_rates_daily is what actually got loaded.
    con.execute(f"CREATE TEMP TABLE _fx_full AS {fx_sql(seed, with_gap=False)}")
    con.execute(f"CREATE TEMP TABLE _fx_gapped AS {fx_sql(seed, with_gap=True)}")
    con.execute(f"CREATE TABLE raw.fx_rates_daily AS {fx_sql(seed, with_gap=broken)}")
    con.execute(f"CREATE TABLE raw.fx_rates_inbox AS {FX_INBOX_SQL}")
    con.execute(f"CREATE TABLE raw.revenue_lines AS {lines_sql(seed)}")

    con.execute(f"CREATE TABLE staging.stg_revenue_lines AS {STG_LINES_SQL}")
    con.execute(f"CREATE TABLE staging.stg_customer AS {STG_CUSTOMER_SQL}")

    con.execute("CREATE TABLE warehouse.mart_revenue_fact AS "
                + mart_fact_sql(filtered=broken))
    con.execute(f"CREATE TABLE warehouse.mart_revenue_dim AS {MART_DIM_SQL}")
    con.execute(f"CREATE TABLE warehouse.mart_revenue_dim__dbt_backup AS {backup_dim_sql(seed)}")

    # The snapshot history is ALWAYS generated from the as-reported staging -
    # the one with the forward-filled APAC rate - in BOTH worlds. rev_snapshot
    # is a record of what the mart said at the time, not a cache of what it says
    # now. The late APAC rate was genuinely wrong when those rows were written
    # and it stays wrong in the record; the only rows that differ between the
    # two worlds are the ones the torn read wrote, which report a state the mart
    # never actually held.
    if broken:
        stg = "staging.stg_revenue_lines"
    else:
        con.execute(f"""
          CREATE TEMP TABLE _stg_asreported AS
          SELECT l.order_id, l.line_nbr, l.order_dt, l.customer_id,
                 c.customer_segment, l.currency, l.amount_local,
                 f.rate_to_usd AS fx_rate,
                 ROUND(l.amount_local * f.rate_to_usd, 2)::DECIMAL(16,2) AS amount_usd,
                 l.loaded_at
          FROM raw.revenue_lines l
          JOIN raw.customer_master c USING (customer_id)
          ASOF JOIN _fx_gapped f
            ON l.currency = f.currency AND l.order_dt >= f.rate_date""")
        stg = "_stg_asreported"

    con.execute(f"CREATE TEMP TABLE _snap AS {snapshot_sql(seed, stg)}")
    if broken:
        con.execute("CREATE TABLE warehouse.rev_snapshot AS "
                    "SELECT * FROM _snap ORDER BY 1, 2")
    else:
        # expected: identical history, with ONLY the torn tick's rows restated
        # to the values the next clean read produced.
        con.execute(f"""
          CREATE TABLE warehouse.rev_snapshot AS
          SELECT s.snapshot_ts, s.region,
                 COALESCE(n.mtd_revenue_usd, s.mtd_revenue_usd) AS mtd_revenue_usd,
                 COALESCE(n.mtd_order_count, s.mtd_order_count) AS mtd_order_count,
                 COALESCE(n.active_accounts, s.active_accounts) AS active_accounts
          FROM _snap s
          LEFT JOIN (SELECT region, mtd_revenue_usd, mtd_order_count, active_accounts
                     FROM _snap WHERE snapshot_ts = TIMESTAMP '{NEXT_TS}') n
            ON s.snapshot_ts = TIMESTAMP '{TORN_TS}' AND n.region = s.region
          ORDER BY 1, 2""")
        con.execute("DROP TABLE _stg_asreported")
    con.execute("DROP TABLE _snap")
    con.execute("DROP TABLE _fx_full")
    con.execute("DROP TABLE _fx_gapped")
    con.execute("CHECKPOINT wh")
    con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./build")
    ap.add_argument("--seed", type=int, default=4211)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    for broken, name in ((True, "world.duckdb"), (False, "expected.duckdb")):
        build_db(out / name, a.seed, broken)
        con = attach(out / name, read_only=True)
        n = con.execute("SELECT COUNT(*) FROM staging.stg_revenue_lines").fetchone()[0]
        con.close()
        print(f"  {name:<16} {n:>9,} revenue lines")

    (out / "sched_state.json").write_text(
        json.dumps(scheduler_state(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")

    con = attach(out / "world.duckdb", read_only=True)
    q = con.execute(f"""
      SELECT
        (SELECT SUM(mtd_revenue_usd) FROM warehouse.rev_snapshot
          WHERE snapshot_ts = TIMESTAMP '{TORN_TS}'),
        (SELECT SUM(mtd_revenue_usd) FROM warehouse.rev_snapshot
          WHERE snapshot_ts = TIMESTAMP '{NEXT_TS}'),
        (SELECT COUNT(*) FROM warehouse.mart_revenue_dim__dbt_backup),
        (SELECT COUNT(*) FROM warehouse.mart_revenue_dim),
        (SELECT COUNT(*) FROM warehouse.mart_revenue_fact),
        (SELECT COUNT(*) FROM warehouse.rev_snapshot)""").fetchone()
    con.close()
    torn, clean = float(q[0]), float(q[1])
    print(f"\n  reported MTD at {TORN_TS} : {torn:>16,.2f}")
    print(f"  first clean read {NEXT_TS}  : {clean:>16,.2f}")
    print(f"  overstatement                      : {torn - clean:>16,.2f}"
          f"   ({100*(torn/clean - 1):.1f}%)")
    print(f"  backup dim rows / live dim rows    : {q[2]:,} / {q[3]:,}")
    print(f"  mart_revenue_fact rows             : {q[4]:,}")
    print(f"  rev_snapshot rows                  : {q[5]:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
