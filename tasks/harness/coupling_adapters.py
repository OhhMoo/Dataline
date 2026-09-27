#!/usr/bin/env python3
"""Per-task adapters for the layer-3 coupling filter (tasks/harness/coupling.py).

WHY AN ADAPTER AND NOT A BUILDER FLAG
-------------------------------------
The coupling test needs three worlds per ordered pair -- base, base+B and
base+A+B -- and no shipped fixture builder takes a stressor subset. GT004's
`fixtures/make_fixture_world.py` builds one world with every defect planted.
Rather than edit a builder that model rollouts are running against, an adapter
*composes* the subset by calling the task's own SQL-builder functions and module
constants. Every row of every subset world therefore comes from the task's own
generator; the adapter only chooses which planting steps run.

    subset {}                         -> the reference world (both defects absent)
    subset {retry_backfill_doubling}  -> fct_orders gets the UNION ALL retry copy
    subset {stale_definition_layout}  -> fct_revenue built with stale=True
    subset {late_arrival}             -> the late payload appended to raw.orders_landing

The per-stressor repairs are transcriptions of the corresponding step of
`solution/solution.sh` (the task's own oracle), reduced to the one defect. The
shipped oracle repairs both defects in a fixed order; the coupling test needs
each half on its own, which is exactly the thing the filter measures.

ADAPTER PROTOCOL (duck-typed, see coupling.py):
    .task              task directory name
    .stressors         list[str]
    .canonical_tables  {table: (keycols, cols)}
    .build(subset, dest) -> Path       build a world, return the .duckdb path
    .repair(name, db)                  apply one stressor's oracle repair in place
    .clauses(name, db) -> list[dict]   evaluate that stressor's contract clauses

CLOCKED ADAPTERS add four more, and `coupling.py --clock` needs all four:
    .clocked           True
    .world(subset, dest) -> Path       build a WORLD ROOT (var/ + workspace/)
    .warehouse(root) -> Path           that world's primary duckdb
    .advance(root)                     move the clock over the planted events,
                                       executing the world's own jobs for real

The split matters. `build` stages a stressor as STATE - rows already in a
landing table, a file already in a feed - which is all a t0 comparison can
express. `world` + `advance` stages it as an EVENT: the repair lands first, and
then the thing happens. docs/implementation_plan/harness/COUPLING.md's limitation 2 is exactly the gap between
those two stagings, and the two GT004 verdicts below are what it costs.

NEW FILE. Edits nothing under tasks/GT*/, world/, gateway/ or the rest of
tasks/harness/.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]


def _load_module(path: Path, name: str):
    """Import a fixture module by path without adding it to the package tree."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _connect(path: Path, alias: str = "wh"):
    """Aliased ATTACH, never a direct open (GT004 DESIGN.md, F622)."""
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + Path(path).as_posix() + "' AS " + alias)
    con.execute("USE " + alias)
    return con


def _close(con) -> None:
    con.execute("CHECKPOINT")
    con.close()


