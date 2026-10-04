"""The dbt semantic layer front-end (world/world/semantic/formats/yaml_model.py).

Three claims:

1. write_project / read_project round-trip a model exactly, and the bytes are
   deterministic (they are fingerprinted workspace content).
2. What MetricFlow cannot say exactly is refused LOUDLY, by name - the Commerce
   model itself is not expressible (CALCULATE overwrite filters, REMOVEFILTERS,
   RELATED inside a measure, metric_time reached through another table, an
   offset metric grouped by year/month integers); a dbt-native variant of it is.
3. A dbt project written the way a dbt shop writes it (MetricFlow's own idioms,
   not this module's writer) reads into a model whose numbers on the fixture are
   MetricFlow's numbers: GOLDEN below was produced by the real MetricFlow on the
   same fixture (a throwaway run; MetricFlow is NOT a dependency).
"""
from __future__ import annotations

import sys
import textwrap
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

WORLD_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORLD_DIR))

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
    Col,
    Divide,
    FilterCompare,
    FilterValues,
    If,
    IterAgg,
    KeepFilters,
    Lit,
    MeasureRef,
    RemoveFilters,
    TimeShift,
)
from world.semantic.formats import yaml_model as ym  # noqa: E402
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

VENDOR = "dbt semantic layer"

#: MetricFlow's answers on examples.write_fixture(), from a throwaway run of
#: dbt-metricflow 0.15.0 / metricflow 0.213.0 (Apache-2.0 since 0.209.0) /
#: dbt-core 1.12.5 / dbt-duckdb 1.11.0 / duckdb 1.5.6, against a project shaped
#: like NATIVE_PROJECT below (`mf query --metrics ... --group-by ...`).
#: Revenue YTD is the cumulative metric with grain_to_date: year and
#: period_agg: last (MetricFlow's default period_agg, first, is refused here).
GOLDEN = {
    "totals": {"net_revenue": "29627", "completed_orders": "122",
               "aov": "242.84426229508196"},
    "by_segment": {"Consumer": "10633", "Enterprise": "8767", "SMB": "10227"},
    "by_month": [  # metric_time__month, net_revenue, revenue_ly, revenue_ytd (last)
        ("2025-01-01", "2770", None, "2770"), ("2025-02-01", "1871", None, "4641"),
        ("2025-03-01", "2028", None, "6669"), ("2025-04-01", "1638", None, "8307"),
        ("2025-05-01", "2330", None, "10637"), ("2025-06-01", "2446", None, "13083"),
        ("2025-07-01", "1632", None, "14715"), ("2025-08-01", "1951", None, "16666"),
        ("2025-09-01", "1832", None, "18498"), ("2025-10-01", "2493", None, "20991"),
        ("2025-11-01", "1693", None, "22684"), ("2025-12-01", "2228", None, "24912"),
        ("2026-01-01", "2050", "2770", "2050"), ("2026-02-01", "2665", "1871", "4715")],
    "top_products": [("Cable", "284"), ("Support Plan", "216"), ("Gadget", "142")],
    "region_2026": {"APAC": "2388", "EMEA": "1403", "NA": "924"},
}

#: The same MetricFlow, run on THIS module's output: write_project(dbt_commerce(),
#: (dbt_report(),)) into a dbt project whose models are views over the fixture,
#: then `dbt run`, `dbt parse` (MetricFlow's semantic-manifest validation passes)
#: and `mf query`. Every number below equals the engine's on read_project() of
#: that project, to the last digit.
GOLDEN_WRITTEN = {
    "net_revenue": "29627", "completed_orders": "122", "aov": "242.84426229508196",
    "customers": "6", "billed_amount": "36058.58", "gross_revenue": "34912",
    "order_count": "142", "big_order_revenue": "22385", "margin_flag": "1",
    "yoy_growth_2026": ("-0.259927797833935", "0.4243719935863175"),
}


@pytest.fixture(scope="module")
def wh(tmp_path_factory) -> Path:
    return write_fixture(tmp_path_factory.mktemp("fixture") / "fixture.duckdb")


def same_number(got, want) -> bool:
    if want is None or got is None:
        return got is want
    return Decimal(str(got)) == Decimal(want)


# ------------------------------------------------- the dbt-expressible Commerce
KF = KeepFilters(COMPLETED)
NET = MeasureRef("Net Revenue")
LY = MeasureRef("Revenue LY")
DAY = Col("Date", "date_day")


