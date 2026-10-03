"""The Sigma front-end (world/world/semantic/formats/sigma).

What is claimed, and how each claim is checked:

1. SHAPE. What `write_project` writes is what Sigma's API returns for the same
   document: the keys of `GET /v2/dataModels/{id}/spec` and of
   `GET /v2/workbooks/{id}?includeContents=true` in Sigma Computing Public
   REST API 2.0.0+20260930, the `inode-.../COLUMN` column ids and
   `[TABLE/Display Name]` formulas of the documented example representations,
   the flat element list placed on pages by layout XML. Each key list below is
   copied from that OpenAPI document, not from the writer.
2. ROUND TRIP. read(write(m, r)) == (m, r) for the Commerce model as Sigma can
   say it, byte-identical across writes; parse(format(e)) == e per formula.
3. REFUSAL. The contract's own Commerce model is refused, by name, exactly
   where Sigma has no formula (CALCULATE overwrite, REMOVEFILTERS, time
   intelligence, RELATED inside a metric, a marked date table), and every
   window/rank/total function is refused when parsed.
4. SIGMA-AUTHORED FILES. tests/fixtures/sigma/native is written by hand in the
   documented shape - default titles, display-name columns, random column ids,
   a named relationship, custom SQL, a join - and reads to the model a Sigma
   user means, column types taken from the warehouse.
5. VALUES (needs world.semantic.render). The Sigma edition computes the same
   numbers as executive_report() on every visual it keeps; a join element
   fans out exactly as SQL does.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

WORLD_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORLD_DIR))

from world.semantic import examples as core  # noqa: E402
from world.semantic.errors import ModelError, Unsupported  # noqa: E402
from world.semantic.expr import (  # noqa: E402
    Agg,
    BinOp,
    Coalesce,
    Col,
    Divide,
    FilterCompare,
    FilterValues,
    Func,
    If,
    IterAgg,
    Lit,
    MeasureRef,
    Neg,
    Not,
    Related,
)
from world.semantic.formats import sigma  # noqa: E402
from world.semantic.formats.sigma import examples as sx  # noqa: E402
from world.semantic.formats.sigma.catalog import Catalog, model_type  # noqa: E402
from world.semantic.formats.sigma.datamodel import (  # noqa: E402
    model_format,
    read_data_model,
    sigma_format,
)
from world.semantic.formats.sigma.workbook import element_visual, parse_layout  # noqa: E402
from world.semantic.model import Page, Relationship, Report, Visual  # noqa: E402

try:
    from world.semantic import render
except ImportError:  # pragma: no cover - the engine is written concurrently
    render = None
needs_engine = pytest.mark.skipif(render is None, reason="world.semantic.render not present")

NATIVE = WORLD_DIR / "tests" / "fixtures" / "sigma" / "native"


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory):
    return core.write_fixture(tmp_path_factory.mktemp("sigma") / "commerce.duckdb")


@pytest.fixture
def project(tmp_path):
    sigma.write_project(sx.commerce_model(), (sx.executive_report(),), tmp_path / "sigma")
    return tmp_path / "sigma"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 1. shape
# Required keys, from the OpenAPI schemas (DataModelSpec; getWorkbook 200).
DATA_MODEL_KEYS = {"dataModelId", "name", "url", "documentVersion", "latestDocumentVersion",
                   "ownerId", "folderId", "createdBy", "updatedBy", "createdAt", "updatedAt",
                   "schemaVersion", "pages"}
WORKBOOK_KEYS = {"workbookId", "workbookUrlId", "name", "url", "path", "latestVersion",
                 "ownerId", "createdBy", "updatedBy", "createdAt", "updatedAt"}
CONTENTS_KEYS = {"schemaVersion", "kind", "pages", "elements", "layout"}


def test_the_data_model_file_is_the_spec_endpoints_shape(project):
    doc = load(project / "data-models" / "commerce.json")
    assert DATA_MODEL_KEYS <= set(doc) <= DATA_MODEL_KEYS | {"description"}
    assert doc["schemaVersion"] == 1
    (page,) = doc["pages"]
    assert set(page) == {"id", "name", "elements"}
    orders = next(el for el in page["elements"] if el["name"] == "Orders")
    assert orders["kind"] == "table"
    assert orders["source"] == {"connectionId": orders["source"]["connectionId"],
                                "kind": "warehouse-table",
                                "path": ["ANALYTICS", "WAREHOUSE", "FCT_ORDERS"]}
    amount = next(c for c in orders["columns"] if c.get("name") == "order_amount")
    # docs/example-representation-data-model-with-a-table-and-a-relationship:
    # {"id": "inode-<22>/START_STATION_ID", "formula": "[TRIP/Start Station Id]"}
    assert amount["formula"] == "[FCT_ORDERS/Order Amount]"
    assert amount["id"].startswith("inode-") and amount["id"].endswith("/ORDER_AMOUNT")
    assert len(amount["id"].split("/")[0]) == len("inode-") + 22
    assert orders["order"] == [c["id"] for c in orders["columns"]]
    rel = orders["relationships"][0]
    assert set(rel) == {"id", "targetElementId", "keys", "name"}
    assert set(rel["keys"][0]) == {"sourceColumnId", "targetColumnId"}
    metric = next(m for m in orders["metrics"] if m["name"] == "Net Revenue")
    assert metric["formula"] == 'SumIf([order_amount], [order_status] = "completed")'
    # docs/format-columns-and-metrics-in-the-code-representation-of-a-data-model
    assert metric["format"] == {"kind": "number", "formatString": "$,.2f",
                                "currencySymbol": "$"}


def test_the_workbook_file_is_the_get_with_contents_shape(project):
    doc = load(project / "workbooks" / "executive-overview.json")
    assert WORKBOOK_KEYS <= set(doc)
    assert set(doc["contents"]) == CONTENTS_KEYS
    contents = doc["contents"]
    assert contents["kind"] == "workbook" and contents["schemaVersion"] == 1
    placed = parse_layout(contents["layout"])
    assert list(placed) == [p["id"] for p in contents["pages"]]
    # "Every element in contents.elements must be placed in the layout"
    assert sorted(sum(placed.values(), [])) == sorted(e["id"] for e in contents["elements"])
    assert contents["layout"].startswith('<?xml version="1.0" encoding="utf-8"?>\n<Page '
                                         'type="grid" gridTemplateColumns="repeat(24, 1fr)"')
    kinds = {e["name"]: e for e in contents["elements"]}
    assert kinds["KPIs"]["kind"] == "pivot-table"          # three values, no rows: totals
    assert kinds["Top Products"]["groupings"][0]["groupBy"]
    bar = kinds["Revenue by Segment"]
    # OpenAPI: yAxis.columnIds, xAxis.columnId (+ sort {by, direction})
    assert set(bar["yAxis"]) == {"columnIds"} and "columnId" in bar["xAxis"]
    assert bar["xAxis"]["sort"]["direction"] == "descending"
    assert bar["source"]["kind"] == "data-model"
    month = kinds["Revenue by Month"]
    assert [c["formula"] for c in month["columns"][:2]] == ["[Orders/Date/year]",
                                                             "[Orders/Date/month]"]
    assert month["color"]["by"] == "category"


def test_write_is_byte_deterministic(tmp_path):
    a = sigma.write_project(sx.commerce_model(), (sx.executive_report(),), tmp_path / "a")
    b = sigma.write_project(sx.commerce_model(), (sx.executive_report(),), tmp_path / "b")
    assert [p.relative_to(tmp_path / "a") for p in a] == [p.relative_to(tmp_path / "b")
                                                          for p in b]
    for x, y in zip(a, b, strict=True):
        assert x.read_bytes() == y.read_bytes()
        text = x.read_text(encoding="utf-8")
        assert json.dumps(json.loads(text), indent=2, sort_keys=True,
                          ensure_ascii=False) + "\n" == text


# ------------------------------------------------------------- 2. round trip
def test_the_sigma_edition_round_trips(project):
    model, reports = sigma.read_project(project)
    assert model == sx.commerce_model()
    assert reports == (sx.executive_report(),)


def test_round_trip_with_types_from_the_warehouse(project, warehouse):
    model, _ = sigma.read_project(project, warehouse=warehouse)
    assert model == sx.commerce_model()


def test_the_sigma_edition_differs_from_the_contract_only_where_sigma_must():
    core_model, edition = core.commerce_model(), sx.commerce_model()
    kept = {m.name for m in edition.measures}
    assert kept == {"Gross Revenue", "Net Revenue", "Order Count", "Completed Orders", "AOV",
                    "Units", "List Price Revenue"}
    same = {m.name for m in core_model.measures if m in edition.measures}
    assert same == {"Gross Revenue", "Order Count", "AOV", "Units"}
    assert edition.relationships == core_model.relationships


# --------------------------------------------------------------- 3. refusal
def test_the_contract_commerce_model_is_refused_by_name(tmp_path):
    with pytest.raises(Unsupported, match="marked date table"):
        sigma.write_project(core.commerce_model(), (core.executive_report(),), tmp_path)
    model = core.commerce_model()
    refused = {}
    for m in model.measures:
        try:
            sigma.format_expression(m.expr, model, table=m.table)
        except Unsupported as exc:
            assert exc.vendor == "Sigma"
            refused[m.name] = exc.feature
    assert refused == {
        "Net Revenue": "CALCULATE filter overwrite",
        "Completed Orders": "CALCULATE filter overwrite",
        "List Price Revenue": "Lookup in a metric",
        "Segment Share": "REMOVEFILTERS / ALL",
        "Revenue YTD": "time intelligence (ytd)",
        "Revenue LY": "time intelligence (same_period_last_year)",
    }


def test_removefilters_alone_is_named():
    model = core.commerce_model()
    share = next(m for m in model.measures if m.name == "Segment Share").expr
    with pytest.raises(Unsupported, match="REMOVEFILTERS"):
        sigma.format_expression(share.den, model, table="Orders")


@pytest.mark.parametrize("formula, feature", [
    ("CumulativeSum([quantity])", "CumulativeSum"),
    ("MovingAvg(Sum([quantity]), 3)", "MovingAvg"),
    ("Rank(Sum([quantity]))", "Rank"),
    ("RowNumber()", "RowNumber"),
    ("GrandTotal(Sum([quantity]))", "GrandTotal"),
    ("PercentOfTotal(Sum([quantity]))", "PercentOfTotal"),
    ("Subtotal(Sum([quantity]), \"x\")", "Subtotal"),
    ("Lag([quantity])", "Lag"),
    ("Lead([quantity])", "Lead"),
    ("DateLookback(Sum([quantity]), [order_date], 1, \"year\")", "DateLookback"),
    ("Median([quantity])", "Median"),
    ("CountDistinctIf([order_id], [quantity] > 1)", "CountDistinctIf"),
    ("Rollup(Sum([quantity]), [order_id], [Orders/order_id])", "Rollup"),
    ("Sum([quantity] ^ 2)", "operator ^ (power)"),
    ("Sum([quantity] % 2)", "operator % (modulo)"),
    ("Frobnicate([quantity])", "Frobnicate"),
    ('Sum(DateTrunc("week", [order_date]) = [order_date])', 'DateTrunc("week")'),
    ("Sum(Len(Left([order_date], -2)))", "Left with a negative or non-literal length"),
    ("Sum(Year([order_date], \"America/New_York\"))", "Year with a time zone"),
])
def test_window_rank_and_unknown_functions_are_refused(formula, feature):
    model = sx.commerce_model()
    with pytest.raises(Unsupported) as exc:
        sigma.parse_expression(formula, model, table="Order Lines")
    assert exc.value.feature == feature and exc.value.vendor == "Sigma"


def test_counting_text_is_exact_or_refused():
    model = sx.commerce_model()
    status = Col("Orders", "order_status")
    # Sigma's Count skips Null AND empty values (docs/count)
    assert sigma.parse_expression("Count([order_status])", model, table="Orders") == \
        IterAgg("count", "Orders", If(BinOp("<>", status, Lit("")), status))
    assert sigma.parse_expression("Count([order_id])", model, table="Orders") == \
        Agg("count", Col("Orders", "order_id"))
    with pytest.raises(Unsupported, match="CountDistinct of a text column"):
        sigma.parse_expression("CountDistinct([order_status])", model, table="Orders")
    with pytest.raises(Unsupported, match="Count of a text column"):
        sigma.format_expression(Agg("count", status), model, table="Orders")


def test_lookup_lowers_only_along_a_relationship():
    model = sx.commerce_model()
    assert sigma.parse_expression("Lookup([Product/unit_price], [product_id], "
                                  "[Product/product_id])", model, table="Order Lines") == \
        Related("Product", "unit_price")
    with pytest.raises(Unsupported, match="Lookup without a matching relationship"):
        sigma.parse_expression("Lookup([Product/unit_price], [quantity], [Product/product_id])",
                               model, table="Order Lines")
    with pytest.raises(Unsupported, match="Lookup outside a calculated column"):
        sigma.parse_expression("Sum(Lookup([Product/unit_price], [product_id], "
                               "[Product/product_id]))", model, table="Order Lines")


# ------------------------------------------------------- 2b. formula round trip
STATUS, AMOUNT = Col("Orders", "order_status"), Col("Orders", "order_amount")
ORDER_DATE = Col("Orders", "order_date")
DONE = BinOp("=", STATUS, Lit("completed"))
CASES = [
    # (table, Sigma text, tree); format(tree) == text and parse(text) == tree
    ("Orders", "Sum([order_amount])", Agg("sum", AMOUNT)),
    ("Orders", "Avg([order_amount])", Agg("avg", AMOUNT)),
    ("Orders", "Min([order_date])", Agg("min", ORDER_DATE)),
    ("Orders", "Max([order_amount])", Agg("max", AMOUNT)),
    ("Orders", "Count()", Agg("count_rows", table="Orders")),
    ("Orders", "CountDistinct([customer_id])", Agg("count_distinct", Col("Orders",
                                                                        "customer_id"))),
    ("Orders", 'SumIf([order_amount], [order_status] = "completed")',
     IterAgg("sum", "Orders", If(DONE, AMOUNT))),
    ("Orders", 'AvgIf([order_amount], [order_status] = "completed" and [order_amount] > 10)',
     IterAgg("avg", "Orders", If(BinOp("and", DONE, BinOp(">", AMOUNT, Lit(10))), AMOUNT))),
    ("Orders", 'CountIf([order_status] != "cancelled")',
     IterAgg("count", "Orders", If(BinOp("<>", STATUS, Lit("cancelled")), Lit(1)))),
    ("Orders", "Sum([order_amount] - Zn([shipping_amount]))",
     IterAgg("sum", "Orders", BinOp("-", AMOUNT, Coalesce((Col("Orders", "shipping_amount"),
                                                           Lit(0)))))),
    ("Orders", "[Metrics/Net Revenue] / NullIf([Metrics/Completed Orders], 0)",
     Divide(MeasureRef("Net Revenue"), MeasureRef("Completed Orders"))),
    ("Orders", "If(Zn([Metrics/Order Count]) = 0, 0, [Metrics/Gross Revenue] / "
               "[Metrics/Order Count])",
     Divide(MeasureRef("Gross Revenue"), MeasureRef("Order Count"), Lit(0))),
    ("Orders", "[Metrics/Gross Revenue] / [Metrics/Order Count]",
     BinOp("/", MeasureRef("Gross Revenue"), MeasureRef("Order Count"))),
    ("Orders", "([Metrics/Net Revenue] - [Metrics/Gross Revenue]) * -1",
     BinOp("*", BinOp("-", MeasureRef("Net Revenue"), MeasureRef("Gross Revenue")), Lit(-1))),
    # row-level (calculated column) formulas
    ("Orders", 'If([order_amount] > 100, "large", [order_amount] > 20, "medium", "small")',
     If(BinOp(">", AMOUNT, Lit(100)), Lit("large"),
        If(BinOp(">", AMOUNT, Lit(20)), Lit("medium"), Lit("small")))),
    ("Orders", 'If([order_status] = "refunded", True)',
     If(BinOp("=", STATUS, Lit("refunded")), Lit(True))),
    ("Orders", "Coalesce([shipping_amount], [order_amount], 0)",
     Coalesce((Col("Orders", "shipping_amount"), AMOUNT, Lit(0)))),
    ("Orders", "IsNull([shipping_amount])",
     Coalesce((BinOp("<>", Col("Orders", "shipping_amount"), Col("Orders", "shipping_amount")),
               Lit(True)))),
    ("Orders", "IsNotNull([shipping_amount])",
     Coalesce((BinOp("=", Col("Orders", "shipping_amount"), Col("Orders", "shipping_amount")),
               Lit(False)))),
    ("Orders", "NullIf([shipping_amount], 0)",
     If(BinOp("=", Col("Orders", "shipping_amount"), Lit(0)), Lit(None),
        Col("Orders", "shipping_amount"))),
    ("Orders", 'DateTrunc("month", [order_date])', Func("date_trunc_month", (ORDER_DATE,))),
    ("Orders", 'DateTrunc("quarter", [order_date])', Func("date_trunc_quarter", (ORDER_DATE,))),
    ("Orders", 'DateTrunc("year", [order_date])', Func("date_trunc_year", (ORDER_DATE,))),
    ("Orders", 'DateTrunc("week_starting_monday", [order_date])',
     Func("date_trunc_week", (ORDER_DATE,))),
    ("Orders", "Year([order_date]) * 100 + Month([order_date])",
     BinOp("+", BinOp("*", Func("year", (ORDER_DATE,)), Lit(100)), Func("month", (ORDER_DATE,)))),
    ("Orders", "Quarter([order_date]) + Day([order_date]) + Weekday([order_date])",
     BinOp("+", BinOp("+", Func("quarter", (ORDER_DATE,)), Func("day", (ORDER_DATE,))),
           Func("weekday", (ORDER_DATE,)))),
    ("Orders", 'Upper(Left([order_status], 3)) & "-" & Lower(Right(Trim([order_status]), 2))',
     BinOp("&", BinOp("&", Func("upper", (Func("left", (STATUS, Lit(3))),)), Lit("-")),
           Func("lower", (Func("right", (Func("trim", (STATUS,)), Lit(2))),)))),
    ("Orders", "Len([order_status]) >= 5 or Not([order_amount] < 0)",
     BinOp("or", BinOp(">=", Func("len", (STATUS,)), Lit(5)),
           Not(BinOp("<", AMOUNT, Lit(0))))),
    ("Orders", "Abs(Round([order_amount], 1)) + Floor([order_amount]) - Ceiling([order_amount])",
     BinOp("-", BinOp("+", Func("abs", (Func("round", (AMOUNT, Lit(1))),)),
                      Func("floor", (AMOUNT,))), Func("ceiling", (AMOUNT,)))),
    ("Orders", 'If([order_date] >= Date("2026-01-01"), 2026, 2025)',
     If(BinOp(">=", ORDER_DATE, Lit(dt.date(2026, 1, 1))), Lit(2026), Lit(2025))),
    ("Orders", "[order_amount] * 1.25", BinOp("*", AMOUNT, Lit(Decimal("1.25")))),
    ("Order Lines", "Lookup([Product/unit_price], [product_id], [Product/product_id]) * "
                    "[quantity]",
     BinOp("*", Related("Product", "unit_price"), Col("Order Lines", "quantity"))),
]


@pytest.mark.parametrize("table, text, tree", CASES, ids=[c[1][:40] for c in CASES])
def test_formula_round_trip(table, text, tree):
    model = sx.commerce_model()
    assert sigma.parse_expression(text, model, table=table) == tree
    assert sigma.format_expression(tree, model, table=table) == text


TRICKY = [Lit("it's"), Lit('say "hi"'), Neg(AMOUNT), Neg(Lit(5)), Lit(-5),
          BinOp("-", AMOUNT, BinOp("-", AMOUNT, Lit(1))),
          BinOp("&", BinOp("=", STATUS, Lit("x")), Lit("y")),
          Not(BinOp("and", BinOp(">", AMOUNT, Lit(1)), Lit(True))),
          BinOp("*", Lit(Decimal("-1.5")), AMOUNT),
          # a plain division by a NullIf must not come back as Divide
          BinOp("/", AMOUNT, If(BinOp("=", AMOUNT, Lit(0)), Lit(None), AMOUNT)),
          If(Lit(True), Lit(1), If(BinOp("=", AMOUNT, Lit(0)), Lit(None), AMOUNT)),
          BinOp("-", Lit(1), Neg(AMOUNT)), Divide(AMOUNT, Divide(AMOUNT, AMOUNT))]


@pytest.mark.parametrize("tree", TRICKY, ids=repr)
def test_quoting_precedence_and_look_alike_shapes_round_trip(tree):
    model = sx.commerce_model()
    text = sigma.format_expression(tree, model, table="Orders")
    assert sigma.parse_expression(text, model, table="Orders") == tree


def test_parse_accepts_the_other_spellings_sigma_allows():
    model = sx.commerce_model()
    parse = sigma.parse_expression
    assert parse('Sum(If([order_status] = "completed", [order_amount]))', model,
                 table="Orders") == IterAgg("sum", "Orders", If(DONE, AMOUNT))
    assert parse('SumIf([order_amount], [order_status] = "completed", [order_amount] > 10)',
                 model, table="Orders") == parse(
        'SumIf([order_amount], [order_status] = "completed" and [order_amount] > 10)',
        model, table="Orders")
    assert parse('Switch([order_status], "completed", 1, "refunded", -1, 0)', model,
                 table="Orders") == If(DONE, Lit(1), If(BinOp("=", STATUS, Lit("refunded")),
                                                        Lit(-1), Lit(0)))
    assert parse('In([order_status], "completed", "refunded")', model, table="Orders") == \
        BinOp("or", DONE, BinOp("=", STATUS, Lit("refunded")))
    assert parse("Between([order_amount], 1, 5)", model, table="Orders") == \
        BinOp("and", BinOp(">=", AMOUNT, Lit(1)), BinOp("<=", AMOUNT, Lit(5)))
    assert parse("And([order_amount] > 1, Or(True, False))", model, table="Orders") == \
        BinOp("and", BinOp(">", AMOUNT, Lit(1)), BinOp("or", Lit(True), Lit(False)))
    assert parse("[order_amount] <> 1", model, table="Orders") == \
        parse("[order_amount] != 1", model, table="Orders")
    assert parse("sum([order_amount])", model, table="Orders") == Agg("sum", AMOUNT)


def test_workbook_scope_formulas_round_trip_without_a_table():
    model = sx.commerce_model()
    tree = IterAgg("sum", "Order Lines", BinOp("*", Col("Order Lines", "quantity"),
                                                Related("Product", "unit_price")))
    text = "Sum([Order Lines/quantity] * [Order Lines/Product/unit_price])"
    assert sigma.format_expression(tree, model) == text
    assert sigma.parse_expression(text, model) == tree
    assert sigma.parse_expression("[Metrics/AOV] * 2", model) == \
        BinOp("*", MeasureRef("AOV"), Lit(2))
    with pytest.raises(ModelError, match="needs the element"):
        sigma.parse_expression("Count()", model)


# ------------------------------------------------------ 4. Sigma-authored files
def test_a_sigma_authored_project_reads_as_its_author_means(warehouse):
    model, (report,) = sigma.read_project(NATIVE, warehouse=warehouse)
    tables = {t.name: t for t in model.tables}
    assert list(tables) == ["FCT_ORDERS", "Order Lines", "DIM_CUSTOMER", "Products", "Refunds",
                            "Orders x Lines"]
    orders = tables["FCT_ORDERS"]                      # unnamed: titled by its table
    assert [(c.name, c.source, c.data_type) for c in orders.columns[:6]] == [
        ("Order Id", "order_id", "int"), ("Order Date", "order_date", "date"),
        ("Customer Id", "customer_id", "int"), ("Order Status", "order_status", "string"),
        ("Order Amount", "order_amount", "decimal"),
        # a random id: the warehouse column is found by its Sigma title
        ("Shipping Amount", "shipping_amount", "decimal")]
    net_of_shipping = orders.columns[6]
    assert net_of_shipping.expr == BinOp("-", Col("FCT_ORDERS", "Order Amount"),
                                         Coalesce((Col("FCT_ORDERS", "Shipping Amount"),
                                                   Lit(0))))
    assert net_of_shipping.data_type == "decimal"
    assert tables["Order Lines"].columns[-1].expr == Related("Products", "Unit Price")
    assert tables["Refunds"].source_sql.startswith("SELECT order_id")
    assert [(c.name, c.data_type) for c in tables["Refunds"].columns] == \
        [("order_id", "int"), ("refund_amount", "decimal")]
    assert model.relationships == (
        Relationship("FCT_ORDERS", "Customer Id", "DIM_CUSTOMER", "Customer Id"),
        Relationship("Order Lines", "Product Id", "Products", "Product Id"))
    measures = {m.name: m for m in model.measures}
    assert measures["Gross Revenue"].format_string == "$0.00"      # d3 "$.2f"
    assert measures["Net Revenue"].expr == IterAgg(
        "sum", "FCT_ORDERS", If(BinOp("=", Col("FCT_ORDERS", "Order Status"), Lit("completed")),
                                Col("FCT_ORDERS", "Order Amount")))
    visuals = {v.name: v for v in report.pages[0].visuals}
    assert list(visuals) == ["Gross", "Completed revenue by segment", "Top Products"]
    assert visuals["Gross"] == Visual("Gross", values=(("Gross Revenue",
                                                        MeasureRef("Gross Revenue")),),
                                      kind="card", groups="rows")
    # a Sigma element keeps every group its source has rows for
    assert {v.groups for v in visuals.values()} == {"rows"}
    seg = visuals["Completed revenue by segment"]
    # "[FCT_ORDERS/Ordered by/Segment]": through the relationship named "Ordered by"
    assert seg.group_by == (Col("DIM_CUSTOMER", "Segment"),)
    assert seg.values == (("Revenue", Agg("sum", Col("FCT_ORDERS", "Order Amount"))),)
    assert seg.filters == (FilterValues(Col("FCT_ORDERS", "Order Status"), ("completed",)),
                           FilterCompare(Col("FCT_ORDERS", "Order Amount"), ">=", Decimal(10)))
    assert seg.order_by == (("Revenue", True),) and seg.kind == "barChart"
    top = visuals["Top Products"]
    assert (top.group_by, top.order_by, top.top_n) == (
        (Col("Products", "Product Name"),), (("Units", True),), 2)


def test_without_a_warehouse_or_snapshot_a_column_type_is_a_named_error():
    with pytest.raises(ModelError, match="sync the connection"):
        sigma.read_project(NATIVE)


def test_a_join_element_keeps_its_fan_out(warehouse):
    model, _ = sigma.read_project(NATIVE, warehouse=warehouse)
    joined = next(t for t in model.tables if t.name == "Orders x Lines")
    with duckdb.connect() as con:
        con.execute(f"ATTACH '{warehouse}' AS wh (READ_ONLY)")
        con.execute("USE wh")
        rows, fanned = con.execute(
            f'SELECT count(*), sum("Order Amount") FROM ({joined.source_sql})').fetchone()
        lines, = con.execute("SELECT count(*) FROM warehouse.fct_order_lines").fetchone()
        once, = con.execute("SELECT sum(order_amount) FROM warehouse.fct_orders").fetchone()
    assert rows == lines                      # one row per order line, as the join makes it
    assert fanned > once                      # an order's amount counted once per line
    measure = next(m for m in model.measures if m.name == "Fanned-out Revenue")
    assert measure.expr == Agg("sum", Col("Orders x Lines", "Order Amount"))


def test_a_join_round_trips_as_custom_sql(tmp_path, warehouse):
    model, _ = sigma.read_project(NATIVE, warehouse=warehouse)
    sigma.write_project(model, (), tmp_path / "p")
    doc = load(tmp_path / "p" / "data-models" / "commerce-sigma-authored.json")
    joined = next(e for e in doc["pages"][0]["elements"] if e["name"] == "Orders x Lines")
    assert joined["source"]["kind"] == "sql"
    again, _ = sigma.read_project(tmp_path / "p")
    assert again == model


# ------------------------------------------------- workbook and model refusals
@pytest.fixture(scope="module")
def catalog(warehouse):
    return Catalog.from_warehouse(warehouse)


def _visual(el: dict, catalog: Catalog):
    model, index = read_data_model(load(NATIVE / "data-models" / "commerce-native.json"),
                                   catalog)
    return element_visual(el, model, index)


def _top_products(**changes) -> dict:
    wb = load(NATIVE / "workbooks" / "sales-review.json")
    el = next(e for e in wb["contents"]["elements"] if e["id"] == "Tbl3pR8uIo")
    el.update(changes)
    return el


@pytest.mark.parametrize("changes, feature", [
    ({"filters": [{"id": "f", "columnId": "Tc2hJ7kLzx", "kind": "top-n",
                   "rankingFunction": "rank", "mode": "top-n", "rowCount": 2}]},
     "Top N filter ranked by rank"),
    ({"filters": [{"id": "f", "columnId": "Tc2hJ7kLzx", "kind": "number-range", "min": 3}]},
     "number-range filter on an aggregate column"),
    ({"groupings": []}, "ungrouped table element"),
    ({"groupings": [{"id": "a", "groupBy": ["Tc1yU5iOpa"], "calculations": []},
                    {"id": "b", "groupBy": [], "calculations": ["Tc2hJ7kLzx"]}]},
     "nested groupings"),
    ({"kind": "control"}, "control element"),
    ({"kind": "point-map"}, "point-map element"),
    ({"source": {"kind": "warehouse-table", "connectionId": "c",
                 "path": ["ANALYTICS", "WAREHOUSE", "FCT_ORDER_LINES"]}},
     "workbook element on a warehouse-table source"),
    ({"columns": [{"id": "Tc1yU5iOpa", "formula": "Upper([Order Lines/Products/Product Name])"},
                  {"id": "Tc2hJ7kLzx", "formula": "[Metrics/Units]"}]},
     "grouping by a calculated column"),
])
def test_workbook_elements_outside_the_subset_are_refused(catalog, changes, feature):
    with pytest.raises(Unsupported) as exc:
        _visual(_top_products(**changes), catalog)
    assert exc.value.feature == feature


def test_a_trellis_chart_is_refused(catalog):
    wb = load(NATIVE / "workbooks" / "sales-review.json")
    bar = next(e for e in wb["contents"]["elements"] if e["id"] == "Bar2nE6tYu")
    bar["trellis"] = {"columnsBy": [{"columnId": "Bc4zX9cVbn"}]}
    with pytest.raises(Unsupported, match="trellis"):
        _visual(bar, catalog)


@pytest.mark.parametrize("change, feature", [
    (lambda el: el.update(filters=[{"id": "f", "columnId": "x", "kind": "list"}]),
     "element filter in a data model element"),
    (lambda el: el["relationships"][0]["keys"].append(el["relationships"][0]["keys"][0]),
     "relationship with several keys"),
    (lambda el: el.update(source={"kind": "csv-table", "connectionId": "c", "inodeId": "i"}),
     "csv-table source in a data model element"),
])
def test_data_model_constructs_outside_the_subset_are_refused(catalog, change, feature):
    doc = load(NATIVE / "data-models" / "commerce-native.json")
    change(doc["pages"][0]["elements"][0])
    with pytest.raises(Unsupported) as exc:
        read_data_model(doc, catalog)
    assert exc.value.feature == feature


@pytest.mark.parametrize("change, feature", [
    (lambda m: replace(m, null_semantics="dax"), "DAX blank arithmetic (null_semantics='dax')"),
    (lambda m: replace(m, relationships=(replace(m.relationships[0],
                                                 cardinality="one_to_one"),)),
     "one_to_one relationship cardinality"),
    (lambda m: replace(m, relationships=(replace(m.relationships[0], cross_filter="both"),)),
     "bidirectional relationship"),
    (lambda m: replace(m, relationships=(replace(m.relationships[0], active=False),)),
     "inactive relationship"),
])
def test_model_constructs_sigma_cannot_say_are_refused(tmp_path, change, feature):
    model = change(sx.commerce_model())
    with pytest.raises(Unsupported) as exc:
        sigma.write_project(model, (), tmp_path)
    assert exc.value.feature == feature


@pytest.mark.parametrize("visual, feature", [
    (Visual("x", group_by=(Col("Date", "year"), Col("Date", "month"), Col("Customer", "region")),
            values=(("n", MeasureRef("Net Revenue")),), kind="lineChart", groups="rows"),
     "chart with more than two grouping columns (trellis)"),
    (Visual("x", values=(("n", MeasureRef("Net Revenue")), ("u", MeasureRef("Units"))),
            kind="card"),
     "element mixing metrics of several data model elements"),
    (Visual("x", group_by=(Col("Customer", "region"),), values=(("n", MeasureRef("Units")),),
            filters=(FilterCompare(Col("Orders", "order_amount"), ">", 3),), kind="table",
            groups="rows"),
     "filter > on a number"),
    (Visual("x", group_by=(Col("Customer", "region"),),
            values=(("n", MeasureRef("Net Revenue")),), kind="table"),
     "dropping all-blank groups (groups='measures')"),
    (Visual("x", values=(("n", MeasureRef("Net Revenue")),), kind="gauge"), "visual kind gauge"),
])
def test_report_constructs_sigma_cannot_say_are_refused(tmp_path, visual, feature):
    report = Report("R", "Commerce", (Page("P", (visual,)),))
    with pytest.raises(Unsupported) as exc:
        sigma.write_project(sx.commerce_model(), (report,), tmp_path)
    assert exc.value.feature == feature


def test_groups_measures_is_refused_where_it_could_change_the_rows(tmp_path):
    """Power BI's report drops all-blank groups; a Sigma element never does.
    Grouped visuals must say groups='rows'; an ungrouped card is one row either
    way, so it is written and reads back as 'rows'."""
    with pytest.raises(Unsupported, match="dropping all-blank groups"):
        sigma.write_project(sx.commerce_model(), (replace(
            sx.executive_report(), name="Measures", pages=core.executive_report().pages[1:]),),
            tmp_path / "a")
    card = Visual("KPI", values=(("Net Revenue", MeasureRef("Net Revenue")),), kind="card")
    assert card.groups == "measures"
    sigma.write_project(sx.commerce_model(), (Report("Card", "Commerce",
                                                     (Page("P", (card,)),)),), tmp_path / "b")
    _, (back,) = sigma.read_project(tmp_path / "b")
    assert back.pages[0].visuals == (replace(card, groups="rows"),)
    assert {v.groups for p in sx.executive_report().pages for v in p.visuals} == {"rows"}


def test_filters_and_sorts_round_trip(tmp_path):
    visual = Visual(
        "Filtered", group_by=(Col("Customer", "segment"),),
        values=(("Net Revenue", MeasureRef("Net Revenue")), ("Orders", MeasureRef("Order Count"))),
        filters=(FilterValues(Col("Customer", "region"), ("EMEA",), negate=True),
                 FilterCompare(Col("Orders", "order_amount"), ">=", Decimal("10.5")),
                 FilterCompare(Col("Orders", "order_amount"), "<=", 500),
                 FilterCompare(Col("Date", "date_day"), ">=", dt.date(2025, 6, 1)),
                 FilterCompare(Col("Date", "date_day"), "<=", dt.date(2025, 12, 31)),
                 FilterValues(Col("Date", "date_day"), (dt.date(2025, 7, 4),))),
        order_by=(("Orders", False), ("segment", True)), top_n=None, kind="table",
        groups="rows")
    report = Report("Filters", "Commerce", (Page("P", (visual,)),))
    sigma.write_project(sx.commerce_model(), (report,), tmp_path)
    _, (back,) = sigma.read_project(tmp_path)
    assert back == report


# ------------------------------------------------------------ odds and ends
@pytest.mark.parametrize("excel, d3", [
    ("$#,0.00", {"kind": "number", "formatString": "$,.2f", "currencySymbol": "$"}),
    ("0.0%", {"kind": "number", "formatString": ".1%"}),
    ("#,0", {"kind": "number", "formatString": ",.0f"}),
    ("0.000 units", {"kind": "number", "formatString": ".3f", "suffix": " units"}),
    ("EUR #,0.00", {"kind": "number", "formatString": ",.2f", "prefix": "EUR "}),
])
def test_number_formats_convert_both_ways(excel, d3):
    assert sigma_format(excel) == d3
    assert model_format(d3) == excel


def test_formats_outside_the_subset_are_refused():
    with pytest.raises(Unsupported):
        sigma_format("#,##0.00;(#,##0.00)")
    with pytest.raises(Unsupported):
        model_format({"kind": "number", "formatString": ".2s"})
    with pytest.raises(Unsupported):
        model_format({"kind": "datetime", "formatString": "%Y"})


@pytest.mark.parametrize("native, dtype", [
    ("INTEGER", "int"), ("BIGINT", "int"), ("NUMBER(38,0)", "int"), ("NUMBER", "int"),
    ("DECIMAL(12,2)", "decimal"), ("NUMBER(12,2)", "decimal"), ("DOUBLE", "float"),
    ("VARCHAR", "string"), ("BOOLEAN", "bool"), ("DATE", "date"),
    ("TIMESTAMP WITH TIME ZONE", "timestamp"),
])
def test_warehouse_types_map_to_model_types(native, dtype):
    assert model_type(native) == dtype


def test_a_written_project_is_found_by_a_fresh_reader(tmp_path, project):
    copy = tmp_path / "copy"
    shutil.copytree(project, copy)
    assert sigma.read_project(copy)[0] == sx.commerce_model()


# ------------------------------------------------------------------ 5. values
@needs_engine
def test_the_sigma_edition_computes_the_contract_reports_numbers(warehouse):
    model, report = sx.commerce_model(), sx.executive_report()
    core_model, core_report = core.commerce_model(), core.executive_report()
    for page, core_page in zip(report.pages, core_report.pages, strict=True):
        for visual, core_visual in zip(page.visuals, core_page.visuals, strict=True):
            mine = render.render_visual(model, visual, warehouse)
            theirs = render.render_visual(core_model, core_visual, warehouse)
            keep = [theirs["columns"].index(c) for c in mine["columns"]]
            assert mine["rows"] == [[row[i] for i in keep] for row in theirs["rows"]], \
                visual.name


@needs_engine
def test_a_sigma_authored_workbook_agrees_with_the_warehouse(warehouse):
    model, (report,) = sigma.read_project(NATIVE, warehouse=warehouse)
    visuals = {v.name: v for v in report.pages[0].visuals}
    with duckdb.connect() as con:
        con.execute(f"ATTACH '{warehouse}' AS wh (READ_ONLY)")
        seg = con.execute("""
            SELECT c.segment, sum(o.order_amount) FROM wh.warehouse.fct_orders o
            LEFT JOIN wh.warehouse.dim_customer c USING (customer_id)
            WHERE o.order_status = 'completed' AND o.order_amount >= 10
            GROUP BY 1 ORDER BY 2 DESC, 1""").fetchall()
        top = con.execute("""
            SELECT p.product_name, sum(l.quantity) FROM wh.warehouse.fct_order_lines l
            LEFT JOIN wh.warehouse.dim_product p USING (product_id)
            GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 2""").fetchall()
    got = render.render_visual(model, visuals["Completed revenue by segment"], warehouse)
    assert got["rows"] == [[s, f"{v:.2f}"] for s, v in seg]
    got = render.render_visual(model, visuals["Top Products"], warehouse)
    assert got["rows"] == [[p, int(u)] for p, u in top]


def _cancelled_by_region(years: tuple) -> Visual:
    filters = (FilterValues(Col("Orders", "order_status"), ("cancelled",)),)
    if years:
        filters += (FilterValues(Col("Date", "year"), years),)
    return Visual("Cancelled by region", group_by=(Col("Customer", "region"),),
                  values=(("Net Revenue", MeasureRef("Net Revenue")),), filters=filters,
                  kind="table", groups="rows")


def _regions_with_rows(warehouse, years: tuple) -> list:
    """Hand-written SQL over the element's source, Orders: the groups a Sigma
    element built on Orders shows."""
    where = "o.order_status = 'cancelled'"
    if years:
        where += f" AND year(o.order_date) IN ({', '.join(map(str, years))})"
    with duckdb.connect() as con:
        con.execute(f"ATTACH '{warehouse}' AS wh (READ_ONLY)")
        return [r for (r,) in con.execute(f"""
            SELECT DISTINCT c.region FROM wh.warehouse.fct_orders o
            LEFT JOIN wh.warehouse.dim_customer c USING (customer_id)
            WHERE {where} ORDER BY 1""").fetchall()]


@needs_engine
def test_a_sigma_element_keeps_all_blank_groups_that_have_rows(warehouse):
    """Sigma's grouping keeps a group whose every value is null when the
    element's source has rows for it: Net Revenue (completed orders only) over
    cancelled orders is null in every region that has one."""
    got = render.render_visual(sx.commerce_model(), _cancelled_by_region(()), warehouse)
    regions = _regions_with_rows(warehouse, ())
    assert regions == ["APAC", "EMEA", "NA"]
    assert got["rows"] == [[r, None] for r in regions]


@needs_engine
def test_a_sigma_element_shows_only_groups_its_source_has_rows_for(warehouse):
    # Under groups='rows' the base is the table the values aggregate (Orders),
    # not the grouping columns' table (Customer): only regions with a cancelled
    # 2026 order appear (model.py Visual.groups).
    got = render.render_visual(sx.commerce_model(), _cancelled_by_region((2026,)), warehouse)
    regions = _regions_with_rows(warehouse, (2026,))
    assert regions == ["EMEA"]
    assert got["rows"] == [[r, None] for r in regions]
