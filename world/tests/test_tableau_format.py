"""The Tableau front-end: .tds/.twb XML and calculated fields, against the contract.

What is claimed, and how each claim is checked:

1. ROUND TRIP. `read_project(write_project(m, r))` gives back the same model and
   reports, and writing what was read reproduces the same bytes - for the
   Commerce example as a Tableau author builds it (formats/tableau/examples.py).
2. LOUD, NEVER SILENT. `examples.commerce_model()` is written in Power BI terms;
   every measure Tableau cannot spell exactly is refused BY NAME, and every one
   it can spell round-trips through calc text. The two renditions render the
   same numbers on the executive report (engine permitting).
3. REAL FORMAT. Output is checked against the shapes of real Desktop saves
   (attribute order, quoting, the build comment, object-graph, relationships'
   unique-key) and, when `xmlschema` is importable, validated against Tableau's
   official 2026.2 XSD (fixtures/tableau/twb_2026.2.0.xsd, Apache-2.0).
   Three real files from MIT repositories (fixtures/tableau/LICENSES.txt) are
   read: one parses, one refuses by name, one parses at the data-source level
   with every worksheet either lowering or refused by name.
4. FAN-OUT. A logical table built from a physical JOIN keeps the join, so SUM of
   the order header amount over order lines double counts exactly as Tableau
   does, and the engine's number equals the warehouse's own SQL over that join.

Run: cd gateway && uv run pytest ../world/tests/test_tableau_format.py -q
XSD validation: cd gateway && uv run --with xmlschema pytest \
    ../world/tests/test_tableau_format.py -q
"""
from __future__ import annotations

import io
import re
import sys
import zipfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

WORLD_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORLD_DIR))

from world.semantic import examples  # noqa: E402
from world.semantic.errors import ModelError, Unsupported  # noqa: E402
from world.semantic.expr import (  # noqa: E402
    Agg,
    BinOp,
    Calculate,
    Coalesce,
    Col,
    Divide,
    FilterCompare,
    FilterValues,
    If,
    IterAgg,
    Lit,
    MeasureRef,
    Not,
    Related,
    RemoveFilters,
)
from world.semantic.formats import tableau  # noqa: E402
from world.semantic.formats.tableau import calc, tds, twb, xmlio  # noqa: E402
from world.semantic.formats.tableau.examples import (  # noqa: E402
    tableau_commerce_model,
    tableau_executive_report,
)
from world.semantic.model import Measure, Page, Report, Visual  # noqa: E402

FIXTURES = WORLD_DIR / "tests" / "fixtures" / "tableau"

try:
    from world.semantic import render
except ImportError:                                     # pragma: no cover
    render = None
needs_engine = pytest.mark.skipif(render is None, reason="world.semantic.render not built yet")


@pytest.fixture(scope="module")
def tab():
    return tableau_commerce_model(), tableau_executive_report()


@pytest.fixture
def project(tab, tmp_path):
    m, r = tab
    tableau.write_project(m, (r,), tmp_path / "bi" / "tableau")
    return tmp_path / "bi" / "tableau"


# ============================================================ 1. round trip
def test_round_trip_model_and_report(tab, project):
    m, r = tab
    model, reports = tableau.read_project(project)
    assert model == m
    assert reports == (r,)


def test_write_is_deterministic_and_stable_under_read(tab, tmp_path):
    m, r = tab
    a = tableau.write_project(m, (r,), tmp_path / "a")
    b = tableau.write_project(*tableau.read_project(tmp_path / "a"), tmp_path / "b")
    assert [p.relative_to(tmp_path / "a") for p in a] == \
        [p.relative_to(tmp_path / "b") for p in b] == \
        [Path("datasources/Commerce.tds"), Path("workbooks/Executive Overview.twb")]
    for pa, pb in zip(a, b, strict=True):
        assert pa.read_bytes() == pb.read_bytes(), pa.name


def test_every_warehouse_class_round_trips(tab, tmp_path):
    m, r = tab
    for warehouse, conn in tds.CONNECTIONS.items():
        tableau.write_project(m, (r,), tmp_path / warehouse, warehouse=warehouse)
        text = (tmp_path / warehouse / "datasources" / "Commerce.tds").read_text("utf-8")
        assert f"class='{conn.cls}'" in text
        assert tableau.read_project(tmp_path / warehouse) == (m, (r,))


def test_single_files_read_alone(tab, project):
    m, r = tab
    assert tableau.read_project(project / "datasources" / "Commerce.tds") == (m, ())
    # A lone workbook finds its published data source in ../datasources.
    assert tableau.read_project(project / "workbooks" / "Executive Overview.twb") == (m, (r,))


def test_workbook_without_its_published_source_is_refused(project, tmp_path):
    lone = tmp_path / "lone.twb"
    lone.write_bytes((project / "workbooks" / "Executive Overview.twb").read_bytes())
    with pytest.raises(ModelError, match="published data source"):
        tableau.read_project(lone)