def dbt_commerce() -> Model:
    """commerce_model() reshaped into what MetricFlow can say exactly:

    - a metric filter INTERSECTS with the query's filters (KeepFilters); it never
      overwrites (CALCULATE's default) - so COMPLETED is wrapped;
    - every fact has its own agg_time_dimension, so Order Lines relates to Date
      itself; its join to Orders (another fact with its own metric_time) is kept
      but inactive, since following it would give Order Lines two paths to Date;
    - the Date table carries the metric_time__<grain> columns the reader adds;
    - no Segment Share (REMOVEFILTERS) and no List Price Revenue (RELATED)."""
    base = {t.name: t for t in commerce_model().tables}
    date = replace(base["Date"], columns=base["Date"].columns
                   + ym.metric_time_columns("Date", "date_day"))
    tables = (base["Orders"], base["Order Lines"], base["Customer"], base["Product"], date)
    rels = (
        Relationship("Orders", "customer_id", "Customer", "customer_id"),
        Relationship("Orders", "order_date", "Date", "date_day"),
        Relationship("Order Lines", "order_id", "Orders", "order_id", active=False),
        Relationship("Order Lines", "product_id", "Product", "product_id"),
        Relationship("Order Lines", "order_date", "Date", "date_day"),
    )
    amount = Col("Orders", "order_amount")
    measures = (
        Measure("Gross Revenue", "Orders", Agg("sum", amount), format_string="$#,0.00"),
        Measure("Net Revenue", "Orders", Calculate(MeasureRef("Gross Revenue"), (KF,)),
                format_string="$#,0.00", description="Revenue from completed orders."),
        Measure("Order Count", "Orders", Agg("count_rows", table="Orders")),
        Measure("Completed Orders", "Orders", Calculate(Agg("count_rows", table="Orders"), (KF,))),
        Measure("AOV", "Orders", Divide(NET, MeasureRef("Completed Orders")),
                format_string="$#,0.00", description="Average order value, completed orders."),
        Measure("Customers", "Orders", Agg("count_distinct", Col("Orders", "customer_id"))),
        Measure("Billed Amount", "Orders", IterAgg("sum", "Orders", BinOp(
            "+", amount, Col("Orders", "shipping_amount")))),
        Measure("Units", "Order Lines", Agg("sum", Col("Order Lines", "quantity"))),
        Measure("Revenue YTD", "Orders",
                TimeShift(Calculate(Agg("sum", amount), (KF,)), DAY, "ytd")),
        Measure("Revenue LY", "Orders", TimeShift(NET, DAY, "same_period_last_year")),
        Measure("YoY Growth", "Orders", Divide(BinOp("-", NET, LY), LY), format_string="0.0%"),
        Measure("Big Order Revenue", "Orders", Calculate(MeasureRef("Gross Revenue"), (
            KeepFilters(FilterCompare(amount, ">=", Decimal("100.00"))),
            KeepFilters(FilterValues(Col("Customer", "segment"), ("SMB", None), negate=True))))),
        Measure("Margin Flag", "Orders", If(BinOp(">", NET, Lit(1000)), Lit(1), Lit(0))),
    )
    return Model("Commerce", tables, rels, measures)


def dbt_report() -> Report:
    m = MeasureRef
    return Report("Executive Overview", "Commerce", (
        Page("Overview", (
            Visual("KPIs", values=(("Net Revenue", NET),
                                   ("Completed Orders", m("Completed Orders")),
                                   ("AOV", m("AOV"))), kind="card"),
            Visual("Revenue by Month", group_by=(Col("Date", "metric_time__month"),),
                   values=(("Net Revenue", NET), ("Revenue LY", LY),
                           ("YoY Growth", m("YoY Growth")), ("Revenue YTD", m("Revenue YTD"))),
                   kind="lineChart"),
            Visual("Revenue by Segment", group_by=(Col("Customer", "segment"),),
                   values=(("Net Revenue", NET),), order_by=(("Net Revenue", True),),
                   kind="barChart"),
            Visual("Top Products", group_by=(Col("Product", "product_name"),),
                   values=(("Units", m("Units")),), order_by=(("Units", True),), top_n=3),
        )),
        Page("Regions", (
            Visual("Revenue by Region 2026", group_by=(Col("Customer", "region"),),
                   values=(("Revenue", NET),),
                   filters=(FilterValues(Col("Date", "year"), (2026,)),), kind="barChart"),
        )),
    ))


# ------------------------------------------------------------------ round trip
def test_round_trip_is_exact(tmp_path):
    model, report = dbt_commerce(), dbt_report()
    render.validate(model, (report,))
    written = ym.write_project(model, (report,), tmp_path)
    assert written == sorted(written)
    assert {p.relative_to(tmp_path).as_posix() for p in written} == {
        "dbt_project.yml", "models/semantic_layer/commerce.yml",
        "dashboards/executive_overview.yml"}
    got_model, got_reports = ym.read_project(tmp_path)
    assert got_model == model
    assert got_reports == (report,)


def test_write_is_deterministic(tmp_path):
    a = ym.write_project(dbt_commerce(), (dbt_report(),), tmp_path / "a")
    b = ym.write_project(dbt_commerce(), (dbt_report(),), tmp_path / "b")
    assert [p.read_bytes() for p in a] == [p.read_bytes() for p in b]
    # sorted keys all the way down
    doc = yaml.safe_load((tmp_path / "a/models/semantic_layer/commerce.yml").read_text())
    def check(node):
        if isinstance(node, dict):
            assert list(node) == sorted(node)
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)
    check(doc)


