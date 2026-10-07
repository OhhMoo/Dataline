"""The vetted Databricks SQL surface: what the lakehouse translates, and how.

FAIL-CLOSED. `databricks_sql.translate` runs every statement's parsed tree through
`vet` before it generates DuckDB SQL. Every function node must be on `FUNCTIONS` (a
sqlglot class) or `NAMED` (a function sqlglot does not know, by name), and every other
expression node must be on `EXPRESSIONS`; anything else is refused naming it, never
translated on trust. sqlglot's `databricks` -> `duckdb` translation is right for most
of SQL and quietly wrong for some of it (DuckDB rounds a CAST to INT, counts Sunday as
day 0, returns a TIMESTAMP from date_add, counts month boundaries in timestampdiff, and
takes `substring(s, 0, n)` one character short), so nothing reaches the engine on the
strength of the translator alone.

An entry is one of:

  vetted as translated   sqlglot's DuckDB form returns what Databricks returns;
  rewritten              `rewrite` builds the DuckDB form here, with Databricks'
                         semantics (the integral CAST truncates, dayofweek counts Sunday
                         as 1, date_add returns a DATE, timestampdiff counts whole units
                         the way java.time does, substring treats position 0 as 1,
                         division by zero raises DIVIDE_BY_ZERO as an ANSI warehouse
                         does, current_date is the WORLD's clock);
  checked                `check` refuses, by name, the argument shapes that are not
                         vetted (a datetime format with tokens beyond yyyy/yy/MM/dd/HH/mm/
                         ss, an EXTRACT field, a truncation unit, a CAST target type).

Every entry has a case in gateway/tests/test_databricks_sql.py's FUNCTION_CORPUS, which
asserts the result Databricks documents for it; a test fails when an entry has none.

The rewrites call DuckDB TEMP macros (`MACROS`), created on the lakehouse connection
before its configuration is locked. A statement cannot call them itself: their names
are on no list.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlglot import exp

#: Marks a node this module built, so the post-order rewrite never rewrites it twice.
GENERATED = "dbx_generated"


class Refused(Exception):
    """An expression the lakehouse does not translate: (feature, why)."""

    def __init__(self, feature: str, why: str):
        super().__init__(feature)
        self.feature = feature
        self.why = why


@dataclass(frozen=True)
class Rule:
    """How one function is answered: as sqlglot translates it, or rewritten."""

    name: str                                             # the Databricks spelling
    rewrite: Callable[..., exp.Expression] | None = None  # (node, context) -> DuckDB form
    check: Callable[[exp.Expression], None] | None = None  # raises Refused


# ----------------------------------------------------------------- the macros
_DIVIDE_BY_ZERO = ("[DIVIDE_BY_ZERO] Division by zero. Use `try_divide` to tolerate "
                   "divisor being 0 and return NULL instead. SQLSTATE: 22012")
#: DuckDB TEMP macros the rewrites call. Spark's substring index rules; java.time's
#: ChronoUnit.between (what Databricks' timestampdiff uses): the end date steps back a
#: day when its time of day is before the start's, months are (packed month*32+day)
#: differences divided by 32, truncating toward zero, time units are whole durations.
MACROS = (
    "CREATE TEMP MACRO dbx_divisor(b) AS (CASE WHEN b = 0 THEN error("
    f"'{_DIVIDE_BY_ZERO}') ELSE b END)",
    "CREATE TEMP MACRO dbx_substring(s, p, l) AS (CASE"
    " WHEN greatest(CASE WHEN p > 0 THEN p - 1 WHEN p < 0 THEN length(s) + p ELSE 0 END,"
    " 0) >= (CASE WHEN p > 0 THEN p - 1 WHEN p < 0 THEN length(s) + p ELSE 0 END) + l"
    " THEN '' ELSE substring(s, greatest(CASE WHEN p > 0 THEN p - 1 WHEN p < 0"
    " THEN length(s) + p ELSE 0 END, 0) + 1, (CASE WHEN p > 0 THEN p - 1 WHEN p < 0"
    " THEN length(s) + p ELSE 0 END) + l - greatest(CASE WHEN p > 0 THEN p - 1"
    " WHEN p < 0 THEN length(s) + p ELSE 0 END, 0)) END)",
    "CREATE TEMP MACRO dbx_substring_from(s, p) AS (substring(s, greatest(CASE"
    " WHEN p > 0 THEN p - 1 WHEN p < 0 THEN length(s) + p ELSE 0 END, 0) + 1))",
    "CREATE TEMP MACRO dbx_ts(x) AS (CAST(x AS TIMESTAMP))",
    "CREATE TEMP MACRO dbx_end_date(s, e) AS (CASE"
    " WHEN CAST(dbx_ts(e) AS DATE) > CAST(dbx_ts(s) AS DATE)"
    " AND CAST(dbx_ts(e) AS TIME) < CAST(dbx_ts(s) AS TIME)"
    " THEN CAST(dbx_ts(e) AS DATE) - 1"
    " WHEN CAST(dbx_ts(e) AS DATE) < CAST(dbx_ts(s) AS DATE)"
    " AND CAST(dbx_ts(e) AS TIME) > CAST(dbx_ts(s) AS TIME)"
    " THEN CAST(dbx_ts(e) AS DATE) + 1 ELSE CAST(dbx_ts(e) AS DATE) END)",
    "CREATE TEMP MACRO dbx_packed(d) AS ((year(d) * 12 + month(d)) * 32 + day(d))",
    "CREATE TEMP MACRO dbx_months(s, e) AS"
    " ((dbx_packed(dbx_end_date(s, e)) - dbx_packed(CAST(dbx_ts(s) AS DATE))) // 32)",
    "CREATE TEMP MACRO dbx_days(s, e) AS (dbx_end_date(s, e) - CAST(dbx_ts(s) AS DATE))",
    "CREATE TEMP MACRO dbx_micros(s, e) AS (epoch_us(dbx_ts(e)) - epoch_us(dbx_ts(s)))",
)


# ----------------------------------------------------------------- builders
def _mark(node: exp.Expression) -> exp.Expression:
    node.meta[GENERATED] = True
    return node


def _fn(name: str, *args: exp.Expression) -> exp.Expression:
    return _mark(exp.Anonymous(this=name, expressions=list(args)))


def _cast(node: exp.Expression, to: str, *, try_: bool = False) -> exp.Expression:
    kind = exp.TryCast if try_ else exp.Cast
    return _mark(kind(this=node, to=exp.DataType.build(to, dialect="duckdb")))


def _int(value: int) -> exp.Expression:
    return exp.Literal.number(value)


def _unit(node: exp.Expression, arg: str = "unit") -> str:
    unit = node.args.get(arg)
    return (unit.name if isinstance(unit, (exp.Var, exp.Literal, exp.Column)) else
            str(unit or "")).upper()


def _literal(node: exp.Expression | None) -> str | None:
    return node.name if isinstance(node, exp.Literal) and node.is_string else None


# ----------------------------------------------------------------- checks
#: sqlglot keeps the Java pattern as strftime codes, some marked `strict`.
_FORMAT_OK = re.compile(r"(%[YymdHMS](?:strict)?|[-/:. ,_T])*")


def _check_format(node: exp.Expression) -> None:
    """A datetime format whose every token is yyyy/yy/MM/dd/HH/mm/ss (sqlglot has
    already turned the Java pattern into strftime codes) and the rest separators."""
    fmt = node.args.get("format")
    if fmt is None:
        return
    text = _literal(fmt)
    if text is None or not _FORMAT_OK.fullmatch(text):
        raise Refused(f"datetime format {fmt.sql()}",
                      "vetted pattern letters: yyyy, yy, MM, dd, HH, mm, ss and "
                      "separators")


_CAST_TYPES = {
    exp.DataType.Type.TINYINT, exp.DataType.Type.SMALLINT, exp.DataType.Type.INT,
    exp.DataType.Type.BIGINT, exp.DataType.Type.DECIMAL, exp.DataType.Type.DOUBLE,
    exp.DataType.Type.FLOAT, exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR,
    exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIMESTAMPTZ,
    exp.DataType.Type.BOOLEAN,
}
_INTEGRAL = {exp.DataType.Type.TINYINT, exp.DataType.Type.SMALLINT, exp.DataType.Type.INT,
             exp.DataType.Type.BIGINT}


def _check_cast(node: exp.Expression) -> None:
    to = node.args.get("to")
    if not isinstance(to, exp.DataType) or to.this not in _CAST_TYPES:
        raise Refused(f"CAST to {to.sql(dialect='databricks') if to else '?'}",
                      "vetted targets: tinyint, smallint, int, bigint, decimal(p, s), "
                      "double, float, string, date, timestamp, boolean")
    if to.this == exp.DataType.Type.DECIMAL and not to.expressions:
        # `CAST(x AS DECIMAL)` is DECIMAL(10,0) in Databricks, but a `1.5BD` literal
        # parses to the same node and is exact: the two cannot be told apart.
        raise Refused("CAST to DECIMAL without a precision (or a BD literal)",
                      "write DECIMAL(p, s)")


_EXTRACT = {
    "YEAR": "year", "YEARS": "year", "Y": "year", "YR": "year", "YRS": "year",
    "QUARTER": "quarter", "QTR": "quarter",
    "MONTH": "month", "MONTHS": "month", "MON": "month", "MONS": "month",
    "WEEK": "weekofyear", "WEEKS": "weekofyear", "W": "weekofyear",
    "DAY": "day", "DAYS": "day", "D": "day",
    "DAYOFWEEK": "dow", "DOW": "dow", "DAYOFWEEK_ISO": "isodow", "DOW_ISO": "isodow",
    "DAYOFWEEKISO": "isodow", "DOY": "dayofyear",
    "HOUR": "hour", "HOURS": "hour", "H": "hour", "HR": "hour", "HRS": "hour",
    "MINUTE": "minute", "MINUTES": "minute", "MIN": "minute", "MINS": "minute",
    "M": "minute", "SECOND": "second", "SECONDS": "second", "S": "second",
    "SEC": "second", "SECS": "second",
}


def _check_extract(node: exp.Expression) -> None:
    if _unit(node, "this") not in _EXTRACT:
        raise Refused(f"EXTRACT({_unit(node, 'this')})",
                      f"vetted fields: {', '.join(sorted(set(_EXTRACT)))}")


_TRUNC_DATE = {"YEAR": "year", "YYYY": "year", "YY": "year", "QUARTER": "quarter",
               "MONTH": "month", "MM": "month", "MON": "month", "WEEK": "week"}
_TRUNC_TS = {**_TRUNC_DATE, "DAY": "day", "DD": "day", "HOUR": "hour",
             "MINUTE": "minute", "SECOND": "second", "MILLISECOND": "millisecond",
             "MICROSECOND": "microsecond"}


def _check_unit(table: dict[str, str], what: str):
    def check(node: exp.Expression) -> None:
        if _unit(node) not in table:
            raise Refused(f"{what} unit {_unit(node) or '?'}",
                          f"vetted units: {', '.join(sorted(table))}")
    return check


_DIFF_UNITS = {"YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "HOUR", "MINUTE", "SECOND",
               "MILLISECOND", "MICROSECOND"}
_ADD_UNITS = _DIFF_UNITS


def _check_args(*allowed: str):
    def check(node: exp.Expression) -> None:
        extra = [k for k, v in node.args.items() if v not in (None, False) and
                 k not in allowed]
        if extra:
            raise Refused(f"{_name(node)} with {', '.join(extra)}",
                          "only the vetted argument forms are translated")
    return check


def _check_like(node: exp.Expression) -> None:
    pattern = _literal(node.args.get("expression"))
    if pattern is not None and "\\" in pattern:
        raise Refused("LIKE with an escape sequence", "vetted patterns use % and _ only")


# ----------------------------------------------------------------- rewrites
def _integral_cast(node: exp.Expression, _context) -> exp.Expression:
    """CAST to an integral type truncates toward zero in Databricks (DuckDB rounds):
    through DECIMAL(38,18), TRUNC, then the target type."""
    to = node.args["to"]
    if to.this not in _INTEGRAL:
        return node
    try_ = isinstance(node, exp.TryCast)
    inner = _cast(node.this, "DECIMAL(38, 18)", try_=try_)
    return _mark(type(node)(this=_fn("trunc", inner), to=to))


def _named_cast(to: str):
    def rewrite(node: exp.Expression, _context) -> exp.Expression:
        if len(node.expressions) != 1:
            raise Refused(f"{node.name}() with {len(node.expressions)} arguments",
                          "a cast function takes one")
        return _integral_cast(_mark(exp.Cast(this=node.expressions[0],
                                             to=exp.DataType.build(to))), None)
    return rewrite


def _day_of_week(node: exp.Expression, _context) -> exp.Expression:
    """dayofweek: Sunday 1 .. Saturday 7 (DuckDB: Sunday 0)."""
    return _mark(exp.Paren(this=_mark(exp.Add(
        this=_mark(exp.Mod(this=_fn("isodow", node.this), expression=_int(7))),
        expression=_int(1)))))


def _weekday(node: exp.Expression, _context) -> exp.Expression:
    """weekday: Monday 0 .. Sunday 6 (DuckDB's weekday is dayofweek)."""
    if len(node.expressions) != 1:
        raise Refused("weekday() with other than one argument", "weekday takes a date")
    return _mark(exp.Paren(this=_mark(exp.Sub(this=_fn("isodow", node.expressions[0]),
                                              expression=_int(1)))))


def _extract(node: exp.Expression, _context) -> exp.Expression:
    target = _EXTRACT[_unit(node, "this")]
    value = node.args["expression"]
    if target == "dow":
        return _day_of_week(exp.DayOfWeek(this=value), None)
    if target == "second":
        # seconds with their fraction, DECIMAL(8,6), as Databricks returns them
        return _cast(_mark(exp.Div(this=_fn("microsecond", value),
                                   expression=exp.Literal.number("1000000.0"))),
                     "DECIMAL(8, 6)")
    return _fn(target, value)


def _date_add(node: exp.Expression, _context) -> exp.Expression:
    """date_add / date_sub: a DATE plus whole days is a DATE."""
    return _cast(_mark(exp.Add(this=_cast(node.this, "DATE"),
                               expression=_cast(node.expression, "INTEGER"))), "DATE")


def _timestamp_add(node: exp.Expression, _context) -> exp.Expression:
    """timestampadd/dateadd(unit, n, ts): a TIMESTAMP, months clamped to month end."""
    unit = _unit(node)
    count = node.expression
    if unit in ("QUARTER", "YEAR"):
        count = _mark(exp.Mul(this=_cast(count, "BIGINT"),
                              expression=_int(3 if unit == "QUARTER" else 12)))
        unit = "MONTH"
    interval = _fn("to_" + {"MONTH": "months", "WEEK": "weeks", "DAY": "days",
                            "HOUR": "hours", "MINUTE": "minutes", "SECOND": "seconds",
                            "MILLISECOND": "milliseconds",
                            "MICROSECOND": "microseconds"}[unit],
                   _cast(count, "INTEGER" if unit in ("MONTH", "WEEK", "DAY") else
                         "BIGINT"))
    return _mark(exp.Add(this=_cast(node.this, "TIMESTAMPTZ"), expression=interval))


def _timestamp_diff(node: exp.Expression, _context) -> exp.Expression:
    """timestampdiff / datediff(unit, start, end): whole units, java.time's way."""
    start, end = node.expression, node.this
    unit = _unit(node) or "DAY"
    if unit in ("YEAR", "QUARTER", "MONTH"):
        months = _fn("dbx_months", start, end)
        if unit == "MONTH":
            return months
        return _mark(exp.IntDiv(this=months, expression=_int(12 if unit == "YEAR" else 3)))
    if unit in ("DAY", "WEEK"):
        if node.args.get("unit") is None or isinstance(node, exp.DateDiff) and \
                _two_arg_datediff(node):
            # datediff(end, start): the difference of the two DATES
            return _mark(exp.Sub(this=_cast(end, "DATE"), expression=_cast(start, "DATE")))
        days = _fn("dbx_days", start, end)
        return days if unit == "DAY" else _mark(exp.IntDiv(this=days, expression=_int(7)))
    per = {"HOUR": 3_600_000_000, "MINUTE": 60_000_000, "SECOND": 1_000_000,
           "MILLISECOND": 1_000, "MICROSECOND": 1}[unit]
    micros = _fn("dbx_micros", start, end)
    return micros if per == 1 else _mark(exp.IntDiv(this=micros, expression=_int(per)))


def _two_arg_datediff(node: exp.Expression) -> bool:
    return bool(node.meta.get("dbx_two_arg"))


def _trunc(table: dict[str, str], to: str):
    def rewrite(node: exp.Expression, _context) -> exp.Expression:
        return _cast(_fn("date_trunc", exp.Literal.string(table[_unit(node)]),
                         _cast(node.this, "TIMESTAMPTZ")), to)
    return rewrite


def _substring(node: exp.Expression, _context) -> exp.Expression:
    start = node.args.get("start") or _int(1)
    length = node.args.get("length")
    if length is None:
        return _fn("dbx_substring_from", node.this, start)
    return _fn("dbx_substring", node.this, start, length)


def _left(node: exp.Expression, _context) -> exp.Expression:
    return _fn("dbx_substring", node.this, _int(1), node.expression)


def _right(node: exp.Expression, _context) -> exp.Expression:
    """right(s, n): '' for n <= 0, else the last n characters (all, when fewer)."""
    n = node.expression
    return _mark(exp.Case(ifs=[_mark(exp.If(this=_mark(exp.LTE(this=n, expression=_int(0))),
                                            true=exp.Literal.string("")))],
                          default=_fn("dbx_substring_from", node.this, _mark(exp.Neg(this=n)))))


def _to_date(node: exp.Expression, _context) -> exp.Expression:
    """to_date(x) without a format: the date part of a date, timestamp or timestamp
    string ('2009-07-30 04:17:52' is 2009-07-30; DuckDB's DATE cast refuses it)."""
    if node.args.get("format") is not None:
        return node
    return _cast(_cast(node.this, "TIMESTAMP"), "DATE")


def _quantile(node: exp.Expression, _context) -> exp.Expression:
    """percentile(x, p): exact, linear interpolation (DuckDB's quantile is discrete)."""
    return _fn("quantile_cont", node.this, node.args["quantile"])


def _divisor(node: exp.Expression, _context) -> exp.Expression:
    """/, div, %: a zero divisor raises DIVIDE_BY_ZERO, as on an ANSI warehouse
    (DuckDB answers inf or NULL)."""
    node.set("expression", _fn("dbx_divisor", node.expression))
    return node


def _session(field: str):
    def rewrite(_node: exp.Expression, context) -> exp.Expression:
        return _mark(exp.Literal.string(getattr(context, field)))
    return rewrite


def _version(_node: exp.Expression, _context) -> exp.Expression:
    return _mark(exp.Literal.string("alkera-lakehouse 1 (Databricks SQL tier 1, DuckDB)"))


def _now(to: str):
    """current_date / current_timestamp / now: the WORLD's clock (the session's
    `now`, the episode's virtual time), never the host's."""
    def rewrite(_node: exp.Expression, context) -> exp.Expression:
        if not getattr(context, "now", None):
            raise Refused("current_date/current_timestamp without a world clock",
                          "this session has no virtual time")
        now = str(context.now)
        return _cast(exp.Literal.string(now[:10] if to == "DATE" else
                                        now.replace("Z", "+00:00")), to)
    return rewrite


# ----------------------------------------------------------------- the lists
def _vetted(name: str, **kw) -> Rule:
    return Rule(name, **kw)


FUNCTIONS: dict[type, Rule] = {
    # aggregates
    exp.Count: _vetted("count"), exp.Sum: _vetted("sum"), exp.Avg: _vetted("avg"),
    exp.Min: _vetted("min"), exp.Max: _vetted("max"), exp.CountIf: _vetted("count_if"),
    exp.AnyValue: _vetted("any_value"), exp.Stddev: _vetted("stddev"),
    exp.StddevSamp: _vetted("stddev_samp"), exp.StddevPop: _vetted("stddev_pop"),
    exp.Variance: _vetted("variance"), exp.VariancePop: _vetted("var_pop"),
    exp.Median: _vetted("median"), exp.Quantile: Rule("percentile", rewrite=_quantile),
    exp.ArrayAgg: _vetted("collect_list"), exp.ArrayUniqueAgg: _vetted("collect_set"),
    exp.LogicalAnd: _vetted("bool_and"), exp.LogicalOr: _vetted("bool_or"),
    exp.ArgMax: _vetted("max_by"), exp.ArgMin: _vetted("min_by"),
    # window
    exp.RowNumber: _vetted("row_number"), exp.Rank: _vetted("rank"),
    exp.DenseRank: _vetted("dense_rank"), exp.Lag: _vetted("lag"),
    exp.Lead: _vetted("lead"), exp.Ntile: _vetted("ntile"),
    exp.PercentRank: _vetted("percent_rank"), exp.CumeDist: _vetted("cume_dist"),
    exp.FirstValue: _vetted("first_value"), exp.LastValue: _vetted("last_value"),
    # conditional
    exp.Case: _vetted("case"), exp.If: _vetted("if"), exp.Coalesce: _vetted("coalesce"),
    exp.Exists: _vetted("exists"),
    exp.Nullif: _vetted("nullif"), exp.Nvl2: _vetted("nvl2"),
    exp.Greatest: _vetted("greatest"), exp.Least: _vetted("least"),
    # math
    exp.Abs: _vetted("abs"), exp.Round: _vetted("round"), exp.Floor: _vetted("floor"),
    exp.Ceil: _vetted("ceil"), exp.Sqrt: _vetted("sqrt"), exp.Pow: _vetted("power"),
    exp.Ln: _vetted("ln"), exp.Log: _vetted("log"), exp.Exp: _vetted("exp"),
    exp.Sign: _vetted("sign"),
    # strings
    exp.Lower: _vetted("lower"), exp.Upper: _vetted("upper"),
    exp.Length: _vetted("length"), exp.Trim: _vetted("trim"),
    exp.Concat: _vetted("concat"), exp.ConcatWs: _vetted("concat_ws"),
    exp.Replace: _vetted("replace"),
    exp.RegexpReplace: Rule("regexp_replace", check=_check_args(
        "this", "expression", "replacement", "modifiers")),
    exp.RegexpExtract: Rule("regexp_extract", check=_check_args(
        "this", "expression", "group", "null_if_pos_overflow")),
    exp.RegexpLike: _vetted("rlike"), exp.Pad: _vetted("lpad/rpad"),
    exp.Initcap: _vetted("initcap"),
    exp.StrPosition: Rule("instr/locate", check=_check_args("this", "substr")),
    exp.Substring: Rule("substring", rewrite=_substring),
    exp.Left: Rule("left", rewrite=_left), exp.Right: Rule("right", rewrite=_right),
    # arrays
    exp.Array: _vetted("array"), exp.ArraySize: _vetted("size"),
    exp.ArrayContains: _vetted("array_contains"),
    # dates and times
    exp.CurrentDate: Rule("current_date", rewrite=_now("DATE")),
    exp.CurrentTimestamp: Rule("current_timestamp", rewrite=_now("TIMESTAMPTZ")),
    exp.Year: _vetted("year"), exp.Quarter: _vetted("quarter"),
    exp.Month: _vetted("month"), exp.Day: _vetted("day"),
    exp.DayOfMonth: _vetted("dayofmonth"), exp.DayOfYear: _vetted("dayofyear"),
    exp.WeekOfYear: _vetted("weekofyear"), exp.Hour: _vetted("hour"),
    exp.Minute: _vetted("minute"), exp.Second: _vetted("second"),
    exp.DayOfWeek: Rule("dayofweek", rewrite=_day_of_week),
    exp.Extract: Rule("extract", rewrite=_extract, check=_check_extract),
    exp.TsOrDsAdd: Rule("date_add/date_sub", rewrite=_date_add,
                        check=_check_unit({"DAY": "day"}, "date_add")),
    exp.TimestampAdd: Rule("timestampadd/dateadd", rewrite=_timestamp_add,
                           check=_check_unit({u: u for u in _ADD_UNITS}, "timestampadd")),
    exp.TimestampDiff: Rule("timestampdiff", rewrite=_timestamp_diff,
                            check=_check_unit({u: u for u in _DIFF_UNITS}, "timestampdiff")),
    exp.DateDiff: Rule("datediff", rewrite=_timestamp_diff,
                       check=_check_unit({u: u for u in _DIFF_UNITS}, "datediff")),
    exp.TimestampTrunc: Rule("date_trunc", rewrite=_trunc(_TRUNC_TS, "TIMESTAMPTZ"),
                             check=_check_unit(_TRUNC_TS, "date_trunc")),
    exp.DateTrunc: Rule("trunc", rewrite=_trunc(_TRUNC_DATE, "DATE"),
                        check=_check_unit(_TRUNC_DATE, "trunc")),
    exp.AddMonths: _vetted("add_months"), exp.LastDay: _vetted("last_day"),
    exp.TsOrDsToDate: Rule("to_date", rewrite=_to_date, check=_check_format),
    exp.StrToTime: Rule("to_timestamp", check=_check_format),
    exp.TimeToStr: Rule("date_format", check=_check_format),
    exp.TimeStrToTime: _vetted("(timestamp of a date_format argument)"),
    exp.Cast: Rule("cast", rewrite=_integral_cast, check=_check_cast),
    exp.TryCast: Rule("try_cast", rewrite=_integral_cast, check=_check_cast),
    # the session
    exp.CurrentCatalog: Rule("current_catalog", rewrite=_session("catalog")),
    exp.CurrentSchema: Rule("current_schema", rewrite=_session("schema")),
    exp.CurrentDatabase: Rule("current_database", rewrite=_session("schema")),
    exp.CurrentUser: Rule("current_user", rewrite=_session("user")),
    exp.SessionUser: Rule("session_user", rewrite=_session("user")),
    exp.CurrentVersion: Rule("current_version", rewrite=_version),
}

#: Functions sqlglot parses as `Anonymous`, by their Databricks name.
NAMED: dict[str, Rule] = {
    "weekday": Rule("weekday", rewrite=_weekday),
    "user": Rule("user", rewrite=_session("user")),
    "bigint": Rule("bigint", rewrite=_named_cast("BIGINT")),
    "smallint": Rule("smallint", rewrite=_named_cast("SMALLINT")),
    "tinyint": Rule("tinyint", rewrite=_named_cast("TINYINT")),
}

#: Expression nodes that are not functions: the statement structure, literals,
#: operators. Their DuckDB forms are the same operations (the arithmetic ones with
#: a checked divisor, below).
EXPRESSIONS: frozenset[type] = frozenset({
    # statements and clauses
    exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Values, exp.With, exp.CTE,
    exp.Subquery, exp.From, exp.Join, exp.Where, exp.Group, exp.Having, exp.Qualify,
    exp.Order, exp.Ordered, exp.Limit, exp.Offset, exp.Distinct, exp.Window,
    exp.WindowSpec, exp.Filter, exp.TableAlias, exp.Alias, exp.Aliases, exp.Table,
    exp.Column, exp.Identifier, exp.Star, exp.Tuple, exp.Paren, exp.Exists,
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.When, exp.Whens, exp.Create,
    exp.Drop, exp.TruncateTable, exp.Comment, exp.Schema, exp.ColumnDef, exp.DataType,
    exp.DataTypeParam, exp.ColumnConstraint, exp.NotNullColumnConstraint,
    exp.Properties, exp.TemporaryProperty, exp.PrimaryKeyColumnConstraint,
    exp.DefaultColumnConstraint, exp.EQ, exp.Var, exp.Placeholder,
    # literals
    exp.Literal, exp.Null, exp.Boolean,
    # operators
    exp.And, exp.Or, exp.Not, exp.Neg, exp.Add, exp.Sub, exp.Mul, exp.Div,
    exp.IntDiv, exp.Mod, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ,
    exp.Is, exp.In, exp.Between, exp.Like, exp.ILike, exp.DPipe, exp.Any, exp.All,
})

#: Operators answered with a rewrite, as functions are.
OPERATORS: dict[type, Rule] = {
    exp.Div: Rule("/", rewrite=_divisor), exp.IntDiv: Rule("div", rewrite=_divisor),
    exp.Mod: Rule("%", rewrite=_divisor),
    exp.Like: Rule("like", check=_check_like),
    exp.ILike: Rule("ilike", check=_check_like),
}


def _name(node: exp.Expression) -> str:
    """The function as the statement spelled it (`approx_count_distinct()`, not
    sqlglot's class name)."""
    if isinstance(node, exp.Anonymous):
        return f"{str(node.this).lower()}()"
    rule = FUNCTIONS.get(type(node))
    if rule is not None:
        return rule.name
    if isinstance(node, exp.Func):
        try:
            spelled = re.match(r"\s*([A-Za-z_]\w*)\s*\(", node.sql(dialect="databricks"))
        except Exception:  # noqa: BLE001 - a node sqlglot cannot write back
            spelled = None
        if spelled:
            return f"{spelled.group(1).lower()}()"
    return type(node).__name__


#: What a refused non-function node is called in the refusal.
_NODE_NAMES = {exp.Interval: "INTERVAL arithmetic (use date_add, date_sub, add_months "
                             "or timestampadd)",
               exp.Bracket: "array/map subscript", exp.Lambda: "lambda functions",
               exp.Dot: "struct field access", exp.HexString: "binary literals",
               exp.Struct: "struct()", exp.VarMap: "map()", exp.Pivot: "PIVOT",
               exp.Unnest: "UNNEST/explode", exp.TableSample: "TABLESAMPLE"}


def vet(tree: exp.Expression) -> None:
    """Refuse, by name, the first node of `tree` the lakehouse does not translate.
    Raises `Refused`."""
    for node in tree.walk():
        if isinstance(node, exp.Literal) and node.is_string and "\\" in node.name:
            raise Refused("backslash escape sequences in string literals",
                          "Databricks unescapes them and the engine would not")
        if type(node) in OPERATORS:
            if OPERATORS[type(node)].check is not None:
                OPERATORS[type(node)].check(node)
            continue
        if type(node) in EXPRESSIONS:      # (sqlglot makes some operators, AND, Funcs)
            continue
        if isinstance(node, exp.Func):
            rule = FUNCTIONS.get(type(node))
            if rule is None and isinstance(node, exp.Anonymous):
                rule = NAMED.get(str(node.this).lower())
            if rule is None:
                raise Refused(f"function {_name(node)}",
                              "not on the lakehouse's vetted function list "
                              "(gateway/src/alkera_gateway/databricks_functions.py)")
            if rule.check is not None:
                rule.check(node)
            continue
        raise Refused(_NODE_NAMES.get(type(node), type(node).__name__),
                      "not on the lakehouse's vetted expression list")


def rewrite(node: exp.Expression, context) -> exp.Expression:
    """The DuckDB form of one vetted node (the node itself when sqlglot's is right)."""
    if node.meta.get(GENERATED):
        return node
    if isinstance(node, exp.DataType) and node.this == exp.DataType.Type.DECIMAL \
            and not node.expressions:
        # a column declared DECIMAL is DECIMAL(10,0) in Databricks, (18,3) in DuckDB
        return _mark(exp.DataType.build("DECIMAL(10, 0)", dialect="duckdb"))
    rule = FUNCTIONS.get(type(node)) or OPERATORS.get(type(node))
    if rule is None and isinstance(node, exp.Anonymous):
        rule = NAMED.get(str(node.this).lower())
    if rule is None or rule.rewrite is None:
        return node
    return rule.rewrite(node, context)


def mark_two_arg_datediff(tree: exp.Expression, sql: str) -> None:
    """`datediff(end, start)` (two arguments: the difference of two DATES) and
    `datediff(DAY, start, end)` (whole days, java.time's way) parse to the same node.
    The source's own tokens say which: each DATEDIFF call's top-level comma count, in
    textual order, which is the order `find_all` meets the nodes in."""
    from sqlglot.dialects.databricks import Databricks
    from sqlglot.tokens import TokenType

    tokens = Databricks().tokenize(sql)
    arities = []
    for i, token in enumerate(tokens):
        if token.text.upper() != "DATEDIFF" or i + 1 >= len(tokens) or \
                tokens[i + 1].token_type != TokenType.L_PAREN:
            continue
        depth, commas = 0, 0
        for later in tokens[i + 1:]:
            if later.token_type == TokenType.L_PAREN:
                depth += 1
            elif later.token_type == TokenType.R_PAREN:
                depth -= 1
                if depth == 0:
                    break
            elif later.token_type == TokenType.COMMA and depth == 1:
                commas += 1
        arities.append(commas + 1)
    for node, arity in zip(tree.find_all(exp.DateDiff), arities, strict=False):
        if arity == 2:
            node.meta["dbx_two_arg"] = True


def vetted_count() -> int:
    return len(FUNCTIONS) + len(NAMED) + len(OPERATORS)
