"""BI-as-code: the dbt semantic layer (MetricFlow YAML) front-end.

A dbt shop's metrics layer is YAML next to its models: `semantic_models:`
(entities, dimensions, measures over a dbt model) and `metrics:` (simple,
ratio, derived, cumulative), evaluated by MetricFlow. This module reads and
writes that YAML as a world.semantic Model, plus a `dashboards:` file (this
module's own format, below) for the reports.

The spec followed (MetricFlow "v1" YAML, which dbt-core 1.12 still reads; the
newer "latest" spec without measures is refused by name):
  - semantic models, entities, dimensions, measures:
    https://docs.getdbt.com/docs/build/semantic-models
    https://raw.githubusercontent.com/dbt-labs/metricflow/v0.213.0/metricflow-semantic-interfaces/metricflow_semantic_interfaces/parsing/schemas.py
  - metrics: https://docs.getdbt.com/docs/build/metrics-overview, .../simple,
    .../ratio, .../derived, .../cumulative
  - time spine: https://docs.getdbt.com/docs/build/metricflow-time-spine
  - joins: https://docs.getdbt.com/docs/build/join-logic
  - MetricFlow is Apache-2.0 from 0.209.0 (dbt-labs/metricflow CHANGELOG.md,
    PR #1905); it is NOT a dependency - tests/test_semantic_yaml.py records the
    numbers a throwaway MetricFlow 0.213.0 run produced on the fixture.

    read_project(path)                      -> (Model, (Report, ...))
    write_project(model, reports, dest)     -> [Path, ...]   (deterministic bytes)
    parse_expression(text, model)           -> metric filter or derived-metric expr
    format_expression(expr, model)          -> the text form

WHERE THINGS LIVE (a dbt project directory)
-------------------------------------------
    dbt_project.yml                 `name` is Model.name; `model-paths` (default
                                    [models]); `models: <name>: <folder>: +schema`
                                    places each dbt model in a warehouse schema;
                                    `vars: {semantic_layer: {description}}`
    <model-path>/**/*.yml|.yaml     `semantic_models:`, `metrics:`, and `models:`
                                    patches: `time_spine:` (the date table),
                                    `config: {schema}`, `columns: [{data_type}]`
    dashboards/*.yml                `dashboards:` - the reports (format below);
                                    outside model-paths, so dbt never parses it

write_project writes `<first model path>/semantic_layer/<model>.yml` and
`dashboards/<report>.yml`, and `dbt_project.yml` only when there is none. A
`models:` patch is written only for what the project cannot already say (a
schema its folder config does not give, the time spine); dbt refuses two
patches of one model, so an existing patch that would need changing is a
ModelError, never silently duplicated.

HOW THE MODEL MAPS (exactly; anything else raises Unsupported(..., VENDOR))
--------------------------------------------------------------------------
  Table            a semantic model over `ref('<relation>')`; Table.source is
                   "<schema>.<relation>". name = label, else the YAML name.
  Column           a dimension (categorical; time for date / timestamp), or an
                   entity / measure column. `expr` = the warehouse column, or a SQL
                   expression (a calculated column). data_type / hidden in
                   `config: meta:`; data_type falls back to the `models:` patch's
                   column data_type, then string / date (it never changes a value).
  Relationship     a foreign entity on the many side matching the primary entity
                   of the one side. MetricFlow needs every measure's
                   `agg_time_dimension`, which is a relationship to the date table
                   (the time spine). A join from one semantic model with measures to
                   another whose metric_time is its own (order_lines -> orders) is
                   kept INACTIVE: followed, it would give the many side two paths to
                   the date table, where MetricFlow keeps metric_time per measure.
  date table       the `time_spine:` model; reading adds hidden calculated columns
                   `metric_time__{day,week,month,quarter,year}` (metric_time_columns)
                   so a visual can group by MetricFlow's metric_time - unless its
                   semantic model says `config: meta: {metric_time_columns: false}`
                   (what writing a date table without them records).
  Measure          a metric (name = snake_case of Measure.name, label = Measure.name):
      Agg / IterAgg over one table's own columns   -> simple metric over a measure
      Calculate(..., KeepFilters(f), ...)          -> its `filter:` (see below)
      Divide(metric, metric)                       -> ratio (MetricFlow: NULLIF(den,0))
      TimeShift(simple, date, ytd|qtd|mtd)         -> cumulative, grain_to_date,
                                                      period_agg: last
      TimeShift(metric, date, same_period_last_year) -> derived input with
                                                      offset_window: 1 year
      anything else over metrics                   -> derived, `expr` in SQL
  filter           `{{ Dimension('entity__dim') }} = 'x'` etc. MetricFlow ANDs a
                   metric filter with the query's - it INTERSECTS, so it is
                   KeepFilters(FilterValues/FilterCompare). CALCULATE's default
                   (overwrite), REMOVEFILTERS and ALLEXCEPT have no MetricFlow form.
  null_semantics   "sql" (MetricFlow emits SQL).

Not exactly expressible, refused by name: CALCULATE overwrite filters,
REMOVEFILTERS / ALLEXCEPT, RELATED inside a measure, an aggregate inside a
derived expression, previous_month / previous_year, time intelligence over a
derived metric, native-SQL tables, inactive / bidirectional / one-to-one
relationships (bar the fact->fact case above), a measure whose table reaches the
date table only through another table, DAX blank semantics. Read side as well:
conversion metrics, cumulative `window` or period_agg other than `last` (the
default, `first`, is not the engine's ytd), offset_window other than 1 year,
offset_to_grain, fill_nulls_with / join_to_timespine, sum_boolean / median /
percentile, non_additive_dimension, unique / natural entities, Entity() or
Metric() in a filter, OR beyond `... OR x IS NULL`.

Known divergence from MetricFlow (the engine's contract, not this module):
offset_window shifts each DATE in context (expr.py TimeShift), MetricFlow shifts
whole PERIODS at the query grain. They agree at day grain, and at month grain
when the date table covers whole months - except a month whose year-earlier
twin is a leap February. Offsets at week / quarter / year grain are refused.
MetricFlow lists the groups its measures' rows produce (after each metric's own
filter), and keeps one whose metric then computes NULL (a ratio over a zero
denominator); Visual.groups "measures" drops a group whose every value is blank,
"rows" keeps the base table's groups even with no measure rows. "measures" is
the closer one, and the default.

DASHBOARDS FORMAT
-----------------
    dashboards:
      - name: Executive Overview          # Report.name
        model: Commerce                   # the dbt project name
        pages:
          - name: Overview
            visuals:
              - name: Top Products
                kind: table               # Visual.kind (default table)
                metrics: [units]          # or {name: units, label: "Units sold"}
                group_by: [product__product_name, metric_time__month]
                where: ["{{ Dimension('customer__region') }} = 'NA'"]
                order_by: [-units]        # '-' = descending; metric or group_by names
                limit: 3                  # Visual.top_n
                groups: rows              # Visual.groups (default measures)
              - name: Legacy panel
                sql: select 1             # a SQL visual (world/bi.py panels)

The vocabulary is MetricFlow's query vocabulary (`mf query --metrics
--group-by --where --order --limit`, and saved_queries' query_params). Reading
applies MetricFlow's own query rules: a metric with an offset or a cumulative
needs a metric_time group-by, and every group-by / filter must be joinable from
every metric's measures (ModelError otherwise - MetricFlow refuses the query).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

from .. import compile as C
from .. import render
from ..errors import ModelError, Unsupported
from ..expr import (
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
from ..model import Column, Measure, Model, Page, Relationship, Report, Table, Visual

VENDOR = "dbt semantic layer"
GRAINS = ("day", "week", "month", "quarter", "year")
OFFSET_GRAINS = ("day", "month")
SEMANTIC_FILE = "semantic_layer"
DASHBOARD_DIR = "dashboards"

AGG_TO_DBT = {"sum": "sum", "avg": "average", "min": "min", "max": "max", "count": "count",
              "count_distinct": "count_distinct"}
DBT_TO_AGG = {v: k for k, v in AGG_TO_DBT.items()}
CUMULATIVE = {"ytd": "year", "qtd": "quarter", "mtd": "month"}
GRAIN_TO_KIND = {v: k for k, v in CUMULATIVE.items()}
DATA_TYPE_TO_DBT = {"int": "integer", "decimal": "decimal", "float": "double",
                    "string": "varchar", "bool": "boolean", "date": "date",
                    "timestamp": "timestamp"}
DBT_TYPE_ALIASES = {
    "int": ("int", "integer", "bigint", "smallint", "tinyint", "hugeint", "int2", "int4",
            "int8", "long"),
    "decimal": ("decimal", "numeric", "number"),
    "float": ("double", "float", "real", "float4", "float8", "double precision"),
    "string": ("varchar", "text", "string", "char", "character varying", "bpchar"),
    "bool": ("boolean", "bool"),
    "date": ("date",),
    "timestamp": ("timestamp", "datetime", "timestamptz", "timestamp_ntz", "timestamp_tz"),
}
#: SQL function name (sqlglot node key) -> engine Func name, for 1-to-1 functions
SQL_FUNCS = {"abs": "abs", "floor": "floor", "ceil": "ceiling", "upper": "upper",
             "lower": "lower", "length": "len", "trim": "trim", "left": "left",
             "right": "right", "year": "year", "quarter": "quarter", "month": "month",
             "day": "day", "round": "round"}
FUNC_TO_SQL = {"abs": "ABS", "floor": "FLOOR", "ceiling": "CEIL", "upper": "UPPER",
               "lower": "LOWER", "len": "LENGTH", "trim": "TRIM", "left": "LEFT",
               "right": "RIGHT", "year": "YEAR", "quarter": "QUARTER", "month": "MONTH",
               "day": "DAY", "round": "ROUND"}
_JINJA = re.compile(r"\{\{(.*?)\}\}", re.S)
_CALL = re.compile(r"^\s*(Dimension|TimeDimension|Entity|Metric)\((.*)\)\s*$", re.S)
_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
#: MetricFlow refuses these as names (dbt parse: "names cannot match reserved
#: time granularity keywords") - a Date table's `year` / `month` columns among them.
RESERVED = ("nanosecond", "microsecond", "millisecond", "second", "minute", "hour",
            "day", "week", "month", "quarter", "year")


def refuse(feature: str, detail: str = "") -> Unsupported:
    return Unsupported(feature, VENDOR, detail)


# ------------------------------------------------------------------- naming
def ident(name: str) -> str:
    """The dbt identifier of a model name: snake_case, no double underscore (MetricFlow
    reserves `__` for entity paths), starting with a letter, and not a time
    granularity keyword (`year` -> `year_value`; the model name rides in `label`)."""
    if _IDENT.match(name) and "__" not in name and not name.endswith("_"):
        s = name
    else:
        s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if not s or not s[0].isalpha():
            s = "x_" + s if s else "x"
    return f"{s}_value" if s in RESERVED else s


def metric_time_columns(table: str, date_column: str) -> tuple[Column, ...]:
    """The hidden calculated columns reading adds to the date table, one per grain:
    `metric_time__<grain>` is MetricFlow's metric_time truncated to that grain (a
    date; weeks start on Monday, as DuckDB's date_trunc does)."""
    day = Col(table, date_column)
    cols = [Column("metric_time__day", "", "date", hidden=True, expr=day)]
    for g in GRAINS[1:]:
        cols.append(Column(f"metric_time__{g}", "", "date", hidden=True,
                           expr=Func(f"date_trunc_{g}", (day,))))
    return tuple(cols)


