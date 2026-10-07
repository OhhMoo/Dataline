"""Databricks (Spark SQL) result types of a statement's expressions, before translation.

The lakehouse answers on DuckDB, whose types differ from Spark's exactly where values
are rounded or rendered: Spark divides DECIMALs into a DECIMAL whose scale its rules fix
(`1.0/3` is DECIMAL(7,6), 0.333333; DuckDB answers a DOUBLE), averages a DECIMAL(p,s)
into DECIMAL(p+4,s+4), rounds a DECIMAL to `min(s, n)` digits, floors a DOUBLE to a
BIGINT. `Typer` gives each node of the parsed Databricks tree the type Spark would, so
`databricks_functions` can rewrite the DuckDB form to land on it - and refuse, by name,
where it cannot know.

Leaf types: literals (an integral literal next to a DECIMAL is DECIMAL(digits, 0), as in
Spark's DecimalPrecision rule), CASTs, and columns, resolved through sqlglot's scopes to
a lakehouse table (`lookup(schema, table) -> {column: duckdb type}`) or to a subquery's
or CTE's projection. Everything else follows the Spark rules below; an expression this
module cannot type is `None`, and a rewrite that needs the type refuses.

DECIMAL arithmetic (spark.sql.decimalOperations.allowPrecisionLoss, the default):

  e1 + e2, e1 - e2   max(s1,s2) + max(p1-s1, p2-s2) + 1, scale max(s1,s2)
  e1 * e2            p1 + p2 + 1, scale s1 + s2
  e1 / e2            p1 - s1 + s2 + max(6, s1 + p2 + 1), scale max(6, s1 + p2 + 1)
  e1 % e2            min(p1-s1, p2-s2) + max(s1,s2), scale max(s1,s2)
  sum(DECIMAL(p,s))  DECIMAL(min(38, p+10), s)
  avg(DECIMAL(p,s))  DECIMAL(min(38, p+4), min(38, s+4))

and a precision above 38 is cut back to 38 keeping at least min(s, 6) of the scale.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlglot import exp

MAX_PRECISION = 38


@dataclass(frozen=True)
class SparkType:
    kind: str                       # int bigint smallint tinyint decimal double float
    #                                 string date timestamp boolean array null
    p: int = 0                      # DECIMAL precision
    s: int = 0                      # DECIMAL scale
    elem: SparkType | None = None   # an ARRAY's element type
    literal_digits: int = 0         # an integral LITERAL's digit count (0: not a literal)

    @property
    def is_decimal(self) -> bool:
        return self.kind == "decimal"

    @property
    def is_integral(self) -> bool:
        return self.kind in ("tinyint", "smallint", "int", "bigint")

    @property
    def is_fractional(self) -> bool:
        return self.kind in ("double", "float")

    @property
    def is_numeric(self) -> bool:
        return self.is_decimal or self.is_integral or self.is_fractional

    def text(self) -> str:
        if self.is_decimal:
            return f"decimal({self.p},{self.s})"
        if self.kind == "array":
            return f"array<{self.elem.text() if self.elem else 'void'}>"
        return self.kind


INT, BIGINT, DOUBLE, FLOAT = (SparkType("int"), SparkType("bigint"), SparkType("double"),
                              SparkType("float"))
STRING, DATE, TIMESTAMP, BOOLEAN, NULL = (SparkType("string"), SparkType("date"),
                                          SparkType("timestamp"), SparkType("boolean"),
                                          SparkType("null"))
_INTEGRAL_DIGITS = {"tinyint": 3, "smallint": 5, "int": 10, "bigint": 20}


def decimal(p: int, s: int) -> SparkType:
    return SparkType("decimal", p, s)


def adjust(p: int, s: int) -> SparkType:
    """Spark's precision cut for a result wider than 38 digits."""
    if p <= MAX_PRECISION:
        return decimal(p, s)
    int_digits = p - s
    min_scale = min(s, 6)
    return decimal(MAX_PRECISION, max(MAX_PRECISION - int_digits, min_scale))


