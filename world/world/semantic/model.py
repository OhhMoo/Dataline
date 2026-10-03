"""The semantic model: THE CONTRACT every front-end and the engine share.

Names are MODEL names (what a report author sees: "Orders", "Net Revenue"), never
warehouse names; a Table maps to a warehouse relation (or a native SQL query) and a
Column to a warehouse column. Measure names are unique across the whole model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .expr import Col, Expr, FilterCompare, FilterValues

DataType = Literal["int", "decimal", "float", "string", "bool", "date", "timestamp"]


@dataclass(frozen=True)
class Column:
    name: str
    #: The warehouse column. Ignored for a calculated column (expr is set).
    source: str
    data_type: DataType
    hidden: bool = False
    #: A calculated column: a row-level expression over this table's row (Col of
    #: this table, Related columns). None for a column read from the warehouse.
    expr: Expr | None = None
    description: str = ""


@dataclass(frozen=True)
class Table:
    name: str
    #: "schema.relation" in the warehouse, e.g. "warehouse.fct_orders". Exactly one
    #: of source / source_sql is set.
    source: str | None
    columns: tuple[Column, ...]
    #: A native query instead of a relation (Power BI Value.NativeQuery, Tableau
    #: custom SQL, Sigma custom SQL, a Tableau physical join lowered to SQL). Its
    #: output columns are the Column.source names.
    source_sql: str | None = None
    is_date_table: bool = False
    #: The date column of a date table (required when is_date_table).
    date_column: str | None = None
    hidden: bool = False
    description: str = ""


@dataclass(frozen=True)
class Relationship:
    """from (many side) -> to (one side)."""
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: Literal["many_to_one", "one_to_one"] = "many_to_one"
    cross_filter: Literal["single", "both"] = "single"
    active: bool = True


@dataclass(frozen=True)
class Measure:
    name: str
    #: The home table: where the measure is displayed. Display only - evaluation
    #: follows the expression, not the home table.
    table: str
    expr: Expr
    format_string: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Model:
    name: str
    tables: tuple[Table, ...]
    relationships: tuple[Relationship, ...] = ()
    measures: tuple[Measure, ...] = ()
    #: How arithmetic treats None (expr.py "Blank"): "dax" for Power BI, "sql" for
    #: Tableau, Sigma and the YAML semantic layer.
    null_semantics: Literal["dax", "sql"] = "sql"
    description: str = ""


@dataclass(frozen=True)
class Visual:
    """Measures by dimensions: one table on a page, or one card.

    A legacy SQL panel (world/bi.py dashboards) sets `sql` and nothing else; its
    rows are the query's rows, unchanged.
    """
    name: str
    group_by: tuple[Col, ...] = ()
    #: (label, expression) pairs; the expression is usually a MeasureRef.
    values: tuple[tuple[str, Expr], ...] = ()
    filters: tuple[FilterValues | FilterCompare, ...] = ()
    #: (label, descending) pairs over group_by column names and value labels.
    #: Empty: ascending by the group_by columns, in order.
    order_by: tuple[tuple[str, bool], ...] = ()
    top_n: int | None = None
    #: The vendor's visual kind ("card", "table", "lineChart", "barChart", ...),
    #: carried for round-trips and display. Does not change the values.
    kind: str = "table"
    sql: str | None = None


@dataclass(frozen=True)
class Page:
    name: str
    visuals: tuple[Visual, ...]


@dataclass(frozen=True)
class Report:
    name: str
    model: str
    pages: tuple[Page, ...] = field(default_factory=tuple)


__all__ = ["Col", "Column", "DataType", "Measure", "Model", "Page", "Relationship",
           "Report", "Table", "Visual"]