def test_packaged_files_read_like_plain_ones(tab, project, tmp_path):
    m, r = tab
    folder = tmp_path / "packaged"
    (folder / "datasources").mkdir(parents=True)
    (folder / "workbooks").mkdir()
    for src, dst, inner in (
            (project / "datasources" / "Commerce.tds",
             folder / "datasources" / "Commerce.tdsx", "Commerce.tds"),
            (project / "workbooks" / "Executive Overview.twb",
             folder / "workbooks" / "Executive Overview.twbx", "Executive Overview.twb")):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(inner, src.read_bytes())
            z.writestr("Data/Extracts/x.hyper", b"not read")
        dst.write_bytes(buf.getvalue())
    assert tableau.read_project(folder) == (m, (r,))


def test_folder_with_two_data_sources_is_refused(tab, project):
    m, _ = tab
    other = replace(m, name="Other", measures=m.measures[:1])
    (project / "datasources" / "Other.tds").write_bytes(
        xmlio.serialize(tds.write(other, tds.CONNECTIONS["duckdb"], root=True)))
    with pytest.raises(ModelError, match="different data sources"):
        tableau.read_project(project)


def test_extract_flag_is_written_and_read(tab, tmp_path):
    m, _ = tab
    tableau.write_project(m, (), tmp_path, extract=True)
    text = (tmp_path / "datasources" / "Commerce.tds").read_bytes()
    assert b"<extract count='-1' enabled='true'" in text
    assert b"<properties context='extract'>" in text
    ds = tableau.project.read_datasource_bytes(text, "Commerce.tds")
    assert ds.extract and ds.model == m


# ======================================================= 2. loud, not silent
REFUSED = {
    "Net Revenue": "filter override (CALCULATE with a filter) in a measure",
    "Completed Orders": "filter override (CALCULATE with a filter) in a measure",
    "Order Count": "row count of a logical table in a calculation",
    "Segment Share": "ALL / REMOVEFILTERS in a measure",
    "Revenue YTD": "time intelligence (ytd)",
    "Revenue LY": "time intelligence (same_period_last_year)",
}


def test_power_bi_commerce_measures_refused_by_name_or_round_tripped():
    m = examples.commerce_model()
    for measure in m.measures:
        if measure.name in REFUSED:
            with pytest.raises(Unsupported) as exc:
                calc.format_expression(measure.expr, m)
            assert exc.value.feature == REFUSED[measure.name], measure.name
            assert exc.value.vendor == "Tableau"
        else:
            text = calc.format_expression(measure.expr, m)
            assert calc.parse_expression(text, m) == measure.expr, (measure.name, text)


def test_power_bi_commerce_model_is_refused_whole(tmp_path):
    with pytest.raises(Unsupported) as exc:
        tableau.write_project(examples.commerce_model(), (examples.executive_report(),),
                              tmp_path)
    assert exc.value.feature == "date table (Mark as Date Table)"


@needs_engine
def test_both_renditions_show_the_same_numbers(tab, tmp_path):
    """Same warehouse, two tools' definitions: on the executive report's views
    the numbers agree, because none of them filters or groups by order_status."""
    wh = examples.write_fixture(tmp_path / "fixture.duckdb")
    m, r = tab
    model, (report,) = tableau.read_project(
        tableau.write_project(m, (r,), tmp_path / "p")[0].parent.parent)
    ours = render.render_report(model, report, wh)
    theirs = render.render_report(examples.commerce_model(), examples.executive_report(), wh)
    assert ours["Overview"]["KPIs"] == theirs["Overview"]["KPIs"]
    assert ours["Overview"]["Revenue by Segment"] == theirs["Overview"]["Revenue by Segment"]
    assert ours["Overview"]["Top Products"] == theirs["Overview"]["Top Products"]
    assert ours["Regions"] == theirs["Regions"]


# =========================================================== 3. real format
def _text(project, rel):
    return (project / rel).read_text("utf-8")


def test_house_style_matches_desktop(project):
    for rel in ("datasources/Commerce.tds", "workbooks/Executive Overview.twb"):
        lines = _text(project, rel).split("\n")
        assert lines[0] == "<?xml version='1.0' encoding='utf-8' ?>"
        assert lines[1] == ""
        # Desktop pads the build comment to a fixed 63 characters.
        assert lines[2] == f"<!-- build {xmlio.BUILD}" + " " * 31 + "-->"
        assert len(lines[2]) == 63
        assert '"' not in lines[3]           # single-quoted attributes throughout
        for line in lines[3:]:
            attrs = re.findall(r" ([A-Za-z_:.-]+)='", line.split(">")[0])
            assert attrs == sorted(attrs), line
    wb = _text(project, "workbooks/Executive Overview.twb")
    assert f"source-build='{xmlio.SOURCE_BUILD}'" in wb
    assert "version='18.1'" in wb and "original-version='18.1'" in wb


def test_datasource_shape(project):
    text = _text(project, "datasources/Commerce.tds")
    # 2024.3+ writes the object model without `_.fcp.` prefixes.
    assert "<object-graph>" in text and "_.fcp.ObjectModel" not in text
    assert "<relation type='collection'>" in text
    assert re.search(r"<object caption='Orders' id='fct_orders \(warehouse\.fct_orders\)_"
                     r"[0-9A-F]{32}'>", text)
    # Duplicate field names carry the relation, as Desktop names them.
    assert "<local-name>[customer_id (dim_customer)]</local-name>" in text
    assert "<map key='[customer_id (dim_customer)]' value='[dim_customer].[customer_id]' />" \
        in text
    # The one side of a relationship is cardinality One.
    assert re.search(r"<second-end-point object-id='dim_customer[^']*' unique-key='true' />",
                     text)
    assert "formula='SUM(IF [order_status] = &apos;completed&apos; THEN [order_amount] END)'" \
        in text
    assert re.search(r"name='\[Calculation_\d{19}\]'", text)
    assert "datatype='table' name='[__tableau_internal_object_id__].[fct_orders" in text


