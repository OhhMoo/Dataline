"""The Commerce model and a tiny deterministic fixture of its warehouse.

The commerce_analytics theme builds this star schema at full size (schema
`warehouse`, dbt models). Every front-end round-trips `commerce_model()` /
`executive_report()`, and the engine's tests evaluate them against
`write_fixture()`, so all of them agree on one example before any theme exists.

Warehouse schema (`warehouse.*`), shared with the theme:

    dim_customer    customer_id INTEGER, customer_name VARCHAR, segment VARCHAR
                    ('Enterprise' | 'SMB' | 'Consumer'), region VARCHAR
                    ('NA' | 'EMEA' | 'APAC'), signup_date DATE
    dim_product     product_id INTEGER, product_name VARCHAR, category VARCHAR,
                    unit_price DECIMAL(12,2)
    dim_date        date_day DATE, year INTEGER, quarter INTEGER, month INTEGER,
                    month_name VARCHAR, week_start DATE, is_weekend BOOLEAN
    fct_orders      order_id INTEGER, order_date DATE, customer_id INTEGER,
                    order_status VARCHAR ('completed' | 'refunded' | 'cancelled'),
                    order_amount DECIMAL(12,2), shipping_amount DECIMAL(12,2)
    fct_order_lines order_line_id INTEGER, order_id INTEGER, product_id INTEGER,
                    quantity INTEGER, line_amount DECIMAL(12,2), order_date DATE,
                    customer_id INTEGER
    (sum of an order's line_amount == its order_amount)
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from .expr import (
    Agg,
    BinOp,
    Calculate,
    Col,
    Divide,
    FilterValues,
    IterAgg,
    MeasureRef,
    Related,
    RemoveFilters,
    TimeShift,
)
from .model import Column, Measure, Model, Page, Relationship, Report, Table, Visual

FIXTURE_START = dt.date(2025, 1, 1)
FIXTURE_END = dt.date(2026, 2, 28)          # inclusive; 14 months, so YoY exists
COMPLETED = FilterValues(Col("Orders", "order_status"), ("completed",))


def commerce_model(null_semantics: str = "sql") -> Model:
    def c(name, dtype, **kw):
        return Column(name=name, source=name, data_type=dtype, **kw)

    tables = (
        Table("Orders", "warehouse.fct_orders", (
            c("order_id", "int"), c("order_date", "date"), c("customer_id", "int", hidden=True),
            c("order_status", "string"), c("order_amount", "decimal"),
            c("shipping_amount", "decimal"))),
        Table("Order Lines", "warehouse.fct_order_lines", (
            c("order_line_id", "int"), c("order_id", "int", hidden=True),
            c("product_id", "int", hidden=True), c("quantity", "int"),
            c("line_amount", "decimal"), c("order_date", "date"),
            c("customer_id", "int", hidden=True))),
        Table("Customer", "warehouse.dim_customer", (
            c("customer_id", "int"), c("customer_name", "string"), c("segment", "string"),
            c("region", "string"), c("signup_date", "date"))),
        Table("Product", "warehouse.dim_product", (
            c("product_id", "int"), c("product_name", "string"), c("category", "string"),
            c("unit_price", "decimal"))),
        Table("Date", "warehouse.dim_date", (
            c("date_day", "date"), c("year", "int"), c("quarter", "int"), c("month", "int"),
            c("month_name", "string"), c("week_start", "date"), c("is_weekend", "bool")),
            is_date_table=True, date_column="date_day"),
    )
    relationships = (
        Relationship("Orders", "customer_id", "Customer", "customer_id"),
        Relationship("Orders", "order_date", "Date", "date_day"),
        Relationship("Order Lines", "order_id", "Orders", "order_id"),
        Relationship("Order Lines", "product_id", "Product", "product_id"),
    )
    net = MeasureRef("Net Revenue")
    ly = MeasureRef("Revenue LY")
    measures = (
        Measure("Gross Revenue", "Orders", Agg("sum", Col("Orders", "order_amount")),
                format_string="$#,0.00"),
        Measure("Net Revenue", "Orders", Calculate(MeasureRef("Gross Revenue"), (COMPLETED,)),
                format_string="$#,0.00", description="Revenue from completed orders."),
        Measure("Order Count", "Orders", Agg("count_rows", table="Orders")),
        Measure("Completed Orders", "Orders",
                Calculate(MeasureRef("Order Count"), (COMPLETED,))),
        Measure("AOV", "Orders", Divide(net, MeasureRef("Completed Orders")),
                format_string="$#,0.00", description="Average order value, completed orders."),
        Measure("Units", "Order Lines", Agg("sum", Col("Order Lines", "quantity"))),
        Measure("List Price Revenue", "Order Lines",
                IterAgg("sum", "Order Lines",
                        BinOp("*", Col("Order Lines", "quantity"),
                              Related("Product", "unit_price")))),
        Measure("Segment Share", "Orders",
                Divide(net, Calculate(
                    net, (RemoveFilters(columns=(Col("Customer", "segment"),)),))),
                format_string="0.0%"),
        Measure("Revenue YTD", "Orders", TimeShift(net, Col("Date", "date_day"), "ytd")),
        Measure("Revenue LY", "Orders",
                TimeShift(net, Col("Date", "date_day"), "same_period_last_year")),
        Measure("YoY Growth", "Orders", Divide(BinOp("-", net, ly), ly), format_string="0.0%"),
    )
    return Model("Commerce", tables, relationships, measures, null_semantics=null_semantics)


def executive_report() -> Report:
    m = MeasureRef
    overview = Page("Overview", (
        Visual("KPIs", values=(("Net Revenue", m("Net Revenue")),
                               ("Completed Orders", m("Completed Orders")),
                               ("AOV", m("AOV"))), kind="card"),
        Visual("Revenue by Month", group_by=(Col("Date", "year"), Col("Date", "month")),
               values=(("Net Revenue", m("Net Revenue")), ("Revenue LY", m("Revenue LY")),
                       ("YoY Growth", m("YoY Growth"))), kind="lineChart"),
        Visual("Revenue by Segment", group_by=(Col("Customer", "segment"),),
               values=(("Net Revenue", m("Net Revenue")),
                       ("Segment Share", m("Segment Share"))),
               order_by=(("Net Revenue", True),), kind="barChart"),
        Visual("Top Products", group_by=(Col("Product", "product_name"),),
               values=(("Units", m("Units")),), order_by=(("Units", True),), top_n=3,
               kind="table"),
    ))
    regions = Page("Regions", (
        Visual("Revenue by Region 2026", group_by=(Col("Customer", "region"),),
               values=(("Net Revenue", m("Net Revenue")),),
               filters=(FilterValues(Col("Date", "year"), (2026,)),), kind="barChart"),
    ))
    return Report("Executive Overview", "Commerce", (overview, regions))


# ------------------------------------------------------------------- fixture
SEGMENTS = ("Enterprise", "Enterprise", "SMB", "SMB", "Consumer", "Consumer")
REGIONS = ("NA", "EMEA", "APAC")
PRODUCTS = ((1, "Widget", "Hardware", Decimal("25.00")),
            (2, "Gadget", "Hardware", Decimal("40.00")),
            (3, "Support Plan", "Services", Decimal("120.00")),
            (4, "Cable", "Accessories", Decimal("5.50")))


def fixture_rows() -> dict[str, list[tuple]]:
    """The fixture's rows, pure: no randomness, no clock."""
    customers = [(i, f"Customer {i:02d}", SEGMENTS[i - 1], REGIONS[(i - 1) % 3],
                  dt.date(2024, i, 1)) for i in range(1, 7)]
    products = list(PRODUCTS)
    dates, day = [], FIXTURE_START
    while day <= FIXTURE_END:
        q = (day.month - 1) // 3 + 1
        dates.append((day, day.year, q, day.month, day.strftime("%B"),
                      day - dt.timedelta(days=day.weekday()), day.weekday() >= 5))
        day += dt.timedelta(days=1)
    orders, lines, line_id = [], [], 0
    for i, (day, *_rest) in enumerate(dates):
        if i % 3:
            continue
        n = i // 3 + 1
        status = "refunded" if n % 11 == 0 else "cancelled" if n % 17 == 0 else "completed"
        customer = n % 6 + 1
        amounts = []
        for k in range(1 + n % 3):
            pid, _, _, price = PRODUCTS[(n + k) % 4]
            qty = 1 + (n + k) % 4
            line_id += 1
            amount = price * qty
            amounts.append(amount)
            lines.append((line_id, n, pid, qty, amount, day, customer))
        orders.append((n, day, customer, status, sum(amounts, Decimal("0.00")),
                       Decimal(f"{(n * 7) % 15}.99")))
    return {"dim_customer": customers, "dim_product": products, "dim_date": dates,
            "fct_orders": orders, "fct_order_lines": lines}


