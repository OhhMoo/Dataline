"""Tableau calculated fields <-> the semantic core's expression tree.

A Tableau calculated field is a formula over FIELD NAMES (`[order_amount]`,
`[customer_id (dim_customer)]`, `[Calculation_1368249927221915648]`), stored in a
`<calculation class='tableau' formula='...'/>`. This module parses the documented
calculation language (help.tableau.com, "Functions in Tableau" / "Operators" /
"Level of Detail expressions") and lowers the subset that maps EXACTLY onto
world/semantic/expr.py; everything else raises `Unsupported(<Tableau's own
name>, "Tableau")`. Sources consulted for the grammar and the function list:
the Tableau calculation function reference (help.tableau.com/current/pro/
desktop/en-us/functions_all_categories.htm), the operator-precedence table
(.../calculations_calculatedfields_formulas.htm) and the LOD order of operations
(.../order_of_operations.htm). No code is vendored.

WHAT LOWERS, AND WHY IT IS EXACT
================================
Every Tableau calc is either ROW-LEVEL (no aggregation anywhere) or AGGREGATE.
Mixing the two in one operator is Tableau's own error ("Cannot mix aggregate and
non-aggregate arguments"), which is a ModelError here, not an approximation.

- A row-level calc becomes a calculated `Column` (Column.expr) on its HOME table:
  the one table from which every table the formula reads is reachable
  many-to-one. Fields of the other tables become `Related`.
- `SUM([col])`, `AVG`, `MIN`, `MAX`, `COUNT`, `COUNTD` of a plain field become
  `Agg`. Of a row-level expression they become `IterAgg` over the expression's
  home table (COUNTD of an expression has no IterAgg func: refused).
- `/` becomes `Divide` (Tableau returns NULL for a zero divisor), `ZN`/`IFNULL`
  `Coalesce`, `IF/ELSEIF/ELSE`, `CASE/WHEN` and `IIF` become `If` chains.
  IIF is NOT IF: `IIF(test, then, else)` returns NULL (or its 4th argument)
  when `test` is NULL, while IF falls through to ELSE; so IIF lowers to
  `If(t, then, If(Not(t), else, unknown))`, which is the same three-valued truth
  table under SQL NULL rules.
- `ISNULL(x)` has no node of its own; it lowers to `If(x = x, FALSE, TRUE)`,
  which is TRUE exactly when x is NULL under SQL comparison semantics.
- `[x] IN (a, b)` lowers to `x = a OR x = b` (SQL IN has the same NULL table).

THE ONE LOD THAT LOWERS. Tableau computes `{FIXED : AGG(x)}` (no dimensions;
`{AGG(x)}` is the same thing) over the data after context and data-source filters
and before dimension filters. This front-end refuses context filters and
data-source filters, so every filter a lowered visual carries is a dimension
filter, and the FIXED value is the aggregate over ALL rows: `Calculate(AGG,
(RemoveFilters(),))`. In the view the LOD is a per-row constant that must be
aggregated; MIN/MAX/AVG of a constant is the constant - but only in a cell
that HAS rows of the LOD's table. A cell with none (a customer segment with no
orders) shows NULL in Tableau and would show the grand total under a bare
RemoveFilters, so the guard is part of the lowering:

    MIN({FIXED : SUM([x])})  ->  If(COUNTROWS(T) > 0, Calculate(SUM(x), ALL), BLANK)

where T is the single table the inner aggregate reads (count_rows over no rows
is BLANK, so the guard is BLANK-safe). That a cell without rows of T shows NULL
is this module's reading of the relationships documentation ("LOD expressions
... appear in [their] owning table"), not a calibrated measurement - see the
build plan's calibration gate. FIXED with dimensions depends on the viz's level
of detail (exact only when its dimensions are a subset of the view's, which a
measure cannot know), INCLUDE/EXCLUDE keep dimension filters on the excluded
field while RemoveFilters drops them ("INCLUDE and EXCLUDE level of detail
expressions are considered after Dimension filters"), and SUM/COUNT of a FIXED
value multiplies it by the row count - all refused by name.

ROW COUNTS. "You can't build calculations on top of a table's Count field.
Count is aggregate-only." (help.tableau.com, What's Changed with Data Sources
and Analysis). So `COUNTROWS(T)` has no calculated-field spelling and is refused
when formatting a measure; a worksheet can still show it (`[cnt:<object id>:qk]`,
twb.py), and the FIXED guard above uses it only inside the tree.

NULLS AND LOGIC. Tableau documents AND as NULL whenever an operand is NULL; on
a live connection the comparison is pushed to the database, whose SQL
three-valued logic the engine also follows ("sql" null semantics). The two
agree except for FALSE AND NULL / TRUE OR NULL, which no calibration has
settled; noted, not hidden.

ROW-LEVEL CALCS ACROSS TABLES. Tableau evaluates a row-level formula that reads
two related tables on their join; the expression tree reads the related table
through `Related`, which is NULL for an unmatched key. The two agree whenever a
NULL related value makes the whole formula NULL (an aggregate then skips the row,
exactly as if the join had dropped it). A formula that can turn a NULL related
value into something else (`ZN`, `IFNULL`, `IF`, `AND`/`OR`) would diverge on
orphan rows, so it is refused rather than guessed at.

FIELD NAMES. Formulas name fields by their Tableau LOCAL NAME (what the XML
stores), never by caption. `Names` is the two-way map; `Names.for_model` derives
it deterministically from a Model the way `write_project` writes it (first table
in model order keeps the plain remote name, later tables get Tableau's
`name (relation)` suffix; measures get `Calculation_<19 digits>` from a digest of
their name). `parse_expression` additionally accepts model names (captions),
because that is what an author types in the calculation editor.
"""
from __future__ import annotations

import datetime as dt
import hashlib
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
from ...model import DataType, Model

V = "Tableau"