def _schema_relation(source: str) -> tuple[str, str]:
    parts = source.split(".")
    if len(parts) != 2:
        raise refuse("table source without a schema", f"{source!r}: write 'schema.relation'")
    return parts[0], parts[1]


# --------------------------------------------------------- SQL <-> expression
def _lit_sql(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, float):
        return f"CAST('{v!r}' AS DOUBLE)"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    if isinstance(v, dt.datetime):
        return f"CAST('{v.isoformat(sep=' ')}' AS TIMESTAMP)"
    if isinstance(v, dt.date):
        return f"CAST('{v.isoformat()}' AS DATE)"
    raise refuse("literal", repr(v))


def _sql_ident(name: str) -> str:
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def expr_sql(e, leaf, top: bool = True) -> str:
    """An expression as MetricFlow SQL (DuckDB dialect); `leaf` spells what is not
    an operator (metric inputs in a derived expr, columns in a measure expr)."""
    def p(x):                       # an operand: parenthesised when it is an operation
        s = expr_sql(x, leaf, top=False)
        return f"({s})" if isinstance(x, (BinOp, Divide)) else s
    if isinstance(e, Lit):
        return _lit_sql(e.value)
    if isinstance(e, BinOp):
        op = {"and": "AND", "or": "OR", "&": "||"}.get(e.op, e.op)
        return f"{p(e.left)} {op} {p(e.right)}"
    if isinstance(e, Neg):
        return f"-({expr_sql(e.arg, leaf, False)})" if isinstance(e.arg, Lit) else f"-{p(e.arg)}"
    if isinstance(e, Not):
        return f"NOT {p(e.arg)}"
    if isinstance(e, Divide):
        d = expr_sql(e.den, leaf, False)
        if e.alternate is None:
            return f"{p(e.num)} / NULLIF({d}, 0)"
        return (f"CASE WHEN NULLIF({d}, 0) IS NULL THEN {expr_sql(e.alternate, leaf, False)} "
                f"ELSE {p(e.num)} / {p(e.den)} END")
    if isinstance(e, If):
        other = "" if e.else_ is None else f" ELSE {expr_sql(e.else_, leaf, False)}"
        return (f"CASE WHEN {expr_sql(e.cond, leaf, False)} THEN "
                f"{expr_sql(e.then, leaf, False)}{other} END")
    if isinstance(e, Coalesce):
        return f"COALESCE({', '.join(expr_sql(a, leaf, False) for a in e.args)})"
    if isinstance(e, Func):
        args = [expr_sql(a, leaf, False) for a in e.args]
        if e.name == "weekday":
            return f"(DAYOFWEEK({args[0]}) + 1)"
        if e.name.startswith("date_trunc_"):
            return f"CAST(DATE_TRUNC('{e.name[11:]}', {args[0]}) AS DATE)"
        return f"{FUNC_TO_SQL[e.name]}({', '.join(args)})"
    return leaf(e)


def sql_expr(text: str, resolve, where: str):
    """MetricFlow SQL (a derived `expr`, a measure / dimension `expr`) -> Expr.
    `resolve(name)` turns an identifier into a leaf expression."""
    try:
        node = sqlglot.parse_one(str(text), read="duckdb")
    except sqlglot.errors.ParseError as exc:
        raise ModelError(f"{where}: cannot parse {text!r}: {exc}") from None
    return _from_sql(node, resolve, where)


def _number(text: str):
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ModelError(f"bad number {text!r}") from None


def _literal(node):
    """A SQL literal node -> Python value, or raise LookupError if it is not one."""
    if isinstance(node, exp.Paren):
        return _literal(node.this)
    if isinstance(node, exp.Literal):
        return node.this if node.is_string else _number(node.this)
    if isinstance(node, exp.Boolean):
        return node.this
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) \
            and not node.this.is_string:
        return -_number(node.this.this)
    if isinstance(node, exp.Cast) and isinstance(node.this, exp.Literal) and node.this.is_string:
        to = node.to.this
        text = node.this.this
        try:
            if to == exp.DataType.Type.DATE:
                return dt.date.fromisoformat(text)
            if to in (exp.DataType.Type.TIMESTAMP, exp.DataType.Type.DATETIME):
                return dt.datetime.fromisoformat(text)
            if to == exp.DataType.Type.DOUBLE:
                return float(text)
        except ValueError:
            raise ModelError(f"bad literal {node.sql()!r}") from None
    raise LookupError(node)


def _from_sql(node, resolve, where: str):
    rec = lambda n: _from_sql(n, resolve, where)  # noqa: E731
    try:
        return Lit(_literal(node))
    except LookupError:
        pass
    if isinstance(node, exp.Paren):
        return rec(node.this)
    if isinstance(node, exp.Column):
        if node.table:
            raise refuse("a qualified column", f"{where}: {node.sql()}")
        return resolve(node.name)
    if isinstance(node, exp.Neg):
        return Neg(rec(node.this))
    if isinstance(node, exp.Not):
        return Not(rec(node.this))
    if isinstance(node, exp.Add) and isinstance(node.this, exp.DayOfWeek) \
            and _is_one(node.expression):
        return Func("weekday", (rec(node.this.this),))
    binops = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.EQ: "=", exp.NEQ: "<>",
              exp.LT: "<", exp.GT: ">", exp.LTE: "<=", exp.GTE: ">=", exp.And: "and",
              exp.Or: "or", exp.DPipe: "&"}
    if type(node) in binops:
        return BinOp(binops[type(node)], rec(node.this), rec(node.expression))
    if isinstance(node, exp.Div):
        den = node.expression
        if isinstance(den, exp.Nullif) and _is_zero(den.expression):
            return Divide(rec(node.this), rec(den.this))
        return BinOp("/", rec(node.this), rec(den))
    if isinstance(node, exp.Case):
        if node.this is not None:
            raise refuse("CASE <value> WHEN", where)
        divide = _case_divide(node, rec)
        if divide is not None:
            return divide
        out = None if node.args.get("default") is None else rec(node.args["default"])
        for branch in reversed(node.args["ifs"]):
            out = If(rec(branch.this), rec(branch.args["true"]), out)
        return out
    if isinstance(node, exp.If):
        other = node.args.get("false")
        return If(rec(node.this), rec(node.args["true"]), None if other is None else rec(other))
    if isinstance(node, exp.Coalesce):
        return Coalesce(tuple(rec(a) for a in [node.this] + list(node.expressions)))
    if isinstance(node, exp.Cast) and isinstance(node.this, (exp.TimestampTrunc, exp.DateTrunc)) \
            and node.to.this == exp.DataType.Type.DATE:
        unit = node.this.args["unit"].name.lower()
        if unit not in GRAINS[1:]:
            raise refuse(f"DATE_TRUNC to {unit}", where)
        return Func(f"date_trunc_{unit}", (rec(node.this.this),))
    key = node.key
    if key in SQL_FUNCS:
        args = [node.this] + [a for k, a in node.args.items()
                              if k != "this" and isinstance(a, exp.Expression)]
        if key == "trim" and len(args) > 1:
            raise refuse("TRIM with characters", where)
        return Func(SQL_FUNCS[key], tuple(rec(a) for a in args))
    raise refuse(f"SQL function {key.upper()}" if isinstance(node, exp.Func)
                 else f"SQL {key.upper()}", f"{where}: {node.sql()}")


def _is_zero(node) -> bool:
    try:
        return _literal(node) == 0
    except LookupError:
        return False


def _is_one(node) -> bool:
    try:
        return _literal(node) == 1
    except LookupError:
        return False


def _case_divide(node, rec):
    """`CASE WHEN NULLIF(d, 0) IS NULL THEN alt ELSE n / d END` -> Divide(n, d, alt)."""
    ifs = node.args["ifs"]
    other = node.args.get("default")
    if len(ifs) != 1 or not isinstance(other, exp.Div):
        return None
    cond = ifs[0].this
    if not (isinstance(cond, exp.Is) and isinstance(cond.expression, exp.Null)
            and isinstance(cond.this, exp.Nullif) and _is_zero(cond.this.expression)):
        return None
    if cond.this.this != other.expression:
        return None
    return Divide(rec(other.this), rec(other.expression), rec(ifs[0].args["true"]))


# --------------------------------------------------------------------- filters
def _filter_dims(text: str) -> tuple[str, dict[str, str]]:
    """Replace each `{{ Dimension('e__d') }}` / `{{ TimeDimension('metric_time',
    'month') }}` by a placeholder identifier; returns the SQL and placeholder ->
    group-by item (`e__d`, `metric_time__month`)."""
    dims: dict[str, str] = {}

    def sub(m):
        call = _CALL.match(m.group(1))
        if not call:
            raise refuse("filter template", m.group(0))
        kind, raw = call.groups()
        if kind in ("Entity", "Metric"):
            raise refuse(f"{kind}() in a filter", m.group(0))
        args = [a.strip().strip("'\"") for a in raw.split(",")]
        if kind == "Dimension" and len(args) != 1 or kind == "TimeDimension" and len(args) > 2:
            raise refuse("filter template arguments", m.group(0))
        key = f"semantic_dim_{len(dims)}"
        dims[key] = "__".join(args)
        return key

    return _JINJA.sub(sub, text), dims


