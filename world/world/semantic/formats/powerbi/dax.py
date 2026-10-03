"""DAX (the subset a measure in this world may use) <-> world.semantic.expr.

WHAT THIS IS. A Power BI measure is DAX text in a TMDL file. The engine
evaluates `expr` trees, never DAX, so this module is the one place DAX meaning
is decided: `parse_expression` lowers text into the tree exactly or refuses by
name, and `format_expression` writes a tree back as the DAX an engineer would
type (`CALCULATE ( [Gross Revenue], Orders[order_status] = "completed" )`), in
the DAX Formatter "short line" spacing SQLBI popularised, because that is what
checked-in measures look like.

THE SUBSET, and why each line of it is exact (sources below):

  SUM AVERAGE MIN MAX COUNT COUNTA DISTINCTCOUNTNOBLANK   -> Agg over one column
  COUNTROWS ( T )                                         -> Agg count_rows
  SUMX AVERAGEX MINX MAXX COUNTX ( T, row expr )          -> IterAgg; RELATED inside
  DIVIDE IF SWITCH COALESCE BLANK TRUE FALSE AND OR NOT   -> scalar nodes
  ABS ROUND FLOOR(x,1) CEILING(x,1) INT YEAR QUARTER MONTH DAY WEEKDAY(x[,1])
  UPPER LOWER LEN TRIM LEFT RIGHT                         -> Func
  DATE ( y, m, d ) with constant arguments                -> Lit(date); the three
      DATE ( YEAR ( x ), ... , 1 ) shapes and x - WEEKDAY ( x, 3 ) -> date_trunc_*
  CALCULATE ( e, filter... )                               -> Calculate
      filters: boolean column filters (= <> < > <= >= IN, NOT, ||, &&), ISBLANK,
      TREATAS ( {..}, col ), FILTER ( ALL|VALUES ( col ), cond ), ALL,
      REMOVEFILTERS, ALLEXCEPT, KEEPFILTERS
  TOTALYTD TOTALQTD TOTALMTD; CALCULATE with SAMEPERIODLASTYEAR, DATESYTD/QTD/MTD,
  PREVIOUSMONTH, PREVIOUSYEAR                             -> TimeShift
  VAR ... RETURN of scalars                               -> inlined

CALCULATE ORDER IS DAX'S, NOT THE ARGUMENT ORDER. DAX evaluates every filter
argument in the outer context, applies the modifiers (ALL, REMOVEFILTERS,
ALLEXCEPT) first, then the explicit filters, ANDed together, overwriting the
outer filters on their columns; KEEPFILTERS arguments intersect instead
(SQLBI, "Order of evaluation in CALCULATE parameters"). The contract applies
`Calculate.modifiers` strictly in order, so lowering emits them in DAX's order:
removals, then the first filter per column (REPLACE), then everything else
wrapped in KeepFilters (INTERSECT) - which is what DAX's AND of two filters on
one column is. A filter on the date column of a marked date table also gets
the ALL ( 'Date' ) DAX adds silently (dax.guide "Time intelligence on marked
date tables"), so a visual grouped by Date[year] does not keep its year filter
where DAX would drop it.

REFUSED BY NAME (errors.Unsupported(feature, "Power BI")): every function not
listed above ("DAX function FILTER" when FILTER is not the column-filter shape,
"DAX function VALUES", ...), USERELATIONSHIP, CROSSFILTER, EARLIER, context
transition (a measure or CALCULATE inside an iterator), nested iterators, VAR
over a table, a VAR used under a different filter context than it was defined
in, == strict equality, ^, IN outside a filter, DISTINCTCOUNT (it counts BLANK
as a value; the engine's count_distinct never counts None - DISTINCTCOUNTNOBLANK
is exact), WEEKDAY with a return_type other than 1 (the engine pins
Sunday=1..Saturday=7), `= BLANK()` in a filter (DAX also matches 0 and ""; ISBLANK is
exact), time intelligence over anything but the date column of a marked date
table, and a time-intelligence filter combined with another filter that
touches the date table. Malformed text and unknown names are errors.ModelError
carrying DAX's own message and the line/column.

Sources: DAX function and operator reference, learn.microsoft.com/dax
(dax-function-reference, dax-operator-reference, dax-syntax-reference,
calculate-function-dax, keepfilters-function-dax, totalytd-function-dax);
SQLBI "Understanding context transition", "Order of evaluation in CALCULATE
parameters", "Variables in DAX"; DAX Formatter (daxformatter.com) spacing.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
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
from ...model import Model, Table

VENDOR = "Power BI"


def unsupported(feature: str, detail: str = "") -> Unsupported:
    return Unsupported(feature, VENDOR, detail)


# ======================================================================
# Tokens
# ======================================================================
@dataclass(frozen=True)
class Tok:
    kind: str           # num str table col ident op eof
    text: str           # the source text of the token
    value: Any          # decoded value (number, string, name)
    pos: int            # offset into the source


TWO_CHAR = ("&&", "||", "<=", ">=", "<>", "==")
ONE_CHAR = "=<>+-*/&^(),{}"


def position(text: str, pos: int) -> tuple[int, int]:
    """1-based (line, column) of an offset - DAX reports both."""
    line = text.count("\n", 0, pos) + 1
    col = pos - (text.rfind("\n", 0, pos) + 1) + 1
    return line, col


def syntax_error(text: str, pos: int, near: str) -> ModelError:
    line, col = position(text, pos)
    return ModelError(f"DAX syntax error at line {line}, column {col}: "
                      f"The syntax for '{near}' is incorrect.")


def tokenize(text: str) -> list[Tok]:
    out: list[Tok] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n ":
            i += 1
            continue
        if text.startswith("--", i) or text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise syntax_error(text, i, "/*")
            i = end + 2
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and text[j].isdigit():
                j += 1
            if j < n and text[j] == ".":
                j += 1
                while j < n and text[j].isdigit():
                    j += 1
            if j < n and text[j] in "eE" and j + 1 < n and (
                    text[j + 1].isdigit() or text[j + 1] in "+-"):
                j += 2
                while j < n and text[j].isdigit():
                    j += 1
            raw = text[i:j]
            if "." in raw or "e" in raw.lower():
                value: Any = Decimal(raw)
            else:
                value = int(raw)
            out.append(Tok("num", raw, value, i))
            i = j
            continue
        if ch in "\"'[":
            close = {"\"": "\"", "'": "'", "[": "]"}[ch]
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise syntax_error(text, i, ch)
                if text[j] == close:
                    if j + 1 < n and text[j + 1] == close:     # doubled = escaped
                        buf.append(close)
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            kind = {"\"": "str", "'": "table", "[": "col"}[ch]
            out.append(Tok(kind, text[i:j + 1], "".join(buf), i))
            i = j + 1
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_."):
                j += 1
            out.append(Tok("ident", text[i:j], text[i:j], i))
            i = j
            continue
        two = text[i:i + 2]
        if two in TWO_CHAR:
            out.append(Tok("op", two, two, i))
            i += 2
            continue
        if ch in ONE_CHAR:
            out.append(Tok("op", ch, ch, i))
            i += 1
            continue
        raise syntax_error(text, i, ch)
    out.append(Tok("eof", "", None, n))
    return out


# ======================================================================
# Syntax tree (DAX shapes, before any name is resolved)
# ======================================================================
@dataclass(frozen=True)
class Node:
    pos: int


@dataclass(frozen=True)
class Num(Node):
    value: Any


@dataclass(frozen=True)
class Str(Node):
    value: str


@dataclass(frozen=True)
class Bool(Node):
    value: bool


@dataclass(frozen=True)
class ColRef(Node):
    table: str | None
    column: str


@dataclass(frozen=True)
class TableRef(Node):
    name: str


@dataclass(frozen=True)
class Ident(Node):
    """A bare identifier: a variable, or an unquoted table name."""
    name: str


@dataclass(frozen=True)
class Call(Node):
    name: str                      # upper-cased
    args: tuple[Node, ...]


@dataclass(frozen=True)
class Bin(Node):
    op: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Unary(Node):
    op: str
    arg: Node


@dataclass(frozen=True)
class NotNode(Node):
    arg: Node


@dataclass(frozen=True)
class InNode(Node):
    left: Node
    items: tuple[Node, ...]


@dataclass(frozen=True)
class TableCtor(Node):
    items: tuple[Node, ...]


@dataclass(frozen=True)
class VarBlock(Node):
    binds: tuple[tuple[str, Node, int], ...]
    body: Node


COMPARE = ("=", "==", "<>", "<", ">", "<=", ">=")


def shape(node: Any) -> Any:
    """A syntax node without source positions, for structural comparison."""
    if isinstance(node, Node):
        return (type(node).__name__,) + tuple(
            shape(getattr(node, f)) for f in node.__dataclass_fields__ if f != "pos")
    if isinstance(node, tuple):
        return tuple(shape(n) for n in node)
    return node


def same(a: Node, b: Node) -> bool:
    return shape(a) == shape(b)


class Parser:
    """Recursive descent over DAX's operator precedence (dax-operator-reference):
    ^, sign, * /, + -, &, comparisons and IN, NOT, &&, ||."""

    def __init__(self, text: str):
        self.text = text
        self.toks = tokenize(text)
        self.i = 0

    # ---------------------------------------------------------- plumbing
    def peek(self, k: int = 0) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Tok:
        tok = self.peek()
        self.i += 1
        return tok

    def is_op(self, text: str, k: int = 0) -> bool:
        tok = self.peek(k)
        return tok.kind == "op" and tok.text == text

    def is_kw(self, word: str, k: int = 0) -> bool:
        tok = self.peek(k)
        return tok.kind == "ident" and tok.text.upper() == word

    def expect_op(self, text: str) -> Tok:
        if not self.is_op(text):
            raise self.error()
        return self.next()

    def error(self, tok: Tok | None = None) -> ModelError:
        tok = tok or self.peek()
        near = tok.text or "end of input"
        return syntax_error(self.text, tok.pos, near)

    def parse_all(self) -> Node:
        node = self.expr()
        if self.peek().kind != "eof":
            raise self.error()
        return node

    # ------------------------------------------------------- precedence
    def expr(self) -> Node:
        if self.is_kw("VAR"):
            return self.var_block()
        return self.or_()

    def var_block(self) -> Node:
        start = self.peek().pos
        binds = []
        while self.is_kw("VAR"):
            self.next()
            name = self.next()
            if name.kind != "ident":
                raise self.error(name)
            self.expect_op("=")
            binds.append((name.text, self.expr(), name.pos))
        if not self.is_kw("RETURN"):
            raise self.error()
        self.next()
        return VarBlock(start, tuple(binds), self.expr())

    def or_(self) -> Node:
        node = self.and_()
        while self.is_op("||"):
            tok = self.next()
            node = Bin(tok.pos, "||", node, self.and_())
        return node

    def and_(self) -> Node:
        node = self.not_()
        while self.is_op("&&"):
            tok = self.next()
            node = Bin(tok.pos, "&&", node, self.not_())
        return node

    def not_(self) -> Node:
        if self.is_kw("NOT"):
            tok = self.next()
            return NotNode(tok.pos, self.not_())
        return self.compare()

    def compare(self) -> Node:
        node = self.concat()
        while True:
            tok = self.peek()
            if tok.kind == "op" and tok.text in COMPARE:
                self.next()
                node = Bin(tok.pos, tok.text, node, self.concat())
            elif self.is_kw("IN"):
                self.next()
                if not self.is_op("{"):
                    raise self.error()
                ctor = self.primary()
                assert isinstance(ctor, TableCtor)
                node = InNode(tok.pos, node, ctor.items)
            else:
                return node

    def concat(self) -> Node:
        node = self.additive()
        while self.is_op("&"):
            tok = self.next()
            node = Bin(tok.pos, "&", node, self.additive())
        return node

    def additive(self) -> Node:
        node = self.multiplicative()
        while self.is_op("+") or self.is_op("-"):
            tok = self.next()
            node = Bin(tok.pos, tok.text, node, self.multiplicative())
        return node

    def multiplicative(self) -> Node:
        node = self.unary()
        while self.is_op("*") or self.is_op("/"):
            tok = self.next()
            node = Bin(tok.pos, tok.text, node, self.unary())
        return node

    def unary(self) -> Node:
        if self.is_op("-") or self.is_op("+"):
            tok = self.next()
            # A sign directly on a number is part of the literal (-5 is
            # Lit(-5)); on anything else, parenthesised or not, it is negation.
            if tok.text == "-" and self.peek().kind == "num":
                num = self.next()
                return self.power(Num(tok.pos, -num.value))
            arg = self.unary()
            return arg if tok.text == "+" else Unary(tok.pos, "-", arg)
        return self.power(self.primary())

    def power(self, node: Node) -> Node:
        if self.is_op("^"):
            tok = self.next()
            return Bin(tok.pos, "^", node, self.unary())
        return node

    def primary(self) -> Node:
        tok = self.next()
        if tok.kind == "num":
            return Num(tok.pos, tok.value)
        if tok.kind == "str":
            return Str(tok.pos, tok.value)
        if tok.kind == "col":
            return ColRef(tok.pos, None, tok.value)
        if tok.kind == "table":
            if self.peek().kind == "col":
                return ColRef(tok.pos, tok.value, self.next().value)
            return TableRef(tok.pos, tok.value)
        if tok.kind == "ident":
            if tok.text.upper() == "VAR":
                self.i -= 1
                return self.var_block()
            if self.is_op("("):
                self.next()
                args = []
                if not self.is_op(")"):
                    args.append(self.expr())
                    while self.is_op(","):
                        self.next()
                        args.append(self.expr())
                self.expect_op(")")
                return Call(tok.pos, tok.text.upper(), tuple(args))
            if self.peek().kind == "col":
                return ColRef(tok.pos, tok.text, self.next().value)
            if tok.text.upper() in ("TRUE", "FALSE"):
                return Bool(tok.pos, tok.text.upper() == "TRUE")
            return Ident(tok.pos, tok.text)
        if tok.kind == "op" and tok.text == "(":
            node = self.expr()
            self.expect_op(")")
            return node
        if tok.kind == "op" and tok.text == "{":
            items = []
            if not self.is_op("}"):
                items.append(self.expr())
                while self.is_op(","):
                    self.next()
                    items.append(self.expr())
            self.expect_op("}")
            return TableCtor(tok.pos, tuple(items))
        raise self.error(tok)


# ======================================================================
# Lowering: DAX shapes -> expr tree, with every refusal decided here
# ======================================================================
AGGREGATORS = {"SUM": "sum", "AVERAGE": "avg", "MIN": "min", "MAX": "max", "COUNT": "count",
               "COUNTA": "count", "DISTINCTCOUNTNOBLANK": "count_distinct"}
ITERATORS = {"SUMX": "sum", "AVERAGEX": "avg", "MINX": "min", "MAXX": "max", "COUNTX": "count"}
SCALAR_1 = {"ABS": "abs", "YEAR": "year", "QUARTER": "quarter", "MONTH": "month",
            "DAY": "day", "UPPER": "upper", "LOWER": "lower", "LEN": "len", "TRIM": "trim"}
SCALAR_2 = {"ROUND": "round", "LEFT": "left", "RIGHT": "right"}
TIME_TOTALS = {"TOTALYTD": "DATESYTD", "TOTALQTD": "DATESQTD", "TOTALMTD": "DATESMTD"}
TIME_FILTERS = {"SAMEPERIODLASTYEAR": "same_period_last_year", "DATESYTD": "ytd",
                "DATESQTD": "qtd", "DATESMTD": "mtd", "PREVIOUSMONTH": "previous_month",
                "PREVIOUSYEAR": "previous_year"}
#: Functions that return tables. In a scalar position, in a VAR, or as an
#: iterator's table they are a construct outside the subset.
TABLE_FUNCTIONS = frozenset({
    "FILTER", "ALL", "ALLEXCEPT", "ALLSELECTED", "ALLNOBLANKROW", "VALUES", "DISTINCT",
    "SUMMARIZE", "SUMMARIZECOLUMNS", "ADDCOLUMNS", "SELECTCOLUMNS", "CALCULATETABLE",
    "TOPN", "RELATEDTABLE", "DATESINPERIOD", "DATESBETWEEN", "DATEADD", "PARALLELPERIOD",
    "SAMEPERIODLASTYEAR", "DATESYTD", "DATESQTD", "DATESMTD", "PREVIOUSMONTH",
    "PREVIOUSYEAR", "PREVIOUSQUARTER", "PREVIOUSDAY", "NEXTMONTH", "NEXTYEAR",
    "TREATAS", "REMOVEFILTERS", "KEEPFILTERS", "CROSSJOIN", "UNION", "EXCEPT",
    "INTERSECT", "GENERATESERIES", "CALENDAR", "CALENDARAUTO", "ROW", "DATATABLE",
    "NATURALINNERJOIN", "NATURALLEFTOUTERJOIN", "GROUPBY", "WINDOW", "OFFSET", "INDEX",
})
#: Named as themselves, because that is what an engineer would search for.
NAMED_REFUSALS = ("USERELATIONSHIP", "CROSSFILTER", "EARLIER", "EARLIEST")
#: DAX's comparison functions in a filter: op as written -> op with sides swapped.
FLIP = {"=": "=", "<>": "<>", "<": ">", ">": "<", "<=": ">=", ">=": "<="}
BINOPS = {"+": "+", "-": "-", "*": "*", "/": "/", "&": "&", "=": "=", "<>": "<>",
          "<": "<", ">": ">", "<=": "<=", ">=": ">=", "&&": "and", "||": "or"}

SINGLE_VALUE = ("A single value for column '{column}' in table '{table}' cannot be "
                "determined. This can happen when a measure formula refers to a column "
                "that contains many values without specifying an aggregation such as "
                "min, max, count, or sum to get a single result.")
NO_RELATIONSHIP = ("The column '{table}[{column}]' either doesn't exist or doesn't have a "
                   "relationship to any table available in the current context.")


@dataclass(frozen=True)
class Binding:
    expr: Expr
    depth: int


@dataclass(frozen=True)
class Ctx:
    """Where an expression is being lowered: the row context's table (an
    iterator's, or a calculated column's), how many filter-context changes
    deep, and the variables in scope."""
    row: str | None = None
    depth: int = 0
    vars: tuple[tuple[str, Binding], ...] = ()

    def deeper(self, row: str | None = None) -> Ctx:
        return Ctx(row=row, depth=self.depth + 1, vars=self.vars)

    def bind(self, name: str, binding: Binding) -> Ctx:
        return Ctx(row=self.row, depth=self.depth,
                   vars=((name.lower(), binding),) + self.vars)

    def lookup(self, name: str) -> Binding | None:
        for key, binding in self.vars:
            if key == name.lower():
                return binding
        return None


def is_constant(e: Expr) -> bool:
    """No aggregate, column, measure or filter anywhere: the same value under
    every filter and row context, so it may be inlined anywhere."""
    if isinstance(e, Lit):
        return True
    if isinstance(e, BinOp):
        return is_constant(e.left) and is_constant(e.right)
    if isinstance(e, (Neg, Not)):
        return is_constant(e.arg)
    if isinstance(e, Func):
        return all(is_constant(a) for a in e.args)
    if isinstance(e, Divide):
        return (is_constant(e.num) and is_constant(e.den)
                and (e.alternate is None or is_constant(e.alternate)))
    if isinstance(e, If):
        return (is_constant(e.cond) and is_constant(e.then)
                and (e.else_ is None or is_constant(e.else_)))
    if isinstance(e, Coalesce):
        return all(is_constant(a) for a in e.args)
    return False


class Lowering:
    """Resolves names against one model and lowers syntax into expr nodes.

    `extra_measures` are measure names visible besides the model's own (a DAX
    query's DEFINE MEASURE). DAX resolves table, column and measure names case-
    insensitively; every node this returns carries the model's spelling.
    """

    def __init__(self, model: Model, text: str = "", extra_measures: tuple[str, ...] = ()):
        self.model = model
        self.text = text
        self.tables = {t.name.lower(): t for t in model.tables}
        self.measures = {m.name.lower(): m.name for m in model.measures}
        for name in extra_measures:
            self.measures[name.lower()] = name
        self.parents: dict[str, set[str]] = {}
        for rel in model.relationships:
            if rel.active:
                self.parents.setdefault(rel.from_table, set()).add(rel.to_table)

    # ---------------------------------------------------------- names
    def error(self, node: Node, message: str) -> ModelError:
        if not self.text:
            return ModelError(message)
        line, col = position(self.text, node.pos)
        return ModelError(f"line {line}, column {col}: {message}")

    def table(self, name: str, node: Node) -> Table:
        table = self.tables.get(name.lower())
        if table is None:
            raise self.error(node, f"Failed to resolve name '{name}'. It is not a valid "
                                   f"table, variable, or function name.")
        return table

    def column(self, table_name: str, column: str, node: Node) -> Col:
        table = self.table(table_name, node)
        for col in table.columns:
            if col.name.lower() == column.lower():
                return Col(table.name, col.name)
        raise self.error(node, f"Column '{column}' in table '{table.name}' cannot be found "
                               f"or may not be used in this expression.")

    def colref(self, node: Node, ctx: Ctx | None = None) -> Col:
        """A column reference argument (SUM's, a filter's): table-qualified, or
        unqualified inside a row context on its table."""
        if not isinstance(node, ColRef):
            raise self.error(node, "The expression specified in the query is not a valid "
                                   "column reference.")
        if node.table is None:
            if ctx is not None and ctx.row is not None:
                return self.column(ctx.row, node.column, node)
            raise self.error(node, f"Column reference '[{node.column}]' must be qualified "
                                   f"with its table name here.")
        return self.column(node.table, node.column, node)

    def table_arg(self, node: Node, function: str) -> str:
        if isinstance(node, (TableRef, Ident)):
            return self.table(node.name, node).name
        if isinstance(node, Call):
            raise unsupported(f"DAX function {node.name}",
                              f"{function} over a table expression")
        raise self.error(node, f"{function} expects a table reference.")

    def expanded(self, table: str) -> set[str]:
        """DAX's expanded table: the table plus every table reachable by
        following active many-to-one relationships from it."""
        seen, todo = {table}, [table]
        while todo:
            for parent in self.parents.get(todo.pop(), ()):
                if parent not in seen:
                    seen.add(parent)
                    todo.append(parent)
        return seen

    def date_table_of(self, col: Col) -> Table | None:
        table = self.tables[col.table.lower()]
        if table.is_date_table and table.date_column == col.column:
            return table
        return None

    # -------------------------------------------------------- scalars
    def lower(self, node: Node, ctx: Ctx) -> Expr:
        if isinstance(node, Num):
            return Lit(node.value)
        if isinstance(node, Str):
            return Lit(node.value)
        if isinstance(node, Bool):
            return Lit(node.value)
        if isinstance(node, ColRef):
            return self.scalar_column(node, ctx)
        if isinstance(node, Ident):
            binding = ctx.lookup(node.name)
            if binding is not None:
                if binding.depth != ctx.depth and not is_constant(binding.expr):
                    raise unsupported("variable evaluated in a different filter context",
                                      f"VAR {node.name} is defined outside the CALCULATE or "
                                      f"iterator that uses it; DAX evaluates it once, where "
                                      f"it is defined")
                return binding.expr
            self.table(node.name, node)
            raise self.error(node, "The expression refers to multiple columns. Multiple "
                                   "columns cannot be converted to a scalar value.")
        if isinstance(node, TableRef):
            self.table(node.name, node)
            raise self.error(node, "The expression refers to multiple columns. Multiple "
                                   "columns cannot be converted to a scalar value.")
        if isinstance(node, Bin):
            if node.op == "==":
                raise unsupported("== (strict equality)")
            if node.op == "^":
                raise unsupported("^ (exponentiation)")
            week = self.week_start(node)
            if week is not None:
                return Func("date_trunc_week", (self.lower(week, ctx),))
            return BinOp(BINOPS[node.op], self.lower(node.left, ctx),
                         self.lower(node.right, ctx))
        if isinstance(node, Unary):
            return Neg(self.lower(node.arg, ctx))
        if isinstance(node, NotNode):
            return Not(self.lower(node.arg, ctx))
        if isinstance(node, InNode):
            raise unsupported("IN outside a CALCULATE filter")
        if isinstance(node, TableCtor):
            raise unsupported("table constructor outside a filter")
        if isinstance(node, VarBlock):
            inner = ctx
            for name, value, _pos in node.binds:
                if self.is_table_expression(value, inner):
                    raise unsupported("VAR over a table expression", f"VAR {name}")
                inner = inner.bind(name, Binding(self.lower(value, inner), inner.depth))
            return self.lower(node.body, inner)
        if isinstance(node, Call):
            return self.call(node, ctx)
        raise self.error(node, "Unrecognised expression.")   # pragma: no cover

    def is_table_expression(self, node: Node, ctx: Ctx) -> bool:
        if isinstance(node, Call):
            return node.name in TABLE_FUNCTIONS
        if isinstance(node, TableCtor):
            return True
        if isinstance(node, TableRef):
            return True
        if isinstance(node, Ident):
            return ctx.lookup(node.name) is None and node.name.lower() in self.tables
        return False

    def scalar_column(self, node: ColRef, ctx: Ctx) -> Expr:
        if node.table is None:
            measure = self.measures.get(node.column.lower())
            if measure is not None:
                if ctx.row is not None:
                    raise unsupported("context transition",
                                      f"measure [{measure}] inside a row context")
                return MeasureRef(measure)
            if ctx.row is not None:
                return self.column(ctx.row, node.column, node)
            raise self.error(node, f"The value for '{node.column}' cannot be determined. "
                                   f"Either the column doesn't exist, or there is no current "
                                   f"row for this column.")
        col = self.column(node.table, node.column, node)
        if ctx.row is not None and col.table == ctx.row:
            return col
        raise self.error(node, SINGLE_VALUE.format(column=col.column, table=col.table))

    def arity(self, node: Call, low: int, high: int | None = None) -> None:
        count = len(node.args)
        if count < low:
            raise self.error(node, f"Too few arguments were passed to the {node.name} "
                                   f"function. The minimum argument count for the function "
                                   f"is {low}.")
        if high is not None and count > high:
            raise self.error(node, f"Too many arguments were passed to the {node.name} "
                                   f"function. The maximum argument count for the function "
                                   f"is {high}.")

    def call(self, node: Call, ctx: Ctx) -> Expr:
        name, args = node.name, node.args
        if name in NAMED_REFUSALS:
            raise unsupported(name)
        if name == "DISTINCTCOUNT":
            raise unsupported("DISTINCTCOUNT", "DISTINCTCOUNT counts BLANK as a value and the "
                              "contract's count_distinct does not say whether None counts; "
                              "DISTINCTCOUNTNOBLANK is exact")
        if name == "WEEKDAY":
            # The engine pins weekday as Sunday=1..Saturday=7: DAX's default
            # return_type 1. Any other numbering is outside the subset.
            self.arity(node, 1, 2)
            if len(args) == 2 and not (isinstance(args[1], Num) and args[1].value == 1):
                raise unsupported("WEEKDAY return_type other than 1")
            return Func("weekday", (self.lower(args[0], ctx),))
        if name in AGGREGATORS:
            self.arity(node, 1, 1)
            return Agg(AGGREGATORS[name], self.colref(args[0], ctx))
        if name == "COUNTROWS":
            self.arity(node, 1, 1)
            return Agg("count_rows", table=self.table_arg(args[0], "COUNTROWS"))
        if name in ITERATORS:
            self.arity(node, 2, 2)
            table = self.table_arg(args[0], name)
            if ctx.row is not None:
                raise unsupported("nested iterator", f"{name} inside a row context")
            return IterAgg(ITERATORS[name], table, self.lower(args[1], ctx.deeper(row=table)))
        if name == "RELATED":
            self.arity(node, 1, 1)
            if not isinstance(args[0], ColRef) or args[0].table is None:
                raise self.error(node, "RELATED expects a fully qualified column reference.")
            col = self.column(args[0].table, args[0].column, args[0])
            if ctx.row is None or col.table == ctx.row or col.table not in self.expanded(ctx.row):
                raise self.error(node, NO_RELATIONSHIP.format(table=col.table,
                                                              column=col.column))
            return Related(col.table, col.column)
        if name == "DIVIDE":
            self.arity(node, 2, 3)
            alternate = self.lower(args[2], ctx) if len(args) == 3 else None
            return Divide(self.lower(args[0], ctx), self.lower(args[1], ctx), alternate)
        if name == "IF":
            self.arity(node, 2, 3)
            else_ = self.lower(args[2], ctx) if len(args) == 3 else None
            return If(self.lower(args[0], ctx), self.lower(args[1], ctx), else_)
        if name == "SWITCH":
            return self.switch(node, ctx)
        if name == "COALESCE":
            self.arity(node, 2)
            return Coalesce(tuple(self.lower(a, ctx) for a in args))
        if name == "BLANK":
            self.arity(node, 0, 0)
            return Lit(None)
        if name in ("TRUE", "FALSE"):
            self.arity(node, 0, 0)
            return Lit(name == "TRUE")
        if name in ("AND", "OR"):
            self.arity(node, 2, 2)
            return BinOp(name.lower(), self.lower(args[0], ctx), self.lower(args[1], ctx))
        if name == "NOT":
            self.arity(node, 1, 1)
            return Not(self.lower(args[0], ctx))
        if name in SCALAR_1:
            self.arity(node, 1, 1)
            return Func(SCALAR_1[name], (self.lower(args[0], ctx),))
        if name in SCALAR_2:
            self.arity(node, 2, 2)
            return Func(SCALAR_2[name], tuple(self.lower(a, ctx) for a in args))
        if name == "INT":
            self.arity(node, 1, 1)
            return Func("floor", (self.lower(args[0], ctx),))
        if name in ("FLOOR", "CEILING"):
            self.arity(node, 2, 2)
            if not (isinstance(args[1], Num) and args[1].value == 1):
                raise unsupported(f"{name} with a significance other than 1")
            return Func(name.lower(), (self.lower(args[0], ctx),))
        if name == "DATE":
            return self.date(node, ctx)
        if name == "CALCULATE":
            self.arity(node, 1)
            return self.calculate(node, args[0], args[1:], ctx)
        if name in TIME_TOTALS:
            self.arity(node, 2, 4)
            if len(args) == 4 or (len(args) == 3 and isinstance(args[2], Str)):
                raise unsupported(f"{name} year_end_date")
            dates = Call(args[1].pos, TIME_TOTALS[name], (args[1],))
            return self.calculate(node, args[0], (dates, *args[2:]), ctx)
        if name in TABLE_FUNCTIONS:
            raise unsupported(f"DAX function {name}")
        raise unsupported(f"DAX function {name}")

    def switch(self, node: Call, ctx: Ctx) -> Expr:
        """SWITCH ( TRUE (), c1, v1, ..., else ) and SWITCH ( x, k1, v1, ..., else )
        are both a chain of IFs; the second compares with DAX's =."""
        self.arity(node, 3)
        head, rest = node.args[0], list(node.args[1:])
        else_node = rest.pop() if len(rest) % 2 else None
        acc = self.lower(else_node, ctx) if else_node is not None else None
        selector = None
        if not (isinstance(head, Bool) and head.value) and not (
                isinstance(head, Call) and head.name == "TRUE" and not head.args):
            selector = self.lower(head, ctx)
        for k in range(len(rest) - 2, -1, -2):
            cond = self.lower(rest[k], ctx)
            if selector is not None:
                cond = BinOp("=", selector, cond)
            acc = If(cond, self.lower(rest[k + 1], ctx), acc)
        assert acc is not None
        return acc

    def date(self, node: Call, ctx: Ctx) -> Expr:
        """DATE with constant arguments is a date literal. The three period
        starts this module writes for date_trunc_* are recognised exactly as
        written (format_expression); any other computed DATE is refused."""
        self.arity(node, 3, 3)
        y, m, d = node.args
        if all(isinstance(a, Num) and isinstance(a.value, int) for a in node.args):
            try:
                return Lit(dt.date(y.value, m.value, d.value))
            except ValueError:
                raise self.error(node, "An argument of function 'DATE' has the wrong data "
                                       "type or the result is too large or too small.") from None
        arg = self.year_arg(y)
        if arg is not None and isinstance(d, Num) and d.value == 1:
            if isinstance(m, Num) and m.value == 1:
                return Func("date_trunc_year", (self.lower(arg, ctx),))
            if isinstance(m, Call) and m.name == "MONTH" and len(m.args) == 1 \
                    and same(m.args[0], arg):
                return Func("date_trunc_month", (self.lower(arg, ctx),))
            if self.quarter_month(m, arg):
                return Func("date_trunc_quarter", (self.lower(arg, ctx),))
        raise unsupported("DAX function DATE with computed arguments")

    @staticmethod
    def week_start(node: Bin) -> Node | None:
        """x - WEEKDAY ( x, 3 ): the Monday on or before x (return_type 3 counts
        Monday as 0), which is date_trunc_week as the engine pins it."""
        right = node.right
        if (node.op == "-" and isinstance(right, Call) and right.name == "WEEKDAY"
                and len(right.args) == 2 and isinstance(right.args[1], Num)
                and right.args[1].value == 3 and same(right.args[0], node.left)):
            return node.left
        return None

    @staticmethod
    def year_arg(node: Node) -> Node | None:
        if isinstance(node, Call) and node.name == "YEAR" and len(node.args) == 1:
            return node.args[0]
        return None

    @staticmethod
    def quarter_month(node: Node, arg: Node) -> bool:
        """( QUARTER ( x ) - 1 ) * 3 + 1, as format_expression writes it."""
        if not (isinstance(node, Bin) and node.op == "+" and isinstance(node.right, Num)
                and node.right.value == 1):
            return False
        product = node.left
        if not (isinstance(product, Bin) and product.op == "*" and isinstance(product.right, Num)
                and product.right.value == 3):
            return False
        minus = product.left
        return (isinstance(minus, Bin) and minus.op == "-" and isinstance(minus.right, Num)
                and minus.right.value == 1 and isinstance(minus.left, Call)
                and minus.left.name == "QUARTER" and len(minus.left.args) == 1
                and same(minus.left.args[0], arg))

    # ------------------------------------------------------- CALCULATE
    def calculate(self, node: Node, expr_node: Node, filter_nodes, ctx: Ctx) -> Expr:
        if ctx.row is not None:
            raise unsupported("context transition", "CALCULATE inside a row context")
        removals: list = []
        overwrites: list = []
        keeps: list = []
        seen: set[Col] = set()
        time = None
        for f in filter_nodes:
            for kind, mod in self.filter_argument(f, ctx):
                if kind == "time":
                    if time is not None:
                        raise unsupported("more than one time-intelligence filter in one "
                                          "CALCULATE")
                    time = mod
                elif kind == "removal":
                    if mod not in removals:
                        removals.append(mod)
                elif kind == "keep":
                    keeps.append(mod)
                elif mod.column in seen:
                    # DAX ANDs two filters on one column; REPLACE then INTERSECT.
                    keeps.append(KeepFilters(mod))
                else:
                    seen.add(mod.column)
                    overwrites.append(mod)
        expr = self.lower(expr_node, ctx.deeper())
        modifiers = tuple(removals + overwrites + keeps)
        if time is None:
            return Calculate(expr, modifiers)
        date_col, kind = time
        date_table = date_col.table
        for mod in modifiers:
            if self.touches(mod, date_table):
                raise unsupported("time-intelligence filter combined with another filter on "
                                  "the date table")
        return TimeShift(Calculate(expr, modifiers) if modifiers else expr, date_col, kind)

    def touches(self, mod, table: str) -> bool:
        """Can this modifier change the filter on `table`?"""
        if isinstance(mod, KeepFilters):
            return mod.modifier.column.table == table
        if isinstance(mod, (FilterValues, FilterCompare)):
            return mod.column.table == table
        if isinstance(mod, RemoveFilters):
            if not mod.tables and not mod.columns:
                return True
            return (any(table in self.expanded(t) for t in mod.tables)
                    or any(c.table == table for c in mod.columns))
        if isinstance(mod, KeepOnly):
            return table in self.expanded(mod.table)
        return True

    def filter_argument(self, node: Node, ctx: Ctx) -> list[tuple[str, Any]]:
        """One CALCULATE filter argument -> (kind, modifier) pairs, kind in
        removal | overwrite | keep | time."""
        if isinstance(node, Call):
            name, args = node.name, node.args
            if name in NAMED_REFUSALS:
                raise unsupported(name)
            if name in ("ALL", "REMOVEFILTERS"):
                return [("removal", self.removal(node))]
            if name == "ALLEXCEPT":
                self.arity(node, 2)
                table = self.table_arg(args[0], name)
                cols = tuple(self.colref(a) for a in args[1:])
                return [("removal", KeepOnly(table, cols))]
            if name == "KEEPFILTERS":
                self.arity(node, 1, 1)
                out = []
                for kind, mod in self.filter_argument(args[0], ctx):
                    if kind != "overwrite":
                        raise unsupported("KEEPFILTERS over a filter modifier")
                    if self.date_table_of(mod.column) is not None:
                        raise unsupported("KEEPFILTERS on the date column of a marked date "
                                          "table")
                    out.append(("keep", KeepFilters(mod)))
                return out
            if name in TIME_FILTERS:
                self.arity(node, 1, 2 if name in ("DATESYTD", "PREVIOUSYEAR") else 1)
                if len(args) == 2:
                    raise unsupported(f"{name} year_end_date")
                return [("time", (self.date_column(args[0]), TIME_FILTERS[name]))]
            if name == "TREATAS":
                self.arity(node, 2)
                if len(args) > 2:
                    raise unsupported("TREATAS over more than one column")
                if not isinstance(args[0], TableCtor):
                    raise unsupported("TREATAS over a table expression")
                col = self.colref(args[1])
                values = tuple(self.constant(v, ctx) for v in args[0].items)
                return self.overwrite(FilterValues(col, values))
            if name == "FILTER":
                return self.filter_function(node, ctx)
            if name == "ISBLANK":
                self.arity(node, 1, 1)
                return self.overwrite(FilterValues(self.colref(args[0]), (None,)))
            if name == "NOT":
                self.arity(node, 1, 1)
                return self.filter_argument(NotNode(node.pos, args[0]), ctx)
            if name in ("AND", "OR"):
                self.arity(node, 2, 2)
                return self.filter_argument(
                    Bin(node.pos, "&&" if name == "AND" else "||", args[0], args[1]), ctx)
            if name in TABLE_FUNCTIONS:
                raise unsupported(f"DAX function {name}", "as a CALCULATE filter")
            raise self.error(node, f"A function '{name}' has been used in a True/False "
                                   f"expression that is used as a table filter expression. "
                                   f"This is not allowed.")
        if isinstance(node, (TableRef, Ident)):
            raise unsupported("table filter argument", f"CALCULATE over table {node.name}")
        mods = self.boolean_filter(node, ctx)
        out = []
        for mod in mods:
            out.extend(self.overwrite(mod))
        return out

    def overwrite(self, mod) -> list[tuple[str, Any]]:
        """A filter that REPLACES its column's predicates - plus the ALL ( 'Date' )
        DAX adds when that column is a marked date table's date column."""
        out: list[tuple[str, Any]] = []
        table = self.date_table_of(mod.column)
        if table is not None:
            out.append(("removal", RemoveFilters(tables=(table.name,))))
        out.append(("overwrite", mod))
        return out

    def removal(self, node: Call) -> RemoveFilters:
        args = node.args
        if not args:
            return RemoveFilters()
        if len(args) == 1 and isinstance(args[0], (TableRef, Ident)):
            return RemoveFilters(tables=(self.table(args[0].name, args[0]).name,))
        cols = tuple(self.colref(a) for a in args)
        if len({c.table for c in cols}) > 1:
            raise self.error(node, f"All column arguments of the {node.name} function must be "
                                   f"from the same table.")
        return RemoveFilters(columns=cols)

    def date_column(self, node: Node) -> Col:
        col = self.colref(node)
        if self.date_table_of(col) is None:
            raise unsupported("time intelligence over a column that is not the date column "
                              "of a marked date table", f"{col.table}[{col.column}]")
        return col

    def filter_function(self, node: Call, ctx: Ctx) -> list[tuple[str, Any]]:
        """FILTER ( ALL ( col ), cond ) is the boolean filter written out;
        FILTER ( VALUES ( col ), cond ) and FILTER ( KEEPFILTERS ( VALUES ( col ) ),
        cond ) keep the outer filter, i.e. KEEPFILTERS. Anything else iterates a
        table and is outside the subset."""
        self.arity(node, 2, 2)
        source, cond = node.args
        keep = False
        if isinstance(source, Call) and source.name == "KEEPFILTERS" and len(source.args) == 1:
            source, keep = source.args[0], True
        if not (isinstance(source, Call) and source.name in ("ALL", "VALUES")
                and len(source.args) == 1 and isinstance(source.args[0], ColRef)):
            raise unsupported("DAX function FILTER", "FILTER over anything but ALL or VALUES "
                                                     "of one column")
        col = self.colref(source.args[0])
        keep = keep or source.name == "VALUES"
        out = []
        for mod in self.boolean_filter(cond, ctx):
            if mod.column != col:
                raise self.error(cond, "FILTER condition must reference the filtered column.")
            if keep:
                if self.date_table_of(col) is not None:
                    raise unsupported("KEEPFILTERS on the date column of a marked date table")
                out.append(("keep", KeepFilters(mod)))
            else:
                out.extend(self.overwrite(mod))
        return out

    def boolean_filter(self, node: Node, ctx: Ctx | None = None) -> list:
        """A True/False filter expression over one or more columns ->
        FilterValues / FilterCompare, each REPLACING its column."""
        if isinstance(node, Bin) and node.op in FLIP:
            col, op, value = self.orient(node, ctx)
            if op == "=":
                if value is None:
                    raise unsupported("= BLANK() in a filter",
                                      "DAX's = also matches 0 and empty strings; use ISBLANK")
                return [FilterValues(col, (value,))]
            return [FilterCompare(col, op, value)]
        if isinstance(node, Bin) and node.op == "==":
            raise unsupported("== (strict equality)")
        if isinstance(node, InNode):
            col = self.colref(node.left)
            values = tuple(self.constant(v, ctx) for v in node.items)
            if None in values:
                raise unsupported("BLANK() in an IN list")
            if not values:
                raise unsupported("empty IN list")
            return [FilterValues(col, values)]
        if isinstance(node, Call) and node.name == "ISBLANK" and len(node.args) == 1:
            return [FilterValues(self.colref(node.args[0]), (None,))]
        if isinstance(node, (NotNode, Call)) and (
                isinstance(node, NotNode) or (node.name == "NOT" and len(node.args) == 1)):
            inner = node.arg if isinstance(node, NotNode) else node.args[0]
            mods = self.boolean_filter(inner, ctx)
            if len(mods) != 1 or not isinstance(mods[0], FilterValues):
                raise unsupported("NOT over a comparison filter")
            mod = mods[0]
            return [FilterValues(mod.column, mod.values, negate=not mod.negate)]
        if isinstance(node, Bin) and node.op == "&&":
            return self.boolean_filter(node.left, ctx) + self.boolean_filter(node.right, ctx)
        if isinstance(node, Bin) and node.op == "||":
            left = self.boolean_filter(node.left, ctx)
            right = self.boolean_filter(node.right, ctx)
            if (len(left) == len(right) == 1 and isinstance(left[0], FilterValues)
                    and isinstance(right[0], FilterValues) and left[0].column == right[0].column
                    and not left[0].negate and not right[0].negate):
                values = left[0].values + tuple(v for v in right[0].values
                                                if v not in left[0].values)
                return [FilterValues(left[0].column, values)]
            raise unsupported("boolean filter combining conditions with ||",
                              "only equalities on one column")
        raise unsupported("filter expression", "a CALCULATE filter must compare one column "
                                               "with constants")

    def orient(self, node: Bin, ctx: Ctx | None = None) -> tuple[Col, str, Any]:
        if isinstance(node.left, ColRef) and not isinstance(node.right, ColRef):
            return self.colref(node.left), node.op, self.constant(node.right, ctx)
        if isinstance(node.right, ColRef) and not isinstance(node.left, ColRef):
            return self.colref(node.right), FLIP[node.op], self.constant(node.left, ctx)
        raise unsupported("filter comparing a column to a measure or expression")

    def constant(self, node: Node, ctx: Ctx | None = None) -> Any:
        if isinstance(node, (Num, Str, Bool)):
            return node.value
        if isinstance(node, Ident) and ctx is not None:
            # A VAR holding a literal (VAR threshold = 100) is that literal.
            binding = ctx.lookup(node.name)
            if binding is not None and isinstance(binding.expr, Lit):
                return binding.expr.value
        if isinstance(node, Call):
            if node.name in ("TRUE", "FALSE") and not node.args:
                return node.name == "TRUE"
            if node.name == "BLANK" and not node.args:
                return None
            if node.name == "DATE":
                lit = self.date(node, Ctx())
                if isinstance(lit, Lit):
                    return lit.value
        if isinstance(node, Unary) and isinstance(node.arg, Num):
            return -node.arg.value
        raise unsupported("filter value that is not a constant")


