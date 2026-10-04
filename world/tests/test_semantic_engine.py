"""The semantic engine against HAND-WRITTEN SQL (world/world/semantic/render.py).

The point of these tests is independent verification: every expected value
below comes from a DuckDB query written here, by hand, from the semantics in
expr.py's docstring - never from the engine, never from compile.py's SQL. If
the engine and the hand-written query disagree, one of them misreads the
contract, and the failure says which number moved.

Layout:
  - the executive report, visual by visual, on the Commerce fixture;
  - targeted semantics: blank arithmetic, modifiers, time intelligence,
    iterators, visual filters, ordering, auto-exist, native-SQL tables (fan-out
    double count), calculated columns;
  - every ModelError and Unsupported path;
  - performance (the report on the fixture, a ~50k-row model);
  - world/bi.py's legacy SQL panels, byte-identical through the engine.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

WORLD_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORLD_DIR))

from world.hashing import canon_value, json_canon  # noqa: E402
from world.semantic import render  # noqa: E402
from world.semantic.errors import ModelError, Unsupported  # noqa: E402
from world.semantic.examples import (  # noqa: E402
    COMPLETED,
    commerce_model,
    executive_report,
    write_fixture,
)
from world.semantic.expr import (  # noqa: E402
    Agg,
    BinOp,
    Calculate,
    Coalesce,
    Col,
    Divide,
    FilterCompare,
    FilterValues,
    Func,
    If,
    IterAgg,
    KeepFilters,
    KeepOnly,
    Lit,
    MeasureRef,
    Neg,
    Not,
    Related,
    RemoveFilters,
    TimeShift,
)
from world.semantic.model import (  # noqa: E402
    Column,
    Measure,
    Model,
    Page,
    Relationship,
    Report,
    Table,
    Visual,
)

NET = MeasureRef("Net Revenue")


# ----------------------------------------------------------------- helpers
@pytest.fixture(scope="module")
def wh(tmp_path_factory) -> Path:
    return write_fixture(tmp_path_factory.mktemp("fixture") / "fixture.duckdb")


def oracle(warehouse: Path, sql: str) -> list[list]:
    """Run hand-written SQL against the fixture; canonical rows."""
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute("ATTACH '" + str(warehouse).replace("'", "''") + "' AS wh (READ_ONLY)")
        con.execute("USE wh.warehouse")
        return [[canon_value(v) for v in row] for row in con.execute(sql).fetchall()]
    finally:
        con.close()


def by_name(report: Report, name: str) -> Visual:
    return next(v for p in report.pages for v in p.visuals if v.name == name)


def with_measures(model: Model, *measures: Measure) -> Model:
    return replace(model, measures=model.measures + measures)


def rows(model: Model, visual: Visual, warehouse: Path) -> list[list]:
    return render.render_visual(model, visual, warehouse)["rows"]


# ------------------------------------------------- the executive report, by hand
def test_kpis_card(wh):
    out = render.render_visual(commerce_model(), by_name(executive_report(), "KPIs"), wh)
    assert out["columns"] == ["Net Revenue", "Completed Orders", "AOV"]
    assert out["rows"] == oracle(wh, """
        SELECT sum(order_amount), count(*), sum(order_amount) / count(*)
        FROM fct_orders WHERE order_status = 'completed'""")
    # MetricFlow 0.213.0 on the same fixture said 29627.00 / 122 / 242.84426229508196
    assert out["rows"] == [["29627.00", 122, "242.84426229508196"]]


def test_revenue_by_month_with_last_year_and_growth(wh):
    out = render.render_visual(commerce_model(),
                               by_name(executive_report(), "Revenue by Month"), wh)
    assert out["columns"] == ["year", "month", "Net Revenue", "Revenue LY", "YoY Growth"]
    # Revenue LY is the dates in context shifted one year: for a whole month that
    # is the same month a year earlier (the fixture holds no Feb 29). Months
    # before the data have no LY, so growth is blank there.
    expected = oracle(wh, """
        WITH m AS (SELECT DISTINCT year, month FROM dim_date),
        net AS (SELECT d.year, d.month, sum(o.order_amount) AS v
                FROM fct_orders o JOIN dim_date d ON d.date_day = o.order_date
                WHERE o.order_status = 'completed' GROUP BY ALL)
        SELECT m.year, m.month, n.v, p.v, (n.v - p.v) / p.v
        FROM m LEFT JOIN net n ON n.year = m.year AND n.month = m.month
               LEFT JOIN net p ON p.year = m.year - 1 AND p.month = m.month
        WHERE n.v IS NOT NULL OR p.v IS NOT NULL
        ORDER BY m.year, m.month""")
    assert out["rows"] == expected
    assert len(expected) == 14
    assert expected[-2][3] == "2770.00" and expected[-1][3] == "1871.00"   # MetricFlow too


def test_revenue_by_segment_share_ordered_descending(wh):
    out = render.render_visual(commerce_model(),
                               by_name(executive_report(), "Revenue by Segment"), wh)
    assert out["columns"] == ["segment", "Net Revenue", "Segment Share"]
    assert out["rows"] == oracle(wh, """
        WITH s AS (SELECT c.segment, sum(o.order_amount) AS v
                   FROM fct_orders o JOIN dim_customer c ON c.customer_id = o.customer_id
                   WHERE o.order_status = 'completed' GROUP BY 1)
        SELECT segment, v,
               v / (SELECT sum(order_amount) FROM fct_orders WHERE order_status = 'completed')
        FROM s ORDER BY v DESC, segment""")
    assert [r[:2] for r in out["rows"]] == [["Consumer", "10633.00"], ["SMB", "10227.00"],
                                            ["Enterprise", "8767.00"]]


def test_top_products_top_n(wh):
    out = render.render_visual(commerce_model(), by_name(executive_report(), "Top Products"), wh)
    assert out["columns"] == ["product_name", "Units"]
    assert out["rows"] == oracle(wh, """
        SELECT p.product_name, sum(l.quantity)
        FROM fct_order_lines l JOIN dim_product p ON p.product_id = l.product_id
        GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 3""")
    assert out["rows"] == [["Cable", 284], ["Support Plan", 216], ["Gadget", 142]]


def test_revenue_by_region_visual_filter(wh):
    out = render.render_visual(commerce_model(),
                               by_name(executive_report(), "Revenue by Region 2026"), wh)
    assert out["columns"] == ["region", "Net Revenue"]
    assert out["rows"] == oracle(wh, """
        SELECT c.region, sum(o.order_amount)
        FROM fct_orders o JOIN dim_customer c ON c.customer_id = o.customer_id
                          JOIN dim_date d ON d.date_day = o.order_date
        WHERE d.year = 2026 AND o.order_status = 'completed'
        GROUP BY 1 ORDER BY 1""")
    assert out["rows"] == [["APAC", "2388.00"], ["EMEA", "1403.00"], ["NA", "924.00"]]


def test_render_report_shape_and_determinism(wh):
    first = render.render_report(commerce_model(), executive_report(), wh)
    assert list(first) == ["Overview", "Regions"]
    assert list(first["Overview"]) == ["KPIs", "Revenue by Month", "Revenue by Segment",
                                       "Top Products"]
    second = render.render_report(commerce_model(), executive_report(), wh)
    assert json_canon(first) == json_canon(second)


def test_dax_semantics_do_not_change_the_report(wh):
    """No measure of the report adds a blank, so both blank rules agree."""
    sql = render.render_report(commerce_model("sql"), executive_report(), wh)
    dax = render.render_report(commerce_model("dax"), executive_report(), wh)
    assert sql == dax


# ------------------------------------------------------------- blank arithmetic
LY = MeasureRef("Revenue LY")
MONTHS = (Col("Date", "year"), Col("Date", "month"))


def month_visual(*values) -> Visual:
    return Visual("v", group_by=MONTHS, values=values)


def test_blank_plus_sql_vs_dax(wh):
    v = month_visual(("Net", NET), ("Net+LY", BinOp("+", NET, LY)),
                     ("LY-Net", BinOp("-", LY, NET)), ("Net*LY", BinOp("*", NET, LY)))
    net_ly = """
        WITH m AS (SELECT DISTINCT year, month FROM dim_date),
        net AS (SELECT d.year, d.month, sum(o.order_amount) AS v
                FROM fct_orders o JOIN dim_date d ON d.date_day = o.order_date
                WHERE o.order_status = 'completed' GROUP BY ALL)
        SELECT m.year, m.month, n.v AS net, p.v AS ly
        FROM m LEFT JOIN net n ON n.year = m.year AND n.month = m.month
               LEFT JOIN net p ON p.year = m.year - 1 AND p.month = m.month
        WHERE n.v IS NOT NULL OR p.v IS NOT NULL"""
    # sql: any blank operand -> blank
    assert rows(commerce_model("sql"), v, wh) == oracle(wh, f"""
        SELECT year, month, net, net + ly, ly - net, net * ly FROM ({net_ly}) ORDER BY 1, 2""")
    # dax: + and - treat a blank as 0 when the other side is not blank; * stays blank
    assert rows(commerce_model("dax"), v, wh) == oracle(wh, f"""
        SELECT year, month, net, coalesce(net, 0) + coalesce(ly, 0),
               coalesce(ly, 0) - coalesce(net, 0), net * ly
        FROM ({net_ly}) ORDER BY 1, 2""")


def test_blank_plus_blank_is_blank_under_dax(wh):
    card = Visual("c", values=(("x", BinOp("+", Lit(None), Lit(None))),
                               ("y", BinOp("-", Lit(None), NET))))
    total = oracle(wh, "SELECT -sum(order_amount) FROM fct_orders "
                       "WHERE order_status = 'completed'")[0][0]
    assert rows(commerce_model("dax"), card, wh) == [[None, total]]
    assert rows(commerce_model("sql"), card, wh) == [[None, None]]


def test_card_with_every_value_blank_still_has_one_row(wh):
    card = Visual("c", values=(("x", LY),), filters=(FilterValues(Col("Date", "year"), (2025,)),))
    assert rows(commerce_model(), card, wh) == [[None]]


@pytest.mark.parametrize("sem", ["sql", "dax"])
def test_division_by_zero_is_refused(wh, sem):
    card = Visual("c", values=(("x", BinOp("/", NET, Lit(0))),))
    with pytest.raises(Unsupported, match="division by (BLANK or )?zero"):
        render.render_visual(commerce_model(sem), card, wh)


def test_division_by_blank_sql_is_blank_dax_is_refused(wh):
    card = Visual("c", values=(("x", BinOp("/", NET, Lit(None))),))
    assert rows(commerce_model("sql"), card, wh) == [[None]]
    with pytest.raises(Unsupported, match="division by BLANK"):
        render.render_visual(commerce_model("dax"), card, wh)


def test_dax_division_refusal_is_data_dependent(wh):
    """2025 months have a blank LY: Net / LY is refused under dax for the grid,
    but not when a visual filter keeps only months that have a LY."""
    v = month_visual(("g", BinOp("/", NET, LY)))
    with pytest.raises(Unsupported, match="division by BLANK"):
        render.render_visual(commerce_model("dax"), v, wh)
    v26 = replace(v, filters=(FilterValues(Col("Date", "year"), (2026,)),))
    assert rows(commerce_model("dax"), v26, wh) == oracle(wh, """
        WITH net AS (SELECT d.year, d.month, sum(o.order_amount) AS v
                     FROM fct_orders o JOIN dim_date d ON d.date_day = o.order_date
                     WHERE o.order_status = 'completed' GROUP BY ALL)
        SELECT n.year, n.month, n.v / p.v FROM net n
        JOIN net p ON p.year = n.year - 1 AND p.month = n.month ORDER BY 1, 2""")


def test_divide_returns_alternate(wh):
    """The alternate takes the quotient's SQL type (DuckDB unifies CASE branches),
    so -1 comes back as the DOUBLE -1.0; a text alternate cannot unify."""
    card = Visual("c", values=(("a", Divide(NET, Lit(0))), ("b", Divide(NET, Lit(0), Lit(-1))),
                               ("c", Divide(NET, Lit(None), Lit(Decimal("2.5")))),
                               ("d", Divide(Lit(None), Lit(4)))))
    for sem in ("sql", "dax"):
        assert rows(commerce_model(sem), card, wh) == [[None, "-1.0", "2.5", None]]
    text = Visual("c", values=(("x", Divide(NET, Lit(None), Lit("n/a"))),))
    with pytest.raises(ModelError, match="convert"):
        render.render_visual(commerce_model(), text, wh)


def test_comparison_with_blank(wh):
    v = Visual("c", values=(("x", If(BinOp(">", LY, Lit(0)), Lit("up"), Lit("none"))),))
    v25 = replace(v, filters=(FilterValues(Col("Date", "year"), (2025,)),))
    # sql: LY > 0 is blank, and If with a blank condition takes the else branch
    assert rows(commerce_model("sql"), v25, wh) == [["none"]]
    with pytest.raises(Unsupported, match="comparison with BLANK"):
        render.render_visual(commerce_model("dax"), v25, wh)


def test_if_without_else_and_coalesce_and_neg(wh):
    v = Visual("c", values=(("i", If(Lit(False), Lit(1))), ("c", Coalesce((LY, NET))),
                            ("n", Neg(NET)), ("k", If(Not(Lit(True)), Lit(1), Lit(2)))),
               filters=(FilterValues(Col("Date", "year"), (2025,)),))
    total = oracle(wh, """SELECT sum(o.order_amount) FROM fct_orders o
                          WHERE o.order_status = 'completed' AND year(o.order_date) = 2025""")
    assert rows(commerce_model(), v, wh) == [[None, total[0][0], "-" + total[0][0], 2]]


def test_dax_logic_with_blank_is_refused(wh):
    v = Visual("c", values=(("x", BinOp("and", BinOp("=", Lit(1), Lit(1)), Lit(None))),))
    assert rows(commerce_model("sql"), v, wh) == [[None]]
    with pytest.raises(Unsupported, match="BLANK in a logical operator"):
        render.render_visual(commerce_model("dax"), v, wh)


@pytest.mark.parametrize("left,op,right,expected", [
    (False, "and", None, False), (None, "and", False, False), (True, "and", None, None),
    (True, "or", None, True), (None, "or", True, True), (False, "or", None, None),
    (None, "and", None, None), (True, "and", True, True)])
def test_sql_logic_is_three_valued(wh, left, op, right, expected):
    """expr.py: under "sql", and / or follow SQL's three-valued logic."""
    v = Visual("c", values=(("x", BinOp(op, Lit(left), Lit(right))),))
    assert rows(commerce_model("sql"), v, wh) == [[expected]]
    if left is not None and right is not None:
        assert rows(commerce_model("dax"), v, wh) == [[expected]]
    else:                       # dax: a BLANK operand of and / or is refused
        with pytest.raises(Unsupported, match="BLANK in a logical operator"):
            render.render_visual(commerce_model("dax"), v, wh)