def parse_filter(text: str, resolve_dim) -> list:
    """A MetricFlow `where` / metric `filter` string -> FilterValues / FilterCompare
    list (AND). The canonical forms (format_filter writes exactly these):

        X = v | X IN (..)                    FilterValues(values)
        X <> v | X NOT IN (..)               FilterValues(values + (None,), negate)
        (X = v | X IN (..)) OR X IS NULL     FilterValues(values + (None,))
        (X <> v | X NOT IN (..)) OR X IS NULL  FilterValues(values, negate)
        X IS NULL / X IS NOT NULL            FilterValues((None,)) [negate]
        X < v, >, <=, >=                     FilterCompare
    """
    sql, dims = _filter_dims(text)
    try:
        node = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError as exc:
        raise ModelError(f"filter {text!r}: {exc}") from None
    out = []

    def column(n):
        if isinstance(n, exp.Paren):
            return column(n.this)
        if isinstance(n, exp.Column) and n.name in dims:
            return resolve_dim(dims[n.name])
        raise refuse("filter on an expression", f"{text!r}: {n.sql()}")

    def values(n):
        try:
            return _literal(n)
        except LookupError:
            raise refuse("filter value that is not a literal", f"{text!r}: {n.sql()}") from None

    def positive(n):
        """(column, values, negate) of an EQ / NEQ / IN / NOT IN node, or None."""
        if isinstance(n, exp.Paren):
            return positive(n.this)
        if isinstance(n, exp.EQ):
            return column(n.this), (values(n.expression),), False
        if isinstance(n, exp.NEQ):
            return column(n.this), (values(n.expression),), True
        if isinstance(n, exp.In):
            return column(n.this), tuple(values(v) for v in n.expressions), False
        if isinstance(n, exp.Not) and isinstance(n.this, exp.In):
            return column(n.this.this), tuple(values(v) for v in n.this.expressions), True
        return None

    def is_null(n):
        n = n.this if isinstance(n, exp.Paren) else n
        if isinstance(n, exp.Is) and isinstance(n.expression, exp.Null):
            return column(n.this)
        return None

    def walk(n):
        if isinstance(n, exp.Paren):
            return walk(n.this)
        if isinstance(n, exp.And):
            walk(n.this)
            walk(n.expression)
            return
        if isinstance(n, exp.Or):
            left = positive(n.this)
            right = is_null(n.expression)
            if left and right is not None and left[0] == right:
                c, vals, neg = left
                out.append(FilterValues(c, vals, negate=True) if neg
                           else FilterValues(c, vals + (None,)))
                return
            raise refuse("OR in a filter", text)
        c = is_null(n)
        if c is not None:
            out.append(FilterValues(c, (None,)))
            return
        if isinstance(n, exp.Not) and is_null(n.this) is not None:
            out.append(FilterValues(is_null(n.this), (None,), negate=True))
            return
        pos = positive(n)
        if pos:
            c, vals, neg = pos
            out.append(FilterValues(c, vals + (None,), negate=True) if neg
                       else FilterValues(c, vals))
            return
        ops = {exp.LT: "<", exp.GT: ">", exp.LTE: "<=", exp.GTE: ">="}
        if type(n) in ops:
            out.append(FilterCompare(column(n.this), ops[type(n)], values(n.expression)))
            return
        raise refuse(f"SQL {n.key.upper()} in a filter", text)

    walk(node)
    return out


def format_filter(f, dim_ref) -> str:
    """FilterValues / FilterCompare -> the canonical filter text (parse_filter's)."""
    x = dim_ref(f.column)
    if isinstance(f, FilterCompare):
        if f.op in ("=", "<>"):
            raise refuse(f"FilterCompare {f.op}", "MetricFlow text cannot tell it from "
                         "FilterValues - write FilterValues")
        if f.value is None:
            raise refuse("comparison with NULL", f"{f!r}")
        return f"{x} {f.op} {_lit_sql(f.value)}"
    vals = [v for v in f.values if v is not None]
    has_null = len(vals) != len(f.values)
    if not f.values:
        raise refuse("an empty FilterValues", f"{f!r}")
    if not vals:
        return f"{x} IS NOT NULL" if f.negate else f"{x} IS NULL"
    lits = ", ".join(_lit_sql(v) for v in vals)
    if len(vals) == 1:
        body = f"{x} {'<>' if f.negate else '='} {lits}"
    else:
        body = f"{x} {'NOT IN' if f.negate else 'IN'} ({lits})"
    if has_null != f.negate:            # NULL needs saying: SQL's = / IN never match it
        return f"({body} OR {x} IS NULL)"
    return body


# ---------------------------------------------------------- model inspection
def _inline(model: Model, e, seen=()):
    """Every Agg / IterAgg / TimeShift a measure expression reaches (MeasureRefs
    followed)."""
    for n in C.walk(e):
        if isinstance(n, MeasureRef):
            if n.name in seen:
                continue
            m = next(x for x in model.measures if x.name == n.name)
            yield from _inline(model, m.expr, seen + (n.name,))
        else:
            yield n


def _bases(model: Model, e) -> set[str]:
    out = set()
    for n in _inline(model, e):
        if isinstance(n, Agg):
            out.add(n.table if n.func == "count_rows" else n.arg.table)
        elif isinstance(n, IterAgg):
            out.add(n.table)
    return out


def _time_kinds(model: Model, e) -> set[str]:
    return {n.kind for n in _inline(model, e) if isinstance(n, TimeShift)}


def _reach(model: Model, base: str, active_only: bool) -> set[str]:
    seen, frontier = {base}, [base]
    while frontier:
        nxt = []
        for t in frontier:
            for r in model.relationships:
                if r.from_table == t and (r.active or not active_only) and r.to_table not in seen:
                    seen.add(r.to_table)
                    nxt.append(r.to_table)
        frontier = nxt
    return seen


def _query_rules(model: Model, visual: Visual, date_table: str | None, writing: bool) -> None:
    """MetricFlow's own query rules, for a dashboard visual."""
    err = refuse if writing else (lambda feature, detail="": ModelError(f"{feature}: {detail}"))
    grains = [c.column.split("__", 1)[1] for c in visual.group_by
              if c.table == date_table and c.column.startswith("metric_time__")]
    cols = list(visual.group_by) + [f.column for f in visual.filters]
    for label, e in visual.values:
        kinds = _time_kinds(model, e)
        if kinds and not grains:
            raise err("metric_time", f"visual {visual.name!r}: {label!r} has an offset or a "
                      "cumulative, which MetricFlow evaluates only grouped by metric_time")
        if "same_period_last_year" in kinds:
            for g in grains:
                if g not in OFFSET_GRAINS:
                    raise refuse(f"offset_window at {g} grain",
                                 f"visual {visual.name!r}: MetricFlow offsets whole {g}s, "
                                 "the semantic core shifts the dates in context")
        for base in _bases(model, e):
            active, every = _reach(model, base, True), _reach(model, base, False)
            for c in cols:
                if c.table in active:
                    continue
                if c.table in every:
                    raise refuse("a join through a semantic model with its own "
                                 "agg_time_dimension",
                                 f"visual {visual.name!r}: {c.table}[{c.column}] from {base!r}")
                raise err("no join path", f"visual {visual.name!r}: MetricFlow cannot join "
                          f"{c.table}[{c.column}] to {label!r}'s measures on {base!r}")