def parse_node(text: str) -> Node:
    return Parser(text).parse_all()


def parse_expression(text: str, model: Model, *, table: str | None = None,
                     extra_measures: tuple[str, ...] = ()) -> Expr:
    """DAX text -> expr tree. `table` parses a calculated column: a row context
    on that table. `extra_measures` names query-scoped measures (DEFINE)."""
    node = parse_node(text)
    lowering = Lowering(model, text, extra_measures)
    if table is not None:
        table = lowering.table(table, node).name
    return lowering.lower(node, Ctx(row=table))


# ======================================================================
# Formatting: expr tree -> idiomatic DAX
# ======================================================================
#: DAX names that need quoting as a table name even though they are plain
#: words, because the parser would read them as a function or keyword. 'Date'
#: is the one every model has.
RESERVED = frozenset({
    "DATE", "TIME", "TRUE", "FALSE", "NOT", "AND", "OR", "IN", "VAR", "RETURN", "DEFINE",
    "EVALUATE", "MEASURE", "ORDER", "BY", "ASC", "DESC", "START", "AT", "CALENDAR",
    "YEAR", "MONTH", "DAY", "QUARTER", "HOUR", "MINUTE", "SECOND", "WEEK", "TABLE",
    "COLUMN", "CURRENCY", "BLANK", "VALUES", "FILTER", "ALL", "SUM", "COUNT", "MIN", "MAX",
    "AVERAGE", "IF", "SWITCH", "TODAY", "NOW", "RANK", "INDEX", "WINDOW", "OFFSET",
})

