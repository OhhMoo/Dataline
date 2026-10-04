"""A Sigma workbook's code representation <-> a semantic Report.

The representation is what `GET /v2/workbooks/{workbookId}?includeContents=true`
returns and `PUT /v2/workbooks/{workbookId}/contents` accepts unchanged ("you
can submit a GET response unchanged"; Beta, Sigma Computing Public REST API
2.0.0+20260930; docs/manage-workbooks-as-code):

    {workbookId, workbookUrlId, name, url, path, latestVersion, ownerId,
     createdBy, updatedBy, createdAt, updatedAt, isArchived, documentVersion,
     contents: {schemaVersion: 1, kind: "workbook",
                pages: [{id, name, visibility?}],
                elements: [ ...every element of every page, flat... ],
                layout: "<?xml ...?><Page id=..><Element elementId=../></Page>.."}}

Elements are flat; the `layout` XML is what puts an element on a page ("the
layout is what assigns an element to a page" - docs/customize-the-layout-of-a-
workbook-in-code-representation). Its `<Page>` elements follow the XML
declaration as siblings, so it is parsed under a synthetic root.

VISUAL <-> ELEMENT (source always the data model: {kind: "data-model",
dataModelId, elementId}; the element's columns read the source as `[B/col]`,
related columns as `[B/Relationship/col]`, metrics as `[Metrics/name]`):

    Visual.kind   Sigma element      grouping columns       values
    card (1)      kpi-chart          -                      value.columnId
    card (n>1)    pivot-table        - (no rows/columns:    values
                                     one row of totals)
    pivotTable    pivot-table        rowsBy (+ columnsBy)   values
    table         table              groupings[0].groupBy   groupings[0].calculations
    barChart      bar-chart          color (category), xAxis   yAxis.columnIds
    lineChart     line-chart         "                      "
    areaChart     area-chart         "                      "

Visual.filters <-> element filters: FilterValues is a `list` filter (`mode:
exclude` when negated); FilterCompare >= / <= on a number is a `number-range`
(min / max, inclusive), on a date a `date-range` `between`. Visual.top_n is a
`top-n` filter ranked by `row-number` on the column the element is sorted by
first. Visual.order_by is the table's `sort`, or a chart's `xAxis.sort`.

GROUPS. A Sigma element shows a group whenever its source has rows for it
after the element's filters, even when every value is null, so every element
reads as Visual.groups = "rows". Writing a grouped visual with groups =
"measures" (Power BI's drop-the-all-blank-groups) is refused; an ungrouped one
is one row in both modes and is written, reading back as "rows".

Refused by name: elements built on warehouse tables, custom SQL or other
elements; ungrouped (detail) tables; nested groupings; trellis; controls;
filters on aggregates (except top-n), Top N by rank (ties), text-match,
hierarchy, relative date filters; every other element kind that carries data.
"""
from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from decimal import Decimal

from ...errors import ModelError, Unsupported
from ...expr import Col, Expr, FilterCompare, FilterValues, MeasureRef
from ...model import Model, Page, Report, Visual
from ._common import (
    EPOCH,
    FOLDER_PATH,
    METRICS,
    OWNER_ID,
    SCHEMA_VERSION,
    base62,
    document_url,
    stable_uuid,
)
from .datamodel import DataModelIndex, data_model_id, element_id
from .formula import (
    FormatScope,
    Scope,
    _tree_table,
    default_relationship_names,
    format_dimension,
    format_formula,
    lower,
    parse_ast,
    unsupported,
)

CHARTS = {"barChart": "bar-chart", "lineChart": "line-chart", "areaChart": "area-chart"}
CHART_KINDS = {v: k for k, v in CHARTS.items()}
_IGNORED = {"text", "image", "divider", "container", "tabbed-container", "button", "embed",
            "navigation", "empty"}
_PAGE_XML = ('<Page type="grid" gridTemplateColumns="repeat(24, 1fr)" '
             'gridTemplateRows="auto" id="{id}">')