# ------------------------------------------------------------------ writing
class _Writer:
    def __init__(self, model: Model, reports: tuple[Report, ...], dest: Path):
        self.model, self.reports, self.dest = model, tuple(reports), Path(dest)
        render.validate(model, self.reports)
        self.g = C.Graph(model)
        self.tables = {t.name: t for t in model.tables}
        self.measures = {m.name: m for m in model.measures}
        self.metric_names: dict[str, str] = {}
        self.sm_measures: dict[str, list] = {t.name: [] for t in model.tables}

    # -- names ---------------------------------------------------------------
    def entity(self, table: str) -> str:
        return ident(table)

    def dim_name(self, col: Col) -> str:
        return ident(col.column)

    def dim_ref(self, col: Col) -> str:
        if col.table == self.date and col.column.startswith("metric_time__"):
            return f"{{{{ TimeDimension('metric_time', '{col.column.split('__', 1)[1]}') }}}}"
        return f"{{{{ Dimension('{self.entity(col.table)}__{self.dim_name(col)}') }}}}"

    def group_item(self, col: Col) -> str:
        if col.table == self.date and col.column.startswith("metric_time__"):
            return col.column
        return f"{self.entity(col.table)}__{self.dim_name(col)}"

    def metric(self, name: str) -> str:
        return self.metric_names[name]

    # -- the model -----------------------------------------------------------
    def check_model(self) -> None:
        m = self.model
        if m.null_semantics != "sql":
            raise refuse("DAX blank semantics", "MetricFlow emits SQL: null_semantics must be "
                         "'sql'")
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", m.name):
            raise refuse("model name that is not a dbt project name", repr(m.name))
        dates = [t for t in m.tables if t.is_date_table]
        if len(dates) != 1:
            raise refuse("a model without exactly one date table",
                         "MetricFlow needs one time spine for metric_time")
        self.date = dates[0].name
        self.date_col = dates[0].date_column
        mt = metric_time_columns(self.date, self.date_col)
        dcols = self.tables[self.date].columns
        self.has_metric_time = dcols[-len(mt):] == mt
        rest = dcols[:-len(mt)] if self.has_metric_time else dcols
        if any(c.name.startswith("metric_time__") for c in rest):
            raise refuse("metric_time columns other than metric_time_columns()",
                         f"{self.date!r}: reading derives metric_time__<grain> itself")
        names = {}
        for t in m.tables:
            if t.source_sql is not None:
                raise refuse("native SQL table", f"{t.name!r}: a semantic model reads a dbt model")
            _schema_relation(t.source)
            n = ident(t.name)
            if n in names:
                raise refuse("two names that spell one dbt identifier",
                             f"{names[n]!r} and {t.name!r} -> {n!r}")
            names[n] = t.name
            dims = {}
            for c in self._columns(t):
                d = ident(c.name)
                if d in dims or d == self.entity(t.name):
                    raise refuse("two names that spell one dbt identifier",
                                 f"{t.name}: {c.name!r} -> {d!r}")
                dims[d] = c.name
        for r in m.relationships:
            if r.cardinality != "many_to_one":
                raise refuse("one-to-one relationship", C._rel_name(r))
            if r.cross_filter != "single":
                raise refuse("bidirectional cross-filter", C._rel_name(r))
            if not r.active and not self._fact_to_fact(r):
                raise refuse("inactive relationship", C._rel_name(r))
            for t, c in ((r.from_table, r.from_column), (r.to_table, r.to_column)):
                if self.g.column(Col(t, c)).expr is not None:
                    raise refuse("relationship on a calculated column", C._rel_name(r))
        for t in m.tables:
            keys = {r.to_column for r in m.relationships if r.to_table == t.name}
            if len(keys) > 1:
                raise refuse("relationships to two different keys of one table",
                             f"{t.name}: {sorted(keys)} (one primary entity per semantic model)")
        seen = {}
        for ms in m.measures:
            n = ident(ms.name)
            if n in seen:
                raise refuse("two names that spell one dbt identifier",
                             f"{seen[n]!r} and {ms.name!r} -> {n!r}")
            seen[n] = ms.name
            self.metric_names[ms.name] = n

    def _fact_to_fact(self, r: Relationship) -> bool:
        """An inactive join from a table with its own date relationship to one that
        reaches the date table: how reading lowers order_lines -> orders."""
        own = any(x.from_table == r.from_table and x.to_table == self.date and x.active
                  for x in self.model.relationships)
        return own and self.date in _reach(self.model, r.to_table, True)

    def _columns(self, t: Table) -> tuple[Column, ...]:
        """A table's columns as dimensions: the date table's metric_time columns
        are not written (reading derives them)."""
        if t.name == self.date and self.has_metric_time:
            return t.columns[:-len(GRAINS)]
        return t.columns

    def column_sql(self, t: Table, e, where: str) -> str:
        """A row-level expression over `t`'s own columns as SQL over its dbt model."""
        def leaf(x):
            if isinstance(x, Col):
                if x.table != t.name:
                    raise refuse("a column of another table in a measure", where)
                c = self.g.column(x)
                return f"({self.column_sql(t, c.expr, where)})" if c.expr is not None \
                    else _sql_ident(c.source)
            if isinstance(x, Related):
                raise refuse("RELATED in a measure expression",
                             f"{where}: a MetricFlow measure reads one semantic model")
            if isinstance(x, (MeasureRef, Calculate, TimeShift)):
                raise refuse("context transition", where)
            if isinstance(x, (Agg, IterAgg)):
                raise refuse("an aggregate inside a row expression", where)
            raise refuse(f"{type(x).__name__} in a measure expression", where)
        return expr_sql(e, leaf)

    def semantic_model(self, t: Table) -> dict:
        doc: dict = {"name": ident(t.name), "label": t.name, "description": t.description,
                     "model": f"ref('{_schema_relation(t.source)[1]}')"}
        meta = {}
        if t.hidden:
            meta["hidden"] = True
        if t.name == self.date and not self.has_metric_time:
            meta["metric_time_columns"] = False      # reading must not add them
        if meta:
            doc["config"] = {"meta": meta}
        into = [r for r in self.model.relationships if r.to_table == t.name]
        entities = []
        if into:
            key = self.g.column(Col(t.name, into[0].to_column))
            entities.append({"name": self.entity(t.name), "type": "primary", "expr": key.source})
        else:
            doc["primary_entity"] = self.entity(t.name)
        for r in self.model.relationships:
            if r.from_table == t.name:
                fk = self.g.column(Col(t.name, r.from_column))
                entities.append({"name": self.entity(r.to_table), "type": "foreign",
                                 "expr": fk.source})
        if entities:
            doc["entities"] = entities
        dims = []
        for c in self._columns(t):
            d: dict = {"name": ident(c.name)}
            if d["name"] != c.name:
                d["label"] = c.name
            if c.data_type in ("date", "timestamp"):
                d["type"] = "time"
                d["type_params"] = {"time_granularity": "day" if c.data_type == "date"
                                    else "second"}
            else:
                d["type"] = "categorical"
            if c.expr is not None:
                d["expr"] = self.column_sql(t, c.expr, f"calculated column {t.name}[{c.name}]")
            elif c.source != d["name"]:
                d["expr"] = c.source
            if c.description:
                d["description"] = c.description
            meta = {"data_type": c.data_type}
            if c.hidden:
                meta["hidden"] = True
            if c.expr is not None:
                meta["calculated"] = True
            d["config"] = {"meta": meta}
            dims.append(d)
        if dims:
            doc["dimensions"] = dims
        return doc

    def agg_time(self, table: str, where: str) -> str:
        rels = [r for r in self.model.relationships
                if r.from_table == table and r.to_table == self.date and r.active]
        if not rels:
            via = "only through another table" if self.date in _reach(self.model, table, True) \
                else "not at all"
            raise refuse("agg_time_dimension", f"{where}: {table!r} reaches the date table "
                         f"{via}; MetricFlow's agg_time_dimension is a time dimension of the "
                         "measure's own semantic model")
        return ident(rels[0].from_column)

    # -- measures --------------------------------------------------------------
    def filters_of(self, mods, where: str, bases: set[str]) -> list[str]:
        out = []
        for m in mods:
            if isinstance(m, KeepFilters):
                for b in bases:
                    if m.modifier.column.table not in _reach(self.model, b, True):
                        raise refuse("a filter no join reaches",
                                     f"{where}: {m.modifier.column} from {b!r}")
                out.append(format_filter(m.modifier, self.dim_ref))
            elif isinstance(m, (FilterValues, FilterCompare)):
                raise refuse("CALCULATE filter (overwrite)",
                             f"{where}: MetricFlow filters intersect - wrap it in KeepFilters")
            elif isinstance(m, RemoveFilters):
                raise refuse("REMOVEFILTERS", where)
            elif isinstance(m, KeepOnly):
                raise refuse("ALLEXCEPT", where)
        return out

    def base_measure(self, e, name: str, where: str) -> tuple[str, dict] | None:
        """An Agg / IterAgg over one table -> (table, measure doc), else None."""
        if isinstance(e, Agg):
            table = e.table if e.func == "count_rows" else e.arg.table
            t = self.tables[table]
            if e.func == "count_rows":
                doc = {"name": name, "agg": "count", "expr": "1"}
            else:
                doc = {"name": name, "agg": AGG_TO_DBT[e.func],
                       "expr": self.column_sql(t, e.arg, where)}
            return table, doc
        if isinstance(e, IterAgg):
            t = self.tables[e.table]
            return e.table, {"name": name, "agg": AGG_TO_DBT[e.func],
                             "expr": self.column_sql(t, e.expr, where)}
        return None

    def split_filters(self, e):
        if isinstance(e, Calculate):
            return e.expr, e.modifiers
        return e, ()

    def add_measure(self, table: str, doc: dict, where: str) -> None:
        self.agg_time(table, where)
        self.sm_measures[table].append(doc)

    def metric_doc(self, ms: Measure) -> dict:
        name = self.metric(ms.name)
        where = f"measure {ms.name!r}"
        doc: dict = {"name": name, "label": ms.name}
        if ms.description:
            doc["description"] = ms.description
        meta = {}
        if ms.format_string:
            meta["format"] = ms.format_string
        inner, mods = self.split_filters(ms.expr)
        simple = self.base_measure(inner, name, where)
        if simple is not None:                                       # simple
            table, measure = simple
            filters = self.filters_of(mods, where, {table})
            self.add_measure(table, measure, where)
            doc["type"] = "simple"
            doc["type_params"] = {"measure": name}
            if filters:
                doc["filter"] = filters[0] if len(filters) == 1 else filters
            home = table
        elif isinstance(ms.expr, TimeShift) and ms.expr.kind in CUMULATIVE:   # cumulative
            ts = ms.expr
            self.check_date_col(ts, where)
            body = ts.expr
            if isinstance(body, MeasureRef):
                body = self.measures[body.name].expr
            binner, bmods = self.split_filters(body)
            simple = self.base_measure(binner, name, where)
            if simple is None:
                raise refuse("time intelligence over a derived metric",
                             f"{where}: a cumulative metric takes a measure")
            table, measure = simple
            filters = self.filters_of(bmods, where, {table})
            self._no_date_filters(bmods, where)
            self.add_measure(table, measure, where)
            # The filter goes on the METRIC: dbt 1.12 drops a filter on a cumulative
            # metric's measure input (measured with MetricFlow 0.213.0 - the input
            # becomes a private metric without it), while a metric filter applies
            # to the measure before cumulation. Reading lowers both alike.
            if filters:
                doc["filter"] = filters[0] if len(filters) == 1 else filters
            doc["type"] = "cumulative"
            doc["type_params"] = {"measure": name,
                                  "cumulative_type_params": {
                                      "grain_to_date": CUMULATIVE[ts.kind],
                                      "period_agg": "last"}}
            home = table
        elif self.ratio_parts(ms.expr) is not None:                  # ratio
            num, den, filters = self.ratio_parts(ms.expr)
            doc["type"] = "ratio"
            doc["type_params"] = {"numerator": self.ratio_input(num, where),
                                  "denominator": self.ratio_input(den, where)}
            if filters:
                doc["filter"] = filters[0] if len(filters) == 1 else filters
            home = self.home(self.input_metric(num))
        else:                                                        # derived
            inputs: dict = {}
            expr = self.derived_sql(ms.expr, inputs, where)
            doc["type"] = "derived"
            doc["type_params"] = {"expr": expr, "metrics": list(inputs.values())}
            if not inputs:
                raise refuse("a derived metric over no metric", where)
            home = self.home(next(iter(inputs.values()))["_measure"])
            for spec in inputs.values():
                del spec["_measure"]
        if ms.table != home:
            meta["home_table"] = ms.table
        if meta:
            doc["config"] = {"meta": meta}
        return doc

    def home(self, measure_name: str) -> str:
        """The default home table of a metric - the rule reading applies: a simple
        or cumulative metric's measure table, else its first input metric's
        (the numerator; the first entry of a derived metric's `metrics:`)."""
        ms = self.measures[measure_name]
        e = ms.expr
        inner, _ = self.split_filters(e)
        if self.base_measure_table(inner) is not None:
            return self.base_measure_table(inner)
        if isinstance(e, TimeShift) and e.kind in CUMULATIVE:
            body = e.expr
            if isinstance(body, MeasureRef):
                body = self.measures[body.name].expr
            return self.base_measure_table(self.split_filters(body)[0]) or ms.table
        parts = self.ratio_parts(e)
        if parts is not None:
            return self.home(self.input_metric(parts[0]))
        inputs: dict = {}
        self.derived_sql(e, inputs, f"measure {measure_name!r}")
        return self.home(next(iter(inputs.values()))["_measure"]) if inputs else ms.table

    @staticmethod
    def base_measure_table(e) -> str | None:
        if isinstance(e, Agg):
            return e.table if e.func == "count_rows" else e.arg.table
        if isinstance(e, IterAgg):
            return e.table
        return None

    def check_date_col(self, ts: TimeShift, where: str) -> None:
        if ts.date_column != Col(self.date, self.date_col):
            raise refuse("time intelligence over another date column", where)

    def _no_date_filters(self, mods, where: str) -> None:
        dates = self.g.expanded(self.date)
        for m in mods:
            if isinstance(m, KeepFilters) and m.modifier.column.table in dates:
                raise refuse("a date-table filter around a time shift", where)

    @staticmethod
    def is_input(e) -> bool:
        if isinstance(e, MeasureRef):
            return True
        return isinstance(e, Calculate) and isinstance(e.expr, MeasureRef)

    def ratio_parts(self, e):
        inner, mods = self.split_filters(e)
        if isinstance(inner, Divide) and inner.alternate is None \
                and self.is_input(inner.num) and self.is_input(inner.den):
            return inner.num, inner.den, self.filters_of(
                mods, "ratio", _bases(self.model, inner))
        return None

    @staticmethod
    def input_metric(e) -> str:
        return e.name if isinstance(e, MeasureRef) else e.expr.name

    def ratio_input(self, e, where: str):
        if isinstance(e, MeasureRef):
            return self.metric(e.name)
        filters = self.filters_of(e.modifiers, where, _bases(self.model, e.expr))
        return {"name": self.metric(e.expr.name),
                "filter": filters[0] if len(filters) == 1 else filters}

    def derived_sql(self, e, inputs: dict, where: str) -> str:
        """A derived metric's `expr`, collecting its input metrics."""
        def leaf(x):
            spec = self.derived_input(x, where)
            if spec is None:
                if isinstance(x, (Agg, IterAgg)):
                    raise refuse("an aggregate inside a derived metric expression",
                                 f"{where}: give the aggregate its own metric")
                if isinstance(x, Calculate):
                    self.filters_of(x.modifiers, where, _bases(self.model, x.expr))
                    raise refuse("CALCULATE around an expression",
                                 f"{where}: MetricFlow filters a metric input, not an expression")
                if isinstance(x, TimeShift):
                    if x.kind not in ("same_period_last_year",) + tuple(CUMULATIVE):
                        raise refuse(x.kind, f"{where}: no MetricFlow offset is this TimeShift")
                    raise refuse("time intelligence over an expression",
                                 f"{where}: offset_window shifts one input metric")
                raise refuse(f"{type(x).__name__} in a derived metric", where)
            key = repr(sorted((k, repr(v)) for k, v in spec.items()))
            for alias, have in inputs.items():
                if repr(sorted((k, repr(v)) for k, v in have.items())) == key:
                    return alias
            base = spec["name"]
            if "offset_window" in spec:
                alias = f"{base}_offset"
            elif "filter" in spec:
                alias = f"{base}_filtered"
            else:
                alias = base
            n = 2
            while alias in inputs:
                alias = f"{base}_{n}"
                n += 1
            if alias != base:
                spec["alias"] = alias
            inputs[alias] = spec
            return alias

        return expr_sql(e, leaf)

    def derived_input(self, x, where: str) -> dict | None:
        offset = False
        if isinstance(x, TimeShift):
            if x.kind != "same_period_last_year":
                if x.kind in CUMULATIVE:
                    raise refuse("time intelligence over a derived metric",
                                 f"{where}: a cumulative metric takes a measure")
                raise refuse(x.kind, f"{where}: MetricFlow has no offset for {x.kind}")
            self.check_date_col(x, where)
            offset, x = True, x.expr
        filters: list[str] = []
        if isinstance(x, Calculate) and isinstance(x.expr, MeasureRef):
            filters = self.filters_of(x.modifiers, where, _bases(self.model, x.expr))
            if offset:
                self._no_date_filters(x.modifiers, where)
            x = x.expr
        if not isinstance(x, MeasureRef):
            if offset:
                raise refuse("offset over an expression",
                              f"{where}: offset_window shifts one input metric")
            return None
        spec: dict = {"name": self.metric(x.name), "_measure": x.name}
        if filters:
            spec["filter"] = filters[0] if len(filters) == 1 else filters
        if offset:
            spec["offset_window"] = "1 year"
        return spec

    # -- reports ---------------------------------------------------------------
    def visual_doc(self, v: Visual) -> dict:
        doc: dict = {"name": v.name, "kind": v.kind}
        if v.groups != "measures":
            doc["groups"] = v.groups
        if v.sql is not None:
            doc["sql"] = v.sql
            return doc
        _query_rules(self.model, v, self.date, writing=True)
        tokens = {}
        metrics = []
        for label, e in v.values:
            if not isinstance(e, MeasureRef):
                raise refuse("a visual value that is not a metric",
                             f"visual {v.name!r} value {label!r}")
            name = self.metric(e.name)
            metrics.append(name if label == e.name else {"name": name, "label": label})
            tokens[label] = name if label == e.name else label
        doc["metrics"] = metrics
        if v.group_by:
            doc["group_by"] = [self.group_item(c) for c in v.group_by]
            for c in v.group_by:
                tokens[c.column] = self.group_item(c)
        if v.filters:
            doc["where"] = [format_filter(f, self.dim_ref) for f in v.filters]
        if v.order_by:
            doc["order_by"] = [("-" if desc else "") + tokens[label] for label, desc in v.order_by]
        if v.top_n is not None:
            doc["limit"] = v.top_n
        return doc

    def report_doc(self, r: Report) -> dict:
        return {"name": r.name, "model": r.model,
                "pages": [{"name": p.name, "visuals": [self.visual_doc(v) for v in p.visuals]}
                          for p in r.pages]}

    # -- the project ---------------------------------------------------------
    def run(self) -> list[Path]:
        self.check_model()
        metrics = [self.metric_doc(m) for m in self.model.measures]
        sms = []
        for t in self.model.tables:
            doc = self.semantic_model(t)
            if self.sm_measures[t.name]:
                doc["defaults"] = {"agg_time_dimension": self.agg_time(t.name, t.name)}
                doc["measures"] = self.sm_measures[t.name]
            sms.append(doc)
        dashboards = {}
        for r in self.reports:
            fname = ident(r.name) + ".yml"
            if fname in dashboards:
                raise refuse("two reports that spell one file name", fname)
            dashboards[fname] = {"dashboards": [self.report_doc(r)]}
        project = _Project(self.dest) if (self.dest / "dbt_project.yml").exists() else None
        if project is not None and project.name != self.model.name:
            raise ModelError(f"{self.dest} is dbt project {project.name!r}, not model "
                             f"{self.model.name!r}")
        if project is not None and project.description != self.model.description:
            raise ModelError(f"{self.dest}/dbt_project.yml describes the model differently; "
                             "edit its vars.semantic_layer.description")
        model_path = project.model_paths[0] if project else "models"
        out_file = self.dest / model_path / SEMANTIC_FILE / f"{ident(self.model.name)}.yml"
        patches = self.patches(project, out_file)
        semantic: dict = {"version": 2, "semantic_models": sms, "metrics": metrics}
        if patches:
            semantic["models"] = patches
        files = {out_file: semantic}
        if project is None:
            dbt: dict = {"name": self.model.name, "version": "1.0.0", "config-version": 2,
                         "profile": ident(self.model.name), "model-paths": ["models"]}
            if self.model.description:
                dbt["vars"] = {"semantic_layer": {"description": self.model.description}}
            files[self.dest / "dbt_project.yml"] = dbt
        for fname, doc in dashboards.items():
            files[self.dest / DASHBOARD_DIR / fname] = doc
        written = []
        for path, doc in sorted(files.items()):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(HEADER + _dump(doc), encoding="utf-8")
            written.append(path)
        return written

    def patches(self, project, out_file: Path) -> list[dict]:
        """`models:` patches for what the project cannot already say."""
        out = []
        for t in self.model.tables:
            schema, relation = _schema_relation(t.source)
            need: dict = {}
            derived = project.schema_of(relation) if project else None
            if derived != schema:
                need["config"] = {"schema": schema}
            if t.is_date_table:
                source = self.g.column(Col(t.name, t.date_column)).source
                need["time_spine"] = {"standard_granularity_column": source}
                need["columns"] = [{"name": source, "granularity": "day"}]
            if not need:
                continue
            existing = project.patches.get(relation) if project else None
            if existing is not None and existing[0] != out_file:
                have = existing[1]
                ok = ("time_spine" not in need or have.get("time_spine") == need["time_spine"]) \
                    and ("config" not in need
                         or (have.get("config") or {}).get("schema") == schema)
                if not ok:
                    raise ModelError(f"dbt model {relation!r} is already documented in "
                                     f"{existing[0]}; it needs {sorted(need)} there "
                                     "(dbt refuses a second patch)")
                continue
            out.append({"name": relation, **need})
        return out


