"""The expression tree every vendor language lowers into.

SEMANTICS (the engine implements exactly this; front-ends lower into exactly this)
================================================================================

Filter context. A mapping from model columns to predicates. Evaluating an
aggregate whose base table is T reads T's EXPANDED table: T joined to every table
reachable from T by following active many-to-one relationships from the many side
(Orders -> Customer, Orders -> Date; Order Lines -> Orders -> Customer). A
predicate filters T when its column belongs to T's expanded table. A predicate on a
table T does not reach (a fact column, when evaluating a dimension measure) does
NOT filter T: relationships filter from the one side to the many side only. This is
DAX's behaviour on a star schema with single-direction relationships, and it is the
only behaviour supported: a relationship with cross_filter="both" raises
Unsupported("bidirectional cross-filter") when an evaluation would traverse it.

Visual grouping. Each group of a visual adds `column = value` predicates for its
grouping columns. Visual filters (Visual.filters) are added first. Both intersect
with whatever is already there.

Modifiers (Calculate.modifiers, Visual.filters), applied in order:
  FilterValues / FilterCompare  CALCULATE overwrite semantics: REPLACE every
                                predicate on that column with this one. Inside
                                KeepFilters, INTERSECT instead.
  RemoveFilters(tables=(T,))    remove every predicate on a column of T's EXPANDED
                                table (DAX ALL(T) / REMOVEFILTERS(T): removing
                                filters on Orders also removes those on Customer).
  RemoveFilters(columns=(c,))   remove predicates on exactly those columns.
  RemoveFilters()               (no tables, no columns) remove every predicate.
  KeepOnly(T, columns)          ALLEXCEPT(T, cols): remove every predicate on T's
                                expanded table except those on `columns`.
In a Visual, only FilterValues and FilterCompare are allowed as filters.

Blank. An aggregate over no rows is None (BLANK), including count and count_rows.
Arithmetic follows Model.null_semantics:
  "sql"  any None operand -> None (Tableau, Sigma, SQL); `and` / `or` follow SQL
         three-valued logic (FALSE and None = FALSE, TRUE or None = TRUE).
  "dax"  + and -: None counts as 0 if the other operand is not None, else None;
         * : None if either is None; / : None / None = None, None / x = None (x not
         0), otherwise refused when the denominator is None or 0 (DAX returns
         infinity/NaN - use Divide); comparisons with None, None in and/or/not,
         in `&` or in a scalar function: refused.
In both modes, `/` by a non-None 0 is refused (use Divide). count and
count_distinct never count None, and are None (BLANK) over no rows.
Divide(num, den, alternate) returns `alternate` (default None) when den is None or 0.
If with a None condition takes the else branch.

Row context. Only IterAgg opens one: its `expr` is evaluated per row of `table`,
may read that table's columns (Col) and columns of tables it reaches many-to-one
(Related). A MeasureRef or Calculate inside an IterAgg would need context
transition: refused, Unsupported("context transition").

Time intelligence. TimeShift evaluates `expr` with the filter on the date table
replaced: the dates currently in context are mapped (ytd: Jan 1 of the latest
year in context through the latest date; mtd/qtd likewise; same_period_last_year:
each date minus one year (Feb 29 -> Feb 28), and when the dates in context include
a month's last day, the whole shifted month (DAX DATEADD: Feb 1-28 2025 -> Feb 1-29
2024); previous_month / previous_year: the whole calendar month
or year before the earliest date in context). `date_column` must be the date
column of a Table with is_date_table=True.

All nodes are frozen dataclasses, so trees compare by value and hash.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

AggFunc = Literal["sum", "avg", "min", "max", "count", "count_distinct", "count_rows"]
IterFunc = Literal["sum", "avg", "min", "max", "count"]
BinaryOp = Literal["+", "-", "*", "/", "=", "<>", "<", ">", "<=", ">=", "and", "or", "&"]
CompareOp = Literal["=", "<>", "<", ">", "<=", ">="]
#: The closed set of scalar functions. Anything else is Unsupported at parse time.
#: date_trunc_* return the first day of the period as a date; date_trunc_week
#: starts weeks on Monday (ISO 8601). weekday is 1 = Sunday .. 7 = Saturday (DAX
#: WEEKDAY's default and Tableau's US-locale DATEPART). len counts characters.
ScalarFunc = Literal[
    "abs", "round", "floor", "ceiling",
    "year", "quarter", "month", "day", "weekday",
    "date_trunc_week", "date_trunc_month", "date_trunc_quarter", "date_trunc_year",
    "upper", "lower", "len", "trim", "left", "right",
]
TimeKind = Literal["ytd", "qtd", "mtd", "same_period_last_year",
                   "previous_month", "previous_year"]


# --------------------------------------------------------------------- scalars
@dataclass(frozen=True)
class Lit:
    """int, decimal.Decimal, float, str, bool, datetime.date or None (BLANK)."""
    value: Any


@dataclass(frozen=True)
class Col:
    """A model column, by model table name and model column name (not warehouse names)."""
    table: str
    column: str


@dataclass(frozen=True)
class MeasureRef:
    name: str


@dataclass(frozen=True)
class Related:
    """A column of a table the current IterAgg row reaches many-to-one (DAX RELATED)."""
    table: str
    column: str


@dataclass(frozen=True)
class BinOp:
    op: BinaryOp
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Neg:
    arg: Expr


@dataclass(frozen=True)
class Not:
    arg: Expr


@dataclass(frozen=True)
class Divide:
    num: Expr
    den: Expr
    alternate: Expr | None = None


@dataclass(frozen=True)
class If:
    cond: Expr
    then: Expr
    else_: Expr | None = None


@dataclass(frozen=True)
class Coalesce:
    args: tuple[Expr, ...]


@dataclass(frozen=True)
class Func:
    name: ScalarFunc
    args: tuple[Expr, ...]


# ----------------------------------------------------------------- aggregates
@dataclass(frozen=True)
class Agg:
    """Aggregate a column over the current filter context.

    count_rows takes `table` and no `arg`; every other func takes `arg` (a Col) and
    its base table is the arg's table. count counts non-None values.
    """
    func: AggFunc
    arg: Col | None = None
    table: str | None = None


@dataclass(frozen=True)
class IterAgg:
    """SUMX-style: evaluate `expr` per row of `table` (row context), then aggregate."""
    func: IterFunc
    table: str
    expr: Expr


# ------------------------------------------------------------------- filters
@dataclass(frozen=True)
class FilterValues:
    column: Col
    values: tuple[Any, ...]
    negate: bool = False


@dataclass(frozen=True)
class FilterCompare:
    column: Col
    op: CompareOp
    value: Any


@dataclass(frozen=True)
class RemoveFilters:
    tables: tuple[str, ...] = ()
    columns: tuple[Col, ...] = ()


@dataclass(frozen=True)
class KeepOnly:
    table: str
    columns: tuple[Col, ...]


@dataclass(frozen=True)
class KeepFilters:
    modifier: FilterValues | FilterCompare


Modifier = FilterValues | FilterCompare | RemoveFilters | KeepOnly | KeepFilters


@dataclass(frozen=True)
class Calculate:
    expr: Expr
    modifiers: tuple[Modifier, ...]


@dataclass(frozen=True)
class TimeShift:
    expr: Expr
    date_column: Col
    kind: TimeKind


Expr = (Lit | Col | MeasureRef | Related | BinOp | Neg | Not | Divide | If | Coalesce | Func
        | Agg | IterAgg | Calculate | TimeShift)
