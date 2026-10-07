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

TYPED. Before the rewrite, `databricks_types.Typer` gives every node the type Spark
gives it (`node.meta["dbx_type"]`), and the rewrites land the DuckDB form on that type
and on Spark's rounding: DECIMAL arithmetic in Spark's result precision and scale
(division and avg exact, HALF_UP, `dbx_decimal_quotient`), `div` as a BIGINT, an
integral CAST truncating by its input's type (a string must be an integer, as ANSI
CAST requires), a DOUBLE rounded and cast on its decimal representation (Java's
Double.toString, `java_double`), the INT-valued functions as INT. Where the type is
needed and not known, the rewrite refuses by name.

Every entry has a case in gateway/tests/test_databricks_functions.py's FUNCTION_CORPUS,
which asserts the value AND the type Databricks returns, as the JSON_ARRAY format
renders the value; a test fails when an entry has none.

The rewrites call DuckDB TEMP macros (`MACROS`) and Python functions (`UDFS`), created on
the lakehouse connection before its configuration is locked. A statement cannot call
them itself: their names are on no list.
"""
from __future__ import annotations

import datetime
import math
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

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
def _malformed(value: str, target: str) -> str:
    """The CAST_INVALID_INPUT message, as SQL building it from `value` (an expression)."""
    return ("'[CAST_INVALID_INPUT] The value ''' || " + value + " || ''' of the type "
            "\"STRING\" cannot be cast to \"' || " + target + " || '\" because it is "
            "malformed. Correct the value as per the syntax, or change its target type. "
            "Use `try_cast` to tolerate malformed input and return NULL instead. "
            "SQLSTATE: 22018'")


#: ANSI CAST of a string to an integral type: optional sign and digits, nothing else,
#: inside whitespace and control characters (UTF8String.toLongExact).
_INTEGER_TEXT = r"[\x00-\x20]*[+-]?[0-9]+[\x00-\x20]*"
_TRIM_ALL = r"^[\x00-\x20]+|[\x00-\x20]+$"

MACROS = (
    # a NULL dividend is NULL before its divisor is looked at (Spark's DivModLike)
    "CREATE TEMP MACRO dbx_divisor(a, b) AS (CASE WHEN b = 0 AND a IS NOT NULL THEN "
    f"error('{_DIVIDE_BY_ZERO}') ELSE b END)",
    "CREATE TEMP MACRO dbx_int_text(s, target) AS (CASE WHEN s IS NULL THEN NULL"
    f" WHEN regexp_full_match(s, '{_INTEGER_TEXT}') THEN regexp_extract(s, '[+-]?[0-9]+')"
    f" ELSE error({_malformed('s', 'target')}) END)",
    "CREATE TEMP MACRO dbx_try_int_text(s) AS (CASE"
    f" WHEN regexp_full_match(s, '{_INTEGER_TEXT}') THEN regexp_extract(s, '[+-]?[0-9]+')"
    " ELSE NULL END)",
    f"CREATE TEMP MACRO dbx_bool_text(s) AS (lower(regexp_replace(s, '{_TRIM_ALL}', '', 'g')))",
    "CREATE TEMP MACRO dbx_bool(s) AS (CASE WHEN s IS NULL THEN NULL"
    " WHEN dbx_bool_text(s) IN ('t', 'true', 'y', 'yes', '1') THEN true"
    " WHEN dbx_bool_text(s) IN ('f', 'false', 'n', 'no', '0') THEN false"
    f" ELSE error({_malformed('s', chr(39) + 'BOOLEAN' + chr(39))}) END)",
    "CREATE TEMP MACRO dbx_try_bool(s) AS (CASE"
    " WHEN dbx_bool_text(s) IN ('t', 'true', 'y', 'yes', '1') THEN true"
    " WHEN dbx_bool_text(s) IN ('f', 'false', 'n', 'no', '0') THEN false ELSE NULL END)",
    # array_contains: NULL when the value is not found and the array holds a NULL
    "CREATE TEMP MACRO dbx_array_contains(a, v) AS (CASE"
    " WHEN a IS NULL OR v IS NULL THEN NULL WHEN list_contains(a, v) THEN true"
    " WHEN len(list_filter(a, lambda x: x IS NULL)) > 0 THEN NULL ELSE false END)",
    # concat_ws over strings and arrays of strings: NULLs skipped, a NULL separator NULL
    "CREATE TEMP MACRO dbx_concat_ws(sep, items) AS (CASE WHEN sep IS NULL THEN NULL"
    " ELSE array_to_string(list_filter(items, lambda x: x IS NOT NULL), sep) END)",
    # lpad/rpad (UTF8String.lpad): no padding to do, or nothing to pad with, is the
    # string cut to the length ('' for a length below 1)
    "CREATE TEMP MACRO dbx_lpad(s, n, p) AS (CASE WHEN n - length(s) <= 0 OR p = ''"
    " THEN substring(s, 1, greatest(n, 0)) ELSE lpad(s, n, p) END)",
    "CREATE TEMP MACRO dbx_rpad(s, n, p) AS (CASE WHEN n - length(s) <= 0 OR p = ''"
    " THEN substring(s, 1, greatest(n, 0)) ELSE rpad(s, n, p) END)",
    # upper/lower: DuckDB's for ASCII, Java's full case mapping otherwise
    "CREATE TEMP MACRO dbx_upper(s) AS (CASE WHEN regexp_full_match(s, '[\\x00-\\x7F]*')"
    " THEN upper(s) ELSE dbx_upper_text(s) END)",
    "CREATE TEMP MACRO dbx_lower(s) AS (CASE WHEN regexp_full_match(s, '[\\x00-\\x7F]*')"
    " THEN lower(s) ELSE dbx_lower_text(s) END)",
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


# ------------------------------------------------------------- Python functions
def java_double(value: float, *, single: bool = False) -> str:
    """A DOUBLE (or, `single`, a FLOAT) as Java's Double.toString / Float.toString
    write it, which is how Spark casts it to a string and renders it: the shortest
    digits that read back as the same value, `1.0` not `1`, and computerized
    scientific notation (`1.0E7`, `1.0E-4`) outside [1e-3, 1e7)."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "-0.0" if math.copysign(1.0, value) < 0 else "0.0"
    if single:
        target = struct.unpack("f", struct.pack("f", value))[0]
        text = next(t for t in (f"{target:.{n}g}" for n in range(1, 10))
                    if struct.unpack("f", struct.pack("f", float(t)))[0] == target)
    else:
        text = repr(float(value))
    sign, digits, exponent = Decimal(text).as_tuple()
    shown = "".join(map(str, digits)).rstrip("0") or "0"
    point = len(digits) + exponent              # digits before the decimal point
    magnitude = abs(float(text))
    if 1e-3 <= magnitude < 1e7:
        if point <= 0:
            body = "0." + "0" * -point + shown
        elif point >= len(shown):
            body = shown + "0" * (point - len(shown)) + ".0"
        else:
            body = shown[:point] + "." + shown[point:]
    else:
        body = f"{shown[0]}.{shown[1:] or '0'}E{point - 1}"
    return ("-" if sign else "") + body