HEADER = ("# dbt semantic layer (MetricFlow). Written by world.semantic.formats.yaml_model;\n"
          "# read back by the same module. Keys are sorted so the bytes are stable.\n")


def _dump(doc) -> str:
    return yaml.safe_dump(doc, sort_keys=True, default_flow_style=False, allow_unicode=False,
                          width=10 ** 6)


# ---------------------------------------------------------------- the project
class _Project:
    """A dbt project directory: its config, its YAML property files, its models."""

    def __init__(self, root: Path):
        self.root = Path(root)
        path = self.root / "dbt_project.yml"
        if not path.exists():
            raise ModelError(f"{self.root} is not a dbt project (no dbt_project.yml)")
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.cfg = cfg
        self.name = str(cfg.get("name", ""))
        self.model_paths = list(cfg.get("model-paths") or ["models"])
        sl = ((cfg.get("vars") or {}).get("semantic_layer") or {})
        self.description = str(sl.get("description", ""))
        self.patches: dict[str, tuple[Path, dict]] = {}
        self.semantic_models: list[tuple[Path, dict]] = []
        self.metrics: list[tuple[Path, dict]] = []
        self.sql: dict[str, Path] = {}
        for mp in self.model_paths:
            base = self.root / mp
            for f in sorted(base.rglob("*.sql")) if base.exists() else []:
                self.sql.setdefault(f.stem, f.relative_to(base))
            files = sorted(list(base.rglob("*.yml")) + list(base.rglob("*.yaml"))) \
                if base.exists() else []
            for f in files:
                doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                if not isinstance(doc, dict):
                    continue
                for p in doc.get("models") or []:
                    if p.get("name") in self.patches:
                        raise ModelError(f"dbt model {p.get('name')!r} is patched twice "
                                         f"({self.patches[p['name']][0]} and {f})")
                    self.patches[p["name"]] = (f, p)
                for sm in doc.get("semantic_models") or []:
                    self.semantic_models.append((f, sm))
                for m in doc.get("metrics") or []:
                    self.metrics.append((f, m))

    def schema_of(self, relation: str) -> str | None:
        """The schema dbt builds `relation` into, from the folder `+schema` config
        (the world's generate_schema_name uses a custom schema verbatim)."""
        patch = self.patches.get(relation)
        if patch and (patch[1].get("config") or {}).get("schema"):
            return patch[1]["config"]["schema"]
        cfg = ((self.cfg.get("models") or {}).get(self.name) or {})
        schema = cfg.get("+schema") or cfg.get("schema")
        rel = self.sql.get(relation)
        if rel is None:                 # no model file: the project cannot say
            return None
        node = cfg
        for part in rel.parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                break
            schema = node.get("+schema") or node.get("schema") or schema
        return schema or "main"