DDL = {
    "dim_customer": "customer_id INTEGER, customer_name VARCHAR, segment VARCHAR, "
                    "region VARCHAR, signup_date DATE",
    "dim_product": "product_id INTEGER, product_name VARCHAR, category VARCHAR, "
                   "unit_price DECIMAL(12,2)",
    "dim_date": "date_day DATE, year INTEGER, quarter INTEGER, month INTEGER, "
                "month_name VARCHAR, week_start DATE, is_weekend BOOLEAN",
    "fct_orders": "order_id INTEGER, order_date DATE, customer_id INTEGER, "
                  "order_status VARCHAR, order_amount DECIMAL(12,2), "
                  "shipping_amount DECIMAL(12,2)",
    "fct_order_lines": "order_line_id INTEGER, order_id INTEGER, product_id INTEGER, "
                       "quantity INTEGER, line_amount DECIMAL(12,2), order_date DATE, "
                       "customer_id INTEGER",
}


def write_fixture(path: Path) -> Path:
    """A DuckDB file with schema `warehouse` holding fixture_rows(). Do not name it
    warehouse.duckdb: the catalog would collide with the schema (world/README.md)."""
    import duckdb

    path = Path(path)
    if path.exists():
        path.unlink()
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE SCHEMA warehouse")
        for table, rows in fixture_rows().items():
            con.execute(f"CREATE TABLE warehouse.{table} ({DDL[table]})")
            marks = ", ".join("?" * len(rows[0]))
            con.executemany(f"INSERT INTO warehouse.{table} VALUES ({marks})", rows)
    return path
