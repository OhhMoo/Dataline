"""Sigma formulas <-> the expression tree (world.semantic.expr).

Sources: help.sigmacomputing.com/docs/operators-overview, /docs/function-index,
/docs/count, /docs/countif, /docs/countdistinct, /docs/lookup,
/docs/use-related-columns-in-a-workbook, /docs/use-metrics-in-a-workbook,
/docs/about-metrics, /docs/datetrunc, /docs/weekday, /docs/left, plus the
formulas in the example representation library (fetched 2026-10-03).

WHERE A FORMULA LIVES DECIDES WHAT IT MEANS. Every Sigma formula is written on
an element, and its references are relative to it. Three scopes are modelled:

  metric    a metric on data model element T: `[col]` is T's column,
            `[Metrics/m]` another metric, `Count()` counts T's rows. Metrics
            "do not support window functions or join functions" (about-metrics),
            so `Lookup` is refused here.
  column    a calculated column on data model element T: row level; `[col]` is
            T's column and `Lookup([P/c], [k], [P/k2])` reads a related element.
            Relationships cannot be used by the element that defines them
            (use-related-columns: "first create a child table"), so Lookup is
            the only way across one here.
  workbook  a column of a workbook element built on data model element B:
            `[B/col]` is B's column, `[B/Rel/col]` a column reached through the
            relationship named Rel (multi-hop: `[B/Rel1/Rel2/col]`),
            `[Metrics/m]` one of B's metrics, `[name]` another column of the
            same element, `Count()` counts B's rows.

The public `parse_expression(text, model, table=...)` reads `table`'s metric
(or, if the formula has no aggregate, calculated-column) scope; without `table`
it reads the workbook scope, with B inferred from the qualified references.
`format_expression` mirrors it.

EXACT LOWERINGS (and why each is exact):

  Sum/Avg/Min/Max([c])      Agg(func, Col)          over the element's rows
  Sum/Avg/Min/Max(expr)     IterAgg(func, T, expr)
  SumIf(v, c1, c2..)        IterAgg(sum, T, If(c1 and c2.., v))   - the
                            condition is per row inside the aggregate, so it
                            INTERSECTS with the element's filters and
                            groupings; and a sum of no non-null value is null,
                            exactly as an aggregate over no rows is BLANK
  AvgIf/MinIf/MaxIf         the same with avg/min/max
  CountIf(c..)              IterAgg(count, T, If(c, 1))  - 0 for a group with no
                            match, as Sigma's CountIf, since count counts
                            non-null values
  Count()                   Agg(count_rows, table=T)
  Count([c]) c not text     Agg(count, Col)
  Count([c]) c text         IterAgg(count, T, If([c] <> "", [c])) - Sigma counts
                            "non-Null and non-empty values" (docs/count)
  CountDistinct([c])        Agg(count_distinct, Col), not for text (Sigma skips
                            empty strings and there is no exact lowering)
  a / NullIf(b, 0)          Divide(a, b)
  If(Zn(b) = 0, x, a / b)   Divide(a, b, x)
  NullIf(a, b)              If(a = b, Null, a)
  Zn(a)                     Coalesce(a, 0)
  IsNull(a) / IsNotNull(a)  Coalesce(a <> a, True) / Coalesce(a = a, False)
  If(c1, v1, c2, v2.., e)   nested If;  Switch(x, k1, v1.., d) the same over x = k
  In(x, a, b..)             x = a or x = b ..;  Between(x, a, b): x >= a and x <= b
  Lookup([P/c], [k], [P/k2])  Related(P, c), only along a declared relationship
                            T.k -> P.k2 (anything else is a different join)
  DateTrunc("month", d)     Func(date_trunc_month) - also year, quarter and
                            "week_starting_monday" (ISO weeks; Sigma's bare
                            "week" starts on Sunday and is refused)

REFUSED BY NAME (Unsupported, vendor "Sigma"): every window, cumulative,
moving, rank and level function (CumulativeSum, MovingAvg, Rank, RowNumber,
Lag, Lead, GrandTotal, Subtotal, PercentOfTotal, DateLookback, ...); join
functions other than Lookup along a relationship; CountDistinct of text;
Median and the other statistical aggregates; and any function not in the
table below. Formatting refuses what Sigma has no formula for: CALCULATE's
filter overwrite, REMOVEFILTERS/ALLEXCEPT, time intelligence, a metric that
reads another element's columns, Lookup inside a metric.

Round trip: `parse(format(e)) == e` for every tree in canonical form (the form
`parse` produces). A non-canonical tree that means the same thing, such as
IterAgg(sum, T, Col(T, c)), comes back canonical (Agg(sum, Col(T, c))).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ...errors import ModelError, Unsupported
from ...expr import (
    Agg,
    BinOp,
    Calculate,
    Coalesce,
    Col,
    Divide,
    Expr,
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
from ...model import Model, Relationship
from ._common import METRICS

VENDOR = "Sigma"


def unsupported(feature: str, detail: str = "") -> Unsupported:
    return Unsupported(feature, VENDOR, detail)


# ------------------------------------------------------------------ tokenizer
_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<ref>\[[^\[\]]*\])
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<str>"[^"]*"|'[^']*'|“[^”]*”)
  | (?P<op><=|>=|!=|<>|[-+*/^%&=<>(),])
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
""", re.VERBOSE)


@dataclass(frozen=True)
class Tok:
    kind: str
    text: str
    pos: int


def tokenize(text: str) -> list[Tok]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ModelError(f"Sigma formula: unexpected {text[pos]!r} at {pos} in {text!r}")
        if m.lastgroup != "ws":
            out.append(Tok(m.lastgroup, m.group(), pos))
        pos = m.end()
    out.append(Tok("end", "", len(text)))
    return out


# --------------------------------------------------------------------- parser
# A small AST, lowered to the expression tree in a second pass (the lowering
# needs the scope; the grammar does not).
#   ("num", Decimal|int) ("str", s) ("bool", b) ("null",) ("ref", parts)
#   ("call", name, args) ("bin", op, l, r) ("neg", x) ("not", x)