def test_workbook_shape(project):
    text = _text(project, "workbooks/Executive Overview.twb")
    assert "class='sqlproxy' dbname='Commerce' directory='/dataserver' port='8096' " \
           "server='tableau.internal'" in text
    assert "<repository-location derived-from='/datasources/Commerce?rev=1.0' id='Commerce' " \
           "path='/datasources' revision='1.0' />" in text
    assert re.search(r"<computed-sort column='\[sqlproxy\.\w+\]\.\[none:segment:nk\]' "
                     r"direction='DESC' using='\[sqlproxy\.\w+\]\.\[usr:Calculation_\d+:qk\]' />",
                     text)
    assert re.search(r"<groupfilter count='3' end='top' function='end' units='records' "
                     r"user:ui-marker='end' user:ui-top-by-field='true'>", text)
    assert re.search(r"<groupfilter function='member' level='\[none:year:qk\]' member='2026' ",
                     text)
    assert "[:Measure Names]" in text and "[Multiple Values]" in text
    assert "<dashboard enable-sort-zone-taborder='true' name='Overview'>" in text


try:
    import xmlschema
except ImportError:                                     # pragma: no cover
    xmlschema = None


@pytest.mark.skipif(xmlschema is None, reason="xmlschema not installed; run with "
                    "`uv run --with xmlschema pytest ...`")
def test_workbook_and_datasource_validate_against_tableau_xsd(tab, project):
    import xml.etree.ElementTree as ET

    schema = xmlschema.XMLSchema(
        str(FIXTURES / "twb_2026.2.0.xsd"),
        locations={"http://www.tableausoftware.com/xml/user": str(FIXTURES / "user_stub.xsd"),
                   "http://www.w3.org/XML/1998/namespace": "http://www.w3.org/2001/xml.xsd"})

    def errors(text):
        # `explain-data` became required in the 2026 schema; 2025 saves (ours, and
        # the real 2025.2 fixture) do not have it.
        return [f"{e.path}: {e.reason}" for e in schema.iter_errors(text)
                if "explain-data" not in str(e.reason)]

    assert errors(_text(project, "workbooks/Executive Overview.twb")) == []
    m, _ = tab
    for extract in (False, True):
        wb = ET.Element("workbook", {"original-version": "18.1", "source-build":
                                     xmlio.SOURCE_BUILD, "source-platform": "win",
                                     "version": "18.1"})
        ET.SubElement(wb, "datasources").append(
            tds.write(m, tds.CONNECTIONS["postgres"], root=False, extract=extract))
        assert errors(xmlio.serialize(wb).decode()) == []


def test_real_postgres_tds_parses():
    """tableau/connector-plugin-sdk's cast_calcs.postgres.tds (MIT)."""
    model, reports = tableau.read_project(FIXTURES / "cast_calcs.postgres.tds")
    assert reports == ()
    assert [(t.name, t.source) for t in model.tables] == [("Calcs", "public.Calcs")]
    cols = {c.name: c for c in model.tables[0].columns}
    assert len(cols) == 27
    assert cols["Num0"].source == "num0" and cols["Num0"].data_type == "float"
    assert cols["Int0"].data_type == "int" and cols["Bool0"].data_type == "bool"


def test_real_excel_join_is_refused_by_name():
    """Calm-Engineer/tableau-portfolio Example_JOIN.twb (MIT, Desktop 2025.1.0)."""
    with pytest.raises(Unsupported) as exc:
        tableau.read_project(FIXTURES / "Example_JOIN.twb")
    assert exc.value.feature == "excel-direct connection"