# ===================================================================== lexer
_TOKEN = re.compile(
    r"""
     (?P<ws>\s+)
    |(?P<lcomment>//[^\n]*)
    |(?P<bcomment>/\*.*?\*/)
    |(?P<field>\[(?:[^\]]|\]\])*\])
    |(?P<str>'(?:[^']|'')*'|"(?:[^"]|"")*")
    |(?P<date>\#[^#\n]*\#)
    |(?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
    |(?P<op><=|>=|<>|!=|==|[-+*/%^=<>(),{}:.])
    |(?P<word>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.X | re.S,
)


@dataclass(frozen=True)
class _Tok:
    kind: str
    text: str
    pos: int


def _tokens(text: str) -> list[_Tok]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ModelError(f"Tableau calculation: unexpected character {text[pos]!r} "
                             f"at {pos} in {text!r}")
        kind = m.lastgroup
        if kind not in ("ws", "lcomment", "bcomment"):
            out.append(_Tok(kind, m.group(), pos))
        pos = m.end()
    out.append(_Tok("end", "", pos))
    return out


# ====================================================================== AST
# The parsed calculation, before any field is resolved. Kept separate from the
# expression tree because lowering needs to know things the text alone does not
# (which table a field lives in, whether a referenced calc aggregates).
@dataclass(frozen=True)
class TField:
    name: str
    qualifier: str | None = None


@dataclass(frozen=True)
class TLit:
    value: Any


@dataclass(frozen=True)
class TCall:
    name: str            # upper-cased
    args: tuple


@dataclass(frozen=True)
class TBin:
    op: str              # + - * / % ^ = <> < > <= >= AND OR
    left: Any
    right: Any


@dataclass(frozen=True)
class TNeg:
    arg: Any


@dataclass(frozen=True)
class TNot:
    arg: Any


@dataclass(frozen=True)
class TIf:
    branches: tuple      # ((cond, then), ...)
    else_: Any = None


@dataclass(frozen=True)
class TCase:
    subject: Any
    whens: tuple         # ((value, then), ...)
    else_: Any = None


@dataclass(frozen=True)
class TLod:
    kind: str            # FIXED | INCLUDE | EXCLUDE
    dims: tuple
    body: Any


@dataclass(frozen=True)
class TIn:
    arg: Any
    values: tuple


_KEYWORDS = {"IF", "THEN", "ELSEIF", "ELSE", "END", "CASE", "WHEN", "AND", "OR", "NOT",
             "TRUE", "FALSE", "NULL", "IN", "FIXED", "INCLUDE", "EXCLUDE"}


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = _tokens(text)
        self.i = 0

    # -- helpers
    def peek(self, k: int = 0) -> _Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def word(self, k: int = 0) -> str:
        t = self.peek(k)
        return t.text.upper() if t.kind == "word" else ""

    def take(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect_op(self, op: str) -> None:
        t = self.take()
        if t.kind != "op" or t.text != op:
            self.fail(f"expected {op!r}", t)

    def expect_word(self, w: str) -> None:
        t = self.take()
        if t.kind != "word" or t.text.upper() != w:
            self.fail(f"expected {w}", t)

    def at_op(self, *ops: str) -> bool:
        t = self.peek()
        return t.kind == "op" and t.text in ops

    def fail(self, what: str, tok: _Tok):
        raise ModelError(f"Tableau calculation: {what} at {tok.pos} "
                         f"(found {tok.text or 'end of formula'!r}) in {self.text!r}")

    # -- grammar (Tableau operator precedence, lowest first):
    #    OR < AND < NOT < IN < comparison < + - < * / % < ^ < unary -
    def parse(self):
        node = self.or_()
        if self.peek().kind != "end":
            self.fail("unexpected text", self.peek())
        return node

    def or_(self):
        node = self.and_()
        while self.word() == "OR":
            self.take()
            node = TBin("OR", node, self.and_())
        return node

    def and_(self):
        node = self.not_()
        while self.word() == "AND":
            self.take()
            node = TBin("AND", node, self.not_())
        return node

    def not_(self):
        if self.word() == "NOT":
            self.take()
            return TNot(self.not_())
        return self.in_()

    def in_(self):
        node = self.cmp()
        if self.word() == "IN":
            self.take()
            self.expect_op("(")
            values = [self.or_()]
            while self.at_op(","):
                self.take()
                values.append(self.or_())
            self.expect_op(")")
            node = TIn(node, tuple(values))
        return node

    def cmp(self):
        node = self.add()
        while self.at_op("=", "==", "<>", "!=", "<", ">", "<=", ">="):
            op = self.take().text
            op = {"==": "=", "!=": "<>"}.get(op, op)
            node = TBin(op, node, self.add())
        return node

    def add(self):
        node = self.mul()
        while self.at_op("+", "-"):
            op = self.take().text
            node = TBin(op, node, self.mul())
        return node

    def mul(self):
        node = self.pow()
        while self.at_op("*", "/", "%"):
            op = self.take().text
            node = TBin(op, node, self.pow())
        return node

    def pow(self):
        node = self.unary()
        if self.at_op("^"):
            self.take()
            node = TBin("^", node, self.pow())
        return node

    def unary(self):
        if self.at_op("-"):
            self.take()
            return TNeg(self.unary())
        if self.at_op("+"):
            self.take()
            return self.unary()
        return self.primary()

    def primary(self):
        t = self.peek()
        if t.kind == "num":
            self.take()
            text = t.text
            if "e" in text.lower():
                return TLit(float(text))
            if "." in text:
                return TLit(Decimal(text))
            return TLit(int(text))
        if t.kind == "str":
            self.take()
            q = t.text[0]
            return TLit(t.text[1:-1].replace(q + q, q))
        if t.kind == "date":
            self.take()
            return TLit(_date_literal(t.text))
        if t.kind == "field":
            self.take()
            name = _unbracket(t.text)
            if self.at_op(".") and self.peek(1).kind == "field":
                self.take()
                inner = _unbracket(self.take().text)
                return TField(inner, qualifier=name)
            return TField(name)
        if t.kind == "op" and t.text == "(":
            self.take()
            node = self.or_()
            self.expect_op(")")
            return node
        if t.kind == "op" and t.text == "{":
            return self.lod()
        if t.kind == "word":
            w = t.text.upper()
            if w == "TRUE":
                self.take()
                return TLit(True)
            if w == "FALSE":
                self.take()
                return TLit(False)
            if w == "NULL":
                self.take()
                return TLit(None)
            if w == "IF":
                return self.if_()
            if w == "CASE":
                return self.case()
            if w not in _KEYWORDS and self.peek(1).kind == "op" and self.peek(1).text == "(":
                self.take()
                self.take()
                args = []
                if not self.at_op(")"):
                    args.append(self.or_())
                    while self.at_op(","):
                        self.take()
                        args.append(self.or_())
                self.expect_op(")")
                return TCall(w, tuple(args))
        self.fail("unexpected token", t)

    def if_(self):
        self.expect_word("IF")
        branches = []
        cond = self.or_()
        self.expect_word("THEN")
        branches.append((cond, self.or_()))
        else_ = None
        while True:
            w = self.word()
            if w == "ELSEIF":
                self.take()
                cond = self.or_()
                self.expect_word("THEN")
                branches.append((cond, self.or_()))
            elif w == "ELSE":
                self.take()
                else_ = self.or_()
                self.expect_word("END")
                break
            elif w == "END":
                self.take()
                break
            else:
                self.fail("expected ELSEIF, ELSE or END", self.peek())
        return TIf(tuple(branches), else_)

    def case(self):
        self.expect_word("CASE")
        subject = self.or_()
        whens = []
        else_ = None
        while self.word() == "WHEN":
            self.take()
            value = self.or_()
            self.expect_word("THEN")
            whens.append((value, self.or_()))
        if not whens:
            self.fail("expected WHEN", self.peek())
        if self.word() == "ELSE":
            self.take()
            else_ = self.or_()
        self.expect_word("END")
        return TCase(subject, tuple(whens), else_)

    def lod(self):
        self.expect_op("{")
        kind = self.word()
        dims: list = []
        if kind in ("FIXED", "INCLUDE", "EXCLUDE"):
            self.take()
            if not self.at_op(":"):
                dims.append(self.or_())
                while self.at_op(","):
                    self.take()
                    dims.append(self.or_())
            self.expect_op(":")
        else:
            kind = "FIXED"    # {AGG(x)} is the table-scoped LOD: FIXED with no dims
        body = self.or_()
        self.expect_op("}")
        return TLod(kind, tuple(dims), body)


def _unbracket(text: str) -> str:
    return text[1:-1].replace("]]", "]")


def bracket(name: str) -> str:
    """A Tableau field reference: `[name]`, `]` doubled."""
    return "[" + name.replace("]", "]]") + "]"


_DATE_RE = re.compile(r"^#\s*(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?\s*#$")


def _date_literal(text: str):
    m = _DATE_RE.match(text)
    if not m:
        raise Unsupported("date literal format", V,
                          f"{text}: only ISO #YYYY-MM-DD# / #YYYY-MM-DD HH:MM:SS# literals "
                          f"have one meaning regardless of the workbook locale")
    y, mo, d, h, mi, s = m.groups()
    if h is None:
        return dt.date(int(y), int(mo), int(d))
    return dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))


def parse_tree(text: str):
    """Formula text -> the unresolved calculation AST."""
    return _Parser(text).parse()


def field_refs(node) -> list[TField]:
    """Every field reference in a parsed formula, in order (for dependency sort)."""
    out: list[TField] = []

    def walk(n):
        if isinstance(n, TField):
            out.append(n)
        elif isinstance(n, (tuple, list)):
            for x in n:
                walk(x)
        elif hasattr(n, "__dataclass_fields__"):
            for f in n.__dataclass_fields__:
                walk(getattr(n, f))

    walk(node)
    return out


# ==================================================================== names
@dataclass(frozen=True)
class CountRef:
    """A logical table's row count: Tableau's `<Table> (Count)` field."""
    table: str


Ref = Col | MeasureRef | CountRef

#: The qualifier of a logical table's Count field: `[__tableau_internal_object_id__].
#: [fct_orders_6F2A...]`, captioned `<Table> (Count)` in the Data pane.
COUNT_QUALIFIER = "__tableau_internal_object_id__"


@dataclass
class Names:
    """Tableau local field names <-> model references, both ways."""
    refs: dict[str, Ref] = field(default_factory=dict)            # local name -> ref
    of_col: dict[tuple[str, str], str] = field(default_factory=dict)
    of_measure: dict[str, str] = field(default_factory=dict)
    of_count: dict[str, str] = field(default_factory=dict)        # table -> count field
    #: model names an author may type (caption fallback); None marks an ambiguity.
    captions: dict[str, Ref | None] = field(default_factory=dict)
    #: logical table object id -> table (the `[__tableau_internal_object_id__].[id]`
    #: count fields; worksheets only - calculations cannot use them)
    counts: dict[str, str] = field(default_factory=dict)
    #: fields that exist but are outside the subset (sets, groups, table calcs in a
    #: leniently read source): name -> the refusal to raise when one is used
    unsupported: dict[str, Unsupported] = field(default_factory=dict)

    def add(self, name: str, ref: Ref, caption: str | None = None) -> None:
        if isinstance(ref, CountRef):
            self.counts[name] = ref.table
            self.of_count[ref.table] = name
            return
        if name in self.refs and self.refs[name] != ref:
            raise ModelError(f"Tableau data source: two fields are named [{name}]")
        self.refs[name] = ref
        if isinstance(ref, Col):
            self.of_col[(ref.table, ref.column)] = name
        else:
            self.of_measure[ref.name] = name
        if caption is not None and caption != name:
            prior = self.captions.get(caption, ref)
            self.captions[caption] = ref if prior == ref else None

    def resolve(self, name: str, allow_captions: bool) -> Ref:
        if name in self.unsupported:
            raise self.unsupported[name]
        if name in self.refs:
            return self.refs[name]
        if allow_captions and name in self.captions:
            ref = self.captions[name]
            if ref is None:
                hits = [bracket(n) for n in self.refs if _cap_hit(self, n, name)]
                raise ModelError(f"Tableau calculation: [{name}] names more than one field; "
                                 f"use the field's name, e.g. {', '.join(hits)}")
            return ref
        hint = ""
        for cap, ref in self.captions.items():
            if cap == name and ref is not None:
                hint = f" (a field captioned {name!r} is named {bracket(self._name_of(ref))})"
        raise ModelError(f"Tableau calculation: unknown field [{name}]{hint}")

    def _name_of(self, ref: Ref) -> str:
        if isinstance(ref, Col):
            return self.of_col[(ref.table, ref.column)]
        if isinstance(ref, MeasureRef):
            return self.of_measure[ref.name]
        return self.of_count[ref.table]

    def name_of(self, ref: Ref) -> str:
        try:
            return self._name_of(ref)
        except KeyError:
            raise ModelError(f"Tableau: no field for {ref!r}") from None

    @classmethod
    def for_model(cls, model: Model) -> Names:
        """The names `write_project` gives `model`'s fields (see module docstring)."""
        names = cls()
        taken: set[str] = set()
        relations = relation_names(model)
        n_calc = len(model.measures)
        for table in model.tables:
            for column in table.columns:
                if column.expr is not None:
                    local = column.source or calc_name(
                        model.name, n_calc, f"{table.name}/{column.name}")
                    n_calc += 1
                else:
                    local = column.source
                    if local in taken:
                        local = f"{column.source} ({relations[table.name]})"
                taken.add(local)
                names.add(local, Col(table.name, column.name), caption=column.name)
        for i, measure in enumerate(model.measures):
            names.add(calc_name(model.name, i, measure.name), MeasureRef(measure.name),
                      caption=measure.name)
        return names


def _cap_hit(names: Names, local: str, caption: str) -> bool:
    ref = names.refs[local]
    return any(c == caption and r is None for c, r in names.captions.items()) and (
        isinstance(ref, Col) and ref.column == caption
        or isinstance(ref, MeasureRef) and ref.name == caption)


def relation_names(model: Model) -> dict[str, str]:
    """Each table's physical relation name: the warehouse relation for a table,
    'Custom SQL Query' (numbered from the second one, as Desktop numbers them) for
    a native query. Also the suffix Tableau puts on a duplicated field name."""
    out, seen = {}, {}
    for table in model.tables:
        if table.source is not None:
            base = table.source.split(".")[-1]
        else:
            base = "Custom SQL Query"
        n = seen.get(base, 0)
        seen[base] = n + 1
        out[table.name] = base if n == 0 else f"{base}{n}"
    return out


def calc_name(model_key: str, index: int, key: str) -> str:
    """`Calculation_<19 digits>`, the shape Desktop gives a new calculated field.

    Desktop's numbers grow with creation time, so in a real file the calcs sorted
    by name are the calcs in the order they were made - which is the only order a
    data source records. These grow with `index` (the measure's position in the
    model) from a start derived from the model, plus a digest of `key`, so the
    same model always writes the same names and reads back in the same order."""
    def h(text: str) -> int:
        return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    start = 1_000_000_000_000_000_000 + h(model_key) % 4_000_000_000_000_000_000
    return f"Calculation_{start + index * 10**14 + h(key) % 10**14}"


# ================================================================== typing
_TABLEAU_TYPE = {"int": "integer", "decimal": "real", "float": "real", "string": "string",
                 "bool": "boolean", "date": "date", "timestamp": "datetime"}


def tableau_datatype(dtype: DataType | None) -> str:
    return _TABLEAU_TYPE.get(dtype or "float", "real")


def infer_type(e: Expr, model: Model) -> DataType | None:
    """The model data type an expression evaluates to (None when unknowable)."""
    cols = {(t.name, c.name): c.data_type for t in model.tables for c in t.columns}
    measures = {m.name: m.expr for m in model.measures}
    seen: set[str] = set()

    def num(a, b):
        if "float" in (a, b):
            return "float"
        if "decimal" in (a, b):
            return "decimal"
        if a == b == "int":
            return "int"
        return a or b

    def t(e):
        if isinstance(e, Lit):
            v = e.value
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
        if isinstance(e, (Col, Related)):
            return cols.get((e.table, e.column))
        if isinstance(e, MeasureRef):
            if e.name in seen or e.name not in measures:
                return None
            seen.add(e.name)
            out = t(measures[e.name])
            seen.discard(e.name)
            return out
        if isinstance(e, BinOp):
            if e.op == "&":
                return "string"
            if e.op in ("+", "-", "*"):
                return num(t(e.left), t(e.right))
            if e.op == "/":
                return "float"
            return "bool"
        if isinstance(e, Divide):
            return "float"
        if isinstance(e, Neg):
            return t(e.arg)
        if isinstance(e, Not):
            return "bool"
        if isinstance(e, If):
            return t(e.then) or (t(e.else_) if e.else_ is not None else None)
        if isinstance(e, Coalesce):
            return next((x for x in map(t, e.args) if x), None)
        if isinstance(e, Func):
            if e.name in ("year", "quarter", "month", "day", "weekday", "len"):
                return "int"
            if e.name.startswith("date_trunc_"):
                return "date"
            if e.name in ("upper", "lower", "trim", "left", "right"):
                return "string"
            if e.name in ("floor", "ceiling"):
                return "int"
            return t(e.args[0])
        if isinstance(e, Agg):
            if e.func in ("count", "count_distinct", "count_rows"):
                return "int"
            inner = t(e.arg)
            return "float" if e.func == "avg" and inner == "int" else inner
        if isinstance(e, IterAgg):
            if e.func == "count":
                return "int"
            inner = t(e.expr)
            return "float" if e.func == "avg" and inner == "int" else inner
        if isinstance(e, (Calculate, TimeShift)):
            return t(e.expr)
        return None

    return t(e)


# ================================================================ lowering
_AGG = {"SUM": "sum", "AVG": "avg", "MIN": "min", "MAX": "max", "COUNT": "count",
        "COUNTD": "count_distinct"}
_SCALAR = {"ABS": ("abs", (1,)), "ROUND": ("round", (1, 2)), "FLOOR": ("floor", (1,)),
           "CEILING": ("ceiling", (1,)), "YEAR": ("year", (1,)), "QUARTER": ("quarter", (1,)),
           "MONTH": ("month", (1,)), "DAY": ("day", (1,)), "UPPER": ("upper", (1,)),
           "LOWER": ("lower", (1,)), "LEN": ("len", (1,)), "TRIM": ("trim", (1,)),
           "LEFT": ("left", (2,)), "RIGHT": ("right", (2,))}
_DATEPART = {"year": "year", "quarter": "quarter", "month": "month", "day": "day"}
_DATETRUNC = {"year": "date_trunc_year", "quarter": "date_trunc_quarter",
              "month": "date_trunc_month"}
TABLE_CALCS = {
    "WINDOW_SUM", "WINDOW_AVG", "WINDOW_MIN", "WINDOW_MAX", "WINDOW_COUNT", "WINDOW_MEDIAN",
    "WINDOW_STDEV", "WINDOW_STDEVP", "WINDOW_VAR", "WINDOW_VARP", "WINDOW_PERCENTILE",
    "WINDOW_CORR", "WINDOW_COVAR", "WINDOW_COVARP", "RUNNING_SUM", "RUNNING_AVG",
    "RUNNING_MIN", "RUNNING_MAX", "RUNNING_COUNT", "LOOKUP", "PREVIOUS_VALUE", "FIRST",
    "LAST", "INDEX", "SIZE", "TOTAL", "RANK", "RANK_DENSE", "RANK_MODIFIED",
    "RANK_PERCENTILE", "RANK_UNIQUE", "SCRIPT_BOOL", "SCRIPT_INT", "SCRIPT_REAL",
    "SCRIPT_STR", "MODEL_PERCENTILE", "MODEL_QUANTILE", "MODEL_EXTENSION_BOOL",
    "MODEL_EXTENSION_INT", "MODEL_EXTENSION_REAL", "MODEL_EXTENSION_STR",
}
OTHER_AGGREGATES = {"ATTR", "MEDIAN", "PERCENTILE", "STDEV", "STDEVP", "VAR", "VARP",
                    "COLLECT", "CORR", "COVAR", "COVARP"}
_PROPAGATING_OPS = {"+", "-", "*", "/", "&", "=", "<>", "<", ">", "<=", ">="}


@dataclass(frozen=True)
class _LodTotal:
    """A lowered `{FIXED : AGG}`: legal only directly inside MIN/MAX/AVG."""
    agg: Expr
    table: str


def reach(model: Model) -> dict[str, set[str]]:
    """table -> the tables its expanded table contains (itself included)."""
    edges: dict[str, set[str]] = {t.name: set() for t in model.tables}
    for r in model.relationships:
        if r.active:
            edges.setdefault(r.from_table, set()).add(r.to_table)
    out = {}
    for start in edges:
        seen, todo = {start}, [start]
        while todo:
            for nxt in edges.get(todo.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        out[start] = seen
    return out


def home_table(tables: set[str], model: Model, what: str) -> str:
    """The one table whose expanded table holds every table in `tables`."""
    reaches = reach(model)
    candidates = [t for t in sorted(tables) if tables <= reaches.get(t, {t})]
    if len(candidates) != 1:
        raise Unsupported("row-level calculation across unrelated tables", V,
                          f"{what} reads {sorted(tables)}; no single table reaches all of "
                          f"them through many-to-one relationships")
    return candidates[0]


def col_tables(e) -> set[str]:
    out: set[str] = set()

    def walk(n):
        if isinstance(n, (Col, Related)):
            out.add(n.table)
        elif isinstance(n, (Agg, IterAgg, MeasureRef, Calculate, TimeShift)):
            return
        elif isinstance(n, tuple):
            for x in n:
                walk(x)
        elif hasattr(n, "__dataclass_fields__"):
            for f in n.__dataclass_fields__:
                walk(getattr(n, f))

    walk(e)
    return out


def relatedify(e, home: str):
    """Row-level tree over `home`: Col of another table -> Related."""
    if isinstance(e, Col):
        return e if e.table == home else Related(e.table, e.column)
    if isinstance(e, Related):
        return Col(e.table, e.column) if e.table == home else e
    if isinstance(e, BinOp):
        return BinOp(e.op, relatedify(e.left, home), relatedify(e.right, home))
    if isinstance(e, Neg):
        return Neg(relatedify(e.arg, home))
    if isinstance(e, Not):
        return Not(relatedify(e.arg, home))
    if isinstance(e, Divide):
        return Divide(relatedify(e.num, home), relatedify(e.den, home),
                      None if e.alternate is None else relatedify(e.alternate, home))
    if isinstance(e, If):
        return If(relatedify(e.cond, home), relatedify(e.then, home),
                  None if e.else_ is None else relatedify(e.else_, home))
    if isinstance(e, Coalesce):
        return Coalesce(tuple(relatedify(a, home) for a in e.args))
    if isinstance(e, Func):
        return Func(e.name, tuple(relatedify(a, home) for a in e.args))
    return e


def _propagates(e) -> bool:
    """True when every Related in `e` sits on a path of NULL-propagating nodes, so
    an unmatched related row makes the whole value NULL (module docstring)."""
    if isinstance(e, Related):
        return True
    if isinstance(e, (Lit, Col)):
        return True
    if not col_tables(e) & _related_tables(e):
        return True
    if isinstance(e, BinOp):
        return e.op in _PROPAGATING_OPS and _propagates(e.left) and _propagates(e.right)
    if isinstance(e, (Neg, Not)):
        return _propagates(e.arg)
    if isinstance(e, Divide):
        return e.alternate is None and _propagates(e.num) and _propagates(e.den)
    if isinstance(e, Func):
        return all(_propagates(a) for a in e.args)
    return False


def _related_tables(e) -> set[str]:
    out: set[str] = set()

    def walk(n):
        if isinstance(n, Related):
            out.add(n.table)
        elif isinstance(n, tuple):
            for x in n:
                walk(x)
        elif hasattr(n, "__dataclass_fields__"):
            for f in n.__dataclass_fields__:
                walk(getattr(n, f))

    walk(e)
    return out


def check_cross_table(e, what: str) -> None:
    if _related_tables(e) and not _propagates(e):
        raise Unsupported(
            "row-level calculation across related tables with NULL handling", V,
            f"{what}: Tableau evaluates it on the join of the tables, the model reads the "
            f"related table with RELATED; they differ on rows whose key has no match "
            f"when ZN/IFNULL/IF/AND/OR can turn the missing value into a non-NULL")


def base_tables(e, model: Model, _seen=frozenset()) -> set[str]:
    """The tables an aggregate expression aggregates over."""
    measures = {m.name: m.expr for m in model.measures}
    out: set[str] = set()

    def walk(n, seen):
        if isinstance(n, Agg):
            out.add(n.table if n.func == "count_rows" else n.arg.table)
        elif isinstance(n, IterAgg):
            out.add(n.table)
        elif isinstance(n, MeasureRef):
            if n.name in measures and n.name not in seen:
                walk(measures[n.name], seen | {n.name})
        elif isinstance(n, tuple):
            for x in n:
                walk(x, seen)
        elif hasattr(n, "__dataclass_fields__"):
            for f in n.__dataclass_fields__:
                walk(getattr(n, f), seen)

    walk(e, _seen)
    return out


class Lowering:
    """Parsed calculation -> expression tree, against a model and its field names.

    `lower(node)` returns (expr, level) with level one of "const", "row", "agg";
    row-level trees use Col for every field (the caller picks the home table and
    relatedifies). Calculated columns a formula references must already be in
    `model` (callers lower calcs in dependency order).
    """

    def __init__(self, model: Model, names: Names, *, allow_captions: bool = False,
                 what: str = "calculation"):
        self.model = model
        self.names = names
        self.allow_captions = allow_captions
        self.what = what
        self.types = {(t.name, c.name): c.data_type for t in model.tables for c in t.columns}

    # ---------------------------------------------------------- entry points
    def lower(self, node):
        e, level = self._low(node)
        if isinstance(e, _LodTotal):
            raise Unsupported("FIXED level of detail expression used without aggregation", V,
                              f"{self.what}: aggregate it with MIN/MAX/AVG")
        return e, level

    # --------------------------------------------------------------- helpers
    def _type(self, e) -> DataType | None:
        return infer_type(e, self.model)

    def _mix(self, levels, where: str) -> str:
        levels = {lv for lv in levels if lv != "const"}
        if levels == {"row", "agg"}:
            raise ModelError(f"Tableau calculation {self.what}: Cannot mix aggregate and "
                             f"non-aggregate arguments with this function ({where})")
        return levels.pop() if levels else "const"

    def _no_lod(self, *exprs) -> None:
        for e in exprs:
            if isinstance(e, _LodTotal):
                raise Unsupported("FIXED level of detail expression used as a row-level value",
                                  V, f"{self.what}: only MIN/MAX/AVG({{FIXED : ...}}) "
                                  f"lowers exactly")

    # ------------------------------------------------------------- lowering
    def _low(self, n):
        if isinstance(n, TLit):
            return Lit(n.value), "const"
        if isinstance(n, TField):
            return self._field(n)
        if isinstance(n, TNeg):
            e, lv = self._low(n.arg)
            self._no_lod(e)
            return Neg(e), lv
        if isinstance(n, TNot):
            e, lv = self._low(n.arg)
            self._no_lod(e)
            return Not(e), lv
        if isinstance(n, TBin):
            return self._bin(n)
        if isinstance(n, TIf):
            return self._if(n)
        if isinstance(n, TCase):
            return self._case(n)
        if isinstance(n, TIn):
            arg, lv = self._low(n.arg)
            vals = [self._low(v) for v in n.values]
            self._no_lod(arg, *(v for v, _ in vals))
            level = self._mix([lv, *(x for _, x in vals)], "IN")
            out = None
            for v, _ in vals:
                eq = BinOp("=", arg, v)
                out = eq if out is None else BinOp("or", out, eq)
            return out, level
        if isinstance(n, TLod):
            return self._lod(n)
        if isinstance(n, TCall):
            return self._call(n)
        raise ModelError(f"Tableau calculation {self.what}: cannot lower {n!r}")

    def _field(self, f: TField):
        if f.qualifier is not None:
            if f.qualifier == COUNT_QUALIFIER:
                raise Unsupported("table count field in a calculation", V,
                                  f"{self.what}: \"You can't build calculations on top of a "
                                  f"table's Count field. Count is aggregate-only.\" (Tableau, "
                                  f"'What's Changed with Data Sources and Analysis')")
            if f.qualifier == "Parameters":
                raise Unsupported("parameter", V, f"[Parameters].[{f.name}] in {self.what}")
            raise Unsupported("data blending (field of another data source)", V,
                              f"[{f.qualifier}].[{f.name}] in {self.what}")
        ref = self.names.resolve(f.name, self.allow_captions)
        if isinstance(ref, Col):
            return ref, "row"
        return ref, "agg"

    def _bin(self, n: TBin):
        if n.op in ("%", "^"):
            raise Unsupported({"%": "% (modulo) operator", "^": "^ (power) operator"}[n.op], V,
                              f"in {self.what}")
        left, ll = self._low(n.left)
        right, rl = self._low(n.right)
        self._no_lod(left, right)
        level = self._mix([ll, rl], n.op)
        if n.op == "/":
            return Divide(left, right), level
        if n.op == "+" and "string" in (self._type(left), self._type(right)):
            return BinOp("&", left, right), level
        op = {"AND": "and", "OR": "or"}.get(n.op, n.op)
        return BinOp(op, left, right), level

    def _if(self, n: TIf):
        parts = []
        for cond, then in n.branches:
            c, cl = self._low(cond)
            t, tl = self._low(then)
            parts.append((c, t, cl, tl))
        else_, el = (None, "const") if n.else_ is None else self._low(n.else_)
        self._no_lod(else_, *(p[0] for p in parts), *(p[1] for p in parts))
        level = self._mix([el, *(p[2] for p in parts), *(p[3] for p in parts)], "IF")
        out = else_
        for c, t, _, _ in reversed(parts):
            out = If(c, t, out)
        return out, level

    def _case(self, n: TCase):
        subject, sl = self._low(n.subject)
        parts = [(self._low(v), self._low(t)) for v, t in n.whens]
        else_, el = (None, "const") if n.else_ is None else self._low(n.else_)
        self._no_lod(subject, else_, *(p[0][0] for p in parts), *(p[1][0] for p in parts))
        level = self._mix([sl, el, *(p[0][1] for p in parts), *(p[1][1] for p in parts)],
                          "CASE")
        out = else_
        for (v, _), (t, _) in reversed(parts):
            out = If(BinOp("=", subject, v), t, out)
        return out, level

    def _lod(self, n: TLod):
        if n.kind != "FIXED":
            raise Unsupported(f"{n.kind} level of detail expression", V,
                              f"in {self.what}: it changes the view's level of detail, "
                              f"which a measure cannot see")
        if n.dims:
            raise Unsupported("FIXED level of detail expression with dimensions", V,
                              f"in {self.what}: its value depends on whether the view's "
                              f"level of detail contains the FIXED dimensions")
        body, level = self._low(n.body)
        self._no_lod(body)
        if level != "agg":
            raise ModelError(f"Tableau calculation {self.what}: the expression in a level of "
                             f"detail expression must be an aggregate")
        tables = base_tables(body, self.model)
        if len(tables) != 1:
            raise Unsupported("FIXED level of detail expression over several tables", V,
                              f"in {self.what}: aggregates {sorted(tables)}; the cells it "
                              f"is NULL in depend on which table's rows the view has")
        return _LodTotal(body, tables.pop()), "row"

    def _call(self, n: TCall):
        name = n.name
        if name in TABLE_CALCS:
            raise Unsupported(f"table calculation {name}", V, f"in {self.what}")
        if name in OTHER_AGGREGATES:
            raise Unsupported(name, V, f"aggregation {name} in {self.what}")
        if name in _AGG:
            return self._agg(n)
        if name in ("ZN", "IFNULL", "ISNULL", "IIF"):
            return self._nulls(n)
        if name in ("DATEPART", "DATETRUNC"):
            return self._dates(n)
        if name in _SCALAR:
            func, arity = _SCALAR[name]
            if len(n.args) not in arity:
                raise ModelError(f"Tableau calculation {self.what}: {name} takes "
                                 f"{' or '.join(map(str, arity))} arguments")
            args = [self._low(a) for a in n.args]
            self._no_lod(*(a for a, _ in args))
            return Func(func, tuple(a for a, _ in args)), self._mix([lv for _, lv in args], name)
        raise Unsupported(f"function {name}", V, f"in {self.what}")

    def _agg(self, n: TCall):
        name = n.name
        if len(n.args) != 1:
            if name in ("MIN", "MAX") and len(n.args) == 2:
                raise Unsupported(f"{name} with two arguments (row-level {name})", V,
                                  f"in {self.what}")
            raise ModelError(f"Tableau calculation {self.what}: {name} takes one argument")
        inner, level = self._low(n.args[0])
        if isinstance(inner, _LodTotal):
            if name in ("MIN", "MAX", "AVG"):
                guard = BinOp(">", Agg("count_rows", table=inner.table), Lit(0))
                return If(guard, Calculate(inner.agg, (RemoveFilters(),)), None), "agg"
            raise Unsupported(f"{name} of a FIXED level of detail expression", V,
                              f"in {self.what}: {name} multiplies the constant by the rows "
                              f"of each cell; only MIN/MAX/AVG lower exactly")
        if level == "agg":
            raise ModelError(f"Tableau calculation {self.what}: argument to {name} is already "
                             f"an aggregation, and cannot be further aggregated")
        func = _AGG[name]
        if isinstance(inner, Col):
            return Agg(func, inner), "agg"
        tables = col_tables(inner)
        if not tables:
            raise Unsupported(f"{name} of a constant", V,
                              f"in {self.what}: with several logical tables a constant has "
                              f"no rows of its own to aggregate")
        if func == "count_distinct":
            raise Unsupported("COUNTD of a calculation", V, f"in {self.what}")
        home = home_table(tables, self.model, self.what)
        expr = relatedify(inner, home)
        check_cross_table(expr, self.what)
        return IterAgg(func, home, expr), "agg"

    def _nulls(self, n: TCall):
        name = n.name
        args = [self._low(a) for a in n.args]
        self._no_lod(*(a for a, _ in args))
        level = self._mix([lv for _, lv in args], name)
        exprs = [a for a, _ in args]
        if name == "ZN":
            if len(exprs) != 1:
                raise ModelError(f"Tableau calculation {self.what}: ZN takes one argument")
            return coalesce((exprs[0], Lit(0))), level
        if name == "IFNULL":
            if len(exprs) != 2:
                raise ModelError(f"Tableau calculation {self.what}: IFNULL takes two arguments")
            return coalesce(tuple(exprs)), level
        if name == "ISNULL":
            if len(exprs) != 1:
                raise ModelError(f"Tableau calculation {self.what}: ISNULL takes one argument")
            return is_null(exprs[0]), level
        if len(exprs) not in (3, 4):
            raise ModelError(f"Tableau calculation {self.what}: IIF takes 3 or 4 arguments")
        test, then, else_ = exprs[:3]
        unknown = exprs[3] if len(exprs) == 4 else None
        return If(test, then, If(Not(test), else_, unknown)), level

    def _dates(self, n: TCall):
        name = n.name
        if len(n.args) not in (2, 3) or not isinstance(n.args[0], TLit) \
                or not isinstance(n.args[0].value, str):
            raise ModelError(f"Tableau calculation {self.what}: {name} takes a date part "
                             f"string, a date and an optional start of week")
        part = n.args[0].value.lower()
        start = None
        if len(n.args) == 3:
            if not isinstance(n.args[2], TLit) or not isinstance(n.args[2].value, str):
                raise Unsupported(f"{name} with a computed start of week", V, f"in {self.what}")
            start = n.args[2].value.lower()
        date, level = self._low(n.args[1])
        self._no_lod(date)
        if name == "DATEPART":
            func = _DATEPART.get(part)
            if func is None:
                raise Unsupported(f"DATEPART('{part}')", V, f"in {self.what}")
        elif part == "week":
            if start != "monday":
                raise Unsupported("DATETRUNC('week') without 'monday' as start of week", V,
                                  f"in {self.what}: the week start then follows the data "
                                  f"source locale (Sunday for en_US); the model's weeks "
                                  f"start on Monday")
            func = "date_trunc_week"
        else:
            func = _DATETRUNC.get(part)
            if func is None:
                raise Unsupported(f"DATETRUNC('{part}')", V, f"in {self.what}")
        if name == "DATETRUNC" and self._type(date) == "timestamp":
            raise Unsupported("DATETRUNC of a datetime field", V,
                              f"in {self.what}: Tableau keeps the datetime type (midnight of "
                              f"the first day), the model's date_trunc returns a date")
        return Func(func, (date,)), level


def coalesce(args: tuple) -> Coalesce:
    """Coalesce with nested coalesces flattened: IFNULL(a, IFNULL(b, c)) is
    COALESCE(a, b, c), so one meaning has one tree."""
    flat: list = []
    for a in args:
        flat.extend(a.args if isinstance(a, Coalesce) else (a,))
    return Coalesce(tuple(flat))


def is_null(e) -> If:
    """ISNULL(e): TRUE exactly when e is NULL (e = e is NULL only then)."""
    return If(BinOp("=", e, e), Lit(False), Lit(True))


# ============================================================== formatting
_PREC = {"or": 1, "and": 2, "=": 5, "<>": 5, "<": 5, ">": 5, "<=": 5, ">=": 5,
         "+": 6, "-": 6, "&": 6, "*": 7, "/": 7}
_AGG_NAME = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "count": "COUNT",
             "count_distinct": "COUNTD"}
_FUNC_NAME = {"abs": "ABS", "round": "ROUND", "floor": "FLOOR", "ceiling": "CEILING",
              "year": "YEAR", "quarter": "QUARTER", "month": "MONTH", "day": "DAY",
              "upper": "UPPER", "lower": "LOWER", "len": "LEN", "trim": "TRIM",
              "left": "LEFT", "right": "RIGHT"}


def literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, Decimal)):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, dt.datetime):
        return "#" + v.strftime("%Y-%m-%d %H:%M:%S") + "#"
    if isinstance(v, dt.date):
        return "#" + v.isoformat() + "#"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    raise Unsupported(f"literal of type {type(v).__name__}", V)