# ===========================================================================
# GT004 -- doubling window
# ===========================================================================
class GT004Adapter:
    task = "GT004-doubling-window"

    # `synthetic_null_unsafe_exclusion` is NOT GT004 fixture code. It is a
    # deliberately disconnected negative control modelled on GT006's mechanism
    # (a NULL-unsafe NOT IN exclusion list), planted on a reporting table fed
    # from the frozen raw.orders_snapshot so that nothing else in this world
    # reads or writes it. It exists to prove the filter can say "no".
    stressors = [
        "retry_backfill_doubling",
        "stale_definition_layout",
        "late_arrival",
        "synthetic_null_unsafe_exclusion",
    ]
    synthetic = {"synthetic_null_unsafe_exclusion"}

    # A defect's clauses hold on a world where it was never planted. An EVENT's
    # do not: "the late lines are present exactly once" is false on a world
    # where they never arrived, because the obligation does not exist there.
    # selfcheck() needs the distinction; the coupling verdict never does -- its
    # baseline is always base+{b}, the world where B's obligation is live.
    kinds = {
        "retry_backfill_doubling": "defect",
        "stale_definition_layout": "defect",
        "late_arrival": "event",
        "synthetic_null_unsafe_exclusion": "defect",
    }

    canonical_tables = {
        "warehouse.fct_orders": (
            ["order_id", "line_nbr"],
            ["order_id", "line_nbr", "order_dt", "order_ts", "customer_id",
             "sku", "qty", "unit_price", "promo_id", "is_manual_adjustment"]),
        "warehouse.fct_revenue": (
            ["order_id", "line_nbr"],
            ["order_id", "line_nbr", "order_dt", "gross_amount", "promo_id",
             "discount_amount", "tax_amount", "net_amount"]),
        "warehouse.kpi_daily": (
            ["dt"],
            ["dt", "order_lines", "orders", "gross_revenue", "net_revenue", "aov"]),
        "warehouse.bi_revenue_extract": (
            ["dt"],
            ["dt", "order_lines", "orders", "gross_revenue", "net_revenue", "aov"]),
        "raw.orders_landing": (
            ["order_id", "line_nbr", "batch_id"],
            ["order_id", "line_nbr", "batch_id", "order_dt", "qty", "unit_price"]),
        "reporting.rpt_channel_mix": (
            ["order_dt", "promo_id"],
            ["order_dt", "promo_id", "lines", "gross"]),
    }

    # The tables each stressor's own repair writes. Reported as evidence only;
    # the verdict never uses it -- the footprint is measured, not declared.
    repair_writes = {
        "retry_backfill_doubling": ["warehouse.fct_orders", "warehouse.kpi_daily",
                                    "warehouse.bi_revenue_extract"],
        "stale_definition_layout": ["warehouse.fct_revenue", "warehouse.kpi_daily",
                                    "warehouse.bi_revenue_extract"],
        "late_arrival": ["warehouse.fct_orders", "warehouse.fct_revenue",
                         "warehouse.kpi_daily", "warehouse.bi_revenue_extract"],
        "synthetic_null_unsafe_exclusion": ["reporting.rpt_channel_mix"],
    }

    def __init__(self, repo: Path = REPO, seed: int = 20260308):
        self.repo = Path(repo)
        self.seed = seed
        self.m = _load_module(
            self.repo / "tasks" / "GT004-doubling-window" / "fixtures"
            / "make_fixture_world.py", "_gt004_mfw")

    # ---------------------------------------------------------------- build
    def build(self, subset, dest: Path, *, db_path: Path | None = None) -> Path:
        m, subset = self.m, set(subset)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        db = Path(db_path) if db_path else dest / "world.duckdb"
        db.parent.mkdir(parents=True, exist_ok=True)
        for f in (db, Path(str(db) + ".wal")):
            f.unlink(missing_ok=True)
        con = _connect(db)
        for s in ("raw", "warehouse", "ops", "reporting"):
            con.execute("CREATE SCHEMA " + s)

        con.execute("CREATE TEMP TABLE src_lines AS " + m.src_lines_sql(self.seed))
        con.execute("CREATE TEMP TABLE base_orders AS " + m.BASE_ORDERS_SQL)
        con.execute("CREATE TABLE warehouse.dim_promotions AS " + m.PROMO_SQL)

        # ---- stressor: retry_backfill_doubling --------------------------
        # FCT_ORDERS_SQL is base_orders UNION ALL the retry copy of the window.
        if "retry_backfill_doubling" in subset:
            con.execute("CREATE TABLE warehouse.fct_orders AS " + m.FCT_ORDERS_SQL)
        else:
            con.execute("CREATE TABLE warehouse.fct_orders AS SELECT * FROM "
                        "base_orders ORDER BY order_dt, order_id, line_nbr")

        con.execute("CREATE TABLE raw.orders_snapshot AS " + m.SNAPSHOT_SQL)
        con.execute("CREATE TABLE raw.orders_landing AS " + m.LANDING_SQL)

        # ---- stressor: stale_definition_layout --------------------------
        # The mart is built from the TRUE business state either way -- which is
        # why the doubling surfaces as order_lines doubling while net_revenue
        # does not. Same call as make_fixture_world.build_world.
        con.execute("CREATE TABLE warehouse.fct_revenue AS " + m.revenue_sql(
            "base_orders", "warehouse.dim_promotions",
            stale="stale_definition_layout" in subset))

        con.execute("CREATE TABLE warehouse.kpi_daily AS " + m.kpi_sql(
            "warehouse.fct_orders", "warehouse.fct_revenue"))
        con.execute("CREATE TABLE " + m.BI_EXTRACT + " AS " + m.BI_EXTRACT_SQL)

        for name, body in m.REPORTING_MODELS.items():
            con.execute("CREATE TABLE reporting." + name + " AS " + m.derender(body))

        con.execute("CREATE TABLE ops.job_runs AS " + m.JOB_RUNS_SQL)
        con.execute("CREATE TABLE ops.freshness_checks AS " + m.FRESHNESS_SQL)
        con.execute("CREATE TABLE ops.dq_checks AS " + m.DQ_SQL)
        con.execute("CREATE TABLE warehouse.pipeline_watermark AS " + m.WATERMARK_SQL)

        # ---- stressor: late_arrival -------------------------------------
        # The same rows apply_late_arrivals.py replays from the dt=* CSVs,
        # taken from the generator's own late_rows_sql so they cannot drift.
        # raw.orders_snapshot and warehouse.* are deliberately untouched: the
        # loader's job is to merge this, and that is what the repair does.
        if "late_arrival" in subset:
            con.execute("CREATE TEMP TABLE late_rows AS " + m.late_rows_sql())
            con.execute("""
                INSERT INTO raw.orders_landing
                SELECT arrival_ts, batch_id, order_id, line_nbr, order_dt,
                       order_ts, customer_id, sku, qty, unit_price, promo_id,
                       'landing/orders/dt=' || strftime(order_dt, '%Y-%m-%d')
                         || '/orders.csv'
                FROM late_rows ORDER BY order_dt, order_id, line_nbr""")

        # ---- the isolated negative-control surface ------------------------
        # Fed from raw.orders_snapshot (a frozen vendor extract) and its own
        # exclusion dim. No other stressor and no other repair in this adapter
        # reads or writes either relation, by construction.
        con.execute("""CREATE TABLE reporting.excluded_promos AS
                       SELECT * FROM (VALUES ('PROMO-007'), (NULL)) AS t(promo_id)""")
        if "synthetic_null_unsafe_exclusion" in subset:
            where = ("WHERE s.promo_id NOT IN "
                     "(SELECT promo_id FROM reporting.excluded_promos)")
        else:
            where = ("WHERE s.promo_id IS NULL OR s.promo_id NOT IN "
                     "(SELECT promo_id FROM reporting.excluded_promos "
                     " WHERE promo_id IS NOT NULL)")
        con.execute("""CREATE TABLE reporting.rpt_channel_mix AS
            SELECT s.order_dt, COALESCE(s.promo_id, 'NONE') AS promo_id,
                   COUNT(*) AS lines,
                   ROUND(SUM(s.qty * s.unit_price), 2)::DECIMAL(14,2) AS gross
            FROM raw.orders_snapshot s """ + where + """
            GROUP BY 1, 2 ORDER BY 1, 2""")

        _close(con)
        return db

    # --------------------------------------------------------------- repair
    def repair(self, name: str, db: Path) -> None:
        getattr(self, "_repair_" + name)(Path(db))

    def _tax_rate(self, con) -> str:
        """The rate the CURRENT layout was written at, discovered from the rows
        that already satisfy the invariant -- the oracle's own method."""
        obs = con.execute(
            "SELECT ROUND(SUM(tax_amount) / NULLIF(SUM(gross_amount - "
            "discount_amount), 0), 4) FROM warehouse.fct_revenue WHERE "
            + self.m.LAYOUT_INVARIANT).fetchone()[0]
        return "%.4f" % float(obs)

    _REV_REFRESH = """
CREATE OR REPLACE TABLE warehouse.fct_revenue AS
WITH o AS (SELECT order_id, line_nbr, order_dt, qty, unit_price, promo_id
           FROM warehouse.fct_orders),
p AS (SELECT *, ROUND(qty * unit_price, 2) AS gross FROM o),
j AS (SELECT p.*, COALESCE(ROUND(p.gross * d.discount_pct / 100.0, 2), 0.00) AS disc
      FROM p LEFT JOIN warehouse.dim_promotions d ON d.promo_id = p.promo_id)
SELECT order_id, line_nbr, order_dt,
       gross::DECIMAL(12,2)                            AS gross_amount,
       promo_id,
       disc::DECIMAL(12,2)                             AS discount_amount,
       ROUND((gross - disc) * {tax}, 2)::DECIMAL(12,2) AS tax_amount,
       (gross - disc + ROUND((gross - disc) * {tax}, 2))::DECIMAL(12,2) AS net_amount,
       TIMESTAMP '2026-03-08 09:00:00'                 AS load_ts,
       'full-refresh-layout'                           AS batch_id
FROM j ORDER BY order_dt, order_id, line_nbr
"""

    _KPI_REBUILD = """
CREATE OR REPLACE TABLE warehouse.kpi_daily AS
WITH o AS (SELECT order_dt AS dt, COUNT(*) AS order_lines,
                  COUNT(DISTINCT order_id) AS orders,
                  ROUND(SUM(qty * unit_price), 2)::DECIMAL(14,2) AS gross_revenue
           FROM warehouse.fct_orders GROUP BY 1),
r AS (SELECT order_dt AS dt, ROUND(SUM(net_amount), 2)::DECIMAL(14,2) AS net_revenue
      FROM warehouse.fct_revenue GROUP BY 1)
SELECT o.dt, o.order_lines, o.orders, o.gross_revenue,
       COALESCE(r.net_revenue, 0)::DECIMAL(14,2) AS net_revenue,
       ROUND(COALESCE(r.net_revenue, 0) / NULLIF(o.orders, 0), 2)::DECIMAL(12,2) AS aov
FROM o LEFT JOIN r USING (dt) ORDER BY 1
"""

    _BI_REBUILD = """
CREATE OR REPLACE TABLE warehouse.bi_revenue_extract AS
SELECT dt, order_lines, orders, gross_revenue, net_revenue, aov
FROM warehouse.kpi_daily ORDER BY dt
"""

    def _downstream(self, con) -> None:
        """kpi_daily is a TABLE, and the dashboard reads bi_revenue_extract, not
        the mart. Every repair in solution.sh carries both hops."""
        con.execute(self._KPI_REBUILD)
        con.execute(self._BI_REBUILD)

    def _repair_retry_backfill_doubling(self, db: Path) -> None:
        """solution.sh step 3/7: de-duplicate keeping the EARLIEST load_ts, which
        is the copy that carries finance's manual adjustments."""
        con = _connect(db)
        con.execute("""
          DELETE FROM warehouse.fct_orders WHERE rowid IN (
            SELECT rowid FROM (
              SELECT rowid, row_number() OVER (PARTITION BY order_id, line_nbr
                     ORDER BY load_ts, rowid) AS rn
              FROM warehouse.fct_orders) WHERE rn > 1)""")
        self._downstream(con)
        _close(con)

    def _repair_stale_definition_layout(self, db: Path) -> None:
        """solution.sh step 4/7: the full refresh the materialisation switch
        never ran, rebuilt FROM warehouse.fct_orders under the current layout."""
        con = _connect(db)
        con.execute(self._REV_REFRESH.replace("{tax}", self._tax_rate(con)))
        self._downstream(con)
        _close(con)

    def _repair_late_arrival(self, db: Path) -> None:
        """The hardened loader's job (solution.sh step 5/7): merge the late batch
        on the business key -- update in place, insert genuinely new lines --
        then carry the affected keys into the mart and the exposure.

        Key-scoped on purpose. A full refresh of fct_revenue here would silently
        perform the stale-layout repair too and the coupling test would be
        measuring this function instead of the world."""
        m = self.m
        con = _connect(db)
        tax = self._tax_rate(con)
        con.execute("""
          UPDATE warehouse.fct_orders f
             SET order_ts = l.order_ts, customer_id = l.customer_id, sku = l.sku,
                 qty = l.qty, unit_price = l.unit_price, promo_id = l.promo_id
            FROM (SELECT * FROM raw.orders_landing
                   WHERE batch_id = '""" + m.LATE_BATCH + """') l
           WHERE f.order_id = l.order_id AND f.line_nbr = l.line_nbr""")
        con.execute("""
          INSERT INTO warehouse.fct_orders
          SELECT l.order_id, l.line_nbr, l.order_dt, l.order_ts, l.customer_id,
                 l.sku, l.qty, l.unit_price, l.promo_id, FALSE, NULL,
                 TIMESTAMP '""" + m.LATE_ARRIVAL_TS + """', '""" + m.LATE_BATCH + """'
            FROM raw.orders_landing l
           WHERE l.batch_id = '""" + m.LATE_BATCH + """'
             AND NOT EXISTS (SELECT 1 FROM warehouse.fct_orders f
                              WHERE f.order_id = l.order_id
                                AND f.line_nbr = l.line_nbr)""")
        con.execute("""
          DELETE FROM warehouse.fct_revenue
           WHERE (order_id, line_nbr) IN (SELECT order_id, line_nbr
                  FROM raw.orders_landing
                 WHERE batch_id = '""" + m.LATE_BATCH + """')""")
        con.execute("""
          INSERT INTO warehouse.fct_revenue
          WITH o AS (SELECT order_id, line_nbr, order_dt, qty, unit_price, promo_id
                     FROM warehouse.fct_orders
                     WHERE (order_id, line_nbr) IN (
                           SELECT order_id, line_nbr FROM raw.orders_landing
                            WHERE batch_id = '""" + m.LATE_BATCH + """')),
          p AS (SELECT *, ROUND(qty * unit_price, 2) AS gross FROM o),
          j AS (SELECT p.*, COALESCE(ROUND(p.gross * d.discount_pct / 100.0, 2),
                                     0.00) AS disc
                FROM p LEFT JOIN warehouse.dim_promotions d
                  ON d.promo_id = p.promo_id)
          SELECT order_id, line_nbr, order_dt, gross::DECIMAL(12,2), promo_id,
                 disc::DECIMAL(12,2),
                 ROUND((gross - disc) * """ + tax + """, 2)::DECIMAL(12,2),
                 (gross - disc
                  + ROUND((gross - disc) * """ + tax + """, 2))::DECIMAL(12,2),
                 TIMESTAMP '""" + m.LATE_ARRIVAL_TS + """',
                 '""" + m.LATE_BATCH + """'
          FROM j""")
        self._downstream(con)
        _close(con)

    def _repair_synthetic_null_unsafe_exclusion(self, db: Path) -> None:
        """NULL-safe rewrite of the exclusion predicate, GT006's oracle edit
        transplanted onto the control table."""
        con = _connect(db)
        con.execute("""CREATE OR REPLACE TABLE reporting.rpt_channel_mix AS
            SELECT s.order_dt, COALESCE(s.promo_id, 'NONE') AS promo_id,
                   COUNT(*) AS lines,
                   ROUND(SUM(s.qty * s.unit_price), 2)::DECIMAL(14,2) AS gross
            FROM raw.orders_snapshot s
            WHERE s.promo_id IS NULL OR s.promo_id NOT IN
                  (SELECT promo_id FROM reporting.excluded_promos
                    WHERE promo_id IS NOT NULL)
            GROUP BY 1, 2 ORDER BY 1, 2""")
        _close(con)

    # -------------------------------------------------------------- clauses
    def clauses(self, name: str, db: Path) -> list:
        con = duckdb.connect(":memory:", config={"threads": 1})
        con.execute("ATTACH '" + Path(db).as_posix() + "' AS wh (READ_ONLY)")
        con.execute("USE wh")
        try:
            return getattr(self, "_clauses_" + name)(con)
        finally:
            con.close()

    @staticmethod
    def _c(name, contract, ok, detail):
        return {"clause": name, "contract": contract, "ok": bool(ok),
                "detail": detail}

    def _clauses_retry_backfill_doubling(self, con) -> list:
        m = self.m
        win = ("order_dt BETWEEN DATE '" + m.WIN_START + "' AND DATE '"
               + m.WIN_END + "'")
        dups = con.execute(
            "SELECT COUNT(*) FROM (SELECT order_id, line_nbr FROM "
            "warehouse.fct_orders WHERE " + win
            + " GROUP BY 1,2 HAVING COUNT(*) > 1)").fetchone()[0]
        adj = con.execute("SELECT COUNT(*) FROM warehouse.fct_orders WHERE "
                          + win + " AND is_manual_adjustment").fetchone()[0]
        adj_exp = len(m.ADJ_DATES_IN_WINDOW) * m.ADJ_PER_DATE
        have, want = con.execute(
            "SELECT (SELECT COUNT(DISTINCT (order_id, line_nbr)) "
            "          FROM warehouse.fct_orders WHERE " + win + "), "
            "       (SELECT COUNT(DISTINCT (order_id, line_nbr)) "
            "          FROM raw.orders_snapshot WHERE " + win + ")").fetchone()
        days = con.execute("SELECT COUNT(DISTINCT order_dt) FROM "
                           "warehouse.fct_orders WHERE " + win).fetchone()[0]
        nwin = (m.dt.date.fromisoformat(m.WIN_END)
                - m.dt.date.fromisoformat(m.WIN_START)).days + 1
        return [
            self._c("no_duplicate_business_keys_in_window", "exactly_once",
                    dups == 0, {"duplicated_keys": dups}),
            self._c("manual_adjustments_preserved", "auditability",
                    adj == adj_exp, {"in_window": adj, "expected": adj_exp}),
            self._c("no_key_destroyed_in_window", "liveness",
                    have >= want, {"distinct_keys": have, "snapshot_keys": want}),
            self._c("window_still_populated", "liveness",
                    days == nwin, {"days_present": days, "expected": nwin}),
        ]

    def _clauses_stale_definition_layout(self, con) -> list:
        m = self.m
        inv = m.LAYOUT_INVARIANT
        bad = con.execute("SELECT COUNT(*) FROM warehouse.fct_revenue "
                          "WHERE NOT (" + inv + ")").fetchone()[0]
        rows, keys = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT (order_id, line_nbr)) "
            "FROM warehouse.fct_revenue").fetchone()
        sd = m.SWITCH_DATE
        pre, post = con.execute(
            "SELECT ROUND(SUM(CASE WHEN order_dt <  DATE '" + sd + "' "
            "                      THEN tax_amount END) "
            "           / NULLIF(SUM(CASE WHEN order_dt <  DATE '" + sd + "' "
            "                      THEN gross_amount - discount_amount END), 0), 4), "
            "       ROUND(SUM(CASE WHEN order_dt >= DATE '" + sd + "' "
            "                      THEN tax_amount END) "
            "           / NULLIF(SUM(CASE WHEN order_dt >= DATE '" + sd + "' "
            "                      THEN gross_amount - discount_amount END), 0), 4) "
            "FROM warehouse.fct_revenue").fetchone()
        seam = (pre is not None and post is not None
                and abs(float(pre) - float(post)) < 5e-4)
        ndays = con.execute("SELECT COUNT(DISTINCT order_dt) "
                            "FROM warehouse.fct_revenue").fetchone()[0]
        hist = (m.dt.date.fromisoformat(m.HIST_END)
                - m.dt.date.fromisoformat(m.HIST_START)).days + 1
        return [
            self._c("layout_invariant_holds_everywhere", "consumer_consistency",
                    bad == 0, {"violating_rows": bad, "invariant": inv}),
            self._c("one_revenue_row_per_business_key", "exactly_once",
                    rows == keys, {"rows": rows, "keys": keys}),
            self._c("no_seam_at_materialisation_switch", "consumer_consistency",
                    seam, {"pre_switch_rate": str(pre), "post_switch_rate": str(post)}),
            self._c("revenue_covers_full_history", "liveness",
                    ndays == hist, {"days": ndays, "expected": hist}),
        ]

    def _clauses_late_arrival(self, con) -> list:
        m = self.m
        con.execute("CREATE TEMP TABLE _late AS " + m.late_rows_sql())
        newk, restk = con.execute(
            "SELECT SUM(change_type = 'new'), SUM(change_type = 'restated') "
            "FROM _late").fetchone()
        mult = ("SELECT COUNT(*) FROM ("
                "  SELECT l.order_id, l.line_nbr, COUNT(f.order_id) AS n"
                "    FROM _late l LEFT JOIN warehouse.fct_orders f"
                "      ON f.order_id = l.order_id AND f.line_nbr = l.line_nbr"
                "   WHERE l.change_type = '{k}' GROUP BY 1,2 HAVING n <> 1)")
        once_new = con.execute(mult.replace("{k}", "new")).fetchone()[0]
        once_rest = con.execute(mult.replace("{k}", "restated")).fetchone()[0]
        stale_vals = con.execute(
            "SELECT COUNT(*) FROM warehouse.fct_orders f JOIN _late l "
            "  ON f.order_id = l.order_id AND f.line_nbr = l.line_nbr "
            " WHERE f.qty <> l.qty OR f.unit_price <> l.unit_price "
            "    OR f.sku <> l.sku").fetchone()[0]
        ingested = con.execute(
            "SELECT COUNT(*) FROM warehouse.fct_orders f JOIN _late l "
            "  ON f.order_id = l.order_id AND f.line_nbr = l.line_nbr "
            " WHERE l.change_type = 'new'").fetchone()[0]
        return [
            self._c("new_late_lines_present_exactly_once", "exactly_once",
                    once_new == 0, {"keys_not_at_multiplicity_1": once_new,
                                    "new_keys": int(newk)}),
            self._c("restated_lines_present_exactly_once", "exactly_once",
                    once_rest == 0, {"keys_not_at_multiplicity_1": once_rest,
                                     "restated_keys": int(restk)}),
            self._c("restated_lines_carry_the_new_values", "version_precedence",
                    stale_vals == 0,
                    {"rows_holding_superseded_values": stale_vals}),
            self._c("late_batch_actually_ingested", "liveness",
                    ingested == int(newk),
                    {"ingested": ingested, "expected": int(newk)}),
        ]

    def _clauses_synthetic_null_unsafe_exclusion(self, con) -> list:
        rows, days = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT order_dt) "
            "FROM reporting.rpt_channel_mix").fetchone()
        leaked = con.execute(
            "SELECT COUNT(*) FROM reporting.rpt_channel_mix "
            "WHERE promo_id = 'PROMO-007'").fetchone()[0]
        kept = con.execute(
            "SELECT COUNT(DISTINCT promo_id) FROM reporting.rpt_channel_mix"
        ).fetchone()[0]
        return [
            self._c("report_not_empty", "liveness", rows > 0,
                    {"rows": rows, "days": days}),
            self._c("only_the_named_promo_excluded", "consumer_consistency",
                    leaked == 0 and kept >= 40,
                    {"excluded_promo_rows": leaked, "distinct_promos_kept": kept}),
        ]