def test_real_star_schema_workbook():
    """VIVEK-MARRI/data-mart-credit-rating-reporting (MIT, Desktop 2025.2.4): a
    Postgres star schema with relationships. The data source reads exactly; each
    worksheet lowers or is refused by Tableau's name for what it uses."""
    root = xmlio.parse((FIXTURES / "credit_rating_visualizations.twb").read_bytes(), "wb")
    elem = root.find("datasources/datasource")
    ds = tds.read(elem, name_hint="wb", strict=False)
    assert [t.source for t in ds.model.tables] == [
        "credit_dm.dim_rating_agency", "credit_dm.dim_rating_type", "credit_dm.dim_security",
        "credit_dm.fact_outlier_metrics", "credit_dm.fact_ratings_scd",
        "credit_dm.kpi_rating_changes"]
    assert {(r.from_table, r.to_table) for r in ds.model.relationships} == {
        ("fact_ratings_scd", "dim_rating_agency"), ("kpi_rating_changes", "dim_rating_agency"),
        ("fact_outlier_metrics", "dim_rating_type"), ("fact_ratings_scd", "dim_rating_type"),
        ("fact_outlier_metrics", "dim_security"), ("fact_ratings_scd", "dim_security")}
    outcomes = {}
    for ws in root.findall("worksheets/worksheet"):
        try:
            outcomes[ws.get("name")] = twb._visual(ws, elem.get("name"), ds, "wb")
        except Unsupported as exc:
            outcomes[ws.get("name")] = exc.feature
    assert outcomes["Outlier % Trend"] == "date part on a shelf (Year)"
    assert outcomes["Top Outliers"] == "ATTR"
    assert outcomes["KPI – Total Upgrade"] == "measure filter"
    assert outcomes["Vendor Rating Changes"] == "sort of an outer dimension by a measure"
    assert outcomes["Country Comparison"] == "generated latitude/longitude"
    sheet = outcomes["Sheet 12"]
    assert sheet.group_by == (Col("kpi_rating_changes", "Year"),)
    assert sheet.values == (
        ("Rating Upgrades", Agg("sum", Col("kpi_rating_changes", "Rating Upgrades"))),
        ("Rating Downgrades", Agg("sum", Col("kpi_rating_changes", "Rating Downgrades"))))
    kpi = outcomes["KPI – Avg Precision Score"]
    assert kpi.values == (("Avg. Precision Score",
                           Agg("avg", Col("fact_outlier_metrics", "Precision Score"))),)
    with pytest.raises(Unsupported, match="generated latitude/longitude"):
        tableau.read_project(FIXTURES / "credit_rating_visualizations.twb")


# ================================================================ 4. fan-out
#: A logical table over a physical INNER JOIN of order headers and order lines,
#: hand-written in the shape of Calm-Engineer's Example_JOIN.twb (Desktop 2025.1).
JOIN_TDS = (FIXTURES / "joined_orders.tds").read_text("utf-8")


def test_physical_join_lowers_to_its_join(tmp_path):
    (tmp_path / "Joined.tds").write_text(JOIN_TDS, encoding="utf-8")
    model, _ = tableau.read_project(tmp_path / "Joined.tds")
    (table,) = model.tables
    assert table.source is None
    assert "INNER JOIN warehouse.fct_order_lines AS fct_order_lines" in table.source_sql
    assert "ON fct_orders.order_id = fct_order_lines.order_id" in table.source_sql
    assert [(c.name, c.source) for c in table.columns] == [
        ("order_id", "order_id"), ("order_amount", "order_amount"),
        ("order_id (fct_order_lines)", "order_id (fct_order_lines)"),
        ("line_amount", "line_amount")]
    assert [m.expr for m in model.measures] == [Agg("sum", Col("Orders", "order_amount")),
                                                Agg("sum", Col("Orders", "line_amount"))]


@needs_engine
def test_physical_join_double_counts_like_the_warehouse(tmp_path):
    import duckdb

    wh = examples.write_fixture(tmp_path / "fixture.duckdb")
    (tmp_path / "Joined.tds").write_text(JOIN_TDS, encoding="utf-8")
    model, _ = tableau.read_project(tmp_path / "Joined.tds")
    out = render.render_visual(model, Visual("v", values=(
        ("Revenue", MeasureRef("Revenue")), ("Line Revenue", MeasureRef("Line Revenue")))), wh)
    with duckdb.connect(str(wh), read_only=True) as con:
        joined, true_total = con.execute(
            "SELECT (SELECT SUM(o.order_amount) FROM warehouse.fct_orders o JOIN "
            "warehouse.fct_order_lines l ON o.order_id = l.order_id), "
            "(SELECT SUM(order_amount) FROM warehouse.fct_orders)").fetchone()
    revenue, line_revenue = out["rows"][0]
    assert Decimal(revenue) == joined            # what Tableau shows: fanned out
    assert Decimal(revenue) > true_total         # ... and it is wrong
    assert Decimal(line_revenue) == true_total   # line grain sums to the header total


def test_custom_sql_relation_is_source_sql(tab, tmp_path):
    m, _ = tab
    sql = "SELECT order_id, order_amount FROM warehouse.fct_orders WHERE order_amount > 0"
    custom = replace(m, tables=(replace(m.tables[0], source=None, source_sql=sql,
                                        columns=m.tables[0].columns[:1]
                                        + m.tables[0].columns[4:5]),),
                     relationships=(), measures=m.measures[:1])
    text = xmlio.serialize(tds.write(custom, tds.CONNECTIONS["duckdb"], root=True))
    assert b"name='Custom SQL Query' type='text'>SELECT order_id, order_amount FROM " \
           b"warehouse.fct_orders WHERE order_amount &gt; 0</relation>" in text
    back = tableau.project.read_datasource_bytes(text, "Commerce.tds").model
    assert back == custom


# ======================================================== relationships
def _tds_text(project):
    return (project / "datasources" / "Commerce.tds").read_text("utf-8")


def _read_text(text):
    return tableau.project.read_datasource_bytes(text.encode(), "Commerce.tds")


def test_many_to_many_relationship_is_refused(project):
    text = _tds_text(project).replace(" unique-key='true' />", " />")
    with pytest.raises(Unsupported) as exc:
        _read_text(text)
    assert exc.value.feature == "Many-to-Many relationship cardinality"