_CMP = {"=", "!=", "<>", "<", "<=", ">", ">="}


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = tokenize(text)
        self.i = 0

    def peek(self) -> Tok:
        return self.toks[self.i]

    def take(self) -> Tok:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def expect(self, text: str) -> None:
        tok = self.take()
        if tok.text != text:
            raise ModelError(f"Sigma formula: expected {text!r} at {tok.pos} in {self.text!r}")

    def keyword(self, word: str) -> bool:
        tok = self.peek()
        return tok.kind == "name" and tok.text.lower() == word

    def parse(self):
        node = self.or_()
        if self.peek().kind != "end":
            tok = self.peek()
            raise ModelError(f"Sigma formula: unexpected {tok.text!r} at {tok.pos} "
                             f"in {self.text!r}")
        return node

    def or_(self):
        node = self.and_()
        while self.keyword("or"):
            self.take()
            node = ("bin", "or", node, self.and_())
        return node

    def and_(self):
        node = self.not_()
        while self.keyword("and"):
            self.take()
            node = ("bin", "and", node, self.not_())
        return node

    def not_(self):
        # `not x` as an operator; `Not(x)` is the function form and is parsed
        # as a call below, so a `not` followed by "(" is left to primary().
        if self.keyword("not") and self.toks[self.i + 1].text != "(":
            self.take()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        node = self.concat()
        if self.peek().kind == "op" and self.peek().text in _CMP:
            op = self.take().text
            node = ("bin", "!=" if op == "<>" else op, node, self.concat())
            if self.peek().kind == "op" and self.peek().text in _CMP:
                raise ModelError(f"Sigma formula: chained comparison in {self.text!r}")
        return node

    def concat(self):
        node = self.add()
        while self.peek().text == "&":
            self.take()
            node = ("bin", "&", node, self.add())
        return node

    def add(self):
        node = self.mul()
        while self.peek().text in ("+", "-") and self.peek().kind == "op":
            op = self.take().text
            node = ("bin", op, node, self.mul())
        return node

    def mul(self):
        node = self.pow()
        while self.peek().text in ("*", "/", "%") and self.peek().kind == "op":
            op = self.take().text
            node = ("bin", op, node, self.pow())
        return node

    def pow(self):
        node = self.unary()
        while self.peek().text == "^":
            self.take()
            node = ("bin", "^", node, self.unary())
        return node

    def unary(self):
        tok = self.peek()
        if tok.kind == "op" and tok.text == "-":
            self.take()
            nxt = self.peek()
            if nxt.kind == "num":
                self.take()
                return ("num", -_number(nxt.text))
            return ("neg", self.unary())
        if tok.kind == "op" and tok.text == "+":
            self.take()
            return self.unary()
        return self.primary()

    def primary(self):
        tok = self.take()
        if tok.kind == "num":
            return ("num", _number(tok.text))
        if tok.kind == "str":
            return ("str", tok.text[1:-1])
        if tok.kind == "ref":
            parts = tuple(p for p in tok.text[1:-1].split("/"))
            if not all(parts):
                raise ModelError(f"Sigma formula: empty reference {tok.text} in {self.text!r}")
            return ("ref", parts)
        if tok.text == "(":
            node = self.or_()
            self.expect(")")
            return node
        if tok.kind == "name":
            low = tok.text.lower()
            if self.peek().text == "(":
                self.take()
                args = []
                if self.peek().text != ")":
                    args.append(self.or_())
                    while self.peek().text == ",":
                        self.take()
                        args.append(self.or_())
                self.expect(")")
                return ("call", tok.text, tuple(args))
            if low in ("true", "false"):
                return ("bool", low == "true")
            if low == "null":
                return ("null",)
        raise ModelError(f"Sigma formula: unexpected {tok.text!r} at {tok.pos} in {self.text!r}")


def _number(text: str) -> int | Decimal:
    return int(text) if "." not in text else Decimal(text)


def parse_ast(text: str):
    return _Parser(text).parse()


def is_aggregate_formula(text: str) -> bool:
    """Does the formula aggregate (an aggregate call or a metric reference)?"""
    return _has_aggregate(parse_ast(text))


def _has_aggregate(node) -> bool:
    kind = node[0]
    if kind == "call":
        if node[1].lower() in _AGGREGATES or node[1].lower() in _COND_AGGREGATES:
            return True
        return any(_has_aggregate(a) for a in node[2])
    if kind == "ref":
        return node[1][0] == METRICS
    if kind == "bin":
        return _has_aggregate(node[2]) or _has_aggregate(node[3])
    if kind in ("neg", "not"):
        return _has_aggregate(node[1])
    return False


# ----------------------------------------------------------- function tables
_AGGREGATES = {"sum": "sum", "avg": "avg", "min": "min", "max": "max", "count": "count",
               "countdistinct": "count_distinct"}
_COND_AGGREGATES = {"sumif": "sum", "avgif": "avg", "minif": "min", "maxif": "max",
                    "countif": "count"}
_SCALAR_1 = {"abs": "abs", "year": "year", "quarter": "quarter", "month": "month",
             "day": "day", "weekday": "weekday", "upper": "upper", "lower": "lower",
             "len": "len", "trim": "trim", "floor": "floor", "ceiling": "ceiling"}
_SIGMA_NAME = {v: k.capitalize() for k, v in _SCALAR_1.items()}
_SIGMA_NAME.update({"round": "Round", "left": "Left", "right": "Right"})
_TRUNC = {"year": "date_trunc_year", "quarter": "date_trunc_quarter",
          "month": "date_trunc_month", "week_starting_monday": "date_trunc_week"}
_TRUNC_BACK = {v: k for k, v in _TRUNC.items()}

