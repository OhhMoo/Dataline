"""What Sigma's REST API says about one workbook element: its columns and its SQL.

Two read endpoints of the Sigma Computing Public REST API (OpenAPI 2.0.0+
20260930.a3bda220, https://assets.sigmacomputing.com/openapi/public-rest-api/
sigma-computing-public-rest-api.json) describe an element rather than its data:

    GET /v2/workbooks/{workbookId}/elements/{elementId}/columns
        entries [{columnId, label, formula, type}]  ("The column's data type,
        represented as a recursive type object"; the example of the
        workbook-wide `/columns` spells it {"type": "number"}, {"type": "text"})
    GET /v2/workbooks/{workbookId}/elements/{elementId}/query
        {elementId, name?, sql?, error?}            ("the SQL query associated
        with a specific element"), and `/queries` lists the same per element

`element_columns` and `element_sql` answer them from the same lowering the
exports use (`workbook.element_visual`), so what the API says an element
computes is what its export computes:

* TYPES come from the semantic model: a grouping column is the warehouse
  column's type, a metric is its aggregate's (`expr_type`). Sigma's type names
  for a column are number, text, datetime and boolean (Sigma's own data types
  are Text, Number, Date, Logical and Variant - docs/data-types-and-formats;
  the API spells Date `datetime`, as the connection catalog here does). A
  column the engine cannot lower is `variant`: a type nobody computed is not
  invented.
* SQL is `world.semantic.compile`'s query for the element - the very query
  `render.render_visual` executes - in the connection's names: a warehouse
  table is `ANALYTICS.WAREHOUSE.FCT_ORDERS` (the path the data model and the
  connection catalog name it by, `_common.table_path`), never the engine's
  `wh` attachment; the output columns carry the element's column labels; and
  the element's sort and Top N, which `render.finish` applies after the query,
  are written into it as ORDER BY / LIMIT, so the statement alone returns the
  rows the element shows. The dialect is the engine's (DuckDB's, which is close
  to Snowflake's here); DuckDB's `AS MATERIALIZED` CTE hint, which only steers
  the engine's evaluation, is dropped.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import compile as C
from ...errors import SemanticError, Unsupported
from ...expr import (
    Agg,
    BinOp,
    Calculate,
    Coalesce,
    Col,
    Divide,
    Func,
    If,
    IterAgg,
    Lit,
    MeasureRef,
    Neg,
    Not,
    Related,
    TimeShift,
)
from ...model import Model
from ._common import table_path
from .datamodel import DataModelIndex
from .formula import Scope, lower, parse_ast
from .workbook import _IGNORED, _column_name, _label, element_visual

#: semantic DataType -> the name in Sigma's column type object.
SIGMA_TYPES = {"int": "number", "decimal": "number", "float": "number", "string": "text",
               "bool": "boolean", "date": "datetime", "timestamp": "datetime"}

_NUMBER_FUNCS = {"year", "quarter", "month", "day", "weekday", "len"}
_SAME_FUNCS = {"abs", "round", "floor", "ceiling"}
_TEXT_FUNCS = {"upper", "lower", "trim", "left", "right"}


def element_name(el: dict) -> str:
    """An element's title, which the representation spells as a string or as
    `{text}` (docs/workbook-representation-example-library)."""
    return _label(el.get("name")) or ""


def has_data(el: dict) -> bool:
    """Whether an element carries data at all - a text, image or container
    element runs no query and has no columns."""
    return el.get("kind") not in _IGNORED


# ------------------------------------------------------------------- types
def expr_type(expr, model: Model, _seen: frozenset = frozenset()) -> str | None:
    """The semantic DataType an expression evaluates to, or None when the
    expression alone does not say (a Lit(None), a measure cycle)."""
    def of(node):
        return expr_type(node, model, _seen)

    if isinstance(expr, Lit):
        value = expr.value
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, Decimal):
            return "decimal"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "string"
        if isinstance(value, dt.datetime):
            return "timestamp"
        if isinstance(value, dt.date):
            return "date"
        return None
    if isinstance(expr, Col | Related):
        for table in model.tables:
            if table.name == expr.table:
                for column in table.columns:
                    if column.name == expr.column:
                        return column.data_type
        return None
    if isinstance(expr, MeasureRef):
        if expr.name in _seen:
            return None
        for measure in model.measures:
            if measure.name == expr.name:
                return expr_type(measure.expr, model, _seen | {expr.name})
        return None
    if isinstance(expr, BinOp):
        if expr.op == "&":
            return "string"
        if expr.op in ("=", "<>", "<", ">", "<=", ">=", "and", "or"):
            return "bool"
        if expr.op == "/":
            return "float"
        return of(expr.left) or of(expr.right)
    if isinstance(expr, Neg):
        return of(expr.arg)
    if isinstance(expr, Not):
        return "bool"
    if isinstance(expr, Divide):
        return "float"
    if isinstance(expr, If):
        return of(expr.then) or (of(expr.else_) if expr.else_ is not None else None)
    if isinstance(expr, Coalesce):
        return next((t for t in (of(a) for a in expr.args) if t), None)
    if isinstance(expr, Func):
        if expr.name in _NUMBER_FUNCS:
            return "int"
        if expr.name.startswith("date_trunc_"):
            return "date"
        if expr.name in _TEXT_FUNCS:
            return "string"
        if expr.name in _SAME_FUNCS and expr.args:
            return of(expr.args[0])
        return None
    if isinstance(expr, Agg):
        if expr.func in ("count", "count_distinct", "count_rows"):
            return "int"
        inner = of(expr.arg) if expr.arg is not None else None
        return inner if expr.func in ("min", "max") else (
            "float" if expr.func == "avg" else inner or "decimal")
    if isinstance(expr, IterAgg):
        if expr.func == "count":
            return "int"
        inner = of(expr.expr)
        return inner if expr.func in ("min", "max") else (
            "float" if expr.func == "avg" else inner or "decimal")
    if isinstance(expr, Calculate | TimeShift):
        return of(expr.expr)
    return None


def _scope(el: dict, model: Model, index: DataModelIndex) -> Scope | None:
    """The scope `element_visual` lowers this element's formulas in, or None for
    an element that is not built on this data model."""
    source = el.get("source") or {}
    if source.get("kind") != "data-model" or source.get("dataModelId") != index.data_model_id:
        return None
    base = index.element_table.get(source.get("elementId"))
    if base is None:
        return None
    names = {}
    for column in el.get("columns", []):
        try:
            names.setdefault(_column_name(column), parse_ast(column["formula"]))
        except (SemanticError, KeyError):
            continue
    return Scope(model, "workbook", base, index.relationship_names, names,
                 metric_names=frozenset(index.metrics.get(base, set())))


def column_type(el: dict, column: dict, model: Model, index: DataModelIndex) -> dict:
    """One element column's type object: `{"type": "number"}` and the like."""
    found = None
    if _scope(el, model, index) is not None:
        for level in ("dimension", "measure"):
            try:
                # A fresh scope a try: lowering records the columns it is resolving.
                expr = lower(parse_ast(column["formula"]), _scope(el, model, index), level)
            except (SemanticError, KeyError):
                continue
            found = expr_type(expr, model)
            break
    return {"type": SIGMA_TYPES.get(found, "variant")}


