"""The Power BI front-end: PBIP files, DAX measures, DAX queries.

What is claimed, and how each claim is checked:

1. ROUND TRIP. The Commerce model and the Executive Overview report written
   as a PBIP project read back equal - the model up to the order of measures
   across home tables, which TMDL cannot carry (a measure lives in its table's
   file; `in_table_order` below, and the coordinator report).
2. REAL FORMATS. Every JSON file written validates against Microsoft's
   published schemas (github.com/microsoft/json-schemas, MIT, vendored under
   fixtures/powerbi/json-schemas at commit 5e9ded2d), with a negative control
   so the validator is shown to bite. TMDL text is spot-checked against what
   Desktop writes, and Desktop-written TMDL (fixtures/powerbi/samples: MIT
   samples from github.com/RuiRomano/pbip-demo-agentic and
   github.com/microsoft/fabric-toolbox) parses.
3. DAX. Every example measure round-trips text -> tree -> text -> tree, the
   lowering follows DAX's CALCULATE order, and everything outside the subset is
   refused by its Power BI name.
4. DAX QUERIES lower into the Visual the engine evaluates, and results come
   back in the executeQueries JSON shape.

Engine-dependent checks (world.semantic.render) are skipped until the engine
lands; the coordinator re-runs them.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

WORLD_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "powerbi"
sys.path.insert(0, str(WORLD_DIR))

from world.semantic.errors import ModelError, Unsupported  # noqa: E402
from world.semantic.examples import commerce_model, executive_report  # noqa: E402
from world.semantic.expr import (  # noqa: E402
    Agg,
    BinOp,
    Calculate,
    Col,
    Func,
    If,
    IterAgg,
    KeepFilters,
    KeepOnly,
    Lit,
    MeasureRef,
    Related,
    RemoveFilters,
    TimeShift,
)
from world.semantic.expr import FilterCompare as FC  # noqa: E402
from world.semantic.expr import FilterValues as FV  # noqa: E402
from world.semantic.formats import powerbi  # noqa: E402
from world.semantic.formats.powerbi import (  # noqa: E402
    dax,
    daxquery,
    mquery,
    pbir,
    project,
    tmdl,
)
from world.semantic.model import Column, Page, Report, Table, Visual  # noqa: E402

MODEL = commerce_model("dax")
REPORT = executive_report()


def in_table_order(model):
    """Measures grouped by home table in table order, each table's in model
    order: the only order a TMDL folder can hold."""
    order = [t.name for t in model.tables]
    measures = sorted(model.measures, key=lambda m: order.index(m.table))
    return replace(model, measures=tuple(measures))


@pytest.fixture
def written(tmp_path):
    dest = tmp_path / "bi" / "powerbi"
    powerbi.write_project(MODEL, (REPORT,), dest)
    return dest


# ======================================================================
# 1. Round trip and determinism
# ======================================================================
def test_project_round_trips(written):
    model, reports = powerbi.read_project(written)
    assert model == in_table_order(MODEL)
    assert reports == (REPORT,)


def test_read_project_accepts_the_pbip_file_and_each_item_folder(written):
    expected = in_table_order(MODEL)
    assert powerbi.read_project(written / "Executive Overview.pbip") == (expected, (REPORT,))
    assert powerbi.read_project(written / "Executive Overview.Report") == (expected, (REPORT,))
    assert powerbi.read_project(written / "Commerce.SemanticModel") == (expected, ())


def test_write_is_deterministic_and_clears_stale_definitions(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    files_a = powerbi.write_project(MODEL, (REPORT,), a)
    powerbi.write_project(MODEL, (REPORT,), b)
    rel = sorted(p.relative_to(a).as_posix() for p in files_a)
    assert rel == sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    for name in rel:
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    # One page fewer: its folder must not survive as a stale page.
    shorter = replace(REPORT, pages=REPORT.pages[:1])
    powerbi.write_project(MODEL, (shorter,), a)
    assert powerbi.read_project(a)[1] == (shorter,)


def test_ids_are_the_shape_power_bi_uses(written):
    pages = json.loads((written / "Executive Overview.Report/definition/pages/pages.json")
                       .read_text())
    assert len(pages["pageOrder"]) == 2
    for pid in pages["pageOrder"]:
        assert len(pid) == 20 and all(c in "0123456789abcdef" for c in pid)
        for visual in (written / "Executive Overview.Report/definition/pages" / pid /
                       "visuals").iterdir():
            assert len(visual.name) == 20
    platform = json.loads((written / "Commerce.SemanticModel/.platform").read_text())
    assert len(platform["config"]["logicalId"]) == 36


def test_sql_null_semantics_is_refused():
    with pytest.raises(Unsupported, match="SQL null semantics"):
        project.project_files(commerce_model("sql"))


# ======================================================================
# 2. Real formats: schemas, TMDL text, Desktop-written samples
# ======================================================================
def schema_registry():
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    from referencing.jsonschema import DRAFT7

    root = FIXTURES / "json-schemas"
    base = "https://developer.microsoft.com/json-schemas/"
    resources = []
    for path in root.rglob("*.json"):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resource = referencing.Resource.from_contents(contents, default_specification=DRAFT7)
        resources.append((base + path.relative_to(root).as_posix(), resource))
    return jsonschema, referencing.Registry().with_resources(resources)


def validation_errors(doc, jsonschema, registry):
    schema = registry.contents(doc["$schema"])
    return list(jsonschema.Draft7Validator(schema, registry=registry).iter_errors(doc))


def test_every_written_json_file_validates_against_microsofts_schemas(written):
    jsonschema, registry = schema_registry()
    checked = 0
    for path in sorted(written.rglob("*")):
        if not path.is_file() or path.name == ".gitignore" or "StaticResources" in path.parts:
            continue
        if path.suffix == ".tmdl":
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        errors = validation_errors(doc, jsonschema, registry)
        assert not errors, f"{path.relative_to(written)}: {errors[0].message}"
        checked += 1
    # pbip, 2x .platform, pbism, pbir, version, report, pages, 2 pages, 5 visuals
    assert checked == 15


def test_the_schema_check_rejects_a_malformed_visual(written):
    jsonschema, registry = schema_registry()
    visual = next(written.rglob("visual.json"))
    doc = json.loads(visual.read_text())
    doc["visual"]["query"]["queryState"]["Values"] = {"projection": []}   # misspelt
    doc["position"].pop("width")
    assert len(validation_errors(doc, jsonschema, registry)) >= 2


def tmdl_text(written, name):
    return (written / "Commerce.SemanticModel/definition" / name).read_text(encoding="utf-8")


def test_table_tmdl_reads_like_desktop_wrote_it(written):
    orders = tmdl_text(written, "tables/Orders.tmdl")
    assert orders.startswith("table Orders\n\tlineageTag: ")
    assert ("\tmeasure 'Net Revenue' = CALCULATE ( [Gross Revenue], "
            "Orders[order_status] = \"completed\" )\n\t\tformatString: $#,0.00\n") in orders
    assert "\t/// Revenue from completed orders.\n\tmeasure 'Net Revenue'" in orders
    assert ("\tcolumn customer_id\n\t\tdataType: int64\n\t\tisHidden\n\t\tformatString: 0\n"
            in orders)
    assert "\t\tsourceColumn: order_amount\n\n\t\tannotation SummarizationSetBy = Automatic" \
        in orders
    assert "\tcolumn order_date\n\t\tdataType: dateTime\n" in orders
    assert "\t\tannotation UnderlyingDateTimeDataType = Date\n" in orders
    assert "\tpartition Orders = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet\n" in orders
    assert ('\t\t\t\t    Source = Databricks.Catalogs(DatabricksServer, DatabricksHttpPath, '
            '[Catalog=null, Database=null, EnableAutomaticProxyDiscovery=null]),') in orders
    assert '\t\t\t\t    fct_orders_Table = warehouse_Schema{[Name="fct_orders",Kind="Table"]}' \
           '[Data]\n\t\t\t\tin\n\t\t\t\t    fct_orders_Table\n' in orders
    assert "\tannotation PBI_ResultType = Table\n" in orders
    lines = tmdl_text(written, "tables/Order Lines.tmdl")
    assert lines.startswith("table 'Order Lines'\n")
    assert ("\tmeasure 'List Price Revenue' = SUMX ( 'Order Lines', 'Order Lines'[quantity] "
            "* RELATED ( Product[unit_price] ) )\n") in lines
    assert "\tpartition 'Order Lines' = m\n" in lines


def test_date_table_is_marked_the_way_tom_marks_it(written):
    date = tmdl_text(written, "tables/Date.tmdl")
    assert "table Date\n\tlineageTag: " in date
    assert "\tdataCategory: Time\n" in date
    assert "\tcolumn date_day\n\t\tdataType: dateTime\n\t\tisKey\n" in date
    assert date.count("isKey") == 1


def test_model_relationships_expressions_database_tmdl(written):
    model = tmdl_text(written, "model.tmdl")
    assert model.startswith("model Model\n\tculture: en-US\n")
    assert "\nref table Orders\nref table 'Order Lines'\nref table Customer\n" in model
    assert "annotation __PBI_TimeIntelligenceEnabled = 0" in model
    rels = tmdl_text(written, "relationships.tmdl")
    assert "\tfromColumn: 'Order Lines'.order_id\n\ttoColumn: Orders.order_id\n" in rels
    assert rels.count("relationship ") == 4
    assert "crossFilteringBehavior" not in rels and "isActive" not in rels   # defaults
    exprs = tmdl_text(written, "expressions.tmdl")
    assert exprs.startswith('expression DatabricksServer = "')
    assert 'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]' in exprs
    assert tmdl_text(written, "database.tmdl") == "database\n\tcompatibilityLevel: 1601\n\n"


def test_relationship_variants_round_trip():
    rels = (replace(MODEL.relationships[0], active=False),
            replace(MODEL.relationships[1], cross_filter="both"),
            replace(MODEL.relationships[2], cardinality="one_to_one"),
            MODEL.relationships[3])
    model = replace(MODEL, relationships=rels)
    files = tmdl.semantic_model_files(model)
    text = files["definition/relationships.tmdl"].decode()
    assert "\tisActive: false\n" in text
    assert "\tcrossFilteringBehavior: bothDirections\n" in text
    assert "\tfromCardinality: one\n" in text
    back, _ = tmdl.read_semantic_model(files, "Commerce")
    assert back.relationships == rels


def test_table_and_column_flags_round_trip():
    tables = list(MODEL.tables)
    tables[2] = replace(tables[2], hidden=True, description="Customers\nfrom the CRM.",
                        columns=(replace(tables[2].columns[0], description="Natural key"),
                                 *tables[2].columns[1:]))
    tables[1] = replace(tables[1], columns=(*tables[1].columns, Column(
        "line_value", "line_value", "decimal",
        expr=BinOp("*", Col("Order Lines", "quantity"), Related("Product", "unit_price")))))
    tables.append(Table("Order Facts", None, (Column("n", "n", "int"),),
                        source_sql='select count(*) as n from warehouse."fct_orders"'))
    model = replace(MODEL, tables=tuple(tables), description="The Commerce star schema.")
    files = tmdl.semantic_model_files(model)
    assert "\tcolumn line_value = 'Order Lines'[quantity] * RELATED ( Product[unit_price] )" \
        in files["definition/tables/Order Lines.tmdl"].decode()
    assert "Value.NativeQuery(Source{" not in files["definition/tables/Order Facts.tmdl"].decode()
    assert "Value.NativeQuery(Database, \"select count(*) as n from warehouse.\"\"fct_orders" \
        in files["definition/tables/Order Facts.tmdl"].decode()
    back, _ = tmdl.read_semantic_model(files, "Commerce")
    assert back == in_table_order(model)


@pytest.mark.parametrize("connector", sorted(mquery.CONNECTORS))
def test_every_connector_round_trips(connector):
    files = tmdl.semantic_model_files(MODEL, connector=connector)
    assert tmdl.read_semantic_model(files, "Commerce")[0] == in_table_order(MODEL)


def test_desktop_written_tmdl_parses():
    samples = FIXTURES / "samples"
    tables = 0
    for path in sorted(samples.rglob("*.tmdl")):
        nodes = tmdl.parse_document(path.read_text(encoding="utf-8-sig"), path.name)
        assert nodes, path
        tables += sum(n.keyword == "table" for n in nodes)
    assert tables == 6
    sales = tmdl.parse_document((samples / "pbip-demo-agentic/Model01.SemanticModel/"
                                 "definition/tables/Sales.tmdl").read_text())[0]
    margin = next(m for m in sales.objects("measure") if m.name == "Margin")
    assert margin.value.splitlines()[:3] == ["SUMX (", "    Sales,", "    Sales[Quantity]"]
    assert margin.prop("formatString") == "$ #,##0"
    ly = next(m for m in sales.objects("measure") if m.name == "Sales Amount (LY)")
    assert ly.description == "Sales Amount Last Year considering a full month"
    calendar = tmdl.parse_document((samples / "pbip-demo-agentic/Model01.SemanticModel/"
                                    "definition/tables/Calendar.tmdl").read_text())[0]
    weekend = next(c for c in calendar.objects("column") if c.name == "Is Weekend")
    assert weekend.prop("formatString") == '"TRUE";"TRUE";"FALSE"'


def test_desktop_sample_models_refuse_what_the_contract_cannot_carry(tmp_path):
    sample = FIXTURES / "samples/pbip-demo-agentic/Model01.SemanticModel"
    with pytest.raises(Unsupported, match=r"row-level security \(roles\)"):
        project.read_project(sample)
    copy = tmp_path / "Model01.SemanticModel"
    shutil.copytree(sample, copy)
    shutil.rmtree(copy / "definition/roles")
    with pytest.raises(Unsupported, match="Power Query step Table.TransformColumnTypes"):
        project.read_project(copy)


def test_a_direct_lake_model_reads_its_entity_partitions():
    fca = FIXTURES / "samples/fabric-toolbox-fca/definition"
    files = {f"definition/{p.relative_to(fca).as_posix()}": p.read_bytes()
             for p in fca.rglob("*.tmdl")}
    files["definition.pbism"] = b'{"version": "4.0", "settings": {}}'
    model, _ = tmdl.read_semantic_model(files, "FCA")
    calendar = next(t for t in model.tables if t.name == "calendar")
    assert calendar.source == "dbo.calendar"
    assert calendar.is_date_table and calendar.date_column == "Date"
    assert next(c for c in calendar.columns if c.name == "Date").data_type == "date"


def test_tmdl_parser_rules():
    text = ("/// The model\nmodel Model\n\tculture: en-US\n\n"
            "table 'It''s'\n\tisHidden\n\tdisplayFolder: \" padded \"\n\n"
            "\tmeasure 'a.b' = ```\n\t\t\tVAR x = 1\n\t\t\t  RETURN x   \n\t\t\t```\n"
            "\t\tformatString: 0\n\n"
            "\tmeasure m =\n\t\t\tSUM ( t[x] )\n\n\t\t\t+ 1\n\t\tisHidden\n")
    nodes = tmdl.parse_document(text)
    assert nodes[0].description == "The model" and nodes[0].prop("culture") == "en-US"
    table = nodes[1]
    assert table.name == "It's" and table.flag("isHidden")
    assert table.prop("displayFolder") == " padded "
    first, second = table.objects("measure")
    assert first.name == "a.b" and first.value == "VAR x = 1\n  RETURN x   "
    assert first.prop("formatString") == "0"
    assert second.value == "SUM ( t[x] )\n\n+ 1" and second.flag("isHidden")
    with pytest.raises(ModelError, match="unexpected indentation"):
        tmdl.parse_document("table t\n\t\t\tcolumn x\n")
    assert tmdl.split_reference("'Order Lines'.'order id'") == ["Order Lines", "order id"]


# ======================================================================
# 2b. Power Query
# ======================================================================
SHARED = dict(mquery.parameter_expressions("databricks"))


def test_partition_sources_lower_to_relations_and_native_queries():
    table = MODEL.tables[0]
    assert mquery.parse_partition(mquery.partition_source(table, "databricks"), SHARED) == (
        "warehouse.fct_orders", None)
    sql = 'select "x"\nfrom warehouse.fct_orders -- #(lf) stays text'
    native = Table("N", None, (), source_sql=sql)
    text = mquery.partition_source(native, "sql")
    assert "#(lf)" in text
    assert mquery.parse_partition(text, dict(mquery.parameter_expressions("sql"))) == (
        None, sql)
    snowflake = mquery.partition_source(table, "snowflake")
    assert '[Name="FCT_ORDERS",Kind="Table"]' in snowflake
    shared = {"Warehouse": 'let Source = Sql.Database("srv", "db") in Source'}
    assert mquery.parse_partition(
        'let t = Warehouse{[Schema="dbo",Item="orders"]}[Data] in t', shared) == (
        "dbo.orders", None)
    assert mquery.parse_partition(
        'Sql.Database("srv", "db", [Query="select 1 as one"])', {}) == (None, "select 1 as one")


@pytest.mark.parametrize("source, feature", [
    ('let\n    Source = Csv.Document(Web.Contents("https://x/y.csv"))\nin\n    Source',
     "Power Query step Csv.Document"),
    ('let\n    Source = Sql.Database("s", "d"),\n    t = Source{[Schema="w",Item="o"]}[Data],\n'
     '    #"Changed Type" = Table.TransformColumnTypes(t, {{"a", Int64.Type}})\n'
     'in\n    #"Changed Type"', "Power Query step Table.TransformColumnTypes"),
    ('let\n    Source = Sql.Database("s", "d"),\n    t = Source{[Schema="w",Item="o"]}[Data],\n'
     '    f = Table.SelectRows(t, each [x] > 1)\nin\n    f', "Power Query step Table.SelectRows"),
    ('let\n    Source = Sql.Database("s", "d"),\n    n = Date.Year(DateTime.LocalNow()) - 3\n'
     'in\n    n', "Power Query step Date.Year"),
    ('let Source = if true then 1 else 2 in Source', "Power Query if expression"),
    ('let Source = Odbc.DataSource("dsn=duck") in Source', "Power Query step Odbc.DataSource"),
])
def test_power_query_beyond_a_plain_source_is_refused(source, feature):
    with pytest.raises(Unsupported) as info:
        mquery.parse_partition(source, {})
    assert info.value.feature == feature and info.value.vendor == "Power BI"


# ======================================================================
# 3. DAX
# ======================================================================
@pytest.mark.parametrize("measure", MODEL.measures, ids=lambda m: m.name)
def test_every_example_measure_round_trips_through_dax(measure):
    text = dax.format_expression(measure.expr, MODEL)
    assert dax.parse_expression(text, MODEL) == measure.expr
    assert dax.format_expression(dax.parse_expression(text, MODEL), MODEL) == text


def test_example_measures_are_written_as_idiomatic_dax():
    text = {m.name: dax.format_expression(m.expr, MODEL) for m in MODEL.measures}
    assert text["Gross Revenue"] == "SUM ( Orders[order_amount] )"
    assert text["Order Count"] == "COUNTROWS ( Orders )"
    assert text["AOV"] == "DIVIDE ( [Net Revenue], [Completed Orders] )"
    assert text["Segment Share"] == ("DIVIDE ( [Net Revenue], CALCULATE ( [Net Revenue], "
                                     "REMOVEFILTERS ( Customer[segment] ) ) )")
    assert text["Revenue YTD"] == "TOTALYTD ( [Net Revenue], 'Date'[date_day] )"
    assert text["Revenue LY"] == ("CALCULATE ( [Net Revenue], SAMEPERIODLASTYEAR ( "
                                  "'Date'[date_day] ) )")
    assert text["YoY Growth"] == "DIVIDE ( [Net Revenue] - [Revenue LY], [Revenue LY] )"


def parse(text):
    return dax.parse_expression(text, MODEL)


NET = MeasureRef("Net Revenue")
STATUS = Col("Orders", "order_status")
SEGMENT = Col("Customer", "segment")
DAY = Col("Date", "date_day")


@pytest.mark.parametrize("text, expected", [
    # names are case-insensitive, quoting optional, comments ignored
    ("sum('orders'[ORDER_AMOUNT]) -- gross", Agg("sum", Col("Orders", "order_amount"))),
    ("/* x */ Average(Orders[order_amount])", Agg("avg", Col("Orders", "order_amount"))),
    ("DISTINCTCOUNTNOBLANK(Orders[customer_id])",
     Agg("count_distinct", Col("Orders", "customer_id"))),
    ("CALCULATE([Net Revenue], Customer[segment] IN {\"SMB\", \"Enterprise\"})",
     Calculate(NET, (FV(SEGMENT, ("SMB", "Enterprise")),))),
    ("CALCULATE([Net Revenue], NOT Customer[segment] IN {\"SMB\"})",
     Calculate(NET, (FV(SEGMENT, ("SMB",), negate=True),))),
    ("CALCULATE([Net Revenue], Customer[segment] = \"SMB\" || Customer[segment] = \"Consumer\")",
     Calculate(NET, (FV(SEGMENT, ("SMB", "Consumer")),))),
    ("CALCULATE([Net Revenue], Orders[order_amount] > 100)",
     Calculate(NET, (FC(Col("Orders", "order_amount"), ">", 100),))),
    ("CALCULATE([Net Revenue], 100 <= Orders[order_amount])",
     Calculate(NET, (FC(Col("Orders", "order_amount"), ">=", 100),))),
    ("CALCULATE([Net Revenue], Orders[order_status] <> \"cancelled\")",
     Calculate(NET, (FC(STATUS, "<>", "cancelled"),))),
    ("CALCULATE([Net Revenue], ISBLANK(Customer[region]))",
     Calculate(NET, (FV(Col("Customer", "region"), (None,)),))),
    ("CALCULATE([Net Revenue], TREATAS({2025, 2026}, 'Date'[year]))",
     Calculate(NET, (FV(Col("Date", "year"), (2025, 2026)),))),
    ("CALCULATE([Net Revenue], FILTER(ALL(Customer[segment]), Customer[segment] = \"SMB\"))",
     Calculate(NET, (FV(SEGMENT, ("SMB",)),))),
    ("CALCULATE([Net Revenue], FILTER(VALUES(Customer[segment]), Customer[segment] = \"SMB\"))",
     Calculate(NET, (KeepFilters(FV(SEGMENT, ("SMB",))),))),
    ("CALCULATE([Net Revenue], KEEPFILTERS(Customer[segment] = \"SMB\"))",
     Calculate(NET, (KeepFilters(FV(SEGMENT, ("SMB",))),))),
    ("CALCULATE([Net Revenue], ALLEXCEPT(Customer, Customer[region]))",
     Calculate(NET, (KeepOnly("Customer", (Col("Customer", "region"),)),))),
    ("CALCULATE([Net Revenue], ALL(Customer))",
     Calculate(NET, (RemoveFilters(tables=("Customer",)),))),
    ("CALCULATE([Net Revenue], REMOVEFILTERS())", Calculate(NET, (RemoveFilters(),))),
    # DAX applies ALL before the filters whatever the argument order ...
    ("CALCULATE([Net Revenue], Customer[segment] = \"SMB\", ALL(Customer))",
     Calculate(NET, (RemoveFilters(tables=("Customer",)), FV(SEGMENT, ("SMB",))))),
    # ... and ANDs two filters on one column: REPLACE, then INTERSECT
    ("CALCULATE([Net Revenue], Orders[order_amount] >= 10, Orders[order_amount] <= 50)",
     Calculate(NET, (FC(Col("Orders", "order_amount"), ">=", 10),
                     KeepFilters(FC(Col("Orders", "order_amount"), "<=", 50))))),
    # a filter on the marked date column brings DAX's implicit ALL ( 'Date' )
    ("CALCULATE([Net Revenue], 'Date'[date_day] >= DATE(2026, 1, 1))",
     Calculate(NET, (RemoveFilters(tables=("Date",)), FC(DAY, ">=", dt.date(2026, 1, 1))))),
    ("TOTALQTD([Net Revenue], 'Date'[date_day])", TimeShift(NET, DAY, "qtd")),
    ("CALCULATE([Net Revenue], DATESMTD('Date'[date_day]))", TimeShift(NET, DAY, "mtd")),
    ("CALCULATE([Net Revenue], PREVIOUSMONTH('Date'[date_day]))",
     TimeShift(NET, DAY, "previous_month")),
    ("CALCULATE([Net Revenue], PREVIOUSYEAR('Date'[date_day]), Customer[segment] = \"SMB\")",
     TimeShift(Calculate(NET, (FV(SEGMENT, ("SMB",)),)), DAY, "previous_year")),
    ("TOTALYTD([Net Revenue], 'Date'[date_day], Customer[segment] = \"SMB\")",
     TimeShift(Calculate(NET, (FV(SEGMENT, ("SMB",)),)), DAY, "ytd")),
    ("VAR rev = [Net Revenue] VAR target = 1000 RETURN IF(rev > target, rev - target)",
     If(BinOp(">", NET, Lit(1000)), BinOp("-", NET, Lit(1000)))),
    ("VAR floor = 100 RETURN CALCULATE([Net Revenue], Orders[order_amount] >= floor)",
     Calculate(NET, (FC(Col("Orders", "order_amount"), ">=", 100),))),
    ("VAR k = 2 RETURN CALCULATE([Net Revenue] * k, ALL(Customer))",
     Calculate(BinOp("*", NET, Lit(2)), (RemoveFilters(tables=("Customer",)),))),
    ("SWITCH(TRUE(), [Net Revenue] > 100, \"high\", [Net Revenue] > 10, \"mid\", \"low\")",
     If(BinOp(">", NET, Lit(100)), Lit("high"),
        If(BinOp(">", NET, Lit(10)), Lit("mid"), Lit("low")))),
    ("SWITCH([Order Count], 1, \"one\", 2, \"two\")",
     If(BinOp("=", MeasureRef("Order Count"), Lit(1)), Lit("one"),
        If(BinOp("=", MeasureRef("Order Count"), Lit(2)), Lit("two"), None))),
    ("SUMX('Order Lines', [quantity] * RELATED(Product[unit_price]))",
     IterAgg("sum", "Order Lines", BinOp("*", Col("Order Lines", "quantity"),
                                         Related("Product", "unit_price")))),
    ("ROUND([AOV], 2) + -1.5", BinOp("+", Func("round", (MeasureRef("AOV"), Lit(2))),
                                     Lit(Decimal("-1.5")))),
    ("FLOOR([AOV], 1) & \"\"", BinOp("&", Func("floor", (MeasureRef("AOV"),)), Lit(""))),
    ("BLANK()", Lit(None)),
])
def test_dax_lowering(text, expected):
    assert parse(text) == expected
    again = dax.format_expression(expected, MODEL)
    assert parse(again) == expected, again


@pytest.mark.parametrize("expr", [
    Func("date_trunc_month", (Agg("max", DAY),)),
    Func("date_trunc_quarter", (Agg("max", DAY),)),
    Func("date_trunc_year", (Agg("min", DAY),)),
    Func("date_trunc_week", (Agg("min", DAY),)),
    Func("weekday", (Agg("min", DAY),)),
    Func("ceiling", (MeasureRef("AOV"),)),
    Func("left", (Agg("max", SEGMENT), Lit(3))),
    BinOp("or", BinOp("and", Lit(True), Lit(False)), Lit(True)),
    BinOp("-", Lit(1), BinOp("-", Lit(2), Lit(3))),
    Lit(dt.date(2026, 2, 28)),
    Calculate(NET, (FV(SEGMENT, ("SMB",)), RemoveFilters(tables=("Customer",)))),
])
def test_trees_round_trip_through_dax(expr):
    text = dax.format_expression(expr, MODEL)
    back = parse(text)
    if isinstance(expr, Calculate) and len(expr.modifiers) == 2:
        # Filter-then-remove is two CALCULATEs in DAX: same meaning, nested tree.
        assert text.count("CALCULATE") == 2
        assert back == Calculate(Calculate(NET, (RemoveFilters(tables=("Customer",)),)),
                                 (FV(SEGMENT, ("SMB",)),))
    else:
        assert back == expr, text


def test_calculated_columns_parse_in_a_row_context():
    expr = dax.parse_expression("[quantity] * RELATED(Product[unit_price])", MODEL,
                                table="Order Lines")
    assert expr == BinOp("*", Col("Order Lines", "quantity"), Related("Product", "unit_price"))


@pytest.mark.parametrize("text, feature", [
    ("CALCULATE([Net Revenue], USERELATIONSHIP(Orders[order_date], 'Date'[date_day]))",
     "USERELATIONSHIP"),
    ("CALCULATE([Net Revenue], CROSSFILTER(Orders[customer_id], Customer[customer_id], BOTH))",
     "CROSSFILTER"),
    ("SUMX(Orders, EARLIER(Orders[order_amount]))", "EARLIER"),
    ("COUNTROWS(FILTER(Orders, Orders[order_amount] > 10))", "DAX function FILTER"),
    ("SUMX(VALUES(Customer[segment]), [Net Revenue])", "DAX function VALUES"),
    ("SUMX(Orders, [Gross Revenue])", "context transition"),
    ("SUMX(Orders, CALCULATE(SUM(Orders[order_amount])))", "context transition"),
    ("SUMX(Orders, SUMX('Order Lines', 'Order Lines'[quantity]))", "nested iterator"),
    ("VAR t = FILTER(Orders, Orders[order_amount] > 1) RETURN COUNTROWS(t)",
     "VAR over a table expression"),
    ("VAR rev = [Net Revenue] RETURN CALCULATE(rev, ALL(Customer))",
     "variable evaluated in a different filter context"),
    ("IF([Net Revenue] == 0, 1)", "== (strict equality)"),
    ("[AOV] ^ 2", "^ (exponentiation)"),
    ("DISTINCTCOUNT(Orders[customer_id])", "DISTINCTCOUNT"),
    ("WEEKDAY(MAX('Date'[date_day]), 2)", "WEEKDAY return_type other than 1"),
    ("CALCULATE([Net Revenue], Customer[region] = BLANK())", "= BLANK() in a filter"),
    ("TOTALYTD([Net Revenue], Orders[order_date])",
     "time intelligence over a column that is not the date column of a marked date table"),
    ("CALCULATE([Net Revenue], SAMEPERIODLASTYEAR('Date'[date_day]), 'Date'[year] = 2026)",
     "time-intelligence filter combined with another filter on the date table"),
    ("CALCULATE([Net Revenue], DATEADD('Date'[date_day], -1, YEAR))", "DAX function DATEADD"),
    ("RANKX(ALL(Customer), [Net Revenue])", "DAX function RANKX"),
    ("IF(Customer[segment] IN {\"SMB\"}, 1)", "IN outside a CALCULATE filter"),
    ("CALCULATE([Net Revenue], Orders)", "table filter argument"),
    ("CALCULATE([Net Revenue], Orders[order_amount] > [AOV])",
     "filter comparing a column to a measure or expression"),
    ("CALCULATE([Net Revenue], Customer[segment] = \"SMB\" || Customer[region] = \"NA\")",
     "boolean filter combining conditions with ||"),
    ("TOTALYTD([Net Revenue], 'Date'[date_day], \"6-30\")", "TOTALYTD year_end_date"),
])
def test_dax_outside_the_subset_is_refused_by_name(text, feature):
    with pytest.raises(Unsupported) as info:
        parse(text)
    assert info.value.feature == feature
    assert info.value.vendor == "Power BI"


@pytest.mark.parametrize("text, message", [
    ("SUM(Orders[order_amount]", r"line 1, column 25: The syntax for 'end of input'"),
    ("CALCULATE([Net Revenue],\n  , 1)", r"line 2, column 3: The syntax for ','"),
    ("SUM(Orders[nope])", "Column 'nope' in table 'Orders' cannot be found"),
    ("[Missing Measure]", "The value for 'Missing Measure' cannot be determined"),
    ("Orders[order_amount] + 1", "A single value for column 'order_amount' in table 'Orders'"),
    ("RELATED(Product[unit_price])", "doesn't have a relationship"),
    ("DIVIDE([AOV])", "Too few arguments were passed to the DIVIDE function"),
    ("SUM(Nowhere[x])", "Failed to resolve name 'Nowhere'"),
])
def test_malformed_dax_is_a_model_error_with_its_position(text, message):
    with pytest.raises(ModelError, match=message):
        parse(text)


def test_formatter_refuses_what_dax_cannot_say():
    with pytest.raises(Unsupported, match="date's date column|date column"):
        dax.format_expression(Calculate(NET, (FV(DAY, (dt.date(2026, 1, 1),)),)), MODEL)
    with pytest.raises(Unsupported, match="time intelligence"):
        dax.format_expression(TimeShift(NET, Col("Orders", "order_date"), "ytd"), MODEL)


# ======================================================================
# 2c. PBIR details
# ======================================================================
def visuals(written):
    out = {}
    for path in written.rglob("visual.json"):
        doc = json.loads(path.read_text())
        title = doc["visual"]["visualContainerObjects"]["title"][0]["properties"]["text"]
        out[title["expr"]["Literal"]["Value"].strip("'")] = doc
    return out


def test_visual_json_carries_type_projections_sort_and_filters(written):
    by_name = visuals(written)
    kpis = by_name["KPIs"]["visual"]
    assert kpis["visualType"] == "cardVisual"
    assert [p["queryRef"] for p in kpis["query"]["queryState"]["Data"]["projections"]] == [
        "Orders.Net Revenue", "Orders.Completed Orders", "Orders.AOV"]
    month = by_name["Revenue by Month"]["visual"]
    assert month["visualType"] == "lineChart"
    category = month["query"]["queryState"]["Category"]["projections"]
    assert [(p["field"]["Column"]["Property"], p["active"]) for p in category] == [
        ("year", True), ("month", True)]
    assert month["query"]["sortDefinition"]["isDefaultSort"] is True
    seg = by_name["Revenue by Segment"]["visual"]
    assert seg["visualType"] == "barChart"
    assert seg["query"]["sortDefinition"]["sort"] == [{"field": {"Measure": {
        "Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "Net Revenue"}},
        "direction": "Descending"}]
    top = by_name["Top Products"]
    assert top["visual"]["visualType"] == "tableEx"
    topn = top["filterConfig"]["filters"][0]
    assert topn["type"] == "TopN"
    assert topn["filter"]["From"][0]["Expression"]["Subquery"]["Query"]["Top"] == 3
    region = by_name["Revenue by Region 2026"]["filterConfig"]["filters"][0]
    assert region["type"] == "Categorical"
    assert region["filter"]["Where"][0]["Condition"]["In"]["Values"] == [
        [{"Literal": {"Value": "2026L"}}]]
    pbir_doc = json.loads((written / "Executive Overview.Report/definition.pbir").read_text())
    assert pbir_doc["datasetReference"] == {"byPath": {"path": "../Commerce.SemanticModel"}}


def test_a_single_value_card_is_the_classic_card():
    report = Report("R", "Commerce", (Page("P", (
        Visual("Revenue", values=(("Revenue", NET),), kind="card"),
        Visual("By status", group_by=(STATUS,), values=(("Sum of order_amount", Agg(
            "sum", Col("Orders", "order_amount"))), ("Orders", MeasureRef("Order Count"))),
               filters=(FC(Col("Orders", "order_amount"), "<>", Decimal("0.00")),
                        FV(SEGMENT, ("SMB",), negate=True)), kind="matrix"),
    )),))
    files = pbir.report_files(report, MODEL, "Commerce.SemanticModel")
    docs = [json.loads(v) for k, v in files.items() if k.endswith("visual.json")]
    assert {d["visual"]["visualType"] for d in docs} == {"card", "pivotTable"}
    info = tmdl.SemanticModelInfo()
    assert pbir.read_report(files, MODEL, info, "R") == report


@pytest.mark.parametrize("value, data_type", [
    (2026, "int"), (Decimal("12.50"), "decimal"), (1.5, "float"), ("it's", "string"),
    (True, "bool"), (dt.date(2026, 1, 31), "date"), (dt.datetime(2026, 1, 31, 8, 30), "timestamp"),
    (None, "string"),
])
def test_filter_literals_round_trip(value, data_type):
    assert pbir.parse_literal(pbir.literal(value), data_type) == value


def edited(files, path_suffix, edit):
    out = dict(files)
    for path in files:
        if path.endswith(path_suffix):
            doc = json.loads(files[path])
            edit(doc)
            out[path] = json.dumps(doc).encode()
    return out


def report_files():
    return pbir.report_files(REPORT, MODEL, "Commerce.SemanticModel")


def first_visual(files):
    return next(p for p in sorted(files) if p.endswith("visual.json"))


def test_page_filters_are_pushed_into_each_visual():
    files = report_files()
    page = next(p for p in sorted(files) if p.endswith("page.json"))
    flt = pbir.filter_json(FV(Col("Customer", "region"), ("NA",)), "f0")
    files = edited(files, page, lambda d: d.setdefault("filterConfig", {"filters": [flt]}))
    report = pbir.read_report(files, MODEL, tmdl.SemanticModelInfo(), "R")
    pid = page.split("/")[2]
    overview = next(p for p in report.pages if any(
        f"pages/{pid}/" in k for k in files if p.name == json.loads(files[page])["displayName"]))
    assert all(v.filters[:1] == (FV(Col("Customer", "region"), ("NA",)),)
               for v in overview.visuals)


@pytest.mark.parametrize("edit, feature", [
    (lambda d: d["visual"].update(visualType="slicer", objects={"general": [{"properties": {
        "filter": {"filter": {}}}}]}), "slicer selection"),
    (lambda d: d.setdefault("filterConfig", {"filters": []})["filters"].append({
        "name": "m", "type": "Advanced", "field": {"Measure": {
            "Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "AOV"}},
        "filter": {"Version": 2, "From": [], "Where": []}}), "filter on a measure value"),
    (lambda d: d["visual"]["query"]["queryState"].setdefault("Tooltips", {"projections": []})
     and d["visual"]["query"]["queryState"]["Data"]["projections"].append({
         "field": {"Aggregation": {"Expression": {"Column": {
             "Expression": {"SourceRef": {"Entity": "Orders"}}, "Property": "customer_id"}},
             "Function": 2}}, "queryRef": "x"}), "Count (Distinct) implicit aggregation"),
    (lambda d: d.setdefault("filterConfig", {"filters": []})["filters"].append({
        "name": "r", "type": "RelativeDate", "field": {}, "filter": {"Version": 2}}),
     "RelativeDate filter"),
])
def test_report_constructs_outside_the_contract_are_refused(edit, feature):
    files = report_files()
    kpis = next(p for p in sorted(files) if p.endswith("visual.json") and b"cardVisual"
                in files[p])
    files = edited(files, kpis, edit)
    with pytest.raises(Unsupported) as info:
        pbir.read_report(files, MODEL, tmdl.SemanticModelInfo(), "R")
    assert info.value.feature == feature


def test_a_visual_sorted_by_a_sort_by_column_is_refused():
    report = Report("R", "Commerce", (Page("P", (
        Visual("By month name", group_by=(Col("Date", "month_name"),),
               values=(("Net Revenue", NET),)),)),))
    files = pbir.report_files(report, MODEL, "Commerce.SemanticModel")
    info = tmdl.SemanticModelInfo(sort_by={("Date", "month_name"): "month"})
    with pytest.raises(Unsupported, match="sortByColumn"):
        pbir.read_report(files, MODEL, info, "R")


def test_two_filters_on_one_field_are_refused_when_written():
    visual = Visual("v", values=(("n", NET),),
                    filters=(FV(SEGMENT, ("SMB",)), FC(SEGMENT, "<>", "x")))
    with pytest.raises(Unsupported, match="two filters on one field"):
        pbir.report_files(Report("R", "Commerce", (Page("P", (visual,)),)), MODEL, "M")


# ======================================================================
# 4. DAX queries
# ======================================================================
def query(text):
    return daxquery.parse_query(text, MODEL)


def test_summarizecolumns_is_the_visual_it_describes():
    q = query("""
        DEFINE
            MEASURE Orders[Refunds] =
                CALCULATE ( [Gross Revenue], Orders[order_status] = "refunded" )
        EVALUATE
            SUMMARIZECOLUMNS (
                'Customer'[region],
                KEEPFILTERS ( TREATAS ( { 2026 }, 'Date'[year] ) ),
                "Net Revenue", [Net Revenue],
                "Refunds", [Refunds]
            )
        ORDER BY [Net Revenue] DESC, 'Customer'[region]
    """)
    assert q.visual == Visual("Query", group_by=(Col("Customer", "region"),),
                              values=(("Net Revenue", NET), ("Refunds", MeasureRef("Refunds"))),
                              filters=(FV(Col("Date", "year"), (2026,)),),
                              order_by=(("Net Revenue", True), ("region", False)), kind="table")
    assert q.columns == ("Customer[region]", "[Net Revenue]", "[Refunds]")
    assert q.measures[0].table == "Orders"
    assert q.model.measures[-1].name == "Refunds"


def test_row_topn_and_measure_override():
    assert query('EVALUATE ROW("Net", [Net Revenue], "AOV", [AOV])').visual == Visual(
        "Query", values=(("Net", NET), ("AOV", MeasureRef("AOV"))), kind="table")
    top = query('EVALUATE TOPN ( 3, SUMMARIZECOLUMNS ( Product[product_name], "Units", '
                '[Units] ), [Units], DESC )').visual
    assert (top.top_n, top.order_by) == (3, (("Units", True),))
    asc = query('EVALUATE TOPN(2, SUMMARIZECOLUMNS(Product[product_name], "Units", [Units]), '
                '[Units], ASC) ORDER BY [Units]').visual
    assert (asc.top_n, asc.order_by) == (2, (("Units", False),))
    override = query('DEFINE MEASURE Orders[Net Revenue] = SUM ( Orders[order_amount] ) '
                     'EVALUATE ROW ( "n", [AOV] )')
    assert [m for m in override.model.measures if m.name == "Net Revenue"][0].expr == Agg(
        "sum", Col("Orders", "order_amount"))
    assert len(override.model.measures) == len(MODEL.measures)


@pytest.mark.parametrize("text, feature", [
    ("EVALUATE Orders", "EVALUATE of a table reference"),
    ("EVALUATE TOPN(5, Orders)", "EVALUATE of a table reference"),
    ("EVALUATE FILTER(Orders, Orders[order_amount] > 1)", "DAX function FILTER"),
    ("EVALUATE ADDCOLUMNS(VALUES(Customer[segment]), \"n\", [Net Revenue])",
     "DAX function ADDCOLUMNS"),
    ("DEFINE VAR x = 1 EVALUATE ROW(\"x\", x)", "DEFINE VAR"),
    ("EVALUATE ROW(\"a\", 1) EVALUATE ROW(\"b\", 2)", "more than one EVALUATE"),
    ("EVALUATE SUMMARIZECOLUMNS(Customer[segment], TREATAS({\"NA\"}, Customer[region]), "
     "FILTER(ALL(Customer[region]), Customer[region] <> \"EMEA\"), \"n\", [Net Revenue])",
     "two filters on one column in SUMMARIZECOLUMNS"),
    ("EVALUATE TOPN(2, SUMMARIZECOLUMNS(Product[product_name], \"Units\", [Units]), [Units]) "
     "ORDER BY [Units] ASC", "TOPN ranked differently from ORDER BY"),
    ("EVALUATE ROW(\"a\", [Net Revenue]) ORDER BY [Net Revenue] START AT 1", "START AT"),
])
def test_dax_queries_outside_the_subset_are_refused(text, feature):
    with pytest.raises(Unsupported) as info:
        query(text)
    assert info.value.feature == feature


def test_results_come_back_in_the_execute_queries_shape():
    q = query('EVALUATE SUMMARIZECOLUMNS(\'Date\'[date_day], Customer[segment], '
              '"Net Revenue", [Net Revenue], "Orders", [Order Count], "AOV", [AOV])')
    rendered = {"columns": ["date_day", "segment", "Net Revenue", "Orders", "AOV"],
                "rows": [["2026-01-01", "SMB", "1200.00", 3, "400.0"],
                         ["2026-01-02", "Consumer", None, None, None]]}
    assert daxquery.execute_queries_result(q, rendered) == {"results": [{"tables": [{"rows": [
        {"Date[date_day]": "2026-01-01T00:00:00", "Customer[segment]": "SMB",
         "[Net Revenue]": 1200, "[Orders]": 3, "[AOV]": 400.0},
        {"Date[date_day]": "2026-01-02T00:00:00", "Customer[segment]": "Consumer"},
    ]}]}]}
    nulls = daxquery.execute_queries_result(q, rendered, include_nulls=True)
    assert nulls["results"][0]["tables"][0]["rows"][1]["[AOV]"] is None


# ======================================================================
# 5. Engine-dependent (skipped until world.semantic.render lands)
# ======================================================================
def test_the_read_project_validates_and_renders_with_the_engine(written, tmp_path):
    render = pytest.importorskip("world.semantic.render",
                                 reason="engine not yet available (world.semantic.render)")
    from world.semantic.examples import write_fixture

    model, reports = powerbi.read_project(written)
    render.validate(model, reports)
    warehouse = write_fixture(tmp_path / "commerce.duckdb")
    assert render.render_report(model, reports[0], warehouse) == render.render_report(
        in_table_order(MODEL), REPORT, warehouse)


def test_a_dax_query_renders_like_the_visual_it_is(tmp_path):
    render = pytest.importorskip("world.semantic.render",
                                 reason="engine not yet available (world.semantic.render)")
    from world.semantic.examples import write_fixture

    warehouse = write_fixture(tmp_path / "commerce.duckdb")
    q = query('EVALUATE SUMMARIZECOLUMNS(Customer[segment], "Net Revenue", [Net Revenue], '
              '"Segment Share", [Segment Share]) ORDER BY [Net Revenue] DESC')
    visual = REPORT.pages[0].visuals[2]
    assert render.render_visual(q.model, q.visual, warehouse)["rows"] == \
        render.render_visual(MODEL, visual, warehouse)["rows"]
