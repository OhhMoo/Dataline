"""Expression + filter context -> SQL: the semantic engine's compiler.

render.py asks for one visual at a time; this module turns it into ONE DuckDB
query (plus a few pre-flight checks) and never touches a connection itself.
What the query computes is exactly expr.py's SEMANTICS; how is below.

WHY ONE BATCHED QUERY, NOT ONE QUERY PER GROUP
----------------------------------------------
The obvious engine evaluates every group of a visual on its own: build the
group's filter context, run one aggregate query per measure. That is
quadratic in practice (a by-day visual over three years is 1 000 groups x
every aggregate) and the north-star world is a warehouse of ~50k facts. So the
filter context is kept SYMBOLIC instead: a predicate is either a constant
(FilterValues / FilterCompare), "equal to this group's value of grouping column
i" (GroupEq), or "inside this group's shifted date range / date set"
(DateRange / DateSet, produced by TimeShift). Modifiers transform a symbolic
context exactly as they would a concrete one - they are not data dependent -
so every aggregate under every context becomes one CTE that yields
(gid, value) for ALL groups at once, and the visual is one SELECT over the
group table `g` joined to those CTEs. The number of queries does not depend on
the number of groups.

The query, in order:

    WITH "t:<Table>"   one per model table used: warehouse columns renamed to
                       model names, calculated columns computed (row level)
         g             the candidate groups (gid, g0..gk)
         d<n>          per-group date ranges / date sets for TimeShift
         a<n>          one aggregate under one context, per group
    SELECT g0..gk, <value expressions over a<n>.v> FROM g LEFT JOIN a<n> ...

Three shapes of aggregate CTE, picked by which predicates the (relevant part of
the) context holds:
  (a) constants only      -> one scalar subquery, broadcast to every gid;
  (b) + group equalities  -> GROUP BY the grouping columns over the fact,
                             joined back to g on them (no g x fact blow-up);
  (c) + date ranges/sets  -> g x fact joined on the per-gid date CTE.

Measure-level arithmetic is SQL too (CASE expressions over the a<n>.v columns),
for two reasons. It keeps DuckDB's own numeric types end to end, so a value the
engine prints is bit-for-bit what a hand-written DuckDB query prints (decimal
arithmetic stays exact, decimal / integer is DOUBLE). And CASE evaluates
lazily per row, which is what makes Divide / If exact: a refused division in
the branch a group does not take is never evaluated for that group.

BLANK AND REFUSALS IN SQL
-------------------------
"sql" null semantics is SQL's NULL propagation, nearly for free. "dax" needs
rewriting (expr.py "Blank"): `+`/`-` become list_sum([a, b]) / list_sum([a, -b])
(list_sum skips NULLs and is NULL only when every element is - exactly DAX's
rule, with each operand written once, so nested sums do not blow up), and the
refused cases (division by BLANK or zero, a comparison with BLANK) become
DuckDB `error()` calls inside COALESCE / CASE, which DuckDB evaluates lazily.
`error()` raises with a marker render.py turns back into errors.Unsupported.

Pinned by expr.py and implemented here: "sql" `and` / `or` are SQL's
three-valued logic (FALSE and BLANK = FALSE, TRUE or BLANK = TRUE); weekday is
1 = Sunday .. 7 = Saturday; date_trunc_week starts weeks on Monday.

Decisions the contract leaves open, taken here (see the report that shipped
this module for the proposed contract text):
  - `/` with a zero denominator is refused under BOTH semantics. SQL engines
    disagree (DuckDB: inf, Postgres / Snowflake: error, Tableau: NULL), so no
    single answer is "the vendor's"; Divide is the node that defines it.
  - "sql" `/` by NULL is NULL; "dax" BLANK / x (x non-zero) is BLANK.
  - Under "dax" a BLANK operand of `and` / `or` / `not`, of `&`, or of a scalar
    function is refused (DAX's answers - BLANK && TRUE = FALSE, BLANK & "x" =
    "x", YEAR(BLANK) = 1899 - are not in the contract).
  - FilterValues is LIST membership: None in `values` matches NULL, and a
    negated list keeps NULL unless None is listed (a filter pane's "exclude").
    FilterCompare is SQL comparison (NULL never passes). Under "dax", where
    BLANK compares as 0 / "" / FALSE, a FilterCompare - and a FilterValues
    listing 0, "" or FALSE - is refused when the filtered column holds a blank
    (a pre-flight check render runs; the blank row counts).
  - trim strips leading/trailing spaces only; len counts characters.
  - count / count_distinct over rows that are all NULL is 0 (there ARE rows);
    count_distinct never counts NULL (SQL COUNT(DISTINCT), not DAX DISTINCTCOUNT).
  - Every aggregate referenced by a visual is computed for every group, so a
    refusal raised INSIDE an aggregate (row-level dax division in an iterator,
    say) refuses the visual even if an If would not have used that group's value.
    Over-refusing is loud; it is never a wrong number.
  - Values take DuckDB's types end to end: an If / Divide whose branches differ
    in type is unified by DuckDB (Divide(x, 0, 1) is the DOUBLE 1.0), and
    branches that cannot unify (a number and a text) are a ModelError.

GROUP CANDIDATES (Visual.groups)
--------------------------------
A visual's candidate groups are the distinct combinations of its group_by
columns over its BASE table's expanded rows, under the visual filters that reach
that table. Relationships are joined with LEFT JOIN, so a row whose key matches
nothing contributes a group with NULL dimension columns.

  "measures" (Power BI). The base is the table whose expanded table holds every
     grouping column and holds the least - grouping by Customer columns it is
     Customer (every segment, whatever the facts say), by Customer x Date
     columns it is Orders (the combinations that occur); should two tie, the
     union of theirs. Plus the base's BLANK ROW when some many-side row points
     at no row of it (an unmatched or NULL key: Power BI adds that row so those
     facts stay visible, as a blank group). render.py then drops the groups
     whose every value is blank (auto-exist).
  "rows" (Tableau, Sigma). The base is the table the visual's VALUES aggregate
     over (MeasureRefs followed to their Agg / IterAgg) - a Sigma element's
     source, a Tableau sheet's data source - so a group exists only if that
     table has rows for it after the filters (a status filter that empties a
     region removes the region). Values over two tables: Unsupported("groups
     over more than one source table"). Every candidate group is shown, blank
     or not. Values with no aggregate at all fall back to the "measures" base.

A relationship's one side must be unique (render checks it and refuses with
Power BI's refresh error), because a LEFT JOIN on a duplicated key would fan the
facts out silently. A native-SQL table that fans out INSIDE its own query is a
different thing: that is the table, and it is aggregated as it stands.
"""
from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from .errors import ModelError, SemanticError, Unsupported
from .expr import (
    Agg,
    BinOp,
    Calculate,
    Coalesce,
    Col,
    Divide,
    FilterCompare,
    FilterValues,
    Func,
    If,
    IterAgg,
    KeepFilters,
    KeepOnly,
    Lit,
    MeasureRef,
    Neg,
    Not,
    Related,
    RemoveFilters,
    TimeShift,
)
from .model import Column, Model, Relationship, Report, Table, Visual

# ------------------------------------------------------------------ constants
AGG_FUNCS = ("sum", "avg", "min", "max", "count", "count_distinct", "count_rows")
ITER_FUNCS = ("sum", "avg", "min", "max", "count")
ARITH = ("+", "-", "*", "/")
COMPARE = ("=", "<>", "<", ">", "<=", ">=")
LOGIC = ("and", "or")
TIME_KINDS = ("ytd", "qtd", "mtd", "same_period_last_year", "previous_month", "previous_year")
DATA_TYPES = ("int", "decimal", "float", "string", "bool", "date", "timestamp")