# ------------------------------------------------------------------- reading
_REF = re.compile(r"""^\s*ref\(\s*['"]([^'"]+)['"]\s*\)\s*$""")


class _Reader:
    def __init__(self, root: Path):
        self.project = _Project(root)
        self.root = Path(root)

    def run(self) -> tuple[Model, tuple[Report, ...]]:
        p = self.project
        self.sms: dict[str, dict] = {}
        for f, sm in p.semantic_models:
            n = sm.get("name")
            if n in self.sms:
                raise ModelError(f"semantic model {n!r} defined twice ({f})")
            self.sms[n] = sm
        # dbt's "latest" spec puts semantics on `models:` entries and drops a
        # metric's type_params (measures are gone): refused by name, not misread
        for rel, (f, patch) in p.patches.items():
            if patch.get("semantic_model") or patch.get("derived_semantics"):
                raise refuse("the latest (v2) semantic layer spec", f"{f}: model {rel!r}")
        for f, m in p.metrics:
            if "type_params" not in m and any(k in m for k in (
                    "agg", "input_metric", "input_metrics", "numerator", "base_metric")):
                raise refuse("the latest (v2) semantic layer spec",
                             f"{f}: metric {m.get('name')!r}")
        self.spine = self._spine()
        self.tables: dict[str, Table] = {}
        self.cols: dict[str, dict] = {}          # sm -> {"dims": {dim: col}, "src": {src: col}}
        self.entity_table: dict[str, str] = {}
        self.key: dict[str, str] = {}            # table -> primary key column
        for sm in self.sms.values():
            self._table(sm)
        if self.spine and self.spine[0] not in {self._relation(sm) for sm in self.sms.values()}:
            self._spine_table()
        self.date = next((t.name for t in self.tables.values() if t.is_date_table), None)
        self.relationships = self._relationships()
        self.measure_defs = self._measure_defs()
        self.metric_defs = self._metric_defs()
        self.labels = {}
        for name, (_, m) in self.metric_defs.items():
            label = str(m.get("label") or name)
            if label in self.labels.values():
                raise ModelError(f"two metrics are labelled {label!r}")
            self.labels[name] = label
        self.homes: dict[str, str] = {}
        measures = tuple(self._measure(n) for n in self.metric_defs)
        tables = tuple(self.tables.values())
        model = Model(p.name, tables, tuple(self.relationships), measures,
                      null_semantics="sql", description=p.description)
        self.model = model
        reports = self._reports()
        render.validate(model, reports)
        for r in reports:
            for page in r.pages:
                for v in page.visuals:
                    if v.sql is None:
                        _query_rules(model, v, self.date, writing=False)
        return model, reports

    # -- tables -----------------------------------------------------------------
    def _relation(self, sm: dict) -> str:
        m = _REF.match(str(sm.get("model", "")))
        if not m:
            raise ModelError(f"semantic model {sm.get('name')!r}: model {sm.get('model')!r} "
                             "is not ref('...')")
        return m.group(1)

    def _spine(self):
        found = [(n, p.get("time_spine")) for n, (_, p) in self.project.patches.items()
                 if p.get("time_spine")]
        if len(found) > 1:
            raise refuse("two time spines", ", ".join(n for n, _ in found))
        if not found:
            return None
        n, ts = found[0]
        col = ts.get("standard_granularity_column")
        if not col:
            raise ModelError(f"time spine {n!r} has no standard_granularity_column")
        if ts.get("custom_granularities"):
            raise refuse("custom granularities", n)
        return n, col

    def _source(self, relation: str) -> str:
        schema = self.project.schema_of(relation) or "main"
        return f"{schema}.{relation}"

    def _patch_types(self, relation: str) -> dict[str, str]:
        patch = self.project.patches.get(relation)
        out = {}
        for c in (patch[1].get("columns") or []) if patch else []:
            if c.get("data_type"):
                out[c["name"]] = _data_type(str(c["data_type"]))
        return out

    def _table(self, sm: dict) -> None:
        sname = sm["name"]
        relation = self._relation(sm)
        name = str(sm.get("label") or sname)
        if name in self.tables:
            raise ModelError(f"two semantic models are named {name!r}")
        meta = ((sm.get("config") or {}).get("meta") or {})
        types = self._patch_types(relation)
        is_date = self.spine is not None and self.spine[0] == relation
        if is_date:                     # the time spine's day column is a date, always
            types.setdefault(self.spine[1], "date")
        columns: list[Column] = []
        dims: dict[str, str] = {}
        by_source: dict[str, str] = {}

        def add(col: Column, dim: str | None):
            if any(c.name == col.name for c in columns):
                raise ModelError(f"semantic model {sname!r}: two columns named {col.name!r}")
            columns.append(col)
            if dim:
                dims[dim] = col.name
            if col.expr is None:
                by_source.setdefault(col.source, col.name)

        pending_calc = []
        for d in sm.get("dimensions") or []:
            dname = d["name"]
            if str(dname).lower() in RESERVED:
                raise ModelError(f"{sname}.{dname}: MetricFlow names cannot match reserved "
                                 "time granularity keywords (dbt parse refuses the project)")
            dmeta = ((d.get("config") or {}).get("meta") or {})
            kind = d.get("type")
            if kind not in ("categorical", "time"):
                raise ModelError(f"{sname}.{dname}: dimension type {kind!r}")
            if kind == "time":
                gran = ((d.get("type_params") or {}).get("time_granularity") or "day")
                if (d.get("type_params") or {}).get("validity_params"):
                    raise refuse("SCD validity_params", f"{sname}.{dname}")
            expr_text = d.get("expr")
            label = str(d.get("label") or dname)
            calc = bool(dmeta.get("calculated")) or (
                expr_text is not None and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$",
                                                       str(expr_text).strip()))
            source = dname if expr_text is None else str(expr_text).strip()
            default = ("date" if gran == "day" else "timestamp") if kind == "time" else "string"
            dtype = dmeta.get("data_type") or types.get(source) or default
            if dtype not in C.DATA_TYPES:
                dtype = _data_type(str(dtype))
            col = Column(label, "" if calc else source, dtype, hidden=bool(dmeta.get("hidden")),
                         description=str(d.get("description") or ""))
            add(col, dname)
            if calc:
                pending_calc.append((label, str(expr_text)))
        for e in sm.get("entities") or []:
            etype = e.get("type")
            if etype not in ("primary", "foreign"):
                raise refuse(f"{etype} entity", f"{sname}.{e.get('name')}: only primary and "
                             "foreign entities lower to relationships")
            src = str(e.get("expr") or e["name"]).strip()
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", src):
                raise refuse("an entity over a SQL expression", f"{sname}.{e['name']}: {src}")
            if src not in by_source:
                add(Column(src, src, types.get(src, "string")), None)
            if etype == "primary":
                self._primary(e["name"], name, by_source[src])
        if sm.get("primary_entity"):
            self._primary(sm["primary_entity"], name, None)
        for ms in sm.get("measures") or []:
            text = str(ms.get("expr", ms["name"])).strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", text) and text not in by_source:
                add(Column(text, text, types.get(text, "decimal")), None)
        date_col = None
        if is_date:
            src = self.spine[1]
            if src not in by_source:
                add(Column(src, src, "date"), None)
            date_col = by_source[src]
        table = Table(name, self._source(relation), tuple(columns),
                      is_date_table=is_date, date_column=date_col, hidden=bool(meta.get("hidden")),
                      description=str(sm["description"]) if "description" in sm
                      else self._patch_description(relation))
        self.tables[name] = table
        self.cols[sname] = {"dims": dims, "src": by_source, "table": name}
        # calculated columns parse against the table's own columns
        calc_exprs = {}
        for label, text in pending_calc:
            calc_exprs[label] = sql_expr(text, lambda n, t=name, s=by_source: self._own(t, s, n),
                                         f"dimension {sname}.{label}")
        if calc_exprs:
            table = replace(table, columns=tuple(
                replace(c, expr=calc_exprs[c.name]) if c.name in calc_exprs else c
                for c in table.columns))
        if is_date and meta.get("metric_time_columns", True):
            table = replace(table, columns=table.columns + metric_time_columns(name, date_col))
        self.tables[name] = table

    def _patch_description(self, relation: str) -> str:
        patch = self.project.patches.get(relation)
        return str(patch[1].get("description") or "") if patch else ""

    def _own(self, table: str, by_source: dict, name: str) -> Col:
        if name not in by_source:
            raise ModelError(f"{table}: no column {name!r}")
        return Col(table, by_source[name])

    def _primary(self, entity: str, table: str, key: str | None) -> None:
        if entity in self.entity_table and self.entity_table[entity] != table:
            raise ModelError(f"entity {entity!r} is primary in two semantic models")
        self.entity_table[entity] = table
        if key is not None:
            self.key[table] = key

    def _spine_table(self) -> None:
        relation, src = self.spine
        name = relation
        cols = (Column(src, src, "date"),) + metric_time_columns(name, src)
        self.tables[name] = Table(name, self._source(relation), cols, is_date_table=True,
                                  date_column=src,
                                  description=self._patch_description(relation))

    # -- relationships ---------------------------------------------------------
    def _relationships(self) -> list[Relationship]:
        rels: list[Relationship] = []
        own_time: dict[str, Relationship] = {}
        for sname, sm in self.sms.items():
            table = self.cols[sname]["table"]
            src = self.cols[sname]["src"]
            for e in sm.get("entities") or []:
                if e["type"] != "foreign":
                    continue
                target = self.entity_table.get(e["name"])
                if target is None or target not in self.key:
                    raise ModelError(f"semantic model {sname!r}: foreign entity {e['name']!r} "
                                     "is no semantic model's primary entity")
                col = src[str(e.get("expr") or e["name"]).strip()]
                rels.append(Relationship(table, col, target, self.key[target]))
            if sm.get("measures"):
                agg = self._agg_time(sname, sm)
                if self.date is None:
                    raise refuse("measures without a time spine",
                                 "MetricFlow needs a time_spine model for agg_time_dimension")
                r = Relationship(table, agg, self.date, self.tables[self.date].date_column)
                own_time[table] = r
                if r not in rels:
                    rels.append(r)
        # A join from a table with its own metric_time to a table that reaches the
        # date table would give two paths to it: keep it, inactive.
        model = Model("", tuple(self.tables.values()), tuple(rels))
        out = []
        for r in rels:
            if r.from_table in own_time and r != own_time[r.from_table] \
                    and self.date in _reach(model, r.to_table, True) \
                    and r.to_table != self.date:
                r = replace(r, active=False)
            out.append(r)
        return out

    def _agg_time(self, sname: str, sm: dict) -> str:
        names = set()
        default = (sm.get("defaults") or {}).get("agg_time_dimension")
        for ms in sm.get("measures") or []:
            names.add(ms.get("agg_time_dimension") or default)
        if None in names:
            raise ModelError(f"semantic model {sname!r}: a measure has no agg_time_dimension")
        if len(names) > 1:
            raise refuse("measures with different agg_time_dimensions in one semantic model",
                         f"{sname}: {sorted(names)}")
        dim = names.pop()
        dims = self.cols[sname]["dims"]
        if dim not in dims:
            raise ModelError(f"semantic model {sname!r}: agg_time_dimension {dim!r} is not a "
                             "dimension")
        return dims[dim]

    # -- measures and metrics ----------------------------------------------------
    def _measure_defs(self) -> dict[str, tuple[str, dict]]:
        out: dict = {}
        for sname, sm in self.sms.items():
            for ms in sm.get("measures") or []:
                if ms["name"] in out:
                    raise ModelError(f"measure {ms['name']!r} defined twice")
                out[ms["name"]] = (sname, ms)
        return out

    def _metric_defs(self) -> dict[str, tuple[Path, dict]]:
        out: dict = {}
        for f, m in self.project.metrics:
            if m.get("name") in out:
                raise ModelError(f"metric {m.get('name')!r} defined twice ({f})")
            out[m["name"]] = (f, m)
        for name, (_, ms) in self._measure_defs().items():
            if ms.get("create_metric") and name not in out:
                out[name] = (None, {"name": name, "type": "simple",
                                    "type_params": {"measure": name}})
        return out

    def _base(self, measure: str, where: str):
        if measure not in self.measure_defs:
            raise ModelError(f"{where}: unknown measure {measure!r}")
        sname, ms = self.measure_defs[measure]
        table = self.cols[sname]["table"]
        agg = ms.get("agg")
        if agg in ("sum_boolean", "median", "percentile"):
            raise refuse(f"{agg} measure", f"measure {measure!r}")
        for key in ("non_additive_dimension", "agg_params"):
            if ms.get(key):
                raise refuse(key, f"measure {measure!r}")
        if agg not in DBT_TO_AGG:
            raise ModelError(f"measure {measure!r}: unknown agg {agg!r}")
        text = str(ms.get("expr", ms["name"])).strip()
        src = self.cols[sname]["src"]
        if agg == "count" and text == "1":
            return table, Agg("count_rows", table=table)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", text):
            return table, Agg(DBT_TO_AGG[agg], Col(table, src[text]))
        row = sql_expr(text, lambda n: self._own(table, src, n), f"measure {measure!r}")
        if agg == "count_distinct":
            raise refuse("count_distinct over an expression", f"measure {measure!r}")
        return table, IterAgg(DBT_TO_AGG[agg], table, row)

    def _filters(self, spec, where: str) -> list:
        if spec is None:
            return []
        texts = [spec] if isinstance(spec, str) else list(spec)
        out = []
        for t in texts:
            out += [KeepFilters(f) for f in parse_filter(str(t), self.resolve_dim)]
        return out

    def resolve_dim(self, name: str) -> Col:
        return _resolve_item(name, self.entity_table, self.cols_by_table(), self.key, self.date)

    def cols_by_table(self) -> dict[str, dict[str, str]]:
        return {v["table"]: v["dims"] for v in self.cols.values()}

    def _input(self, spec, where: str):
        """A metric input of a ratio / derived metric -> (key, Expr, metric name)."""
        if isinstance(spec, str):
            spec = {"name": spec}
        name = spec["name"]
        if name not in self.metric_defs:
            raise ModelError(f"{where}: unknown metric {name!r}")
        e = MeasureRef(self.labels[name])
        filters = self._filters(spec.get("filter"), where)
        if spec.get("offset_to_grain"):
            raise refuse("offset_to_grain", f"{where}: MetricFlow's offset_to_grain is the "
                         "value at the start of the period, not a period-to-date")
        if filters:
            e = Calculate(e, tuple(filters))
        offset = spec.get("offset_window")
        if offset is not None:
            if not re.fullmatch(r"\s*1\s+years?\s*", str(offset)):
                raise refuse(f"offset_window {offset}", f"{where}: only 1 year lowers "
                             "(same_period_last_year)")
            if self.date is None:
                raise refuse("offset_window without a time spine", where)
            if any(f.modifier.column.table in self._date_tables() for f in filters):
                raise refuse("a date-table filter on an offset input", where)
            e = TimeShift(e, Col(self.date, self.tables[self.date].date_column),
                          "same_period_last_year")
        for k in ("fill_nulls_with", "join_to_timespine"):
            if spec.get(k) is not None:
                raise refuse(k, where)
        return spec.get("alias") or name, e, name

    def _date_tables(self) -> set[str]:
        return set(C.Graph(Model("", tuple(self.tables.values()),
                                 tuple(self.relationships))).expanded(self.date))

    def _measure(self, name: str) -> Measure:
        f, m = self.metric_defs[name]
        where = f"metric {name!r}"
        kind = m.get("type")
        tp = m.get("type_params") or {}
        meta = ((m.get("config") or {}).get("meta") or {})
        filters = self._filters(m.get("filter"), where)
        if kind == "simple":
            spec = tp.get("measure")
            spec = {"name": spec} if isinstance(spec, str) else (spec or {})
            for k in ("fill_nulls_with", "join_to_timespine"):
                if spec.get(k) is not None:
                    raise refuse(k, where)
            home, e = self._base(spec.get("name"), where)
            mods = self._filters(spec.get("filter"), where) + filters
            if mods:
                e = Calculate(e, tuple(mods))
        elif kind == "cumulative":
            spec = tp.get("measure")
            spec = {"name": spec} if isinstance(spec, str) else (spec or {})
            home, e = self._base(spec.get("name"), where)
            ctp = dict(tp.get("cumulative_type_params") or {})
            for k in ("window", "grain_to_date"):
                if tp.get(k) is not None:
                    ctp.setdefault(k, tp[k])
            if ctp.get("window"):
                raise refuse("cumulative window", f"{where}: a rolling window is not a "
                             "period-to-date")
            grain = ctp.get("grain_to_date")
            if grain not in GRAIN_TO_KIND:
                raise refuse("cumulative over all time" if grain is None
                             else f"cumulative grain_to_date {grain}", where)
            agg = ctp.get("period_agg", "first")
            if agg != "last":
                raise refuse(f"cumulative period_agg {agg}", f"{where}: the semantic core's "
                             "ytd/qtd/mtd is the value at the period's last date "
                             "(period_agg: last)")
            if self.date is None:
                raise refuse("cumulative metric without a time spine", where)
            mods = self._filters(spec.get("filter"), where) + filters
            if any(x.modifier.column.table in self._date_tables() for x in filters):
                raise refuse("a date-table filter on a cumulative metric", where)
            if mods:
                e = Calculate(e, tuple(mods))
            e = TimeShift(e, Col(self.date, self.tables[self.date].date_column),
                          GRAIN_TO_KIND[grain])
        elif kind == "ratio":
            _, num, nname = self._input(tp.get("numerator"), where)
            _, den, _ = self._input(tp.get("denominator"), where)
            e = Divide(num, den)
            if filters:
                e = Calculate(e, tuple(filters))
            home = self._home(nname)
        elif kind == "derived":
            inputs = {}
            first = None
            for spec in tp.get("metrics") or []:
                key, ie, iname = self._input(spec, where)
                if key in inputs:
                    raise ModelError(f"{where}: input {key!r} twice (give one an alias)")
                inputs[key] = ie
                first = first or iname
            if first is None:
                raise ModelError(f"{where}: a derived metric with no input metrics")

            def resolve(n, inputs=inputs):
                if n not in inputs:
                    raise ModelError(f"{where}: {n!r} is not one of its input metrics")
                return inputs[n]
            e = sql_expr(str(tp.get("expr", "")), resolve, where)
            if filters:
                if any(isinstance(x, TimeShift) for x in C.walk(e)):
                    raise refuse("a filter on a derived metric with an offset input", where)
                e = Calculate(e, tuple(filters))
            home = self._home(first)
        elif kind == "conversion":
            raise refuse("conversion metric", where)
        else:
            raise ModelError(f"{where}: unknown metric type {kind!r}")
        if m.get("time_granularity"):
            raise refuse("metric time_granularity", where)
        table = str(meta.get("home_table") or home)
        if table not in self.tables:
            raise ModelError(f"{where}: home_table {table!r} is not a table")
        self.homes[name] = home
        return Measure(self.labels[name], table, e, format_string=meta.get("format"),
                       description=str(m.get("description") or ""))

    def _home(self, metric: str) -> str:
        if metric in self.homes:
            return self.homes[metric]
        f, m = self.metric_defs[metric]
        tp = m.get("type_params") or {}
        kind = m.get("type")
        if kind in ("simple", "cumulative"):
            spec = tp.get("measure")
            spec = spec if isinstance(spec, str) else (spec or {}).get("name")
            if spec not in self.measure_defs:
                raise ModelError(f"metric {metric!r}: unknown measure {spec!r}")
            return self.cols[self.measure_defs[spec][0]]["table"]
        if kind == "ratio":
            num = tp.get("numerator")
            return self._home(num if isinstance(num, str) else num["name"])
        if kind == "derived":
            first = (tp.get("metrics") or [{}])[0]
            return self._home(first if isinstance(first, str) else first["name"])
        raise ModelError(f"metric {metric!r}: unknown type {kind!r}")

    # -- dashboards ------------------------------------------------------------
    def _reports(self) -> tuple[Report, ...]:
        out = []
        base = self.root / DASHBOARD_DIR
        for f in sorted(base.glob("*.yml")) + sorted(base.glob("*.yaml")) if base.exists() else []:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            for r in doc.get("dashboards") or []:
                out.append(self._report(r, f))
        return tuple(out)

    def _report(self, r: dict, f: Path) -> Report:
        pages = []
        for p in r.get("pages") or []:
            pages.append(Page(str(p["name"]), tuple(self._visual(v, f)
                                                    for v in p.get("visuals") or [])))
        return Report(str(r["name"]), str(r.get("model", self.project.name)), tuple(pages))

    def _visual(self, v: dict, f: Path) -> Visual:
        name = str(v["name"])
        kind = str(v.get("kind", "table"))
        groups = str(v.get("groups", "measures"))
        if v.get("sql") is not None:
            return Visual(name, kind=kind, sql=str(v["sql"]), groups=groups)
        where = f"visual {name!r} ({f.name})"
        by_name = {mname: self.labels[mname] for mname in self.metric_defs}
        values, tokens = [], {}
        for item in v.get("metrics") or []:
            spec = {"name": item} if isinstance(item, str) else dict(item)
            mname = spec["name"]
            if mname not in by_name:
                raise ModelError(f"{where}: unknown metric {mname!r}")
            label = str(spec.get("label") or by_name[mname])
            values.append((label, MeasureRef(by_name[mname])))
            tokens[mname if "label" not in spec else label] = label
        group = []
        for item in v.get("group_by") or []:
            col = _resolve_item(str(item), self.entity_table, self.cols_by_table(), self.key,
                                self.date)
            group.append(col)
            tokens[str(item)] = col.column
        filters = []
        for text in v.get("where") or []:
            filters += parse_filter(str(text), self.resolve_dim)
        order = []
        for tok in v.get("order_by") or []:
            tok = str(tok)
            desc = tok.startswith("-")
            key = tok[1:] if desc else tok
            if key not in tokens:
                raise ModelError(f"{where}: order_by {tok!r} is not a metric or group_by item")
            order.append((tokens[key], desc))
        return Visual(name, group_by=tuple(group), values=tuple(values), filters=tuple(filters),
                      order_by=tuple(order), top_n=v.get("limit"), kind=kind, groups=groups)