def test_written_yaml_is_metricflow_shaped(tmp_path):
    ym.write_project(dbt_commerce(), (dbt_report(),), tmp_path)
    doc = yaml.safe_load((tmp_path / "models/semantic_layer/commerce.yml").read_text())
    sms = {s["name"]: s for s in doc["semantic_models"]}
    assert sorted(sms) == ["customer", "date", "order_lines", "orders", "product"]
    orders = sms["orders"]
    assert orders["model"] == "ref('fct_orders')" and orders["label"] == "Orders"
    assert orders["defaults"] == {"agg_time_dimension": "order_date"}
    assert {"name": "orders", "type": "primary", "expr": "order_id"} in orders["entities"]
    assert {"name": "customer", "type": "foreign", "expr": "customer_id"} in orders["entities"]
    metrics = {m["name"]: m for m in doc["metrics"]}
    assert metrics["completed_orders"]["type"] == "simple"
    assert metrics["completed_orders"]["filter"] == \
        "{{ Dimension('orders__order_status') }} = 'completed'"
    assert metrics["aov"] == {"name": "aov", "label": "AOV", "type": "ratio",
                              "description": "Average order value, completed orders.",
                              "config": {"meta": {"format": "$#,0.00"}},
                              "type_params": {"numerator": "net_revenue",
                                              "denominator": "completed_orders"}}
    assert metrics["revenue_ly"]["type_params"] == {
        "expr": "net_revenue_offset", "metrics": [
            {"name": "net_revenue", "offset_window": "1 year", "alias": "net_revenue_offset"}]}
    assert metrics["revenue_ytd"]["type_params"]["cumulative_type_params"] == {
        "grain_to_date": "year", "period_agg": "last"}
    # on the metric, not the measure input: dbt 1.12 drops the latter
    assert metrics["revenue_ytd"]["filter"] == \
        "{{ Dimension('orders__order_status') }} = 'completed'"
    assert metrics["revenue_ytd"]["type_params"]["measure"] == "revenue_ytd"
    assert metrics["yoy_growth"]["type_params"]["expr"] == \
        "(net_revenue - revenue_ly) / NULLIF(revenue_ly, 0)"
    spine = next(m for m in doc["models"] if m["name"] == "dim_date")
    assert spine["time_spine"] == {"standard_granularity_column": "date_day"}
    dash = yaml.safe_load((tmp_path / "dashboards/executive_overview.yml").read_text())
    month = dash["dashboards"][0]["pages"][0]["visuals"][1]
    assert month["group_by"] == ["metric_time__month"]
    assert month["metrics"] == ["net_revenue", "revenue_ly", "yoy_growth", "revenue_ytd"]


def test_round_tripped_model_renders_metricflows_numbers(tmp_path, wh):
    ym.write_project(dbt_commerce(), (dbt_report(),), tmp_path)
    model, (report,) = ym.read_project(tmp_path)
    out = render.render_report(model, report, wh)
    check_golden(out["Overview"], out["Regions"]["Revenue by Region 2026"])


def test_metricflow_numbers_for_the_written_project(tmp_path, wh):
    ym.write_project(dbt_commerce(), (dbt_report(),), tmp_path)
    model, _ = ym.read_project(tmp_path)
    names = [k for k in GOLDEN_WRITTEN if not k.startswith("yoy")]
    label = {ym.ident(m.name): m.name for m in model.measures}
    card = Visual("c", values=tuple((n, MeasureRef(label[n])) for n in names))
    row = render.render_visual(model, card, wh)["rows"][0]
    assert all(same_number(g, GOLDEN_WRITTEN[n]) for g, n in zip(row, names, strict=True))
    yoy = Visual("y", group_by=(Col("Date", "metric_time__month"),),
                 values=(("g", MeasureRef("YoY Growth")),),
                 filters=(FilterValues(Col("Date", "year"), (2026,)),))
    got = [r[1] for r in render.render_visual(model, yoy, wh)["rows"]]
    assert all(abs(float(g) - float(w)) < 1e-15
               for g, w in zip(got, GOLDEN_WRITTEN["yoy_growth_2026"], strict=True))


def check_golden(overview: dict, regions: dict) -> None:
    kpis = overview["KPIs"]["rows"][0]
    assert all(same_number(g, GOLDEN["totals"][k]) for g, k in
               zip(kpis, ("net_revenue", "completed_orders", "aov"), strict=True))
    month = overview["Revenue by Month"]
    assert month["columns"][:3] == ["metric_time__month", "Net Revenue", "Revenue LY"]
    ytd = month["columns"].index("Revenue YTD")
    assert len(month["rows"]) == len(GOLDEN["by_month"])
    for row, (m, net, ly, y) in zip(month["rows"], GOLDEN["by_month"], strict=True):
        assert row[0] == m and same_number(row[1], net) and same_number(row[2], ly)
        assert same_number(row[ytd], y)
    seg = overview["Revenue by Segment"]["rows"]
    assert {r[0]: r[1] for r in seg}.keys() == GOLDEN["by_segment"].keys()
    assert all(same_number(r[1], GOLDEN["by_segment"][r[0]]) for r in seg)
    top = overview["Top Products"]["rows"]
    assert [(r[0], str(r[1])) for r in top] == GOLDEN["top_products"]
    reg = {r[0]: r[1] for r in regions["rows"]}
    assert reg.keys() == GOLDEN["region_2026"].keys()
    assert all(same_number(reg[k], v) for k, v in GOLDEN["region_2026"].items())