def as_decimal(t: SparkType | None) -> SparkType | None:
    """An operand as the DECIMAL Spark widens it to beside a DECIMAL (None: it is not
    one Spark widens - a DOUBLE, a string)."""
    if t is None:
        return None
    if t.is_decimal:
        return t
    if t.is_integral:
        return decimal(t.literal_digits or _INTEGRAL_DIGITS[t.kind], 0)
    return None


_DUCK_SIMPLE = {
    "BOOLEAN": BOOLEAN, "TINYINT": SparkType("tinyint"), "SMALLINT": SparkType("smallint"),
    "INTEGER": INT, "BIGINT": BIGINT, "HUGEINT": BIGINT, "UTINYINT": SparkType("smallint"),
    "USMALLINT": INT, "UINTEGER": BIGINT, "UBIGINT": decimal(20, 0),
    "FLOAT": FLOAT, "REAL": FLOAT, "DOUBLE": DOUBLE, "VARCHAR": STRING, "DATE": DATE,
    "TIMESTAMP": TIMESTAMP, "TIMESTAMP WITH TIME ZONE": TIMESTAMP, "TIMESTAMP_S": TIMESTAMP,
    "TIMESTAMP_MS": TIMESTAMP, "TIMESTAMP_NS": TIMESTAMP,
}


def from_duckdb(text: str) -> SparkType | None:
    text = str(text).strip().upper()
    found = re.fullmatch(r"DECIMAL\((\d+),\s*(\d+)\)", text)
    if found:
        return decimal(int(found.group(1)), int(found.group(2)))
    if text.endswith("[]"):
        inner = from_duckdb(text[:-2])
        return SparkType("array", elem=inner) if inner else None
    return _DUCK_SIMPLE.get(text)


_DATATYPE = {
    exp.DataType.Type.TINYINT: SparkType("tinyint"),
    exp.DataType.Type.SMALLINT: SparkType("smallint"), exp.DataType.Type.INT: INT,
    exp.DataType.Type.BIGINT: BIGINT, exp.DataType.Type.DOUBLE: DOUBLE,
    exp.DataType.Type.FLOAT: FLOAT, exp.DataType.Type.TEXT: STRING,
    exp.DataType.Type.VARCHAR: STRING, exp.DataType.Type.DATE: DATE,
    exp.DataType.Type.TIMESTAMP: TIMESTAMP, exp.DataType.Type.TIMESTAMPTZ: TIMESTAMP,
    exp.DataType.Type.TIMESTAMPLTZ: TIMESTAMP, exp.DataType.Type.BOOLEAN: BOOLEAN,
}


def from_datatype(dt: exp.DataType | None) -> SparkType | None:
    if not isinstance(dt, exp.DataType):
        return None
    if dt.this == exp.DataType.Type.DECIMAL:
        params = [int(e.name) for e in dt.expressions if e.name.isdigit()]
        if len(params) == 2:
            return decimal(*params)
        if len(params) == 1:
            return decimal(params[0], 0)
        return decimal(10, 0)
    return _DATATYPE.get(dt.this)


def literal_type(node: exp.Literal) -> SparkType:
    if node.is_string:
        return STRING
    text = node.name
    if re.search(r"[eE]", text):
        return DOUBLE
    if "." in text:
        whole, _, frac = text.lstrip("+-").partition(".")
        digits = (whole.lstrip("0") + frac).lstrip("0") or "0"
        s = len(frac)
        return decimal(max(len(digits), s, 1), s)
    digits = len(text.lstrip("+-").lstrip("0") or "0")
    value = int(text)
    kind = "int" if -2**31 <= value < 2**31 else "bigint"
    return SparkType(kind, literal_digits=digits)


