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
    def build(self, subset, dest: Path) -> Path:
        m, subset = self.m, set(subset)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        db = dest / "world.duckdb"
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


ADAPTERS = {GT004Adapter.task: GT004Adapter}


def get_adapter(task_dir):
    """Resolve an adapter from a task directory path or name. None if unknown."""
    cls = ADAPTERS.get(Path(task_dir).name)
    return cls() if cls else None
