#!/usr/bin/env python3
"""
GT006c/d (STACKED) fixture generator - the world, the reference and the probe batch.

STACKED UNIT. This is GT006 with a SECOND, EASY, INDEPENDENT defect planted on
top of the original one, for the scope-closure 2x2 in
implementation_structure/TASK_DESIGN_2026-09-15.md:

    DEFECT B  (unchanged from GT006, byte-identical rows, dates and counts)
              the NULL-unsafe harness exclusion in the nightly loader silently
              drops every card-not-present payment whose terminal_ref is empty.
              Lives in: pipelines/build_payments.sql -> warehouse.fct_payments.
              Touches merchants M0001..M0006 only (the legacy acquirer).

    DEFECT A  (new) the merchant monthly rollup, which moved into dbt on
              DIM_LOADED, joins INNER to reporting.dim_merchant for the
              merchant name. That table was loaded once from the commercial
              team's list, config/merchants.csv, which stops at M0030. Every
              merchant from M0031 upward has settled every day since the feed
              began and is absent from reporting.rpt_merchant_monthly entirely.
              Lives in: dbt_project/models/reporting/rpt_merchant_monthly.sql
              -> reporting.rpt_merchant_monthly. A different pipeline stage, a
              different table, a different mechanism, a DISJOINT set of
              merchants. fixture_contract._stacking_checks asserts all four.

A is the incident that got reported (see prompts/). It is obvious - the
merchants are missing from the report outright and the join is ten lines long.
B is what a thorough engineer finds underneath when they check that the
repaired statements actually tie to the processor.

WHAT IS BYTE-IDENTICAL TO GT006. raw.payments_landing, warehouse.fct_payments,
warehouse.kpi_payments_daily, warehouse.bi_payments_extract, ops.* and the
reference expected.duckdb have the same content digests as the original
contract; fixture_contract.py asserts the original digests for every one of
them. Every pipeline file, config/checks.yml, the runbook, the data dictionary
and the KPI model are the same bytes. What differs: reporting.rpt_merchant_monthly
(now short ten merchants and carrying merchant_name), the new
reporting.dim_merchant, the rollup model, sources.yml, schema.yml, and one line
each in README.md and CHANGELOG.md about where merchant names come from.

---------------------------------------------------------------------------

    python make_fixture_world.py --out ./build --seed 6006
    python make_fixture_world.py --out ./build --seed 6006 --verify-twice
    python make_fixture_world.py --out ./build --seed 6006 --json

THE MECHANISM (archetype A003). A data-quality rule written to keep processor
test-harness traffic out of the payments mart is NULL-unsafe:

    WHERE terminal_ref NOT IN ('TEST-HARNESS', 'SMOKE-TEST')

Under SQL's three-valued logic a NULL `terminal_ref` makes that predicate
UNKNOWN, not TRUE, so the row is excluded. `terminal_ref` is legitimately NULL
for card-not-present payments taken through the legacy acquirer, which has no
terminal to report. Since the rule was deployed the mart has therefore been
silently dropping real, settled payments - a few percent a day, never a fixed
number, always inside the daily volume swing.

Nothing structural sees it. Row counts are plausible, the key is still unique,
every not-null control is on a column that is populated, freshness is green and
the nightly job has never failed. The only way to find it is to compare the mart
against the stream it is built from, per day, and notice the survival rate steps
down on one date.

DELIBERATELY ORTHOGONAL TO GT004 (A009). GT004 is GAIN: an append-mode re-run
duplicated a window, plus a materialisation switch left stale-layout rows. This
world has ZERO duplicate keys, ZERO re-runs, and one materialisation throughout.
An agent that arrives looking for duplicates finds none, and must argue from a
row that is ABSENT - which is the harder direction and the reason the two units
compose instead of teaching the same lesson twice.

DETERMINISM. Every value is derived from `--seed` through `rnd()` below; there
is no RNG state, no wall clock and no filesystem order anywhere in the build.
Text files are written with newline="" so a CRLF can never creep in. The
`.duckdb` CONTAINERS are NOT byte-reproducible - duckdb's storage layer writes
free-space metadata that varies between runs - so `--verify-twice` compares the
text tree byte for byte and the databases by ORDER-INSENSITIVE CONTENT DIGEST,
and prints the container mismatch rather than hiding it.

F622 - ALWAYS ALIASED ATTACH. Nothing here opens a .duckdb file directly.
The deployed warehouse lands at var/warehouse/warehouse.duckdb, where a direct
open would make the catalog and the `warehouse` schema collide and every
two-part reference raise "Binder Error: Ambiguous reference". The fixture is
built at world.duckdb, whose stem does not collide - so a direct open here would
work, and would still let through table bodies that only resolve under the
convenient filename.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import duckdb

# --------------------------------------------------------------- contract ---

def _contract_order(path):
    """Sort key for every walk that feeds a recorded digest: case-insensitive,
    component by component. The contracts were recorded on Windows, where
    `sorted()` over paths compares that way; plain `sorted()` on Linux puts
    `README.md` before `data/` and every tree digest drifted (measured
    2026-09-26: GT005, GT007 and GT008 refused to build on a Linux host).
    Explicit so the digest is the same on every OS."""
    return [part.lower() for part in path.parts]

SEED = 6006

HIST_START, HIST_END = "2025-09-15", "2026-03-14"      # 181 days
VNOW = "2026-03-15T09:00:00Z"                          # world "now"

# The processor upgrade that started emitting harness traffic into the feed.
HARNESS_START = "2025-12-05"
# The morning the rule went in. Everything from here on is the damage window.
RULE_DEPLOY = "2025-12-08"
# The three days of harness rows that DID reach the mart and were cleaned out by
# hand on the day the rule was deployed. They are why the mart is clean before
# the rule and why a backfill helper exists in the repo with those dates in it.
CLEANUP_FROM, CLEANUP_TO = "2025-12-05", "2025-12-07"

EXCLUDED_REFS = ("TEST-HARNESS", "SMOKE-TEST")
# The shipped rule, as it appears in both pipeline files. The NULL-unsafe one.
SHIPPED_PREDICATE = "terminal_ref NOT IN ('TEST-HARNESS', 'SMOKE-TEST')"

N_MERCHANTS = 40
LEGACY_ACQUIRER_MAX = 6          # merchants M0001..M0006 are on the legacy
                                 # acquirer and report no terminal for CNP
ROWS_MIN, ROWS_SPREAD = 400, 60  # 400..459 settled payments a day
ROWS_CAP = 460                   # the per-day sequence generator's upper bound

ID_BASE = 700_000_000            # payment_id = ID_BASE + day*10000 + n
HARNESS_ID_BASE = 790_000_000    # disjoint from ID_BASE by 90M
PROBE_ID_BASE = 795_000_000      # the G1c probe batch; never in the world

FEE_RATE = "0.0215"              # strings, never float literals
FEE_FLOOR = "0.10"

# The G1c probe batch: one partition the world has never seen, applied by the
# GRADER to a copy of the pre-repair world so the agent's OWN repaired rule can
# be executed against rows it has never met. Never staged into the world.
PROBE_DATE = "2026-03-15"
PROBE_ARRIVAL_TS = "2026-03-16 02:10:00"
PROBE_BATCH = "proc-20260315"
PROBE_ORDINARY = 40              # terminal_ref populated  -> must be admitted
PROBE_NULL_REF = 14              # terminal_ref NULL       -> must be admitted
PROBE_HARNESS = 6                # TEST-HARNESS/SMOKE-TEST -> must be rejected

DASHBOARD_ID = "payments_daily"

# ---- DEFECT A ---------------------------------------------------------------
# The day rpt_merchant_monthly moved into dbt and picked up merchant names from
# the commercial team's list. CHANGELOG.md already carried this date in GT006.
DIM_LOADED = "2025-11-12"
# The commercial list stops here. Merchants M0031..M0040 settle every day and
# are not in it. DISJOINT from LEGACY_ACQUIRER_MAX (M0001..M0006), which is the
# set defect B touches - asserted by fixture_contract._stacking_checks.
DIM_MERCHANTS = 30
DIM_TABLE = "reporting.dim_merchant"
ROLLUP_TABLE = "reporting.rpt_merchant_monthly"
# The shipped join. INNER. The one-word repair is `left join`; the other valid
# repair is completing the list and reloading the table.
SHIPPED_JOIN = "join {{ source('reporting', 'dim_merchant') }} m"

# Thirty trading names for the commercial list. Names, not figures: nothing in
# the world is derived from them and nothing grades them.
MERCHANT_NAMES = [
    "Harbour Lane Coffee", "Northgate Cycles", "Pimlico Books", "Sable & Fern",
    "Ashdown Garden Centre", "The Copper Kettle", "Ridgeway Outdoors",
    "Marlowe Pharmacy", "Kestrel Print", "Oakfield Butchers", "Linden Florist",
    "Brightwater Aquatics", "Tarn Street Bakery", "Fenwick Hardware",
    "Halcyon Records", "Greyfriars Wines", "Solent Marine Supplies",
    "Ember Ceramics", "Whitlock Opticians", "Castle Row Deli",
    "Ivy House Interiors", "Penrose Toys", "Larkspur Yoga", "Millbrook Vets",
    "Quayside Fish", "Redfern Tailoring", "Stanhope Stationers",
    "Thornbury Cheese", "Umber Art Supplies", "Vale Sports",
]
assert len(MERCHANT_NAMES) == DIM_MERCHANTS

MERCHANT_SEGMENTS = ["food_and_drink", "retail", "services"]

# rnd(k, n, salt) -> deterministic float in [0,1) from the seed and a row-unique
# key. `hash` is duckdb's own; the seed enters through `salt`.
RND = ("((hash(CAST({k} AS BIGINT) * 1000003 + CAST({n} AS BIGINT) + {salt}) "
       "% 1000000) / 1000000.0)")

# The projections every diff is taken on. `loaded_at` and `batch_id` are
# deliberately absent: a correct repair is free to stamp its own provenance, and
# grading a provenance string would fail honest work.
BUSINESS_COLS = {
    "fct_payments": ["payment_id", "payment_dt", "captured_at", "merchant_id",
                     "channel", "terminal_ref", "card_last4", "amount_gross",
                     "fee_amount", "amount_net", "status"],
    "kpi_payments_daily": ["dt", "payments", "merchants", "gross_amount",
                           "fee_amount", "net_amount"],
}

# The one relation the agent must not touch: the processor's delivery. Stated in
# every prompt variant's ground rules, so scoring it is not a trap.
SOURCE_OF_RECORD = "raw.payments_landing"


def rnd(k: str, n: str, salt: int) -> str:
    return RND.format(k=k, n=n, salt=salt)


# ------------------------------------------------------------ connections ---
def _create(path: Path):
    """Aliased ATTACH, never a direct open (F622)."""
    for f in (path, Path(str(path) + ".wal")):
        f.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{path.as_posix()}' AS wh")
    con.execute("USE wh")
    return con


def _readonly(path: Path, alias: str = "wh"):
    """READ_ONLY matters beyond tidiness: a read-write connection takes duckdb's
    exclusive file lock, and K rollouts verifying the same cached fixture at once
    knock each other over with a sharing violation."""
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{path.as_posix()}' AS {alias} (READ_ONLY)")
    return con


def _close(con) -> None:
    con.execute("CHECKPOINT")
    con.close()


def _write_text(path: Path, text: str) -> None:
    """newline="" - no translation, ever. A CRLF that changes no row count is
    invisible to every length check and parse test; only a byte-level diff finds
    it, and by then K rollouts have run against two different worlds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


