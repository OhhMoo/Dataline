"""The Commerce model and Executive Overview as a Sigma team would build them.

`world.semantic.examples.commerce_model()` is written in Power BI's terms, and
four of its constructs have no Sigma formula (formula.py, refused by name):

  Net Revenue, Completed Orders   CALCULATE(..., status = "completed") REPLACES
                                  any filter on order_status; a Sigma condition
                                  (SumIf, CountIf) intersects with the element's
                                  filters and groupings
  Segment Share                   REMOVEFILTERS(segment) - no Sigma formula
                                  drops one filter
  Revenue YTD, Revenue LY         time intelligence over a marked date table;
  (and YoY Growth, built on LY)   Sigma has neither
  List Price Revenue              RELATED inside a metric; Sigma metrics do not
                                  support join functions

and its Date table is a marked date table, which Sigma has no notion of. Its
visuals drop all-blank groups (groups="measures"); a Sigma element keeps every
group its source has rows for (groups="rows").

This edition is the same star schema with the same names, written the way
Sigma says it: conditions inside the aggregate, the product price brought onto
Order Lines as a Lookup column, no date marking, and the visuals that only use
what Sigma can compute, each with groups="rows". On every visual it keeps from
executive_report() it computes the same values on the fixture: none of them
groups or filters by order_status - exactly the case where overwrite and
intersect agree - and no group of theirs is all blank there.
"""
from __future__ import annotations

from dataclasses import replace

from ... import examples as core
from ...expr import Agg, BinOp, Col, Divide, If, IterAgg, Lit, MeasureRef, Related
from ...model import Column, Measure, Model, Page, Report, Table, Visual

COMPLETED = BinOp("=", Col("Orders", "order_status"), Lit("completed"))
LIST_UNIT_PRICE = "list_unit_price"


def commerce_model() -> Model:
    base = core.commerce_model("sql")
    tables = []
    for t in base.tables:
        if t.is_date_table:
            t = replace(t, is_date_table=False, date_column=None)
        if t.name == "Order Lines":
            t = replace(t, columns=t.columns + (
                Column(LIST_UNIT_PRICE, LIST_UNIT_PRICE, "decimal",
                       expr=Related("Product", "unit_price"),
                       description="Product list price, looked up along the relationship."),))
        tables.append(t)
    net, completed = MeasureRef("Net Revenue"), MeasureRef("Completed Orders")
    measures = (
        Measure("Gross Revenue", "Orders", Agg("sum", Col("Orders", "order_amount")),
                format_string="$#,0.00"),
        Measure("Net Revenue", "Orders",
                IterAgg("sum", "Orders", If(COMPLETED, Col("Orders", "order_amount"))),
                format_string="$#,0.00", description="Revenue from completed orders."),
        Measure("Order Count", "Orders", Agg("count_rows", table="Orders")),
        Measure("Completed Orders", "Orders", IterAgg("count", "Orders", If(COMPLETED, Lit(1)))),
        Measure("AOV", "Orders", Divide(net, completed), format_string="$#,0.00",
                description="Average order value, completed orders."),
        Measure("Units", "Order Lines", Agg("sum", Col("Order Lines", "quantity"))),
        Measure("List Price Revenue", "Order Lines",
                IterAgg("sum", "Order Lines",
                        BinOp("*", Col("Order Lines", "quantity"),
                              Col("Order Lines", LIST_UNIT_PRICE)))),
    )
    return Model(base.name, tuple(tables), base.relationships, measures, null_semantics="sql",
                 description=base.description)


#: Visuals of executive_report() Sigma can compute, with their unsupported
#: values removed (Revenue LY, YoY Growth, Segment Share).
KEPT_VALUES = {"Revenue by Month": ("Net Revenue",), "Revenue by Segment": ("Net Revenue",)}


def executive_report() -> Report:
    pages = []
    for page in core.executive_report().pages:
        visuals = []
        for v in page.visuals:
            keep = KEPT_VALUES.get(v.name)
            if keep is not None:
                v = replace(v, values=tuple((lab, e) for lab, e in v.values if lab in keep))
            # Sigma shows every group its source has rows for (workbook.py).
            visuals.append(replace(v, groups="rows"))
        pages.append(Page(page.name, tuple(visuals)))
    return Report("Executive Overview", "Commerce", tuple(pages))


__all__ = ["commerce_model", "executive_report", "KEPT_VALUES", "Table", "Visual"]