def test_sql_logic_over_a_blank_measure(wh):
    """2025 has no Revenue LY: (net > 0 or LY > 0) is TRUE, (net > 0 and LY > 0) blank."""
    pos = lambda e: BinOp(">", e, Lit(0))  # noqa: E731
    v = Visual("c", values=(("or", BinOp("or", pos(NET), pos(LY))),
                            ("and", BinOp("and", pos(NET), pos(LY))),
                            ("if", If(BinOp("and", pos(NET), pos(LY)), Lit("both"), Lit("no")))),
               filters=(FilterValues(Col("Date", "year"), (2025,)),))
    assert rows(commerce_model("sql"), v, wh) == [[True, None, "no"]]


def test_weekday_and_week_start_conventions(wh):
    """expr.py: weekday is 1 = Sunday .. 7 = Saturday; weeks start on Monday."""
    sat, sun, mon = dt.date(2026, 2, 28), dt.date(2026, 3, 1), dt.date(2026, 3, 2)
    v = Visual("c", values=tuple(
        (f"{k}{i}", Func(f, (Lit(d),))) for i, d in enumerate((sat, sun, mon))
        for k, f in (("wd", "weekday"), ("wk", "date_trunc_week"))))
    assert rows(commerce_model(), v, wh) == [[7, "2026-02-23", 1, "2026-02-23", 2,
                                              "2026-03-02"]]
    # the same at row level: a calculated weekday over every order, by hand
    base = commerce_model()
    orders = table_named(base, "Orders")
    m = swap_table(base, replace(orders, columns=orders.columns + (
        Column("wd", "", "int", expr=Func("weekday", (Col("Orders", "order_date"),))),
        Column("wk", "", "date", expr=Func("date_trunc_week", (Col("Orders", "order_date"),))))))
    g = Visual("g", group_by=(Col("Orders", "wd"),), values=(("n", MeasureRef("Order Count")),))
    assert rows(m, g, wh) == oracle(wh, """
        SELECT CASE strftime(order_date, '%A') WHEN 'Sunday' THEN 1 WHEN 'Monday' THEN 2
               WHEN 'Tuesday' THEN 3 WHEN 'Wednesday' THEN 4 WHEN 'Thursday' THEN 5
               WHEN 'Friday' THEN 6 ELSE 7 END AS wd, count(*)
        FROM fct_orders GROUP BY 1 ORDER BY 1""")
    w = Visual("w", group_by=(Col("Orders", "wk"),), values=(("n", MeasureRef("Order Count")),))
    assert rows(m, w, wh) == oracle(wh, """
        SELECT order_date - CAST(isodow(order_date) - 1 AS INTEGER), count(*)
        FROM fct_orders GROUP BY 1 ORDER BY 1""")


def test_text_concatenation(wh):
    v = Visual("c", group_by=(Col("Customer", "segment"),),
               values=(("x", BinOp("&", Func("upper", (Lit("seg "),)), Lit("x"))),))
    assert rows(commerce_model(), v, wh) == [["Consumer", "SEG x"], ["Enterprise", "SEG x"],
                                             ["SMB", "SEG x"]]
    bad = Visual("c", values=(("x", BinOp("&", NET, Lit("x"))),))
    with pytest.raises(Unsupported, match="& on a non-text value"):
        render.render_visual(commerce_model(), bad, wh)


def test_scalar_functions_at_measure_level(wh):
    v = Visual("c", values=(
        ("abs", Func("abs", (Neg(NET),))), ("round", Func("round", (Divide(NET, Lit(7)), Lit(2)))),
        ("year", Func("year", (Agg("max", Col("Orders", "order_date")),))),
        ("wd", Func("weekday", (Agg("max", Col("Orders", "order_date")),))),
        ("wk", Func("date_trunc_week", (Agg("max", Col("Orders", "order_date")),))),
        ("q", Func("date_trunc_quarter", (Agg("max", Col("Orders", "order_date")),))),
        ("len", Func("len", (Agg("max", Col("Customer", "customer_name")),))),
        ("left", Func("left", (Agg("max", Col("Customer", "customer_name")), Lit(3)))),
        ("trim", Func("trim", (Lit("  a b  "),)))))
    assert rows(commerce_model(), v, wh) == oracle(wh, """
        SELECT abs(-n), round(n / 7, 2), 2026, dayofweek(DATE '2026-02-28') + 1,
               DATE '2026-02-23', DATE '2026-01-01', 11, 'Cus', 'a b'
        FROM (SELECT sum(order_amount) AS n FROM fct_orders WHERE order_status = 'completed')""")