def wider(a: SparkType | None, b: SparkType | None) -> SparkType | None:
    """The common type of two branches (CASE, COALESCE, ...), Spark's widening."""
    if a is None or b is None:
        return None
    if a.kind == "null":
        return SparkType(b.kind, b.p, b.s, b.elem)
    if b.kind == "null":
        return SparkType(a.kind, a.p, a.s, a.elem)
    if a.kind == b.kind and not a.is_decimal:
        return SparkType(a.kind, elem=a.elem)
    if a.is_fractional or b.is_fractional:
        if a.is_numeric and b.is_numeric:
            return DOUBLE if "double" in (a.kind, b.kind) or a.is_decimal or b.is_decimal \
                else FLOAT
        return None
    if a.is_numeric and b.is_numeric:
        if a.is_integral and b.is_integral:
            order = ["tinyint", "smallint", "int", "bigint"]
            return SparkType(max(a.kind, b.kind, key=order.index))
        # widening (CASE, COALESCE, a UNION) takes an INT as DECIMAL(10,0) even when
        # it is a literal: Spark's minimum-precision literals are arithmetic's only
        da = as_decimal(SparkType(a.kind, a.p, a.s))
        db = as_decimal(SparkType(b.kind, b.p, b.s))
        s = max(da.s, db.s)
        return adjust(max(da.p - da.s, db.p - db.s) + s, s)
    if {a.kind, b.kind} == {"date", "timestamp"}:
        return TIMESTAMP
    if "string" in (a.kind, b.kind):
        return STRING
    return None


Lookup = Callable[[str, str], dict]
#: Functions sqlglot parses as `Anonymous` (databricks_functions.NAMED), by name.
_NAMED = {"weekday": INT, "bigint": BIGINT, "smallint": SparkType("smallint"),
          "tinyint": SparkType("tinyint"), "user": STRING}