#: Functions Sigma has and this subset refuses by name (docs/function-index,
#: docs/window-functions, docs/join-functions). Anything else unknown is
#: refused the same way; the list exists so the refusal is not a guess.
REFUSED = (
    "CumulativeSum", "CumulativeAvg", "CumulativeCount", "CumulativeMin", "CumulativeMax",
    "CumulativeStdDev", "CumulativeVariance", "CumulativeCorr", "MovingSum", "MovingAvg",
    "MovingCount", "MovingMin", "MovingMax", "MovingStdDev", "MovingVariance", "MovingCorr",
    "Rank", "RankDense", "RankPercentile", "RowNumber", "Ntile", "Lag", "Lead", "First",
    "Last", "Nth", "FillDown", "GrandTotal", "Subtotal", "PercentOfTotal", "DateLookback",
    "Rollup", "LookupMatchNulls", "CountDistinctIf", "Median", "PercentileCont",
    "PercentileDisc", "StdDev", "Variance", "VariancePop", "Corr", "ListAgg",
    "ListAggDistinct", "ArrayAgg", "ArrayAggDistinct", "SumProduct", "RegressionSlope",
    "RegressionIntercept", "RegressionR2", "Concat", "Text", "Number", "Int", "Logical",
    "DateAdd", "DateDiff", "DatePart", "Today", "Now", "Mod", "Power", "Div",
)
_REFUSED = {name.lower(): name for name in REFUSED}


# ---------------------------------------------------------------------- scope
@dataclass
class Scope:
    """Where a formula lives (see the module docstring).

    `relationship_names` maps (from table, relationship name) to the
    relationship; data model files name relationships, the model does not, so
    the reader passes the names it read and everything else defaults to the
    target table's name (what Sigma does when a relationship is not renamed).
    `element_columns` is a workbook element's own columns by name (raw AST).
    """
    model: Model
    kind: str                                   # "metric" | "column" | "workbook"
    table: str | None
    relationship_names: dict[tuple[str, str], Relationship] | None = None
    element_columns: dict[str, Any] = field(default_factory=dict)
    metric_names: frozenset[str] | None = None  # metrics visible from this scope
    _resolving: set[str] = field(default_factory=set)

    def tables(self) -> dict[str, Any]:
        return {t.name: t for t in self.model.tables}

    def column_names(self, table: str) -> set[str]:
        t = self.tables().get(table)
        if t is None:
            raise ModelError(f"Sigma formula: unknown element {table!r}")
        return {c.name for c in t.columns}

    def rel_names(self) -> dict[tuple[str, str], Relationship]:
        if self.relationship_names is not None:
            return self.relationship_names
        return default_relationship_names(self.model)


def default_relationship_names(model: Model) -> dict[tuple[str, str], Relationship]:
    names: dict[tuple[str, str], Relationship] = {}
    for rel in model.relationships:
        key = (rel.from_table, rel.to_table)
        if key in names:
            raise unsupported("two relationships between the same elements",
                              f"{rel.from_table} -> {rel.to_table}: Sigma names a relationship "
                              "after its target element unless renamed")
        names[key] = rel
    return names


# ------------------------------------------------------------------- lowering
def lower(node, scope: Scope, level: str) -> Expr:
    """AST -> Expr. `level` is "measure" (aggregate level), "row" (one row of
    the scope's table) or "dimension" (a grouping column of a workbook element).
    """
    kind = node[0]
    if kind == "num":
        return Lit(node[1])
    if kind == "str":
        return Lit(node[1])
    if kind == "bool":
        return Lit(node[1])
    if kind == "null":
        return Lit(None)
    if kind == "ref":
        return _lower_ref(node[1], scope, level)
    if kind == "neg":
        inner = lower(node[1], scope, level)
        return Neg(inner)
    if kind == "not":
        return Not(lower(node[1], scope, level))
    if kind == "bin":
        return _lower_bin(node, scope, level)
    if kind == "call":
        return _lower_call(node[1], node[2], scope, level)
    raise AssertionError(node)


def _lower_bin(node, scope, level) -> Expr:
    _, op, left, right = node
    if op == "^":
        raise unsupported("operator ^ (power)")
    if op == "%":
        raise unsupported("operator % (modulo)")
    # x / NullIf(y, 0) is Divide(x, y): NullIf turns a zero denominator into
    # Null and Null propagates, which is exactly Divide with no alternate.
    if op == "/" and right[0] == "call" and right[1].lower() == "nullif" and len(right[2]) == 2 \
            and right[2][1] == ("num", 0):
        return Divide(lower(left, scope, level), lower(right[2][0], scope, level))
    a, b = lower(left, scope, level), lower(right, scope, level)
    return BinOp("<>" if op == "!=" else op, a, b)


def _lower_ref(parts: tuple[str, ...], scope: Scope, level: str) -> Expr:
    if parts[0] == METRICS:
        if len(parts) != 2:
            raise ModelError(f"Sigma formula: bad metric reference [{'/'.join(parts)}]")
        if level != "measure":
            raise unsupported("metric inside a row-level formula",
                              f"[{'/'.join(parts)}] is an aggregate")
        name = parts[1]
        visible = scope.metric_names if scope.metric_names is not None else \
            frozenset(m.name for m in scope.model.measures)
        if name not in visible:
            raise ModelError(f"Sigma formula: unknown metric {name!r}")
        return MeasureRef(name)
    if level == "measure":
        # A bare column at aggregate level is either another value column of a
        # workbook element (an aggregate itself) or an author's mistake.
        if len(parts) == 1 and parts[0] in scope.element_columns:
            return _element_column(parts[0], scope, level)
        raise ModelError(f"Sigma formula: column [{'/'.join(parts)}] outside an aggregate")
    if scope.kind in ("metric", "column"):
        table = scope.table
        if len(parts) == 1:
            if parts[0] not in scope.column_names(table):
                raise ModelError(f"Sigma formula: {table!r} has no column {parts[0]!r}")
            return Col(table, parts[0])
        raise ModelError(f"Sigma formula: [{'/'.join(parts)}] reads another element; "
                         f"use Lookup along a relationship")
    # workbook scope
    if len(parts) == 1:
        if parts[0] in scope.element_columns:
            return _element_column(parts[0], scope, level)
        if scope.table is not None and parts[0] in scope.column_names(scope.table):
            return Col(scope.table, parts[0])
        raise ModelError(f"Sigma formula: unknown column [{parts[0]}]")
    head, *path, column = parts
    if scope.table is None:
        scope.table = head
    if head != scope.table:
        raise ModelError(f"Sigma formula: [{'/'.join(parts)}] is not a column of the "
                         f"element's source {scope.table!r}")
    if not path:
        if column not in scope.column_names(head):
            raise ModelError(f"Sigma formula: {head!r} has no column {column!r}")
        return Col(head, column)
    target = follow(scope, head, path)
    if column not in scope.column_names(target):
        raise ModelError(f"Sigma formula: {target!r} has no column {column!r}")
    return Col(target, column) if level == "dimension" else Related(target, column)