def _resolve_item(item: str, entity_table: dict, dims: dict, keys: dict,
                  date: str | None) -> Col:
    """A MetricFlow group-by item / filter dimension -> a model column:
    `metric_time__<grain>`, `entity__dimension` (or a multi-hop path - the last
    entity names the table), or a bare `entity` (its key)."""
    parts = item.split("__")
    if parts[0] == "metric_time":
        grain = parts[1] if len(parts) > 1 else "day"
        if len(parts) > 2 or grain not in GRAINS:
            raise refuse(f"metric_time grain {grain}", item)
        if date is None:
            raise ModelError(f"{item!r}: no time spine")
        return Col(date, f"metric_time__{grain}")
    if len(parts) == 1:
        table = entity_table.get(parts[0])
        if table is None or table not in keys:
            raise ModelError(f"unknown entity {parts[0]!r}")
        return Col(table, keys[table])
    if len(parts) >= 3 and parts[-1] in GRAINS:
        table = entity_table.get(parts[-3])
        if table is not None and parts[-2] in dims.get(table, {}):
            if parts[-1] != "day":
                raise refuse("a time dimension at a grain (only metric_time has grains here)",
                             item)
            return Col(table, dims[table][parts[-2]])
    entity, dim = parts[-2], parts[-1]
    table = entity_table.get(entity)
    if table is None:
        raise ModelError(f"{item!r}: unknown entity {entity!r}")
    if dim not in dims.get(table, {}):
        raise ModelError(f"{item!r}: {table!r} has no dimension {dim!r}")
    return Col(table, dims[table][dim])