class Typer:
    """Spark types for the nodes of one parsed statement (see the module docstring)."""

    def __init__(self, tree: exp.Expression, lookup: Lookup | None, schema: str):
        self.tree = tree
        self.lookup = lookup
        self.schema = schema
        self.memo: dict[int, SparkType | None] = {}
        self.scope_of: dict[int, object] = {}
        self._tables: dict[tuple[str, str], dict] = {}
        #: [catalog, schema, table] the lookup knows no columns of
        self.missing: list[list[str]] = []
        try:
            from sqlglot.optimizer.scope import traverse_scope

            for query in _top_queries(tree):
                for scope in traverse_scope(query):
                    for column in scope.columns:
                        self.scope_of.setdefault(id(column), scope)
        except Exception:  # noqa: BLE001 - an unscoped column is simply untyped
            pass

    def annotate(self, tree: exp.Expression) -> None:
        for node in tree.walk():
            node.meta["dbx_type"] = self.type_of(node)

    # ------------------------------------------------------------------- columns
    def _table_columns(self, table: exp.Table) -> dict | None:
        if self.lookup is None or not isinstance(table.this, exp.Identifier):
            return None
        catalog = (table.catalog or "").lower()
        if catalog and catalog not in ("main", "hive_metastore", "system"):
            return None
        schema = table.db or self.schema
        key = (schema.lower(), table.name.lower())
        if key not in self._tables:
            try:
                self._tables[key] = {k.lower(): v for k, v in
                                     (self.lookup(schema, table.name) or {}).items()}
            except Exception:  # noqa: BLE001
                self._tables[key] = {}
            if not self._tables[key]:
                # not a lakehouse table: a refusal for want of its types would hide
                # the TABLE_OR_VIEW_NOT_FOUND the engine raises (translate)
                self.missing.append([catalog or "main", schema.lower(), table.name.lower()])
        return self._tables[key]

    def _source_type(self, source, name: str) -> SparkType | None:
        if isinstance(source, exp.Table):
            cols = self._table_columns(source)
            return from_duckdb(cols[name]) if cols and name in cols else None
        expression = getattr(source, "expression", None)
        while isinstance(expression, exp.SetOperation):
            expression = expression.left
        if isinstance(expression, exp.Values):
            return self._values_type(expression, name)
        if not isinstance(expression, exp.Select):
            return None
        for projection in expression.selects:
            if projection.alias_or_name.lower() == name:
                return self.type_of(projection.unalias() if isinstance(projection, exp.Alias)
                                    else projection)
        if any(isinstance(p, exp.Star) for p in expression.selects):
            for inner in getattr(source, "sources", {}).values():
                found = self._source_type(inner, name)
                if found is not None:
                    return found
        return None

    def _values_type(self, values: exp.Values, name: str) -> SparkType | None:
        """A VALUES column: the common type of its rows' values (`VALUES (0.10),
        (0.20) AS v(v)`: DECIMAL(2,2))."""
        alias = values.args.get("alias")
        names = [c.name.lower() for c in (alias.columns if alias else [])]
        if name not in names:
            names = names or [f"col{i + 1}" for i in range(
                len(values.expressions[0].expressions) if values.expressions else 0)]
            if name not in names:
                return None
        index, result = names.index(name), NULL
        for row in values.expressions:
            items = row.expressions if isinstance(row, exp.Tuple) else [row]
            if index >= len(items):
                return None
            result = wider(result, self.type_of(items[index]))
        return result

    def _column(self, column: exp.Column) -> SparkType | None:
        scope = self.scope_of.get(id(column))
        name = column.name.lower()
        if scope is None:
            # outside every query scope: an UPDATE's SET, a DELETE's or MERGE's
            # condition - the statement's own tables
            for table in self.tree.find_all(exp.Table):
                if column.table and column.table.lower() not in (
                        table.alias_or_name.lower(), table.name.lower()):
                    continue
                found = self._source_type(table, name)
                if found is not None:
                    return found
            for sub in self.tree.find_all(exp.Subquery):
                if not sub.alias or (column.table and column.table.lower() !=
                                     sub.alias.lower()):
                    continue
                query = sub.this
                while isinstance(query, exp.SetOperation):
                    query = query.left
                for projection in getattr(query, "selects", []):
                    if projection.alias_or_name.lower() == name:
                        return self.type_of(projection)
            return None
        while scope is not None:
            sources = scope.sources
            if column.table:
                source = sources.get(column.table)
                if source is not None:
                    return self._source_type(source, name)
            else:
                for source in sources.values():
                    found = self._source_type(source, name)
                    if found is not None:
                        return found
            scope = scope.parent
        return None

    # ------------------------------------------------------------------- nodes
    def type_of(self, node: exp.Expression | None) -> SparkType | None:
        if node is None:
            return None
        key = id(node)
        if key not in self.memo:
            self.memo[key] = None          # cycles never recurse forever
            self.memo[key] = self._infer(node)
        return self.memo[key]

    def _infer(self, node: exp.Expression) -> SparkType | None:  # noqa: C901 - one table
        t = self.type_of
        if isinstance(node, exp.Literal):
            return literal_type(node)
        if isinstance(node, exp.Null):
            return NULL
        if isinstance(node, exp.Boolean):
            return BOOLEAN
        if isinstance(node, (exp.Cast, exp.TryCast)):
            return from_datatype(node.args.get("to"))
        if isinstance(node, exp.Column):
            return self._column(node)
        if isinstance(node, (exp.Paren, exp.Alias, exp.Window, exp.Filter)):
            return t(node.this)
        if isinstance(node, exp.Distinct):
            return t(node.expressions[0]) if len(node.expressions) == 1 else None
        if isinstance(node, exp.Neg):
            inner = t(node.this)
            return SparkType(inner.kind, inner.p, inner.s, inner.elem,
                             inner.literal_digits) if inner else None
        if isinstance(node, exp.Anonymous):
            return _NAMED.get(str(node.this).lower())
        if isinstance(node, exp.Extract):
            unit = node.this.name.upper() if isinstance(node.this, exp.Expression) else ""
            return decimal(8, 6) if unit in ("SECOND", "SECONDS", "S", "SEC", "SECS") \
                else INT
        if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv)):
            return self._arith(node, t(node.this), t(node.expression))
        if isinstance(node, exp.DateDiff) and (node.meta.get("dbx_two_arg") or
                                               node.args.get("unit") is None):
            return INT                                   # datediff(end, start)
        if isinstance(node, (exp.Count, exp.CountIf, exp.RowNumber, exp.Rank,
                             exp.DenseRank, exp.Ntile, exp.TimestampDiff, exp.DateDiff)):
            return BIGINT if not isinstance(node, (exp.Rank, exp.DenseRank, exp.RowNumber,
                                                   exp.Ntile)) else INT
        if isinstance(node, exp.Sum):
            inner = t(node.this)
            if inner is None:
                return None
            if inner.is_decimal:
                return decimal(min(MAX_PRECISION, inner.p + 10), inner.s)
            return BIGINT if inner.is_integral else \
                DOUBLE if inner.is_numeric or inner.kind == "null" else None
        if isinstance(node, exp.Avg):
            inner = t(node.this)
            if inner is None:
                return None
            if inner.is_decimal:
                return decimal(min(MAX_PRECISION, inner.p + 4), min(MAX_PRECISION, inner.s + 4))
            return DOUBLE if inner.is_numeric or inner.kind == "null" else None
        if isinstance(node, (exp.Min, exp.Max, exp.AnyValue, exp.FirstValue, exp.LastValue,
                             exp.Abs, exp.ArgMax, exp.ArgMin)):
            inner = t(node.this)
            return SparkType(inner.kind, inner.p, inner.s, inner.elem) if inner else None
        if isinstance(node, (exp.Lag, exp.Lead)):
            return wider(t(node.this), t(node.args.get("default")) or NULL)
        if isinstance(node, (exp.Stddev, exp.StddevSamp, exp.StddevPop, exp.Variance,
                             exp.VariancePop, exp.Median, exp.Quantile, exp.PercentRank,
                             exp.CumeDist, exp.Sqrt, exp.Pow, exp.Ln, exp.Log, exp.Exp,
                             exp.Sign)):
            return DOUBLE
        if isinstance(node, (exp.Floor, exp.Ceil)):
            inner = t(node.this)
            if inner is None:
                return None
            if inner.is_decimal:
                return decimal(min(MAX_PRECISION, inner.p - inner.s + 1), 0)
            return BIGINT if inner.is_numeric else None
        if isinstance(node, exp.Round):
            inner = t(node.this)
            if inner is None:
                return None
            if inner.is_decimal:
                scale = _int_arg(node.args.get("decimals"), 0)
                if scale is None:
                    return None
                least = inner.p - inner.s + 1
                if scale < 0:
                    return decimal(min(least - scale, MAX_PRECISION), 0)
                new_scale = min(inner.s, scale)
                return decimal(min(least + new_scale, MAX_PRECISION), new_scale)
            return SparkType(inner.kind) if inner.is_numeric else None
        if isinstance(node, (exp.Coalesce, exp.Greatest, exp.Least)):
            result = t(node.this)
            for other in node.expressions:
                result = wider(result, t(other))
            return result
        if isinstance(node, exp.Nullif):
            return t(node.this)
        if isinstance(node, exp.If):
            return wider(t(node.args.get("true")), t(node.args.get("false")) or NULL)
        if isinstance(node, exp.Case):
            result = t(node.args.get("default")) or NULL
            for branch in node.args.get("ifs") or []:
                result = wider(result, t(branch.args.get("true")))
            return result
        if isinstance(node, exp.Nvl2):
            return wider(t(node.args.get("true")), t(node.args.get("false")))
        if isinstance(node, (exp.ArrayAgg, exp.ArrayUniqueAgg)):
            inner = t(node.this)
            return SparkType("array", elem=inner) if inner else None
        if isinstance(node, exp.Array):
            elem = NULL
            for item in node.expressions:
                elem = wider(elem, t(item))
            return SparkType("array", elem=elem)
        if isinstance(node, (exp.Lower, exp.Upper, exp.Trim, exp.Concat, exp.ConcatWs,
                             exp.Replace, exp.RegexpReplace, exp.RegexpExtract, exp.Pad,
                             exp.Initcap, exp.Substring, exp.Left, exp.Right, exp.DPipe,
                             exp.TimeToStr, exp.CurrentCatalog, exp.CurrentSchema,
                             exp.CurrentDatabase, exp.CurrentUser, exp.SessionUser,
                             exp.CurrentVersion)):
            return STRING
        if isinstance(node, (exp.Length, exp.StrPosition, exp.Year, exp.Quarter, exp.Month,
                             exp.Day, exp.DayOfMonth, exp.DayOfYear, exp.WeekOfYear,
                             exp.Hour, exp.Minute, exp.Second, exp.DayOfWeek, exp.ArraySize)):
            return INT
        if isinstance(node, (exp.CurrentDate, exp.TsOrDsAdd, exp.AddMonths, exp.LastDay,
                             exp.TsOrDsToDate, exp.DateTrunc, exp.DateAdd, exp.DateSub)):
            return DATE
        if isinstance(node, (exp.CurrentTimestamp, exp.TimestampAdd, exp.TimestampTrunc,
                             exp.StrToTime, exp.TimeStrToTime)):
            return TIMESTAMP
        if isinstance(node, (exp.Predicate, exp.Not, exp.And, exp.Or, exp.Exists,
                             exp.ArrayContains, exp.LogicalAnd, exp.LogicalOr, exp.In,
                             exp.Between, exp.Like, exp.ILike, exp.RegexpLike, exp.Is)):
            return BOOLEAN
        if isinstance(node, exp.Subquery):
            inner = node.this
            if isinstance(inner, exp.Select) and len(inner.selects) == 1:
                return t(inner.selects[0])
            return None
        return None

    def _arith(self, node, a: SparkType | None, b: SparkType | None) -> SparkType | None:
        if a is None or b is None:
            return None
        if a.kind == "null" or b.kind == "null":
            other = b if a.kind == "null" else a
            a = b = other
        if isinstance(node, exp.IntDiv):
            return BIGINT
        if not (a.is_numeric and b.is_numeric):
            return None
        if a.is_fractional or b.is_fractional:
            return DOUBLE if not (a.kind == b.kind == "float") else FLOAT
        if a.is_integral and b.is_integral and not (a.is_decimal or b.is_decimal):
            if isinstance(node, exp.Div):
                return DOUBLE
            order = ["tinyint", "smallint", "int", "bigint"]
            return SparkType(max(a.kind, b.kind, key=order.index))
        da, db = as_decimal(a), as_decimal(b)
        p1, s1, p2, s2 = da.p, da.s, db.p, db.s
        if isinstance(node, (exp.Add, exp.Sub)):
            s = max(s1, s2)
            return adjust(max(p1 - s1, p2 - s2) + s + 1, s)
        if isinstance(node, exp.Mul):
            return adjust(p1 + p2 + 1, s1 + s2)
        if isinstance(node, exp.Div):
            s = max(6, s1 + p2 + 1)
            return adjust(p1 - s1 + s2 + s, s)
        s = max(s1, s2)                                      # Mod
        return adjust(min(p1 - s1, p2 - s2) + s, s)