# --------------------------------------- a dbt project written by a dbt shop
NATIVE_PROJECT = {
    "dbt_project.yml": """
        name: Commerce
        version: "1.0.0"
        config-version: 2
        profile: commerce
        model-paths: ["models"]
        models:
          Commerce:
            warehouse: {+materialized: table, +schema: warehouse}
        """,
    "models/warehouse/fct_orders.sql": "select 1",
    "models/warehouse/fct_order_lines.sql": "select 1",
    "models/warehouse/dim_customer.sql": "select 1",
    "models/warehouse/dim_product.sql": "select 1",
    "models/warehouse/dim_date.sql": "select 1",
    "models/warehouse/schema.yml": """
        version: 2
        models:
          - name: dim_date
            description: One row per calendar day.
            time_spine:
              standard_granularity_column: date_day
            columns:
              - name: date_day
                granularity: day
              - name: year
                data_type: integer
          - name: fct_orders
            columns:
              - name: order_amount
                data_type: decimal(12,2)
        """,
    "models/marts/semantic.yml": """
        semantic_models:
          - name: orders
            model: ref('fct_orders')
            defaults:
              agg_time_dimension: order_date
            entities:
              - name: order
                type: primary
                expr: order_id
              - name: customer
                type: foreign
                expr: customer_id
              - name: date
                type: foreign
                expr: order_date
            dimensions:
              - name: order_status
                type: categorical
              - name: order_date
                type: time
                type_params:
                  time_granularity: day
            measures:
              - name: order_amount
                agg: sum
              - name: order_count
                agg: sum
                expr: "1"
          - name: customers
            model: ref('dim_customer')
            entities:
              - name: customer
                type: primary
                expr: customer_id
            dimensions:
              - name: segment
                type: categorical
              - name: region
                type: categorical
          - name: order_lines
            model: ref('fct_order_lines')
            defaults:
              agg_time_dimension: order_date
            entities:
              - name: order_line
                type: primary
                expr: order_line_id
              - name: order
                type: foreign
                expr: order_id
              - name: product
                type: foreign
                expr: product_id
            dimensions:
              - name: order_date
                type: time
                type_params:
                  time_granularity: day
            measures:
              - name: quantity
                agg: sum
                create_metric: true
          - name: products
            model: ref('dim_product')
            entities:
              - name: product
                type: primary
                expr: product_id
            dimensions:
              - name: product_name
                type: categorical
          - name: calendar
            model: ref('dim_date')
            entities:
              - name: date
                type: primary
                expr: date_day
            dimensions:
              - name: cal_year
                type: categorical
                expr: year
        metrics:
          - name: net_revenue
            label: Net Revenue
            type: simple
            type_params:
              measure: order_amount
            filter: "{{ Dimension('order__order_status') }} = 'completed'"
          - name: completed_orders
            label: Completed Orders
            type: simple
            type_params:
              measure: order_count
            filter: "{{ Dimension('order__order_status') }} = 'completed'"
          - name: aov
            label: AOV
            type: ratio
            type_params:
              numerator: net_revenue
              denominator: completed_orders
          - name: revenue_ly
            label: Revenue LY
            type: derived
            type_params:
              expr: net_revenue_ly
              metrics:
                - name: net_revenue
                  offset_window: 1 year
                  alias: net_revenue_ly
          - name: revenue_ytd
            label: Revenue YTD
            type: cumulative
            type_params:
              measure: order_amount
              cumulative_type_params:
                grain_to_date: year
                period_agg: last
            filter: "{{ Dimension('order__order_status') }} = 'completed'"
        """,
    "dashboards/executive.yml": """
        dashboards:
          - name: Executive Overview
            model: Commerce
            pages:
              - name: Overview
                visuals:
                  - name: KPIs
                    kind: card
                    metrics: [net_revenue, completed_orders, aov]
                  - name: Revenue by Month
                    group_by: [metric_time__month]
                    metrics: [net_revenue, revenue_ly, revenue_ytd]
                  - name: Revenue by Segment
                    group_by: [customer__segment]
                    metrics: [net_revenue]
                  - name: Top Products
                    group_by: [product__product_name]
                    metrics: [quantity]
                    order_by: [-quantity]
                    limit: 3
              - name: Regions
                visuals:
                  - name: Revenue by Region 2026
                    group_by: [customer__region]
                    metrics: [{name: net_revenue, label: Revenue}]
                    where: ["{{ Dimension('date__cal_year') }} = 2026"]
        """,
}


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return root