# -------------------------------------------------------------------- SQL ---
def landing_sql(seed: int) -> str:
    """raw.payments_landing - every settled payment the processor delivered.

    This is the SYSTEM OF RECORD and the only source of truth in the world. The
    mart is built from it; the reference is built from it; the defect is that
    the mart drops rows this table holds.
    """
    day_index = "CAST(payment_dt - DATE '" + HIST_START + "' AS INTEGER)"
    day_rnd = rnd(day_index, "0", seed + 11)
    harness_rnd = rnd(day_index, "0", seed + 101)
    return f"""
WITH days AS (
  SELECT UNNEST(generate_series(DATE '{HIST_START}', DATE '{HIST_END}',
                                INTERVAL 1 DAY))::DATE AS payment_dt
), sized AS (
  SELECT payment_dt,
         CAST(payment_dt - DATE '{HIST_START}' AS INTEGER) AS di,
         {ROWS_MIN} + CAST(FLOOR({day_rnd} * {ROWS_SPREAD}) AS INTEGER) AS n_rows
  FROM days
), seq AS (
  SELECT UNNEST(generate_series(1, {ROWS_CAP})) AS n
), base AS (
  SELECT s.payment_dt, s.di, q.n,
         {ID_BASE} + s.di * 10000 + q.n AS payment_id,
         1 + CAST(FLOOR({rnd('s.di', 'q.n', seed + 23)} * {N_MERCHANTS}) AS INTEGER) AS merchant_num,
         {rnd('s.di', 'q.n', seed + 37)} AS r_channel,
         {rnd('s.di', 'q.n', seed + 53)} AS r_amount,
         {rnd('s.di', 'q.n', seed + 71)} AS r_time,
         {rnd('s.di', 'q.n', seed + 89)} AS r_card
  FROM sized s JOIN seq q ON q.n <= s.n_rows
), shaped AS (
  SELECT payment_id, payment_dt, di, n, merchant_num,
         'M' || lpad(CAST(merchant_num AS VARCHAR), 4, '0') AS merchant_id,
         CASE WHEN r_channel < 0.58 THEN 'card_present'
              WHEN r_channel < 0.92 THEN 'ecommerce'
              ELSE 'moto' END AS channel,
         payment_dt
           + INTERVAL (6 + CAST(FLOOR(r_time * 16) AS INTEGER)) HOUR
           + INTERVAL (CAST(FLOOR(r_card * 60) AS INTEGER)) MINUTE
           + INTERVAL (CAST(FLOOR(r_amount * 60) AS INTEGER)) SECOND AS captured_at,
         lpad(CAST(CAST(FLOOR(r_card * 10000) AS INTEGER) AS VARCHAR), 4, '0') AS card_last4,
         ROUND(4.00 + r_amount * 295.0, 2) AS amount_gross
  FROM base
), priced AS (
  SELECT *,
         ROUND(amount_gross * {FEE_RATE} + {FEE_FLOOR}, 2) AS fee_amount
  FROM shaped
), real_rows AS (
  SELECT
    payment_id,
    payment_dt,
    captured_at,
    merchant_id,
    channel,
    -- THE COLUMN THE WHOLE TASK TURNS ON. Card-present payments always carry a
    -- terminal. Card-not-present payments carry a virtual terminal id only if
    -- the merchant's acquirer issues one; the legacy acquirer does not, and
    -- reports NULL. That is correct behaviour and always has been.
    CASE
      WHEN channel = 'card_present'
        THEN 'T' || lpad(CAST(merchant_num * 7 + (n % 3) AS VARCHAR), 5, '0')
      WHEN merchant_num <= {LEGACY_ACQUIRER_MAX} THEN NULL
      ELSE 'V' || lpad(CAST(merchant_num AS VARCHAR), 5, '0')
    END AS terminal_ref,
    card_last4,
    CAST(amount_gross AS DECIMAL(12,2)) AS amount_gross,
    CAST(fee_amount AS DECIMAL(12,2)) AS fee_amount,
    CAST(amount_gross - fee_amount AS DECIMAL(12,2)) AS amount_net,
    'settled' AS status,
    'proc-' || strftime(payment_dt, '%Y%m%d') AS batch_id,
    payment_dt + INTERVAL 1 DAY + INTERVAL 2 HOUR + INTERVAL 10 MINUTE AS arrival_ts
  FROM priced
), harness_days AS (
  SELECT UNNEST(generate_series(DATE '{HARNESS_START}', DATE '{HIST_END}',
                                INTERVAL 1 DAY))::DATE AS payment_dt
), harness_sized AS (
  SELECT payment_dt,
         CAST(payment_dt - DATE '{HIST_START}' AS INTEGER) AS di,
         6 + CAST(FLOOR({harness_rnd} * 5) AS INTEGER) AS n_rows
  FROM harness_days
), harness_seq AS (
  SELECT UNNEST(generate_series(1, 10)) AS n
), harness_rows AS (
  -- The processor's own test harness. These rows are NOT payments: no merchant,
  -- a synthetic amount, and a reserved terminal_ref. Excluding them is correct
  -- and the rule that does it must keep doing it.
  SELECT
    {HARNESS_ID_BASE} + h.di * 100 + q.n AS payment_id,
    h.payment_dt,
    h.payment_dt + INTERVAL 3 HOUR + INTERVAL (q.n) MINUTE AS captured_at,
    'M0000' AS merchant_id,
    'card_present' AS channel,
    CASE WHEN q.n % 2 = 1 THEN '{EXCLUDED_REFS[0]}' ELSE '{EXCLUDED_REFS[1]}' END AS terminal_ref,
    '0000' AS card_last4,
    CAST(1.00 AS DECIMAL(12,2)) AS amount_gross,
    CAST(0.00 AS DECIMAL(12,2)) AS fee_amount,
    CAST(1.00 AS DECIMAL(12,2)) AS amount_net,
    'settled' AS status,
    'proc-' || strftime(h.payment_dt, '%Y%m%d') AS batch_id,
    h.payment_dt + INTERVAL 1 DAY + INTERVAL 2 HOUR + INTERVAL 10 MINUTE AS arrival_ts
  FROM harness_sized h JOIN harness_seq q ON q.n <= h.n_rows
)
SELECT * FROM real_rows
UNION ALL
SELECT * FROM harness_rows
ORDER BY payment_dt, payment_id
"""


def mart_select(source: str, *, apply_rule: bool, drop_harness: bool) -> str:
    """The mart body, in the three forms the world needs.

    apply_rule   - the SHIPPED, NULL-unsafe predicate. Drops harness rows AND
                   every legitimate row whose terminal_ref is NULL.
    drop_harness - the CORRECT exclusion. Drops harness rows only.
    Neither     - the pre-upgrade world, where no harness row exists to drop.
    """
    if apply_rule:
        where = f"WHERE {SHIPPED_PREDICATE}"
    elif drop_harness:
        where = ("WHERE terminal_ref IS DISTINCT FROM '" + EXCLUDED_REFS[0] + "'\n"
                 "  AND terminal_ref IS DISTINCT FROM '" + EXCLUDED_REFS[1] + "'")
    else:
        where = ""
    return f"""
SELECT payment_id, payment_dt, captured_at, merchant_id, channel, terminal_ref,
       card_last4, amount_gross, fee_amount, amount_net, status, batch_id,
       arrival_ts,
       arrival_ts + INTERVAL 40 MINUTE AS loaded_at
FROM {source}
{where}
"""