def _int_arg(node: exp.Expression | None, default: int) -> int | None:
    if node is None:
        return default
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return -int(node.this.name)
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(node.name)
        except ValueError:
            return None
    return None


def _top_queries(tree: exp.Expression) -> list[exp.Expression]:
    """The outermost query expressions of a statement (the statement itself when it is
    one): what sqlglot's scope traversal starts from."""
    if isinstance(tree, exp.Query):
        return [tree]
    found = []
    for node in tree.find_all(exp.Query):
        parent = node.parent
        while parent is not None and not isinstance(parent, exp.Query):
            parent = parent.parent
        if parent is None:
            found.append(node)
    return found


def cte_references(tree: exp.Expression) -> set[int]:
    """ids of the Table nodes that name a CTE visible IN THEIR OWN SCOPE: a WITH in a
    subquery names nothing outside it (`SELECT * FROM pragma_database_list WHERE 1 IN
    (WITH pragma_database_list AS (...) ...)` reads a table, not that CTE). Anything the
    scopes cannot place is not a CTE reference - it gets qualified, and fails loudly."""
    found: set[int] = set()
    try:
        from sqlglot.optimizer.scope import traverse_scope

        for query in _top_queries(tree):
            for scope in traverse_scope(query):
                ctes = {name.lower() for name in scope.cte_sources}
                for table in scope.tables:
                    if not table.db and table.name.lower() in ctes:
                        found.add(id(table))
    except Exception:  # noqa: BLE001 - unscoped: nothing is exempt from qualification
        return set()
    return found