#: name -> (min args, max args, SQL template over {0}, {1}); the closed set of
#: expr.ScalarFunc. Templates are DuckDB; the comments in the module docstring
#: say which vendor convention each follows where the vendors differ.
FUNCS: dict[str, tuple[int, int, str]] = {
    "abs": (1, 1, "abs({0})"),
    "round": (1, 2, "round({0}, {1})"),
    "floor": (1, 1, "floor({0})"),
    "ceiling": (1, 1, "ceil({0})"),
    "year": (1, 1, "year({0})"),
    "quarter": (1, 1, "quarter({0})"),
    "month": (1, 1, "month({0})"),
    "day": (1, 1, "day({0})"),
    "weekday": (1, 1, "(dayofweek({0}) + 1)"),
    "date_trunc_week": (1, 1, "CAST(date_trunc('week', {0}) AS DATE)"),
    "date_trunc_month": (1, 1, "CAST(date_trunc('month', {0}) AS DATE)"),
    "date_trunc_quarter": (1, 1, "CAST(date_trunc('quarter', {0}) AS DATE)"),
    "date_trunc_year": (1, 1, "CAST(date_trunc('year', {0}) AS DATE)"),
    "upper": (1, 1, "upper({0})"),
    "lower": (1, 1, "lower({0})"),
    "len": (1, 1, "length({0})"),
    "trim": (1, 1, "trim({0})"),
    "left": (2, 2, "left({0}, {1})"),
    "right": (2, 2, "right({0}, {1})"),
}
TEXT_FUNCS = ("upper", "lower", "trim", "left", "right")
#: The SQL type of each argument, for a literal BLANK argument under "sql".
FUNC_ARG_TYPES: dict[str, tuple[str, ...]] = {
    **{f: ("DOUBLE", "INTEGER") for f in ("abs", "round", "floor", "ceiling")},
    **{f: ("DATE",) for f in ("year", "quarter", "month", "day", "weekday", "date_trunc_week",
                              "date_trunc_month", "date_trunc_quarter", "date_trunc_year")},
    **{f: ("VARCHAR", "INTEGER") for f in ("upper", "lower", "len", "trim", "left", "right")},
}

#: Prefix of every DuckDB error() raised by compiled SQL; render.py parses it.
REFUSE_MARKER = "semantic-unsupported"


# ------------------------------------------------------------- SQL spelling
def qident(name: str) -> str:
    """A double-quoted identifier: model names carry spaces and capitals."""
    return '"' + str(name).replace('"', '""') + '"'