# ===========================================================================
# GT004 -- the same world, on a clock
# ===========================================================================
#
# WHY THIS EXISTS. `late_arrival` is not a defect, it is an EVENT: rows that
# arrive AFTER a repair has been made. The unclocked adapter can only stage it
# as rows that are already in `raw.orders_landing` when the comparison starts,
# which is a world in which the repair and the arrival are simultaneous - and
# in that world the dedupe never reads the landing table, so the pair reads
# uncoupled. docs/implementation_plan/harness/COUPLING.md calls that its weakest limitation and predicts the
# verdict would move. This is the measurement of that prediction.
#
# The clocked world is the same warehouse under a two-job nightly:
#   02:00 `late_delivery`   only when `late_arrival` is in the subset. Inserts
#                           the late batch into raw.orders_landing - the
#                           delivery, at the moment it is delivered.
#   03:00 `revenue_nightly` the world's own loader, in APPEND mode, which is
#                           the defect GT004 is built around. It inserts the
#                           late batch into warehouse.fct_orders without asking
#                           whether those business keys are already there.
# Both are `sql` exec specs, so nothing here needs a workspace script and the
# live plane carries no host path.
VTIME_START_GT004 = "2026-03-01T00:00:00Z"
GT004_NIGHT = "2026-03-02T00:00:00Z"