def follow(scope: Scope, start: str, path: list[str]) -> str:
    names = scope.rel_names()
    table = start
    for name in path:
        rel = names.get((table, name))
        if rel is None:
            raise ModelError(f"Sigma formula: {table!r} has no relationship {name!r}")
        table = rel.to_table
    return table


def _element_column(name: str, scope: Scope, level: str) -> Expr:
    if name in scope._resolving:
        raise ModelError(f"Sigma formula: column [{name}] refers to itself")
    scope._resolving.add(name)
    try:
        return lower(scope.element_columns[name], scope, level)
    finally:
        scope._resolving.discard(name)


def _lit_int(node) -> int | None:
    return node[1] if node[0] == "num" and isinstance(node[1], int) else None


def _and_all(conds: list[Expr]) -> Expr:
    out = conds[0]
    for c in conds[1:]:
        out = BinOp("and", out, c)
    return out


def _lower_call(name: str, args: tuple, scope: Scope, level: str) -> Expr:
    low = name.lower()
    n = len(args)

    def arg(i, lvl=level):
        return lower(args[i], scope, lvl)

    def arity(*ok):
        if n not in ok:
            raise ModelError(f"Sigma formula: {name} takes {' or '.join(map(str, ok))} "
                             f"arguments, got {n}")

    if low in _AGGREGATES or low in _COND_AGGREGATES:
        if level != "measure":
            raise unsupported(f"aggregate {name} in a row-level formula",
                              "a calculated column is evaluated per row")
        return _lower_aggregate(name, low, args, scope)
    if low == "lookup":
        return _lower_lookup(args, scope, level)
    if low in _REFUSED:
        raise unsupported(_REFUSED[low])
    if low == "if":
        if n < 2:
            raise ModelError("Sigma formula: If needs a condition and a value")
        out = _if_chain([arg(i) for i in range(n)])
        # If(Zn(b) = 0, x, a / b) is Divide(a, b, x): Zn(b) = 0 holds exactly
        # when b is Null or zero, which is when Divide answers its alternate.
        if n == 3 and isinstance(out.else_, BinOp) and out.else_.op == "/" \
                and out.cond == BinOp("=", Coalesce((out.else_.right, Lit(0))), Lit(0)):
            return Divide(out.else_.left, out.else_.right, out.then)
        return out
    if low == "switch":
        if n < 3:
            raise ModelError("Sigma formula: Switch needs a value and at least one case")
        subject = arg(0)
        rest = [arg(i) for i in range(1, n)]
        chain = []
        for i in range(0, len(rest) - 1, 2):
            chain += [BinOp("=", subject, rest[i]), rest[i + 1]]
        if len(rest) % 2:
            chain.append(rest[-1])
        return _if_chain(chain)
    if low == "coalesce":
        if n < 1:
            raise ModelError("Sigma formula: Coalesce needs arguments")
        return Coalesce(tuple(arg(i) for i in range(n)))
    if low == "zn":
        arity(1)
        return Coalesce((arg(0), Lit(0)))
    if low == "isnull":
        arity(1)
        x = arg(0)
        return Coalesce((BinOp("<>", x, x), Lit(True)))
    if low == "isnotnull":
        arity(1)
        x = arg(0)
        return Coalesce((BinOp("=", x, x), Lit(False)))
    if low == "nullif":
        arity(2)
        a, b = arg(0), arg(1)
        return If(BinOp("=", a, b), Lit(None), a)
    if low in ("and", "or"):
        if n < 2:
            raise ModelError(f"Sigma formula: {name} needs two or more arguments")
        out = arg(0)
        for i in range(1, n):
            out = BinOp(low, out, arg(i))
        return out
    if low == "not":
        arity(1)
        return Not(arg(0))
    if low == "in":
        if n < 2:
            raise ModelError("Sigma formula: In needs a value and candidates")
        subject = arg(0)
        out = BinOp("=", subject, arg(1))
        for i in range(2, n):
            out = BinOp("or", out, BinOp("=", subject, arg(i)))
        return out
    if low == "between":
        arity(3)
        x = arg(0)
        return BinOp("and", BinOp(">=", x, arg(1)), BinOp("<=", x, arg(2)))
    if low == "date":
        arity(1)
        if args[0][0] != "str":
            raise unsupported("Date of a non-literal (type conversion)")
        return Lit(_parse_date(args[0][1]))
    if low == "datetrunc":
        arity(2)
        if args[0][0] != "str":
            raise unsupported("DateTrunc with a non-literal precision")
        precision = args[0][1].lower()
        if precision in ("week", "week_starting_sunday"):
            raise unsupported(f'DateTrunc("{args[0][1]}")',
                              "Sigma weeks start on Sunday; only week_starting_monday "
                              "lowers to an ISO week")
        if precision not in _TRUNC:
            raise unsupported(f'DateTrunc("{args[0][1]}")')
        return Func(_TRUNC[precision], (arg(1),))
    if low in ("round",):
        arity(1, 2)
        return Func("round", tuple(arg(i) for i in range(n)))
    if low in ("floor", "ceiling"):
        if n != 1:
            raise unsupported(f"{name} with a factor")
        return Func(_SCALAR_1[low], (arg(0),))
    if low in ("year", "month", "day", "weekday"):
        if n != 1:
            raise unsupported(f"{name} with a time zone")
        return Func(_SCALAR_1[low], (arg(0),))
    if low in _SCALAR_1:
        arity(1)
        return Func(_SCALAR_1[low], (arg(0),))
    if low in ("left", "right"):
        arity(2)
        count = _lit_int(args[1])
        if count is None or count < 0:
            # Left(s, -n) drops n characters from the end (docs/left); only a
            # non-negative literal count means the same thing everywhere.
            raise unsupported(f"{name} with a negative or non-literal length")
        return Func(low, (arg(0), Lit(count)))
    raise unsupported(name)