def test_dax_function_of_blank_is_refused(wh):
    v = Visual("c", values=(("x", Func("year", (Lit(None),))),))
    assert rows(commerce_model("sql"), v, wh) == [[None]]
    with pytest.raises(Unsupported, match="BLANK in a function"):
        render.render_visual(commerce_model("dax"), v, wh)


# ------------------------------------------------------------------- modifiers
SEGMENT = Col("Customer", "segment")
REGION = Col("Customer", "region")
STATUS = Col("Orders", "order_status")


def test_remove_filters_on_a_table_clears_its_expanded_table(wh):
    """REMOVEFILTERS(Orders) also removes the predicates on Customer and Date,
    which are in Orders' expanded table; REMOVEFILTERS(Date) does not."""
    m = with_measures(commerce_model(),
                      Measure("All Orders", "Orders",
                              Calculate(NET, (RemoveFilters(("Orders",)),))),
                      Measure("All Dates", "Orders", Calculate(NET, (RemoveFilters(("Date",)),))))
    v = Visual("v", group_by=(SEGMENT,), values=(("net", NET), ("all", MeasureRef("All Orders")),
                                                 ("dates", MeasureRef("All Dates"))),
               filters=(FilterValues(REGION, ("NA",)), FilterValues(Col("Date", "year"), (2025,))))
    assert rows(m, v, wh) == oracle(wh, """
        WITH f AS (SELECT c.segment, c.region, year(o.order_date) AS y, o.order_amount
                   FROM fct_orders o JOIN dim_customer c ON c.customer_id = o.customer_id
                   WHERE o.order_status = 'completed')
        SELECT segment,
               sum(order_amount) FILTER (WHERE region = 'NA' AND y = 2025),
               (SELECT sum(order_amount) FROM f),
               (SELECT sum(order_amount) FROM f f2 WHERE f2.segment = f.segment
                                                     AND f2.region = 'NA')
        FROM f WHERE region = 'NA' GROUP BY segment ORDER BY segment""")


def test_remove_filters_on_columns_and_all(wh):
    m = with_measures(commerce_model(),
                      Measure("No Seg", "Orders",
                              Calculate(NET, (RemoveFilters(columns=(SEGMENT,)),))),
                      Measure("Nothing", "Orders", Calculate(NET, (RemoveFilters(),))))
    v = Visual("v", group_by=(REGION, SEGMENT),
               values=(("net", NET), ("noseg", MeasureRef("No Seg")),
                       ("all", MeasureRef("Nothing"))))
    assert rows(m, v, wh) == oracle(wh, """
        WITH f AS (SELECT c.segment, c.region, o.order_amount
                   FROM fct_orders o JOIN dim_customer c ON c.customer_id = o.customer_id
                   WHERE o.order_status = 'completed')
        SELECT region, segment, sum(order_amount),
               (SELECT sum(order_amount) FROM f f2 WHERE f2.region = f.region),
               (SELECT sum(order_amount) FROM f)
        FROM f GROUP BY ALL ORDER BY 1, 2""")


def test_keep_only_is_allexcept(wh):
    m = with_measures(commerce_model(),
                      Measure("Region Only", "Orders",
                              Calculate(NET, (KeepOnly("Customer", (REGION,)),))),
                      Measure("Orders Except Region", "Orders",
                              Calculate(NET, (KeepOnly("Orders", (REGION,)),))))
    v = Visual("v", group_by=(REGION, SEGMENT),
               values=(("r", MeasureRef("Region Only")), ("o", MeasureRef("Orders Except Region"))),
               filters=(FilterValues(Col("Date", "year"), (2026,)),))
    # KeepOnly(Customer) leaves the Date filter alone; KeepOnly(Orders) drops it too
    assert rows(m, v, wh) == oracle(wh, """
        WITH f AS (SELECT c.segment, c.region, year(o.order_date) AS y, o.order_amount
                   FROM fct_orders o JOIN dim_customer c ON c.customer_id = o.customer_id
                   WHERE o.order_status = 'completed'),
        g AS (SELECT DISTINCT region, segment FROM dim_customer)
        SELECT region, segment,
               (SELECT sum(order_amount) FROM f WHERE f.region = g.region AND y = 2026),
               (SELECT sum(order_amount) FROM f WHERE f.region = g.region)
        FROM g ORDER BY 1, 2""")


def test_calculate_overwrites_but_keepfilters_intersects(wh):
    m = with_measures(commerce_model(),
                      Measure("Net KF", "Orders",
                              Calculate(MeasureRef("Gross Revenue"), (KeepFilters(COMPLETED),))))
    v = Visual("v", group_by=(STATUS,),
               values=(("gross", MeasureRef("Gross Revenue")), ("net", NET),
                       ("kf", MeasureRef("Net KF"))))
    completed = oracle(wh, "SELECT sum(order_amount) FROM fct_orders "
                           "WHERE order_status = 'completed'")[0][0]
    assert rows(m, v, wh) == [
        [s, g, completed, g if s == "completed" else None]
        for s, g in oracle(wh, "SELECT order_status, sum(order_amount) FROM fct_orders "
                               "GROUP BY 1 ORDER BY 1")]


def test_modifiers_apply_in_order(wh):
    """CALCULATE(m, filter, REMOVEFILTERS(table)) as written: the removal wins."""
    m = with_measures(commerce_model(), Measure("x", "Orders", Calculate(
        MeasureRef("Gross Revenue"),
        (FilterValues(SEGMENT, ("SMB",)), RemoveFilters(("Customer",))))))
    v = Visual("v", values=(("x", MeasureRef("x")),))
    assert rows(m, v, wh) == oracle(wh, "SELECT sum(order_amount) FROM fct_orders")


def test_filter_compare_and_negated_values(wh):
    v = Visual("v", group_by=(REGION,), values=(("net", NET),),
               filters=(FilterValues(SEGMENT, ("SMB",), negate=True),
                        FilterCompare(Col("Date", "month"), "<=", 6),
                        FilterCompare(Col("Date", "year"), "=", 2025)))
    assert rows(commerce_model(), v, wh) == oracle(wh, """
        SELECT c.region, sum(o.order_amount)
        FROM fct_orders o JOIN dim_customer c ON c.customer_id = o.customer_id
        WHERE o.order_status = 'completed' AND c.segment <> 'SMB'
          AND month(o.order_date) <= 6 AND year(o.order_date) = 2025
        GROUP BY 1 ORDER BY 1""")


# --------------------------------------------------------- time intelligence
def ts(kind: str) -> TimeShift:
    return TimeShift(NET, Col("Date", "date_day"), kind)


NET_BY_DAY = """(SELECT o.order_date AS day, sum(o.order_amount) AS v FROM fct_orders o
                 WHERE o.order_status = 'completed' GROUP BY 1)"""


def test_every_time_shift_kind_by_month(wh):
    kinds = ("ytd", "qtd", "mtd", "same_period_last_year", "previous_month", "previous_year")
    v = month_visual(*((k, ts(k)) for k in kinds))
    assert rows(commerce_model(), v, wh) == oracle(wh, f"""
        WITH m AS (SELECT year, month, min(date_day) AS lo, max(date_day) AS hi
                   FROM dim_date GROUP BY 1, 2),
        n AS {NET_BY_DAY}
        SELECT year, month,
          (SELECT sum(v) FROM n WHERE day BETWEEN make_date(year, 1, 1) AND hi),
          (SELECT sum(v) FROM n WHERE day BETWEEN date_trunc('quarter', hi) AND hi),
          (SELECT sum(v) FROM n WHERE day BETWEEN date_trunc('month', hi) AND hi),
          (SELECT sum(v) FROM n WHERE day BETWEEN lo - INTERVAL 1 YEAR AND hi - INTERVAL 1 YEAR),
          (SELECT sum(v) FROM n WHERE day >= lo - INTERVAL 1 MONTH AND day < lo),
          (SELECT sum(v) FROM n WHERE year(day) = year - 1)
        FROM m ORDER BY 1, 2""")


def test_time_shift_over_week_and_card_contexts(wh):
    v = Visual("v", group_by=(Col("Date", "week_start"),), values=(("mtd", ts("mtd")),))
    assert rows(commerce_model(), v, wh) == oracle(wh, f"""
        WITH w AS (SELECT week_start, max(date_day) AS hi FROM dim_date GROUP BY 1),
        n AS {NET_BY_DAY}
        SELECT * FROM (
            SELECT week_start,
                   (SELECT sum(v) FROM n WHERE day BETWEEN date_trunc('month', hi) AND hi) AS x
            FROM w) WHERE x IS NOT NULL ORDER BY 1""")
    card = Visual("c", values=tuple((k, ts(k)) for k in ("ytd", "previous_month", "previous_year",
                                                         "same_period_last_year")),
                  filters=(FilterValues(Col("Date", "year"), (2026,)),))
    assert rows(commerce_model(), card, wh) == oracle(wh, f"""
        WITH n AS {NET_BY_DAY}
        SELECT (SELECT sum(v) FROM n WHERE year(day) = 2026),
               (SELECT sum(v) FROM n WHERE day BETWEEN '2025-12-01' AND '2025-12-31'),
               (SELECT sum(v) FROM n WHERE year(day) = 2025),
               (SELECT sum(v) FROM n WHERE day BETWEEN '2025-01-01' AND '2025-02-28')""")