class GT004ClockedAdapter(GT004Adapter):
    """GT004Adapter plus a world root and an advance. Same stressors, same
    repairs, same clauses - only the staging of `late_arrival` changes."""

    clocked = True

    def _nightly_sql(self) -> list:
        m = self.m
        return [
            # append mode: the batch goes in, the business key is not consulted
            """INSERT INTO warehouse.fct_orders
               SELECT l.order_id, l.line_nbr, l.order_dt, l.order_ts,
                      l.customer_id, l.sku, l.qty, l.unit_price, l.promo_id,
                      FALSE, NULL, TIMESTAMP '""" + m.LATE_ARRIVAL_TS + """',
                      '""" + m.LATE_BATCH + """'
                 FROM raw.orders_landing l
                WHERE l.batch_id = '""" + m.LATE_BATCH + """'""",
            self._KPI_REBUILD,
            self._BI_REBUILD,
        ]

    def _delivery_sql(self) -> list:
        m = self.m
        return [
            "CREATE TEMP TABLE late_rows AS " + m.late_rows_sql(),
            """INSERT INTO raw.orders_landing
               SELECT arrival_ts, batch_id, order_id, line_nbr, order_dt,
                      order_ts, customer_id, sku, qty, unit_price, promo_id,
                      'landing/orders/dt=' || strftime(order_dt, '%Y-%m-%d')
                        || '/orders.csv'
                 FROM late_rows ORDER BY order_dt, order_id, line_nbr""",
        ]

    def world(self, subset, dest: Path) -> Path:
        import json
        subset = set(subset)
        root = Path(dest)
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        # `late_arrival` is staged as a DELIVERY here, not as rows: the whole
        # point is that it happens after the repair.
        self.build(subset - {"late_arrival"}, root,
                   db_path=root / "var" / "warehouse" / "warehouse.duckdb")
        jobs = {}
        if "late_arrival" in subset:
            jobs["late_delivery"] = {
                "command": "late_delivery", "duration_s": 30,
                "time": "02:00", "day_offset": 0,
                "exec": {"kind": "sql", "statements": self._delivery_sql(),
                         "detail": "late_delivery"}}
        jobs["revenue_nightly"] = {
            "command": "revenue_nightly", "duration_s": 120,
            "time": "03:00", "day_offset": 0,
            "exec": {"kind": "sql", "statements": self._nightly_sql(),
                     "detail": "revenue_nightly"}}
        state = {"vnow": VTIME_START_GT004,
                 "jobs": {n: {"command": s["command"],
                              "duration_s": s["duration_s"],
                              "time": s["time"], "day_offset": s["day_offset"]}
                          for n, s in jobs.items()},
                 "runs": []}
        live = {"vtime_start": VTIME_START_GT004, "days_landed": 1, "jobs": jobs}
        for rel, doc in (("var/sched/state.json", state),
                         ("var/world/live.json", live)):
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        return root

    @staticmethod
    def warehouse(root: Path) -> Path:
        return Path(root) / "var" / "warehouse" / "warehouse.duckdb"

    @staticmethod
    def advance(root: Path) -> dict:
        scheduler = _world_scheduler()
        return scheduler.advance(Path(root), 24.0)

    def _merge_sql(self) -> list:
        """The hardened nightly: upsert the batch on the business key.

        solution.sh step 5 calls the late-arrival repair "the hardened
        loader's job", and under a clock that phrase becomes load-bearing: a
        one-shot merge applied at t0 cannot incorporate a delivery that has not
        happened, and the world's own append-mode nightly then duplicates it.
        So the clocked repair changes the NIGHTLY as well as the rows, which is
        what the task's own oracle says it is.
        """
        m = self.m
        batch = "'" + m.LATE_BATCH + "'"
        return [
            """DELETE FROM warehouse.fct_orders
                WHERE (order_id, line_nbr) IN (
                      SELECT order_id, line_nbr FROM raw.orders_landing
                       WHERE batch_id = """ + batch + ")",
            """INSERT INTO warehouse.fct_orders
               SELECT l.order_id, l.line_nbr, l.order_dt, l.order_ts,
                      l.customer_id, l.sku, l.qty, l.unit_price, l.promo_id,
                      FALSE, NULL, TIMESTAMP '""" + m.LATE_ARRIVAL_TS + """',
                      """ + batch + """
                 FROM raw.orders_landing l
                WHERE l.batch_id = """ + batch,
            self._KPI_REBUILD,
            self._BI_REBUILD,
        ]

    def _repair_late_arrival(self, db: Path) -> None:
        import json
        db = Path(db)
        root = db.parent.parent.parent
        live_path = root / "var" / "world" / "live.json"
        live = json.loads(live_path.read_text(encoding="utf-8"))
        live["jobs"]["revenue_nightly"]["exec"]["statements"] = self._merge_sql()
        with live_path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(live, indent=2, sort_keys=True) + "\n")
        super()._repair_late_arrival(db)