def _if_chain(items: list[Expr]) -> Expr:
    cond, then, rest = items[0], items[1], items[2:]
    if not rest:
        return If(cond, then)
    if len(rest) == 1:
        return If(cond, then, rest[0])
    return If(cond, then, _if_chain(rest))


def _base_table(scope: Scope, name: str) -> str:
    if scope.table is None:
        raise ModelError(f"Sigma formula: {name} needs the element it aggregates "
                         f"(pass table=, or qualify a column reference)")
    return scope.table


def _lower_aggregate(name: str, low: str, args: tuple, scope: Scope) -> Expr:
    # The table is fixed by the first qualified reference in workbook scope,
    # so lower the arguments before asking which table this is.
    if low == "count" and not args:
        return Agg("count_rows", table=_base_table(scope, name))
    if low in _COND_AGGREGATES:
        func = _COND_AGGREGATES[low]
        if func == "count":
            if not args:
                raise ModelError("Sigma formula: CountIf needs a condition")
            conds = [lower(a, scope, "row") for a in args]
            value: Expr = Lit(1)
        else:
            if len(args) < 2:
                raise ModelError(f"Sigma formula: {name} needs a value and a condition")
            value = lower(args[0], scope, "row")
            conds = [lower(a, scope, "row") for a in args[1:]]
        return IterAgg(func, _base_table(scope, name), If(_and_all(conds), value))
    if len(args) != 1:
        raise ModelError(f"Sigma formula: {name} takes one argument")
    arg = lower(args[0], scope, "row")
    table = _base_table(scope, name)
    func = _AGGREGATES[low]
    dtype = infer_type(arg, scope.model)
    if func == "count_distinct":
        if not isinstance(arg, Col):
            raise unsupported("CountDistinct of a formula")
        if dtype == "string":
            raise unsupported("CountDistinct of a text column",
                              "Sigma skips empty strings; the model counts them")
        return Agg("count_distinct", arg)
    if func == "count":
        if dtype == "string":
            return IterAgg("count", table, If(BinOp("<>", arg, Lit("")), arg))
        if isinstance(arg, Col) and arg.table == table:
            return Agg("count", arg)
        return IterAgg("count", table, arg)
    if isinstance(arg, Col) and arg.table == table:
        return Agg(func, arg)
    return IterAgg(func, table, arg)


def _lower_lookup(args: tuple, scope: Scope, level: str) -> Expr:
    if scope.kind != "column":
        raise unsupported("Lookup outside a calculated column",
                          "metrics do not support join functions (docs/about-metrics)")
    if level != "row":
        raise unsupported("Lookup at aggregate level")
    if len(args) != 3:
        raise unsupported("Lookup with several keys" if len(args) > 3 else "Lookup",
                          "only a single-key Lookup along a relationship lowers exactly")
    target, local, external = args
    if target[0] != "ref" or len(target[1]) != 2:
        raise unsupported("Lookup of a formula", "only Lookup([Element/Column], ...) lowers")
    if local[0] != "ref" or len(local[1]) != 1 or external[0] != "ref" \
            or len(external[1]) != 2 or external[1][0] != target[1][0]:
        raise unsupported("Lookup keys", "expected Lookup([P/c], [key], [P/key])")
    other, column = target[1]
    for rel in scope.model.relationships:
        if (rel.from_table, rel.from_column, rel.to_table, rel.to_column) == \
                (scope.table, local[1][0], other, external[1][1]):
            if column not in scope.column_names(other):
                raise ModelError(f"Sigma formula: {other!r} has no column {column!r}")
            return Related(other, column)
    raise unsupported("Lookup without a matching relationship",
                      f"no relationship {scope.table}.{local[1][0]} -> "
                      f"{other}.{external[1][1]}; a Lookup on other keys is a different join")


def _parse_date(text: str) -> dt.date | dt.datetime:
    try:
        if len(text) == 10:
            return dt.date.fromisoformat(text)
        return dt.datetime.fromisoformat(text)
    except ValueError:
        raise ModelError(f"Sigma formula: Date({text!r}) is not an ISO date") from None


# -------------------------------------------------------------- type inference
_NUMERIC = ("int", "decimal", "float")


def infer_type(expr: Expr, model: Model) -> str | None:
    """The model data type a formula produces, or None when it cannot be told
    (a bare Null). Sigma itself only has Number/Text/Date/Logical; the model's
    finer types follow SQL's rules for the warehouse the formula runs on."""
    t = _infer(expr, model, set())
    return t


def _column_type(model: Model, table: str, column: str) -> str:
    for t in model.tables:
        if t.name == table:
            for c in t.columns:
                if c.name == column:
                    return c.data_type
    raise ModelError(f"unknown column {table}.{column}")