def sql_literal(value) -> str:
    """A Python value (Lit, filter values) as a typed DuckDB literal."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):                     # before int: bool is an int
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        if -(2 ** 63) <= value < 2 ** 63:
            return str(value)
        return f"CAST('{value}' AS HUGEINT)"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ModelError(f"literal {value!r} is not a finite decimal")
        text = format(value, "f")
        digits = text.lstrip("-")
        whole, _, frac = digits.partition(".")
        precision = max(len(whole.lstrip("0")) + len(frac), len(frac), 1)
        if precision > 38:
            raise ModelError(f"literal {value!r} has more than 38 digits")
        return f"CAST('{text}' AS DECIMAL({precision},{len(frac)}))"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ModelError(f"literal {value!r} is not finite")
        return f"CAST('{value!r}' AS DOUBLE)"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None:
            raise ModelError(f"literal {value!r}: time zones are not supported")
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, _dt.date):
        return f"DATE '{value.isoformat()}'"
    raise ModelError(f"unsupported literal {value!r} ({type(value).__name__})")


def refuse_sql(feature: str, vendor: str | None = None) -> str:
    """SQL that raises errors.Unsupported(feature, vendor) when DuckDB evaluates it."""
    text = f"{REFUSE_MARKER}[{feature}|{vendor or ''}]"
    return f"error({sql_literal(text)})"


def source_sql(table: Table) -> str:
    """The FROM item of a table: the warehouse relation through the `wh` alias
    (never the bare file catalog - world/README.md "catalog/schema collision"),
    or the native query, which render.py runs under `USE wh`."""
    if table.source_sql is not None:
        return f"({table.source_sql})"
    parts = table.source.split(".")
    if not 1 <= len(parts) <= 2 or not all(parts):
        raise ModelError(f"table {table.name!r}: source {table.source!r} is not 'schema.relation'")
    return "wh." + ".".join(qident(p) for p in parts)


# ------------------------------------------------------------------ the graph
class Graph:
    """The model, indexed, with its relationship topology.

    Expanded tables follow ACTIVE relationships many -> one (one_to_one is read
    from -> to, the same way). Filters flow one -> many along every active
    relationship and many -> one as well along cross_filter="both" ones; a
    predicate that could only reach an aggregate through such a "both" edge is
    the one case the engine refuses (bidirectional cross-filter).
    """

    def __init__(self, model: Model):
        self.model = model
        self.tables: dict[str, Table] = {}
        self.columns: dict[tuple[str, str], Column] = {}
        self.measures = {}
        for t in model.tables:
            if t.name in self.tables:
                raise ModelError(f"duplicate table {t.name!r}")
            self.tables[t.name] = t
            for c in t.columns:
                if (t.name, c.name) in self.columns:
                    raise ModelError(f"table {t.name!r}: duplicate column {c.name!r}")
                self.columns[(t.name, c.name)] = c
        for m in model.measures:
            if m.name in self.measures:
                raise ModelError(f"duplicate measure {m.name!r}")
            self.measures[m.name] = m
        for r in model.relationships:
            self._check_relationship(r)
        self.active = tuple(r for r in model.relationships if r.active)
        self.out: dict[str, list[Relationship]] = {t: [] for t in self.tables}
        for r in self.active:
            self.out[r.from_table].append(r)
        self._paths = {t: self._walk(t) for t in self.tables}

    # -- lookups ----------------------------------------------------------
    def table(self, name: str) -> Table:
        if name not in self.tables:
            raise ModelError(f"unknown table {name!r}")
        return self.tables[name]

    def column(self, col: Col) -> Column:
        self.table(col.table)
        if (col.table, col.column) not in self.columns:
            raise ModelError(f"unknown column {col.table!r}[{col.column!r}]")
        return self.columns[(col.table, col.column)]

    def measure(self, name: str):
        if name not in self.measures:
            raise ModelError(f"unknown measure {name!r}")
        return self.measures[name]

    # -- topology ---------------------------------------------------------
    def _check_relationship(self, r: Relationship) -> None:
        for t, c in ((r.from_table, r.from_column), (r.to_table, r.to_column)):
            if t not in self.tables:
                raise ModelError(f"relationship {_rel_name(r)}: unknown table {t!r}")
            if (t, c) not in self.columns:
                raise ModelError(f"relationship {_rel_name(r)}: unknown column {t!r}[{c!r}]")
        if r.from_table == r.to_table:
            raise ModelError(f"relationship {_rel_name(r)} relates a table to itself")
        if r.cardinality not in ("many_to_one", "one_to_one"):
            raise ModelError(f"relationship {_rel_name(r)}: cardinality {r.cardinality!r}")
        if r.cross_filter not in ("single", "both"):
            raise ModelError(f"relationship {_rel_name(r)}: cross_filter {r.cross_filter!r}")

    def _walk(self, start: str) -> dict[str, tuple[Relationship, ...]]:
        """Every table reachable many -> one from `start`, with THE path to it.

        Two distinct active paths to one table make the expanded table ill
        defined (which Customer row does an order line see?) - Power BI refuses
        to activate such a relationship, and so does this. A path back to the
        start is a cycle."""
        paths: dict[str, tuple[Relationship, ...]] = {start: ()}
        frontier = [start]
        while frontier:
            nxt = []
            for t in frontier:
                for r in self.out[t]:
                    path = paths[t] + (r,)
                    if r.to_table == start or r.to_table in {x.from_table for x in path}:
                        raise ModelError(f"relationship cycle through {_rel_name(r)}")
                    if r.to_table in paths:
                        raise ModelError(
                            f"ambiguous relationship paths from {start!r} to {r.to_table!r}: "
                            "make all but one inactive")
                    paths[r.to_table] = path
                    nxt.append(r.to_table)
            frontier = nxt
        return paths

    def expanded(self, table: str) -> frozenset[str]:
        return frozenset(self._paths[table])

    def path(self, src: str, dst: str) -> tuple[Relationship, ...]:
        return self._paths[src][dst]

    def needs_bidirectional(self, src: str, dst: str) -> bool:
        """True when a predicate on `src` could reach `dst` only through a
        cross_filter="both" relationship (callers know src is not in dst's
        expanded table, so any filter path must cross one)."""
        seen, frontier = {src}, [src]
        while frontier:
            nxt = []
            for t in frontier:
                for r in self.active:
                    hops = []
                    if r.to_table == t:
                        hops.append(r.from_table)          # one -> many: always
                    if r.from_table == t and r.cross_filter == "both":
                        hops.append(r.to_table)            # many -> one: "both" only
                    for h in hops:
                        if h == dst:
                            return True
                        if h not in seen:
                            seen.add(h)
                            nxt.append(h)
            frontier = nxt
        return False

    def date_table_of(self, col: Col) -> Table:
        """The date table whose date column `col` is (TimeShift's requirement)."""
        self.column(col)
        t = self.tables[col.table]
        if not t.is_date_table or t.date_column != col.column:
            raise ModelError(f"time intelligence over {col.table}[{col.column}]: not the date "
                             "column of a table marked is_date_table")
        return t


def _rel_name(r: Relationship) -> str:
    return f"{r.from_table}[{r.from_column}] -> {r.to_table}[{r.to_column}]"


# ------------------------------------------------------------------ validation
def check_model(model: Model) -> Graph:
    """Every ModelError a model can raise without touching data."""
    if model.null_semantics not in ("sql", "dax"):
        raise ModelError(f"null_semantics {model.null_semantics!r} is neither 'sql' nor 'dax'")
    g = Graph(model)
    for t in model.tables:
        if (t.source is None) == (t.source_sql is None):
            raise ModelError(f"table {t.name!r}: set exactly one of source / source_sql")
        if t.is_date_table:
            if t.date_column is None:
                raise ModelError(f"date table {t.name!r} has no date_column")
            dc = g.column(Col(t.name, t.date_column))
            if dc.data_type != "date":
                raise ModelError(f"date table {t.name!r}: date_column {t.date_column!r} is "
                                 f"{dc.data_type}, not date")
        elif t.date_column is not None:
            raise ModelError(f"table {t.name!r} sets date_column but is not a date table")
        for c in t.columns:
            if c.data_type not in DATA_TYPES:
                raise ModelError(f"column {t.name}[{c.name}]: data_type {c.data_type!r}")
            if c.expr is not None:
                _check_expr(g, c.expr, f"calculated column {t.name}[{c.name}]", t.name)
        _calc_cycles(g, t)
    for m in model.measures:
        g.table(m.table)
        _check_expr(g, m.expr, f"measure {m.name!r}", None)
    _measure_cycles(g)
    return g


def check_visual(g: Graph, visual: Visual) -> None:
    where = f"visual {visual.name!r}"
    if visual.sql is not None:
        if visual.group_by or visual.values or visual.filters or visual.order_by \
                or visual.top_n is not None:
            raise ModelError(f"{where}: a SQL visual sets `sql` and nothing else")
        return
    if visual.groups not in ("measures", "rows"):
        raise ModelError(f"{where}: groups {visual.groups!r} is neither 'measures' nor 'rows'")
    if not visual.group_by and not visual.values:
        raise ModelError(f"{where} has neither group_by nor values")
    labels = []
    for col in visual.group_by:
        if not isinstance(col, Col):
            raise ModelError(f"{where}: group_by {col!r} is not a Col")
        g.column(col)
        labels.append(col.column)
    if len(set(visual.group_by)) != len(visual.group_by):
        raise ModelError(f"{where}: a column is grouped twice")
    tables = {c.table for c in visual.group_by}
    if tables and not any(tables <= g.expanded(t) for t in g.tables):
        raise ModelError(f"{where}: no single table reaches all of {sorted(tables)} "
                         "(the group_by columns need one fact that relates to them all)")
    for label, expr in visual.values:
        if not isinstance(label, str):
            raise ModelError(f"{where}: value label {label!r} is not a string")
        labels.append(label)
        _check_expr(g, expr, f"{where} value {label!r}", None)
    if len(set(labels)) != len(labels):
        raise ModelError(f"{where}: duplicate column labels {labels}")
    for f in visual.filters:
        if not isinstance(f, (FilterValues, FilterCompare)):
            raise ModelError(f"{where}: filter {f!r} - only FilterValues and FilterCompare "
                             "are visual filters")
        _check_modifier(g, f, where)
    for item in visual.order_by:
        if not (isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bool)):
            raise ModelError(f"{where}: order_by item {item!r} is not (label, descending)")
        if item[0] not in labels:
            raise ModelError(f"{where}: order_by label {item[0]!r} is not one of {labels}")
    if visual.groups == "rows":
        tables = value_tables(g, visual)
        if len(tables) == 1:
            base = next(iter(tables))
            for c in visual.group_by:
                if c.table not in g.expanded(base):
                    raise ModelError(f"{where}: groups='rows' over {base!r}, which does not "
                                     f"reach the group_by column {c.table}[{c.column}]")
    if visual.top_n is not None and (not isinstance(visual.top_n, int)
                                     or isinstance(visual.top_n, bool) or visual.top_n < 1):
        raise ModelError(f"{where}: top_n {visual.top_n!r} is not a positive integer")


def value_tables(g: Graph, visual: Visual) -> set[str]:
    """The tables a visual's values aggregate over, MeasureRefs followed."""
    out: set[str] = set()

    def visit(e, stack):
        for n in walk(e):
            if isinstance(n, MeasureRef) and n.name not in stack:
                visit(g.measure(n.name).expr, stack + (n.name,))
            elif isinstance(n, Agg):
                out.add(n.table if n.func == "count_rows" else n.arg.table)
            elif isinstance(n, IterAgg):
                out.add(n.table)

    for _, e in visual.values:
        visit(e, ())
    return out


def check_report(g: Graph, report: Report) -> None:
    if report.model != g.model.name:
        raise ModelError(f"report {report.name!r} is for model {report.model!r}, "
                         f"not {g.model.name!r}")
    pages = [p.name for p in report.pages]
    if len(set(pages)) != len(pages):
        raise ModelError(f"report {report.name!r}: duplicate page names")
    for p in report.pages:
        names = [v.name for v in p.visuals]
        if len(set(names)) != len(names):
            raise ModelError(f"report {report.name!r} page {p.name!r}: duplicate visual names")
        for v in p.visuals:
            check_visual(g, v)