def test_same_period_last_year_maps_each_date(wh):
    """Weekend days only: the shifted set is those dates minus a year, not a range."""
    v = Visual("v", values=(("ly", ts("same_period_last_year")),),
               filters=(FilterValues(Col("Date", "is_weekend"), (True,)),
                        FilterValues(Col("Date", "year"), (2026,))))
    assert rows(commerce_model(), v, wh) == oracle(wh, f"""
        WITH n AS {NET_BY_DAY}
        SELECT sum(v) FROM n WHERE day IN (
            SELECT CAST(date_day - INTERVAL 1 YEAR AS DATE) FROM dim_date
            WHERE is_weekend AND year = 2026)""")


def test_nested_time_shift(wh):
    """YTD of last year: the inner shift sees the outer shift's dates."""
    m = with_measures(commerce_model(), Measure("YTD LY", "Orders", TimeShift(
        MeasureRef("Revenue YTD"), Col("Date", "date_day"), "same_period_last_year")))
    v = replace(month_visual(("x", MeasureRef("YTD LY"))),
                filters=(FilterValues(Col("Date", "year"), (2026,)),))
    assert rows(m, v, wh) == oracle(wh, f"""
        WITH n AS {NET_BY_DAY}
        SELECT 2026, 1, (SELECT sum(v) FROM n WHERE day BETWEEN '2025-01-01' AND '2025-01-31')
        UNION ALL
        SELECT 2026, 2, (SELECT sum(v) FROM n WHERE day BETWEEN '2025-01-01' AND '2025-02-28')""")


# ---------------------------------------------------------------- iterators
def test_iterator_with_related_list_price_revenue(wh):
    v = Visual("v", group_by=(Col("Product", "category"), SEGMENT),
               values=(("lpr", MeasureRef("List Price Revenue")), ("units", MeasureRef("Units"))))
    assert rows(commerce_model(), v, wh) == oracle(wh, """
        SELECT p.category, c.segment, sum(l.quantity * p.unit_price), sum(l.quantity)
        FROM fct_order_lines l JOIN dim_product p ON p.product_id = l.product_id
             JOIN fct_orders o ON o.order_id = l.order_id
             JOIN dim_customer c ON c.customer_id = o.customer_id
        GROUP BY 1, 2 ORDER BY 1, 2""")


def test_iterator_functions(wh):
    m = with_measures(commerce_model(), *(
        Measure(f"it {f}", "Orders", IterAgg(f, "Orders", BinOp(
            "-", Col("Orders", "order_amount"), Col("Orders", "shipping_amount"))))
        for f in ("sum", "avg", "min", "max", "count")))
    v = Visual("v", group_by=(STATUS,),
               values=tuple((f, MeasureRef(f"it {f}")) for f in ("sum", "avg", "min", "max",
                                                                 "count")))
    assert rows(m, v, wh) == oracle(wh, """
        SELECT order_status, sum(order_amount - shipping_amount),
               avg(order_amount - shipping_amount), min(order_amount - shipping_amount),
               max(order_amount - shipping_amount), count(*)
        FROM fct_orders GROUP BY 1 ORDER BY 1""")


def test_every_aggregate_function(wh):
    o = "Orders"
    v = Visual("v", group_by=(SEGMENT,), values=(
        ("sum", Agg("sum", Col(o, "shipping_amount"))),
        ("avg", Agg("avg", Col(o, "shipping_amount"))),
        ("min", Agg("min", Col(o, "order_date"))), ("max", Agg("max", Col(o, "order_status"))),
        ("count", Agg("count", Col(o, "order_id"))),
        ("distinct", Agg("count_distinct", Col(o, "customer_id"))),
        ("rows", Agg("count_rows", table=o))))
    assert rows(commerce_model(), v, wh) == oracle(wh, """
        SELECT c.segment, sum(shipping_amount), avg(shipping_amount), min(order_date),
               max(order_status), count(order_id), count(DISTINCT o.customer_id), count(*)
        FROM fct_orders o JOIN dim_customer c ON c.customer_id = o.customer_id
        GROUP BY 1 ORDER BY 1""")


def test_count_over_no_rows_is_blank(wh):
    v = Visual("c", values=(("rows", Agg("count_rows", table="Orders")),
                            ("n", Agg("count", Col("Orders", "order_id"))),
                            ("d", Agg("count_distinct", Col("Orders", "order_id")))),
               filters=(FilterValues(STATUS, ("no such status",)),))
    assert rows(commerce_model(), v, wh) == [[None, None, None]]


def test_dimension_measure_is_not_filtered_by_a_fact_column(wh):
    """Relationships filter one -> many only: order_status does not reach Customer."""
    v = Visual("v", group_by=(STATUS,),
               values=(("customers", Agg("count_rows", table="Customer")),
                       ("orders", MeasureRef("Order Count"))))
    assert rows(commerce_model(), v, wh) == oracle(wh, """
        SELECT order_status, (SELECT count(*) FROM dim_customer), count(*)
        FROM fct_orders GROUP BY 1 ORDER BY 1""")


# ------------------------------------------------- ordering, top_n, auto-exist
def test_order_by_ascending_value_then_top_n(wh):
    v = Visual("v", group_by=(Col("Customer", "customer_name"),), values=(("net", NET),),
               order_by=(("net", False),), top_n=2)
    assert rows(commerce_model(), v, wh) == oracle(wh, """
        SELECT c.customer_name, sum(o.order_amount)
        FROM fct_orders o JOIN dim_customer c ON c.customer_id = o.customer_id
        WHERE o.order_status = 'completed' GROUP BY 1 ORDER BY 2, 1 LIMIT 2""")


def test_top_n_refuses_ties_at_the_boundary(wh):
    """DAX's TOPN keeps every row tied at the cut; a silent cut would differ.
    Quarter 1 spans two years (2 distinct years), quarters 2-4 one each."""
    years = Agg("count_distinct", Col("Date", "year"))
    v = Visual("v", group_by=(Col("Date", "quarter"),), values=(("years", years),),
               order_by=(("years", True),))
    assert rows(commerce_model(), replace(v, top_n=1), wh) == [[1, 2]]       # no tie at the cut
    assert rows(commerce_model(), replace(v, top_n=4), wh) == [[1, 2], [2, 1], [3, 1], [4, 1]]
    with pytest.raises(Unsupported, match="TOPN with ties at the boundary"):
        render.render_visual(commerce_model(), replace(v, top_n=2), wh)


def test_order_by_group_column_descending(wh):
    v = Visual("v", group_by=MONTHS, values=(("net", NET),),
               order_by=(("year", True), ("month", False)))
    assert rows(commerce_model(), v, wh) == oracle(wh, """
        SELECT d.year, d.month, sum(o.order_amount)
        FROM fct_orders o JOIN dim_date d ON d.date_day = o.order_date
        WHERE o.order_status = 'completed' GROUP BY 1, 2 ORDER BY 1 DESC, 2""")


def test_auto_exist_drops_all_blank_groups(wh):
    """Days with no completed order drop out; a value that never blanks keeps them."""
    v = Visual("v", group_by=(Col("Date", "date_day"),), values=(("net", NET),))
    expected = oracle(wh, """
        SELECT o.order_date, sum(o.order_amount) FROM fct_orders o
        WHERE o.order_status = 'completed' GROUP BY 1 ORDER BY 1""")
    assert rows(commerce_model(), v, wh) == expected
    assert len(expected) < 424
    v2 = replace(v, values=(("net", NET), ("one", Lit(1))))
    assert len(rows(commerce_model(), v2, wh)) == 424


def test_groups_cross_tables_and_auto_exist_within_a_table(wh):
    """Customer columns combine by existence; Date x Customer is a cross join that
    the blank values then prune."""
    v = Visual("v", group_by=(Col("Date", "year"), REGION, SEGMENT), values=(("net", NET),))
    assert rows(commerce_model(), v, wh) == oracle(wh, """
        SELECT year(o.order_date), c.region, c.segment, sum(o.order_amount)
        FROM fct_orders o JOIN dim_customer c ON c.customer_id = o.customer_id
        WHERE o.order_status = 'completed' GROUP BY ALL ORDER BY 1, 2, 3""")