def element_columns(el: dict, model: Model | None,
                    index: DataModelIndex | None) -> list[dict]:
    """`GET .../elements/{elementId}/columns` entries: {columnId, label, formula,
    type}, in the element's column order, hidden columns included (they are
    columns of the element; `hidden` is how it is displayed). `model` None (a
    data model that does not parse) types every column `variant`."""
    out = []
    for column in el.get("columns", []):
        kind = (column_type(el, column, model, index) if model is not None and
                index is not None else {"type": "variant"})
        out.append({"columnId": column["id"], "label": _column_name(column),
                    "formula": column.get("formula", ""), "type": kind})
    return out


# --------------------------------------------------------------------- SQL
def element_sql(el: dict, model: Model, index: DataModelIndex) -> str:
    """The statement an element runs (the module docstring says how it is
    named and ordered). Raises SemanticError for an element the engine cannot
    compute - the caller reports that as the query's `error`."""
    if not has_data(el):
        raise Unsupported(f"{el.get('kind')} element", "it runs no query")
    visual = element_visual(el, model, index)
    graph = C.check_model(model)
    C.check_visual(graph, visual)
    compiled = C.compile_visual(graph, visual)
    sql = compiled.sql
    for table in model.tables:
        if table.source and table.source_sql is None:
            try:
                named = ".".join(table_path(table.source))
            except SemanticError:
                named = table.source.upper()
            sql = sql.replace(C.source_sql(table), named)
    sql = sql.replace(" AS MATERIALIZED (", " AS (")
    k = compiled.n_groups
    inner = [f'q."g{i}"' for i in range(k)] + \
            [f'q."v{i}"' for i in range(len(compiled.labels) - k)]
    select = ", ".join(f"{source} AS {C.qident(label)}"
                       for source, label in zip(inner, compiled.labels, strict=True))
    # render.finish: the element's sort keys, then the grouping columns
    # ascending as the tie-break; a blank sorts lowest (first ascending, last
    # descending); Top N keeps the first n rows of that order.
    order = [f"{C.qident(label)} {'DESC NULLS LAST' if desc else 'ASC NULLS FIRST'}"
             for label, desc in visual.order_by]
    order += [f"{C.qident(label)} ASC NULLS FIRST" for label in compiled.labels[:k]]
    statement = f"SELECT {select} FROM ({sql}) q"
    if order:
        statement += " ORDER BY " + ", ".join(order)
    if visual.top_n is not None:
        statement += f" LIMIT {int(visual.top_n)}"
    return statement


__all__ = ["SIGMA_TYPES", "column_type", "element_columns", "element_name", "element_sql",
           "expr_type", "has_data"]