PRECEDENCE = {"or": 1, "and": 2, "=": 4, "<>": 4, "<": 4, ">": 4, "<=": 4, ">=": 4,
              "&": 5, "+": 6, "-": 6, "*": 7, "/": 7}
DAX_OP = {"and": "&&", "or": "||"}
AGG_NAMES = {"sum": "SUM", "avg": "AVERAGE", "min": "MIN", "max": "MAX", "count": "COUNT",
             "count_distinct": "DISTINCTCOUNTNOBLANK"}
ITER_NAMES = {"sum": "SUMX", "avg": "AVERAGEX", "min": "MINX", "max": "MAXX", "count": "COUNTX"}
FUNC_NAMES = {"abs": "ABS", "year": "YEAR", "quarter": "QUARTER", "month": "MONTH",
              "day": "DAY", "upper": "UPPER", "lower": "LOWER", "len": "LEN", "trim": "TRIM",
              "round": "ROUND", "left": "LEFT", "right": "RIGHT"}
FUNC_ARITY = {"round": 2, "left": 2, "right": 2}
TIME_NAMES = {"ytd": "TOTALYTD", "qtd": "TOTALQTD", "mtd": "TOTALMTD"}
TIME_FILTER_NAMES = {"same_period_last_year": "SAMEPERIODLASTYEAR",
                     "previous_month": "PREVIOUSMONTH", "previous_year": "PREVIOUSYEAR"}


