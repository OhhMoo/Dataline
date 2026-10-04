"""The Commerce example as a Tableau shop would build it.

`examples.commerce_model()` is written in Power BI's terms, and five of its
eleven measures have no exact Tableau calculated field (each refusal is named by
`calc.Formatter`; test_tableau_format.py asserts them):

    Net Revenue, Completed Orders   CALCULATE(..., status = "completed") REPLACES a
                                    view filter on status; a Tableau calc can only
                                    intersect with it (SUM(IF ... END))
    Order Count                     COUNTROWS: a table's Count field cannot be used
                                    in a calculation
    Segment Share                   ALL(segment): Tableau has no per-column filter
                                    removal; FIXED ignores every dimension filter
    Revenue YTD, Revenue LY, YoY    time intelligence: Tableau has none
    (and the Date table)            "Mark as date table" has no Tableau setting

This is the same star schema with the measures written the way a Tableau author
writes them. On the executive report's views (which never filter or group by
order_status, and filter only what Segment Share does not read) the numbers are
the Power BI model's numbers; the definitions differ exactly where the two tools
differ, which is the point of keeping both.
"""
from __future__ import annotations

from dataclasses import replace

from ...examples import commerce_model
from ...expr import (
    Agg,
    BinOp,
    Calculate,
    Col,
    Divide,
    FilterValues,
    If,
    IterAgg,
    Lit,
    MeasureRef,
    Related,
    RemoveFilters,
)
from ...model import Measure, Model, Page, Report, Visual

COMPLETED = BinOp("=", Col("Orders", "order_status"), Lit("completed"))


def tableau_commerce_model() -> Model:
    base = commerce_model("sql")
    net = MeasureRef("Net Revenue")
    measures = (
        Measure("Gross Revenue", "Orders", Agg("sum", Col("Orders", "order_amount")),
                format_string="$#,0.00"),
        # SUM(IF [order_status] = 'completed' THEN [order_amount] END)
        Measure("Net Revenue", "Orders",
                IterAgg("sum", "Orders", If(COMPLETED, Col("Orders", "order_amount"))),
                format_string="$#,0.00", description="Revenue from completed orders."),
        # COUNTD([order_id]): the Tableau idiom for an order count (order_id is the key)
        Measure("Order Count", "Orders", Agg("count_distinct", Col("Orders", "order_id"))),
        # SUM(IF [order_status] = 'completed' THEN 1 END)
        Measure("Completed Orders", "Orders", IterAgg("sum", "Orders", If(COMPLETED, Lit(1)))),
        Measure("AOV", "Orders", Divide(net, MeasureRef("Completed Orders")),
                format_string="$#,0.00", description="Average order value, completed orders."),
        Measure("Units", "Order Lines", Agg("sum", Col("Order Lines", "quantity"))),
        Measure("List Price Revenue", "Order Lines",
                IterAgg("sum", "Order Lines",
                        BinOp("*", Col("Order Lines", "quantity"),
                              Related("Product", "unit_price")))),
        # [Net Revenue] / MIN({FIXED : [Net Revenue]})
        Measure("Segment Share", "Orders",
                Divide(net, If(BinOp(">", Agg("count_rows", table="Orders"), Lit(0)),
                               Calculate(net, (RemoveFilters(),)))),
                format_string="0.0%"),
    )
    tables = tuple(replace(t, is_date_table=False, date_column=None) for t in base.tables)
    return replace(base, tables=tables, measures=measures)


def tableau_executive_report() -> Report:
    """The executive report as Tableau worksheets: every view shows each group
    that has rows (Visual.groups="rows"), Tableau's behaviour."""
    m = MeasureRef
    overview = Page("Overview", (
        Visual("KPIs", values=(("Net Revenue", m("Net Revenue")),
                               ("Completed Orders", m("Completed Orders")),
                               ("AOV", m("AOV"))), kind="card", groups="rows"),
        Visual("Revenue by Month", group_by=(Col("Date", "year"), Col("Date", "month")),
               values=(("Net Revenue", m("Net Revenue")),
                       ("Gross Revenue", m("Gross Revenue"))), kind="lineChart",
               groups="rows"),
        Visual("Revenue by Segment", group_by=(Col("Customer", "segment"),),
               values=(("Net Revenue", m("Net Revenue")),
                       ("Segment Share", m("Segment Share"))),
               order_by=(("Net Revenue", True),), kind="barChart", groups="rows"),
        Visual("Top Products", group_by=(Col("Product", "product_name"),),
               values=(("Units", m("Units")),), order_by=(("Units", True),), top_n=3,
               kind="table", groups="rows"),
    ))
    regions = Page("Regions", (
        Visual("Revenue by Region 2026", group_by=(Col("Customer", "region"),),
               values=(("Net Revenue", m("Net Revenue")),),
               filters=(FilterValues(Col("Date", "year"), (2026,)),), kind="barChart",
               groups="rows"),
    ))
    return Report("Executive Overview", "Commerce", (overview, regions))
