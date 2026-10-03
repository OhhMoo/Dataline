"""Visual -> canonical {columns, rows}: the semantic engine's front door.

Graders call this on the agent's FINAL project files, in the grading zone
(world.semantic rule 2), so it must be exact, deterministic and loud:

    render_visual(model, visual, warehouse)  -> {"columns": [...], "rows": [[...]]}
    render_report(model, report, warehouse)  -> {page: {visual: <render_visual>}}
    validate(model, reports=())              -> None, or raises errors.ModelError

`columns` are the group_by column names, then the value labels. Values are
`hashing.canon_value`'d (int/str/bool/None as-is, Decimal -> str, float ->
repr, date -> ISO), the same canon as world/bi.py's panels, so a number here and
a number a legacy dashboard printed compare byte for byte.

Rows, after compile.py's query has evaluated every candidate group (its
docstring says which groups are candidates):
  1. Visual.groups == "measures" (Power BI): groups whose every value is blank
     are dropped (auto-exist); "rows" (Tableau, Sigma): every candidate group
     stays. A visual with no values keeps every group in both modes (it lists
     what exists), and a visual with no group_by is exactly one row, blank or not;
  2. ordered by Visual.order_by, then ascending by the group_by columns as the
     tie-break (so the order is total); blank sorts lowest (DAX's order: first
     ascending, last descending); text compares by code point;
  3. top_n keeps the first n rows of that order. When the order_by keys of
     the n-th and the (n+1)-th rows tie, the cut is refused -
     Unsupported("TOPN with ties at the boundary"): DAX's TOPN keeps every tied
     row, other tools cut by their own tie-break, and no answer is everyone's.

THE WAREHOUSE CONNECTION. Always in-memory, the file ATTACHed READ_ONLY as `wh`
and never opened directly: DuckDB names a catalog after the file stem, and
`warehouse.duckdb`'s stem collides with schema `warehouse` (world/README.md).
Model queries run under `USE wh` (so a native-SQL table's `warehouse.x` and a
Table.source resolve the same way); a legacy SQL visual runs under
`USE wh.warehouse` when that schema exists - exactly what world/bi.py always
did, so a panel that says `from fct_revenue` keeps working. threads=1.

Errors. compile.py's SQL raises refusals through DuckDB `error()` with a
marker; they come back here as errors.Unsupported. A relation or column the
warehouse does not have is the model's mistake: errors.ModelError, with
DuckDB's message.
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb

from ..hashing import canon_value
from . import compile as C
from .errors import ModelError, SemanticError, Unsupported
from .model import Model, Report, Visual

_REFUSAL = re.compile(re.escape(C.REFUSE_MARKER) + r"\[([^|\]]*)\|([^\]]*)\]")


def validate(model: Model, reports: tuple[Report, ...] = ()) -> None:
    """Raise errors.ModelError for anything malformed in the model or the reports:
    unknown tables / columns / measures, measure-reference cycles, a bad date
    table, relationships over missing columns, ambiguous or cyclic relationship
    paths, visuals grouping by columns no single table reaches, SQL visuals that
    also set fields. Touches no data."""
    g = C.check_model(model)
    for r in reports:
        C.check_report(g, r)


class Session:
    """One read-only connection to a warehouse file, reused across visuals.

    Pre-flight checks (one-side uniqueness, date tables, blank comparisons) are
    data facts, cached per session: a report checks each relationship once."""

    def __init__(self, warehouse: Path):
        self.warehouse = Path(warehouse)
        self.con = duckdb.connect(":memory:", config={"threads": 1})
        self.con.execute("ATTACH '" + str(self.warehouse).replace("'", "''")
                         + "' AS wh (READ_ONLY)")
        self.has_warehouse_schema = bool(self.con.execute(
            "SELECT 1 FROM duckdb_schemas() WHERE database_name = 'wh' "
            "AND schema_name = 'warehouse'").fetchall())
        self._checked: dict[str, SemanticError | None] = {}
        self._graphs: dict[int, tuple[Model, C.Graph]] = {}

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.con.close()

    # ------------------------------------------------------------------
    def sql_rows(self, sql: str) -> tuple[list[str], list[list]]:
        """A legacy SQL panel: the query's rows, unchanged but canonical."""
        self.con.execute("USE wh.warehouse" if self.has_warehouse_schema else "USE wh")
        cur = self.con.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        return columns, [[canon_value(c) for c in r] for r in cur.fetchall()]

    def graph(self, model: Model) -> C.Graph:
        hit = self._graphs.get(id(model))
        if hit is None or hit[0] is not model:
            hit = (model, C.check_model(model))
            self._graphs[id(model)] = hit
        return hit[1]

    def visual(self, model: Model, visual: Visual) -> dict:
        if visual.sql is not None:
            C.check_visual(C.Graph(Model("", ())), visual)
            columns, rows = self.sql_rows(visual.sql)
            return {"columns": columns, "rows": rows}
        g = self.graph(model)
        C.check_visual(g, visual)
        compiled = C.compile_visual(g, visual)
        self.con.execute("USE wh")
        for check in compiled.checks:
            self._run_check(check)
        raw = self._execute(compiled.sql)
        return {"columns": compiled.labels, "rows": finish(visual, compiled, raw)}

    def _run_check(self, check: C.Check) -> None:
        if check.sql not in self._checked:
            hit = self._execute(check.sql)
            self._checked[check.sql] = check.error if hit else None
        if self._checked[check.sql] is not None:
            raise self._checked[check.sql]

    def _execute(self, sql: str) -> list[tuple]:
        try:
            return self.con.execute(sql).fetchall()
        except duckdb.Error as exc:
            m = _REFUSAL.search(str(exc))
            if m:
                raise Unsupported(m.group(1), m.group(2) or None) from None
            if isinstance(exc, (duckdb.CatalogException, duckdb.BinderException,
                                duckdb.ConversionException)):
                # a relation / column the warehouse lacks, or values of types
                # that do not combine (If(c, 1, "a")): the model's mistake
                raise ModelError(f"the warehouse cannot answer the model: {exc}") from None
            raise