def quote_table(name: str) -> str:
    simple = name.replace("_", "a").isalnum() and name.isascii() and not name[:1].isdigit()
    if simple and name.upper() not in RESERVED:
        return name
    return "'" + name.replace("'", "''") + "'"


def quote_column(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def column_text(col: Col | Related) -> str:
    return quote_table(col.table) + quote_column(col.column)


def literal_text(value: Any) -> str:
    if value is None:
        return "BLANK ()"
    if value is True:
        return "TRUE ()"
    if value is False:
        return "FALSE ()"
    if isinstance(value, str):
        return '"' + value.replace('"', '""') + '"'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dt.datetime):
        raise unsupported("datetime literal")
    if isinstance(value, dt.date):
        return f"DATE ( {value.year}, {value.month}, {value.day} )"
    raise ModelError(f"no DAX literal for {value!r}")


class Formatter:
    def __init__(self, model: Model):
        self.model = model
        self.lowering = Lowering(model)

    def fmt(self, e: Expr, outer: int = 0) -> str:
        if isinstance(e, Lit):
            return literal_text(e.value)
        if isinstance(e, Col):
            return column_text(e)
        if isinstance(e, Related):
            return f"RELATED ( {column_text(e)} )"
        if isinstance(e, MeasureRef):
            return quote_column(e.name)
        if isinstance(e, BinOp):
            prec = PRECEDENCE[e.op]
            right_prec = prec + 1
            left_prec = prec + 1 if prec == 4 else prec
            text = (f"{self.fmt(e.left, left_prec)} {DAX_OP.get(e.op, e.op)} "
                    f"{self.fmt(e.right, right_prec)}")
            return f"( {text} )" if prec < outer else text
        if isinstance(e, Neg):
            if isinstance(e.arg, Lit) and isinstance(e.arg.value, (int, float, Decimal)):
                return f"-( {self.fmt(e.arg)} )"
            return "-" + self.fmt(e.arg, 8)
        if isinstance(e, Not):
            text = f"NOT ( {self.fmt(e.arg)} )"
            return f"( {text} )" if outer > 3 else text
        if isinstance(e, Divide):
            parts = [self.fmt(e.num), self.fmt(e.den)]
            if e.alternate is not None:
                parts.append(self.fmt(e.alternate))
            return f"DIVIDE ( {', '.join(parts)} )"
        if isinstance(e, If):
            if isinstance(e.else_, If):
                parts, node = ["TRUE ()"], e
                while isinstance(node, If):
                    parts += [self.fmt(node.cond), self.fmt(node.then)]
                    node = node.else_
                if node is not None:
                    parts.append(self.fmt(node))
                return f"SWITCH ( {', '.join(parts)} )"
            parts = [self.fmt(e.cond), self.fmt(e.then)]
            if e.else_ is not None:
                parts.append(self.fmt(e.else_))
            return f"IF ( {', '.join(parts)} )"
        if isinstance(e, Coalesce):
            return f"COALESCE ( {', '.join(self.fmt(a) for a in e.args)} )"
        if isinstance(e, Func):
            return self.func(e)
        if isinstance(e, Agg):
            if e.func == "count_rows":
                if e.table is None:
                    raise ModelError("count_rows needs a table")
                return f"COUNTROWS ( {quote_table(e.table)} )"
            if e.arg is None:
                raise ModelError(f"{e.func} needs a column")
            return f"{AGG_NAMES[e.func]} ( {column_text(e.arg)} )"
        if isinstance(e, IterAgg):
            return f"{ITER_NAMES[e.func]} ( {quote_table(e.table)}, {self.fmt(e.expr)} )"
        if isinstance(e, Calculate):
            return self.calculate(e)
        if isinstance(e, TimeShift):
            if self.lowering.date_table_of(e.date_column) is None:
                raise unsupported("time intelligence over a column that is not the date "
                                  "column of a marked date table")
            dates = column_text(e.date_column)
            if e.kind in TIME_NAMES:
                return f"{TIME_NAMES[e.kind]} ( {self.fmt(e.expr)}, {dates} )"
            return (f"CALCULATE ( {self.fmt(e.expr)}, "
                    f"{TIME_FILTER_NAMES[e.kind]} ( {dates} ) )")
        raise ModelError(f"cannot write {type(e).__name__} as DAX")

    def func(self, e: Func) -> str:
        name, args = e.name, e.args
        if name == "round" and len(args) == 1:
            args = (args[0], Lit(0))            # DAX's ROUND always takes the digits
        if name in FUNC_ARITY and len(args) != FUNC_ARITY[name]:
            raise ModelError(f"{name} takes {FUNC_ARITY[name]} arguments")
        if name not in FUNC_ARITY and len(args) != 1:
            raise ModelError(f"{name} takes 1 argument")
        if name == "weekday":
            return f"WEEKDAY ( {self.fmt(args[0])} )"
        if name in ("floor", "ceiling"):
            return f"{name.upper()} ( {self.fmt(args[0])}, 1 )"
        if name.startswith("date_trunc_"):
            x = self.fmt(args[0])
            unit = name.removeprefix("date_trunc_")
            if unit == "year":
                return f"DATE ( YEAR ( {x} ), 1, 1 )"
            if unit == "month":
                return f"DATE ( YEAR ( {x} ), MONTH ( {x} ), 1 )"
            if unit == "quarter":
                return f"DATE ( YEAR ( {x} ), ( QUARTER ( {x} ) - 1 ) * 3 + 1, 1 )"
            return f"( {x} - WEEKDAY ( {x}, 3 ) )"
        return f"{FUNC_NAMES[name]} ( {', '.join(self.fmt(a) for a in args)} )"

    # ------------------------------------------------------- CALCULATE
    def calculate(self, e: Calculate) -> str:
        groups = self.groups(e.modifiers)
        text = self.fmt(e.expr)
        for group in reversed(groups):
            mods = [self.modifier(m) for m in group]
            text = f"CALCULATE ( {', '.join([text, *mods])} )"
        return text

    def groups(self, modifiers) -> list[list]:
        """Split the contract's in-order modifiers into runs one CALCULATE
        expresses exactly: removals, then at most one REPLACE per column, then
        INTERSECTs. A run that would be reordered by DAX starts an inner
        CALCULATE (the inner one applies last, as the contract's order needs)."""
        if not modifiers:
            return [[]]
        groups, current, phase, columns = [], [], 0, set()
        for mod in modifiers:
            if isinstance(mod, (RemoveFilters, KeepOnly)):
                kind = 0
            elif isinstance(mod, KeepFilters):
                kind = 2
            else:
                kind = 1
            fits = kind >= phase and not (kind == 1 and mod.column in columns)
            if kind == 1 and fits:
                table = self.lowering.date_table_of(mod.column)
                if table is not None and not any(self.covers(r, table.name) for r in current
                                                 if isinstance(r, RemoveFilters)):
                    raise unsupported("filter on a marked date table's date column that keeps "
                                      "the other date filters")
            if not fits:
                groups.append(current)
                current, phase, columns = [], 0, set()
                if kind == 1 and self.lowering.date_table_of(mod.column) is not None:
                    raise unsupported("filter on a marked date table's date column that keeps "
                                      "the other date filters")
            current.append(mod)
            phase = kind
            if kind == 1:
                columns.add(mod.column)
        groups.append(current)
        return groups

    def covers(self, mod: RemoveFilters, table: str) -> bool:
        if not mod.tables and not mod.columns:
            return True
        return any(table in self.lowering.expanded(t) for t in mod.tables)

    def modifier(self, mod) -> str:
        if isinstance(mod, KeepFilters):
            return f"KEEPFILTERS ( {self.modifier(mod.modifier)} )"
        if isinstance(mod, FilterValues):
            col = column_text(mod.column)
            if not mod.values:
                raise unsupported("empty IN list")
            if mod.values == (None,):
                text = f"ISBLANK ( {col} )"
                return f"NOT ( {text} )" if mod.negate else text
            if None in mod.values:
                raise unsupported("BLANK() in an IN list")
            if len(mod.values) == 1 and not mod.negate:
                return f"{col} = {literal_text(mod.values[0])}"
            items = ", ".join(literal_text(v) for v in mod.values)
            text = f"{col} IN {{ {items} }}"
            return f"NOT ( {text} )" if mod.negate else text
        if isinstance(mod, FilterCompare):
            if mod.value is None:
                raise unsupported("= BLANK() in a filter")
            return f"{column_text(mod.column)} {mod.op} {literal_text(mod.value)}"
        if isinstance(mod, RemoveFilters):
            parts = [quote_table(t) for t in mod.tables]
            by_table: dict[str, list[Col]] = {}
            for col in mod.columns:
                by_table.setdefault(col.table, []).append(col)
            parts += [", ".join(column_text(c) for c in cols) for cols in by_table.values()]
            if not parts:
                return "REMOVEFILTERS ()"
            return ", ".join(f"REMOVEFILTERS ( {p} )" for p in parts)
        if isinstance(mod, KeepOnly):
            cols = ", ".join(column_text(c) for c in mod.columns)
            return f"ALLEXCEPT ( {quote_table(mod.table)}, {cols} )"
        raise ModelError(f"unknown modifier {mod!r}")


def format_expression(expr: Expr, model: Model) -> str:
    return Formatter(model).fmt(expr)


__all__ = ["Lowering", "Parser", "format_expression", "parse_expression", "quote_column",
           "quote_table", "column_text", "literal_text", "unsupported", "VENDOR"]

