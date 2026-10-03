"""Tableau workbooks (.twb) <-> a Report: worksheets are Visuals, dashboards Pages.

A worksheet's meaning is spread over its `<table>`: the fields on the rows and
columns shelves (`[ds].[none:segment:nk]`, `[ds].[sum:order_amount:qk]`), the
fields on the Marks card (`<encodings>`), `<filter>`s and sorts inside `<view>`,
and the column instances `<datasource-dependencies>` declares for all of them.
This module lowers exactly the subset below and refuses the rest by Tableau's
name for it (shapes from MIT-licensed 2024/2025 Desktop saves and the Apache-2.0
twb schema at github.com/tableau/tableau-document-schemas):

    derivation None on any shelf/encoding    a grouping column (Visual.group_by)
    Sum/Avg/Min/Max/Count/CountD of a field  Agg(...)          label as Tableau shows it
    User (an aggregate calculation)          MeasureRef / a workbook-local calc
    Count of a table's Count field           Agg(count_rows)   "<Table> (Count)"
    :Measure Names + Measure Values          the measures listed in the Measure Names
                                             filter, in that order
    categorical filter (member/union/except) FilterValues (negate for except)
    quantitative filter, in-range            FilterCompare >= min, <= max
    computed-sort on the innermost dimension order_by (measure label, DESC?)
    alphabetic-sort                          order_by (column, DESC?)
    Top N filter on the only dimension,      top_n, when the view has no other filter
      with a matching computed sort          (Top N is computed before dimension
                                             filters, Visual.top_n after them)
    (every view)                             groups="rows": Tableau shows a group that
                                             has rows even when its measures are null
    Non-null filter on the only measure      groups="measures" (the one exact spelling
                                             of dropping all-blank groups)

Refused: date parts on a shelf (YEAR(Order Date) is a computed grouping the model
cannot name), table calculations / quick table calcs, measure filters, context
filters, relative-date filters, manual sorts, sorting an outer dimension by a
measure, disaggregated views, sets, parameters, blending, stories.

LABELS are what Tableau's own crosstab/CSV export prints for the pill: the
caption for SUM and for calculations, "Avg. x", "Min. x", "Max. x", "Count of x",
"Distinct count of x" (help.tableau.com "Query View Data" examples; the TSC test
fixture populate_csv.csv). A Visual value whose label is not the one Tableau
would print is written as a workbook-local calculated field captioned with it.

The workbook connects to the PUBLISHED data source (`sqlproxy`) the way a data
engineer's workbooks do: the model lives in one .tds, published once, and every
workbook reads it. Reading also accepts workbooks that embed their data source.
"""
from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal

from ...errors import ModelError, Unsupported
from ...expr import Agg, Col, Expr, FilterCompare, FilterValues, MeasureRef
from ...model import Model, Page, Report, Visual
from . import calc, tds
from .xmlio import FILE_VERSION, SOURCE_BUILD, sub, user

V = "Tableau"

_AGG_DERIVATION = {"Sum": "sum", "Avg": "avg", "Min": "min", "Max": "max", "Count": "count",
                   "CountD": "count_distinct"}
_PREFIX = {"sum": "Sum", "avg": "Avg", "min": "Min", "max": "Max", "count": "Count",
           "count_distinct": "CountD"}
_INSTANCE_PREFIX = {"Sum": "sum", "Avg": "avg", "Min": "min", "Max": "max", "Count": "cnt",
                    "CountD": "ctd", "None": "none", "User": "usr"}
_DATE_PARTS = {"Year", "Quarter", "Month", "Week", "Day", "Weekday", "Hour", "Minute",
               "Second", "Year-Trunc", "Quarter-Trunc", "Month-Trunc", "Week-Trunc",
               "Day-Trunc", "Hour-Trunc", "Minute-Trunc", "Second-Trunc", "MY", "MDY",
               "ISO-Year", "ISO-Quarter", "ISO-Week", "ISO-Weekday"}
_MARK_KIND = {"Bar": "barChart", "Line": "lineChart", "Area": "areaChart",
              "Circle": "scatterChart", "Shape": "scatterChart", "Pie": "pieChart",
              "Square": "heatmap", "Map": "map", "Polygon": "map", "Gantt Bar": "ganttChart"}
_KIND_MARK = {"barChart": "Bar", "lineChart": "Line", "areaChart": "Area",
              "scatterChart": "Circle", "pieChart": "Pie", "heatmap": "Square",
              "card": "Text", "table": "Text"}
MEASURE_NAMES = ":Measure Names"
MEASURE_VALUES = "Multiple Values"


def default_label(func: str, caption: str) -> str:
    return {"sum": caption, "avg": f"Avg. {caption}", "min": f"Min. {caption}",
            "max": f"Max. {caption}", "count": f"Count of {caption}",
            "count_distinct": f"Distinct count of {caption}",
            "count_rows": f"{caption} (Count)"}[func]


# ================================================================== reading
@dataclass
class _Instance:
    column: str          # the field's name, unbracketed
    derivation: str
    table_calc: bool
    qualifier: str | None = None   # __tableau_internal_object_id__ for count fields


def _parts(ref: str) -> list[str]:
    return tds._split_qualified(ref)


