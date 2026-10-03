"""The DAX *query* subset Power BI's executeQueries REST call takes -> a Visual.

`POST /v1.0/myorg/groups/{g}/datasets/{d}/executeQueries` runs one DAX query
and answers `{"results": [{"tables": [{"rows": [{"Customer[segment]": "SMB",
"[Net Revenue]": 1234.5}]}]}]}`. The engine evaluates Visuals, so a query is
lowered into exactly one Visual (plus the query-scoped measures it DEFINEs):

    [DEFINE MEASURE T[m] = <measure DAX> ...]
    EVALUATE SUMMARIZECOLUMNS ( <group cols>, [<filter tables>], "Label", <expr>, ... )
           | ROW ( "Label", <expr>, ... )
           | TOPN ( n, SUMMARIZECOLUMNS ( ... ), <[Label] | T[c]>, [DESC|ASC|0|1] )
    [ORDER BY <[Label] | T[c]> [ASC|DESC], ...]

Filter tables: TREATAS ( { v, ... }, T[c] ), FILTER ( ALL ( T[c] ) | VALUES (
T[c] ), <condition on T[c]> ), KEEPFILTERS ( ... ) of those, and plain boolean
column filters - the shapes Power BI's own visuals and DAX query view emit.
A SUMMARIZECOLUMNS starts from an empty filter context, so KEEPFILTERS and
the ALL ( 'Date' ) DAX adds for a marked date column change nothing there.

Result column names follow the REST reference: a column "fully qualified in
the query" comes back as `Table[Column]`, a named expression as `[Name]`.
Blank values are omitted from a row unless `serializerSettings.includeNulls`;
dates come back as `2025-01-31T00:00:00`, numbers as JSON numbers.

Refused (errors.Unsupported, "Power BI"): any other EVALUATE table expression
(a bare table, FILTER, ADDCOLUMNS, CALCULATETABLE, SUMMARIZE, ...), DEFINE VAR /
TABLE / COLUMN / FUNCTION, START AT, more than one EVALUATE, two filters on one
column, a TOPN ranked differently from its ORDER BY. TOPN keeps ties in DAX;
Visual.top_n's tie rule is the engine's (see the report to the coordinator).

Sources: DAX queries (learn.microsoft.com/dax/dax-queries), SUMMARIZECOLUMNS,
TOPN, TREATAS, ROW function references (learn.microsoft.com/dax/*-function-dax),
Datasets - Execute Queries In Group (learn.microsoft.com/rest/api/power-bi/
datasets/execute-queries-in-group).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from ...errors import ModelError
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
    Lit,
    MeasureRef,
    Neg,
    Not,
    Related,
    TimeShift,
)
from ...model import Measure, Model, Visual
from .dax import (
    Bool,
    Call,
    ColRef,
    Ctx,
    Ident,
    Lowering,
    Num,
    Parser,
    Str,
    TableRef,
    position,
    syntax_error,
    unsupported,
)

DEFINE_KINDS = ("MEASURE", "VAR", "TABLE", "COLUMN", "FUNCTION")


@dataclass(frozen=True)
class Query:
    """A parsed DAX query: the Visual it is, the measures it DEFINEs, the model
    to evaluate it against (the published model plus those measures), and the
    result column names in order (group columns, then named expressions)."""
    visual: Visual
    measures: tuple[Measure, ...]
    model: Model
    columns: tuple[str, ...]


def query_model(model: Model, measures: tuple[Measure, ...]) -> Model:
    """DEFINE MEASURE overrides a model measure of the same name for the query."""
    defined = {m.name.lower() for m in measures}
    kept = tuple(m for m in model.measures if m.name.lower() not in defined)
    return replace(model, measures=kept + tuple(measures))


class QueryParser:
    def __init__(self, text: str, model: Model):
        self.text = text
        self.model = model
        self.p = Parser(text)

    def error(self, message: str, pos: int) -> ModelError:
        line, col = position(self.text, pos)
        return ModelError(f"Query ({line}, {col}) {message}")

    def parse(self) -> Query:
        p = self.p
        defined: list[tuple[str, str, Any]] = []
        if p.is_kw("DEFINE"):
            p.next()
            while True:
                tok = p.peek()
                word = tok.text.upper() if tok.kind == "ident" else ""
                if word == "MEASURE":
                    p.next()
                    ref = p.primary()
                    if not isinstance(ref, ColRef) or ref.table is None:
                        raise syntax_error(self.text, ref.pos, "MEASURE")
                    p.expect_op("=")
                    defined.append((ref.table, ref.column, p.expr()))
                    continue
                if word in DEFINE_KINDS:
                    raise unsupported(f"DEFINE {word}")
                break
            if not defined:
                raise p.error()
        if not p.is_kw("EVALUATE"):
            raise p.error()
        p.next()
        table_expr = p.expr()
        order: list[tuple[Any, bool]] = []
        if p.is_kw("ORDER"):
            p.next()
            if not p.is_kw("BY"):
                raise p.error()
            p.next()
            while True:
                item = p.expr()
                descending = False
                if p.is_kw("DESC") or p.is_kw("ASC"):
                    descending = p.next().text.upper() == "DESC"
                order.append((item, descending))
                if p.is_op(","):
                    p.next()
                    continue
                break
        if p.is_kw("START"):
            raise unsupported("START AT")
        if p.is_kw("EVALUATE"):
            raise unsupported("more than one EVALUATE", "executeQueries returns one table "
                                                        "per query")
        if p.peek().kind != "eof":
            raise p.error()

        names = tuple(column for _, column, _ in defined)
        lowering = Lowering(self.model, self.text, extra_measures=names)
        measures = []
        for table, name, node in defined:
            home = lowering.table(table, node).name
            measures.append(Measure(name, home, lowering.lower(node, Ctx())))
        model = query_model(self.model, tuple(measures))
        lowering = Lowering(model, self.text)
        visual, columns = self.table_expression(table_expr, lowering, order)
        return Query(visual, tuple(measures), model, columns)

    # -------------------------------------------------------- EVALUATE
    def table_expression(self, node, lowering: Lowering, order) -> tuple[Visual, tuple]:
        top = None
        if isinstance(node, Call) and node.name == "TOPN":
            lowering.arity(node, 2)
            n_node, inner = node.args[0], node.args[1]
            if isinstance(inner, (TableRef, Ident)):
                raise unsupported("EVALUATE of a table reference",
                                  "query measures by columns with SUMMARIZECOLUMNS")
            if len(node.args) == 2:
                raise unsupported("TOPN without an order", "DAX returns arbitrary rows")
            if not (isinstance(n_node, Num) and isinstance(n_node.value, int)):
                raise unsupported("TOPN with a computed row count")
            ranks = list(node.args[2:])
            rank_keys = []
            while ranks:
                key = ranks.pop(0)
                descending = True                       # TOPN's default order is DESC
                if ranks and self.is_order_flag(ranks[0]):
                    descending = self.order_flag(ranks.pop(0))
                rank_keys.append((key, descending))
            top = (n_node.value, rank_keys)
            node = inner
        if isinstance(node, Call) and node.name == "SUMMARIZECOLUMNS":
            group_by, filters, values = self.summarizecolumns(node, lowering)
        elif isinstance(node, Call) and node.name == "ROW":
            if top is not None:
                raise unsupported("TOPN over ROW")
            group_by, filters, values = (), (), self.named_values(node.args, lowering)
        elif isinstance(node, (TableRef, Ident)):
            raise unsupported("EVALUATE of a table reference",
                              "query measures by columns with SUMMARIZECOLUMNS")
        elif isinstance(node, Call):
            raise unsupported(f"DAX function {node.name}", "as an EVALUATE table expression")
        else:
            raise unsupported("EVALUATE of a scalar expression", "wrap it in ROW")
        labels = {c.column: ("col", c) for c in group_by}
        for label, _ in values:
            if label in labels:
                raise ModelError(f"Function 'SUMMARIZECOLUMNS' cannot add column [{label}] "
                                 f"since it already exists.")
            labels[label] = ("value", label)
        order_by = tuple(self.order_label(item, desc, labels, lowering) for item, desc in order)
        top_n = None
        if top is not None:
            n, rank_keys = top
            ranking = tuple(self.order_label(k, d, labels, lowering) for k, d in rank_keys)
            if order_by and order_by[:len(ranking)] != ranking:
                raise unsupported("TOPN ranked differently from ORDER BY")
            order_by = order_by or ranking
            top_n = n
        visual = Visual("Query", group_by=group_by, values=values, filters=filters,
                        order_by=order_by, top_n=top_n, kind="table")
        columns = tuple(f"{c.table}[{c.column}]" for c in group_by) + tuple(
            f"[{label}]" for label, _ in values)
        return visual, columns

    @staticmethod
    def is_order_flag(node) -> bool:
        if isinstance(node, Ident):
            return node.name.upper() in ("ASC", "DESC")
        if isinstance(node, (Bool,)):
            return True
        if isinstance(node, Num):
            return node.value in (0, 1)
        return isinstance(node, Call) and node.name in ("TRUE", "FALSE") and not node.args

    @staticmethod
    def order_flag(node) -> bool:
        """TOPN order: 0 / FALSE / DESC descending, 1 / TRUE / ASC ascending."""
        if isinstance(node, Ident):
            return node.name.upper() == "DESC"
        if isinstance(node, Bool):
            return not node.value
        if isinstance(node, Num):
            return node.value == 0
        return node.name == "FALSE"

    def order_label(self, item, descending: bool, labels: dict, lowering: Lowering):
        if isinstance(item, ColRef) and item.table is None and item.column in labels:
            return (item.column, descending)
        if isinstance(item, ColRef) and item.table is not None:
            col = lowering.column(item.table, item.column, item)
            entry = labels.get(col.column)
            if entry and entry[0] == "col" and entry[1] == col:
                return (col.column, descending)
        raise unsupported("ORDER BY an expression that is not a result column")

    def summarizecolumns(self, node: Call, lowering: Lowering):
        group_by: list[Col] = []
        filters: list = []
        args = list(node.args)
        i = 0
        while i < len(args) and isinstance(args[i], ColRef):
            group_by.append(lowering.colref(args[i]))
            i += 1
        while i < len(args) and not isinstance(args[i], Str):
            arg = args[i]
            if isinstance(arg, Call) and arg.name in ("ALL", "REMOVEFILTERS", "ALLEXCEPT",
                                                       "ALLSELECTED"):
                raise unsupported(f"{arg.name} as a SUMMARIZECOLUMNS filter")
            # KEEPFILTERS intersects with the filter context, which is empty
            # here: the same filter. Unwrapped before lowering, so a marked
            # date column (whose KEEPFILTERS CALCULATE refuses) is fine.
            while isinstance(arg, Call) and arg.name == "KEEPFILTERS" and len(arg.args) == 1:
                arg = arg.args[0]
            for kind, mod in lowering.filter_argument(arg, Ctx()):
                if kind == "time":
                    raise unsupported("time-intelligence filter in SUMMARIZECOLUMNS")
                if kind == "removal":
                    continue                  # the auto ALL ( 'Date' ): a no-op here
                if isinstance(mod, KeepFilters):
                    mod = mod.modifier
                filters.append(mod)
            i += 1
        values = self.named_values(args[i:], lowering)
        seen = set()
        for mod in filters:
            if not isinstance(mod, (FilterValues, FilterCompare)):
                raise unsupported("SUMMARIZECOLUMNS filter", type(mod).__name__)
            if mod.column in seen:
                raise unsupported("two filters on one column in SUMMARIZECOLUMNS")
            seen.add(mod.column)
        if not group_by and not values:
            raise ModelError("SUMMARIZECOLUMNS needs a column or a named expression")
        return tuple(group_by), tuple(filters), values

    def named_values(self, args, lowering: Lowering) -> tuple[tuple[str, Expr], ...]:
        if len(args) % 2:
            raise ModelError("Named expressions must come in \"Name\", expression pairs.")
        out = []
        for k in range(0, len(args), 2):
            name = args[k]
            if not isinstance(name, Str):
                raise self.error("Expected a string name for the expression.", name.pos)
            out.append((name.value, lowering.lower(args[k + 1], Ctx())))
        return tuple(out)


def parse_query(text: str, model: Model) -> Query:
    return QueryParser(text, model).parse()


# ======================================================================
# Results in the executeQueries JSON shape
# ======================================================================
def value_kind(expr: Expr, model: Model, seen: frozenset = frozenset()) -> str:
    """The DAX data type a value comes back as: int, decimal, float, string,
    bool, date or timestamp."""
    if isinstance(expr, Lit):
        v = expr.value
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "int"
        if isinstance(v, Decimal):
            return "decimal"
        if isinstance(v, float):
            return "float"
        if isinstance(v, dt.datetime):
            return "timestamp"
        if isinstance(v, dt.date):
            return "date"
        return "string"
    if isinstance(expr, (Col, Related)):
        for table in model.tables:
            if table.name == expr.table:
                for col in table.columns:
                    if col.name == expr.column:
                        return col.data_type
        return "string"
    if isinstance(expr, MeasureRef):
        if expr.name in seen:
            return "float"
        for measure in model.measures:
            if measure.name == expr.name:
                return value_kind(measure.expr, model, seen | {expr.name})
        return "float"
    if isinstance(expr, Agg):
        if expr.func in ("count", "count_rows", "count_distinct"):
            return "int"
        if expr.func == "avg":
            return "float"
        return value_kind(expr.arg, model, seen) if expr.arg is not None else "float"
    if isinstance(expr, IterAgg):
        if expr.func == "count":
            return "int"
        if expr.func == "avg":
            return "float"
        return value_kind(expr.expr, model, seen)
    if isinstance(expr, BinOp):
        if expr.op in ("=", "<>", "<", ">", "<=", ">=", "and", "or"):
            return "bool"
        if expr.op == "&":
            return "string"
        if expr.op == "/":
            return "float"
        kinds = {value_kind(expr.left, model, seen), value_kind(expr.right, model, seen)}
        if "float" in kinds:
            return "float"
        if "decimal" in kinds:
            return "decimal"
        if kinds <= {"int", "bool"}:
            return "int"
        return kinds.pop() if len(kinds) == 1 else "float"
    if isinstance(expr, Divide):
        return "float"
    if isinstance(expr, Neg):
        return value_kind(expr.arg, model, seen)
    if isinstance(expr, Not):
        return "bool"
    if isinstance(expr, If):
        if expr.then == Lit(None) and expr.else_ is not None:
            return value_kind(expr.else_, model, seen)
        return value_kind(expr.then, model, seen)
    if isinstance(expr, Coalesce):
        for arg in expr.args:
            if arg != Lit(None):
                return value_kind(arg, model, seen)
        return "string"
    if isinstance(expr, Func):
        if expr.name in ("year", "quarter", "month", "day", "weekday", "len"):
            return "int"
        if expr.name.startswith("date_trunc_"):
            return "date"
        if expr.name in ("upper", "lower", "trim", "left", "right"):
            return "string"
        if expr.name == "round":
            return "float"
        return value_kind(expr.args[0], model, seen)
    if isinstance(expr, (Calculate, TimeShift)):
        return value_kind(expr.expr, model, seen)
    return "float"


def json_cell(value: Any, kind: str) -> Any:
    """A canonical engine value (world.hashing.canon_value: decimals and floats
    as strings, dates ISO) -> the JSON value executeQueries serialises."""
    if value is None:
        return None
    if kind in ("date", "timestamp"):
        text = value.isoformat() if isinstance(value, (dt.date, dt.datetime)) else str(value)
        if len(text) == 10:
            return f"{text}T00:00:00"
        return dt.datetime.fromisoformat(text).replace(tzinfo=None).isoformat(timespec="seconds")
    if kind == "bool":
        return value if isinstance(value, bool) else str(value).lower() == "true"
    if kind == "string":
        return value if isinstance(value, str) else str(value)
    if isinstance(value, bool):
        return value
    if kind == "int":
        return int(Decimal(str(value)))
    number = Decimal(str(value)) if not isinstance(value, float) else value
    if isinstance(number, Decimal):
        if kind == "decimal" and number == number.to_integral_value():
            return int(number)
        return float(number)
    return number


MAX_ROWS = 100_000
MAX_VALUES = 1_000_000


def execute_queries_result(query: Query, rendered: dict, *, include_nulls: bool = False
                           ) -> dict:
    """The engine's {"columns", "rows"} for `query.visual` -> executeQueries JSON.

    Rows beyond the documented limits (100,000 rows or 1,000,000 values) are
    cut and the table carries the error the REST reference describes, with a
    200 status, as the service does."""
    kinds = [value_kind(c, query.model) for c in query.visual.group_by]
    kinds += [value_kind(e, query.model) for _, e in query.visual.values]
    rows = rendered.get("rows", [])
    limit = min(MAX_ROWS, MAX_VALUES // max(1, len(query.columns)))
    table: dict[str, Any] = {"rows": []}
    for row in rows[:limit]:
        if len(row) != len(query.columns):
            raise ModelError(f"the engine returned {len(row)} columns for "
                             f"{len(query.columns)} query columns")
        out = {}
        for name, kind, value in zip(query.columns, kinds, row, strict=True):
            cell = json_cell(value, kind)
            if cell is not None or include_nulls:
                out[name] = cell
        table["rows"].append(out)
    if len(rows) > limit:
        table["error"] = {"code": "QueryResultSizeExceeded",
                          "message": f"More than {limit} rows in a query result"}
    return {"results": [{"tables": [table]}]}


__all__ = ["Query", "execute_queries_result", "json_cell", "parse_query", "query_model",
           "value_kind"]