def test_groups_measures_drops_and_rows_keeps_all_blank_groups(wh):
    """Completed-order revenue that INTERSECTS a refunded-status filter
    (KeepFilters - the commerce model's Net Revenue would overwrite it) is blank
    everywhere: Power BI ("measures") shows nothing, Tableau / Sigma ("rows")
    show every group the source - Orders - has rows for, blank."""
    m = with_measures(commerce_model(), Measure("Net KF", "Orders", Calculate(
        MeasureRef("Gross Revenue"), (KeepFilters(COMPLETED),))))
    v = Visual("v", group_by=(SEGMENT,), values=(("net", MeasureRef("Net KF")),),
               filters=(FilterValues(STATUS, ("refunded",)),))
    assert rows(m, v, wh) == []
    segments = oracle(wh, """
        SELECT DISTINCT c.segment, NULL FROM fct_orders o
        LEFT JOIN dim_customer c ON c.customer_id = o.customer_id
        WHERE o.order_status = 'refunded' ORDER BY 1""")
    assert len(segments) == 3
    assert rows(m, replace(v, groups="rows"), wh) == segments
    # a filter that empties two segments of rows removes them under "rows" too
    v26 = replace(v, groups="rows", filters=v.filters + (
        FilterValues(Col("Date", "year"), (2026,)),))
    assert rows(m, v26, wh) == [["Enterprise", None]]
    # order and top_n see the blank rows: blank sorts lowest, and blank rows at
    # the cut tie
    mixed = replace(v, groups="rows", order_by=(("n", True),),
                    values=(("net", MeasureRef("Net KF")), ("n", MeasureRef("Order Count"))))
    counts = oracle(wh, """
        SELECT c.segment, NULL, count(*) FROM fct_orders o
        JOIN dim_customer c ON c.customer_id = o.customer_id
        WHERE o.order_status = 'refunded' GROUP BY 1 ORDER BY 3 DESC, 1""")
    assert rows(m, mixed, wh) == counts
    with pytest.raises(Unsupported, match="TOPN with ties"):
        render.render_visual(m, replace(v, groups="rows", top_n=1,
                                        order_by=(("net", True),)), wh)


def test_rows_groups_come_from_the_values_source(wh, orphan_wh):
    """Under "rows" the base is the table the values aggregate (Orders): days
    with an order, not every day of the Date table; under "measures" the
    grouping columns' own table (Date) is the base."""
    day = Col("Date", "date_day")
    v = Visual("v", group_by=(day,), values=(("net", NET),), groups="rows")
    assert rows(commerce_model(), v, wh) == oracle(wh, """
        SELECT order_date, sum(order_amount) FILTER (WHERE order_status = 'completed')
        FROM fct_orders GROUP BY 1 ORDER BY 1""")
    one = Visual("v", group_by=(day,), values=(("one", Lit(1)),))
    assert len(rows(commerce_model(), one, wh)) == 424
    assert len(rows(commerce_model(), replace(one, groups="rows"), wh)) == 424  # no aggregate
    # an unmatched key is a NULL group of the source's rows
    seg = Visual("v", group_by=(SEGMENT,), values=(("n", MeasureRef("Order Count")),),
                 groups="rows")
    assert rows(commerce_model(), seg, orphan_wh) == oracle(orphan_wh, """
        SELECT c.segment, count(*) FROM fct_orders o
        LEFT JOIN dim_customer c ON c.customer_id = o.customer_id
        GROUP BY 1 ORDER BY 1 NULLS FIRST""")


def test_rows_groups_over_two_sources_is_refused(wh):
    v = Visual("v", group_by=(SEGMENT,), values=(("net", NET), ("u", MeasureRef("Units"))),
               groups="rows")
    with pytest.raises(Unsupported, match="groups over more than one source table"):
        render.render_visual(commerce_model(), v, wh)
    assert rows(commerce_model(), replace(v, groups="measures"), wh)


def test_candidates_are_the_base_tables_combinations(wh):
    """Grouping by Orders x Date columns: the base table is Orders, so the
    candidates are the (status, day) pairs that occur - not the cross join."""
    v = Visual("v", group_by=(STATUS, Col("Date", "date_day")), values=(("one", Lit(1)),))
    expected = oracle(wh, """
        SELECT DISTINCT order_status, order_date, 1 FROM fct_orders ORDER BY 1, 2""")
    assert len(expected) == 142
    assert rows(commerce_model(), v, wh) == expected
    assert rows(commerce_model(), replace(v, groups="rows"), wh) == expected


def test_visual_with_only_group_by_lists_existing_values(wh):
    v = Visual("v", group_by=(REGION, SEGMENT), filters=(FilterValues(REGION, ("NA",)),))
    assert render.render_visual(commerce_model(), v, wh) == {
        "columns": ["region", "segment"],
        "rows": oracle(wh, "SELECT DISTINCT region, segment FROM dim_customer "
                           "WHERE region = 'NA' ORDER BY 1, 2")}


# -------------------------------------- the blank row, native SQL, calc columns
@pytest.fixture(scope="module")
def orphan_wh(tmp_path_factory) -> Path:
    """The fixture plus two completed orders whose customer is not in dim_customer
    (99, NULL), a cancelled one dated outside dim_date, and an order line whose
    order does not exist."""
    path = tmp_path_factory.mktemp("orphan") / "orphan.duckdb"
    write_fixture(path)
    with duckdb.connect(str(path)) as con:
        con.execute("INSERT INTO warehouse.fct_orders VALUES "
                    "(9001, DATE '2025-03-03', 99, 'completed', 100.00, 1.00), "
                    "(9002, DATE '2025-03-04', NULL, 'completed', 50.00, 1.00), "
                    "(9003, DATE '2030-01-01', 1, 'cancelled', 10.00, 1.00)")
        con.execute("INSERT INTO warehouse.fct_order_lines VALUES "
                    "(9001, 777, 1, 2, 50.00, DATE '2025-03-03', 1)")
    return path


def test_unmatched_keys_show_as_the_blank_group(orphan_wh):
    v = Visual("v", group_by=(SEGMENT,), values=(("net", NET),))
    expected = oracle(orphan_wh, """
        SELECT c.segment, sum(o.order_amount)
        FROM fct_orders o LEFT JOIN dim_customer c ON c.customer_id = o.customer_id
        WHERE o.order_status = 'completed' GROUP BY 1 ORDER BY 1 NULLS FIRST""")
    assert expected[0] == [None, "150.00"]
    assert rows(commerce_model(), v, orphan_wh) == expected
    # a value filter excludes the blank row, a negated one keeps it
    keep = replace(v, filters=(FilterValues(SEGMENT, ("SMB",), negate=True),))
    assert rows(commerce_model(), keep, orphan_wh) == [r for r in expected if r[0] != "SMB"]
    only = replace(v, filters=(FilterValues(SEGMENT, ("SMB",)),))
    assert rows(commerce_model(), only, orphan_wh) == [r for r in expected if r[0] == "SMB"]
    blank = replace(v, filters=(FilterValues(SEGMENT, (None,)),))
    assert rows(commerce_model(), blank, orphan_wh) == [[None, "150.00"]]


def test_two_hop_blank_row(orphan_wh):
    """An order line whose order is missing reaches no Customer either."""
    v = Visual("v", group_by=(SEGMENT,), values=(("units", MeasureRef("Units")),))
    assert rows(commerce_model(), v, orphan_wh) == oracle(orphan_wh, """
        SELECT c.segment, sum(l.quantity)
        FROM fct_order_lines l LEFT JOIN fct_orders o ON o.order_id = l.order_id
             LEFT JOIN dim_customer c ON c.customer_id = o.customer_id
        GROUP BY 1 ORDER BY 1 NULLS FIRST""")


def fanout_model() -> Model:
    """A Tableau-style physical join lowered to SQL: orders x lines repeats each
    order once per line, so a sum of order_amount double counts - which is what the
    vendor shows, so the engine must too."""
    base = commerce_model()
    joined = Table("Order Facts", None, (
        Column("order_id", "order_id", "int"), Column("customer_id", "customer_id", "int"),
        Column("amount", "order_amount", "decimal"), Column("qty", "quantity", "int")),
        source_sql="SELECT o.order_id, o.customer_id, o.order_amount, l.quantity "
                   "FROM warehouse.fct_orders o JOIN warehouse.fct_order_lines l "
                   "ON l.order_id = o.order_id")
    return replace(base, tables=base.tables + (joined,),
                   relationships=base.relationships + (
                       Relationship("Order Facts", "customer_id", "Customer", "customer_id"),),
                   measures=base.measures + (
                       Measure("Fanned Amount", "Order Facts",
                               Agg("sum", Col("Order Facts", "amount"))),
                       Measure("Fanned Rows", "Order Facts",
                               Agg("count_rows", table="Order Facts"))))


def test_native_sql_table_fans_out_faithfully(wh):
    v = Visual("v", group_by=(SEGMENT,), values=(("amount", MeasureRef("Fanned Amount")),
                                                 ("rows", MeasureRef("Fanned Rows")),
                                                 ("gross", MeasureRef("Gross Revenue"))))
    got = rows(fanout_model(), v, wh)
    assert got == oracle(wh, """
        WITH j AS (SELECT o.customer_id, o.order_amount FROM fct_orders o
                   JOIN fct_order_lines l ON l.order_id = o.order_id),
        g AS (SELECT customer_id, sum(order_amount) AS gross FROM fct_orders GROUP BY 1)
        SELECT c.segment, sum(j.order_amount), count(*),
               (SELECT sum(gross) FROM g JOIN dim_customer c2 USING (customer_id)
                WHERE c2.segment = c.segment)
        FROM j JOIN dim_customer c ON c.customer_id = j.customer_id GROUP BY 1 ORDER BY 1""")
    assert all(Decimal(r[1]) > Decimal(r[3]) for r in got)        # double counted