def _infer(e: Expr, model: Model, seen: set) -> str | None:
    if isinstance(e, Lit):
        v = e.value
        if v is None:
            return None
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "int"
        if isinstance(v, Decimal):
            return "decimal"
        if isinstance(v, float):
            return "float"
        if isinstance(v, str):
            return "string"
        if isinstance(v, dt.datetime):
            return "timestamp"
        if isinstance(v, dt.date):
            return "date"
        return None
    if isinstance(e, Col | Related):
        return _column_type(model, e.table, e.column)
    if isinstance(e, MeasureRef):
        if e.name in seen:
            raise ModelError(f"measure {e.name!r} refers to itself")
        for m in model.measures:
            if m.name == e.name:
                return _infer(m.expr, model, seen | {e.name})
        raise ModelError(f"unknown measure {e.name!r}")
    if isinstance(e, BinOp):
        if e.op in ("=", "<>", "<", ">", "<=", ">=", "and", "or"):
            return "bool"
        if e.op == "&":
            return "string"
        a, b = _infer(e.left, model, seen), _infer(e.right, model, seen)
        return _numeric(a, b, e.op)
    if isinstance(e, Neg):
        return _infer(e.arg, model, seen)
    if isinstance(e, Not):
        return "bool"
    if isinstance(e, Divide):
        a, b = _infer(e.num, model, seen), _infer(e.den, model, seen)
        return _numeric(a, b, "/")
    if isinstance(e, If):
        t = _infer(e.then, model, seen)
        return t if t is not None else (_infer(e.else_, model, seen) if e.else_ else None)
    if isinstance(e, Coalesce):
        for a in e.args:
            t = _infer(a, model, seen)
            if t is not None:
                return t
        return None
    if isinstance(e, Func):
        if e.name in ("year", "quarter", "month", "day", "weekday", "len"):
            return "int"
        if e.name.startswith("date_trunc_"):
            return "date"
        if e.name in ("upper", "lower", "trim", "left", "right"):
            return "string"
        return _infer(e.args[0], model, seen)
    if isinstance(e, Agg):
        if e.func in ("count", "count_distinct", "count_rows"):
            return "int"
        t = _infer(e.arg, model, seen)
        return _agg_type(e.func, t)
    if isinstance(e, IterAgg):
        if e.func == "count":
            return "int"
        return _agg_type(e.func, _infer(e.expr, model, seen))
    if isinstance(e, Calculate | TimeShift):
        return _infer(e.expr, model, seen)
    return None


def _numeric(a, b, op) -> str | None:
    if a is None or b is None:
        return a or b
    if "float" in (a, b):
        return "float"
    if op == "/":
        return "decimal"
    if a == b == "int":
        return "int"
    if a in _NUMERIC and b in _NUMERIC:
        return "decimal"
    return a


def _agg_type(func, t):
    if func == "avg":
        return "float" if t == "float" else "decimal"
    return t


# ------------------------------------------------------------------ formatting
# Precedence, loosest first. A child is parenthesized when it binds looser than
# its position requires.
_PREC = {"or": 1, "and": 2, "not": 3, "=": 4, "<>": 4, "<": 4, ">": 4, "<=": 4, ">=": 4,
         "&": 5, "+": 6, "-": 6, "*": 7, "/": 7}
_OP_TEXT = {"<>": "!=", "and": "and", "or": "or"}
_ATOM = 10


@dataclass
class FormatScope:
    model: Model
    kind: str              # "metric" | "column" | "workbook"
    table: str | None
    relationship_names: dict[tuple[str, str], Relationship] | None = None

    def path_to(self, target: str) -> list[str]:
        """Relationship names from `table` to `target`, shortest first."""
        names = self.relationship_names
        if names is None:
            names = default_relationship_names(self.model)
        frontier = [(self.table, [])]
        seen = {self.table}
        while frontier:
            nxt = []
            for table, path in frontier:
                for (src, name), rel in sorted(names.items()):
                    if src == table and rel.to_table not in seen:
                        if rel.to_table == target:
                            return path + [name]
                        seen.add(rel.to_table)
                        nxt.append((rel.to_table, path + [name]))
            frontier = nxt
        raise unsupported(f"{target} is not reachable from {self.table}",
                          "no chain of many-to-one relationships leads there")


def format_formula(expr: Expr, scope: FormatScope) -> str:
    return _fmt(expr, scope, "measure")[0]


def format_row_formula(expr: Expr, scope: FormatScope) -> str:
    return _fmt(expr, scope, "row")[0]


def _paren(text_prec: tuple[str, int], need: int) -> str:
    text, prec = text_prec
    return f"({text})" if prec < need else text


def _ref(*parts: str) -> str:
    for p in parts:
        if any(ch in p for ch in "[]/"):
            raise ModelError(f"name {p!r} cannot be written in a Sigma reference")
    return "[" + "/".join(parts) + "]"


def _literal(value) -> str:
    if value is None:
        return "Null"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        text = repr(value)
        if "e" in text or "inf" in text or "nan" in text:
            raise unsupported(f"number literal {text}")
        return text
    if isinstance(value, str):
        if '"' not in value:
            return f'"{value}"'
        if "'" not in value:
            return f"'{value}'"
        raise ModelError(f"text {value!r} has both quote characters")
    if isinstance(value, dt.datetime):
        return f'Date("{value.isoformat(sep=" ")}")'
    if isinstance(value, dt.date):
        return f'Date("{value.isoformat()}")'
    raise unsupported(f"literal of type {type(value).__name__}")


def _col_text(col: Col, scope: FormatScope, level: str) -> str:
    if scope.kind in ("metric", "column"):
        if col.table != scope.table:
            raise unsupported("formula reading another element's column",
                              f"{col.table}.{col.column} on element {scope.table}: a Sigma "
                              f"metric or calculated column reads its own element only")
        return _ref(col.column)
    if scope.table is None:
        scope.table = col.table
    if col.table == scope.table:
        return _ref(scope.table, col.column)
    if level == "dimension":
        return _ref(scope.table, *scope.path_to(col.table), col.column)
    if level == "row":
        raise unsupported("aggregate over another element's column",
                          f"{col.table}.{col.column} from {scope.table}")
    raise ModelError(f"column {col.table}.{col.column} outside an aggregate")


def format_dimension(col: Col, scope: FormatScope) -> str:
    """A grouping or filter column of a workbook element: `[B/col]` or
    `[B/Rel/col]` along the relationships from the element's source B."""
    return _col_text(col, scope, "dimension")