class Formatter:
    """Expression tree -> Tableau formula text, using `names` for field names.

    The output parses back (with `Lowering`) to the same tree for every tree in
    the image of `Lowering`; trees outside it raise Unsupported naming the
    construct Tableau has no exact spelling for.
    """

    def __init__(self, model: Model, names: Names):
        self.model = model
        self.names = names

    def fmt(self, e, prec: int = 0) -> str:
        if isinstance(e, Lit):
            text = literal(e.value)
            return f"({text})" if text.startswith("-") and prec > 0 else text
        if isinstance(e, (Col, Related)):
            return bracket(self.names.name_of(Col(e.table, e.column)))
        if isinstance(e, MeasureRef):
            return bracket(self.names.name_of(e))
        if isinstance(e, BinOp):
            p = _PREC[e.op]
            op = {"and": "AND", "or": "OR", "&": "+"}.get(e.op, e.op)
            # Left-associative: the right operand of an equal-precedence operator
            # needs parentheses ((a - b) - c vs a - (b - c)).
            text = f"{self.fmt(e.left, p)} {op} {self.fmt(e.right, p + 1)}"
            return f"({text})" if p < prec else text
        if isinstance(e, Neg):
            return f"-{self.fmt(e.arg, 9)}"
        if isinstance(e, Not):
            text = f"NOT {self.fmt(e.arg, 3)}"
            return f"({text})" if prec > 3 else text
        if isinstance(e, Divide):
            if e.alternate is None:
                text = f"{self.fmt(e.num, 7)} / {self.fmt(e.den, 8)}"
                return f"({text})" if prec > 7 else text
            den = self.fmt(e.den, 5)
            return (f"IF ISNULL({self.fmt(e.den)}) OR {den} = 0 THEN {self.fmt(e.alternate)} "
                    f"ELSE {self.fmt(e.num, 7)} / {self.fmt(e.den, 8)} END")
        if isinstance(e, Coalesce):
            return self._coalesce(e.args)
        if isinstance(e, Func):
            return self._func(e)
        if isinstance(e, If):
            return self._if(e)
        if isinstance(e, Agg):
            if e.func == "count_rows":
                raise Unsupported("row count of a logical table in a calculation", V,
                                  f"COUNTROWS({e.table}): a table's Count field is "
                                  f"aggregate-only and cannot be used in calculations; "
                                  f"COUNT([key]) counts non-NULL keys, not rows")
            return f"{_AGG_NAME[e.func]}({self.fmt(e.arg)})"
        if isinstance(e, IterAgg):
            tables = col_tables(e.expr) | _related_tables(e.expr)
            if not tables or home_table(tables, self.model, "IterAgg") != e.table:
                raise Unsupported("aggregation over a table the expression does not read", V,
                                  f"{e.func.upper()}X over {e.table!r}: Tableau aggregates a "
                                  f"formula over the rows of the tables it references")
            if isinstance(e.expr, Col):
                raise Unsupported("iterator over a single column", V,
                                  f"write Agg({e.func!r}, {e.expr!r}) instead")
            check_cross_table(e.expr, "IterAgg")
            return f"{_AGG_NAME[e.func]}({self.fmt(e.expr)})"
        if isinstance(e, Calculate):
            return self._calculate(e)
        if isinstance(e, TimeShift):
            raise Unsupported(f"time intelligence ({e.kind})", V,
                              "Tableau has no time-intelligence functions (no TOTALYTD / "
                              "SAMEPERIODLASTYEAR); a date-shifted measure has no exact "
                              "calculated-field spelling")
        raise Unsupported(f"expression {type(e).__name__}", V)

    def _coalesce(self, args) -> str:
        if len(args) == 2 and args[1] == Lit(0):
            return f"ZN({self.fmt(args[0])})"
        if len(args) == 2:
            return f"IFNULL({self.fmt(args[0])}, {self.fmt(args[1])})"
        return f"IFNULL({self.fmt(args[0])}, {self._coalesce(args[1:])})"

    def _func(self, e: Func) -> str:
        if e.name in _FUNC_NAME:
            return f"{_FUNC_NAME[e.name]}({', '.join(self.fmt(a) for a in e.args)})"
        if e.name in ("date_trunc_month", "date_trunc_quarter", "date_trunc_year"):
            return f"DATETRUNC('{e.name.rsplit('_', 1)[1]}', {self.fmt(e.args[0])})"
        if e.name == "date_trunc_week":
            return f"DATETRUNC('week', {self.fmt(e.args[0])}, 'monday')"
        raise Unsupported(f"scalar function {e.name}", V,
                          "DATEPART('weekday') numbering is locale-dependent in Tableau")

    def _if(self, e: If) -> str:
        # ISNULL(x)
        if (isinstance(e.cond, BinOp) and e.cond.op == "=" and e.cond.left == e.cond.right
                and e.then == Lit(False) and e.else_ == Lit(True)):
            return f"ISNULL({self.fmt(e.cond.left)})"
        # MIN({FIXED : AGG})
        lod = _lod_total(e)
        if lod is not None:
            agg, table = lod
            if base_tables(agg, self.model) != {table}:
                raise Unsupported("FIXED level of detail expression over several tables", V)
            return f"MIN({{FIXED : {self.fmt(agg)}}})"
        # IIF(test, then, else[, unknown])
        if isinstance(e.else_, If) and e.else_.cond == Not(e.cond):
            args = [self.fmt(e.cond), self.fmt(e.then), self.fmt(e.else_.then)]
            if e.else_.else_ is not None:
                args.append(self.fmt(e.else_.else_))
            return f"IIF({', '.join(args)})"
        parts = [f"IF {self.fmt(e.cond)} THEN {self.fmt(e.then)}"]
        rest = e.else_
        while isinstance(rest, If) and not (isinstance(rest.else_, If)
                                            and rest.else_.cond == Not(rest.cond)) \
                and _lod_total(rest) is None and not (
                    isinstance(rest.cond, BinOp) and rest.cond.op == "="
                    and rest.cond.left == rest.cond.right and rest.then == Lit(False)):
            parts.append(f"ELSEIF {self.fmt(rest.cond)} THEN {self.fmt(rest.then)}")
            rest = rest.else_
        if rest is not None:
            parts.append(f"ELSE {self.fmt(rest)}")
        return " ".join(parts) + " END"

    def _calculate(self, e: Calculate) -> str:
        for m in e.modifiers:
            if isinstance(m, KeepFilters):
                raise Unsupported("KEEPFILTERS filter in a measure", V,
                                  "write the condition inside the aggregate: "
                                  "SUM(IF <condition> THEN [field] END)")
            if isinstance(m, RemoveFilters):
                raise Unsupported("ALL / REMOVEFILTERS in a measure", V,
                                  "Tableau's only filter-ignoring construct is a FIXED level "
                                  "of detail expression, which also keeps context filters and "
                                  "is NULL in cells without rows")
            if isinstance(m, KeepOnly):
                raise Unsupported("ALLEXCEPT in a measure", V)
        raise Unsupported("filter override (CALCULATE with a filter) in a measure", V,
                          "a Tableau calculation cannot REPLACE a view's filter on a field; "
                          "SUM(IF [field] = value THEN ... END) intersects with it instead, "
                          "which differs whenever the view filters or groups by that field")