def _decimal_of(value: float) -> Decimal:
    """A DOUBLE as the decimal Spark reads it as (`BigDecimal.decimal`: its toString)."""
    return Decimal(repr(float(value)))


def _half_up(q: Fraction, scale: int) -> Decimal:
    scaled = q * Fraction(10) ** scale
    n = (2 * abs(scaled.numerator) + scaled.denominator) // (2 * scaled.denominator)
    return Decimal(-n if scaled < 0 else n).scaleb(-scale)


def _udf_double_str(value: float) -> str:
    return java_double(value)


def _udf_float_str(value: float) -> str:
    return java_double(value, single=True)


def _udf_round_double(value: float, scale: int) -> float:
    """round(DOUBLE, n): HALF_UP on the double's decimal representation, so
    round(1.005, 2) is 1.01 as in Spark (the binary value is just below 1.005)."""
    if math.isnan(value) or math.isinf(value):
        return value
    return float(_decimal_of(value).quantize(Decimal(1).scaleb(-scale), ROUND_HALF_UP))


def _udf_double_decimal(value: float, scale: int) -> str:
    """CAST(DOUBLE AS DECIMAL(p, s)): the decimal representation, HALF_UP to `s`."""
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"[CAST_INVALID_INPUT] The value {java_double(value)} of the type "
                         f"\"DOUBLE\" cannot be cast to a DECIMAL. SQLSTATE: 22018")
    return format(_decimal_of(value).quantize(Decimal(1).scaleb(-scale), ROUND_HALF_UP), "f")


def _quotient(a: str, b: str) -> Fraction:
    divisor = Fraction(Decimal(b))
    if divisor == 0:
        raise ZeroDivisionError(_DIVIDE_BY_ZERO)
    return Fraction(Decimal(a)) / divisor


def _udf_decimal_quotient(a: str, b: str, scale: int, final_scale: int) -> str:
    """a / b for DECIMAL operands, exact: HALF_UP to Spark's result scale (then, for
    avg, to its result type's scale - Spark rounds twice there, and so does this)."""
    result = _half_up(_quotient(a, b), scale)
    if final_scale != scale:
        result = _half_up(Fraction(result), final_scale)
    return format(result, "f")


def _udf_decimal_div(a: str, b: str) -> int:
    """a div b for DECIMAL operands: the quotient truncated toward zero, exactly."""
    return int(_quotient(a, b))


def _udf_upper(text: str) -> str:
    return text.upper()