def calc_model() -> Model:
    base = commerce_model()
    tables = []
    for t in base.tables:
        if t.name == "Orders":
            t = replace(t, columns=t.columns + (
                Column("net_amount", "", "decimal", expr=BinOp(
                    "-", Col("Orders", "order_amount"), Col("Orders", "shipping_amount"))),
                Column("size", "", "string", expr=If(
                    BinOp(">=", Col("Orders", "net_amount"), Lit(100)), Lit("big"), Lit("small"))),
                Column("segment_status", "", "string", expr=BinOp(
                    "&", Related("Customer", "segment"), Col("Orders", "order_status")))))
        if t.name == "Order Lines":
            t = replace(t, columns=t.columns + (
                Column("list_value", "", "decimal", expr=BinOp(
                    "*", Col("Order Lines", "quantity"), Related("Product", "unit_price"))),
                Column("seg", "", "string", expr=Related("Customer", "segment"))))
        tables.append(t)
    return replace(base, tables=tuple(tables), measures=base.measures + (
        Measure("Net Amount", "Orders", Agg("sum", Col("Orders", "net_amount"))),
        Measure("List Value", "Order Lines", Agg("sum", Col("Order Lines", "list_value")))))


def test_calculated_columns(wh):
    v = Visual("v", group_by=(Col("Orders", "size"),),
               values=(("net", MeasureRef("Net Amount")), ("n", MeasureRef("Order Count"))))
    assert rows(calc_model(), v, wh) == oracle(wh, """
        SELECT CASE WHEN order_amount - shipping_amount >= 100 THEN 'big' ELSE 'small' END,
               sum(order_amount - shipping_amount), count(*)
        FROM fct_orders GROUP BY 1 ORDER BY 1""")
    v2 = Visual("v", group_by=(Col("Order Lines", "seg"),),
                values=(("lv", MeasureRef("List Value")),
                        ("lpr", MeasureRef("List Price Revenue"))))
    got = rows(calc_model(), v2, wh)
    assert got == oracle(wh, """
        SELECT c.segment, sum(l.quantity * p.unit_price), sum(l.quantity * p.unit_price)
        FROM fct_order_lines l JOIN dim_product p ON p.product_id = l.product_id
             JOIN fct_orders o ON o.order_id = l.order_id
             JOIN dim_customer c ON c.customer_id = o.customer_id GROUP BY 1 ORDER BY 1""")
    seg_status = Col("Orders", "segment_status")
    v3 = Visual("v", group_by=(seg_status,), values=(("n", MeasureRef("Order Count")),),
                filters=(FilterValues(seg_status, ("SMBcompleted", "SMBrefunded")),))
    assert rows(calc_model(), v3, wh) == oracle(wh, """
        SELECT c.segment || o.order_status, count(*)
        FROM fct_orders o JOIN dim_customer c ON c.customer_id = o.customer_id
        WHERE c.segment = 'SMB' AND o.order_status IN ('completed', 'refunded')
        GROUP BY 1 ORDER BY 1""")


# ----------------------------------------------------------------- SQL visuals
def test_sql_visual_returns_the_querys_rows(wh):
    v = Visual("legacy", sql="select segment, count(*) as n from warehouse.dim_customer "
                             "group by 1 order by 1")
    assert render.render_visual(Model("empty", ()), v, wh) == {
        "columns": ["segment", "n"],
        "rows": [["Consumer", 2], ["Enterprise", 2], ["SMB", 2]]}


# ------------------------------------------------------------- model errors
def add(model: Model, name: str, table: str, expr) -> Model:
    return with_measures(model, Measure(name, table, expr))


def drop_measure(model: Model, name: str) -> Model:
    return replace(model, measures=tuple(m for m in model.measures if m.name != name))


def table_named(model: Model, name: str) -> Table:
    return next(t for t in model.tables if t.name == name)


def swap_table(model: Model, table: Table) -> Model:
    return replace(model, tables=tuple(table if t.name == table.name else t for t in model.tables))


MODEL_ERRORS = {
    "unknown measure": lambda m: (add(m, "x", "Orders", MeasureRef("Nope")), ()),
    "unknown table": lambda m: (
        add(m, "x", "Nope", Agg("sum", Col("Orders", "order_amount"))), ()),
    "unknown column": lambda m: (add(m, "x", "Orders", Agg("sum", Col("Orders", "nope"))), ()),
    "unknown iterator table": lambda m: (
        add(m, "x", "Orders", IterAgg("sum", "Nope", Lit(1))), ()),
    "measure cycle": lambda m: (with_measures(drop_measure(m, "Gross Revenue"), Measure(
        "Gross Revenue", "Orders", BinOp("+", MeasureRef("AOV"), Lit(1)))), ()),
    "self cycle": lambda m: (add(m, "x", "Orders", MeasureRef("x")), ()),
    "relationship column missing": lambda m: (replace(m, relationships=m.relationships + (
        Relationship("Orders", "nope", "Customer", "customer_id"),)), ()),
    "relationship table missing": lambda m: (replace(m, relationships=m.relationships + (
        Relationship("Orders", "customer_id", "Nope", "customer_id"),)), ()),
    "date table without date column": lambda m: (swap_table(m, replace(
        table_named(m, "Date"), date_column=None)), ()),
    "date column not a date": lambda m: (swap_table(m, replace(
        table_named(m, "Date"), date_column="year")), ()),
    "time shift on a non-date table": lambda m: (add(m, "x", "Orders", TimeShift(
        NET, Col("Orders", "order_date"), "ytd")), ()),
    "table with no source": lambda m: (
        swap_table(m, replace(table_named(m, "Product"), source=None)), ()),
    "table with two sources": lambda m: (swap_table(m, replace(table_named(m, "Product"),
                                                              source_sql="select 1")), ()),
    "duplicate measure": lambda m: (add(m, "AOV", "Orders", Lit(1)), ()),
    "column outside an aggregate": lambda m: (
        add(m, "x", "Orders", Col("Orders", "order_amount")), ()),
    "related outside a row context": lambda m: (
        add(m, "x", "Orders", Related("Customer", "segment")), ()),
    "related not reachable": lambda m: (add(m, "x", "Orders", IterAgg(
        "sum", "Orders", Related("Product", "unit_price"))), ()),
    "column of another table in a row context": lambda m: (add(m, "x", "Orders", IterAgg(
        "sum", "Orders", Col("Customer", "customer_id"))), ()),
    "relationship cycle": lambda m: (replace(m, relationships=m.relationships + (
        Relationship("Customer", "customer_id", "Orders", "order_id"),)), ()),
    "ambiguous paths": lambda m: (replace(m, relationships=m.relationships + (
        Relationship("Order Lines", "customer_id", "Customer", "customer_id"),)), ()),
    "bad function arity": lambda m: (add(m, "x", "Orders", Func("abs", ())), ()),
    "keeponly column outside the table": lambda m: (add(m, "x", "Orders", Calculate(
        NET, (KeepOnly("Customer", (Col("Product", "category"),)),))), ()),
    "report for another model": lambda m: (m, (Report("r", "Other", ()),)),
    "visual mixing sql and values": lambda m: (m, (Report("r", "Commerce", (Page("p", (
        Visual("v", values=(("n", NET),), sql="select 1"),)),)),)),
    "visual mixing sql and group_by": lambda m: (m, (Report("r", "Commerce", (Page("p", (
        Visual("v", group_by=(SEGMENT,), sql="select 1"),)),)),)),
    "group_by no single fact reaches": lambda m: (replace(m, relationships=tuple(
        replace(r, active=False) if r.to_table == "Orders" else r for r in m.relationships)),
        (Report("r", "Commerce", (Page("p", (Visual("v", group_by=(
            SEGMENT, Col("Product", "category")), values=(("u", MeasureRef("Units")),)),)),)),)),
    "visual group_by unknown column": lambda m: (m, (Report("r", "Commerce", (Page("p", (
        Visual("v", group_by=(Col("Customer", "nope"),), values=(("n", NET),)),)),)),)),
    "visual filter that is not a filter": lambda m: (m, (Report("r", "Commerce", (Page("p", (
        Visual("v", values=(("n", NET),), filters=(RemoveFilters(),)),)),)),)),
    "visual order_by unknown label": lambda m: (m, (Report("r", "Commerce", (Page("p", (
        Visual("v", values=(("n", NET),), order_by=(("nope", True),)),)),)),)),
    "visual duplicate labels": lambda m: (m, (Report("r", "Commerce", (Page("p", (
        Visual("v", group_by=(SEGMENT,), values=(("segment", NET),)),)),)),)),
    "visual with nothing": lambda m: (m, (Report("r", "Commerce", (Page("p", (Visual("v"),)),)),)),
    "visual groups mode": lambda m: (m, (Report("r", "Commerce", (Page("p", (
        Visual("v", values=(("n", NET),), groups="columns"),)),)),)),
    "rows over a source that does not reach a group column": lambda m: (m, (Report(
        "r", "Commerce", (Page("p", (Visual("v", group_by=(Col("Product", "category"),),
                                            values=(("n", NET),), groups="rows"),)),)),)),
    "duplicate visual names": lambda m: (m, (Report("r", "Commerce", (Page("p", (
        Visual("v", values=(("n", NET),)), Visual("v", values=(("n", NET),)))),)),)),
}


@pytest.mark.parametrize("case", sorted(MODEL_ERRORS))
def test_model_errors(case):
    model, reports = MODEL_ERRORS[case](commerce_model())
    with pytest.raises(ModelError):
        render.validate(model, reports)


def test_the_commerce_model_and_report_validate():
    render.validate(commerce_model(), (executive_report(),))
    render.validate(commerce_model("dax"), (executive_report(),))


def test_render_visual_validates_first(wh):
    with pytest.raises(ModelError):
        render.render_visual(commerce_model(), Visual("v", values=(("n", MeasureRef("Nope")),)), wh)