def _check_expr(g: Graph, e, where: str, row: str | None) -> None:
    """Names, arities and the measure/row-context split. `row` is the table whose
    row is current (an IterAgg's or a calculated column's), None at measure level.
    Constructs the ENGINE refuses (context transition, ...) are not ModelErrors:
    they are raised as Unsupported when a visual actually evaluates them."""
    rec = lambda x, r=row: _check_expr(g, x, where, r)  # noqa: E731
    if isinstance(e, Lit):
        sql_literal(e.value)
    elif isinstance(e, Col):
        g.column(e)
        if row is None:
            raise ModelError(f"{where}: column {e.table}[{e.column}] outside a row context "
                             "(aggregate it, or iterate the table)")
        if e.table != row:
            raise ModelError(f"{where}: column {e.table}[{e.column}] in the row context of "
                             f"{row!r} (use Related)")
    elif isinstance(e, Related):
        g.column(Col(e.table, e.column))
        if row is None:
            raise ModelError(f"{where}: Related({e.table}[{e.column}]) outside a row context")
        if e.table == row or e.table not in g.expanded(row):
            raise ModelError(f"{where}: {e.table!r} is not reachable many-to-one from {row!r}")
    elif isinstance(e, MeasureRef):
        g.measure(e.name)
    elif isinstance(e, BinOp):
        if e.op not in ARITH + COMPARE + LOGIC + ("&",):
            raise ModelError(f"{where}: unknown operator {e.op!r}")
        rec(e.left)
        rec(e.right)
    elif isinstance(e, (Neg, Not)):
        rec(e.arg)
    elif isinstance(e, Divide):
        rec(e.num)
        rec(e.den)
        if e.alternate is not None:
            rec(e.alternate)
    elif isinstance(e, If):
        rec(e.cond)
        rec(e.then)
        if e.else_ is not None:
            rec(e.else_)
    elif isinstance(e, Coalesce):
        if not e.args:
            raise ModelError(f"{where}: Coalesce with no arguments")
        for a in e.args:
            rec(a)
    elif isinstance(e, Func):
        if e.name not in FUNCS:
            raise ModelError(f"{where}: unknown function {e.name!r}")
        lo, hi, _ = FUNCS[e.name]
        if not lo <= len(e.args) <= hi:
            raise ModelError(f"{where}: {e.name} takes {lo}..{hi} arguments, got {len(e.args)}")
        for a in e.args:
            rec(a)
    elif isinstance(e, Agg):
        if e.func not in AGG_FUNCS:
            raise ModelError(f"{where}: unknown aggregate {e.func!r}")
        if e.func == "count_rows":
            if e.arg is not None or e.table is None:
                raise ModelError(f"{where}: count_rows takes a table and no argument")
            g.table(e.table)
        else:
            if not isinstance(e.arg, Col):
                raise ModelError(f"{where}: {e.func} takes a column argument")
            g.column(e.arg)
            if e.table is not None and e.table != e.arg.table:
                raise ModelError(f"{where}: {e.func} over {e.arg.table!r} names table {e.table!r}")
    elif isinstance(e, IterAgg):
        if e.func not in ITER_FUNCS:
            raise ModelError(f"{where}: unknown iterator function {e.func!r}")
        g.table(e.table)
        _check_expr(g, e.expr, where, e.table)
    elif isinstance(e, Calculate):
        rec(e.expr)
        for m in e.modifiers:
            _check_modifier(g, m, where)
    elif isinstance(e, TimeShift):
        if e.kind not in TIME_KINDS:
            raise ModelError(f"{where}: unknown time intelligence kind {e.kind!r}")
        if not isinstance(e.date_column, Col):
            raise ModelError(f"{where}: TimeShift.date_column is not a Col")
        g.date_table_of(e.date_column)
        rec(e.expr)
    else:
        raise ModelError(f"{where}: {e!r} is not an expression")


def _check_modifier(g: Graph, m, where: str) -> None:
    if isinstance(m, FilterValues):
        g.column(m.column)
        if not isinstance(m.values, tuple):
            raise ModelError(f"{where}: FilterValues.values must be a tuple, got {m.values!r}")
        for v in m.values:
            sql_literal(v)
    elif isinstance(m, FilterCompare):
        g.column(m.column)
        if m.op not in COMPARE:
            raise ModelError(f"{where}: unknown comparison {m.op!r}")
        sql_literal(m.value)
    elif isinstance(m, RemoveFilters):
        for t in m.tables:
            g.table(t)
        for c in m.columns:
            g.column(c)
    elif isinstance(m, KeepOnly):
        g.table(m.table)
        for c in m.columns:
            g.column(c)
            if c.table not in g.expanded(m.table):
                raise ModelError(f"{where}: KeepOnly({m.table!r}) keeps {c.table}[{c.column}], "
                                 f"which is not in {m.table!r}'s expanded table")
    elif isinstance(m, KeepFilters):
        if not isinstance(m.modifier, (FilterValues, FilterCompare)):
            raise ModelError(f"{where}: KeepFilters wraps {m.modifier!r}, not a filter")
        _check_modifier(g, m.modifier, where)
    else:
        raise ModelError(f"{where}: {m!r} is not a filter modifier")


def _children(e) -> list:
    if isinstance(e, BinOp):
        return [e.left, e.right]
    if isinstance(e, (Neg, Not)):
        return [e.arg]
    if isinstance(e, Divide):
        return [e.num, e.den] + ([e.alternate] if e.alternate is not None else [])
    if isinstance(e, If):
        return [e.cond, e.then] + ([e.else_] if e.else_ is not None else [])
    if isinstance(e, (Coalesce, Func)):
        return list(e.args)
    if isinstance(e, (IterAgg, Calculate, TimeShift)):
        return [e.expr]
    return []


def walk(e):
    """Every node of an expression tree, pre-order."""
    yield e
    for c in _children(e):
        yield from walk(c)


def _measure_cycles(g: Graph) -> None:
    deps = {name: [n.name for n in walk(m.expr) if isinstance(n, MeasureRef)]
            for name, m in g.measures.items()}
    _no_cycles(deps, "measure")


def _calc_cycles(g: Graph, t: Table) -> None:
    calc = {c.name for c in t.columns if c.expr is not None}
    deps = {c.name: [n.column for n in walk(c.expr) if isinstance(n, Col)
                     and n.table == t.name and n.column in calc]
            for c in t.columns if c.expr is not None}
    _no_cycles(deps, f"calculated column of {t.name!r}")


def _no_cycles(deps: dict[str, list[str]], what: str) -> None:
    state: dict[str, int] = {}

    def visit(n, stack):
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            raise ModelError(f"{what} reference cycle: {' -> '.join(stack + [n])}")
        state[n] = 1
        for d in deps.get(n, ()):
            visit(d, stack + [n])
        state[n] = 2

    for n in sorted(deps):
        visit(n, [])


# ------------------------------------------------------------- filter context
@dataclass(frozen=True)
class GroupEq:
    """The column equals the current group's value of grouping column `index`."""
    index: int


@dataclass(frozen=True)
class DateRange:
    """The column lies in [lo, hi] of the current group's row of CTE `cte`."""
    cte: str


@dataclass(frozen=True)
class DateSet:
    """The column is one of the current group's dates in CTE `cte` (distinct)."""
    cte: str


Pred = FilterValues | FilterCompare | GroupEq | DateRange | DateSet


def _col_key(c: Col) -> tuple[str, str]:
    return (c.table, c.column)


@dataclass(frozen=True)
class Ctx:
    """A symbolic filter context: (column, predicates) pairs, sorted, immutable,
    so equal contexts are equal keys (the aggregate memo relies on it).
    Predicates on one column intersect."""
    items: tuple[tuple[Col, tuple[Pred, ...]], ...] = ()

    @staticmethod
    def of(d: dict[Col, tuple[Pred, ...]]) -> Ctx:
        return Ctx(tuple(sorted(((c, p) for c, p in d.items() if p),
                                key=lambda cp: _col_key(cp[0]))))

    def as_dict(self) -> dict[Col, tuple[Pred, ...]]:
        return dict(self.items)

    def replace(self, col: Col, pred: Pred) -> Ctx:
        d = self.as_dict()
        d[col] = (pred,)
        return Ctx.of(d)

    def add(self, col: Col, pred: Pred) -> Ctx:
        d = self.as_dict()
        d[col] = d.get(col, ()) + (pred,)
        return Ctx.of(d)

    def drop(self, keep: Callable[[Col], bool]) -> Ctx:
        return Ctx(tuple((c, p) for c, p in self.items if keep(c)))