def finish(visual: Visual, compiled: C.Compiled, raw: list[tuple]) -> list[list]:
    """Auto-exist, order, top_n, canon (the module docstring's three steps)."""
    k = compiled.n_groups
    rows = [list(r) for r in raw]
    if k and visual.values and visual.groups == "measures":
        rows = [r for r in rows if any(v is not None for v in r[k:])]
    labels = compiled.labels
    # the tie-break first, then each order key from last to first: Python's sort
    # is stable, so the result is ordered by the keys in order, then the groups
    for i in reversed(range(k)):
        rows.sort(key=lambda r, i=i: _key(r[i]))
    for label, desc in reversed(visual.order_by):
        i = labels.index(label)
        rows.sort(key=lambda r, i=i: _key(r[i]), reverse=desc)
    if visual.top_n is not None:
        n = visual.top_n
        if visual.order_by and len(rows) > n:
            keys = [labels.index(label) for label, _ in visual.order_by]
            if [rows[n - 1][i] for i in keys] == [rows[n][i] for i in keys]:
                raise Unsupported("TOPN with ties at the boundary", None,
                                  f"visual {visual.name!r}: rows {n} and {n + 1} tie on "
                                  f"{[label for label, _ in visual.order_by]}")
        rows = rows[:n]
    return [[canon_value(v) for v in r] for r in rows]


def _key(v):
    """Blank lowest; everything else by value (numbers compare across int /
    Decimal / float exactly)."""
    return (v is not None, v if v is not None else 0)


def render_visual(model: Model, visual: Visual, warehouse: Path) -> dict:
    """One visual over the warehouse file at `warehouse`."""
    with Session(warehouse) as s:
        return s.visual(model, visual)


def render_report(model: Model, report: Report, warehouse: Path) -> dict:
    """Every visual of a report: {page name: {visual name: render_visual dict}}."""
    validate(model, (report,))
    out: dict[str, dict] = {}
    with Session(warehouse) as s:
        for page in report.pages:
            out[page.name] = {v.name: s.visual(model, v) for v in page.visuals}
    return out


__all__ = ["Session", "render_report", "render_visual", "validate"]