# ===========================================================================
# GT009 -- the settlement cutoff. Clocked by construction.
# ===========================================================================
#
# EVERY STRESSOR HERE IS AN EVENT, and none of them is stageable as t0 state:
# the obligation each one creates does not exist until the night it arrives.
# `build` therefore stages what the unclocked filter CAN see - the partition
# sitting in the feed, unread - and that is the honest answer to "what does
# this stressor look like at t0": nothing in the warehouse has moved, and no
# repair applied at t0 can satisfy a clause about a publication that has not
# happened. `--selfcheck` without `--clock` says so, per stressor, rather than
# returning a verdict computed on a world where the question is not yet asked.
class GT009Adapter:
    task = "GT009-settlement-cutoff"
    clocked = True

    stressors = [
        "higher_version_correction_after_cutoff",
        "older_replay_after_the_correction",
        "retried_delivery_between_commit_and_checkpoint",
    ]
    kinds = {name: "event" for name in stressors}

    # A stressor that is harmless ALONE and harmful only beside another. The
    # self-check's teeth test presumes harm in isolation, and a stressor that
    # breaks nothing on its own is exactly what a COUPLED stressor looks like -
    # so the teeth leg is staged with the partner present (and unrepaired).
    # The replay is the worked example: re-sending version 1 of six payments is
    # a no-op in a world where version 2 was never delivered.
    conditional_on = {
        "older_replay_after_the_correction":
            ["higher_version_correction_after_cutoff"],
    }

    canonical_tables = {
        "warehouse.settlement_handoff": (
            ["settlement_day", "merchant_id"],
            ["settlement_day", "merchant_id", "payments", "net_cents",
             "correction_ref"]),
        "warehouse.clearing_window": (
            ["closed_on", "settlement_day"],
            ["closed_on", "settlement_day", "status", "payments", "net_cents"]),
        "warehouse.fct_payments": (
            ["payment_id"],
            ["payment_id", "payment_day", "merchant_id", "amount_cents",
             "version"]),
    }

    repair_writes = {
        "higher_version_correction_after_cutoff":
            ["warehouse.settlement_handoff", "warehouse.settlement_corrections"],
        "older_replay_after_the_correction":
            ["warehouse.fct_payments", "warehouse.settlement_handoff"],
        "retried_delivery_between_commit_and_checkpoint":
            ["raw.payments_events", "warehouse.fct_payments",
             "warehouse.settlement_handoff"],
    }

    LOADER = "pipelines/load_payments.py"
    PUBLISHER = "pipelines/publish_settlement.py"
    SUBSET_FILE = ".coupling-subset.json"

    def __init__(self, repo: Path = REPO):
        self.repo = Path(repo)
        task_dir = self.repo / "tasks" / self.task
        self.build_dir = task_dir / "fixtures" / "build"
        self.m = _load_module(task_dir / "fixtures" / "make_fixture_world.py",
                              "_gt009_mfw")
        self.contract = _load_module(
            task_dir / "fixtures" / "fixture_contract.py",
            "_gt009_contract").CONTRACT
        self.good_loader = (task_dir / "solution" / "load_payments.py").read_text(
            encoding="utf-8")
        self.good_publisher = (
            task_dir / "solution" / "publish_settlement.py").read_text(
                encoding="utf-8")
        self.feed = self.m.build_feed(self.m.SEED)
        refs = self.m.trajectories(self.feed)
        closed = self.contract["closed_day"]
        self.closed_day = closed
        self.v1_closed = sorted(
            row[:4] for row in refs["t0"][self.m.HANDOFF] if row[0] == closed)
        self.v2_closed = sorted(
            row[:4] for row in
            refs["variants"]["oracle"]["nights"][1][self.m.HANDOFF]
            if row[0] == closed)
        self.restated = set(refs["restated_payments"])
        last = self.contract["plant_dates"][-1]
        self.last_day = last
        self.last_day_payments = len({
            row["payment_id"] for row in self.feed.plants[last]
            if row["payment_day"] == last})

    # ------------------------------------------------------------ partitions
    def _rows(self, day: str, subset: set) -> tuple[list, list]:
        """(base rows, the extra file's rows) for one planted partition."""
        rows = [[r["event_id"], r["delivered_at"], r["payment_day"],
                 r["payment_id"], r["version"], r["event_type"],
                 r["merchant_id"], r["amount_cents"]]
                for r in self.feed.plants[day]]
        night = self.contract["plant_dates"].index(day) + 1
        if night == 2 and "higher_version_correction_after_cutoff" not in subset:
            rows = [r for r in rows if r[2] != self.closed_day]
        if night == 3 and "older_replay_after_the_correction" not in subset:
            rows = [r for r in rows if r[2] != self.closed_day]
        retry = []
        if night == 4 and \
                "retried_delivery_between_commit_and_checkpoint" in subset:
            retry = list(rows)
        return rows, retry

    def _land(self, root: Path, day: str, subset: set) -> None:
        rows, retry = self._rows(day, subset)
        feed = (Path(root) / "workspace"
                / self.contract["layout"]["feed_relpath"] / ("dt=" + day))
        self.m._write(feed / "events.csv", self.m._csv(rows))
        if retry:
            self.m._write(feed / "events-retry.csv", self.m._csv(retry))

    def _stage_root(self, dest: Path) -> Path:
        root = Path(dest) / "world"
        self.m.stage_world_root(self.build_dir, root)
        return root

    # ---------------------------------------------------------------- build
    def build(self, subset, dest: Path) -> Path:
        """t0 staging: the partitions sit in the feed and nothing has read them.

        This is everything an unclocked comparison can express about an event.
        The warehouse is byte-identical across every subset, which is the
        honest answer and is what `--selfcheck` without `--clock` reports.
        """
        subset = set(subset)
        root = self._stage_root(dest)
        for day in self.contract["plant_dates"]:
            self._land(root, day, subset)
        return self.warehouse(root)

    # ---------------------------------------------------------------- clock
    def world(self, subset, dest: Path) -> Path:
        import json
        root = self._stage_root(dest)
        with (root / self.SUBSET_FILE).open("w", encoding="utf-8",
                                            newline="") as fh:
            fh.write(json.dumps(sorted(subset)) + "\n")
        return root

    @staticmethod
    def warehouse(root: Path) -> Path:
        return Path(root) / "var" / "warehouse" / "warehouse.duckdb"

    def advance(self, root: Path) -> dict:
        """Land each planted partition and advance a night, four times.

        GT009's stressors sit on three different nights - the correction on 2,
        the replay on 3, the retried delivery on 4 - so "the next planted
        event" for a PAIR is the whole schedule. Advancing all four is what
        makes every ordered pair measurable on one staging.
        """
        import json
        root = Path(root)
        subset = set(json.loads(
            (root / self.SUBSET_FILE).read_text(encoding="utf-8")))
        scheduler = _world_scheduler()
        out = None
        for day in self.contract["plant_dates"]:
            self._land(root, day, subset)
            out = scheduler.advance(
                root, float(self.contract["advance_hours_per_night"]))
        return out

    # --------------------------------------------------------------- repair
    def _root_of(self, db: Path) -> Path:
        """The world root a warehouse path belongs to.

        GT009's repairs are PIPELINE changes - a file in the workspace and a
        row in a register - because the decisions they encode are taken before
        the events they answer. The protocol only hands a db path, so the root
        is derived from the layout agent.md 9.2.1 freezes.
        """
        db = Path(db)
        if db.parent.name == "warehouse" and db.parent.parent.name == "var":
            return db.parent.parent.parent
        raise ValueError(f"{db} is not <root>/var/warehouse/warehouse.duckdb")

    def _put(self, root: Path, relpath: str, source: str) -> None:
        self.m._write(Path(root) / "workspace" / relpath, source)

    def repair(self, name: str, db: Path) -> None:
        root = self._root_of(db)
        if name == "higher_version_correction_after_cutoff":
            # the protocol: register the approval, then publish under it
            self._put(root, self.PUBLISHER, self.good_publisher)
            correction = self.contract["correction"]
            con = _connect(db)
            con.execute(
                "INSERT INTO warehouse.settlement_corrections VALUES "
                f"(DATE '{self.closed_day}', '{correction['ticket']}', "
                f"'{correction['approver']}', "
                f"DATE '{correction['approved_at']}')")
            _close(con)
        else:
            # both remaining stressors are answered by the same loader: the
            # published contract's fold, and one row per delivered event.
            # Sharing a repair is a fact about the pair, not a defect here.
            self._put(root, self.LOADER, self.good_loader)

    # -------------------------------------------------------------- clauses
    def clauses(self, name: str, db: Path) -> list:
        con = duckdb.connect(":memory:", config={"threads": 1})
        con.execute("ATTACH '" + Path(db).as_posix() + "' AS wh (READ_ONLY)")
        con.execute("USE wh")
        try:
            return getattr(self, "_clauses_" + name)(con)
        finally:
            con.close()

    @staticmethod
    def _c(name, contract, ok, detail):
        return {"clause": name, "contract": contract, "ok": bool(ok),
                "detail": detail}

    # The published source contract, as SQL over whatever the world has been
    # delivered. Stated RELATIVE to the feed rather than against a constant,
    # because a subset world is a world where fewer events arrived: a clause
    # that demanded the version-2 figures would fail on a world in which the
    # version-2 events were never delivered, which is a bug in the clause and
    # not a property of the pair.
    _CONTRACT_FOLD = """
        SELECT payment_day, merchant_id, COUNT(*) AS payments,
               SUM(amount_cents) AS net_cents
        FROM (SELECT *, row_number() OVER (
                         PARTITION BY payment_id
                         ORDER BY version DESC, delivered_at DESC,
                                  event_id DESC) AS rn
              FROM raw.payments_events)
        WHERE rn = 1 AND event_type <> 'void' AND payment_day = DATE '{day}'
        GROUP BY 1, 2"""

    def _closed_rows(self, con) -> list:
        return sorted([str(r[0]), r[1], int(r[2]), int(r[3])] for r in con.execute(
            "SELECT settlement_day, merchant_id, payments, net_cents "
            "FROM warehouse.settlement_handoff WHERE settlement_day = DATE '"
            + self.closed_day + "'").fetchall())

    def _fold_rows(self, con) -> list:
        return sorted([str(r[0]), r[1], int(r[2]), int(r[3])] for r in con.execute(
            self._CONTRACT_FOLD.replace("{day}", self.closed_day)).fetchall())

    def _clauses_higher_version_correction_after_cutoff(self, con) -> list:
        held, want = self._closed_rows(con), self._fold_rows(con)
        refs = con.execute(
            "SELECT COUNT(*), COUNT(correction_ref) FROM "
            "warehouse.settlement_handoff WHERE settlement_day = DATE '"
            + self.closed_day + "'").fetchone()
        approved = con.execute(
            "SELECT COUNT(*) FROM warehouse.settlement_handoff h "
            "JOIN warehouse.settlement_corrections c "
            "  ON c.settlement_day = h.settlement_day "
            " AND c.ticket = h.correction_ref "
            "WHERE h.settlement_day = DATE '" + self.closed_day + "'").fetchone()[0]
        return [
            self._c("closed_day_holds_the_contract_fold", "cutoff_integrity",
                    held == want,
                    {"held": held[:2], "contract_fold": want[:2]}),
            self._c("restatement_carries_an_approved_ref", "cutoff_integrity",
                    refs[0] > 0 and refs[1] == refs[0] and approved == refs[0],
                    {"rows": refs[0], "with_ref": refs[1],
                     "matching_an_approval": approved}),
        ]

    def _clauses_older_replay_after_the_correction(self, con) -> list:
        stale = con.execute("""
            SELECT COUNT(*) FROM warehouse.fct_payments f
            JOIN (SELECT payment_id, MAX(version) AS top
                    FROM raw.payments_events GROUP BY 1) r
              ON r.payment_id = f.payment_id
            WHERE f.version < r.top""").fetchone()[0]
        twice = con.execute(
            "SELECT COUNT(*) FROM (SELECT event_id FROM raw.payments_events "
            "GROUP BY 1 HAVING COUNT(*) > 1)").fetchone()[0]
        return [
            self._c("superseded_version_is_not_current", "version_precedence",
                    int(stale) == 0,
                    {"payments_held_below_their_top_version": int(stale)}),
            # deliberately NOT "the closed day holds the contract fold": that is
            # the CORRECTION's obligation, and putting it here would make the
            # replay's clause set unsatisfiable by the replay's own repair -
            # the G4-restates-G1c error agent.md 8.10 records.
            self._c("replayed_events_land_once", "exactly_once",
                    int(twice) == 0,
                    {"event_ids_delivered_more_than_once": int(twice)}),
        ]

    def _clauses_retried_delivery_between_commit_and_checkpoint(self, con) -> list:
        """MEASURED TOOTHLESS against this world, and kept for that reason.

        `--selfcheck --clock` reports `planted_world_fails: False` for this
        stressor: the shipped loader already folds by business key, so a
        partition delivered twice collapses on its own and there is nothing for
        a repair to fix. The duplicate only reaches the bank under a publisher
        that counts DELIVERIES rather than payments, which is a rubric
        adversary (tests/run_baselines.py:duplicate_counted) and not a property
        of the world. Every pair involving this stressor is therefore reported
        `executed` and INVALID, which is the self-check doing its job - the
        same way it caught GT004's event/clean-world confusion.
        """
        doubled = con.execute(
            "SELECT COALESCE(SUM(payments), 0) FROM warehouse.settlement_handoff "
            "WHERE settlement_day = DATE '" + self.last_day + "'").fetchone()[0]
        dup = con.execute(
            "SELECT COUNT(*) FROM (SELECT payment_id FROM warehouse.fct_payments "
            "GROUP BY 1 HAVING COUNT(*) > 1)").fetchone()[0]
        return [
            self._c("retried_delivery_settled_once", "version_precedence",
                    int(doubled) == self.last_day_payments,
                    {"payments_settled": int(doubled),
                     "payments_delivered": self.last_day_payments}),
            self._c("one_row_per_payment", "version_precedence", int(dup) == 0,
                    {"payments_at_multiplicity_above_one": int(dup)}),
        ]


def _world_scheduler():
    """`world.scheduler`, imported on demand.

    A task adapter may know the world's LAYOUT and not its internals
    (agent.md 10.2); advancing a clock is the one thing that cannot be done
    from outside, so the import is here, local, and nothing else in this module
    touches `world/`.
    """
    import sys as _sys
    path = str(REPO / "world")
    if path not in _sys.path:
        _sys.path.insert(0, path)
    from world import scheduler                                    # noqa: PLC0415
    return scheduler


ADAPTERS = {GT004Adapter.task: GT004Adapter,
            GT009Adapter.task: GT009Adapter}

# `--clock` on GT004 selects the clocked subclass; everything else is unchanged.
CLOCKED = {GT004Adapter.task: GT004ClockedAdapter,
           GT009Adapter.task: GT009Adapter}


def get_adapter(task_dir, *, clock: bool = False):
    """Resolve an adapter from a task directory path or name. None if unknown.

    `clock=True` prefers the clocked variant where one exists. GT009 has only
    one class - every stressor it carries is an event, so there is no unclocked
    version of it that means anything.
    """
    table = CLOCKED if clock else ADAPTERS
    cls = table.get(Path(task_dir).name) or ADAPTERS.get(Path(task_dir).name)
    return cls() if cls else None