def test_duplicate_keys_on_the_one_side_are_refused(tmp_path):
    path = write_fixture(tmp_path / "dup.duckdb")
    with duckdb.connect(str(path)) as con:
        con.execute("INSERT INTO warehouse.dim_customer VALUES "
                    "(1, 'Customer 01 again', 'SMB', 'NA', DATE '2024-01-01')")
    with pytest.raises(ModelError, match="duplicate"):
        render.render_visual(commerce_model(), Visual("v", group_by=(SEGMENT,),
                                                      values=(("n", NET),)), path)


def test_missing_warehouse_relation_is_a_model_error(wh):
    m = swap_table(commerce_model(), replace(table_named(commerce_model(), "Product"),
                                             source="warehouse.nope"))
    with pytest.raises(ModelError, match="nope"):
        render.render_visual(m, by_name(executive_report(), "Top Products"), wh)


# ------------------------------------------------------------- unsupported
def test_bidirectional_cross_filter_is_refused_when_traversed(wh):
    m = commerce_model()
    m = replace(m, relationships=tuple(
        replace(r, cross_filter="both") if r.to_table == "Customer" else r
        for r in m.relationships))
    customers = Visual("v", group_by=(STATUS,),
                       values=(("c", Agg("count_rows", table="Customer")),))
    with pytest.raises(Unsupported, match="bidirectional cross-filter"):
        render.render_visual(m, customers, wh)
    # The Date filter would reach Customer through Orders, which changes the
    # regions that exist (auto-exist) - traversed, so refused too.
    with pytest.raises(Unsupported, match="bidirectional cross-filter"):
        render.render_visual(m, by_name(executive_report(), "Revenue by Region 2026"), wh)
    # not traversed: every filter flows one -> many, so this is exact
    for name in ("Revenue by Segment", "KPIs"):
        assert rows(m, by_name(executive_report(), name), wh) == \
            rows(commerce_model(), by_name(executive_report(), name), wh)


def test_inactive_relationships_are_ignored(wh):
    m = commerce_model()
    m = replace(m, relationships=tuple(replace(r, active=False) if r.to_table == "Customer"
                                       else r for r in m.relationships))
    v = Visual("v", group_by=(SEGMENT,), values=(("n", NET),))
    total = oracle(wh, "SELECT sum(order_amount) FROM fct_orders "
                       "WHERE order_status = 'completed'")[0][0]
    assert rows(m, v, wh) == [["Consumer", total], ["Enterprise", total], ["SMB", total]]


@pytest.mark.parametrize("inner", [
    MeasureRef("Units"),
    Calculate(Col("Order Lines", "quantity"), (COMPLETED,)),
    TimeShift(Col("Order Lines", "quantity"), Col("Date", "date_day"), "ytd"),
])
def test_context_transition_is_refused(wh, inner):
    m = with_measures(commerce_model(),
                      Measure("x", "Order Lines", IterAgg("sum", "Order Lines", inner)))
    with pytest.raises(Unsupported, match="context transition"):
        render.render_visual(m, Visual("v", values=(("x", MeasureRef("x")),)), wh)


def test_aggregate_inside_an_iterator_is_refused(wh):
    m = with_measures(commerce_model(), Measure("x", "Order Lines", IterAgg(
        "sum", "Order Lines", BinOp("/", Col("Order Lines", "quantity"),
                                    Agg("sum", Col("Order Lines", "quantity"))))))
    with pytest.raises(Unsupported, match="aggregate inside an iterator"):
        render.render_visual(m, Visual("v", values=(("x", MeasureRef("x")),)), wh)


def test_aggregate_in_a_calculated_column_is_refused(wh):
    base = commerce_model()
    orders = table_named(base, "Orders")
    m = swap_table(base, replace(orders, columns=orders.columns + (
        Column("share", "", "float", expr=Agg("sum", Col("Orders", "order_amount"))),)))
    v = Visual("v", group_by=(Col("Orders", "share"),), values=(("n", NET),))
    with pytest.raises(Unsupported, match="aggregate in a calculated column"):
        render.render_visual(m, v, wh)


def test_dax_row_level_division_by_zero_is_refused(wh):
    base = commerce_model("dax")
    m = with_measures(base, Measure("x", "Orders", IterAgg("sum", "Orders", BinOp(
        "/", Col("Orders", "order_amount"), BinOp("-", Col("Orders", "order_id"),
                                                  Col("Orders", "order_id"))))))
    with pytest.raises(Unsupported, match="division by BLANK or zero"):
        render.render_visual(m, Visual("v", values=(("x", MeasureRef("x")),)), wh)


@pytest.mark.parametrize("col,value", [(SEGMENT, ""), (Col("Orders", "customer_id"), 0),
                                       (Col("Date", "is_weekend"), False)])
def test_dax_filter_values_matching_blank_as_zero_is_refused(orphan_wh, wh, col, value):
    """DAX's `=` matches BLANK to 0, "" and FALSE: refused where the filtered
    column holds a blank (an orphan order's customer, segment, or date), fine
    where it does not. Negated lists too: DAX would drop the blanks."""
    v = Visual("v", values=(("n", Agg("count_rows", table="Orders")),),
               filters=(FilterValues(col, (value,)),))
    with pytest.raises(Unsupported, match="BLANK matched by 0"):
        render.render_visual(commerce_model("dax"), v, orphan_wh)
    neg = replace(v, filters=(FilterValues(col, (value,), negate=True),))
    with pytest.raises(Unsupported, match="BLANK matched by 0"):
        render.render_visual(commerce_model("dax"), neg, orphan_wh)
    by = Visual("v", group_by=(col,), values=(("n", NET),),
                filters=(FilterValues(col, (value,), negate=True),))
    with pytest.raises(Unsupported, match="BLANK matched by 0"):
        render.render_visual(commerce_model("dax"), by, orphan_wh)
    # the clean fixture holds no blank there: nothing to refuse, same as "sql"
    assert rows(commerce_model("dax"), v, wh) == rows(commerce_model("sql"), v, wh)
    other = replace(v, filters=(FilterValues(SEGMENT, ("SMB",)),))
    assert rows(commerce_model("dax"), other, orphan_wh) == \
        rows(commerce_model("sql"), other, orphan_wh)


def test_dax_filter_compare_over_blanks_is_refused(orphan_wh):
    v = Visual("v", values=(("n", NET),), filters=(FilterCompare(SEGMENT, ">", "A"),))
    assert rows(commerce_model("sql"), v, orphan_wh) == oracle(orphan_wh, """
        SELECT sum(o.order_amount) FROM fct_orders o
        JOIN dim_customer c ON c.customer_id = o.customer_id
        WHERE o.order_status = 'completed' AND c.segment > 'A'""")
    with pytest.raises(Unsupported, match="comparison with BLANK"):
        render.render_visual(commerce_model("dax"), v, orphan_wh)


# ------------------------------------------------------------- performance
def test_executive_report_is_fast(wh):
    start = time.perf_counter()
    render.render_report(commerce_model(), executive_report(), wh)
    elapsed = time.perf_counter() - start
    print(f"\nexecutive_report on the fixture: {elapsed:.3f}s")
    assert elapsed < 5


def write_big_fixture(path: Path, orders: int = 50_000) -> Path:
    """The Commerce schema at ~50k orders / ~100k lines, 2000 customers, three years."""
    write_fixture(path)
    with duckdb.connect(str(path)) as con:
        con.execute("DELETE FROM warehouse.fct_orders; DELETE FROM warehouse.fct_order_lines; "
                    "DELETE FROM warehouse.dim_customer; DELETE FROM warehouse.dim_date")
        con.execute("""
            INSERT INTO warehouse.dim_date
            SELECT d, year(d), quarter(d), month(d), monthname(d),
                   CAST(date_trunc('week', d) AS DATE), dayofweek(d) IN (0, 6)
            FROM (SELECT CAST(unnest(generate_series(DATE '2023-01-01', DATE '2025-12-31',
                                                     INTERVAL 1 DAY)) AS DATE) AS d)""")
        con.execute("""
            INSERT INTO warehouse.dim_customer
            SELECT i, 'Customer ' || i, ['Enterprise', 'SMB', 'Consumer'][i % 3 + 1],
                   ['NA', 'EMEA', 'APAC'][(i // 3) % 3 + 1],
                   DATE '2022-01-01' + CAST(i % 365 AS INTEGER)
            FROM range(1, 2001) t(i)""")
        con.execute(f"""
            INSERT INTO warehouse.fct_orders
            SELECT i, DATE '2023-01-01' + CAST(i % 1095 AS INTEGER), i % 2000 + 1,
                   CASE WHEN i % 11 = 0 THEN 'refunded' WHEN i % 17 = 0 THEN 'cancelled'
                        ELSE 'completed' END,
                   CAST((i * 37) % 50000 AS DECIMAL(12, 2)) / 100,
                   CAST((i * 7) % 1500 AS DECIMAL(12, 2)) / 100
            FROM range(1, {orders + 1}) t(i)""")
        con.execute("""
            INSERT INTO warehouse.fct_order_lines
            SELECT row_number() OVER (ORDER BY o.order_id, k), o.order_id, (o.order_id + k) % 4 + 1,
                   1 + (o.order_id + k) % 4, o.order_amount / 2, o.order_date, o.customer_id
            FROM warehouse.fct_orders o, range(2) t(k)""")
    return path