def test_one_side_on_the_first_end_point_reverses_the_direction(project):
    text = _tds_text(project)
    block = re.search(r"<relationship>\s*<expression op='='>\s*<expression op='\[customer_id\]' />"
                      r".*?</relationship>", text, re.S).group()
    swapped = block.replace("<first-end-point object-id=", "<first-end-point unique-key='true' "
                            "object-id=").replace(" unique-key='true' />\n", " />\n", 1)
    model = _read_text(text.replace(block, swapped)).model
    assert any(r.from_table == "Customer" and r.to_table == "Orders"
               for r in model.relationships)


def test_extract_only_source_is_refused(project):
    text = _tds_text(project).replace("class='genericodbc'", "class='hyper'")
    with pytest.raises(Unsupported, match="extract-only data source"):
        _read_text(text)


def test_data_source_filter_is_refused(project):
    text = _tds_text(project).replace(
        "  <aliases enabled='yes' />",
        "  <aliases enabled='yes' />\n  <filter class='categorical' column='[segment]' />")
    with pytest.raises(Unsupported, match="data source filter"):
        _read_text(text)


# ================================================================== calc
@pytest.fixture(scope="module")
def m():
    return tableau_commerce_model()


ROUND_TRIP = [
    "SUM([order_amount])",
    "AVG([order_amount])",
    "COUNTD([customer_id (dim_customer)])",
    "SUM([quantity] * [unit_price])",
    "SUM(IF [order_status] = 'completed' THEN [order_amount] END)",
    "ZN(SUM([order_amount])) - SUM([shipping_amount])",
    "IFNULL(SUM([order_amount]), 0) / SUM([shipping_amount])",
    "IIF(SUM([order_amount]) > 100, 'big', 'small')",
    "IIF(SUM([order_amount]) > 100, 'big', 'small', 'unknown')",
    "IF SUM([order_amount]) > 100 THEN 'big' ELSEIF SUM([order_amount]) > 10 THEN 'mid' "
    "ELSE 'small' END",
    "MIN({FIXED : SUM([order_amount])})",
    "SUM([order_amount]) / MIN({FIXED : SUM([order_amount])})",
    "MAX(DATETRUNC('month', [order_date]))",
    "MAX(DATETRUNC('week', [order_date], 'monday'))",
    "COUNTD(UPPER([customer_name]) + ' x')",
    "SUM(ROUND([order_amount], 1))",
    "NOT (SUM([order_amount]) > 0) AND SUM([quantity]) > 0 OR SUM([quantity]) < 0",
    "SUM(ABS(-[order_amount]))",
    "MAX(ISNULL([order_amount]))",
    "SUM(IF [order_date] >= #2026-01-01# THEN [order_amount] END)",
]


@pytest.mark.parametrize("text", ROUND_TRIP)
def test_calc_round_trip(text, m):
    try:
        expr = calc.parse_expression(text, m)
    except Unsupported as exc:
        assert text.startswith("COUNTD(UPPER"), exc   # COUNTD of an expression: refused
        return
    again = calc.format_expression(expr, m)
    assert calc.parse_expression(again, m) == expr
    assert calc.format_expression(calc.parse_expression(again, m), m) == again


def test_calc_lowerings_are_the_documented_trees(m):
    status, amount = Col("Orders", "order_status"), Col("Orders", "order_amount")
    p = lambda t: calc.parse_expression(t, m)  # noqa: E731
    assert p("SUM([quantity] * [unit_price])") == IterAgg(
        "sum", "Order Lines", BinOp("*", Col("Order Lines", "quantity"),
                                    Related("Product", "unit_price")))
    # IIF is three-valued: NULL test -> NULL (or the 4th argument), not ELSE.
    test = BinOp(">", amount, Lit(10))
    assert p("IIF([order_amount] > 10, 1, 2)") == If(test, Lit(1), If(Not(test), Lit(2)))
    assert p("IF [order_amount] > 10 THEN 1 ELSE 2 END") == If(test, Lit(1), Lit(2))
    assert p("ISNULL([order_amount])") == If(BinOp("=", amount, amount), Lit(False), Lit(True))
    assert p("ZN([order_amount])") == Coalesce((amount, Lit(0)))
    assert p("IFNULL([order_amount], IFNULL([shipping_amount], 0))") == Coalesce(
        (amount, Col("Orders", "shipping_amount"), Lit(0)))
    assert p("CASE [order_status] WHEN 'completed' THEN 1 ELSE 0 END") == \
        If(BinOp("=", status, Lit("completed")), Lit(1), Lit(0))
    assert p("[order_status] IN ('a', 'b')") == BinOp(
        "or", BinOp("=", status, Lit("a")), BinOp("=", status, Lit("b")))
    assert p("SUM([order_amount]) / SUM([shipping_amount])") == Divide(
        Agg("sum", amount), Agg("sum", Col("Orders", "shipping_amount")))
    assert p("MIN({FIXED : SUM([order_amount])})") == If(
        BinOp(">", Agg("count_rows", table="Orders"), Lit(0)),
        Calculate(Agg("sum", amount), (RemoveFilters(),)))
    # {AGG} is FIXED with no dimensions.
    assert p("MIN({SUM([order_amount])})") == p("MIN({FIXED : SUM([order_amount])})")
    assert p("[customer_name] + 'x'") == BinOp("&", Col("Customer", "customer_name"), Lit("x"))
    assert p("// comment\nSUM([order_amount]) /* inline */") == Agg("sum", amount)
    assert p("'it''s'") == Lit("it's")
    # Model names (what an author types in the editor) resolve too.
    assert p("[Net Revenue]") == MeasureRef("Net Revenue")