def test_a_native_dbt_project_reads_and_matches_metricflow(tmp_path, wh):
    model, (report,) = ym.read_project(write_tree(tmp_path, NATIVE_PROJECT))
    assert model.name == "Commerce" and model.null_semantics == "sql"
    names = {t.name: t for t in model.tables}
    assert names["orders"].source == "warehouse.fct_orders"        # +schema from dbt_project
    assert names["calendar"].is_date_table and names["calendar"].date_column == "date_day"
    assert names["calendar"].description == "One row per calendar day."
    year = next(c for c in names["calendar"].columns if c.name == "cal_year")
    assert year.data_type == "int"                                  # from models: columns
    measures = {m.name: m for m in model.measures}
    assert measures["Net Revenue"].expr == Calculate(
        Agg("sum", Col("orders", "order_amount")),
        (KeepFilters(FilterValues(Col("orders", "order_status"), ("completed",))),))
    assert measures["quantity"].expr == Agg("sum", Col("order_lines", "quantity"))
    # order_lines -> orders joins a fact that has its own metric_time: kept, inactive
    rel = next(r for r in model.relationships if r.to_table == "orders")
    assert rel.from_table == "order_lines" and not rel.active
    out = render.render_report(model, report, wh)
    overview = dict(out["Overview"])
    month = overview["Revenue by Month"]
    month["columns"] = ["metric_time__month", "Net Revenue", "Revenue LY", "Revenue YTD"]
    overview["Top Products"] = {"columns": ["product_name", "Units"],
                                "rows": overview["Top Products"]["rows"]}
    check_golden(overview, out["Regions"]["Revenue by Region 2026"])


# ------------------------------------------------- what the dbt spec cannot say
def refuses(model: Model, reports=(), match: str = "") -> Unsupported:
    with pytest.raises(Unsupported, match=match) as info:
        ym.write_project(model, reports, Path("/nonexistent/never/written"))
    assert info.value.vendor == VENDOR
    return info.value


def test_the_commerce_model_itself_is_refused():
    """CALCULATE's overwrite filter is the first thing MetricFlow cannot say."""
    refuses(commerce_model(), (executive_report(),), match="CALCULATE filter")


def with_measure(model: Model, measure: Measure) -> Model:
    return replace(model, measures=model.measures + (measure,))


@pytest.mark.parametrize("expr,match", [
    (Calculate(NET, (COMPLETED,)), "CALCULATE filter"),
    (Divide(NET, Calculate(NET, (RemoveFilters(columns=(Col("Customer", "segment"),)),))),
     "REMOVEFILTERS"),
    (IterAgg("sum", "Order Lines", BinOp("*", Col("Order Lines", "quantity"),
                                         MeasureRef("Units"))), "context transition"),
    (TimeShift(NET, DAY, "previous_month"), "previous_month"),
    (BinOp("*", Agg("sum", Col("Orders", "order_amount")), Lit(2)), "aggregate inside"),
    (Calculate(MeasureRef("Gross Revenue"), (KeepFilters(FilterCompare(
        Col("Orders", "order_amount"), ">", None)),)), "comparison with NULL"),
])
def test_measure_refusals(expr, match):
    refuses(with_measure(dbt_commerce(), Measure("x2", "Orders", expr)), match=match)


def test_related_in_a_measure_is_refused():
    lpr = next(m for m in commerce_model().measures if m.name == "List Price Revenue")
    refuses(with_measure(dbt_commerce(), lpr), match="RELATED")


def test_metric_time_through_another_table_is_refused():
    """Commerce's Order Lines reaches Date only through Orders: MetricFlow's
    agg_time_dimension must be the measure's own column."""
    m = commerce_model()
    m = replace(m, measures=tuple(x for x in m.measures if x.name in ("Gross Revenue", "Units")))
    refuses(m, match="agg_time_dimension")


@pytest.mark.parametrize("change,match", [
    (lambda m: replace(m, null_semantics="dax"), "DAX"),
    (lambda m: replace(m, relationships=tuple(replace(r, cross_filter="both")
                                              if r.to_table == "Customer" else r
                                              for r in m.relationships)), "bidirectional"),
    (lambda m: replace(m, relationships=tuple(replace(r, cardinality="one_to_one")
                                              if r.to_table == "Product" else r
                                              for r in m.relationships)), "one-to-one"),
    (lambda m: replace(m, relationships=tuple(replace(r, active=False)
                                              if r.to_table == "Product" else r
                                              for r in m.relationships)), "inactive"),
    (lambda m: replace(m, tables=tuple(replace(t, source=None, source_sql="select 1")
                                       if t.name == "Product" else t for t in m.tables)),
     "native SQL"),
    (lambda m: replace(m, tables=tuple(t for t in m.tables if t.name != "Date"),
                       relationships=tuple(r for r in m.relationships if r.to_table != "Date"),
                       measures=m.measures[:1]), "date table"),
    (lambda m: replace(m, name="Commerce Model"), "project name"),
])
def test_model_refusals(change, match):
    refuses(change(dbt_commerce()), match=match)


@pytest.mark.parametrize("visual,match", [
    (Visual("v", group_by=(Col("Date", "year"), Col("Date", "month")),
            values=(("LY", LY),)), "metric_time"),
    (Visual("v", group_by=(Col("Date", "metric_time__year"),), values=(("LY", LY),)),
     "offset_window at year grain"),
    (Visual("v", values=(("x", BinOp("+", NET, Lit(1))),)), "not a metric"),
])
def test_visual_refusals(visual, match):
    report = Report("r", "Commerce", (Page("p", (visual,)),))
    refuses(dbt_commerce(), (report,), match=match)