def apply_modifier(g: Graph, ctx: Ctx, m) -> Ctx:
    """One CALCULATE modifier (or visual filter), exactly as expr.py says."""
    if isinstance(m, (FilterValues, FilterCompare)):
        return ctx.replace(m.column, m)
    if isinstance(m, KeepFilters):
        return ctx.add(m.modifier.column, m.modifier)
    if isinstance(m, RemoveFilters):
        if not m.tables and not m.columns:
            return Ctx()
        gone_tables = set().union(*(g.expanded(t) for t in m.tables)) if m.tables else set()
        gone_cols = set(m.columns)
        return ctx.drop(lambda c: c.table not in gone_tables and c not in gone_cols)
    if isinstance(m, KeepOnly):
        exp, kept = g.expanded(m.table), set(m.columns)
        return ctx.drop(lambda c: c.table not in exp or c in kept)
    raise ModelError(f"{m!r} is not a filter modifier")


# ------------------------------------------------------------------ the output
@dataclass(frozen=True)
class Check:
    """A pre-flight query: when it returns a row, raise `error`."""
    sql: str
    error: SemanticError


@dataclass
class Compiled:
    sql: str
    labels: list[str]
    n_groups: int
    checks: list[Check] = field(default_factory=list)


# ------------------------------------------------------------ the compiler
class VisualCompiler:
    """One visual -> one query. Not reusable: create one per visual."""

    def __init__(self, g: Graph, visual: Visual):
        self.g = g
        self.model = g.model
        self.visual = visual
        self.dax = g.model.null_semantics == "dax"
        self.ctes: list[tuple[str, str]] = []          # (name, body) after g
        self.memo: dict = {}
        self.used_tables: set[str] = set()
        self.calc_needed: dict[str, set[str]] = {}     # table -> calc columns its CTE computes
        self.table_deps: dict[str, set[str]] = {}      # table CTE -> table CTEs it joins
        self.used_rels: dict[Relationship, None] = {}  # ordered set
        self.checks: dict[str, Check] = {}
        self.value_ctes: dict[str, None] = {}          # aggregate CTEs the SELECT joins

    # -- table CTEs ----------------------------------------------------------
    def table_ref(self, name: str) -> str:
        """The CTE holding table `name` in model names; marks it used."""
        if name not in self.used_tables:
            self.used_tables.add(name)
            self.calc_needed.setdefault(name, set())
            self.table_deps.setdefault(name, set())
        return qident(f"t:{name}")

    def need_column(self, col: Col) -> None:
        """Make the table CTE carry `col` (a calculated column is computed only
        when something reads it, and then for every row of its table)."""
        self.table_ref(col.table)
        c = self.g.column(col)
        if c.expr is not None and col.column not in self.calc_needed[col.table]:
            self.calc_needed[col.table].add(col.column)
            # resolve its dependencies now so the CTE order is known
            self._calc_sql(col.table, c, {}, set())

    def _calc_sql(self, table: str, column: Column, joins: dict, stack: set) -> str:
        """Row-level SQL of a calculated column inside table `table`'s CTE.

        `joins` collects (alias -> join SQL) for Related lookups; columns of the
        table itself are read from the warehouse relation (r0) by source name,
        calculated ones inlined (cycles were refused by check_model)."""
        if column.name in stack:
            raise ModelError(f"calculated column cycle at {table}[{column.name}]")
        stack = stack | {column.name}
        where = f"calculated column {table}[{column.name}]"

        def leaf(e):
            if isinstance(e, Col):
                c = self.g.column(e)
                if c.expr is not None:
                    return f"({self._calc_sql(table, c, joins, stack)})"
                return "r0." + qident(c.source)
            if isinstance(e, Related):
                alias = self._calc_join(table, e.table, joins)
                self.need_column(Col(e.table, e.column))
                return f"{alias}.{qident(e.column)}"
            if isinstance(e, (MeasureRef, Calculate, TimeShift)):
                raise Unsupported("context transition", None,
                                  f"{where} evaluates a measure / CALCULATE per row")
            if isinstance(e, (Agg, IterAgg)):
                raise Unsupported("aggregate in a calculated column", None, where)
            raise ModelError(f"{where}: {e!r} is not a row-level expression")

        return self.scalar(column.expr, leaf)

    def _calc_join(self, table: str, target: str, joins: dict) -> str:
        """Alias of `target` joined into `table`'s CTE along THE path."""
        prev = "r0"
        for r in self.g.path(table, target):
            if r not in joins:
                alias = f"j{len(joins)}"
                if prev == "r0":
                    fc = self.g.column(Col(r.from_table, r.from_column))
                    if fc.expr is not None:
                        raise Unsupported("relationship on a calculated column", None,
                                          f"{_rel_name(r)} feeds RELATED in a calculated column")
                    left = "r0." + qident(fc.source)
                else:
                    left = f"{prev}.{qident(r.from_column)}"
                    self.need_column(Col(r.from_table, r.from_column))
                self.need_column(Col(r.to_table, r.to_column))
                joins[r] = (alias, f"LEFT JOIN {self.table_ref(r.to_table)} {alias} "
                                   f"ON {left} = {alias}.{qident(r.to_column)}")
                self.table_deps[table].add(r.to_table)
                self.used_rels[r] = None
            prev = joins[r][0]
        return prev

    def _table_cte(self, name: str) -> str:
        t = self.g.tables[name]
        cols = [f"r0.{qident(c.source)} AS {qident(c.name)}" for c in t.columns if c.expr is None]
        joins: dict = {}
        for c in t.columns:
            if c.expr is not None and c.name in self.calc_needed[name]:
                cols.append(f"{self._calc_sql(name, c, joins, set())} AS {qident(c.name)}")
        body = f"SELECT {', '.join(cols) or 'NULL AS _'} FROM {source_sql(t)} r0"
        body += "".join(" " + j for _, j in joins.values())
        # A native query or a calculated column is materialised: the native
        # query runs once, and a calculated column is evaluated for EVERY row of
        # its table (as a model refresh would), never only for the rows a
        # pushed-down filter happens to leave - so a refusal inside it does not
        # depend on the optimizer.
        mat = " MATERIALIZED" if (joins or self.calc_needed[name] or t.source_sql) else ""
        return f"{qident('t:' + name)} AS{mat} ({body})"

    def table_ctes(self) -> list[str]:
        """Table CTEs, dependencies first, ties by name (deterministic SQL)."""
        out, done = [], set()

        def emit(n):
            if n in done:
                return
            done.add(n)
            for d in sorted(self.table_deps.get(n, ())):
                emit(d)
            out.append(self._table_cte(n))

        # computing a CTE may mark more tables used: iterate to a fixpoint
        while True:
            pending = sorted(self.used_tables - done)
            if not pending:
                break
            for n in pending:
                emit(n)
        return out

    # -- the expanded relation of an aggregate -------------------------------
    def expanded_from(self, base: str, cols) -> tuple[str, dict[Col, str]]:
        """FROM-item over `base`'s expanded table carrying `cols`: the base table
        LEFT JOINed along the unique path to each table a column lives on."""
        tables = {c.table for c in cols} | {base}
        rels: list[Relationship] = []
        for t in sorted(tables):
            for r in self.g.path(base, t):
                if r not in rels:
                    rels.append(r)
        rels.sort(key=lambda r: (len(self.g.path(base, r.to_table)), r.to_table))
        alias = {base: "r0"}
        joins = []
        for i, r in enumerate(rels, 1):
            alias[r.to_table] = f"r{i}"
            self.need_column(Col(r.from_table, r.from_column))
            self.need_column(Col(r.to_table, r.to_column))
            joins.append(f"LEFT JOIN {self.table_ref(r.to_table)} r{i} ON "
                         f"{alias[r.from_table]}.{qident(r.from_column)} = "
                         f"r{i}.{qident(r.to_column)}")
            self.used_rels[r] = None
        colmap, select = {}, []
        for i, c in enumerate(sorted(cols, key=_col_key)):
            self.need_column(c)
            colmap[c] = f'e."x{i}"'
            select.append(f"{alias[c.table]}.{qident(c.column)} AS \"x{i}\"")
        sql = (f"(SELECT {', '.join(select) or '1 AS x'} FROM {self.table_ref(base)} r0"
               + "".join(" " + j for j in joins) + ")")
        return sql, colmap

    def relevant(self, ctx: Ctx, base: str) -> Ctx:
        """The part of `ctx` that filters `base`: predicates on its expanded
        table. Anything else does not reach it (filters flow one -> many) -
        unless a cross_filter="both" relationship would carry it there."""
        exp = self.g.expanded(base)
        for c, _ in ctx.items:
            if c.table not in exp and self.g.needs_bidirectional(c.table, base):
                raise Unsupported("bidirectional cross-filter", None,
                                  f"a filter on {c.table!r} reaches {base!r} only through a "
                                  "relationship with cross_filter='both'")
        return ctx.drop(lambda c: c.table in exp)

    def pred_sql(self, x: str, p: Pred) -> str:
        if isinstance(p, FilterValues):
            parts = []
            nonnull = [v for v in p.values if v is not None]
            if nonnull:
                parts.append(f"COALESCE({x} IN ({', '.join(sql_literal(v) for v in nonnull)}), "
                             "FALSE)")
            if any(v is None for v in p.values):
                parts.append(f"{x} IS NULL")
            body = " OR ".join(parts) or "FALSE"
            return f"(NOT ({body}))" if p.negate else f"({body})"
        if isinstance(p, FilterCompare):
            if self.dax and p.value is None:
                raise Unsupported("comparison with BLANK", "DAX semantics",
                                  f"filter {p.column.table}[{p.column.column}] {p.op} BLANK")
            return f"({x} {p.op} {sql_literal(p.value)})"
        if isinstance(p, GroupEq):
            return f"{x} IS NOT DISTINCT FROM g.\"g{p.index}\""
        raise AssertionError(p)

    def _dax_compare_check(self, base: str, col: Col, feature: str = "comparison with BLANK",
                           blank: str | None = None) -> None:
        """Under "dax", a filter that DAX would evaluate against BLANK differently
        from SQL is refused when the filtered data holds a blank: FilterCompare
        (DAX compares BLANK as 0 / ""), and FilterValues listing 0, "" or FALSE
        (DAX's `=` matches BLANK to each). `blank` is the base table's blank-row
        test, for group candidates."""
        frm, cm = self.expanded_from(base, {col})
        why = f"a filter on {col.table}[{col.column}], which holds a blank"
        self.checks.setdefault(f"{feature}:{base}:{col}", Check(
            f"SELECT 1 FROM {frm} e WHERE {cm[col]} IS NULL LIMIT 1",
            Unsupported(feature, "DAX semantics", why)))
        if blank is not None:
            self.checks.setdefault(f"{feature}:blank:{base}:{col}", Check(
                f"SELECT 1 WHERE {blank}",
                Unsupported(feature, "DAX semantics", f"{base!r} has a blank row and {why}")))

    def _dax_filter_checks(self, base: str, col: Col, p, blank: str | None = None) -> None:
        if not self.dax:
            return
        if isinstance(p, FilterCompare):
            self._dax_compare_check(base, col, blank=blank)
        elif isinstance(p, FilterValues) and any(
                v is not None and (v == 0 or v == "") for v in p.values):
            self._dax_compare_check(base, col, 'BLANK matched by 0, "" or FALSE', blank)

    def over(self, base: str, ctx: Ctx, cols, value: Callable[[dict], str], mode: str,
             vkey, prefix: str = "a") -> str:
        """The CTE computing `value` over `base`'s expanded table under `ctx`,
        for every group: mode "agg" -> (gid, v); mode "set" -> distinct (gid, d).
        `vkey` identifies `value` for the memo: the same aggregate under the same
        relevant context is computed once per visual."""
        rctx = self.relevant(ctx, base)
        key = (mode, base, rctx, vkey)
        if key in self.memo:
            return self.memo[key]
        consts, eqs, dates = [], [], []
        for c, preds in rctx.items:
            for p in preds:
                if isinstance(p, (FilterValues, FilterCompare)):
                    consts.append((c, p))
                    self._dax_filter_checks(base, c, p)
                elif isinstance(p, GroupEq):
                    eqs.append((c, p))
                else:
                    dates.append((c, p))
        frm, cm = self.expanded_from(base, set(cols) | {c for c, _ in rctx.items})
        where = [self.pred_sql(cm[c], p) for c, p in consts]
        v = value(cm)
        if not eqs and not dates:                                       # shape (a)
            w = f" WHERE {' AND '.join(where)}" if where else ""
            if mode == "agg":
                body = f"SELECT g.gid, s.v FROM g, (SELECT {v} AS v FROM {frm} e{w}) s"
            else:
                body = f"SELECT g.gid, s.d FROM g, (SELECT DISTINCT {v} AS d FROM {frm} e{w}) s"
        elif not dates:                                                 # shape (b)
            keys = ", ".join(f"{cm[c]} AS k{i}" for i, (c, _) in enumerate(eqs))
            on = " AND ".join(f'g."g{p.index}" IS NOT DISTINCT FROM s.k{i}'
                              for i, (_, p) in enumerate(eqs))
            w = f" WHERE {' AND '.join(where)}" if where else ""
            if mode == "agg":
                grp = ", ".join(str(i + 1) for i in range(len(eqs)))
                body = (f"SELECT g.gid, s.v FROM g JOIN (SELECT {keys}, {v} AS v FROM {frm} e{w} "
                        f"GROUP BY {grp}) s ON {on}")
            else:
                body = (f"SELECT g.gid, s.d FROM g JOIN (SELECT DISTINCT {keys}, {v} AS d "
                        f"FROM {frm} e{w}) s ON {on}")
        else:                                                           # shape (c)
            joins = []
            for i, (c, p) in enumerate(dates):
                if isinstance(p, DateRange):
                    joins.append(f"JOIN {p.cte} q{i} ON q{i}.gid = g.gid "
                                 f"AND {cm[c]} BETWEEN q{i}.lo AND q{i}.hi")
                else:
                    joins.append(f"JOIN {p.cte} q{i} ON q{i}.gid = g.gid AND {cm[c]} = q{i}.d")
            where += [self.pred_sql(cm[c], p) for c, p in eqs]
            w = f" WHERE {' AND '.join(where)}" if where else ""
            body = f"FROM g CROSS JOIN {frm} e {' '.join(joins)}{w}"
            if mode == "agg":
                body = f"SELECT g.gid, {v} AS v {body} GROUP BY g.gid"
            else:
                body = f"SELECT DISTINCT g.gid, {v} AS d {body}"
        name = f"{prefix}{len(self.ctes)}"
        self.ctes.append((name, body))
        self.memo[key] = name
        return name

    # -- measure level -------------------------------------------------------
    def measure_sql(self, e, ctx: Ctx, stack: tuple[str, ...] = ()) -> str:
        """SQL of a measure-level expression under `ctx`, over g / a<n>."""
        def leaf(x):
            return self._measure_leaf(x, ctx, stack)
        return self.scalar(e, leaf)

    def _measure_leaf(self, e, ctx: Ctx, stack) -> str:
        if isinstance(e, MeasureRef):
            if e.name in stack:
                raise ModelError(f"measure reference cycle: {' -> '.join(stack + (e.name,))}")
            # A measure reference is an implicit CALCULATE; outside a row context
            # there is no row to transition, so it is evaluated in place.
            return f"({self.measure_sql(self.g.measure(e.name).expr, ctx, stack + (e.name,))})"
        if isinstance(e, Calculate):
            for m in e.modifiers:
                ctx = apply_modifier(self.g, ctx, m)
            return self.measure_sql(e.expr, ctx, stack)
        if isinstance(e, TimeShift):
            return self.measure_sql(e.expr, self.time_shift(e, ctx), stack)
        if isinstance(e, Agg):
            return self.aggregate(e, ctx)
        if isinstance(e, IterAgg):
            return self.iterate(e, ctx)
        if isinstance(e, (Col, Related)):
            raise ModelError(f"{e!r} outside a row context")
        raise ModelError(f"{e!r} is not an expression")

    def aggregate(self, e: Agg, ctx: Ctx) -> str:
        base = e.table if e.func == "count_rows" else e.arg.table
        cols = set() if e.arg is None else {e.arg}

        def value(cm):
            return agg_sql(e.func, None if e.arg is None else cm[e.arg])
        name = self.over(base, ctx, cols, value, "agg", e)
        self.value_ctes[name] = None
        return f"{name}.v"

    def iterate(self, e: IterAgg, ctx: Ctx) -> str:
        """SUMX-style: the row expression over the expanded table, aggregated."""
        cols = set()
        for n in walk(e.expr):
            if isinstance(n, (MeasureRef, Calculate, TimeShift)):
                raise Unsupported("context transition", None,
                                  f"a measure / CALCULATE inside an iterator over {e.table!r}")
            if isinstance(n, (Agg, IterAgg)):
                raise Unsupported("aggregate inside an iterator", None,
                                  f"an aggregate nested in an iterator over {e.table!r}")
            if isinstance(n, Col):
                cols.add(n)
            elif isinstance(n, Related):
                cols.add(Col(n.table, n.column))

        def value(cm):
            def leaf(x):
                if isinstance(x, Col):
                    return cm[x]
                if isinstance(x, Related):
                    return cm[Col(x.table, x.column)]
                raise ModelError(f"{x!r} in a row context")
            return agg_sql(e.func, self.scalar(e.expr, leaf))
        name = self.over(e.table, ctx, cols, value, "agg", e)
        self.value_ctes[name] = None
        return f"{name}.v"

    def time_shift(self, e: TimeShift, ctx: Ctx) -> Ctx:
        """Replace the date table's filter by the mapped dates of the context."""
        dt = self.g.date_table_of(e.date_column)
        dc = e.date_column
        self.checks.setdefault(f"date:{dt.name}", Check(
            f"SELECT 1 FROM (SELECT {qident(dc.column)} AS d, count(*) AS n FROM "
            f"{self.table_ref(dt.name)} GROUP BY 1) WHERE d IS NULL OR n > 1 LIMIT 1",
            ModelError(f"date table {dt.name!r}: {dc.column!r} must be unique and never blank")))
        if e.kind == "same_period_last_year":
            def shift(cm):
                return f"CAST({cm[dc]} - INTERVAL 1 YEAR AS DATE)"
            # NULL dates are refused by the check above, so no NULL d here
            cte = self.over(dt.name, ctx, {dc}, shift, "set", (e.kind, dc), "d")
            pred: Pred = DateSet(cte)
        else:
            func = "min" if e.kind.startswith("previous") else "max"

            def edge(cm, func=func):
                return f"{func}({cm[dc]})"
            m = self.over(dt.name, ctx, {dc}, edge, "agg", (func, dc), "m")
            lo, hi = {
                "ytd": ("make_date(year(v), 1, 1)", "v"),
                "qtd": ("CAST(date_trunc('quarter', v) AS DATE)", "v"),
                "mtd": ("CAST(date_trunc('month', v) AS DATE)", "v"),
                "previous_month": (
                    "CAST(date_trunc('month', v) - INTERVAL 1 MONTH AS DATE)",
                    "CAST(date_trunc('month', v) - INTERVAL 1 DAY AS DATE)"),
                "previous_year": ("make_date(year(v) - 1, 1, 1)", "make_date(year(v) - 1, 12, 31)"),
            }[e.kind]
            key = ("range", m, e.kind)
            if key not in self.memo:
                name = f"d{len(self.ctes)}"
                self.ctes.append((name, f"SELECT gid, {lo} AS lo, {hi} AS hi FROM {m} "
                                        "WHERE v IS NOT NULL"))
                self.memo[key] = name
            pred = DateRange(self.memo[key])
        exp = self.g.expanded(dt.name)
        return ctx.drop(lambda c: c.table not in exp).add(dc, pred)

    # -- scalars (both levels) -------------------------------------------------
    def scalar(self, e, leaf: Callable[[object], str]) -> str:
        """SQL of a scalar expression; `leaf` compiles what is not scalar
        (columns at row level, aggregates and measures at measure level)."""
        rec = lambda x: self.scalar(x, leaf)  # noqa: E731
        dax = self.dax
        if isinstance(e, Lit):
            return sql_literal(e.value)
        if isinstance(e, BinOp):
            a, b = rec(e.left), rec(e.right)
            if e.op in ("+", "-"):
                if dax:     # BLANK counts as 0 unless both are BLANK (list_sum skips NULLs)
                    return f"list_sum([{a}, {b if e.op == '+' else f'-({b})'}])"
                return f"({a} {e.op} {b})"
            if e.op == "*":
                return f"({a} * {b})"
            if e.op == "/":
                # The refusal wraps the whole division, not the denominator: a
                # constant NULL operand types the quotient as NULL and DuckDB folds
                # such an expression away without evaluating its arguments.
                if dax:
                    return (f"(CASE WHEN ({b}) IS NULL OR ({b}) = 0 THEN "
                            f"{refuse_sql('division by BLANK or zero', 'DAX semantics')} "
                            f"ELSE ({a}) / ({b}) END)")
                return (f"(CASE WHEN ({b}) = 0 THEN {refuse_sql('division by zero')} "
                        f"ELSE ({a}) / ({b}) END)")
            if e.op in COMPARE:
                if dax:
                    return (f"COALESCE(({a} {e.op} {b}), "
                            f"{refuse_sql('comparison with BLANK', 'DAX semantics')})")
                return f"({a} {e.op} {b})"
            if e.op in LOGIC:
                if dax:
                    no = refuse_sql("BLANK in a logical operator", "DAX semantics")
                    a, b = (f"COALESCE({x}, {no})" for x in (a, b))
                return f"({a} {e.op.upper()} {b})"
            if e.op == "&":
                for side in (e.left, e.right):
                    if self.type_of(side) != "string":
                        raise Unsupported("& on a non-text value", None,
                                          f"{side!r}: number and date formatting differ by vendor")
                if dax:
                    a, b = (f"COALESCE({x}, {refuse_sql('BLANK in &', 'DAX semantics')})"
                            for x in (a, b))
                return f"({a} || {b})"
            raise ModelError(f"unknown operator {e.op!r}")
        if isinstance(e, Neg):
            return f"(-({rec(e.arg)}))"
        if isinstance(e, Not):
            a = rec(e.arg)
            if dax:
                a = f"COALESCE({a}, {refuse_sql('BLANK in a logical operator', 'DAX semantics')})"
            return f"(NOT ({a}))"
        if isinstance(e, Divide):
            n, d = rec(e.num), rec(e.den)
            alt = "NULL" if e.alternate is None else rec(e.alternate)
            return f"(CASE WHEN ({d}) IS NULL OR ({d}) = 0 THEN {alt} ELSE ({n}) / ({d}) END)"
        if isinstance(e, If):
            other = "NULL" if e.else_ is None else rec(e.else_)
            return f"(CASE WHEN {rec(e.cond)} THEN {rec(e.then)} ELSE {other} END)"
        if isinstance(e, Coalesce):
            return f"COALESCE({', '.join(rec(a) for a in e.args)})"
        if isinstance(e, Func):
            lo, hi, template = FUNCS[e.name]
            args = []
            for i, a in enumerate(e.args):
                if isinstance(a, Lit) and a.value is None:
                    if dax:
                        raise Unsupported("BLANK in a function", "DAX semantics",
                                          f"{e.name}(BLANK)")
                    # an untyped NULL leaves DuckDB no overload to pick
                    args.append(f"CAST(NULL AS {FUNC_ARG_TYPES[e.name][i]})")
                    continue
                s = rec(a)
                if dax and not isinstance(a, Lit):
                    s = f"COALESCE({s}, {refuse_sql('BLANK in a function', 'DAX semantics')})"
                args.append(s)
            if e.name == "round" and len(args) == 1:
                return f"round({args[0]})"
            return template.format(*args)
        return leaf(e)

    def type_of(self, e) -> str | None:
        """A static type, as far as `&` needs one: "string" or not."""
        if isinstance(e, Lit):
            return "string" if isinstance(e.value, str) or e.value is None else "other"
        if isinstance(e, Col):
            return "string" if self.g.column(e).data_type == "string" else "other"
        if isinstance(e, Related):
            return self.type_of(Col(e.table, e.column))
        if isinstance(e, BinOp) and e.op == "&":
            return "string"
        if isinstance(e, Func):
            return "string" if e.name in TEXT_FUNCS else "other"
        if isinstance(e, Agg) and e.func in ("min", "max"):
            return self.type_of(e.arg)
        if isinstance(e, MeasureRef):
            return self.type_of(self.g.measure(e.name).expr)
        if isinstance(e, (Calculate, TimeShift)):
            return self.type_of(e.expr)
        if isinstance(e, If):
            kinds = {self.type_of(e.then)} | ({self.type_of(e.else_)} if e.else_ else set())
            return "string" if kinds == {"string"} else "other"
        if isinstance(e, Coalesce):
            return "string" if {self.type_of(a) for a in e.args} == {"string"} else "other"
        return "other"

    # -- groups ----------------------------------------------------------------
    def group_bases(self) -> tuple[list[str], bool]:
        """The visual's base table(s), and whether the blank row joins them
        (module docstring, GROUP CANDIDATES)."""
        if self.visual.groups == "rows":
            tables = value_tables(self.g, self.visual)
            if len(tables) > 1:
                raise Unsupported("groups over more than one source table", None,
                                  f"visual {self.visual.name!r} aggregates {sorted(tables)}; "
                                  "a Tableau sheet / Sigma element has one source")
            if tables:
                return sorted(tables), False
        need = {c.table for c in self.visual.group_by}
        fits = [t for t in sorted(self.g.tables) if need <= self.g.expanded(t)]
        return [t for t in fits
                if not any(self.g.expanded(o) < self.g.expanded(t) for o in fits)], True

    def groups_cte(self) -> str:
        """g(gid, g0..gk): the candidate groups (module docstring, GROUP CANDIDATES)."""
        v = self.visual
        if not v.group_by:
            return "g AS (SELECT 1 AS gid)"
        vctx = Ctx()
        for f in v.filters:
            vctx = vctx.add(f.column, f)
        k = len(v.group_by)
        parts = []
        bases, with_blank_row = self.group_bases()
        for t in bases:
            rctx = self.relevant(vctx, t)
            cols = set(v.group_by) | {c for c, _ in rctx.items}
            frm, cm = self.expanded_from(t, cols)
            preds = [(c, p) for c, ps in rctx.items for p in ps]
            blank = self.blank_row_sql(t) if with_blank_row else None
            for c, p in preds:
                self._dax_filter_checks(t, c, p, blank)
            where = " AND ".join(self.pred_sql(cm[c], p) for c, p in preds)
            sel = ", ".join(f'{cm[c]} AS "g{i}"' for i, c in enumerate(v.group_by))
            parts.append(f"SELECT DISTINCT {sel} FROM {frm} e"
                         + (f" WHERE {where}" if where else ""))
            if blank is not None:
                bwhere = " AND ".join([blank] + [self.pred_sql("NULL", p) for _, p in preds])
                nul = ", ".join(f'NULL AS "g{i}"' for i in range(k))
                parts.append(f"SELECT {nul} WHERE {bwhere}")
        order = ", ".join(f'"g{i}" ASC NULLS FIRST' for i in range(k))
        cols = ", ".join(f'"g{i}"' for i in range(k))
        return (f"g AS MATERIALIZED (SELECT row_number() OVER (ORDER BY {order}) AS gid, {cols} "
                f"FROM ({' UNION '.join(parts)}))")

    def blank_row_sql(self, table: str) -> str | None:
        """A boolean SQL expression: does `table` have Power BI's blank row (some
        active many-side row whose key matches no row of it)? None when nothing
        points at the table."""
        into = [r for r in self.g.active if r.to_table == table]
        if not into:
            return None
        tests = []
        for r in into:
            self.used_rels[r] = None
            self.need_column(Col(r.from_table, r.from_column))
            self.need_column(Col(r.to_table, r.to_column))
            tests.append(f"EXISTS (SELECT 1 FROM {self.table_ref(r.from_table)} f "
                         f"WHERE NOT EXISTS (SELECT 1 FROM {self.table_ref(r.to_table)} t "
                         f"WHERE t.{qident(r.to_column)} = f.{qident(r.from_column)}))")
        return "(" + " OR ".join(tests) + ")"

    # -- the whole visual --------------------------------------------------------
    def compile(self) -> Compiled:
        v = self.visual
        g_cte = self.groups_cte()
        base = Ctx()
        for f in v.filters:                        # visual filters first, intersecting
            base = base.add(f.column, f)
        for i, c in enumerate(v.group_by):         # then the group's own values
            base = base.add(c, GroupEq(i))
        values = [self.measure_sql(e, base) for _, e in v.values]
        for r in self.used_rels:
            self._unique_check(r)
        tables = self.table_ctes()
        withs = tables + [g_cte] + [f"{n} AS ({b})" for n, b in self.ctes]
        joins = "".join(f" LEFT JOIN {n} ON {n}.gid = g.gid" for n in self.value_ctes)
        select = [f'g."g{i}"' for i in range(len(v.group_by))]
        select += [f'{s} AS "v{i}"' for i, s in enumerate(values)]
        sql = (f"WITH {', '.join(withs)} SELECT {', '.join(select) or '1'} FROM g{joins} "
               "ORDER BY g.gid")
        labels = [c.column for c in v.group_by] + [label for label, _ in v.values]
        prefix = "WITH " + ", ".join(tables) + " " if tables else ""
        checks = [Check(prefix + c.sql, c.error) for c in self.checks.values()]
        return Compiled(sql, labels, len(v.group_by), checks)

    def _unique_check(self, r: Relationship) -> None:
        """The one side of a relationship must be unique - Power BI refuses the
        refresh otherwise, and a LEFT JOIN on a duplicated key would silently
        fan the many side out."""
        sides = [(r.to_table, r.to_column)]
        if r.cardinality == "one_to_one":
            sides.append((r.from_table, r.from_column))
        for t, c in sides:
            self.checks.setdefault(f"uniq:{t}:{c}", Check(
                f"SELECT 1 FROM {self.table_ref(t)} WHERE {qident(c)} IS NOT NULL "
                f"GROUP BY {qident(c)} HAVING count(*) > 1 LIMIT 1",
                ModelError(f"relationship {_rel_name(r)}: column {t}[{c}] contains duplicate "
                           "values, which is not allowed on the one side of a relationship")))


def agg_sql(func: str, x: str | None) -> str:
    """An aggregate over a SQL argument. Over no rows every aggregate is NULL
    (BLANK) - including the counts, which SQL would make 0."""
    if func == "count_rows":
        return "CASE WHEN count(*) = 0 THEN NULL ELSE count(*) END"
    if func == "count":
        return f"CASE WHEN count(*) = 0 THEN NULL ELSE count({x}) END"
    if func == "count_distinct":
        return f"CASE WHEN count(*) = 0 THEN NULL ELSE count(DISTINCT {x}) END"
    return f"{func}({x})"


def compile_visual(g: Graph, visual: Visual) -> Compiled:
    return VisualCompiler(g, visual).compile()


__all__ = ["Check", "Compiled", "Graph", "check_model", "check_report", "check_visual",
           "compile_visual", "qident", "sql_literal", "walk", "REFUSE_MARKER"]