def test_row_level_expressions_can_be_homed_on_a_table(m):
    """`table=` is the calculated column's table: Tableau formulas name fields, so
    it only picks the row; it must reach every table the formula reads."""
    p = calc.parse_expression
    assert p("[quantity] * [unit_price]", m, table="Order Lines") == BinOp(
        "*", Col("Order Lines", "quantity"), Related("Product", "unit_price"))
    assert p("[unit_price]", m, table="Order Lines") == Related("Product", "unit_price")
    assert p("[unit_price]", m) == Col("Product", "unit_price")
    with pytest.raises(ModelError, match="does not reach"):
        p("[quantity] * [unit_price]", m, table="Product")
    assert tableau.format_expression(Related("Product", "unit_price"), m,
                                     table="Order Lines") == "[unit_price]"


REFUSALS = [
    ("WINDOW_SUM(SUM([order_amount]))", "table calculation WINDOW_SUM"),
    ("RUNNING_SUM(SUM([order_amount]))", "table calculation RUNNING_SUM"),
    ("LOOKUP(SUM([order_amount]), -1)", "table calculation LOOKUP"),
    ("RANK(SUM([order_amount]))", "table calculation RANK"),
    ("INDEX()", "table calculation INDEX"),
    ("TOTAL(SUM([order_amount]))", "table calculation TOTAL"),
    ("SIZE()", "table calculation SIZE"),
    ("{INCLUDE [segment] : SUM([order_amount])}", "INCLUDE level of detail expression"),
    ("{EXCLUDE [segment] : SUM([order_amount])}", "EXCLUDE level of detail expression"),
    ("{FIXED [segment] : SUM([order_amount])}",
     "FIXED level of detail expression with dimensions"),
    ("SUM({FIXED : SUM([order_amount])})", "SUM of a FIXED level of detail expression"),
    ("{FIXED : SUM([order_amount])} + 1",
     "FIXED level of detail expression used as a row-level value"),
    ("{FIXED : SUM([order_amount])}", "FIXED level of detail expression used without "
                                      "aggregation"),
    ("[Parameters].[Threshold]", "parameter"),
    ("[Other Source].[Sales]", "data blending (field of another data source)"),
    ("ATTR([segment])", "ATTR"),
    ("MEDIAN([order_amount])", "MEDIAN"),
    ("DATEPART('weekday', [order_date])", "DATEPART('weekday')"),
    ("DATETRUNC('week', [order_date])", "DATETRUNC('week') without 'monday' as start of week"),
    ("COUNTD([order_amount] + 1)", "COUNTD of a calculation"),
    ("SUM([order_amount]) % 2", "% (modulo) operator"),
    ("MIN([order_amount], [shipping_amount])", "MIN with two arguments (row-level MIN)"),
    ("SUM(ZN([unit_price]) * [quantity])",
     "row-level calculation across related tables with NULL handling"),
    ("SUM([unit_price] + [order_amount])", "row-level calculation across unrelated tables"),
    ("SUM(1)", "SUM of a constant"),
    ("DATEADD('month', 1, [order_date])", "function DATEADD"),
    ("[__tableau_internal_object_id__].[Orders]", "table count field in a calculation"),
]


@pytest.mark.parametrize("text,feature", REFUSALS)
def test_calc_refusals_name_the_tableau_feature(text, feature, m):
    with pytest.raises(Unsupported) as exc:
        calc.parse_expression(text, m)
    assert exc.value.feature == feature
    assert exc.value.vendor == "Tableau"


def test_calc_author_errors_are_model_errors(m):
    with pytest.raises(ModelError, match="Cannot mix aggregate and non-aggregate"):
        calc.parse_expression("SUM([order_amount]) + [order_amount]", m)
    with pytest.raises(ModelError, match="already an aggregation"):
        calc.parse_expression("SUM(SUM([order_amount]))", m)
    with pytest.raises(ModelError, match="unknown field"):
        calc.parse_expression("[no such field]", m)
    with pytest.raises(ModelError, match="expected"):
        calc.parse_expression("IF [order_amount] > 1 THEN 1", m)