def test_report_over_fifty_thousand_orders(tmp_path):
    path = write_big_fixture(tmp_path / "big.duckdb")
    m = commerce_model()
    report = replace(executive_report(), pages=executive_report().pages + (Page("Detail", (
        Visual("By Day", group_by=(Col("Date", "date_day"),),
               values=(("Net Revenue", NET), ("Revenue LY", MeasureRef("Revenue LY")),
                       ("Revenue YTD", MeasureRef("Revenue YTD")))),
        Visual("By Customer", group_by=(Col("Customer", "customer_name"),),
               values=(("Net Revenue", NET), ("AOV", MeasureRef("AOV")),
                       ("Segment Share", MeasureRef("Segment Share")),
                       ("List Price Revenue", MeasureRef("List Price Revenue")))),
    )),))
    start = time.perf_counter()
    out = render.render_report(m, report, path)
    elapsed = time.perf_counter() - start
    print(f"\n50k-order report: {elapsed:.3f}s")
    assert elapsed < 30
    # 2025-12-31 has no order but does have a YTD and a LY, so it stays
    assert len(out["Detail"]["By Day"]["rows"]) == 1096
    assert len(out["Detail"]["By Customer"]["rows"]) == 2000
    by_day = oracle(path, """
        WITH n AS (SELECT order_date AS d, sum(order_amount) AS v FROM fct_orders
                   WHERE order_status = 'completed' GROUP BY 1),
        x AS (SELECT dd.date_day AS d, n0.v AS net, n1.v AS ly,
                     (SELECT sum(v) FROM n c WHERE c.d BETWEEN make_date(year(dd.date_day), 1, 1)
                                                         AND dd.date_day) AS ytd
              FROM dim_date dd LEFT JOIN n n0 ON n0.d = dd.date_day
                   LEFT JOIN n n1 ON n1.d = CAST(dd.date_day - INTERVAL 1 YEAR AS DATE))
        SELECT * FROM x WHERE net IS NOT NULL OR ly IS NOT NULL OR ytd IS NOT NULL
        ORDER BY 1""")
    assert out["Detail"]["By Day"]["rows"] == by_day
    by_customer = oracle(path, """
        WITH o AS (SELECT c.customer_name, c.segment, o.order_amount, o.order_id
                   FROM fct_orders o JOIN dim_customer c ON c.customer_id = o.customer_id
                   WHERE o.order_status = 'completed'),
        l AS (SELECT c.customer_name, sum(l.quantity * p.unit_price) AS lpr
              FROM fct_order_lines l JOIN dim_product p ON p.product_id = l.product_id
              JOIN fct_orders o ON o.order_id = l.order_id
              JOIN dim_customer c ON c.customer_id = o.customer_id GROUP BY 1)
        SELECT o.customer_name, sum(order_amount), sum(order_amount) / count(*),
               -- Segment Share removes only the segment filter: by customer it is 1
               sum(order_amount) / sum(order_amount), any_value(l.lpr)
        FROM o JOIN l USING (customer_name) GROUP BY 1 ORDER BY 1""")
    assert out["Detail"]["By Customer"]["rows"] == by_customer


# ------------------------------------------- world/bi.py: byte-identical panels
#: `json_canon(world.bi.render(root)) + b"\n"` (what `python -m world.bi ROOT`
#: prints) for each theme's example seed, captured from the pre-engine bi.py
#: (commit 41ac089) before bi.py was routed through world.semantic.render.
BI_GOLDEN = {
    "excel_close": (
        '{"close_summary":{"adjustments_total":[["202602","227.20"],["202603","170.48"]],"mon'
        'thly_net_by_account":[["202602","4000-REV","47070.73"],["202602","4100-REV","44599.8'
        '8"],["202602","4200-REV","53712.60"],["202602","5000-COGS","40402.84"],["202602","51'
        '00-OPEX","45854.06"],["202602","5200-PAYROLL","44869.86"],["202603","4000-REV","2663'
        '.22"],["202603","4100-REV","4175.77"],["202603","4200-REV","2429.68"],["202603","500'
        '0-COGS","4283.70"],["202603","5100-OPEX","2007.78"],["202603","5200-PAYROLL","2105.2'
        '4"]]}}'
        "\n"),
    "mainframe_batch": (
        '{"claims_daily":{"daily_claims":[["2026-02-01",43,"111555.81"],["2026-02-02",44,"101'
        '588.54"],["2026-02-03",33,"74544.37"],["2026-02-04",35,"96276.46"],["2026-02-05",41,'
        '"102343.39"],["2026-02-06",37,"81402.60"],["2026-02-07",40,"102297.82"],["2026-02-08'
        '",45,"117762.87"],["2026-02-09",48,"120686.91"],["2026-02-10",42,"87917.59"],["2026-'
        '02-11",43,"119474.39"],["2026-02-12",36,"92041.15"],["2026-02-13",36,"82566.75"],["2'
        '026-02-14",43,"99987.04"],["2026-02-15",38,"94997.65"],["2026-02-16",50,"135332.26"]'
        ',["2026-02-17",39,"83780.73"],["2026-02-18",37,"94034.76"],["2026-02-19",36,"82716.2'
        '9"],["2026-02-20",36,"91136.59"],["2026-02-21",44,"133646.07"],["2026-02-22",41,"983'
        '81.08"],["2026-02-23",45,"104454.79"],["2026-02-24",45,"98902.42"],["2026-02-25",40,'
        '"86116.51"],["2026-02-26",39,"102121.64"],["2026-02-27",34,"78282.67"],["2026-02-28"'
        ',39,"96722.64"],["2026-03-01",33,"84805.57"],["2026-03-02",42,"112083.35"]],"grand_t'
        'otal":[["2967960.71"]]}}'
        "\n"),
    "revenue_daily": (
        '{"revenue_daily":{"daily_revenue":[["2026-02-01","5331.45"],["2026-02-02","5866.51"]'
        ',["2026-02-03","4148.08"],["2026-02-04","5100.06"],["2026-02-05","5736.78"],["2026-0'
        '2-06","4195.22"],["2026-02-07","4434.83"],["2026-02-08","5194.63"],["2026-02-09","59'
        '53.62"],["2026-02-10","4997.11"],["2026-02-11","5951.04"],["2026-02-12","4218.84"],['
        '"2026-02-13","4734.05"],["2026-02-14","5209.05"],["2026-02-15","4629.37"],["2026-02-'
        '16","6507.16"],["2026-02-17","4135.90"],["2026-02-18","5070.19"],["2026-02-19","4621'
        '.14"],["2026-02-20","4584.85"],["2026-02-21","5199.42"],["2026-02-22","5471.24"],["2'
        '026-02-23","5015.44"],["2026-02-24","5375.73"],["2026-02-25","4618.82"],["2026-02-26'
        '","4804.69"],["2026-02-27","4368.87"],["2026-02-28","4349.51"],["2026-03-01","4428.2'
        '4"],["2026-03-02","5749.26"]],"total_revenue":[["150001.10"]]}}'
        "\n"),
    "sftp_vendor": (
        '{"vendor_daily":{"daily_net_revenue":[["2026-02-01","4785.65"],["2026-02-02","5767.5'
        '9"],["2026-02-03","3596.39"],["2026-02-04","5100.06"],["2026-02-05","5685.36"],["202'
        '6-02-06","3529.84"],["2026-02-07","3932.48"],["2026-02-08","5007.77"],["2026-02-09",'
        '"5509.19"],["2026-02-10","4735.05"],["2026-02-11","5951.04"],["2026-02-12","3934.64"'
        '],["2026-02-13","4597.99"],["2026-02-14","5170.89"],["2026-02-15","4478.92"],["2026-'
        '02-16","6276.59"],["2026-02-17","3880.07"],["2026-02-18","5070.19"],["2026-02-19","4'
        '097.27"],["2026-02-20","4225.20"],["2026-02-21","5094.67"],["2026-02-22","5349.36"],'
        '["2026-02-23","5015.44"],["2026-02-24","5375.73"],["2026-02-25","3975.00"],["2026-02'
        '-26","4000.26"],["2026-02-27","4368.87"],["2026-02-28","3730.14"],["2026-03-01","415'
        '8.05"],["2026-03-02","5360.33"]],"totals":[[1504,79,"141760.03"]]}}'
        "\n"),
}


@pytest.mark.parametrize("theme", sorted(BI_GOLDEN))
def test_bi_render_is_byte_identical_through_the_engine(theme, tmp_path, capsysbinary,
                                                         monkeypatch):
    from world import bi
    from world import build as wbuild

    seed = WORLD_DIR / "world" / "themes" / theme / "seed.example.yaml"
    root = wbuild.build(seed, tmp_path / "w", None)
    # every panel goes through the engine as a SQL Visual
    seen = []
    real = render.Session.visual

    def spy(self, model, visual):
        seen.append(visual)
        return real(self, model, visual)
    monkeypatch.setattr(render.Session, "visual", spy)
    assert json_canon(bi.render(root)) + b"\n" == BI_GOLDEN[theme].encode("ascii")
    panels = sum(len(p) for p in json.loads(BI_GOLDEN[theme]).values())
    assert len(seen) == panels and all(v.sql and not v.values for v in seen)
    bi.main([str(root)])
    assert capsysbinary.readouterr().out == BI_GOLDEN[theme].encode("ascii")


def test_sql_values_are_canonical(wh):
    v = Visual("legacy", sql="select DATE '2025-01-01' as d, 1.5::DOUBLE as f, "
                             "2.50::DECIMAL(4,2) as m, NULL as n, true as b")
    assert rows(Model("empty", ()), v, wh) == [["2025-01-01", "1.5", "2.50", None, True]]


def test_example_constants_unchanged():
    """The fixture dates the time tests rely on."""
    assert dt.date(2026, 2, 28).weekday() == 5