def _data_type(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"\(.*\)$", "", t).strip()
    for dtype, names in DBT_TYPE_ALIASES.items():
        if t in names:
            return dtype
    raise ModelError(f"unknown data_type {text!r}")


# ----------------------------------------------------------------- public API
def read_project(path: Path, *, warehouse: Path | None = None
                 ) -> tuple[Model, tuple[Report, ...]]:
    """A dbt project directory -> (Model, reports). See the module docstring.
    `warehouse` is accepted and ignored: dimension types live in the YAML."""
    del warehouse
    return _Reader(Path(path)).run()


def write_project(model: Model, reports: tuple[Report, ...], dest: Path) -> list[Path]:
    """Write the model (and reports) as dbt semantic-layer YAML under the dbt
    project `dest`; returns the written paths, sorted. Same inputs, same bytes."""
    return _Writer(model, tuple(reports), Path(dest)).run()


def _model_names(model: Model):
    date = next((t for t in model.tables if t.is_date_table), None)
    entity_table = {ident(t.name): t.name for t in model.tables}
    dims = {t.name: {ident(c.name): c.name for c in t.columns
                     if not c.name.startswith("metric_time__")} for t in model.tables}
    keys = {}
    for r in model.relationships:
        keys[r.to_table] = r.to_column
    return date, entity_table, dims, keys


def parse_expression(text: str, model: Model, *, table: str | None = None):
    """A metric filter (text with `{{ Dimension(...) }}`) -> FilterValues /
    FilterCompare (one predicate; AND of several is a list in YAML), or a derived
    metric's `expr` over metric names -> Expr. Names follow write_project's
    spelling: a table's entity and semantic model are its snake_case name.
    `table` is accepted and ignored: MetricFlow text names its tables itself."""
    del table
    date, entity_table, dims, keys = _model_names(model)
    if "{{" in text:
        out = parse_filter(text, lambda n: _resolve_item(
            n, entity_table, dims, keys, None if date is None else date.name))
        if len(out) != 1:
            raise refuse("a filter with AND", "parse_expression returns one predicate")
        return out[0]
    by_metric = {ident(m.name): m.name for m in model.measures}

    def resolve(n):
        if n not in by_metric:
            raise ModelError(f"unknown metric {n!r}")
        return MeasureRef(by_metric[n])
    return sql_expr(text, resolve, "expression")


def format_expression(expr, model: Model, *, table: str | None = None) -> str:
    """The inverse of parse_expression: a filter predicate (or KeepFilters of one)
    as filter text, else an expression over MeasureRefs as derived-metric SQL.
    `table` is accepted and ignored."""
    del table
    date, entity_table, dims, keys = _model_names(model)

    def dim_ref(col: Col) -> str:
        if date is not None and col.table == date.name and col.column.startswith("metric_time__"):
            return f"{{{{ TimeDimension('metric_time', '{col.column.split('__', 1)[1]}') }}}}"
        return f"{{{{ Dimension('{ident(col.table)}__{ident(col.column)}') }}}}"

    if isinstance(expr, KeepFilters):
        expr = expr.modifier
    if isinstance(expr, (FilterValues, FilterCompare)):
        return format_filter(expr, dim_ref)

    def leaf(x):
        if isinstance(x, MeasureRef):
            return ident(x.name)
        raise refuse(f"{type(x).__name__} in a derived metric expression",
                     "format_expression spells expressions over metrics only")
    return expr_sql(expr, leaf)


__all__ = ["VENDOR", "format_expression", "metric_time_columns", "parse_expression",
           "read_project", "write_project"]