# ------------------------------------------------------------------- layout
def parse_layout(layout: str) -> dict[str, list[str]]:
    """page id -> element ids in layout order (containers and tabs flattened)."""
    body = layout.strip()
    if body.startswith("<?xml"):
        body = body[body.index("?>") + 2:]
    try:
        root = ET.fromstring(f"<Layout>{body}</Layout>")
    except ET.ParseError as exc:
        raise ModelError(f"workbook layout is not XML: {exc}") from None
    pages: dict[str, list[str]] = {}
    for page in root:
        if page.tag != "Page":
            raise ModelError(f"workbook layout has a <{page.tag}> outside a <Page>")
        pages[page.get("id")] = [node.get("elementId") for node in page.iter()
                                 if node.tag in ("Element", "Container", "TabbedContainer")]
    return pages


def write_layout(pages: list[tuple[str, list[str]]]) -> str:
    out = ['<?xml version="1.0" encoding="utf-8"?>']
    for page_id, element_ids in pages:
        if not element_ids:
            out.append(_PAGE_XML.format(id=page_id)[:-1] + "/>")
            continue
        out.append(_PAGE_XML.format(id=page_id))
        for i, eid in enumerate(element_ids):
            out.append(f'  <Element elementId="{eid}" gridColumn="1 / 25" '
                       f'gridRow="{1 + 20 * i} / {21 + 20 * i}"/>')
        out.append("</Page>")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ reading
def contents_of(doc: dict) -> dict:
    """The code representation, from a GET (`contents`), a deprecated
    `/spec` response (`document`) or a bare representation."""
    if "contents" in doc:
        return doc["contents"]
    if "document" in doc:
        return doc["document"]
    if "elements" in doc and "pages" in doc:
        return doc
    raise ModelError("workbook has no code representation (contents)")