def kpi_sql(mart: str) -> str:
    return f"""
SELECT payment_dt AS dt,
       COUNT(*) AS payments,
       COUNT(DISTINCT merchant_id) AS merchants,
       CAST(SUM(amount_gross) AS DECIMAL(14,2)) AS gross_amount,
       CAST(SUM(fee_amount)   AS DECIMAL(14,2)) AS fee_amount,
       CAST(SUM(amount_net)   AS DECIMAL(14,2)) AS net_amount
FROM {mart}
GROUP BY 1
ORDER BY 1
"""


def merchant_monthly_sql(mart: str, dim: str | None = None) -> str:
    """The merchant rollup.

    With `dim`, this is the SHIPPED model as dbt materialised it on DIM_LOADED:
    an INNER join to the commercial list for the merchant name, which is
    DEFECT A - every merchant the list does not carry vanishes from the report.
    Without `dim`, it is the rollup GT006 shipped and the shape a correct
    repair produces on the business projection (month, merchant_id, payments,
    net_amount); merchant_name is not graded.
    """
    if dim is None:
        return f"""
SELECT strftime(payment_dt, '%Y-%m') AS month,
       merchant_id,
       COUNT(*) AS payments,
       CAST(SUM(amount_net) AS DECIMAL(14,2)) AS net_amount
FROM {mart}
GROUP BY 1, 2
ORDER BY 1, 2
"""
    return f"""
SELECT strftime(p.payment_dt, '%Y-%m') AS month,
       p.merchant_id,
       m.merchant_name,
       COUNT(*) AS payments,
       CAST(SUM(p.amount_net) AS DECIMAL(14,2)) AS net_amount
FROM {mart} p
JOIN {dim} m ON m.merchant_id = p.merchant_id
GROUP BY 1, 2, 3
ORDER BY 1, 2
"""


def merchant_rows() -> list[tuple[str, str, str]]:
    """The commercial team's list: (merchant_id, merchant_name, segment) for
    M0001..M{DIM_MERCHANTS}. Deterministic; no acquirer column, on purpose -
    which merchants sit on the legacy acquirer is defect B's territory and the
    list must not hint at it."""
    return [(f"M{i:04d}", MERCHANT_NAMES[i - 1],
             MERCHANT_SEGMENTS[(i * 7) % len(MERCHANT_SEGMENTS)])
            for i in range(1, DIM_MERCHANTS + 1)]


def merchants_csv() -> str:
    lines = ["merchant_id,merchant_name,segment"]
    lines += [",".join(r) for r in merchant_rows()]
    return "\n".join(lines) + "\n"


def dim_merchant_sql() -> str:
    values = ",\n".join(f"('{i}', '{n}', '{s}')" for i, n, s in merchant_rows())
    return f"""
CREATE TABLE {DIM_TABLE} AS
SELECT * FROM (VALUES
{values}) AS t(merchant_id, merchant_name, segment)
ORDER BY 1
"""


# ---------------------------------------------------------- green surfaces ---
def ops_sql() -> str:
    """ops.* - the monitoring this team actually has. All of it green.

    A003's premise is that the loss is invisible to every control that exists.
    So: freshness against the mart (green - the mart IS fresh, it is just short),
    not-null controls on columns that are populated (green), a row-count-greater
    -than-zero control (green), and a nightly job that has never failed.

    WHAT IS DELIBERATELY ABSENT, and named here so a reader can see it is a
    choice: there is no control that compares the mart against the landing
    stream it is built from, and no not-null control on terminal_ref. Either one
    would find the defect on the day it shipped, which is the point - the
    monitoring is asymmetric, and correctness is the axis nobody monitors.
    """
    return f"""
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE ops.job_runs AS
WITH d AS (
  SELECT UNNEST(generate_series(DATE '{HIST_START}' + INTERVAL 1 DAY,
                                DATE '{HIST_END}' + INTERVAL 1 DAY,
                                INTERVAL 1 DAY))::DATE AS run_dt
), j AS (
  SELECT * FROM (VALUES ('land_payments', 40, 130),
                        ('build_payments', 40, 205),
                        ('refresh_bi_extract', 0, 245)) AS t(job, ofs, base_s)
)
SELECT ROW_NUMBER() OVER (ORDER BY d.run_dt, j.job) AS run_id,
       j.job,
       d.run_dt - INTERVAL 1 DAY AS business_dt,
       d.run_dt + INTERVAL 2 HOUR + INTERVAL (j.ofs) MINUTE AS started_at,
       d.run_dt + INTERVAL 2 HOUR + INTERVAL (j.ofs) MINUTE
         + INTERVAL (j.base_s) SECOND AS ended_at,
       'success' AS status
FROM d CROSS JOIN j
ORDER BY 1;

CREATE TABLE ops.freshness_checks AS
SELECT ended_at AS checked_at,
       'warehouse.fct_payments' AS dataset,
       business_dt AS max_partition_dt,
       36 AS threshold_hours,
       'PASS' AS status
FROM ops.job_runs WHERE job = 'build_payments'
UNION ALL
SELECT ended_at, 'warehouse.kpi_payments_daily', business_dt, 36, 'PASS'
FROM ops.job_runs WHERE job = 'build_payments'
ORDER BY 1, 2;

CREATE TABLE ops.dq_checks AS
WITH r AS (SELECT ended_at, business_dt FROM ops.job_runs WHERE job = 'build_payments'),
     c AS (SELECT * FROM (VALUES
            ('not_null_payment_id'),
            ('not_null_payment_dt'),
            ('not_null_amount_net'),
            ('row_count_gt_zero')) AS t(check_name))
SELECT r.ended_at AS checked_at,
       'warehouse.fct_payments' AS dataset,
       c.check_name,
       'PASS' AS status,
       0 AS failed_rows
FROM r CROSS JOIN c
ORDER BY 1, 3;
"""