def _lod_total(e) -> tuple | None:
    """If(count_rows(T) > 0, Calculate(agg, (RemoveFilters(),)), None) -> (agg, T)."""
    if (isinstance(e, If) and e.else_ is None and isinstance(e.cond, BinOp)
            and e.cond.op == ">" and e.cond.right == Lit(0)
            and isinstance(e.cond.left, Agg) and e.cond.left.func == "count_rows"
            and isinstance(e.then, Calculate) and e.then.modifiers == (RemoveFilters(),)):
        return e.then.expr, e.cond.left.table
    return None


# ============================================================ public entry
def lower_measure(text: str, model: Model, names: Names, what: str,
                  allow_captions: bool = False):
    """Formula text -> (expr, level, home table or None)."""
    lowering = Lowering(model, names, allow_captions=allow_captions, what=what)
    expr, level = lowering.lower(parse_tree(text))
    if level == "agg":
        return expr, level, None
    tables = col_tables(expr)
    if not tables:
        return expr, level, None
    home = home_table(tables, model, what)
    expr = relatedify(expr, home)
    check_cross_table(expr, what)
    return expr, level, home


def parse_expression(text: str, model: Model, *, table: str | None = None) -> Expr:
    """Tableau formula text -> expression tree, names per `Names.for_model(model)`
    (model names accepted too). A row-level formula comes back over its home
    table - Col for that table's fields, Related for the others - or over `table`
    when given (a calculated column's table), which must reach every table the
    formula reads: Tableau formulas are not relative to a table, so `table` only
    picks the row the formula is evaluated on."""
    expr, level, home = lower_measure(text, model, Names.for_model(model), "expression",
                                      allow_captions=True)
    if table is None or level != "row":
        return expr
    if home is not None and home != table:
        tables = col_tables(expr) | _related_tables(expr)
        if not tables <= reach(model).get(table, {table}):
            raise ModelError(f"Tableau calculation: {text!r} reads {sorted(tables)}, which "
                             f"{table!r} does not reach many-to-one")
        expr = relatedify(_unrelate(expr), table)
        check_cross_table(expr, "expression")
    return expr


def _unrelate(e):
    """Related -> Col everywhere (before re-homing a row-level tree)."""
    if isinstance(e, Related):
        return Col(e.table, e.column)
    if isinstance(e, tuple):
        return tuple(_unrelate(x) for x in e)
    if hasattr(e, "__dataclass_fields__") and not isinstance(e, (Agg, IterAgg)):
        return type(e)(**{f: _unrelate(getattr(e, f)) for f in e.__dataclass_fields__})
    return e


def format_expression(expr: Expr, model: Model, *, table: str | None = None) -> str:
    """Expression tree -> Tableau formula text. `table` (a calculated column's
    home) changes nothing: a Tableau formula names fields, not rows."""
    return Formatter(model, Names.for_model(model)).fmt(expr)