def read_workbook(doc: dict, model: Model, index: DataModelIndex) -> Report:
    contents = contents_of(doc)
    if contents.get("schemaVersion", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise unsupported(f"schemaVersion {contents.get('schemaVersion')}")
    if contents.get("overlays") or contents.get("automatedActions"):
        raise unsupported("overlays and automated actions" if contents.get("overlays")
                          else "automated actions")
    by_id = {el["id"]: el for el in contents.get("elements", [])}
    placed = parse_layout(contents.get("layout", ""))
    pages = []
    seen: set[str] = set()
    for page in contents.get("pages", []):
        visuals = []
        for eid in placed.get(page["id"], []):
            el = by_id.get(eid)
            if el is None:
                raise ModelError(f"layout places unknown element {eid}")
            seen.add(eid)
            if el.get("kind") in _IGNORED:
                continue
            visuals.append(element_visual(el, model, index))
        pages.append(Page(page["name"], tuple(visuals)))
    missing = set(by_id) - seen
    if missing:
        raise ModelError(f"element {sorted(missing)[0]!r} is not placed in layout")
    name = doc.get("name")
    if not name:
        raise ModelError("workbook has no name")
    return Report(name, model.name, tuple(pages))


def _column_name(col: dict) -> str:
    if col.get("name"):
        return col["name"]
    formula = col.get("formula", "").strip()
    if formula.startswith("[") and formula.endswith("]") and "[" not in formula[1:]:
        return formula[1:-1].split("/")[-1]
    return formula


def _label(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text")
    return None


def element_visual(el: dict, model: Model, index: DataModelIndex) -> Visual:
    """One workbook element -> the Visual it computes (see the module table)."""
    kind = el.get("kind")
    if kind == "control":
        raise unsupported("control element", "a control filters other elements")
    if kind not in ("kpi-chart", "pivot-table", "table") and kind not in CHART_KINDS:
        raise unsupported(f"{kind} element")
    source = el.get("source") or {}
    if source.get("kind") != "data-model":
        raise unsupported(f"workbook element on a {source.get('kind')} source",
                          "only elements built on the data model are modelled")
    if source.get("dataModelId") != index.data_model_id:
        raise ModelError(f"element {el['id']} reads another data model")
    if source.get("groupingId"):
        raise unsupported("element on a grouping level of its source")
    base = index.element_table.get(source.get("elementId"))
    if base is None:
        raise ModelError(f"element {el['id']} reads an unknown data model element")
    columns = {c["id"]: c for c in el.get("columns", [])}
    names = {}
    for c in el.get("columns", []):
        names.setdefault(_column_name(c), parse_ast(c["formula"]))
    metrics = frozenset(index.metrics.get(base, set()))

    def scope() -> Scope:
        return Scope(model, "workbook", base, index.relationship_names, names,
                     metric_names=metrics)

    def column(cid: str) -> dict:
        if cid not in columns:
            raise ModelError(f"element {el['id']} has no column {cid}")
        return columns[cid]

    def dim(cid: str) -> Col:
        expr = lower(parse_ast(column(cid)["formula"]), scope(), "dimension")
        if not isinstance(expr, Col):
            raise unsupported("grouping by a calculated column",
                              f"{column(cid)['formula']} on {el['id']}")
        return expr

    def value(cid: str) -> tuple[str, Expr]:
        c = column(cid)
        return _column_name(c), lower(parse_ast(c["formula"]), scope(), "measure")

    def label(cid: str) -> str:
        c = column(cid)
        return dim(cid).column if cid in dims_ids else _column_name(c)

    dims_ids: list[str] = []
    value_ids: list[str] = []
    order_by: list[tuple[str, bool]] = []
    if kind == "kpi-chart":
        value_ids = [el["value"]["columnId"]]
        vkind = "card"
    elif kind == "pivot-table":
        dims_ids = [r["columnId"] for r in el.get("rowsBy", [])] + \
                   [r["columnId"] for r in el.get("columnsBy", [])]
        value_ids = list(el.get("values", []))
        vkind = "pivotTable" if dims_ids else "card"
    elif kind == "table":
        groupings = el.get("groupings") or []
        if not groupings:
            raise unsupported("ungrouped table element", "detail rows are not measures by "
                                                         "dimensions")
        if len(groupings) > 1:
            raise unsupported("nested groupings")
        dims_ids = list(groupings[0].get("groupBy", []))
        value_ids = list(groupings[0].get("calculations", []))
        for s in el.get("sort", []) or []:
            if s.get("nulls") not in (None, "connection-default"):
                raise unsupported("sort with explicit null placement")
            order_by.append((s["columnId"], s["direction"] == "descending"))
        vkind = "table"
    else:
        if el.get("trellis"):
            raise unsupported("trellis")
        x = (el.get("xAxis") or {}).get("columnId") or (el.get("xAxis") or {}).get("id")
        if not x:
            raise unsupported("chart without an x-axis")
        color = el.get("color") or {}
        if color.get("by") == "category":
            dims_ids.append(color["column"])
        elif color.get("by") == "scale":
            raise unsupported("color by scale")
        dims_ids.append(x)
        y = el.get("yAxis") or {}
        for item in (y.get("columnIds", []) if isinstance(y, dict) else y):
            value_ids.append(item if isinstance(item, str) else
                             item.get("columnId") or item.get("id"))
        sort = (el.get("xAxis") or {}).get("sort")
        if sort:
            by = sort.get("by", x)
            if by == "row-count":
                raise unsupported("sort by row count")
            if sort.get("aggregation"):
                raise unsupported("sort by an aggregation of another column")
            order_by.append((by, sort["direction"] == "descending"))
        vkind = CHART_KINDS[kind]
    if not value_ids:
        raise unsupported(f"{kind} without values")
    group_by = tuple(dim(c) for c in dims_ids)
    values = tuple(value(c) for c in value_ids)
    top_n, filters = _read_filters(el, dims_ids, value_ids, dim, label, order_by, model)
    name = _label(el.get("name")) or el["id"]
    return Visual(name, group_by=group_by, values=values, filters=tuple(filters),
                  order_by=tuple((label(c), desc) for c, desc in order_by), top_n=top_n,
                  kind=vkind, groups="rows")


def _typed(values, dtype: str):
    out = []
    for v in values:
        if v is None:
            out.append(None)
        elif dtype in ("date", "timestamp") and isinstance(v, str):
            stamp = v.replace("Z", "+00:00")
            parsed = dt.datetime.fromisoformat(stamp) if "T" in stamp or " " in stamp \
                else dt.date.fromisoformat(stamp)
            if dtype == "date" and isinstance(parsed, dt.datetime):
                if parsed.time() != dt.time(0):
                    raise ModelError(f"date filter value {v!r} has a time")
                parsed = parsed.date()
            out.append(parsed.replace(tzinfo=None) if isinstance(parsed, dt.datetime)
                       else parsed)
        elif dtype == "decimal" and isinstance(v, int | float) and not isinstance(v, bool):
            out.append(Decimal(str(v)))
        else:
            out.append(v)
    return tuple(out)


def _col_type(model: Model, col: Col) -> str:
    for t in model.tables:
        if t.name == col.table:
            for c in t.columns:
                if c.name == col.column:
                    return c.data_type
    raise ModelError(f"unknown column {col.table}.{col.column}")


def _read_filters(el, dims_ids, value_ids, dim, label, order_by, model):
    top_n = None
    out: list = []
    for f in el.get("filters", []) or []:
        if f.get("state") == "disabled":
            continue
        cid = f["columnId"]
        kind = f.get("kind")
        if kind == "top-n":
            if f.get("rankingFunction") != "row-number" or f.get("mode") not in ("top-n",
                                                                                 "bottom-n"):
                raise unsupported(f"Top N filter ranked by {f.get('rankingFunction')}",
                                  "rank and rank-dense keep ties; only row-number keeps "
                                  "exactly N rows")
            desc = f["mode"] == "top-n"
            if not order_by:
                raise unsupported("Top N filter on an unsorted element",
                                  "the rows Top N keeps are the first rows of the sort")
            if order_by[0] != (cid, desc):
                raise unsupported("Top N filter on a column the element is not sorted by")
            if f.get("includeNulls") not in (None, "when-no-value-is-selected", "never"):
                raise unsupported("Top N filter that always includes nulls")
            top_n = int(f["rowCount"])
            continue
        if cid in value_ids:
            raise unsupported(f"{kind} filter on an aggregate column")
        col = dim(cid)
        dtype = _col_type(model, col)
        if f.get("includeNulls") == "always":
            raise unsupported("filter that always includes nulls")
        if kind == "list":
            values = f.get("values") or []
            if not values:
                continue  # nothing selected: Sigma applies no filter
            out.append(FilterValues(col, _typed(values, dtype),
                                    negate=f.get("mode", "include") == "exclude"))
        elif kind == "number-range":
            if "min" in f and f["min"] is not None:
                out.append(FilterCompare(col, ">=", _typed([f["min"]], dtype)[0]))
            if "max" in f and f["max"] is not None:
                out.append(FilterCompare(col, "<=", _typed([f["max"]], dtype)[0]))
        elif kind == "date-range":
            if f.get("mode") != "between" or "startDate" not in f or "endDate" not in f:
                raise unsupported(f"date-range filter mode {f.get('mode')}",
                                  "relative and open-ended ranges depend on the day it runs")
            if dtype != "date":
                raise unsupported("date-range filter on a timestamp")
            lo, hi = _typed([f["startDate"], f["endDate"]], "date")
            out += [FilterCompare(col, ">=", lo), FilterCompare(col, "<=", hi)]
        else:
            raise unsupported(f"{kind} filter")
    return top_n, out


# ------------------------------------------------------------------ writing
def workbook_id(report: Report) -> str:
    return stable_uuid("workbook", report.name)


def write_workbook(report: Report, model: Model) -> dict:
    if report.model != model.name:
        raise ModelError(f"report {report.name!r} is on model {report.model!r}, "
                         f"not {model.name!r}")
    elements, pages, layout = [], [], []
    names: set[str] = set()
    for page in report.pages:
        page_id = base62("page", report.name, page.name)
        ids = []
        for visual in page.visuals:
            if (page.name, visual.name) in names:
                raise ModelError(f"two visuals named {visual.name!r} on page {page.name!r}")
            names.add((page.name, visual.name))
            el = write_visual(visual, model, report.name, page.name)
            elements.append(el)
            ids.append(el["id"])
        pages.append({"id": page_id, "name": page.name})
        layout.append((page_id, ids))
    url_id = base62("url", "workbook", report.name, length=22)
    return {
        "workbookId": workbook_id(report),
        "workbookUrlId": url_id,
        "name": report.name,
        "url": document_url("workbook", report.name, url_id),
        "path": FOLDER_PATH,
        "latestVersion": 1,
        "ownerId": OWNER_ID,
        "createdBy": OWNER_ID,
        "updatedBy": OWNER_ID,
        "createdAt": EPOCH,
        "updatedAt": EPOCH,
        "isArchived": False,
        "documentVersion": 1,
        "contents": {"schemaVersion": SCHEMA_VERSION, "kind": "workbook", "pages": pages,
                     "elements": elements, "layout": write_layout(layout)},
    }


def _base_of(visual: Visual, model: Model) -> str:
    homes = {m.name: m.table for m in model.measures}
    found: set[str] = set()
    for _value_label, expr in visual.values:
        if isinstance(expr, MeasureRef):
            if expr.name not in homes:
                raise ModelError(f"visual {visual.name!r}: unknown measure {expr.name!r}")
            found.add(homes[expr.name])
        else:
            t = _tree_table(expr)
            if t is None:
                raise unsupported("value that reads no element", visual.name)
            found.add(t)
    if len(found) != 1:
        raise unsupported("element mixing metrics of several data model elements",
                          f"{visual.name}: {sorted(found)}; a Sigma element is built on one")
    return found.pop()


def write_visual(visual: Visual, model: Model, report: str, page: str) -> dict:
    if visual.sql is not None:
        raise unsupported("legacy SQL panel", visual.name)
    if not visual.values:
        raise unsupported("visual without values", visual.name)
    if visual.groups != "rows" and visual.group_by:
        # A Sigma element shows every group its source has rows for, nulls
        # and all. Dropping the all-blank ones would take a filter on an OR of
        # the aggregate columns, which is not exact either: under "measures" a
        # group with NO source rows still shows when a value is non-blank over
        # nothing (Coalesce, a Divide alternate, a literal).
        raise unsupported("dropping all-blank groups (groups='measures')",
                          f"{visual.name}: a Sigma element keeps every group its source "
                          f"has rows for, even when every value is null; write "
                          f"groups='rows'")
    base = _base_of(visual, model)
    rel_names = default_relationship_names(model)

    def fscope() -> FormatScope:
        return FormatScope(model, "workbook", base, rel_names)

    def cid(*parts) -> str:
        return base62("column", report, page, visual.name, *map(str, parts))

    columns: list[dict] = []
    dim_ids: list[str] = []
    for i, col in enumerate(visual.group_by):
        dim_ids.append(cid("dim", i))
        columns.append({"id": dim_ids[-1], "formula": format_dimension(col, fscope())})
    value_ids: list[str] = []
    for i, (label, expr) in enumerate(visual.values):
        value_ids.append(cid("value", i))
        columns.append({"id": value_ids[-1], "formula": format_formula(expr, fscope()),
                        "name": label})
    labels = {c.column: i for i, c in reversed(list(enumerate(visual.group_by)))}

    def id_of(label: str) -> str:
        for i, (lab, _e) in enumerate(visual.values):
            if lab == label:
                return value_ids[i]
        if label in labels:
            return dim_ids[labels[label]]
        raise ModelError(f"visual {visual.name!r} orders by unknown {label!r}")

    filters = []
    helper: dict[Col, str] = {}
    pending = list(visual.filters)
    i = 0
    while i < len(pending):
        f = pending[i]
        col = f.column
        if col in visual.group_by:
            fcid = dim_ids[visual.group_by.index(col)]
        elif col in helper:
            fcid = helper[col]
        else:
            fcid = cid("filter", len(helper))
            helper[col] = fcid
            columns.append({"id": fcid, "formula": format_dimension(col, fscope()),
                            "hidden": True})
        fid = base62("filter", report, page, visual.name, str(i))
        dtype = _col_type(model, col)
        if isinstance(f, FilterValues):
            entry = {"id": fid, "columnId": fcid, "kind": "list",
                     "values": [_json_value(v) for v in f.values]}
            if f.negate:
                entry["mode"] = "exclude"
            filters.append(entry)
            i += 1
            continue
        nxt = pending[i + 1] if i + 1 < len(pending) else None
        pair = (f.op == ">=" and isinstance(nxt, FilterCompare) and nxt.column == col
                and nxt.op == "<=")
        if dtype in ("int", "decimal", "float"):
            if f.op not in (">=", "<="):
                raise unsupported(f"filter {f.op} on a number",
                                  "a Sigma number-range filter has inclusive bounds only")
            entry = {"id": fid, "columnId": fcid, "kind": "number-range"}
            if pair:
                entry.update(min=_json_value(f.value), max=_json_value(nxt.value))
                i += 2
            else:
                entry["min" if f.op == ">=" else "max"] = _json_value(f.value)
                i += 1
            filters.append(entry)
        elif dtype == "date":
            if not pair:
                raise unsupported(f"open-ended date filter {f.op}",
                                  "only a between range (>= and <=) is written exactly")
            filters.append({"id": fid, "columnId": fcid, "kind": "date-range",
                            "mode": "between", "startDate": _json_value(f.value),
                            "endDate": _json_value(nxt.value)})
            i += 2
        else:
            raise unsupported(f"filter {f.op} on a {dtype} column")

    order = [(id_of(lab), desc) for lab, desc in visual.order_by]
    if visual.top_n is not None:
        if not order:
            raise unsupported("Top N without a sort", visual.name)
        first, desc = order[0]
        filters.append({"id": base62("filter", report, page, visual.name, "top-n"),
                        "columnId": first, "kind": "top-n", "rankingFunction": "row-number",
                        "mode": "top-n" if desc else "bottom-n", "rowCount": visual.top_n})

    el: dict = {"id": base62("element", report, page, visual.name), "name": visual.name,
                "source": {"kind": "data-model", "dataModelId": data_model_id(model),
                           "elementId": element_id(model, base)},
                "columns": columns}
    kind = visual.kind
    if kind == "card":
        if visual.group_by or order:
            raise unsupported("card with grouping or sort", visual.name)
        if len(value_ids) == 1:
            el.update(kind="kpi-chart", value={"columnId": value_ids[0]})
        else:
            el.update(kind="pivot-table", values=value_ids)
    elif kind == "pivotTable":
        if not visual.group_by:
            raise ModelError(f"pivotTable {visual.name!r} has no rows: it reads back as a "
                             f"card; write kind='card'")
        if order:
            raise unsupported("sorted pivot table", visual.name)
        el.update(kind="pivot-table", values=value_ids,
                  rowsBy=[{"columnId": d} for d in dim_ids])
    elif kind == "table":
        if not visual.group_by:
            raise unsupported("table without grouping", visual.name)
        el.update(kind="table", groupings=[{"id": base62("grouping", report, page,
                                                         visual.name),
                                            "groupBy": dim_ids, "calculations": value_ids}])
        if order:
            el["sort"] = [{"columnId": c, "direction": "descending" if d else "ascending"}
                          for c, d in order]
    elif kind in CHARTS:
        if not visual.group_by:
            raise unsupported("chart without an x-axis", visual.name)
        if len(visual.group_by) > 2:
            raise unsupported("chart with more than two grouping columns (trellis)",
                              visual.name)
        x = {"columnId": dim_ids[-1]}
        if len(order) > 1:
            raise unsupported("chart sorted by more than one column", visual.name)
        if order:
            x["sort"] = {"by": order[0][0],
                         "direction": "descending" if order[0][1] else "ascending"}
        el.update(kind=CHARTS[kind], xAxis=x, yAxis={"columnIds": value_ids})
        if len(dim_ids) == 2:
            el["color"] = {"by": "category", "column": dim_ids[0]}
    else:
        raise unsupported(f"visual kind {kind}", visual.name)
    if filters:
        el["filters"] = filters
    return el


def _json_value(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() and v.as_tuple().exponent >= 0 \
            else float(v)
    if isinstance(v, dt.datetime):
        return v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return v


__all__ = ["CHARTS", "METRICS", "Unsupported", "element_visual", "parse_layout",
           "read_workbook", "workbook_id", "write_layout", "write_visual", "write_workbook"]