def _udf_lower(text: str) -> str:
    return text.lower()


#: (name, function, parameter types, result type), created on the lakehouse connection.
UDFS = (
    ("dbx_double_str", _udf_double_str, ["DOUBLE"], "VARCHAR"),
    ("dbx_float_str", _udf_float_str, ["FLOAT"], "VARCHAR"),
    ("dbx_round_double", _udf_round_double, ["DOUBLE", "INTEGER"], "DOUBLE"),
    ("dbx_double_decimal", _udf_double_decimal, ["DOUBLE", "INTEGER"], "VARCHAR"),
    ("dbx_decimal_quotient", _udf_decimal_quotient,
     ["VARCHAR", "VARCHAR", "INTEGER", "INTEGER"], "VARCHAR"),
    ("dbx_decimal_div", _udf_decimal_div, ["VARCHAR", "VARCHAR"], "BIGINT"),
    # Java's full case mapping ('ß' upper is 'SS'), for text that is not ASCII
    ("dbx_upper_text", _udf_upper, ["VARCHAR"], "VARCHAR"),
    ("dbx_lower_text", _udf_lower, ["VARCHAR"], "VARCHAR"),
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


def _t(node: exp.Expression | None):
    """The Spark type databricks_types gave `node` (None: not known)."""
    return node.meta.get("dbx_type") if node is not None else None


def _sql(node: exp.Expression) -> str:
    try:
        return node.sql(dialect="databricks")
    except Exception:  # noqa: BLE001 - a node sqlglot cannot write back
        return type(node).__name__


def _unknown(node: exp.Expression, what: str) -> Refused:
    return Refused(f"{what} on an expression of unknown type ({_sql(node)})",
                   "the lakehouse answers it only where it knows Spark's result type; "
                   "CAST the operands")


def _as(node: exp.Expression, spark_type) -> exp.Expression:
    """`node` CAST to the DuckDB spelling of a Spark type."""
    return _cast(node, _DUCK_TYPES.get(spark_type.kind) or
                 f"DECIMAL({spark_type.p}, {spark_type.s})")


_DUCK_TYPES = {"tinyint": "TINYINT", "smallint": "SMALLINT", "int": "INTEGER",
               "bigint": "BIGINT", "double": "DOUBLE", "float": "FLOAT",
               "string": "VARCHAR", "date": "DATE", "timestamp": "TIMESTAMP",
               "boolean": "BOOLEAN"}


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
_ZONED = {exp.DataType.Type.TIMESTAMPTZ, exp.DataType.Type.TIMESTAMPLTZ}


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
_SPARK_NAMES = {exp.DataType.Type.TINYINT: "TINYINT", exp.DataType.Type.SMALLINT: "SMALLINT",
                exp.DataType.Type.INT: "INT", exp.DataType.Type.BIGINT: "BIGINT"}


def _typed_cast(node: exp.Expression, _context) -> exp.Expression:
    """CAST as Databricks (ANSI) does it, by the input's type:

      to an integral type   a DECIMAL or DOUBLE truncates toward zero (DuckDB rounds);
                            a string must be an integer - '1.9' and '1e3' are
                            CAST_INVALID_INPUT (try_cast: NULL);
      to DECIMAL(p, s)      a DOUBLE is its decimal representation, HALF_UP;
      to STRING             a DOUBLE or FLOAT is written as Java writes it ('1.0E20');
      to BOOLEAN            a string is t/true/y/yes/1 or f/false/n/no/0, any case."""
    to = node.args["to"]
    source = _t(node.this)
    try_ = isinstance(node, exp.TryCast)
    if to.this in _INTEGRAL:
        if source is None:
            # not known: through DECIMAL(38,18), exact for every value but a string
            # that is not an integer
            inner = _cast(node.this, "DECIMAL(38, 18)", try_=try_)
            return _mark(type(node)(this=_fn("trunc", inner), to=to))
        if source.kind == "string":
            text = _fn("dbx_try_int_text", node.this) if try_ else \
                _fn("dbx_int_text", node.this, exp.Literal.string(_SPARK_NAMES[to.this]))
            return _mark(type(node)(this=text, to=to))
        if source.is_decimal or source.is_fractional:
            return _mark(type(node)(this=_fn("trunc", node.this), to=to))
        return node
    if to.this == exp.DataType.Type.DECIMAL and source is not None and source.is_fractional:
        target = _t(node)
        return _mark(type(node)(this=_fn("dbx_double_decimal", node.this, _int(target.s)),
                                to=to))
    if to.this in (exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR) and \
            source is not None and source.is_fractional:
        return _fn("dbx_float_str" if source.kind == "float" else "dbx_double_str", node.this)
    if to.this == exp.DataType.Type.BOOLEAN and source is not None and source.kind == "string":
        return _fn("dbx_try_bool" if try_ else "dbx_bool", node.this)
    return node


def _named_cast(to: str):
    def rewrite(node: exp.Expression, context) -> exp.Expression:
        if len(node.expressions) != 1:
            raise Refused(f"{node.name}() with {len(node.expressions)} arguments",
                          "a cast function takes one")
        return _typed_cast(_mark(exp.Cast(this=node.expressions[0],
                                          to=exp.DataType.build(to))), context)
    return rewrite


def _as_int(node: exp.Expression, _context=None) -> exp.Expression:
    """An INT-valued Spark function (year, length, instr, size, ...): DuckDB's is a
    BIGINT."""
    return _cast(_mark(node), "INTEGER")


def _day_of_week(node: exp.Expression, _context) -> exp.Expression:
    """dayofweek: Sunday 1 .. Saturday 7 (DuckDB: Sunday 0)."""
    return _as_int(_mark(exp.Paren(this=_mark(exp.Add(
        this=_mark(exp.Mod(this=_fn("isodow", node.this), expression=_int(7))),
        expression=_int(1))))))


def _weekday(node: exp.Expression, _context) -> exp.Expression:
    """weekday: Monday 0 .. Sunday 6 (DuckDB's weekday is dayofweek)."""
    if len(node.expressions) != 1:
        raise Refused("weekday() with other than one argument", "weekday takes a date")
    return _as_int(_mark(exp.Paren(this=_mark(exp.Sub(
        this=_fn("isodow", node.expressions[0]), expression=_int(1))))))


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
    return _as_int(_fn(target, value))


# --------------------------------------------------------- typed arithmetic
def _divide(node: exp.Expression, _context) -> exp.Expression:
    """a / b: a DECIMAL quotient exact in Spark's result precision and scale, HALF_UP
    (DuckDB divides DECIMALs as DOUBLEs); a DOUBLE otherwise. A zero divisor raises
    DIVIDE_BY_ZERO, as on an ANSI warehouse (DuckDB answers inf or NULL)."""
    result = _t(node)
    a, b = node.this, node.expression
    if result is None:
        raise _unknown(node, "DECIMAL arithmetic (/)")
    divisor = _fn("dbx_divisor", a.copy(), b)
    if result.is_decimal:
        quotient = _fn("dbx_decimal_quotient", _cast(a, "VARCHAR"), _cast(divisor, "VARCHAR"),
                       _int(result.s), _int(result.s))
        return _as(quotient, result)
    node.set("expression", divisor)
    return _mark(node)


def _int_divide(node: exp.Expression, _context) -> exp.Expression:
    """a div b: the quotient truncated toward zero, a BIGINT - exact for DECIMALs
    (`dbx_decimal_div`), on the DOUBLE quotient for DOUBLEs."""
    a, b = node.this, node.expression
    ta, tb = _t(a), _t(b)
    if ta is None or tb is None or not (ta.is_numeric or ta.kind == "null") or \
            not (tb.is_numeric or tb.kind == "null"):
        raise _unknown(node, "div")
    divisor = _fn("dbx_divisor", a.copy(), b)
    if ta.is_fractional or tb.is_fractional:
        return _cast(_fn("trunc", _mark(exp.Div(this=a, expression=divisor))), "BIGINT")
    if ta.is_decimal or tb.is_decimal:
        return _fn("dbx_decimal_div", _cast(a, "VARCHAR"), _cast(divisor, "VARCHAR"))
    return _cast(_mark(exp.IntDiv(this=a, expression=divisor)), "BIGINT")


def _modulo(node: exp.Expression, _context) -> exp.Expression:
    """a % b: the dividend's sign, as DuckDB's; a DECIMAL in Spark's result type."""
    node.set("expression", _fn("dbx_divisor", node.this.copy(), node.expression))
    return _typed_result(_mark(node))


def _typed_result(node: exp.Expression, _context=None) -> exp.Expression:
    """+, -, *, %: DuckDB's DECIMAL result has Spark's value and scale (exact sums and
    products) but its own precision: CAST to Spark's type. Subtracting DATEs or
    TIMESTAMPs (an INTERVAL in Databricks) is refused."""
    if isinstance(node, exp.Sub):
        left, right = _t(node.this), _t(node.expression)
        if left is not None and right is not None and \
                {left.kind, right.kind} <= {"date", "timestamp"}:
            raise Refused("subtracting DATEs or TIMESTAMPs (an INTERVAL)",
                          "use datediff or timestampdiff")
    result = _t(node)
    if result is not None and result.is_decimal:
        return _as(_mark(node), result)
    return node


def _round(node: exp.Expression, _context) -> exp.Expression:
    """round(x, n): HALF_UP. A DECIMAL in Spark's result type; a DOUBLE rounded on its
    decimal representation (round(1.005, 2) is 1.01, as in Spark)."""
    source, result = _t(node.this), _t(node)
    if source is None:
        raise _unknown(node, "round")
    if source.is_fractional:
        scale = node.args.get("decimals") or _int(0)
        return _fn("dbx_round_double", node.this, _cast(scale, "INTEGER"))
    if source.is_decimal:
        if result is None:
            raise Refused("round(DECIMAL, n) with a scale that is not a constant",
                          "Spark's result type depends on it")
        return _as(_mark(node), result)
    return node


def _floor_ceil(node: exp.Expression, _context) -> exp.Expression:
    """floor/ceil: a BIGINT for a DOUBLE or an integer, DECIMAL(p-s+1, 0) for a
    DECIMAL (DuckDB keeps a DOUBLE)."""
    result = _t(node)
    if result is None:
        raise _unknown(node, "floor/ceil")
    return _as(_mark(node), result)


def _sign(node: exp.Expression, _context) -> exp.Expression:
    return _cast(_mark(node), "DOUBLE")


# ------------------------------------------------------------ typed strings
def _string_args(args: list[exp.Expression]) -> list[exp.Expression]:
    """Arguments a string function reads as strings: a DOUBLE or FLOAT as Java writes it."""
    out = []
    for arg in args:
        kind = getattr(_t(arg), "kind", None)
        if kind in ("double", "float"):
            arg = _fn("dbx_float_str" if kind == "float" else "dbx_double_str", arg)
        out.append(arg)
    return out


def _concat(node: exp.Expression, _context) -> exp.Expression:
    """concat / ||: a DOUBLE argument written as Java writes it; NULL when an argument
    is NULL, as a STRING."""
    if isinstance(node, exp.DPipe):
        left, right = _string_args([node.this, node.expression])
        node.set("this", left)
        node.set("expression", right)
    else:
        node.set("expressions", _string_args(node.expressions))
    args = [node.this, node.expression] if isinstance(node, exp.DPipe) else node.expressions
    if any(getattr(_t(a), "kind", None) == "null" for a in args):
        return _cast(_mark(node), "VARCHAR")
    return _mark(node)


def _concat_ws(node: exp.Expression, _context) -> exp.Expression:
    """concat_ws(sep, ...): strings and ARRAYs of strings, flattened, NULLs skipped."""
    separator, *args = node.expressions
    types = [_t(a) for a in args]
    if not any(t is not None and t.kind == "array" for t in types):
        if any(t is None for t in types):
            raise _unknown(node, "concat_ws")
        node.set("expressions", [separator, *_string_args(args)])
        return _mark(node)
    if any(t is None for t in types):
        raise _unknown(node, "concat_ws")
    parts = []
    for arg, kind in zip(args, types, strict=True):
        if kind.kind == "array":
            parts.append(_cast(arg, "VARCHAR[]"))
        elif kind.kind != "null":
            parts.append(_mark(exp.Array(expressions=[_cast(_string_args([arg])[0],
                                                            "VARCHAR")])))
    items = _fn("flatten", _mark(exp.Array(expressions=parts))) if parts else \
        _cast(_mark(exp.Array(expressions=[])), "VARCHAR[]")
    return _fn("dbx_concat_ws", separator, items)


def _java_replacement(text: str) -> str:
    """A java.util.regex replacement ($1) as RE2's (\\1). `\\` cannot reach here: a
    string literal with a backslash is refused before translation."""
    out, i = [], 0
    while i < len(text):
        if text[i] != "$":
            out.append(text[i])
            i += 1
            continue
        group = re.match(r"\$(\d+)", text[i:])
        if group is None or len(group.group(1)) > 1:
            raise Refused("regexp_replace replacement with "
                          + ("a named or malformed group reference" if group is None
                             else "a group number above 9"),
                          "vetted: $0 .. $9")
        out.append("\\" + group.group(1))
        i += 2
    return "".join(out)


def _case(name: str):
    def rewrite(node: exp.Expression, _context) -> exp.Expression:
        return _fn(name, node.this)
    return rewrite


def _check_regexp_extract(node: exp.Expression) -> None:
    """regexp_extract(s, p[, idx]): idx (default 1) within the pattern's groups - Spark
    fails the statement otherwise, DuckDB would answer ''."""
    _check_args("this", "expression", "group", "null_if_pos_overflow")(node)
    pattern, group = _literal(node.expression), node.args.get("group")
    index = int(group.name) if isinstance(group, exp.Literal) and not group.is_string \
        else 1 if group is None else None
    if pattern is None or index is None:
        return
    try:
        groups = re.compile(pattern).groups
    except re.error:
        return
    if index > groups:
        raise Refused(f"regexp_extract group index {index} of a pattern with {groups} "
                      f"group(s)", "Databricks fails it: REGEX_GROUP_INDEX")


def _regexp_replace(node: exp.Expression, _context) -> exp.Expression:
    replacement = node.args.get("replacement")
    text = _literal(replacement)
    if replacement is not None and text is None:
        raise Refused("regexp_replace with a replacement that is not a string literal",
                      "Java's $n group references are translated from a literal only")
    if text is not None and "$" in text:
        node.set("replacement", _mark(exp.Literal.string(_java_replacement(text))))
    return node


def _pad(node: exp.Expression, _context) -> exp.Expression:
    fill = node.args.get("fill_pattern") or exp.Literal.string(" ")
    return _fn("dbx_lpad" if node.args.get("is_left") else "dbx_rpad",
               node.this, node.expression, fill)


def _replace(node: exp.Expression, _context) -> exp.Expression:
    """replace(s, search): the two-argument form removes `search`."""
    if node.args.get("replacement") is None:
        node.set("replacement", exp.Literal.string(""))
    return node


def _array_contains(node: exp.Expression, _context) -> exp.Expression:
    return _fn("dbx_array_contains", node.this, node.expression)


def _add_months(node: exp.Expression, _context) -> exp.Expression:
    """add_months(start, n): a DATE, also from a TIMESTAMP (DuckDB keeps the time)."""
    return _cast(_mark(exp.Add(this=_cast(node.this, "DATE"),
                               expression=_fn("to_months", _cast(node.expression,
                                                                 "INTEGER")))), "DATE")


def _like(node: exp.Expression, _context) -> exp.Expression:
    """LIKE / ILIKE: `\\` escapes the next _ or % as in Spark (DuckDB has no default
    escape character), for a pattern read from a column too."""
    if isinstance(node.expression, (exp.Any, exp.All)) or isinstance(node.parent, exp.Escape):
        return node
    return _mark(exp.Escape(this=_mark(node), expression=exp.Literal.string("\\")))


# ------------------------------------------------------------- aggregates
def _finish(node: exp.Expression, finisher,
            call: exp.Expression | None = None) -> exp.Expression:
    """An aggregate whose DuckDB result needs finishing (a CAST, a COALESCE, avg's
    exact division): finished here, or - when it is the `this` of a FILTER or OVER -
    by `wrapped`, around the whole call, which is where SQL allows it. `call` is the
    node's DuckDB form when a rewrite replaced it."""
    call = node if call is None else call
    if isinstance(node.parent, (exp.Filter, exp.Window)) and node.parent.this is node:
        call.meta["dbx_finish"] = finisher
        return call
    return finisher(lambda inner: inner)


def wrapped(node: exp.Expression) -> exp.Expression:
    """A FILTER/OVER around an aggregate that needs finishing (`_finish`)."""
    if isinstance(node.parent, (exp.Filter, exp.Window)) and node.parent.this is node:
        return node                                   # the outer one finishes it
    chain, inner = [], node
    while isinstance(inner, (exp.Filter, exp.Window)):
        chain.append(inner)
        inner = inner.this
    finisher = inner.meta.get("dbx_finish")
    if finisher is None:
        return node

    def rewrap(call: exp.Expression) -> exp.Expression:
        for wrapper in reversed(chain):
            clone = wrapper.copy()
            clone.set("this", call)
            call = _mark(clone)
        return call

    return finisher(rewrap)


def _sum(node: exp.Expression, _context) -> exp.Expression:
    """sum(DECIMAL(p, s)) is DECIMAL(p+10, s) (DuckDB's is (38, s))."""
    result = _t(node)
    if result is None or not result.is_decimal:
        return node
    return _finish(node, lambda over: _as(over(_mark(node)), result))


def _avg(node: exp.Expression, _context) -> exp.Expression:
    """avg(DECIMAL(p, s)) is DECIMAL(p+4, s+4), as Spark computes it: the DECIMAL(p+10,
    s) sum divided by the count as DECIMAL(20, 0), HALF_UP to the division's scale,
    then to s+4 (DuckDB averages DECIMALs as DOUBLEs)."""
    result, source = _t(node), _t(node.this)
    if result is None:
        raise _unknown(node, "avg")
    if not result.is_decimal:
        return node
    from .databricks_types import adjust

    total_p = min(38, source.p + 10)
    scale = max(6, source.s + 20 + 1)
    divided = adjust(total_p - source.s + scale, scale)

    def finisher(over):
        total = over(_mark(exp.Sum(this=node.this.copy())))
        count = over(_mark(exp.Count(this=node.this.copy())))
        return _as(_fn("dbx_decimal_quotient", _cast(total, "VARCHAR"),
                       _cast(count, "VARCHAR"), _int(divided.s), _int(result.s)), result)

    return _finish(node, finisher)


def _as_double(node: exp.Expression, _context) -> exp.Expression:
    """median / percentile: a DOUBLE (DuckDB keeps a DECIMAL input's type)."""
    return _finish(node, lambda over: _cast(over(_mark(node)), "DOUBLE"))


def _collect(node: exp.Expression, _context) -> exp.Expression:
    """collect_list / collect_set over no rows (or only NULLs) is [] (DuckDB: NULL)."""
    return _finish(node, lambda over: _mark(exp.Coalesce(
        this=over(_mark(node)), expressions=[_mark(exp.Array(expressions=[]))])))


def _ranking(node: exp.Expression, _context) -> exp.Expression:
    """row_number, rank, dense_rank, ntile: an INT (DuckDB's is a BIGINT)."""
    return _finish(node, lambda over: _cast(over(_mark(node)), "INTEGER"))


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
    return _mark(exp.Add(this=_cast(node.this, "TIMESTAMP"), expression=interval))


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
            # datediff(end, start): the difference of the two DATES, an INT
            return _as_int(exp.Sub(this=_cast(end, "DATE"), expression=_cast(start, "DATE")))
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
                         _cast(node.this, "TIMESTAMP")), to)
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
    """percentile(x, p): exact, linear interpolation (DuckDB's quantile is discrete), a
    DOUBLE."""
    call = _fn("quantile_cont", node.this, node.args["quantile"])
    return _finish(node, lambda over: _cast(over(call), "DOUBLE"), call)


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
        now = datetime.datetime.fromisoformat(str(context.now).replace("Z", "+00:00"))
        if now.tzinfo is not None:
            now = now.astimezone(datetime.UTC).replace(tzinfo=None)
        return _cast(exp.Literal.string(now.date().isoformat() if to == "DATE" else
                                        now.isoformat(sep=" ")), to)
    return rewrite