def read(root: ET.Element, *, name: str, strict: bool = True,
         resolve_published=None, deferred: dict | None = None
         ) -> tuple[tds.DataSource, Report]:
    """A `<workbook>` -> (its one data source, its Report).

    Lenient reading (`strict=False`, the fake server's mode) keeps going past a
    worksheet outside the subset: the sheet is left out of the Report and its
    refusal is put in `deferred[sheet name]`, to be raised when that sheet's data
    is asked for - the way Tableau Server renders the other sheets of a workbook
    one of whose sheets it cannot."""
    what = f"workbook {name!r}"
    sources: dict[str, ET.Element] = {}
    for d in root.findall("datasources/datasource"):
        sources[d.get("name")] = d
    cache: dict[str, tds.DataSource] = {}

    def source(ds_name: str) -> tds.DataSource:
        if ds_name == "Parameters":
            raise Unsupported("parameter", V, f"{what} uses parameters")
        if ds_name not in sources:
            raise ModelError(f"{what}: worksheet uses unknown data source {ds_name!r}")
        if ds_name not in cache:
            cache[ds_name] = tds.read(sources[ds_name], name_hint=ds_name, strict=strict,
                                      resolve_published=resolve_published)
        return cache[ds_name]

    visuals: dict[str, Visual] = {}
    used: set[str] = set()
    order: list[str] = []
    for ws in root.findall("worksheets/worksheet"):
        ws_name = ws.get("name")
        ds_names = [d.get("name") for d in ws.findall("table/view/datasources/datasource")]
        if "Parameters" in ds_names:
            raise Unsupported("parameter", V, f"{what}: worksheet {ws_name!r}")
        if len(ds_names) > 1:
            raise Unsupported("data blending", V, f"{what}: worksheet {ws_name!r} uses "
                              f"{len(ds_names)} data sources")
        if not ds_names:
            visuals[ws_name] = Visual(ws_name)
            order.append(ws_name)
            continue
        used.add(ds_names[0])
        try:
            visuals[ws_name] = _visual(ws, ds_names[0], source(ds_names[0]), what)
        except Unsupported as exc:
            if strict or deferred is None:
                raise
            deferred[ws_name] = exc
            continue
        order.append(ws_name)
    if len(used) > 1:
        raise Unsupported("workbook over several data sources", V,
                          f"{what} uses {sorted(used)}; a Report has one model")
    if used:
        ds = source(used.pop())
    elif sources:
        ds = source(next(iter(sources)))
    else:
        raise ModelError(f"{what} has no data source")
    pages: list[Page] = []
    on_dashboard: set[str] = set()
    for dash in root.findall("dashboards/dashboard"):
        sheets: list[str] = []
        for zone in dash.iter("zone"):
            zname = zone.get("name")
            if zname and zone.get("type-v2") is None and zone.get("type") is None \
                    and zname not in sheets:
                if zname in visuals:
                    sheets.append(zname)
                elif deferred is not None and zname in deferred:
                    deferred.setdefault(dash.get("name"), deferred[zname])
        on_dashboard.update(sheets)
        pages.append(Page(dash.get("name"), tuple(visuals[s] for s in sheets)))
    for ws_name in order:
        if ws_name not in on_dashboard:
            pages.append(Page(ws_name, (visuals[ws_name],)))
    return ds, Report(name=name, model=ds.model.name, pages=tuple(pages))


def _visual(ws: ET.Element, ds_name: str, ds: tds.DataSource, what: str) -> Visual:
    ws_name = ws.get("name")
    what = f"{what}: worksheet {ws_name!r}"
    table = ws.find("table")
    view = table.find("view")
    if view.find("aggregation") is not None and view.find("aggregation").get("value") == "false":
        raise Unsupported("disaggregated measures (Aggregate Measures off)", V, what)
    instances: dict[str, _Instance] = {}
    deps = view.find("datasource-dependencies")
    if deps is not None:
        for ci in deps.findall("column-instance"):
            parts = _parts(ci.get("column"))
            inst_parts = _parts(ci.get("name"))
            instances[inst_parts[-1]] = _Instance(
                column=parts[-1], derivation=ci.get("derivation"),
                table_calc=ci.find("table-calc") is not None,
                qualifier=parts[0] if len(parts) > 1 else None)
    binder = _Binder(ds, ds_name, instances, what)

    shelf_refs: list[str] = []
    for shelf in ("rows", "cols"):
        text = table.findtext(shelf) or ""
        shelf_refs.extend(_shelf_fields(text))
    for enc in table.findall("panes/pane/encodings/*"):
        if enc.get("column"):
            shelf_refs.append(enc.get("column"))
    group_by: list[Col] = []
    values: list[tuple[str, Expr]] = []
    measure_names = False
    for ref in shelf_refs:
        kind, item = binder.field(ref)
        if kind == "measure_names":
            measure_names = True
        elif kind == "measure_values":
            continue
        elif kind == "dim":
            if item not in group_by:
                group_by.append(item)
        elif item not in values:
            values.append(item)
    filters: list = []
    top = None
    non_null: list = []
    for flt in view.findall("filter"):
        out = binder.filter(flt)
        if out is None:
            continue
        if out[0] == "non_null":
            non_null.append(out[1])
        elif out[0] == "measure_names":
            values = [v for v in values if v not in out[1]] + list(out[1])
            measure_names = True
        elif out[0] == "top":
            top = out[1]
        else:
            filters.extend(out[1])
    if measure_names and not any(f.get("column", "").endswith(f"[{MEASURE_NAMES}]")
                                 for f in view.findall("filter")):
        raise Unsupported("Measure Names without a Measure Names filter", V, what)
    # Tableau shows every group that has rows (Visual.groups "rows"). The one
    # exact spelling of "measures" - drop groups whose values are all blank - is a
    # Null-values-excluded filter on the view's only measure.
    groups = "rows"
    if non_null:
        if len(non_null) != 1 or len(values) != 1 or non_null[0] != values[0] or not group_by:
            raise Unsupported("measure filter", V,
                              f"{what}: a filter on an aggregate is applied after "
                              f"aggregation; only 'Non-null values' on the view's single "
                              f"measure lowers exactly")
        groups = "measures"
    order_by = binder.sorts(view, group_by, values)
    top_n = None
    if top is not None:
        dim, count, direction, label = top
        if group_by != [dim] or filters:
            raise Unsupported("Top N filter with other dimensions or filters", V,
                              f"{what}: Tableau computes Top N before the view's other "
                              f"dimension filters; the model applies top_n after them")
        if order_by[:1] != [(label, direction == "DESC")]:
            raise Unsupported("Top N filter without a matching sort", V, what)
        top_n = count
    mark = table.find("panes/pane/mark")
    mark_class = mark.get("class") if mark is not None else "Automatic"
    if mark_class == "Text":
        kind = "card" if not group_by else "table"
    else:
        kind = _MARK_KIND.get(mark_class, "table")
    visual = Visual(name=ws_name, group_by=tuple(group_by), values=tuple(values),
                    filters=tuple(filters), order_by=tuple(order_by), top_n=top_n, kind=kind,
                    groups=groups)
    check_directions(ds.model, visual, what)
    return visual