def test_executive_report_visuals_that_cannot_be_written():
    """Revenue by Month groups an offset metric by year/month integers (MetricFlow
    requires metric_time); Segment Share needs REMOVEFILTERS."""
    m = dbt_commerce()
    month = next(v for v in executive_report().pages[0].visuals if v.name == "Revenue by Month")
    refuses(m, (Report("r", "Commerce", (Page("p", (month,)),)),), match="metric_time")


# ------------------------------------------- what MetricFlow YAML we refuse
BASE = {k: v for k, v in NATIVE_PROJECT.items() if not k.startswith("dashboards/")}


def native_with(tmp_path: Path, extra_semantic: str = "", dashboards: str | None = None) -> Path:
    files = dict(BASE)
    files["models/marts/semantic.yml"] = (
        textwrap.dedent(NATIVE_PROJECT["models/marts/semantic.yml"])
        + textwrap.indent(textwrap.dedent(extra_semantic).lstrip("\n"), "  "))
    if dashboards is not None:
        files["dashboards/d.yml"] = dashboards
    return write_tree(tmp_path, files)


METRIC_REFUSALS = {
    "conversion metric": """
          - name: conv
            label: conv
            type: conversion
            type_params: {conversion_type_params: {base_measure: order_count}}
        """,
    "cumulative window": """
          - name: rolling
            label: rolling
            type: cumulative
            type_params:
              measure: order_amount
              cumulative_type_params: {window: 7 days}
        """,
    "period_agg first": """
          - name: ytd_first
            label: ytd_first
            type: cumulative
            type_params:
              measure: order_amount
              cumulative_type_params: {grain_to_date: year}
        """,
    "offset_window 1 month": """
          - name: lm
            label: lm
            type: derived
            type_params:
              expr: x
              metrics: [{name: net_revenue, offset_window: 1 month, alias: x}]
        """,
    "offset_to_grain": """
          - name: soy
            label: soy
            type: derived
            type_params:
              expr: x
              metrics: [{name: net_revenue, offset_to_grain: year, alias: x}]
        """,
    "fill_nulls_with": """
          - name: filled
            label: filled
            type: simple
            type_params: {measure: {name: order_amount, fill_nulls_with: 0}}
        """,
    "Entity\\(\\) in a filter": """
          - name: ent
            label: ent
            type: simple
            type_params: {measure: order_amount}
            filter: "{{ Entity('customer') }} = 3"
        """,
    "OR in a filter": """
          - name: either
            label: either
            type: simple
            type_params: {measure: order_amount}
            filter: >-
              {{ Dimension('order__order_status') }} = 'a'
              OR {{ Dimension('customer__segment') }} = 'b'
        """,
    "SQL function MD5": """
          - name: weird
            label: weird
            type: derived
            type_params:
              expr: md5(net_revenue)
              metrics: [{name: net_revenue}]
        """,
}


@pytest.mark.parametrize("match", sorted(METRIC_REFUSALS))
def test_metricflow_constructs_refused_on_read(tmp_path, match):
    with pytest.raises(Unsupported, match=match) as info:
        ym.read_project(native_with(tmp_path, METRIC_REFUSALS[match]))
    assert info.value.vendor == VENDOR


@pytest.mark.parametrize("measure,match", [
    ("{name: flags, agg: sum_boolean, expr: order_id}", "sum_boolean"),
    ("{name: med, agg: median, expr: order_amount}", "median"),
    ("{name: p90, agg: percentile, expr: order_amount, agg_params: {percentile: 0.9}}",
     "percentile"),
    ("{name: semi, agg: sum, expr: order_amount, non_additive_dimension: {name: order_date}}",
     "non_additive_dimension"),
])
def test_measure_kinds_refused_on_read(tmp_path, measure, match):
    """Refused when a metric uses the measure (an unused measure lowers to nothing)."""
    name = yaml.safe_load(measure)["name"]
    files = dict(BASE)
    files["models/marts/semantic.yml"] = textwrap.dedent(
        NATIVE_PROJECT["models/marts/semantic.yml"]).replace(
        "      - name: order_count\n", f"      - {measure}\n      - name: order_count\n", 1) \
        + f"  - {{name: uses, label: uses, type: simple, type_params: {{measure: {name}}}}}\n"
    with pytest.raises(Unsupported, match=match):
        ym.read_project(write_tree(tmp_path, files))


def test_the_latest_v2_spec_is_refused_by_name(tmp_path):
    files = dict(BASE)
    files["models/marts/v2.yml"] = textwrap.dedent("""
        models:
          - name: fct_returns
            semantic_model: true
        metrics:
          - name: returns
            type: simple
            agg: sum
            expr: amount
        """)
    with pytest.raises(Unsupported, match="latest"):
        ym.read_project(write_tree(tmp_path, files))