# ----------------------------------------------------------------- the lists
def _vetted(name: str, **kw) -> Rule:
    return Rule(name, **kw)


FUNCTIONS: dict[type, Rule] = {
    # aggregates
    exp.Count: _vetted("count"), exp.Sum: Rule("sum", rewrite=_sum),
    exp.Avg: Rule("avg", rewrite=_avg),
    exp.Min: _vetted("min"), exp.Max: _vetted("max"), exp.CountIf: _vetted("count_if"),
    exp.AnyValue: _vetted("any_value"), exp.Stddev: _vetted("stddev"),
    exp.StddevSamp: _vetted("stddev_samp"), exp.StddevPop: _vetted("stddev_pop"),
    exp.Variance: _vetted("variance"), exp.VariancePop: _vetted("var_pop"),
    exp.Median: Rule("median", rewrite=_as_double),
    exp.Quantile: Rule("percentile", rewrite=_quantile),
    exp.ArrayAgg: Rule("collect_list", rewrite=_collect),
    exp.ArrayUniqueAgg: Rule("collect_set", rewrite=_collect),
    exp.LogicalAnd: _vetted("bool_and"), exp.LogicalOr: _vetted("bool_or"),
    exp.ArgMax: _vetted("max_by"), exp.ArgMin: _vetted("min_by"),
    # window
    exp.RowNumber: Rule("row_number", rewrite=_ranking),
    exp.Rank: Rule("rank", rewrite=_ranking),
    exp.DenseRank: Rule("dense_rank", rewrite=_ranking), exp.Lag: _vetted("lag"),
    exp.Lead: _vetted("lead"), exp.Ntile: Rule("ntile", rewrite=_ranking),
    exp.PercentRank: _vetted("percent_rank"), exp.CumeDist: _vetted("cume_dist"),
    exp.FirstValue: _vetted("first_value"), exp.LastValue: _vetted("last_value"),
    # conditional
    exp.Case: _vetted("case"), exp.If: _vetted("if"), exp.Coalesce: _vetted("coalesce"),
    exp.Exists: _vetted("exists"),
    exp.Nullif: _vetted("nullif"), exp.Nvl2: _vetted("nvl2"),
    exp.Greatest: _vetted("greatest"), exp.Least: _vetted("least"),
    # math
    exp.Abs: _vetted("abs"), exp.Round: Rule("round", rewrite=_round),
    exp.Floor: Rule("floor", rewrite=_floor_ceil, check=_check_args("this")),
    exp.Ceil: Rule("ceil", rewrite=_floor_ceil, check=_check_args("this")),
    exp.Sqrt: _vetted("sqrt"), exp.Pow: _vetted("power"),
    exp.Ln: _vetted("ln"), exp.Log: _vetted("log"), exp.Exp: _vetted("exp"),
    exp.Sign: Rule("sign", rewrite=_sign),
    # strings
    exp.Lower: Rule("lower", rewrite=_case("dbx_lower")),
    exp.Upper: Rule("upper", rewrite=_case("dbx_upper")),
    exp.Length: Rule("length", rewrite=_as_int), exp.Trim: _vetted("trim"),
    exp.Concat: Rule("concat", rewrite=_concat),
    exp.ConcatWs: Rule("concat_ws", rewrite=_concat_ws),
    exp.Replace: Rule("replace", rewrite=_replace),
    exp.RegexpReplace: Rule("regexp_replace", rewrite=_regexp_replace, check=_check_args(
        "this", "expression", "replacement", "modifiers")),
    exp.RegexpExtract: Rule("regexp_extract", check=_check_regexp_extract),
    exp.RegexpLike: _vetted("rlike"), exp.Pad: Rule("lpad/rpad", rewrite=_pad),
    exp.Initcap: _vetted("initcap"),
    exp.StrPosition: Rule("instr/locate", rewrite=_as_int,
                          check=_check_args("this", "substr")),
    exp.Substring: Rule("substring", rewrite=_substring),
    exp.Left: Rule("left", rewrite=_left), exp.Right: Rule("right", rewrite=_right),
    # arrays
    exp.Array: _vetted("array"), exp.ArraySize: Rule("size", rewrite=_as_int),
    exp.ArrayContains: Rule("array_contains", rewrite=_array_contains),
    # dates and times
    exp.CurrentDate: Rule("current_date", rewrite=_now("DATE")),
    exp.CurrentTimestamp: Rule("current_timestamp", rewrite=_now("TIMESTAMP")),
    exp.Year: Rule("year", rewrite=_as_int), exp.Quarter: Rule("quarter", rewrite=_as_int),
    exp.Month: Rule("month", rewrite=_as_int), exp.Day: Rule("day", rewrite=_as_int),
    exp.DayOfMonth: Rule("dayofmonth", rewrite=_as_int),
    exp.DayOfYear: Rule("dayofyear", rewrite=_as_int),
    exp.WeekOfYear: Rule("weekofyear", rewrite=_as_int),
    exp.Hour: Rule("hour", rewrite=_as_int), exp.Minute: Rule("minute", rewrite=_as_int),
    exp.Second: Rule("second", rewrite=_as_int),
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
    exp.TimestampTrunc: Rule("date_trunc", rewrite=_trunc(_TRUNC_TS, "TIMESTAMP"),
                             check=_check_unit(_TRUNC_TS, "date_trunc")),
    exp.DateTrunc: Rule("trunc", rewrite=_trunc(_TRUNC_DATE, "DATE"),
                        check=_check_unit(_TRUNC_DATE, "trunc")),
    exp.AddMonths: Rule("add_months", rewrite=_add_months),
    exp.LastDay: _vetted("last_day"),
    exp.TsOrDsToDate: Rule("to_date", rewrite=_to_date, check=_check_format),
    exp.StrToTime: Rule("to_timestamp", check=_check_format),
    exp.TimeToStr: Rule("date_format", check=_check_format),
    exp.TimeStrToTime: _vetted("(timestamp of a date_format argument)"),
    exp.Cast: Rule("cast", rewrite=_typed_cast, check=_check_cast),
    exp.TryCast: Rule("try_cast", rewrite=_typed_cast, check=_check_cast),
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
    exp.Properties, exp.PrimaryKeyColumnConstraint,
    exp.DefaultColumnConstraint, exp.EQ, exp.Var, exp.Placeholder,
    # literals
    exp.Literal, exp.Null, exp.Boolean,
    # operators
    exp.And, exp.Or, exp.Not, exp.Neg, exp.Add, exp.Sub, exp.Mul, exp.Div,
    exp.IntDiv, exp.Mod, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ,
    exp.NullSafeNEQ,
    exp.Is, exp.In, exp.Between, exp.Like, exp.ILike, exp.DPipe, exp.Any, exp.All,
})

#: Operators answered with a rewrite, as functions are.
OPERATORS: dict[type, Rule] = {
    exp.Div: Rule("/", rewrite=_divide), exp.IntDiv: Rule("div", rewrite=_int_divide),
    exp.Mod: Rule("%", rewrite=_modulo),
    exp.Add: Rule("+", rewrite=_typed_result), exp.Sub: Rule("-", rewrite=_typed_result),
    exp.Mul: Rule("*", rewrite=_typed_result),
    exp.DPipe: Rule("||", rewrite=_concat),
    exp.Like: Rule("like", rewrite=_like, check=_check_like),
    exp.ILike: Rule("ilike", rewrite=_like, check=_check_like),
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
    if isinstance(node, exp.DataType) and node.this in _ZONED:
        # Databricks' TIMESTAMP is an instant in the session time zone (UTC here): a
        # plain TIMESTAMP, which a string CAST writes as `2024-01-01 00:00:00` (a
        # TIMESTAMPTZ would add `+00`)
        return _mark(exp.DataType.build("TIMESTAMP", dialect="duckdb"))
    if isinstance(node, (exp.Window, exp.Filter)):
        return wrapped(node)
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