def _fmt(e: Expr, scope: FormatScope, level: str) -> tuple[str, int]:
    if isinstance(e, Lit):
        text = _literal(e.value)
        neg = isinstance(e.value, int | Decimal | float) and not isinstance(e.value, bool) \
            and e.value < 0
        return text, (9 if neg else _ATOM)
    if isinstance(e, Col):
        return _col_text(e, scope, level), _ATOM
    if isinstance(e, Related):
        if scope.kind == "metric":
            raise unsupported("Lookup in a metric",
                              "metrics do not support join functions (docs/about-metrics); "
                              "add the related column as a calculated column first")
        if scope.kind == "column":
            for rel in scope.model.relationships:
                if rel.from_table == scope.table and rel.to_table == e.table:
                    return (f"Lookup({_ref(e.table, e.column)}, {_ref(rel.from_column)}, "
                            f"{_ref(e.table, rel.to_column)})"), _ATOM
            raise unsupported("Lookup across more than one relationship",
                              f"{scope.table} has no relationship to {e.table}")
        if scope.table is None:
            raise ModelError("a related column needs the element it is read from (table=)")
        return _ref(scope.table, *scope.path_to(e.table), e.column), _ATOM
    if isinstance(e, MeasureRef):
        if level != "measure":
            raise unsupported("metric inside a row-level formula", e.name)
        measure = next((m for m in scope.model.measures if m.name == e.name), None)
        if measure is None:
            raise ModelError(f"unknown measure {e.name!r}")
        return _ref(METRICS, e.name), _ATOM
    if isinstance(e, BinOp):
        if e.op in ("=", "<>", "<", ">", "<=", ">="):
            p = _PREC[e.op]
            return (f"{_paren(_fmt(e.left, scope, level), p + 1)} {_OP_TEXT.get(e.op, e.op)} "
                    f"{_paren(_fmt(e.right, scope, level), p + 1)}"), p
        p = _PREC[e.op]
        right = _paren(_fmt(e.right, scope, level), p + 1)
        if e.op == "/" and isinstance(e.right, If) and _is_nullif(e.right):
            # `a / NullIf(b, 0)` is read back as Divide(a, b); a plain division
            # by a NullIf must therefore be written in its If spelling.
            right = _fmt_if(e.right, scope, level, spell_nullif=False)
        return f"{_paren(_fmt(e.left, scope, level), p)} {_OP_TEXT.get(e.op, e.op)} {right}", p
    if isinstance(e, Neg):
        inner = _fmt(e.arg, scope, level)
        if isinstance(e.arg, Lit) and not isinstance(e.arg.value, bool) \
                and isinstance(e.arg.value, int | Decimal | float):
            # "-5" reads back as the literal -5; the negation of 5 is "-(5)".
            return f"-({inner[0]})", 9
        return f"-{_paren(inner, _ATOM)}", 9
    if isinstance(e, Not):
        return f"Not({_fmt(e.arg, scope, level)[0]})", _ATOM
    if isinstance(e, Divide):
        num = _paren(_fmt(e.num, scope, level), 7)
        den = _fmt(e.den, scope, level)[0]
        if e.alternate is None:
            return f"{num} / NullIf({den}, 0)", 7
        alt = _fmt(e.alternate, scope, level)[0]
        return f"If(Zn({den}) = 0, {alt}, {num} / {_paren(_fmt(e.den, scope, level), 8)})", _ATOM
    if isinstance(e, If):
        return _fmt_if(e, scope, level), _ATOM
    if isinstance(e, Coalesce):
        if len(e.args) == 2:
            x, d = e.args
            if isinstance(x, BinOp) and x.left == x.right and type(d.value) is bool:
                if x.op == "<>" and d == Lit(True):
                    return f"IsNull({_fmt(x.left, scope, level)[0]})", _ATOM
                if x.op == "=" and d == Lit(False):
                    return f"IsNotNull({_fmt(x.left, scope, level)[0]})", _ATOM
            if d == Lit(0) and type(d.value) is int:
                return f"Zn({_fmt(x, scope, level)[0]})", _ATOM
        return "Coalesce(" + ", ".join(_fmt(a, scope, level)[0] for a in e.args) + ")", _ATOM
    if isinstance(e, Func):
        if e.name in _TRUNC_BACK:
            return f'DateTrunc("{_TRUNC_BACK[e.name]}", {_fmt(e.args[0], scope, level)[0]})', _ATOM
        if e.name in ("left", "right"):
            n = e.args[1]
            if not (isinstance(n, Lit) and type(n.value) is int and n.value >= 0):
                raise unsupported(f"{_SIGMA_NAME[e.name]} with a negative or non-literal length")
        name = _SIGMA_NAME.get(e.name)
        if name is None:
            raise unsupported(f"function {e.name}")
        return name + "(" + ", ".join(_fmt(a, scope, level)[0] for a in e.args) + ")", _ATOM
    if isinstance(e, Agg | IterAgg):
        if level != "measure":
            raise unsupported("aggregate in a row-level formula")
        return _fmt_agg(e, scope), _ATOM
    if isinstance(e, Calculate):
        _refuse_calculate(e)
    if isinstance(e, TimeShift):
        raise unsupported(f"time intelligence ({e.kind})",
                          "Sigma has no date-table time intelligence; DateLookback is a "
                          "window function over the element's rows")
    raise unsupported(f"expression {type(e).__name__}")


def _is_nullif(e: If) -> bool:
    return (isinstance(e.cond, BinOp) and e.cond.op == "=" and e.then == Lit(None)
            and e.else_ is not None and e.else_ == e.cond.left)


def _fmt_if(e: If, scope, level, spell_nullif: bool = True) -> str:
    if spell_nullif and _is_nullif(e):
        return (f"NullIf({_fmt(e.cond.left, scope, level)[0]}, "
                f"{_fmt(e.cond.right, scope, level)[0]})")
    parts = []
    node: Expr | None = e
    while True:
        parts += [_fmt(node.cond, scope, level)[0], _fmt(node.then, scope, level)[0]]
        nxt = node.else_
        if isinstance(nxt, If) and not _is_nullif(nxt):
            node = nxt
            continue
        if nxt is not None:
            parts.append(_fmt(nxt, scope, level)[0])
        break
    return "If(" + ", ".join(parts) + ")"


_AGG_NAME = {"sum": "Sum", "avg": "Avg", "min": "Min", "max": "Max", "count": "Count",
             "count_distinct": "CountDistinct"}