# ----------------------------------------------------------- world builder ---
def build_world(path: Path, seed: int) -> None:
    con = _create(path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute("CREATE SCHEMA IF NOT EXISTS warehouse")
        con.execute("CREATE SCHEMA IF NOT EXISTS reporting")
        con.execute(f"CREATE TABLE raw.payments_landing AS {landing_sql(seed)}")

        # The mart as the pipeline actually produced it, day by day:
        #   before the harness upgrade  - everything landed
        #   upgrade .. rule deploy      - everything landed, harness cleaned out
        #                                 by hand on the deploy day
        #   rule deploy .. today        - the NULL-unsafe rule ran every night
        con.execute(f"""
CREATE TABLE warehouse.fct_payments AS
{mart_select('raw.payments_landing', apply_rule=False, drop_harness=True)}
  AND payment_dt < DATE '{RULE_DEPLOY}'
""")
        con.execute(f"""
INSERT INTO warehouse.fct_payments
{mart_select('raw.payments_landing', apply_rule=True, drop_harness=False)}
  AND payment_dt >= DATE '{RULE_DEPLOY}'
""")
        con.execute(
            f"CREATE TABLE warehouse.kpi_payments_daily AS "
            f"{kpi_sql('warehouse.fct_payments')}")
        # DEFECT A. The commercial list, loaded once on DIM_LOADED, and the
        # rollup as the shipped dbt model materialises it: inner-joined to
        # that list, so M0031..M0040 are not in the report at all.
        con.execute(dim_merchant_sql())
        con.execute(
            f"CREATE TABLE {ROLLUP_TABLE} AS "
            f"{merchant_monthly_sql('warehouse.fct_payments', DIM_TABLE)}")
        # The BI extract the dashboard reads. A materialised copy of the KPI
        # table, one step downstream, so a repair that stops at the mart leaves
        # a visibly stale number in front of the business.
        con.execute("CREATE TABLE warehouse.bi_payments_extract AS "
                    "SELECT * FROM warehouse.kpi_payments_daily")
        for stmt in ops_sql().split(";\n"):
            if stmt.strip():
                con.execute(stmt)
    finally:
        _close(con)


def build_expected(out: Path, seed: int) -> None:
    """The harness-side reference: the mart as it WOULD be if the rule had only
    ever done what it was written to do.

    Built from raw.payments_landing by the CORRECT exclusion, over the whole
    history. Nothing else differs: same columns, same derivations, same order.
    """
    world = out / "world.duckdb"
    path = out / "expected.duckdb"
    for f in (path, Path(str(path) + ".wal")):
        f.unlink(missing_ok=True)
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute(f"ATTACH '{world.as_posix()}' AS src (READ_ONLY)")
        con.execute(f"ATTACH '{path.as_posix()}' AS ref")
        con.execute("USE ref")
        con.execute("CREATE SCHEMA IF NOT EXISTS warehouse")
        con.execute(
            f"CREATE TABLE warehouse.fct_payments AS "
            f"{mart_select('src.raw.payments_landing', apply_rule=False, drop_harness=True)}")
        con.execute(
            f"CREATE TABLE warehouse.kpi_payments_daily AS "
            f"{kpi_sql('ref.warehouse.fct_payments')}")
        con.execute("CHECKPOINT")
    finally:
        con.close()


# --------------------------------------------------------- the probe batch ---
PROBE_HEADER = ["payment_id", "payment_dt", "captured_at", "merchant_id",
                "channel", "terminal_ref", "card_last4", "amount_gross",
                "fee_amount", "amount_net", "status", "batch_id", "arrival_ts"]


def probe_rows() -> list[dict]:
    """One partition of payments the world has never seen.

    The grader appends it to a COPY of the pre-repair world and runs the agent's
    OWN nightly loader over it. That is the liveness clause: a rule that was
    deleted admits the harness rows, a rule that never runs admits nothing, and
    only a rule that is both live and precise admits exactly the 54 legitimate
    payments and none of the 6 harness rows.

    Deterministic and tiny: 60 rows, written out as one CSV so a human can read
    what the gate is actually asserting.
    """
    rows = []
    for i in range(1, PROBE_ORDINARY + 1):
        merchant = 10 + (i % 25)          # never <= LEGACY_ACQUIRER_MAX
        gross = round(12.00 + i * 3.17, 2)
        fee = round(gross * 0.0215 + 0.10, 2)
        rows.append({
            "payment_id": PROBE_ID_BASE + i,
            "payment_dt": PROBE_DATE,
            "captured_at": f"{PROBE_DATE} {8 + i % 12:02d}:{i % 60:02d}:00",
            "merchant_id": f"M{merchant:04d}",
            "channel": "card_present" if i % 2 else "ecommerce",
            "terminal_ref": (f"T{merchant * 7 + i % 3:05d}" if i % 2
                             else f"V{merchant:05d}"),
            "card_last4": f"{(i * 37) % 10000:04d}",
            "amount_gross": f"{gross:.2f}",
            "fee_amount": f"{fee:.2f}",
            "amount_net": f"{gross - fee:.2f}",
            "status": "settled",
            "batch_id": PROBE_BATCH,
            "arrival_ts": PROBE_ARRIVAL_TS,
            "_class": "ordinary",
        })
    for i in range(1, PROBE_NULL_REF + 1):
        merchant = 1 + (i % LEGACY_ACQUIRER_MAX)      # legacy acquirer
        gross = round(21.00 + i * 7.41, 2)
        fee = round(gross * 0.0215 + 0.10, 2)
        rows.append({
            "payment_id": PROBE_ID_BASE + 100 + i,
            "payment_dt": PROBE_DATE,
            "captured_at": f"{PROBE_DATE} {9 + i % 10:02d}:{(i * 7) % 60:02d}:00",
            "merchant_id": f"M{merchant:04d}",
            "channel": "ecommerce" if i % 2 else "moto",
            "terminal_ref": None,
            "card_last4": f"{(i * 53) % 10000:04d}",
            "amount_gross": f"{gross:.2f}",
            "fee_amount": f"{fee:.2f}",
            "amount_net": f"{gross - fee:.2f}",
            "status": "settled",
            "batch_id": PROBE_BATCH,
            "arrival_ts": PROBE_ARRIVAL_TS,
            "_class": "null_ref",
        })
    for i in range(1, PROBE_HARNESS + 1):
        rows.append({
            "payment_id": PROBE_ID_BASE + 200 + i,
            "payment_dt": PROBE_DATE,
            "captured_at": f"{PROBE_DATE} 03:{i:02d}:00",
            "merchant_id": "M0000",
            "channel": "card_present",
            "terminal_ref": EXCLUDED_REFS[0] if i % 2 else EXCLUDED_REFS[1],
            "card_last4": "0000",
            "amount_gross": "1.00",
            "fee_amount": "0.00",
            "amount_net": "1.00",
            "status": "settled",
            "batch_id": PROBE_BATCH,
            "arrival_ts": PROBE_ARRIVAL_TS,
            "_class": "harness",
        })
    return rows


def write_probe_batch(out: Path) -> dict:
    rows = probe_rows()
    lines = [",".join(PROBE_HEADER)]
    for r in rows:
        lines.append(",".join(
            "" if r[c] is None else str(r[c]) for c in PROBE_HEADER))
    body = "\n".join(lines) + "\n"
    _write_text(out / "probe_batch" / "payments.csv", body)
    manifest = {
        "partition": PROBE_DATE,
        "batch_id": PROBE_BATCH,
        "arrival_ts": PROBE_ARRIVAL_TS,
        "rows": len(rows),
        "must_be_admitted": sorted(r["payment_id"] for r in rows
                                   if r["_class"] != "harness"),
        "must_be_rejected": sorted(r["payment_id"] for r in rows
                                   if r["_class"] == "harness"),
        "null_ref_ids": sorted(r["payment_id"] for r in rows
                               if r["_class"] == "null_ref"),
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    _write_text(out / "probe_batch_manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


# ---------------------------------------------------------------- workspace ---
README = f"""# commercial-payments

The payments mart and the daily settlement KPI the finance and commercial teams
read. DuckDB warehouse, SQL pipelines for the load, dbt for the marts on top.

    var/warehouse/warehouse.duckdb      the warehouse
    pipelines/                          the nightly SQL
    dbt_project/                        the modelled layer
    config/checks.yml                   the nightly data-quality controls
    docs/                               runbooks and the data dictionary

## Layout

    raw.payments_landing        every settled payment the processor delivered.
                                SYSTEM OF RECORD. Never written by us.
    warehouse.fct_payments      one row per payment, built from the landing
                                stream by pipelines/build_payments.sql
    warehouse.kpi_payments_daily  the daily settlement KPI (dbt)
    warehouse.bi_payments_extract the materialised extract the dashboard reads
    reporting.rpt_merchant_monthly  merchant rollup (dbt); the statements pack
    reporting.dim_merchant      merchant names, from config/merchants.csv
    ops.*                       job runs, freshness and DQ check history

## Running anything by hand

    python scripts/warehouse.py inventory

opens the warehouse the way every script in this repo does: an in-memory
connection, `ATTACH '<path>' AS wh`, then `USE wh.warehouse`. Do not open the
file directly - the catalog would then be called `warehouse` too and every
two-part name becomes ambiguous.
"""

RUNBOOK = f"""# Runbook: nightly payments load

Runs at 02:40 every night, after the processor delivery lands at 02:10.

    pipelines/build_payments.sql     raw.payments_landing -> warehouse.fct_payments
    dbt run --select warehouse+      the marts on top

## Scope

`build_payments.sql` is incremental by partition. It takes every `payment_dt`
that has landed since the mart's own high-water mark, deletes those partitions
and re-inserts them. Re-running it on an unchanged stream is a no-op; re-running
it after a re-delivery restates exactly the partitions that were re-delivered.

## Restating an older range

`pipelines/backfill_payments.sql` does the same thing for a closed date range.
Edit the two dates at the top and run it. It was last used on {RULE_DEPLOY} to
clean the processor's test-harness rows out of {CLEANUP_FROM}..{CLEANUP_TO};
the dates in the file are still those.

## The exclusion rule

Since {RULE_DEPLOY} the load excludes the processor's test harness by
`terminal_ref`. See `config/checks.yml` and DATA-2071. The harness rows are not
payments and must not reach the mart or any number built on it.
"""

DATA_DICTIONARY = """# Data dictionary

## raw.payments_landing

| column | type | notes |
|---|---|---|
| payment_id | BIGINT | the processor's id. Unique across the feed. |
| payment_dt | DATE | settlement date; the partition column. |
| captured_at | TIMESTAMP | when the payment was taken. |
| merchant_id | VARCHAR | `M0001`..`M0040`. `M0000` is not a merchant. |
| channel | VARCHAR | `card_present`, `ecommerce`, `moto`. |
| terminal_ref | VARCHAR | the terminal that took the payment. **Nullable.** |
| card_last4 | VARCHAR | last four digits. |
| amount_gross | DECIMAL(12,2) | amount presented. |
| fee_amount | DECIMAL(12,2) | processor fee. |
| amount_net | DECIMAL(12,2) | gross - fee. Settled to the merchant. |
| status | VARCHAR | `settled`. The feed carries settled payments only. |
| batch_id | VARCHAR | `proc-YYYYMMDD`, one delivery per day. |
| arrival_ts | TIMESTAMP | when the delivery landed. |

### terminal_ref

Populated for every card-present payment. For card-not-present payments it
carries the merchant's virtual terminal id **if the acquirer issues one**. The
legacy acquirer does not, so a card-not-present payment on a legacy-acquirer
merchant has no terminal_ref. This has been true since the feed began and is
not an error.

The processor also uses this column to mark its own test traffic. Those rows
carry a reserved ref and a `merchant_id` of `M0000`.

## warehouse.fct_payments

The same grain as the landing stream, with `loaded_at` added. Built by
`pipelines/build_payments.sql`.
"""

CHECKS_YML = f"""# Nightly data-quality controls for the payments mart.
#
# Each control runs after pipelines/build_payments.sql and writes a row to
# ops.dq_checks. A control that fails raises the dq_failing alarm.

version: 1
dataset: warehouse.fct_payments

controls:
  - name: not_null_payment_id
    column: payment_id
    assert: not_null
  - name: not_null_payment_dt
    column: payment_dt
    assert: not_null
  - name: not_null_amount_net
    column: amount_net
    assert: not_null
  - name: row_count_gt_zero
    assert: row_count_gt
    value: 0

# Exclusions applied by the load itself, not by a control.
#
#   DATA-2071 ({RULE_DEPLOY}) - the processor's test harness started writing
#   into the feed with the {HARNESS_START} platform upgrade. The load excludes
#   it by terminal_ref:
#
#       {SHIPPED_PREDICATE}
#
#   The same predicate appears in pipelines/backfill_payments.sql.
exclusions:
  - id: DATA-2071
    applied_in: [pipelines/build_payments.sql, pipelines/backfill_payments.sql]
    column: terminal_ref
    values: [{EXCLUDED_REFS[0]}, {EXCLUDED_REFS[1]}]
"""

BUILD_PAYMENTS_SQL = f"""-- Nightly payments load: raw.payments_landing -> warehouse.fct_payments.
--
-- Incremental by partition. Everything that has landed since the mart's own
-- high-water mark is deleted and re-inserted, so a re-delivery restates exactly
-- the partitions that were re-delivered and an unchanged stream is a no-op.
--
-- Scheduled: 02:40 nightly (var/sched/state.json, job `build_payments`).

CREATE OR REPLACE TEMP TABLE _scope AS
SELECT DISTINCT l.payment_dt
FROM raw.payments_landing l
WHERE l.arrival_ts > COALESCE((SELECT max(arrival_ts) FROM warehouse.fct_payments),
                              TIMESTAMP '1970-01-01');

DELETE FROM warehouse.fct_payments
WHERE payment_dt IN (SELECT payment_dt FROM _scope);

INSERT INTO warehouse.fct_payments
SELECT l.payment_id, l.payment_dt, l.captured_at, l.merchant_id, l.channel,
       l.terminal_ref, l.card_last4, l.amount_gross, l.fee_amount, l.amount_net,
       l.status, l.batch_id, l.arrival_ts,
       l.arrival_ts + INTERVAL 40 MINUTE AS loaded_at
FROM raw.payments_landing l
WHERE l.payment_dt IN (SELECT payment_dt FROM _scope)
  -- DATA-2071: keep the processor's test harness out of the mart.
  AND l.{SHIPPED_PREDICATE};
"""

BACKFILL_PAYMENTS_SQL = f"""-- Restate a closed range of warehouse.fct_payments from the landing stream.
--
-- Edit the two dates and run. Last used {RULE_DEPLOY} to clear the processor's
-- test-harness rows out of {CLEANUP_FROM}..{CLEANUP_TO}, which is why those are
-- still the dates in the file.

DELETE FROM warehouse.fct_payments
WHERE payment_dt BETWEEN DATE '{CLEANUP_FROM}' AND DATE '{CLEANUP_TO}';

INSERT INTO warehouse.fct_payments
SELECT l.payment_id, l.payment_dt, l.captured_at, l.merchant_id, l.channel,
       l.terminal_ref, l.card_last4, l.amount_gross, l.fee_amount, l.amount_net,
       l.status, l.batch_id, l.arrival_ts,
       l.arrival_ts + INTERVAL 40 MINUTE AS loaded_at
FROM raw.payments_landing l
WHERE l.payment_dt BETWEEN DATE '{CLEANUP_FROM}' AND DATE '{CLEANUP_TO}'
  -- DATA-2071: keep the processor's test harness out of the mart.
  AND l.{SHIPPED_PREDICATE};
"""

REFRESH_BI_SQL = """-- Refresh the materialised extract the payments dashboard reads.
-- Runs after the marts, at 02:45.

CREATE OR REPLACE TABLE warehouse.bi_payments_extract AS
SELECT * FROM warehouse.kpi_payments_daily;
"""

PIPELINES_README = f"""# pipelines/

| file | what it does | scheduled |
|---|---|---|
| `build_payments.sql` | the nightly incremental load into `warehouse.fct_payments` | 02:40 |
| `backfill_payments.sql` | restate a closed date range of the same mart | no |
| `refresh_bi_extract.sql` | rebuild `warehouse.bi_payments_extract` | 02:45 |

The dbt project builds everything downstream of `warehouse.fct_payments`.

Both SQL loaders carry the DATA-2071 exclusion. If you change it, change it in
both - {RULE_DEPLOY}'s deploy only changed one of them at first and the backfill
put the harness rows straight back.
"""

DBT_PROJECT_YML = """name: commercial_payments
version: "1.0.0"
config-version: 2
profile: commercial_payments
model-paths: ["models"]
macro-paths: ["macros"]
analysis-paths: ["analyses"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]

models:
  commercial_payments:
    warehouse:
      +materialized: table
      +schema: warehouse
    reporting:
      +materialized: table
      +schema: reporting
"""

DBT_SOURCES_YML = """version: 2

sources:
  - name: warehouse
    schema: warehouse
    tables:
      - name: fct_payments
        description: >-
          One row per settled payment. Loaded by pipelines/build_payments.sql,
          not by dbt.
  - name: reporting
    schema: reporting
    tables:
      - name: dim_merchant
        description: >-
          Merchant names, from the commercial team's list
          (config/merchants.csv). Loaded by scripts/load_merchants.py, not by
          dbt.
"""

DBT_KPI_SQL = """{{ config(materialized='table', alias='kpi_payments_daily') }}

-- The daily settlement KPI. Everything the commercial pack counts comes from
-- here.

select
    payment_dt                                as dt,
    count(*)                                  as payments,
    count(distinct merchant_id)               as merchants,
    cast(sum(amount_gross) as decimal(14,2))  as gross_amount,
    cast(sum(fee_amount)   as decimal(14,2))  as fee_amount,
    cast(sum(amount_net)   as decimal(14,2))  as net_amount
from {{ source('warehouse', 'fct_payments') }}
group by 1
order by 1
"""

DBT_MERCHANT_SQL = """{{ config(materialized='table', alias='rpt_merchant_monthly') }}

-- The merchant statements pack. One row per merchant per calendar month.
-- Merchant names come from the commercial team's list (reporting.dim_merchant).

select
    strftime(p.payment_dt, '%Y-%m')            as month,
    p.merchant_id,
    m.merchant_name,
    count(*)                                   as payments,
    cast(sum(p.amount_net) as decimal(14,2))   as net_amount
from {{ source('warehouse', 'fct_payments') }} p
""" + SHIPPED_JOIN + """
  on m.merchant_id = p.merchant_id
group by 1, 2, 3
order by 1, 2
"""

DBT_SCHEMA_YML = """version: 2

models:
  - name: kpi_payments_daily
    description: Daily settlement KPI, one row per settlement date.
    columns:
      - name: dt
        tests: [not_null, unique]
      - name: payments
        tests: [not_null]
      - name: net_amount
        tests: [not_null]

  - name: rpt_merchant_monthly
    description: Merchant rollup by calendar month; the merchant statements pack.
    columns:
      - name: month
        tests: [not_null]
      - name: merchant_id
        tests: [not_null]
      - name: merchant_name
        description: From reporting.dim_merchant (config/merchants.csv).
"""

WAREHOUSE_PY = '''#!/usr/bin/env python3
"""Open the warehouse the way every script in this repo does.

    python scripts/warehouse.py inventory
    python scripts/warehouse.py sql "SELECT count(*) FROM warehouse.fct_payments"

Aliased ATTACH, never a direct open. Opening var/warehouse/warehouse.duckdb
directly names the catalog `warehouse` as well as the schema, and every two-part
reference then raises "Binder Error: Ambiguous reference".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb

DEFAULT = Path(os.environ.get("ALKERA_WAREHOUSE",
                              "/var/warehouse/warehouse.duckdb"))


def connect(db: Path | None = None, *, read_only: bool = True, alias: str = "wh"):
    path = Path(db or DEFAULT)
    con = duckdb.connect(":memory:")
    ro = " (READ_ONLY)" if read_only else ""
    con.execute(f"ATTACH '{path.as_posix()}' AS {alias}{ro}")
    con.execute(f"USE {alias}.warehouse")
    return con


def inventory(con, alias: str = "wh") -> list:
    return con.execute(
        "SELECT schema_name, table_name, estimated_size FROM duckdb_tables() "
        f"WHERE database_name = '{alias}' ORDER BY 1, 2").fetchall()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "inventory":
        con = connect()
        for schema, table, rows in inventory(con):
            print(f"{schema}.{table:<28} {rows:>10,}")
        return 0
    if argv[0] == "sql" and len(argv) > 1:
        con = connect()
        for row in con.execute(argv[1]).fetchall():
            print(row)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''

LOAD_MERCHANTS_PY = '''#!/usr/bin/env python3
"""Load the commercial team's merchant list into reporting.dim_merchant.

    python scripts/load_merchants.py [config/merchants.csv]

Replaces the table with the contents of the CSV. Run it whenever the list
changes; nothing runs it on a schedule.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from warehouse import connect          # noqa: E402

DEFAULT_CSV = Path(__file__).resolve().parent.parent / "config" / "merchants.csv"


def main() -> int:
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    con = connect(read_only=False)
    con.execute("CREATE SCHEMA IF NOT EXISTS wh.reporting")
    con.execute(f"""
        CREATE OR REPLACE TABLE wh.reporting.dim_merchant AS
        SELECT merchant_id, merchant_name, segment
        FROM read_csv_auto('{csv_path.as_posix()}', header = true)
        ORDER BY merchant_id
    """)
    n = con.execute("SELECT count(*) FROM wh.reporting.dim_merchant").fetchone()[0]
    print(f"reporting.dim_merchant: {n} merchants from {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

CHECK_PIPELINE_PY = '''#!/usr/bin/env python3
"""Does the mart tie to the stream it is built from?

    python scripts/check_pipeline.py

Prints, for every partition, the rows in raw.payments_landing and the rows in
warehouse.fct_payments. It does NOT know what the difference should be - the
load excludes some rows on purpose - so it reports and never judges.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from warehouse import connect          # noqa: E402


def main() -> int:
    con = connect()
    rows = con.execute("""
        SELECT l.payment_dt,
               count(*) AS landed,
               (SELECT count(*) FROM wh.warehouse.fct_payments f
                 WHERE f.payment_dt = l.payment_dt) AS in_mart
        FROM wh.raw.payments_landing l
        GROUP BY 1 ORDER BY 1
    """).fetchall()
    print(f"{'partition':<12} {'landed':>8} {'in mart':>8} {'diff':>8}")
    for dt, landed, in_mart in rows:
        print(f"{str(dt):<12} {landed:>8,} {in_mart:>8,} {landed - in_mart:>8,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

CHANGELOG = f"""# Changelog

## {RULE_DEPLOY}
- DATA-2071: exclude the processor's test-harness traffic from the payments
  load. Cleared the rows that had already reached the mart
  ({CLEANUP_FROM}..{CLEANUP_TO}) with `pipelines/backfill_payments.sql`.

## {HARNESS_START}
- Processor platform upgrade. New delivery format, same columns.

## {DIM_LOADED}
- `rpt_merchant_monthly` moved into dbt from the old reporting view. Merchant
  names now come from the commercial team's list, `config/merchants.csv`,
  loaded into `reporting.dim_merchant` by `scripts/load_merchants.py`.

## 2025-09-15
- First delivery from the new processor. Payments mart live.
"""

GITIGNORE = "target/\ndbt_packages/\nlogs/\n*.duckdb.wal\n__pycache__/\n"

REQUIREMENTS = "duckdb==1.5.5\ndbt-core==1.8.7\ndbt-duckdb==1.8.4\npyyaml==6.0.2\n"

RUN_SH = """#!/usr/bin/env bash
# The nightly, end to end. What the scheduler runs.
set -euo pipefail
python scripts/warehouse.py sql "$(cat pipelines/build_payments.sql)"
dbt run --select warehouse+ --project-dir dbt_project
python scripts/warehouse.py sql "$(cat pipelines/refresh_bi_extract.sql)"
"""

# The schema is the gateway's own: alkera_gateway/surfaces.py:326-351 forbids
# extra keys, so `id`, `kind`, `description` and `reads` are not fields and a
# dashboard carrying them loads as `invalid_dashboard` and the whole BI surface
# reads DEAD. Measured: bi_list and bi_render both came back
# invalid_dashboard against the first draft of this file.
#
# EVERY PANEL READS THE MATERIALISED EXTRACT, never the mart and never the KPI
# table. That is the design: the extract is one refresh behind, so a repair that
# stops at the mart leaves the wrong number in front of the business.
# fixture_contract._exposure_checks refuses a dashboard that reaches past it.
DASHBOARD_YML = f"""version: 1
dashboard: {DASHBOARD_ID}
title: Payments - daily settlement
owner: Payments Operations
panels:
  - name: payments_per_day
    title: Payments settled per day
    sql: >-
      SELECT dt, payments FROM warehouse.bi_payments_extract
      ORDER BY dt DESC LIMIT 60
  - name: net_per_day
    title: Net settled per day
    sql: >-
      SELECT dt, net_amount FROM warehouse.bi_payments_extract
      ORDER BY dt DESC LIMIT 60
  - name: last_30
    title: Last 30 days
    sql: >-
      SELECT dt, payments, gross_amount, fee_amount, net_amount
      FROM warehouse.bi_payments_extract ORDER BY dt DESC LIMIT 30
"""

WORKSPACE_FILES = {
    "README.md": README,
    ".gitignore": GITIGNORE,
    "requirements.txt": REQUIREMENTS,
    "run.sh": RUN_SH,
    "CHANGELOG.md": CHANGELOG,
    "config/checks.yml": CHECKS_YML,
    "pipelines/README.md": PIPELINES_README,
    "pipelines/build_payments.sql": BUILD_PAYMENTS_SQL,
    "pipelines/backfill_payments.sql": BACKFILL_PAYMENTS_SQL,
    "pipelines/refresh_bi_extract.sql": REFRESH_BI_SQL,
    "docs/runbook_payments_load.md": RUNBOOK,
    "docs/data_dictionary.md": DATA_DICTIONARY,
    "dbt_project/dbt_project.yml": DBT_PROJECT_YML,
    "dbt_project/models/sources.yml": DBT_SOURCES_YML,
    "dbt_project/models/warehouse/kpi_payments_daily.sql": DBT_KPI_SQL,
    "dbt_project/models/warehouse/schema.yml": DBT_SCHEMA_YML,
    "dbt_project/models/reporting/rpt_merchant_monthly.sql": DBT_MERCHANT_SQL,
    "scripts/warehouse.py": WAREHOUSE_PY,
    "scripts/check_pipeline.py": CHECK_PIPELINE_PY,
    # DEFECT A's two artefacts: the commercial list and the script that loads it.
    "config/merchants.csv": merchants_csv(),
    "scripts/load_merchants.py": LOAD_MERCHANTS_PY,
}


def write_workspace(out: Path) -> dict:
    ws = out / "workspace"
    if ws.exists():
        shutil.rmtree(ws)
    for rel, body in WORKSPACE_FILES.items():
        _write_text(ws / rel, body)
    _write_text(out / "bi" / "dashboards" / f"{DASHBOARD_ID}.yml", DASHBOARD_YML)
    return {rel: hashlib.sha256((ws / rel).read_bytes()).hexdigest()
            for rel in sorted(WORKSPACE_FILES)}


# ------------------------------------------------- staging: world + grading ---
def scheduler_state(vnow: str = VNOW) -> dict:
    """var/sched/state.json.

    Every job carries an `exec` block naming the SAME pipeline file a human
    would run by hand, so triggering the job and running the file cannot
    diverge. Without one, `world.executor.execute` answers `unsupported` and the
    agent cannot re-run its own pipeline through the scheduler - the single
    thing a data engineer does most.
    """
    return {
        "vnow": vnow,
        "jobs": {
            "land_payments": {
                "command": "land_payments", "duration_s": 130, "time": "02:10",
                "exec": {"kind": "noop", "detail": "processor delivery"}},
            "build_payments": {
                "command": "build_payments", "duration_s": 205, "time": "02:40",
                "exec": {"kind": "sql", "script": "pipelines/build_payments.sql",
                         "detail": "build_payments"}},
            "refresh_bi_extract": {
                "command": "refresh_bi_extract", "duration_s": 245, "time": "02:45",
                "exec": {"kind": "sql", "script": "pipelines/refresh_bi_extract.sql",
                         "detail": "refresh_bi_extract"}},
        },
        "runs": [
            {"job": "land_payments", "vts_start": "2026-03-15T02:10:00Z",
             "vts_end": "2026-03-15T02:12:10Z", "status": "success",
             "detail": f"dt={HIST_END}"},
            {"job": "build_payments", "vts_start": "2026-03-15T02:40:00Z",
             "vts_end": "2026-03-15T02:43:25Z", "status": "success",
             "detail": f"dt={HIST_END}"},
            {"job": "refresh_bi_extract", "vts_start": "2026-03-15T02:45:00Z",
             "vts_end": "2026-03-15T02:49:05Z", "status": "success",
             "detail": DASHBOARD_ID},
            {"job": "build_payments", "vts_start": f"{RULE_DEPLOY}T09:12:00Z",
             "vts_end": f"{RULE_DEPLOY}T09:15:40Z", "status": "success",
             "detail": f"backfill_payments {CLEANUP_FROM}..{CLEANUP_TO}"},
        ],
    }


def live_state() -> dict:
    """var/world/live.json - where `scheduler._execute` reads a job's work.

    state.json carries the SCHEDULE; live.json carries the WORK. Without this
    file every trigger comes back `declares no executable work`.
    """
    return {
        "jobs": {
            "land_payments": {"exec": {"kind": "noop",
                                       "detail": "processor delivery"}},
            "build_payments": {
                "exec": {"kind": "sql", "script": "pipelines/build_payments.sql",
                         "detail": "build_payments"}},
            "refresh_bi_extract": {
                "exec": {"kind": "sql", "script": "pipelines/refresh_bi_extract.sql",
                         "detail": "refresh_bi_extract"}},
        },
    }


def stage_world_root(build: Path, root: Path) -> dict:
    """Lay the built fixture out as a world root.

    The warehouse lands at var/warehouse/warehouse.duckdb - the CONTRACT
    filename, where the duckdb catalog and the `warehouse` schema collide - so
    the agent's own two-part references run under exactly the condition the
    deployed world has. Staging it anywhere else is how a suite prints TASK
    VALID with an oracle that aborts on statement one.
    """
    build, root = Path(build), Path(root)
    (root / "var" / "warehouse").mkdir(parents=True, exist_ok=True)
    (root / "var" / "sched").mkdir(parents=True, exist_ok=True)
    wh = root / "var" / "warehouse" / "warehouse.duckdb"
    shutil.copy(build / "world.duckdb", wh)
    Path(str(wh) + ".wal").unlink(missing_ok=True)
    for src, dst in (("workspace", root / "workspace"),
                     ("bi", root / "var" / "bi")):
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(build / src, dst)
    _write_text(root / "var" / "sched" / "state.json",
                json.dumps(scheduler_state(), indent=2, sort_keys=True) + "\n")
    (root / "var" / "world").mkdir(parents=True, exist_ok=True)
    _write_text(root / "var" / "world" / "live.json",
                json.dumps(live_state(), indent=2, sort_keys=True) + "\n")
    return {"world_root": str(root), "warehouse": str(wh),
            "workspace": str(root / "workspace"),
            "dashboards": str(root / "var" / "bi" / "dashboards"),
            "scheduler": str(root / "var" / "sched" / "state.json"),
            "virtual_time": VNOW}


def write_grading_zone(build: Path, world_root: Path, grading: Path,
                       snapshot_fn=None) -> dict:
    """Everything the grader reads, in a directory the episode cannot reach.

    F121. `grading` MUST NOT be inside `world_root`; this refuses otherwise
    rather than trusting the caller. An agent that can read the reference copies
    instead of diagnosing, and the unit is dead.
    """
    build, world_root, grading = Path(build), Path(world_root), Path(grading)
    if grading.resolve() == world_root.resolve() or \
            grading.resolve().is_relative_to(world_root.resolve()):
        raise SystemExit(
            "F121: the grading zone must not live inside the world root "
            f"({grading} is inside {world_root})")
    grading.mkdir(parents=True, exist_ok=True)
    shutil.copy(build / "expected.duckdb", grading / "expected.duckdb")
    shutil.copy(build / "probe_batch_manifest.json",
                grading / "probe_batch_manifest.json")
    for name in ("probe_batch", "pre_repair_world"):
        dst = grading / name
        if dst.exists():
            shutil.rmtree(dst)
    shutil.copytree(build / "probe_batch", grading / "probe_batch")
    # A COPY of the pristine pre-episode world, not a reconstruction. G1c runs
    # the agent's repaired loader against this, so it must be the world as it
    # was planted, not the world as the contract describes it.
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
        "probe_batch": str(grading / "probe_batch"),
    }


def _import_collateral_snapshot():
    import sys
    tests = Path(__file__).resolve().parent.parent / "tests"
    if str(tests) not in sys.path:
        sys.path.insert(0, str(tests))
    from collateral import snapshot                 # noqa: E402
    return snapshot


# --------------------------------------------------------------- measuring ---
def measure(out: Path) -> dict:
    """Read every number the contract publishes. Measured, never asserted."""
    con = _readonly(out / "world.duckdb", "wh")
    try:
        con.execute(f"ATTACH '{(out / 'expected.duckdb').as_posix()}' AS ref (READ_ONLY)")

        def one(sql: str):
            return con.execute(sql).fetchone()[0]

        biz = ", ".join(BUSINESS_COLS["fct_payments"])
        kbiz = ", ".join(BUSINESS_COLS["kpi_payments_daily"])
        excl = ", ".join(f"'{r}'" for r in EXCLUDED_REFS)

        stats = {
            "world": {
                "landing_rows": one("SELECT count(*) FROM wh.raw.payments_landing"),
                "landing_harness_rows": one(
                    f"SELECT count(*) FROM wh.raw.payments_landing "
                    f"WHERE terminal_ref IN ({excl})"),
                "landing_null_ref_rows": one(
                    "SELECT count(*) FROM wh.raw.payments_landing "
                    "WHERE terminal_ref IS NULL"),
                "fct_payments_rows": one("SELECT count(*) FROM wh.warehouse.fct_payments"),
                "fct_null_ref_rows": one(
                    "SELECT count(*) FROM wh.warehouse.fct_payments "
                    "WHERE terminal_ref IS NULL"),
                "fct_harness_rows": one(
                    f"SELECT count(*) FROM wh.warehouse.fct_payments "
                    f"WHERE terminal_ref IN ({excl})"),
                "duplicate_payment_ids": one(
                    "SELECT count(*) FROM (SELECT payment_id FROM "
                    "wh.warehouse.fct_payments GROUP BY 1 HAVING count(*) > 1)"),
                "kpi_rows": one("SELECT count(*) FROM wh.warehouse.kpi_payments_daily"),
                "max_payment_dt": str(one(
                    "SELECT max(payment_dt) FROM wh.warehouse.fct_payments")),
                "job_runs": one("SELECT count(*) FROM wh.ops.job_runs"),
                "job_runs_not_success": one(
                    "SELECT count(*) FROM wh.ops.job_runs WHERE status <> 'success'"),
                "freshness_not_pass": one(
                    "SELECT count(*) FROM wh.ops.freshness_checks WHERE status <> 'PASS'"),
                "dq_not_pass": one(
                    "SELECT count(*) FROM wh.ops.dq_checks WHERE status <> 'PASS'"),
                "dq_failed_rows": one(
                    "SELECT coalesce(sum(failed_rows), 0) FROM wh.ops.dq_checks"),
                "merchant_monthly_rows": one(
                    "SELECT count(*) FROM wh.reporting.rpt_merchant_monthly"),
                "dim_merchant_rows": one(
                    "SELECT count(*) FROM wh.reporting.dim_merchant"),
                "bi_extract_rows": one(
                    "SELECT count(*) FROM wh.warehouse.bi_payments_extract"),
            },
            "expected": {
                "fct_payments_rows": one("SELECT count(*) FROM ref.warehouse.fct_payments"),
                "fct_null_ref_rows": one(
                    "SELECT count(*) FROM ref.warehouse.fct_payments "
                    "WHERE terminal_ref IS NULL"),
                "fct_harness_rows": one(
                    f"SELECT count(*) FROM ref.warehouse.fct_payments "
                    f"WHERE terminal_ref IN ({excl})"),
                "kpi_rows": one("SELECT count(*) FROM ref.warehouse.kpi_payments_daily"),
            },
        }
        # The defect, both directions, on the business projection.
        stats["defect"] = {
            "dropped_rows": one(
                f"SELECT count(*) FROM (SELECT {biz} FROM ref.warehouse.fct_payments "
                f"EXCEPT ALL SELECT {biz} FROM wh.warehouse.fct_payments)"),
            "extra_rows": one(
                f"SELECT count(*) FROM (SELECT {biz} FROM wh.warehouse.fct_payments "
                f"EXCEPT ALL SELECT {biz} FROM ref.warehouse.fct_payments)"),
            "damaged_partitions": one(
                "SELECT count(*) FROM ref.warehouse.kpi_payments_daily r "
                "JOIN wh.warehouse.kpi_payments_daily w USING (dt) "
                "WHERE w.payments <> r.payments"),
            "kpi_days_differing": one(
                f"SELECT count(*) FROM (SELECT {kbiz} FROM ref.warehouse.kpi_payments_daily "
                f"EXCEPT ALL SELECT {kbiz} FROM wh.warehouse.kpi_payments_daily)"),
            "window_start": str(one(
                "SELECT min(payment_dt) FROM (SELECT payment_dt FROM "
                "ref.warehouse.fct_payments EXCEPT ALL SELECT payment_dt FROM "
                "wh.warehouse.fct_payments)")),
            "window_end": str(one(
                "SELECT max(payment_dt) FROM (SELECT payment_dt FROM "
                "ref.warehouse.fct_payments EXCEPT ALL SELECT payment_dt FROM "
                "wh.warehouse.fct_payments)")),
            "net_amount_short": str(one(
                "SELECT CAST(SUM(r.net_amount) - SUM(w.net_amount) AS DECIMAL(16,2)) "
                "FROM ref.warehouse.kpi_payments_daily r "
                "JOIN wh.warehouse.kpi_payments_daily w USING (dt)")),
        }
        # DEFECT A, measured. The rollup of the mart AS IT STANDS (B-broken) on
        # the business projection, minus what the report carries. A is graded
        # against the agent's own mart, never against the reference, so this
        # number is a property of the join and nothing else.
        rollup = merchant_monthly_sql("wh.warehouse.fct_payments")
        stats["stack"] = {
            "mart_merchants": one(
                "SELECT count(DISTINCT merchant_id) FROM wh.warehouse.fct_payments"),
            "rollup_merchants": one(
                "SELECT count(DISTINCT merchant_id) FROM wh.reporting.rpt_merchant_monthly"),
            "missing_merchants": one(
                "SELECT count(*) FROM (SELECT DISTINCT merchant_id FROM "
                "wh.warehouse.fct_payments EXCEPT SELECT merchant_id FROM "
                "wh.reporting.dim_merchant)"),
            "first_missing_merchant": str(one(
                "SELECT min(merchant_id) FROM (SELECT DISTINCT merchant_id FROM "
                "wh.warehouse.fct_payments EXCEPT SELECT merchant_id FROM "
                "wh.reporting.dim_merchant)")),
            "missing_merchant_months": one(
                f"SELECT count(*) FROM ({rollup}) r WHERE NOT EXISTS ("
                f"SELECT 1 FROM wh.reporting.rpt_merchant_monthly x "
                f"WHERE x.month = r.month AND x.merchant_id = r.merchant_id)"),
            # Rows for merchants the list DOES carry must tie to the mart
            # exactly, or the rollup has a second defect.
            "present_rows_differing": one(
                f"SELECT count(*) FROM (SELECT month, merchant_id, payments, net_amount "
                f"FROM wh.reporting.rpt_merchant_monthly EXCEPT ALL "
                f"SELECT month, merchant_id, payments, net_amount FROM ({rollup}))"),
            "months": one(
                "SELECT count(DISTINCT strftime(payment_dt, '%Y-%m')) "
                "FROM wh.warehouse.fct_payments"),
            # THE BAIT'S REFUTATION. Merchant-months that ARE in the report and
            # are short against the feed net of harness rows: B's merchants,
            # every month since the rule went in. Must be > 0 or "the missing
            # merchants explain the reconciliation gap" would be true.
            "present_merchant_months_short": one(f"""
                SELECT count(*) FROM (
                  SELECT strftime(l.payment_dt, '%Y-%m') AS month, l.merchant_id,
                         count(*) AS landed
                  FROM wh.raw.payments_landing l
                  WHERE l.terminal_ref IS DISTINCT FROM '{EXCLUDED_REFS[0]}'
                    AND l.terminal_ref IS DISTINCT FROM '{EXCLUDED_REFS[1]}'
                  GROUP BY 1, 2) f
                JOIN wh.reporting.rpt_merchant_monthly r
                  ON r.month = f.month AND r.merchant_id = f.merchant_id
                WHERE r.payments < f.landed"""),
            # Overlap between A's merchants and B's merchants. MUST be zero.
            "merchants_in_both_defects": one(
                "SELECT count(*) FROM (SELECT DISTINCT merchant_id FROM "
                "wh.warehouse.fct_payments EXCEPT SELECT merchant_id FROM "
                "wh.reporting.dim_merchant) a WHERE a.merchant_id IN ("
                "SELECT DISTINCT merchant_id FROM wh.raw.payments_landing "
                "WHERE terminal_ref IS NULL)"),
        }
        # The symptom, as an agent would first see it: survival rate per day.
        pre = one(
            f"SELECT ROUND(100.0 * (SELECT count(*) FROM wh.warehouse.fct_payments "
            f"WHERE payment_dt < DATE '{RULE_DEPLOY}') / (SELECT count(*) FROM "
            f"wh.raw.payments_landing WHERE payment_dt < DATE '{RULE_DEPLOY}' "
            f"AND terminal_ref IS DISTINCT FROM '{EXCLUDED_REFS[0]}' "
            f"AND terminal_ref IS DISTINCT FROM '{EXCLUDED_REFS[1]}'), 3)")
        post = one(
            f"SELECT ROUND(100.0 * (SELECT count(*) FROM wh.warehouse.fct_payments "
            f"WHERE payment_dt >= DATE '{RULE_DEPLOY}') / (SELECT count(*) FROM "
            f"wh.raw.payments_landing WHERE payment_dt >= DATE '{RULE_DEPLOY}' "
            f"AND terminal_ref IS DISTINCT FROM '{EXCLUDED_REFS[0]}' "
            f"AND terminal_ref IS DISTINCT FROM '{EXCLUDED_REFS[1]}'), 3)")
        loss = con.execute(f"""
            SELECT min(pct), max(pct), ROUND(avg(pct), 3) FROM (
              SELECT l.payment_dt,
                     ROUND(100.0 * (count(*) FILTER (WHERE l.terminal_ref IS NULL))
                           / count(*), 3) AS pct
              FROM wh.raw.payments_landing l
              WHERE l.payment_dt >= DATE '{RULE_DEPLOY}'
                AND l.terminal_ref IS DISTINCT FROM '{EXCLUDED_REFS[0]}'
                AND l.terminal_ref IS DISTINCT FROM '{EXCLUDED_REFS[1]}'
              GROUP BY 1)""").fetchone()
        vol = con.execute(f"""
            SELECT min(c), max(c) FROM (
              SELECT count(*) AS c FROM wh.raw.payments_landing
              WHERE terminal_ref IS DISTINCT FROM '{EXCLUDED_REFS[0]}'
                AND terminal_ref IS DISTINCT FROM '{EXCLUDED_REFS[1]}'
              GROUP BY payment_dt)""").fetchone()
        stats["symptom"] = {
            "survival_pct_before_rule": float(pre),
            "survival_pct_after_rule": float(post),
            "daily_loss_pct_min": float(loss[0]),
            "daily_loss_pct_max": float(loss[1]),
            "daily_loss_pct_mean": float(loss[2]),
            "daily_legit_volume_min": int(vol[0]),
            "daily_legit_volume_max": int(vol[1]),
            "daily_volume_swing_pct": round(100.0 * (vol[1] - vol[0]) / vol[0], 3),
        }
        return stats
    finally:
        con.close()


# -------------------------------------------------------------------- main ---
def generate(out: Path, seed: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    build_world(out / "world.duckdb", seed)
    build_expected(out, seed)
    probe = write_probe_batch(out)
    ws = write_workspace(out)
    stats = measure(out)
    stats["probe_batch"] = probe
    stats["workspace_sha256"] = ws
    return stats


def main_build(out: Path, seed: int) -> dict:
    """Build the fixture without printing. The entrypoint tests/build_world.py
    calls, so a rollout never depends on argv parsing or on stdout."""
    return generate(Path(out), int(seed))


def tree_hashes(root: Path, text_only: bool = True) -> dict:
    h = {}
    for p in sorted(root.rglob("*"), key=_contract_order):
        if p.is_file() and not (text_only and p.suffix == ".duckdb"):
            h[p.relative_to(root).as_posix()] = hashlib.sha256(
                p.read_bytes()).hexdigest()
    return h


def content_digest(db: Path) -> dict:
    """Order-insensitive multiset hash of every table in a database.

    md5 per row, then md5 over the row hashes sorted - invariant to physical row
    order (a repair may rewrite a table in any order) but NOT to multiplicity,
    which matters: a mart short 3,000 rows must not digest the same as a
    repaired one.
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
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--verify-twice", action="store_true",
                    help="build a second copy into a temp dir and diff every byte")
    ap.add_argument("--json", action="store_true", help="dump measured stats")
    a = ap.parse_args()

    out = Path(a.out).resolve()
    stats = generate(out, a.seed)
    if a.json:
        print(json.dumps(stats, indent=2, sort_keys=True))
        return 0

    w, e, d, s = stats["world"], stats["expected"], stats["defect"], stats["symptom"]
    print(f"  landing          {w['landing_rows']:>9,} rows "
          f"({w['landing_harness_rows']:,} harness, "
          f"{w['landing_null_ref_rows']:,} with a NULL terminal_ref)")
    print(f"  fct_payments     {w['fct_payments_rows']:>9,} rows   "
          f"reference {e['fct_payments_rows']:,}")
    print(f"  kpi_daily        {w['kpi_rows']:>9,} rows   "
          f"reference {e['kpi_rows']:,}")
    print()
    print(f"  THE DEFECT   rows the rule silently dropped : {d['dropped_rows']:>7,}")
    print(f"               rows present that should not be: {d['extra_rows']:>7,}"
          f"   <- zero: this is LOSS, not gain")
    print(f"               duplicate payment_ids in mart  : "
          f"{w['duplicate_payment_ids']:>7,}   <- zero: NOT a doubling")
    print(f"               window {d['window_start']} .. {d['window_end']}")
    print(f"               kpi days differing             : {d['kpi_days_differing']:>7,}")
    print(f"               net settled under-reported     : {d['net_amount_short']}")
    print()
    print(f"  INVISIBILITY survival before the rule : {s['survival_pct_before_rule']}%")
    print(f"               survival after the rule  : {s['survival_pct_after_rule']}%")
    print(f"               daily loss   {s['daily_loss_pct_min']}% .. "
          f"{s['daily_loss_pct_max']}% (mean {s['daily_loss_pct_mean']}%)")
    print(f"               daily volume swings {s['daily_volume_swing_pct']}% "
          f"({s['daily_legit_volume_min']:,}..{s['daily_legit_volume_max']:,}/day)"
          f"  <- the loss hides inside this")
    print(f"  GREEN        job runs {w['job_runs']:,}, not-success "
          f"{w['job_runs_not_success']} | freshness not-PASS "
          f"{w['freshness_not_pass']} | dq not-PASS {w['dq_not_pass']} "
          f"| dq failed rows {w['dq_failed_rows']}")
    k = stats["stack"]
    print(f"  DEFECT A     merchants in mart {k['mart_merchants']}, in the "
          f"report {k['rollup_merchants']}, not in the commercial list "
          f"{k['missing_merchants']} (from {k['first_missing_merchant']})")
    print(f"               merchant-months missing from the report: "
          f"{k['missing_merchant_months']}; present rows differing from the "
          f"mart: {k['present_rows_differing']}; merchants touched by BOTH "
          f"defects: {k['merchants_in_both_defects']}   <- zero: A and B are disjoint")
    p = stats["probe_batch"]
    print(f"  G1c PROBE    {p['rows']} rows at {p['partition']}: "
          f"{len(p['must_be_admitted'])} must be admitted "
          f"({len(p['null_ref_ids'])} of them NULL-ref), "
          f"{len(p['must_be_rejected'])} must be rejected")

    if a.verify_twice:
        tmp = Path(tempfile.mkdtemp(prefix="gt006-det-"))
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
                        print(f"      DIFFERS {t}")
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