def test_unique_entities_are_refused(tmp_path):
    files = dict(BASE)
    files["models/marts/semantic.yml"] = textwrap.dedent(
        NATIVE_PROJECT["models/marts/semantic.yml"]).replace(
        "type: primary\n        expr: product_id", "type: unique\n        expr: product_id")
    with pytest.raises(Unsupported, match="unique entity"):
        ym.read_project(write_tree(tmp_path, files))


DASHBOARD_ERRORS = {
    "unknown metric": ("metrics: [nope]", ModelError),
    "no metric_time": ("metrics: [revenue_ly]\n    group_by: [customer__segment]", ModelError),
    "unknown dimension": ("metrics: [net_revenue]\n    group_by: [customer__nope]", ModelError),
    "unreachable": ("metrics: [net_revenue]\n    group_by: [product__product_name]", ModelError),
    "through a fact": ("metrics: [quantity]\n    group_by: [customer__segment]", Unsupported),
    "filter through a fact": (
        "metrics: [quantity]\n    where: [\"{{ Dimension('order__order_status') }} = 'x'\"]",
        Unsupported),
    "metric_time grain": ("metrics: [net_revenue]\n    group_by: [metric_time__hour]", Unsupported),
}


@pytest.mark.parametrize("case", sorted(DASHBOARD_ERRORS))
def test_dashboard_errors(tmp_path, case):
    body, error = DASHBOARD_ERRORS[case]
    dash = ("dashboards:\n- name: d\n  model: Commerce\n  pages:\n  - name: p\n    visuals:\n"
            "    - name: v\n      " + body.replace("\n    ", "\n      ") + "\n")
    with pytest.raises(error):
        ym.read_project(native_with(tmp_path, dashboards=dash))


def test_project_errors(tmp_path):
    files = dict(NATIVE_PROJECT)
    files["models/marts/semantic.yml"] = textwrap.dedent(files["models/marts/semantic.yml"]) \
        .replace("measure: order_count", "measure: nope")
    with pytest.raises(ModelError, match="nope"):
        ym.read_project(write_tree(tmp_path / "a", files))
    files = dict(NATIVE_PROJECT)
    files["models/marts/more.yml"] = "metrics:\n- {name: aov, label: x, type: simple, " \
                                     "type_params: {measure: order_count}}\n"
    with pytest.raises(ModelError, match="aov"):
        ym.read_project(write_tree(tmp_path / "b", files))


# ------------------------------------------------- parse / format expressions
@pytest.mark.parametrize("modifier,text", [
    (FilterValues(Col("Orders", "order_status"), ("completed",)),
     "{{ Dimension('orders__order_status') }} = 'completed'"),
    (FilterValues(Col("Orders", "order_status"), ("a", "b")),
     "{{ Dimension('orders__order_status') }} IN ('a', 'b')"),
    (FilterValues(Col("Customer", "segment"), ("SMB", None), negate=True),
     "{{ Dimension('customer__segment') }} <> 'SMB'"),
    (FilterValues(Col("Customer", "segment"), ("SMB",), negate=True),
     "({{ Dimension('customer__segment') }} <> 'SMB' "
     "OR {{ Dimension('customer__segment') }} IS NULL)"),
    (FilterValues(Col("Customer", "segment"), (None,)),
     "{{ Dimension('customer__segment') }} IS NULL"),
    (FilterValues(Col("Customer", "segment"), (None,), negate=True),
     "{{ Dimension('customer__segment') }} IS NOT NULL"),
    (FilterValues(Col("Customer", "segment"), ("a", None)),
     "({{ Dimension('customer__segment') }} = 'a' "
     "OR {{ Dimension('customer__segment') }} IS NULL)"),
    (FilterCompare(Col("Orders", "order_amount"), ">=", Decimal("100.50")),
     "{{ Dimension('orders__order_amount') }} >= 100.50"),
    (FilterCompare(Col("Date", "date_day"), "<", __import__("datetime").date(2026, 1, 1)),
     "{{ Dimension('date__date_day') }} < CAST('2026-01-01' AS DATE)"),
    (FilterValues(Col("Date", "metric_time__month"), (__import__("datetime").date(2026, 1, 1),)),
     "{{ TimeDimension('metric_time', 'month') }} = CAST('2026-01-01' AS DATE)"),
    (FilterValues(Col("Date", "is_weekend"), (True,)),
     "{{ Dimension('date__is_weekend') }} = TRUE"),
    (FilterCompare(Col("Date", "year"), "<=", 2025),
     "{{ Dimension('date__year_value') }} <= 2025"),
    (FilterValues(Col("Orders", "order_status"), ("a", "b", None), negate=True),
     "{{ Dimension('orders__order_status') }} NOT IN ('a', 'b')"),
])
def test_filters_round_trip(modifier, text):
    m = dbt_commerce()
    assert ym.format_expression(modifier, m) == text
    assert ym.parse_expression(text, m) == modifier