def _fmt_agg(e: Agg | IterAgg, scope: FormatScope) -> str:
    table = e.table if isinstance(e, IterAgg) or e.func == "count_rows" else e.arg.table
    if scope.kind in ("metric", "column"):
        if table != scope.table:
            raise unsupported("metric aggregating another element",
                              f"{table} from element {scope.table}: a Sigma metric reads its "
                              "own element's columns")
    else:
        if scope.table is None:
            scope.table = table
        if table != scope.table:
            raise unsupported("aggregate over another element",
                              f"{table} from an element built on {scope.table}")
    if isinstance(e, Agg):
        if e.func == "count_rows":
            return "Count()"
        dtype = _column_type(scope.model, e.arg.table, e.arg.column)
        if dtype == "string" and e.func in ("count", "count_distinct"):
            raise unsupported(f"{_AGG_NAME[e.func]} of a text column",
                              "Sigma skips empty strings; the model counts them")
        return f"{_AGG_NAME[e.func]}({_fmt(e.arg, scope, 'row')[0]})"
    body = e.expr
    if isinstance(body, If) and body.else_ is None and not _is_nullif(body):
        cond = _fmt(body.cond, scope, "row")[0]
        if e.func == "count" and body.then == Lit(1) and type(body.then.value) is int:
            return f"CountIf({cond})"
        if e.func == "count":
            x = body.then
            if body.cond == BinOp("<>", x, Lit("")) and infer_type(x, scope.model) == "string":
                return f"Count({_fmt(x, scope, 'row')[0]})"
        else:
            return f"{_AGG_NAME[e.func]}If({_fmt(body.then, scope, 'row')[0]}, {cond})"
    if e.func == "count" and infer_type(body, scope.model) == "string":
        raise unsupported("Count of text",
                          "Sigma skips empty strings; the model counts them")
    return f"{_AGG_NAME[e.func]}({_fmt(body, scope, 'row')[0]})"


def _refuse_calculate(e: Calculate):
    for mod in e.modifiers:
        if isinstance(mod, RemoveFilters):
            raise unsupported("REMOVEFILTERS / ALL",
                              "Sigma has no formula that drops one filter; GrandTotal and "
                              "PercentOfTotal depend on the element's grouping")
        if isinstance(mod, KeepOnly):
            raise unsupported("ALLEXCEPT", "Sigma has no formula that drops filters")
    if all(isinstance(m, KeepFilters) for m in e.modifiers):
        raise unsupported("KEEPFILTERS around a measure",
                          "write the condition inside the aggregate (SumIf, CountIf, "
                          "Sum(If(...)))")
    raise unsupported("CALCULATE filter overwrite",
                      "Sigma conditions (SumIf, CountIf, If inside an aggregate) intersect "
                      "with the element's filters and groupings; a filter that replaces "
                      "the context has no Sigma formula")


# ----------------------------------------------------------------- public API
def parse_expression(text: str, model: Model, *, table: str | None = None) -> Expr:
    """A Sigma formula -> Expr.

    `table` is the data model element the formula is written on: an aggregate
    formula is read as one of its metrics, any other formula as one of its
    calculated columns. Without `table` the formula is read as a column of a
    workbook element built on the data model, the element being the one its
    qualified references name (`[Orders/order_amount]`).
    """
    ast = parse_ast(text)
    if table is not None:
        if table not in {t.name for t in model.tables}:
            raise ModelError(f"unknown table {table!r}")
        if _has_aggregate(ast):
            return lower(ast, Scope(model, "metric", table), "measure")
        return lower(ast, Scope(model, "column", table), "row")
    scope = Scope(model, "workbook", _qualified_head(ast))
    return lower(ast, scope, "measure" if _has_aggregate(ast) else "row")


def _qualified_head(ast) -> str | None:
    heads: set[str] = set()

    def walk(node):
        if node[0] == "ref" and len(node[1]) > 1 and node[1][0] != METRICS:
            heads.add(node[1][0])
        elif node[0] == "call":
            for a in node[2]:
                walk(a)
        elif node[0] == "bin":
            walk(node[2])
            walk(node[3])
        elif node[0] in ("neg", "not"):
            walk(node[1])

    walk(ast)
    if len(heads) > 1:
        raise ModelError(f"Sigma formula reads several elements: {sorted(heads)}")
    return next(iter(heads), None)


def format_expression(expr: Expr, model: Model, *, table: str | None = None) -> str:
    """Expr -> a Sigma formula (the inverse of `parse_expression`)."""
    level = "measure" if _is_aggregate_tree(expr) else "row"
    if table is not None:
        kind = "metric" if level == "measure" else "column"
        return _fmt(expr, FormatScope(model, kind, table), level)[0]
    scope = FormatScope(model, "workbook", _tree_table(expr))
    return _fmt(expr, scope, level)[0]


def _is_aggregate_tree(e: Expr) -> bool:
    if isinstance(e, Agg | IterAgg | MeasureRef | Calculate | TimeShift):
        return True
    for value in vars(e).values():
        if isinstance(value, tuple):
            if any(_is_aggregate_tree(v) for v in value if hasattr(v, "__dataclass_fields__")):
                return True
        elif hasattr(value, "__dataclass_fields__") and _is_aggregate_tree(value):
            return True
    return False


def _tree_table(e: Expr) -> str | None:
    """The one element a workbook-scope formula is built on."""
    found: set[str] = set()

    def walk(x):
        if isinstance(x, Agg):
            found.add(x.table if x.func == "count_rows" else x.arg.table)
            return
        if isinstance(x, IterAgg):
            found.add(x.table)
            return
        if isinstance(x, Col):
            found.add(x.table)
            return
        for value in vars(x).values():
            if isinstance(value, tuple):
                for v in value:
                    if hasattr(v, "__dataclass_fields__"):
                        walk(v)
            elif hasattr(value, "__dataclass_fields__"):
                walk(value)

    walk(e)
    if len(found) > 1:
        raise unsupported("formula over several elements", ", ".join(sorted(found)))
    return next(iter(found), None)


__all__ = ["Scope", "FormatScope", "format_expression", "format_formula",
           "format_row_formula", "infer_type", "is_aggregate_formula", "lower",
           "parse_ast", "parse_expression", "tokenize", "unsupported", "REFUSED", "VENDOR"]