def check_directions(model: Model, visual: Visual, what: str) -> None:
    """Refuse a view whose dimension or filter sits on a table the measure's
    table does not reach many-to-one.

    Tableau's relationships are not directional: COUNTD([customer_name]) by
    [order_status] counts each status's customers through the join, and a
    filter on an Orders field subsets a Customer measure. The model filters only
    from the one side to the many side, so it would print the same total on
    every status row - a silently different number, hence a refusal."""
    reaches = calc.reach(model)
    columns = [*visual.group_by, *(f.column for f in visual.filters)]
    for label, expr in visual.values:
        for base in calc.base_tables(expr, model):
            for col in columns:
                if col.table not in reaches.get(base, {base}):
                    raise Unsupported(
                        "dimension from the many side of a relationship", V,
                        f"{what}: [{label}] aggregates {base!r}, which does not reach "
                        f"{col.table}.{col.column} many-to-one; Tableau would compute it "
                        f"through the relationship, the model would not filter it")


def _shelf_fields(text: str) -> list[str]:
    """Field references on a shelf, in order: `([a].[b] / [a].[c])` -> two refs.
    The operators (`/` nest, `*` cross, `+` concatenate panes) do not change the
    set of groups or measures, only the layout."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "[":
            j, parts = i, []
            while j < len(text) and text[j] == "[":
                k = j + 1
                while k < len(text):
                    if text[k] == "]" and text[k + 1:k + 2] == "]":
                        k += 2
                        continue
                    if text[k] == "]":
                        break
                    k += 1
                parts.append(text[j:k + 1])
                j = k + 1
                if text[j:j + 2] == ".[":
                    j += 1
                    continue
                break
            out.append(".".join(parts))
            i = j
        else:
            i += 1
    return out


class _Binder:
    """Worksheet field references -> model expressions, for one data source."""

    def __init__(self, ds: tds.DataSource, ds_name: str, instances: dict[str, _Instance],
                 what: str):
        self.ds, self.ds_name, self.instances, self.what = ds, ds_name, instances, what

    def _instance(self, ref: str) -> tuple[_Instance | None, str]:
        parts = _parts(ref)
        if len(parts) >= 2 and parts[0] != self.ds_name:
            raise Unsupported("data blending", V, f"{self.what}: field of {parts[0]!r}")
        name = parts[-1]
        inst = self.instances.get(name)
        if inst is None and ":" in name:
            prefix, _, rest = name.partition(":")
            column, _, _suffix = rest.rpartition(":")
            deriv = {v: k for k, v in _INSTANCE_PREFIX.items()}.get(prefix)
            if deriv is not None:
                inst = _Instance(column=column, derivation=deriv, table_calc=False,
                                 qualifier=parts[1] if len(parts) == 3 else None)
        return inst, name

    def field(self, ref: str):
        parts = _parts(ref)
        if parts[-1] == MEASURE_NAMES:
            return "measure_names", None
        if parts[-1] == MEASURE_VALUES:
            return "measure_values", None
        inst, name = self._instance(ref)
        if inst is None:
            if name.endswith("(generated)"):
                raise Unsupported("generated latitude/longitude", V, self.what)
            raise ModelError(f"{self.what}: no column instance for {ref}")
        return self._lower(inst, ref)

    def _lower(self, inst: _Instance, ref: str):
        what = f"{self.what}: {ref}"
        if inst.table_calc:
            raise Unsupported("table calculation (quick table calculation)", V, what)
        if inst.qualifier == calc.COUNT_QUALIFIER:
            table = self.ds.names.counts.get(inst.column)
            if table is None:
                raise ModelError(f"{what}: unknown logical table {inst.column!r}")
            if inst.derivation not in ("Count", "Sum"):
                raise Unsupported(f"{inst.derivation} of a table count field", V, what)
            return "value", (default_label("count_rows", table),
                             Agg("count_rows", table=table))
        d = inst.derivation
        if d in _DATE_PARTS:
            raise Unsupported(f"date part on a shelf ({d})", V,
                              f"{what}: group by a column holding the {d.lower()} instead")
        if d in ("Attribute", "Median", "Stdev", "Stdevp", "Var", "Varp", "Percentile",
                 "InOut", "Collect"):
            raise Unsupported({"Attribute": "ATTR", "InOut": "set (IN/OUT)"}.get(d, d.upper()),
                              V, what)
        if inst.column in self.ds.local_calcs:
            caption, expr = self.ds.local_calcs[inst.column]
            if d not in ("User", "None", "Agg"):
                raise ModelError(f"{what}: an aggregate calculation cannot be re-aggregated")
            return "value", (caption, expr)
        ref_obj = self.ds.names.resolve(inst.column, allow_captions=False)
        if isinstance(ref_obj, MeasureRef):
            return "value", (ref_obj.name, ref_obj)
        if d == "None":
            return "dim", ref_obj
        func = _AGG_DERIVATION.get(d)
        if func is None:
            raise Unsupported(f"{d} derivation", V, what)
        return "value", (default_label(func, ref_obj.column), Agg(func, ref_obj))

    # ------------------------------------------------------------ filters
    def filter(self, flt: ET.Element):
        col_ref = flt.get("column", "")
        if flt.get("context") == "true":
            raise Unsupported("context filter", V, f"{self.what}: {col_ref}")
        cls = flt.get("class")
        if col_ref.endswith(f"[{MEASURE_NAMES}]"):
            return "measure_names", self._measure_names(flt)
        if cls == "relative-date":
            raise Unsupported("relative date filter", V, self.what)
        if cls not in ("categorical", "quantitative"):
            raise Unsupported(f"{cls} filter", V, self.what)
        kind, item = self.field(col_ref)
        if kind == "value" and cls == "quantitative" \
                and flt.get("included-values") == "non-null" \
                and flt.find("min") is None and flt.find("max") is None:
            return "non_null", item
        if kind != "dim":
            raise Unsupported("measure filter", V,
                              f"{self.what}: a filter on an aggregate ({col_ref}) is applied "
                              f"after aggregation; the model filters rows")
        column: Col = item
        dtype = self._dtype(column)
        if cls == "quantitative":
            included = flt.get("included-values", "in-range")
            if included == "all":
                return None
            if included != "in-range":
                raise Unsupported(f"quantitative filter including {included}", V, self.what)
            out = []
            lo, hi = flt.findtext("min"), flt.findtext("max")
            if lo is not None:
                out.append(FilterCompare(column, ">=", _member(lo, dtype, self.what)))
            if hi is not None:
                out.append(FilterCompare(column, "<=", _member(hi, dtype, self.what)))
            return "filters", out
        gf = flt.find("groupfilter")
        if gf is None:
            return None
        fn = gf.get("function")
        if fn == "end":
            return "top", self._top(gf, column)
        if fn == "level-members":
            return None
        if fn == "member":
            return "filters", [FilterValues(column, (_member(gf.get("member"), dtype,
                                                             self.what),))]
        if fn == "union":
            members = []
            for child in gf.findall("groupfilter"):
                if child.get("function") != "member":
                    raise Unsupported(f"{child.get('function')} in a filter", V, self.what)
                members.append(_member(child.get("member"), dtype, self.what))
            return "filters", [FilterValues(column, tuple(members))]
        if fn == "except":
            kids = gf.findall("groupfilter")
            if not kids or kids[0].get("function") != "level-members":
                raise Unsupported("exclusion filter of a computed set", V, self.what)
            members = []
            for child in kids[1:]:
                pool = [child] if child.get("function") == "member" else child.findall(
                    "groupfilter")
                for m in pool:
                    if m.get("function") != "member":
                        raise Unsupported(f"{m.get('function')} in a filter", V, self.what)
                    members.append(_member(m.get("member"), dtype, self.what))
            return "filters", [FilterValues(column, tuple(members), negate=True)]
        if fn in ("filter", "range"):
            raise Unsupported("condition or wildcard filter", V, self.what)
        raise Unsupported(f"{fn} filter", V, self.what)

    def _measure_names(self, flt: ET.Element) -> list:
        gf = flt.find("groupfilter")
        members = []
        if gf is None:
            return []
        pool = [gf] if gf.get("function") == "member" else gf.findall("groupfilter")
        if gf.get("function") not in ("member", "union"):
            raise Unsupported("Measure Names filter that is not a list", V, self.what)
        for m in pool:
            raw = m.get("member", "")
            if raw.startswith('"') and raw.endswith('"'):
                raw = raw[1:-1].replace('""', '"')
            kind, item = self.field(raw)
            if kind != "value":
                raise ModelError(f"{self.what}: Measure Names lists a dimension {raw}")
            members.append(item)
        return members

    def _top(self, gf: ET.Element, column: Col):
        count = int(gf.get("count", "0"))
        end = gf.get("end", "top")
        order = gf.find("groupfilter")
        if order is None or order.get("function") != "order" or not order.get("expression"):
            raise Unsupported("Top N filter by formula or field list", V, self.what)
        expr, level, _home = calc.lower_measure(order.get("expression"), self.ds.model,
                                                self.ds.names, f"{self.what}: Top N")
        if level != "agg":
            raise ModelError(f"{self.what}: Top N by a non-aggregate")
        label = None
        if isinstance(expr, MeasureRef):
            label = expr.name
        elif isinstance(expr, Agg) and expr.arg is not None:
            label = default_label(expr.func, expr.arg.column)
        direction = order.get("direction", "DESC")
        if (end == "top") != (direction == "DESC"):
            direction = "ASC" if direction == "DESC" else "DESC"
        return column, count, direction, label

    def sorts(self, view: ET.Element, group_by: list[Col], values) -> list[tuple[str, bool]]:
        sorts: dict[Col, tuple[str, bool]] = {}
        for el in view:
            tag = el.tag
            if tag == "sort":
                tag = {"computed": "computed-sort", "alphabetic": "alphabetic-sort",
                       "manual": "manual-sort", "natural": "natural-sort"}.get(
                    el.get("class"), "sort")
            if tag not in ("computed-sort", "alphabetic-sort", "manual-sort", "natural-sort"):
                continue
            kind, col = self.field(el.get("column"))
            if kind != "dim":
                raise ModelError(f"{self.what}: sort on a non-dimension")
            desc = el.get("direction", "ASC") == "DESC"
            if tag == "manual-sort":
                raise Unsupported("manual sort", V, self.what)
            if tag == "natural-sort":
                sorts[col] = (col.column, desc)
            elif tag == "alphabetic-sort":
                sorts[col] = (col.column, desc)
            else:
                kind, item = self.field(el.get("using"))
                if kind != "value":
                    raise ModelError(f"{self.what}: sort using a non-measure")
                label, expr = item
                if (label, expr) not in values:
                    raise Unsupported("sort by a measure that is not in the view", V, self.what)
                sorts[col] = (label, desc)
        for shelf_sort in view.findall("shelf-sorts/shelf-sort-v2"):
            kind, col = self.field(shelf_sort.get("dimension-to-sort"))
            kind2, item = self.field(shelf_sort.get("measure-to-sort-by"))
            if kind != "dim" or kind2 != "value":
                raise ModelError(f"{self.what}: malformed shelf sort")
            sorts[col] = (item[0], shelf_sort.get("direction", "ASC") == "DESC")
        out = []
        for i, col in enumerate(group_by):
            entry = sorts.get(col)
            if entry is None:
                out.append((col.column, False))
                continue
            if entry[0] != col.column and i != len(group_by) - 1:
                raise Unsupported("sort of an outer dimension by a measure", V,
                                  f"{self.what}: Tableau orders {col.column} by the measure's "
                                  f"total per {col.column}, not per row")
            out.append(entry)
        pairs = zip(out, group_by, strict=True)
        if all(label == col.column and not desc for (label, desc), col in pairs):
            return []
        return out

    def _dtype(self, col: Col):
        for t in self.ds.model.tables:
            if t.name == col.table:
                for c in t.columns:
                    if c.name == col.column:
                        return c.data_type
        return None


def _member(raw: str | None, dtype, what: str):
    """A filter member as Tableau writes it: `"text"`, `2026`, `#2026-01-01#`,
    `%null%`, `true`."""
    if raw is None:
        raise ModelError(f"{what}: filter member without a value")
    if raw == "%null%":
        return None
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1].replace('""', '"')
    if raw.startswith("#") and raw.endswith("#"):
        value = calc._date_literal(raw)
        return value.date() if dtype == "date" and isinstance(value, dt.datetime) else value
    if raw in ("true", "false"):
        return raw == "true"
    try:
        if dtype in ("decimal",) or ("." in raw and dtype != "float"):
            return Decimal(raw)
        if dtype == "float":
            return float(raw)
        return int(raw)
    except (ValueError, ArithmeticError):
        raise ModelError(f"{what}: filter member {raw!r} is not a {dtype}") from None


# ================================================================== writing
@dataclass
class _Sheet:
    """The pieces of one worksheet being written."""
    ds: str
    columns: dict[str, ET.Element] = field(default_factory=dict)
    instances: dict[str, ET.Element] = field(default_factory=dict)


def member_text(value) -> str:
    if value is None:
        return "%null%"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace('"', '""') + '"'
    if isinstance(value, dt.datetime):
        return "#" + value.strftime("%Y-%m-%d %H:%M:%S") + "#"
    if isinstance(value, dt.date):
        return "#" + value.isoformat() + "#"
    return str(value)


class _Writer:
    def __init__(self, model: Model, report: Report, server: str, port: int,
                 embed: tds.Connection | None = None, extract: bool = False):
        self.model, self.report = model, report
        self.server, self.port = server, port
        self.embed, self.extract = embed, extract
        self.names = tds.names_for(model)
        self.ds_name = ("federated." + tds.base36(f"datasource/{model.name}") if embed
                        else "sqlproxy." + tds.base36(f"sqlproxy/{model.name}"))
        self.types = {(t.name, c.name): c.data_type for t in model.tables for c in t.columns}
        self.local_calcs: dict[str, tuple[str, Expr]] = {}
        self.formatter = calc.Formatter(model, self.names)
        self.measures = {m.name: m for m in model.measures}

    # -- field helpers
    def _q(self, inst: str) -> str:
        return f"[{self.ds_name}].{calc.bracket(inst)}"

    def _col_elem(self, local: str, dtype, role: str, typ: str, extra=None) -> ET.Element:
        attrs = {"datatype": calc.tableau_datatype(dtype), "name": calc.bracket(local),
                 "role": role, "type": typ}
        attrs.update(extra or {})
        return ET.Element("column", attrs)

    def dim(self, sheet: _Sheet, col: Col) -> str:
        local = self.names.of_col[(col.table, col.column)]
        dtype = self.types[(col.table, col.column)]
        typ = "quantitative" if dtype in ("int", "decimal", "float") else (
            "ordinal" if dtype in ("date", "timestamp") else "nominal")
        suffix = {"quantitative": "qk", "ordinal": "ok", "nominal": "nk"}[typ]
        inst = f"none:{local}:{suffix}"
        sheet.columns[local] = self._col_elem(local, dtype, "dimension", typ, self._calc(col))
        if self._is_calc(col):
            self._attach_formula(sheet.columns[local], col)
        sheet.instances[inst] = ET.Element("column-instance", {
            "column": calc.bracket(local), "derivation": "None", "name": calc.bracket(inst),
            "pivot": "key", "type": typ})
        return self._q(inst)

    def _is_calc(self, col: Col) -> bool:
        for t in self.model.tables:
            if t.name == col.table:
                return any(c.name == col.column and c.expr is not None for c in t.columns)
        return False

    def _calc(self, col: Col) -> dict:
        local = self.names.of_col[(col.table, col.column)]
        return {"caption": col.column} if col.column != local and self._is_calc(col) else {}

    def _attach_formula(self, elem: ET.Element, col: Col) -> None:
        for t in self.model.tables:
            for c in t.columns:
                if t.name == col.table and c.name == col.column and c.expr is not None:
                    sub(elem, "calculation", {"class": "tableau",
                                              "formula": self.formatter.fmt(c.expr)})

    def value(self, sheet: _Sheet, label: str, expr: Expr) -> str:
        """A Visual value -> the field reference that shows it."""
        if isinstance(expr, MeasureRef) and label == expr.name:
            local = self.names.of_measure[expr.name]
            m = self.measures[expr.name]
            elem = self._col_elem(local, calc.infer_type(m.expr, self.model), "measure",
                                  "quantitative", {"caption": m.name})
            sub(elem, "calculation", {"class": "tableau", "formula": self.formatter.fmt(m.expr)})
            sheet.columns[local] = elem
            return self._instance(sheet, local, "User")
        if isinstance(expr, Agg) and expr.func == "count_rows" \
                and label == default_label("count_rows", expr.table):
            oid = self.names.of_count[expr.table]
            col = f"[{calc.COUNT_QUALIFIER}].{calc.bracket(oid)}"
            inst = f"cnt:{oid}:qk"
            sheet.columns[f"{calc.COUNT_QUALIFIER}.{oid}"] = ET.Element("column", {
                "caption": expr.table, "datatype": "table", "name": col, "role": "measure",
                "type": "quantitative"})
            sheet.instances[inst] = ET.Element("column-instance", {
                "column": col, "derivation": "Count",
                "name": f"[{calc.COUNT_QUALIFIER}].{calc.bracket(inst)}", "pivot": "key",
                "type": "quantitative"})
            return f"[{self.ds_name}].[{calc.COUNT_QUALIFIER}].{calc.bracket(inst)}"
        if isinstance(expr, Agg) and expr.arg is not None \
                and label == default_label(expr.func, expr.arg.column):
            col = expr.arg
            local = self.names.of_col[(col.table, col.column)]
            dtype = self.types[(col.table, col.column)]
            role = "measure" if dtype in ("int", "decimal", "float") else "dimension"
            typ = "quantitative" if role == "measure" else "nominal"
            sheet.columns[local] = self._col_elem(local, dtype, role, typ, self._calc(col))
            if self._is_calc(col):
                self._attach_formula(sheet.columns[local], col)
            return self._instance(sheet, local, _PREFIX[expr.func])
        # Anything else is a calculated field local to this workbook.
        local = calc.calc_name(f"{self.model.name}/{self.report.name}",
                               len(self.local_calcs), f"{label}/{expr!r}")
        for name, (cap, e) in self.local_calcs.items():
            if (cap, e) == (label, expr):
                local = name
        self.local_calcs[local] = (label, expr)
        elem = self._col_elem(local, calc.infer_type(expr, self.model), "measure",
                              "quantitative", {"caption": label})
        sub(elem, "calculation", {"class": "tableau", "formula": self.formatter.fmt(expr)})
        sheet.columns[local] = elem
        return self._instance(sheet, local, "User")

    def _instance(self, sheet: _Sheet, local: str, derivation: str) -> str:
        inst = f"{_INSTANCE_PREFIX[derivation]}:{local}:qk"
        sheet.instances[inst] = ET.Element("column-instance", {
            "column": calc.bracket(local), "derivation": derivation,
            "name": calc.bracket(inst), "pivot": "key", "type": "quantitative"})
        return self._q(inst)

    # -- worksheet
    def worksheet(self, visual: Visual) -> ET.Element:
        if visual.sql is not None:
            raise Unsupported("SQL panel", V, f"{visual.name!r}: a Tableau worksheet is not a "
                              f"SQL query; use custom SQL in the data source")
        check_directions(self.model, visual, f"worksheet {visual.name!r}")
        sheet = _Sheet(self.ds_name)
        dims = [self.dim(sheet, c) for c in visual.group_by]
        vals = [self.value(sheet, label, e) for label, e in visual.values]
        value_by_label = dict(zip((label for label, _ in visual.values), vals, strict=True))
        ws = ET.Element("worksheet", {"name": visual.name})
        table = sub(ws, "table")
        view = sub(table, "view")
        dss = sub(view, "datasources")
        sub(dss, "datasource", {"caption": self.model.name, "name": self.ds_name})
        filters: list[ET.Element] = []
        slices: list[str] = []
        for flt in visual.filters:
            filters.append(self.filter(sheet, flt, slices))
        top_filter = self.top_n(sheet, visual, value_by_label, slices)
        if top_filter is not None:
            filters.append(top_filter)
        if visual.groups == "measures" and visual.group_by and vals:
            if len(vals) != 1:
                raise Unsupported(
                    "hiding groups whose every measure is blank", V,
                    f"{visual.name!r} (groups='measures' over {len(vals)} values): a "
                    f"Tableau view shows every group with rows; a Non-null filter can "
                    f"drop the blank groups of one measure, not of several together")
            filters.append(ET.Element("filter", {"class": "quantitative", "column": vals[0],
                                                 "included-values": "non-null"}))
        # Several measures in a text table go through Measure Names / Measure
        # Values; on a chart they sit side by side on one shelf.
        multi = len(vals) > 1 and visual.kind not in ("barChart", "lineChart", "areaChart")
        if multi:
            names_ref = f"[{self.ds_name}].[{MEASURE_NAMES}]"
            sheet.columns[MEASURE_NAMES] = ET.Element("column", {
                "datatype": "string", "name": f"[{MEASURE_NAMES}]", "role": "dimension",
                "type": "nominal"})
            mn = ET.Element("filter", {"class": "categorical", "column": names_ref})
            union = sub(mn, "groupfilter", {"function": "union",
                                            user("op"): "manage"})
            for ref in vals:
                sub(union, "groupfilter", {"function": "member",
                                           "level": f"[{MEASURE_NAMES}]",
                                           "member": '"' + ref.replace('"', '""') + '"'})
            filters.insert(0, mn)
            slices.insert(0, names_ref)
        deps = sub(view, "datasource-dependencies", {"datasource": self.ds_name})
        for _, elem in sorted([*sheet.columns.items(), *sheet.instances.items()],
                              key=lambda kv: kv[1].get("name")):
            deps.append(elem)
        for f in filters:
            view.append(f)
        sorts = self.sorts(visual, dims, value_by_label)
        for s in sorts:
            view.append(s)
        if slices:
            sl = sub(view, "slices")
            for ref in slices:
                sub(sl, "column", text=ref)
        sub(view, "aggregation", {"value": "true"})
        sub(table, "style")
        panes = sub(table, "panes")
        pane = sub(panes, "pane", {"selection-relaxation-option": "selection-relaxation-allow"})
        pv = sub(pane, "view")
        sub(pv, "breakdown", {"value": "auto"})
        mark = _KIND_MARK.get(visual.kind, "Automatic")
        sub(pane, "mark", {"class": mark})
        rows, cols, text = self.layout(visual, dims, vals, multi)
        if text is not None:
            enc = sub(pane, "encodings")
            sub(enc, "text", {"column": text})
        sub(table, "rows", text=rows) if rows else sub(table, "rows")
        sub(table, "cols", text=cols) if cols else sub(table, "cols")
        sub(ws, "simple-id", {"uuid": tds.guid(f"{self.report.name}/{visual.name}")})
        return ws

    def layout(self, visual: Visual, dims: list[str], vals: list[str], multi: bool):
        """(rows, cols, text encoding) by visual kind; the reader recovers the
        same groups and the same measure order from any of them."""
        def nest(refs):
            return refs[0] if len(refs) == 1 else "(" + " / ".join(refs) + ")"

        def concat(refs):
            return refs[0] if len(refs) == 1 else "(" + " + ".join(refs) + ")"

        names_ref = f"[{self.ds_name}].[{MEASURE_NAMES}]"
        values_ref = f"[{self.ds_name}].[{MEASURE_VALUES}]"
        if visual.kind in ("barChart",) and vals:
            return nest(dims) if dims else None, concat(vals), None
        if visual.kind in ("lineChart", "areaChart") and vals:
            return concat(vals), nest(dims) if dims else None, None
        # card / table / anything else: a text table.
        if multi:
            return (nest(dims) if dims else None), names_ref, values_ref
        return (nest(dims) if dims else None), None, (vals[0] if vals else None)

    def filter(self, sheet: _Sheet, flt, slices: list[str]) -> ET.Element:
        ref = self.dim(sheet, flt.column)
        slices.append(ref)
        level = _parts(ref)[-1]
        dtype = self.types[(flt.column.table, flt.column.column)]
        if isinstance(flt, FilterCompare):
            if flt.op in (">=", "<="):
                el = ET.Element("filter", {"class": "quantitative", "column": ref,
                                           "included-values": "in-range"})
                sub(el, "min" if flt.op == ">=" else "max", text=member_text(flt.value)
                    .strip('"'))
                return el
            if flt.op in ("=", "<>"):
                flt = FilterValues(flt.column, (flt.value,), negate=flt.op == "<>")
            else:
                raise Unsupported(f"filter with {flt.op}", V,
                                  "Tableau range filters include their end points")
        el = ET.Element("filter", {"class": "categorical", "column": ref})
        ui = {user("ui-domain"): "database", user("ui-enumeration"):
              "exclusive" if flt.negate else "inclusive", user("ui-marker"): "enumerate"}
        if dtype in ("date", "timestamp") and any(isinstance(v, str) for v in flt.values):
            raise ModelError("filter on a date column with text values")
        members = [{"function": "member", "level": f"[{level}]", "member": member_text(v)}
                   for v in flt.values]
        if flt.negate:
            ex = sub(el, "groupfilter", {"function": "except", **ui})
            sub(ex, "groupfilter", {"function": "level-members", "level": f"[{level}]"})
            if len(members) == 1:
                sub(ex, "groupfilter", members[0])
            else:
                un = sub(ex, "groupfilter", {"function": "union"})
                for m in members:
                    sub(un, "groupfilter", m)
        elif len(members) == 1:
            sub(el, "groupfilter", {**members[0], **ui})
        else:
            un = sub(el, "groupfilter", {"function": "union", **ui})
            for m in members:
                sub(un, "groupfilter", m)
        return el

    def top_n(self, sheet: _Sheet, visual: Visual, value_by_label, slices):
        if visual.top_n is None:
            return None
        if len(visual.group_by) != 1 or visual.filters or not visual.order_by:
            raise Unsupported("top N of a view with several dimensions or filters", V,
                              f"{visual.name!r}: a Tableau Top N filter ranks one dimension "
                              f"before the view's other filters")
        label, desc = visual.order_by[0]
        expr = dict(visual.values).get(label)
        if expr is None:
            raise Unsupported("top N by a grouping column", V, visual.name)
        ref = self.dim(sheet, visual.group_by[0])
        slices.append(ref)
        level = _parts(ref)[-1]
        el = ET.Element("filter", {"class": "categorical", "column": ref})
        end = sub(el, "groupfilter", {"count": str(visual.top_n),
                                      "end": "top" if desc else "bottom", "function": "end",
                                      "units": "records", user("ui-marker"): "end",
                                      user("ui-top-by-field"): "true"})
        order = sub(end, "groupfilter", {"direction": "DESC" if desc else "ASC",
                                         "expression": self.formatter.fmt(expr)
                                         if not isinstance(expr, MeasureRef)
                                         else calc.bracket(self.names.of_measure[expr.name]),
                                         "function": "order", user("ui-marker"): "order"})
        sub(order, "groupfilter", {"function": "level-members", "level": f"[{level}]",
                                   user("ui-enumeration"): "all", user("ui-marker"): "enumerate"})
        return el

    def sorts(self, visual: Visual, dims: list[str], value_by_label) -> list[ET.Element]:
        if not visual.order_by:
            return []
        out = []
        by_col = {c.column: (c, ref) for c, ref in zip(visual.group_by, dims, strict=True)}
        entries = list(visual.order_by)
        # The shape the reader accepts: one entry per grouping column, in order;
        # the last may be a measure (a computed sort of the innermost dimension).
        if len(entries) != len(visual.group_by):
            raise Unsupported("sort that is not one key per dimension", V,
                              f"{visual.name!r}: Tableau sorts each dimension's members")
        for i, ((label, desc), col) in enumerate(zip(entries, visual.group_by, strict=True)):
            ref = by_col[col.column][1]
            if label == col.column:
                if desc:
                    out.append(ET.Element("alphabetic-sort", {"column": ref,
                                                              "direction": "DESC"}))
                continue
            if label not in value_by_label or i != len(entries) - 1:
                raise Unsupported("sort of an outer dimension by a measure", V, visual.name)
            out.append(ET.Element("computed-sort", {
                "column": ref, "direction": "DESC" if desc else "ASC",
                "using": value_by_label[label]}))
        return out

    # -- dashboards and the document
    def workbook(self) -> ET.Element:
        wb = ET.Element("workbook", {"original-version": FILE_VERSION,
                                     "source-build": SOURCE_BUILD, "source-platform": "win",
                                     "version": FILE_VERSION})
        manifest = sub(wb, "document-format-change-manifest")
        for feature in ("AnimationOnByDefault", "MarkAnimation", "ObjectModelEncapsulateLegacy",
                        "ObjectModelExtractV2", "ObjectModelTableType", "SchemaViewerObjectModel",
                        "SheetIdentifierTracking", "SortTagCleanup"):
            sub(manifest, feature)
        sub(manifest, "_.fcp.VConnDownstreamExtractsWithWarnings.true..."
                      "VConnDownstreamExtractsWithWarnings")
        sub(manifest, "WindowsPersistSimpleIdentifiers")
        prefs = sub(wb, "preferences")
        sub(prefs, "preference", {"name": "ui.encoding.shelf.height", "value": "24"})
        sub(prefs, "preference", {"name": "ui.shelf.height", "value": "26"})
        style = sub(wb, "style")
        rule = sub(style, "style-rule", {"element": "animation"})
        sub(rule, "format", {"attr": "animation-on", "value": "ao-off"})
        datasources = sub(wb, "datasources")
        sheets, seen = [], set()
        dashboards: list[Page] = []
        for page in self.report.pages:
            for visual in page.visuals:
                if visual.name not in seen:
                    seen.add(visual.name)
                    sheets.append(self.worksheet(visual))
            if not (len(page.visuals) == 1 and page.visuals[0].name == page.name):
                dashboards.append(page)
        if self.embed is not None:
            datasources.append(tds.write(self.model, self.embed, root=False,
                                         extract=self.extract, local_calcs=self.local_calcs))
        else:
            datasources.append(tds.write_sqlproxy(self.model, server=self.server,
                                                  port=self.port, local_calcs=self.local_calcs))
        worksheets = sub(wb, "worksheets")
        for ws in sheets:
            worksheets.append(ws)
        if dashboards:
            dash_root = sub(wb, "dashboards")
            for page in dashboards:
                dash_root.append(self.dashboard(page))
        windows = sub(wb, "windows", {"source-height": "30"})
        for page in dashboards:
            win = sub(windows, "window", {"class": "dashboard", "maximized": "true",
                                          "name": page.name})
            vps = sub(win, "viewpoints")
            for v in page.visuals:
                sub(vps, "viewpoint", {"name": v.name})
            sub(win, "active", {"id": "-1"})
            sub(win, "simple-id", {"uuid": tds.guid(f"{self.report.name}/window/{page.name}")})
        for ws in sheets:
            win = sub(windows, "window", {"class": "worksheet", "name": ws.get("name")})
            cards = sub(win, "cards")
            left = sub(sub(cards, "edge", {"name": "left"}), "strip", {"size": "160"})
            for card in ("pages", "filters", "marks"):
                sub(left, "card", {"type": card})
            top = sub(cards, "edge", {"name": "top"})
            for card, size in (("columns", "2147483647"), ("rows", "2147483647"),
                               ("title", "30")):
                sub(sub(top, "strip", {"size": size}), "card", {"type": card})
            sub(win, "simple-id", {"uuid": tds.guid(
                f"{self.report.name}/window/{ws.get('name')}")})
        return wb

    def dashboard(self, page: Page) -> ET.Element:
        if any(v.name == page.name for v in self.report.pages for v in v.visuals):
            raise ModelError(f"dashboard {page.name!r} has the name of a worksheet")
        dash = ET.Element("dashboard", {"enable-sort-zone-taborder": "true", "name": page.name})
        sub(dash, "style")
        sub(dash, "size", {"maxheight": "800", "maxwidth": "1000", "minheight": "800",
                           "minwidth": "1000"})
        zones = sub(dash, "zones")
        n = max(len(page.visuals), 1)
        root = sub(zones, "zone", {"h": "100000", "id": "2", "type-v2": "layout-basic",
                                   "w": "100000", "x": "0", "y": "0"})
        h = 98000 // n
        for i, v in enumerate(page.visuals):
            sub(root, "zone", {"h": str(h), "id": str(3 + i), "name": v.name, "w": "98400",
                               "x": "800", "y": str(1000 + i * h)})
        sub(dash, "simple-id", {"uuid": tds.guid(f"{self.report.name}/dashboard/{page.name}")})
        return dash


def write(model: Model, report: Report, *, server: str = "tableau.internal",
          port: int = 8096, embed: tds.Connection | None = None,
          extract: bool = False) -> ET.Element:
    """The `<workbook>` for `report`: connected to `model` published on `server`,
    or - with `embed` - carrying its own copy of the data source over that
    warehouse connection (an extract when `extract`)."""
    if report.model != model.name:
        raise ModelError(f"report {report.name!r} is over model {report.model!r}, "
                         f"not {model.name!r}")
    return _Writer(model, report, server, port, embed, extract).workbook()


__all__ = ["read", "write", "default_label"]