def test_calc_columns_and_unsupported_calcs_in_a_data_source(tab, project):
    """A row-level calc becomes a calculated column on its home table; a calc
    outside the subset is refused when read strictly, and only when USED when
    read leniently (the fake server's mode)."""
    text = _tds_text(project)
    extra = (
        "  <column caption='Line Value' datatype='real' name='[Calculation_1]' "
        "role='measure' type='quantitative'>\n"
        "    <calculation class='tableau' formula='[quantity] * [unit_price]' />\n"
        "  </column>\n"
        "  <column caption='Running' datatype='real' name='[Calculation_2]' role='measure' "
        "type='quantitative'>\n"
        "    <calculation class='tableau' formula='RUNNING_SUM(SUM([order_amount]))' />\n"
        "  </column>\n")
    text = text.replace("  <layout ", extra + "  <layout ", 1)
    with pytest.raises(Unsupported, match="RUNNING_SUM"):
        _read_text(text)
    ds = tableau.project.read_datasource_bytes(text.encode(), "Commerce.tds", strict=False)
    lines = next(t for t in ds.model.tables if t.name == "Order Lines")
    col = lines.columns[-1]
    assert (col.name, col.source, col.data_type) == ("Line Value", "Calculation_1", "decimal")
    assert col.expr == BinOp("*", Col("Order Lines", "quantity"), Related("Product", "unit_price"))
    assert "Calculation_2" in ds.deferred
    with pytest.raises(Unsupported, match="RUNNING_SUM"):
        ds.names.resolve("Calculation_2", allow_captions=False)


def test_sets_groups_and_bins_are_refused_by_name(project):
    text = _tds_text(project)
    a_set = ("  <group caption='Top Customers' name='[Top Customers]' name-style='unqualified' "
             "user:ui-builder='filter-group'>\n    <groupfilter function='member' "
             "level='[customer_name]' member='&quot;Customer 01&quot;' />\n  </group>\n")
    with_set = text.replace("  <layout ", a_set + "  <layout ", 1)
    _read_text(with_set)                                    # defined, unused: fine
    use = ("  <column caption='In Top' datatype='boolean' name='[Calculation_9]' "
           "role='dimension' type='nominal'>\n    <calculation class='tableau' "
           "formula='[Top Customers]' />\n  </column>\n")
    with pytest.raises(Unsupported) as exc:
        _read_text(with_set.replace("  <layout ", use + "  <layout ", 1))
    assert exc.value.feature == "set"
    a_group = ("  <column caption='Segment (group)' datatype='string' name='[segment (group)]' "
               "role='dimension' type='nominal'>\n    <calculation class='categorical-bin' "
               "column='[segment]' new-bin='true' />\n  </column>\n")
    with pytest.raises(Unsupported) as exc:
        _read_text(text.replace("  <layout ", a_group + "  <layout ", 1))
    assert exc.value.feature == "group"


# ================================================================ reports
def test_report_shapes_the_writer_refuses(tab, tmp_path):
    m, _ = tab
    bad = [
        Visual("v", group_by=(Col("Customer", "segment"), Col("Customer", "region")),
               values=(("Net Revenue", MeasureRef("Net Revenue")),),
               order_by=(("Net Revenue", True), ("region", False))),
        Visual("v", group_by=(Col("Customer", "segment"),),
               values=(("Net Revenue", MeasureRef("Net Revenue")),),
               filters=(FilterCompare(Col("Orders", "order_amount"), ">", 10),)),
        Visual("v", sql="select 1"),
    ]
    for visual in bad:
        with pytest.raises(Unsupported):
            tableau.write_project(m, (Report("R", "Commerce", (Page("P", (visual,)),)),),
                                  tmp_path)


def test_values_with_their_own_labels_become_workbook_calcs(tab, tmp_path):
    """A value Tableau would label differently (or an inline expression) is
    written as a calculated field local to the workbook, captioned with the
    label - and reads back as the same (label, expression)."""
    m, _ = tab
    visual = Visual("Margin", group_by=(Col("Customer", "region"),), values=(
        ("Revenue", MeasureRef("Gross Revenue")),
        ("Ship share", Divide(Agg("sum", Col("Orders", "shipping_amount")),
                              MeasureRef("Gross Revenue"))),
        ("Avg. order_amount", Agg("avg", Col("Orders", "order_amount"))),
        ("Orders (Count)", Agg("count_rows", table="Orders")),
    ), filters=(FilterValues(Col("Customer", "segment"), ("SMB",), negate=True),
                FilterCompare(Col("Orders", "order_date"), ">=",
                              __import__("datetime").date(2025, 6, 1))), kind="table",
                    groups="rows")
    report = Report("Margins", "Commerce", (Page("Margin", (visual,)),))
    tableau.write_project(m, (report,), tmp_path)
    text = (tmp_path / "workbooks" / "Margins.twb").read_text("utf-8")
    assert "caption='Ship share'" in text and "caption='Revenue'" in text
    assert "[__tableau_internal_object_id__].[cnt:fct_orders" in text
    assert "<dashboard " not in text          # a one-sheet page is just the sheet
    assert tableau.read_project(tmp_path) == (m, (report,))


def test_member_values_keep_their_types(tab, tmp_path):
    m, _ = tab
    visual = Visual("Filters", group_by=(Col("Date", "year"),),
                    values=(("Units", MeasureRef("Units")),),
                    filters=(FilterValues(Col("Date", "is_weekend"), (False,)),
                             FilterValues(Col("Customer", "segment"), ("SMB", "Consumer")),
                             FilterValues(Col("Date", "year"), (2025, 2026)),
                             FilterValues(Col("Product", "unit_price"), (Decimal("25.00"),)),
                             FilterValues(Col("Date", "date_day"),
                                          (__import__("datetime").date(2025, 1, 1),))),
                    kind="table", groups="rows")
    report = Report("Typed", "Commerce", (Page("Filters", (visual,)),))
    tableau.write_project(m, (report,), tmp_path)
    assert tableau.read_project(tmp_path)[1] == (report,)