@pytest.mark.parametrize("expr,text", [
    (Divide(BinOp("-", NET, LY), LY), "(net_revenue - revenue_ly) / NULLIF(revenue_ly, 0)"),
    (Divide(NET, MeasureRef("Units"), Lit(0)),
     "CASE WHEN NULLIF(units, 0) IS NULL THEN 0 ELSE net_revenue / units END"),
    (If(BinOp(">", NET, Lit(Decimal("1.5"))), Lit("big"), Lit("small")),
     "CASE WHEN net_revenue > 1.5 THEN 'big' ELSE 'small' END"),
    (BinOp("*", BinOp("+", NET, Lit(1)), Lit(2)), "(net_revenue + 1) * 2"),
])
def test_derived_expressions_round_trip(expr, text):
    m = dbt_commerce()
    assert ym.format_expression(expr, m) == text
    assert ym.parse_expression(text, m) == expr


def test_reserved_dimension_names(tmp_path):
    """MetricFlow refuses a dimension named after a time grain; writing renames
    (`year` -> `year_value`, the model name kept as the label), reading a project
    that uses one is the project's error, as `dbt parse` says."""
    ym.write_project(dbt_commerce(), (), tmp_path / "w")
    doc = yaml.safe_load((tmp_path / "w/models/semantic_layer/commerce.yml").read_text())
    date = next(s for s in doc["semantic_models"] if s["name"] == "date")
    year = next(d for d in date["dimensions"] if d.get("label") == "year")
    assert year["name"] == "year_value" and year["expr"] == "year"
    files = dict(NATIVE_PROJECT)
    files["models/marts/semantic.yml"] = textwrap.dedent(files["models/marts/semantic.yml"]) \
        .replace("- name: cal_year\n        type: categorical\n        expr: year",
                 "- name: year\n        type: categorical")
    with pytest.raises(ModelError, match="reserved time granularity"):
        ym.read_project(write_tree(tmp_path / "r", files))


def test_parse_expression_refuses_unknown_names():
    with pytest.raises(ModelError, match="nope"):
        ym.parse_expression("nope + 1", dbt_commerce())
    with pytest.raises(Unsupported):
        ym.parse_expression("{{ Dimension('customer__segment') }} LIKE 'S%'", dbt_commerce())


def test_reads_back_what_a_dbt_project_already_patches(tmp_path):
    """Writing into an existing dbt project that already documents the date model
    with the time spine reuses that patch instead of writing a duplicate one (dbt
    refuses two patches of one model)."""
    root = write_tree(tmp_path, {k: v for k, v in NATIVE_PROJECT.items()
                                 if k in ("dbt_project.yml", "models/warehouse/schema.yml")
                                 or k.endswith(".sql")})
    paths = ym.write_project(dbt_commerce(), (), root)
    doc = yaml.safe_load((root / "models/semantic_layer/commerce.yml").read_text())
    assert "models" not in doc                              # everything was derivable
    assert [p.relative_to(root).as_posix() for p in paths] == ["models/semantic_layer/commerce.yml"]
    model, _ = ym.read_project(root)
    assert model == dbt_commerce()


def test_existing_patch_that_conflicts_is_refused(tmp_path):
    files = {k: v for k, v in NATIVE_PROJECT.items()
             if k in ("dbt_project.yml", "models/warehouse/schema.yml") or k.endswith(".sql")}
    files["models/warehouse/schema.yml"] = files["models/warehouse/schema.yml"].replace(
        "standard_granularity_column: date_day", "standard_granularity_column: week_start")
    with pytest.raises(ModelError, match="dim_date"):
        ym.write_project(dbt_commerce(), (), write_tree(tmp_path, files))


def test_unused_column_metadata_survives(tmp_path):
    m = dbt_commerce()
    m = replace(m, tables=tuple(replace(t, description="Customers.", hidden=True, columns=tuple(
        replace(c, description="Segment.", hidden=False) if c.name == "segment" else c
        for c in t.columns)) if t.name == "Customer" else t for t in m.tables))
    ym.write_project(m, (), tmp_path)
    assert ym.read_project(tmp_path)[0] == m


def test_calculated_columns_round_trip(tmp_path):
    m = dbt_commerce()
    calc = Column("size", "", "string", expr=If(
        BinOp(">=", Col("Orders", "order_amount"), Lit(100)), Lit("big"), Lit("small")))
    m = replace(m, tables=tuple(replace(t, columns=t.columns + (calc,)) if t.name == "Orders"
                                else t for t in m.tables))
    ym.write_project(m, (), tmp_path)
    assert ym.read_project(tmp_path)[0] == m


def test_table_and_label_names(tmp_path):
    t = Table("Sales Facts", "warehouse.fct_orders", (
        Column("Order ID", "order_id", "int"), Column("order_date", "order_date", "date"),
        Column("Amount (USD)", "order_amount", "decimal")))
    d = Table("Date", "warehouse.dim_date", (Column("date_day", "date_day", "date"),)
              + ym.metric_time_columns("Date", "date_day"), is_date_table=True,
              date_column="date_day")
    m = Model("Sales", (t, d), (Relationship("Sales Facts", "order_date", "Date", "date_day"),),
              (Measure("Total $", "Sales Facts", Agg("sum", Col("Sales Facts", "Amount (USD)"))),))
    ym.write_project(m, (), tmp_path)
    assert ym.read_project(tmp_path)[0] == m