def test_groups_rows_is_tableau_and_measures_needs_one_measure(tab, tmp_path):
    """Tableau shows every group that has rows (Visual.groups="rows"). Dropping
    all-blank groups ("measures") has one exact spelling - a Non-null filter on
    the view's only measure - and none for several measures together."""
    m, _ = tab
    one = Visual("One", group_by=(Col("Customer", "segment"),),
                 values=(("Net Revenue", MeasureRef("Net Revenue")),), groups="measures")
    report = Report("Groups", "Commerce", (Page("One", (one,)),))
    tableau.write_project(m, (report,), tmp_path / "one")
    text = (tmp_path / "one" / "workbooks" / "Groups.twb").read_text("utf-8")
    assert re.search(r"<filter class='quantitative' column='\[sqlproxy\.\w+\]\.\[usr:"
                     r"Calculation_\d+:qk\]' included-values='non-null' />", text)
    assert tableau.read_project(tmp_path / "one")[1] == (report,)
    two = replace(one, name="Two", values=one.values + (("Units", MeasureRef("Units")),))
    with pytest.raises(Unsupported) as exc:
        tableau.write_project(m, (Report("G", "Commerce", (Page("Two", (two,)),)),),
                              tmp_path / "two")
    assert exc.value.feature == "hiding groups whose every measure is blank"
    # A card has one row either way: written as is, read back as Tableau's "rows".
    card = Visual("Card", values=one.values + (("Units", MeasureRef("Units")),), kind="card")
    tableau.write_project(m, (Report("C", "Commerce", (Page("Card", (card,)),)),),
                          tmp_path / "card")
    assert tableau.read_project(tmp_path / "card")[1][0].pages[0].visuals == (
        replace(card, groups="rows"),)


def test_a_measure_grouped_by_its_many_side_is_refused(tab, tmp_path):
    """Tableau's relationships compute COUNTD(customer) per order status through
    the join; the model never filters Customer by an Orders column. Refused on
    write and on read rather than printed as one total on every row."""
    m, _ = tab
    visual = Visual("Customers by status", group_by=(Col("Orders", "order_status"),),
                    values=(("Distinct count of customer_name",
                             Agg("count_distinct", Col("Customer", "customer_name"))),),
                    groups="rows")
    report = Report("Dir", "Commerce", (Page("Customers by status", (visual,)),))
    with pytest.raises(Unsupported) as exc:
        tableau.write_project(m, (report,), tmp_path)
    assert exc.value.feature == "dimension from the many side of a relationship"
    # The same sheet written by hand (a twb Desktop would save) is refused on read.
    fine = replace(visual, group_by=(Col("Customer", "region"),))
    tableau.write_project(m, (replace(report, pages=(Page("Customers by status", (fine,)),)),),
                          tmp_path)
    path = tmp_path / "workbooks" / "Dir.twb"
    text = path.read_text("utf-8")
    text = text.replace("[none:region:nk]", "[none:order_status:nk]").replace(
        "column='[region]'", "column='[order_status]'").replace("name='[region]'",
                                                                "name='[order_status]'")
    path.write_text(text, "utf-8")
    with pytest.raises(Unsupported, match="dimension from the many side of a relationship"):
        tableau.read_project(tmp_path)


def test_dashboard_reads_sheets_in_zone_order_and_orphans_as_pages(project):
    model, (report,) = tableau.read_project(project)
    assert [p.name for p in report.pages] == ["Overview", "Regions"]
    assert [v.name for v in report.pages[0].visuals] == [
        "KPIs", "Revenue by Month", "Revenue by Segment", "Top Products"]
    text = (project / "workbooks" / "Executive Overview.twb").read_text("utf-8")
    start = text.index("    <dashboard enable-sort-zone-taborder='true' name='Regions'>")
    end = text.index("</dashboard>", start) + len("</dashboard>")
    (project / "workbooks" / "Executive Overview.twb").write_text(text[:start] + text[end:],
                                                                  "utf-8")
    _, (report,) = tableau.read_project(project)
    assert [p.name for p in report.pages] == ["Overview", "Revenue by Region 2026"]


def test_report_measure_order_survives(tab):
    m, _ = tab
    names = calc.Names.for_model(m)
    numbers = [int(names.of_measure[x.name].split("_")[1]) for x in m.measures]
    assert numbers == sorted(numbers)        # creation order == name order, as in Desktop


def test_unknown_warehouse_is_refused(tab, tmp_path):
    with pytest.raises(ModelError, match="unknown warehouse"):
        tableau.write_project(*tab[:1], (), tmp_path, warehouse="oracle")


def test_measure_round_trip_keeps_format_and_description(tab, project):
    m, _ = tab
    model, _ = tableau.read_project(project)
    net = next(x for x in model.measures if x.name == "Net Revenue")
    assert net == Measure("Net Revenue", "Orders", m.measures[1].expr, "$#,0.00",
                          "Revenue from completed orders.")
