"""PBIR: the report half of a Power BI project, read and written.

A `<Report>.Report/` folder in the Power BI enhanced report format (the
default since PBIR went GA):

    .platform                                   type Report, displayName, logicalId
    definition.pbir                             datasetReference.byPath -> ../<Model>.SemanticModel
    definition/version.json                     PBIR format version
    definition/report.json                      theme, settings, report-level filters
    definition/pages/pages.json                 pageOrder, activePageName
    definition/pages/<pageId>/page.json         displayName, size, page-level filters
    definition/pages/<pageId>/visuals/<visualId>/visual.json
    StaticResources/SharedResources/BaseThemes/CY24SU10.json

A `Visual` is one visual.json. `visual.visualType` is the kind; the fields are
`visual.query.queryState.<role>.projections`, each a semantic-query expression
(`Column`, `Measure`, `Aggregation`) on an `Entity` (model table); the sort is
`visual.query.sortDefinition`; the visual's title (where the Visual's name
lives) is `visual.visualContainerObjects.title[0].properties.text`; filters are
`filterConfig.filters`: Categorical (`In`, `Not In`) -> FilterValues, Advanced
(`Comparison`, `Not` of an equality) -> FilterCompare, TopN (a subquery with
`Top` and `OrderBy`) -> Visual.top_n.

SEMANTICS THAT ARE PINNED HERE, not guessed:

- Grouping is every Column projection whose `active` is not false (Desktop
  marks the drilled-away levels of a hierarchy `active: false` and expanded
  levels `active: true`); values are the Measure / Aggregation projections in
  the value roles (`Tooltips` adds no displayed value and is skipped).
- Filters from report.json and page.json apply to every visual of the page,
  so they are pushed down into each Visual's filters. Power BI intersects
  filters on one field across levels while a Visual's filters REPLACE per
  column (expr.py), so two filters on one field are refused, not merged.
- A visual with no explicit sort is written with its grouping columns
  ascending (`isDefaultSort: true`), which is what Visual.order_by=() means.
  Reading a visual without a sortDefinition uses the visual type's default
  only where it is the contract's default (table, matrix, line chart, cards);
  other types are refused rather than guessed.
- `sortByColumn` on a grouping column reorders a visual in Power BI, so a
  visual whose sort involves such a column is refused.
- Implicit measures: Sum, Average, Min, Max and Count (non-blank) are exact
  Agg nodes; "Count (Distinct)" counts BLANK as a value like DISTINCTCOUNT
  and is refused.

Literal encoding (semanticQuery schema, QueryLiteralExpression): integers
"24L", decimals "2.4M", doubles "2.4D", text "'x'" ('' escapes), "true",
"datetime'2026-01-01T00:00:00'", "null".

Sources: Power BI enhanced report format (learn.microsoft.com/power-bi/
developer/projects/projects-report); JSON schemas github.com/microsoft/
json-schemas fabric/item/report/definition/* (report 3.3.0, page 2.1.0,
pagesMetadata 1.1.0, versionMetadata 1.0.0, visualContainer 2.12.0,
semanticQuery 1.4.0, filterConfiguration 1.3.0) and definitionProperties
2.0.0 - MIT, vendored in world/tests/fixtures/powerbi/json-schemas and
validated against in the tests; Desktop-written reports in
github.com/microsoft/fabric-toolbox (MIT), whose base theme CY24SU10.json is
shipped next to this module.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from ...errors import ModelError, Unsupported
from ...expr import Agg, Col, Expr, FilterCompare, FilterValues, MeasureRef
from ...model import Model, Page, Report, Visual
from .ids import short_id
from .tmdl import SemanticModelInfo, json_bytes, json_document, platform_json

VENDOR = "Power BI"
SCHEMAS = "https://developer.microsoft.com/json-schemas/fabric/item/report"
PBIR_SCHEMA = f"{SCHEMAS}/definitionProperties/2.0.0/schema.json"
VERSION_SCHEMA = f"{SCHEMAS}/definition/versionMetadata/1.0.0/schema.json"
REPORT_SCHEMA = f"{SCHEMAS}/definition/report/3.3.0/schema.json"
PAGES_SCHEMA = f"{SCHEMAS}/definition/pagesMetadata/1.1.0/schema.json"
PAGE_SCHEMA = f"{SCHEMAS}/definition/page/2.1.0/schema.json"
VISUAL_SCHEMA = f"{SCHEMAS}/definition/visualContainer/2.12.0/schema.json"
THEME = "CY24SU10"
THEME_FILE = Path(__file__).with_name(f"{THEME}.json")

#: Model kind -> (visualType, grouping role, value role).
KINDS = {
    "table": ("tableEx", "Values", "Values"),
    "matrix": ("pivotTable", "Rows", "Values"),
    "lineChart": ("lineChart", "Category", "Y"),
    "barChart": ("barChart", "Category", "Y"),
    "columnChart": ("columnChart", "Category", "Y"),
    "clusteredBarChart": ("clusteredBarChart", "Category", "Y"),
    "clusteredColumnChart": ("clusteredColumnChart", "Category", "Y"),
    "hundredPercentStackedBarChart": ("hundredPercentStackedBarChart", "Category", "Y"),
    "hundredPercentStackedColumnChart": ("hundredPercentStackedColumnChart", "Category", "Y"),
    "areaChart": ("areaChart", "Category", "Y"),
    "stackedAreaChart": ("stackedAreaChart", "Category", "Y"),
    "pieChart": ("pieChart", "Category", "Y"),
    "donutChart": ("donutChart", "Category", "Y"),
    "funnel": ("funnel", "Category", "Y"),
    "treemap": ("treemap", "Group", "Values"),
}
KIND_OF = {"tableEx": "table", "pivotTable": "matrix", "card": "card", "cardVisual": "card",
           "multiRowCard": "card"}
#: Visual types that hold no data: nothing to compute.
DECORATIVE = {"textbox", "image", "shape", "basicShape", "actionButton", "bookmarkNavigator",
              "pageNavigator", "button"}
#: Visual types whose unsorted default order is "grouping columns ascending".
DEFAULT_SORT_IS_GROUPS = {"tableEx", "pivotTable", "lineChart", "areaChart", "stackedAreaChart",
                          "card", "cardVisual", "multiRowCard"}
GROUP_ROLES = ("Category", "Rows", "Axis", "Group", "Columns", "Series", "Legend", "Details",
               "Values")
VALUE_ROLES = ("Y", "Values", "Data", "ColumnY", "LineY", "Y2", "Size", "Gauge")
SKIPPED_ROLES = {"Tooltips"}
AGG_CODES = {"sum": 0, "avg": 1, "min": 3, "max": 4, "count": 5}
AGG_FROM_CODE = {code: func for func, code in AGG_CODES.items()}
AGG_LABEL = {"sum": ("Sum", "Sum of"), "avg": ("Avg", "Average of"), "min": ("Min", "Min of"),
             "max": ("Max", "Max of"), "count": ("CountNonNull", "Count of")}
COMPARISON = {"=": 0, ">": 1, ">=": 2, "<": 3, "<=": 4}
COMPARISON_FROM = {code: op for op, code in COMPARISON.items()}


def unsupported(feature: str, detail: str = "") -> Unsupported:
    return Unsupported(feature, VENDOR, detail)


# ======================================================================
# Literals and fields
# ======================================================================
def literal(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return f"{value}L"
    if isinstance(value, Decimal):
        return f"{format(value, 'f')}M"
    if isinstance(value, float):
        return f"{value!r}D"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, dt.datetime):
        return f"datetime'{value.strftime('%Y-%m-%dT%H:%M:%S')}'"
    if isinstance(value, dt.date):
        return f"datetime'{value.isoformat()}T00:00:00'"
    raise ModelError(f"no Power BI literal for {value!r}")


def parse_literal(text: str, data_type: str | None) -> Any:
    if text == "null":
        return None
    if text in ("true", "false"):
        return text == "true"
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    if text.startswith("datetime'") and text.endswith("'"):
        moment = dt.datetime.fromisoformat(text[9:-1])
        if data_type == "date" and moment.time() == dt.time():
            return moment.date()
        return moment
    if text[-1:] == "L":
        return int(text[:-1])
    if text[-1:] == "M":
        return Decimal(text[:-1])
    if text[-1:] == "D":
        return float(text[:-1])
    raise unsupported("filter literal", text)


def lit(value: str) -> dict:
    return {"expr": {"Literal": {"Value": value}}}


def entity_ref(entity: str) -> dict:
    return {"Expression": {"SourceRef": {"Entity": entity}}}


def column_field(col: Col) -> dict:
    return {"Column": {**entity_ref(col.table), "Property": col.column}}


def measure_home(model: Model, name: str) -> str:
    for measure in model.measures:
        if measure.name == name:
            return measure.table
    raise ModelError(f"measure {name!r} is not in the model")


def value_field(model: Model, label: str, expr: Expr) -> dict:
    """A visual value -> a RoleProjection (Measure or implicit Aggregation)."""
    if isinstance(expr, MeasureRef):
        home = measure_home(model, expr.name)
        proj = {"field": {"Measure": {**entity_ref(home), "Property": expr.name}},
                "queryRef": f"{home}.{expr.name}", "nativeQueryRef": expr.name}
        if label != expr.name:
            proj["displayName"] = label
        return proj
    if isinstance(expr, Agg) and expr.func in AGG_CODES and expr.arg is not None:
        ref, prefix = AGG_LABEL[expr.func]
        col = expr.arg
        native = f"{prefix} {col.column}"
        proj = {"field": {"Aggregation": {"Expression": {"Column": {**entity_ref(col.table),
                                                                    "Property": col.column}},
                                          "Function": AGG_CODES[expr.func]}},
                "queryRef": f"{ref}({col.table}.{col.column})", "nativeQueryRef": native}
        if label != native:
            proj["displayName"] = label
        return proj
    if isinstance(expr, Agg) and expr.func == "count_distinct":
        raise unsupported("Count (Distinct) implicit aggregation",
                          "it counts BLANK as a value; use a DISTINCTCOUNTNOBLANK measure")
    raise unsupported("visual field that is not a measure or a column aggregation",
                      f"value {label!r}")


# ======================================================================
# Writing
# ======================================================================
def report_json() -> dict:
    return {
        "$schema": REPORT_SCHEMA,
        "themeCollection": {"baseTheme": {
            "name": THEME,
            "reportVersionAtImport": {"visual": "1.8.95", "report": "2.0.95", "page": "1.3.95"},
            "type": "SharedResources"}},
        "objects": {"section": [{"properties": {"verticalAlignment": lit("'Top'")}}]},
        "resourcePackages": [{"name": "SharedResources", "type": "SharedResources",
                              "items": [{"name": THEME, "path": f"BaseThemes/{THEME}.json",
                                         "type": "BaseTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True,
                     "exportDataMode": "AllowSummarized",
                     "defaultDrillFilterOtherVisuals": True,
                     "allowChangeFilterTypes": True,
                     "useEnhancedTooltips": True,
                     "useDefaultAggregateDisplayName": True},
    }


def alias_for(entity: str) -> str:
    """The From alias Desktop uses: the entity's first letter, lower-cased."""
    first = entity[:1].lower()
    return first if first.isalpha() else "t"


def filter_json(mod, name: str) -> dict:
    col = mod.column
    alias = alias_for(col.table)
    source = {"Column": {"Expression": {"SourceRef": {"Source": alias}}, "Property": col.column}}
    if isinstance(mod, FilterValues):
        if not mod.values:
            raise unsupported("empty filter")
        condition: dict = {"In": {"Expressions": [source],
                                  "Values": [[{"Literal": {"Value": literal(v)}}]
                                             for v in mod.values]}}
        if mod.negate:
            condition = {"Not": {"Expression": condition}}
        kind = "Categorical"
    elif isinstance(mod, FilterCompare):
        compare = {"Comparison": {"ComparisonKind": COMPARISON.get(mod.op, 0), "Left": source,
                                  "Right": {"Literal": {"Value": literal(mod.value)}}}}
        condition = {"Not": {"Expression": compare}} if mod.op == "<>" else compare
        kind = "Advanced"
    else:
        raise unsupported("visual filter modifier", type(mod).__name__)
    return {"name": name, "field": column_field(col), "type": kind,
            "filter": {"Version": 2, "From": [{"Name": alias, "Entity": col.table, "Type": 0}],
                       "Where": [{"Condition": condition}]},
            "howCreated": "User"}


def with_source(field: dict, alias_of: dict[str, str]) -> dict:
    """A field written against From aliases instead of entities."""
    if isinstance(field, dict):
        out = {}
        for key, value in field.items():
            if key == "SourceRef" and "Entity" in value:
                out[key] = {"Source": alias_of[value["Entity"]]}
            else:
                out[key] = with_source(value, alias_of)
        return out
    if isinstance(field, list):
        return [with_source(v, alias_of) for v in field]
    return field


def entities_in(field: Any, out: list[str]) -> list[str]:
    if isinstance(field, dict):
        for key, value in field.items():
            if key == "SourceRef" and "Entity" in value:
                if value["Entity"] not in out:
                    out.append(value["Entity"])
            else:
                entities_in(value, out)
    elif isinstance(field, list):
        for v in field:
            entities_in(v, out)
    return out


def top_n_filter(group: Col, order_field: dict, descending: bool, n: int, name: str) -> dict:
    entities = entities_in(column_field(group), [])
    entities = entities_in(order_field, entities)
    alias_of: dict[str, str] = {}
    for entity in entities:
        alias = alias_for(entity)
        while alias in alias_of.values() or alias == "subquery":
            alias += "1"
        alias_of[entity] = alias
    select = with_source(column_field(group), alias_of)
    select["Name"] = "field"
    subquery = {"Version": 2,
                "From": [{"Name": alias_of[e], "Entity": e, "Type": 0} for e in entities],
                "Select": [select],
                "OrderBy": [{"Direction": 2 if descending else 1,
                             "Expression": with_source(order_field, alias_of)}],
                "Top": n}
    outer = alias_of[group.table]
    return {"name": name, "field": column_field(group), "type": "TopN",
            "filter": {"Version": 2,
                       "From": [{"Name": "subquery", "Type": 2,
                                 "Expression": {"Subquery": {"Query": subquery}}},
                                {"Name": outer, "Entity": group.table, "Type": 0}],
                       "Where": [{"Condition": {"In": {
                           "Expressions": [{"Column": {"Expression": {"SourceRef":
                                                                     {"Source": outer}},
                                                       "Property": group.column}}],
                           "Table": {"SourceRef": {"Source": "subquery"}}}}}]},
            "howCreated": "User"}


def visual_json(model: Model, report: Report, page: Page, index: int, visual: Visual) -> dict:
    if visual.sql is not None:
        raise unsupported("SQL panel visual", f"visual {visual.name!r}")
    vid = short_id("visual", report.name, page.name, visual.name)
    kind = visual.kind
    if kind == "card":
        if visual.group_by:
            raise unsupported("card visual with grouping fields", f"visual {visual.name!r}")
        visual_type, group_role, value_role = (("card", None, "Values") if len(visual.values) == 1
                                               else ("cardVisual", None, "Data"))
    elif kind in KINDS:
        visual_type, group_role, value_role = KINDS[kind]
    else:
        raise unsupported(f"visual type {kind}", f"visual {visual.name!r}")
    labels: dict[str, dict] = {}
    groups = []
    for col in visual.group_by:
        if col.column in labels:
            raise ModelError(f"visual {visual.name!r} groups by two columns named "
                             f"{col.column!r}")
        proj = {"field": column_field(col), "queryRef": f"{col.table}.{col.column}",
                "nativeQueryRef": col.column}
        if group_role in ("Category", "Rows"):
            proj["active"] = True
        groups.append(proj)
        labels[col.column] = proj["field"]
    values = []
    for label, expr in visual.values:
        if label in labels:
            raise ModelError(f"visual {visual.name!r} has two fields labelled {label!r}")
        proj = value_field(model, label, expr)
        values.append(proj)
        labels[label] = proj["field"]
    state: dict[str, Any] = {}
    if group_role == value_role:
        state[group_role] = {"projections": groups + values}
    else:
        if groups:
            state[group_role] = {"projections": groups}
        state[value_role] = {"projections": values}
    query: dict[str, Any] = {"queryState": state}
    if visual.order_by:
        sort = []
        for label, descending in visual.order_by:
            if label not in labels:
                raise ModelError(f"visual {visual.name!r} sorts by {label!r}, which it does "
                                 f"not show")
            sort.append({"field": labels[label],
                         "direction": "Descending" if descending else "Ascending"})
        query["sortDefinition"] = {"sort": sort}
    elif visual.group_by:
        query["sortDefinition"] = {"sort": [{"field": column_field(c), "direction": "Ascending"}
                                            for c in visual.group_by],
                                   "isDefaultSort": True}
    title = "'" + visual.name.replace("'", "''") + "'"
    body: dict[str, Any] = {
        "$schema": VISUAL_SCHEMA,
        "name": vid,
        "position": layout(index),
        "visual": {"visualType": visual_type, "query": query,
                   "visualContainerObjects": {"title": [{"properties": {
                       "show": lit("true"), "text": lit(title)}}]},
                   "drillFilterOtherVisuals": True},
    }
    filters = []
    seen = set()
    for k, mod in enumerate(visual.filters):
        if mod.column in seen:
            raise unsupported("two filters on one field",
                              "Power BI intersects them; a Visual's filters replace")
        seen.add(mod.column)
        filters.append(filter_json(mod, short_id("filter", vid, str(k))))
    if visual.top_n is not None:
        if len(visual.group_by) != 1 or not visual.order_by:
            raise unsupported("Top N filter", "needs exactly one grouping field and a sort")
        label, descending = visual.order_by[0]
        filters.append(top_n_filter(visual.group_by[0], labels[label], descending,
                                    visual.top_n, short_id("filter", vid, "topn")))
    if filters:
        body["filterConfig"] = {"filters": filters}
    return body


def layout(index: int) -> dict:
    """A two-column grid on a 1280 x 720 page, in visual order."""
    column, row = index % 2, index // 2
    return {"x": 20 + column * 630, "y": 20 + row * 230, "z": index * 1000,
            "height": 210, "width": 610, "tabOrder": index * 1000}


def report_files(report: Report, model: Model, model_folder: str) -> dict[str, bytes]:
    """Every file of `<report.name>.Report/`, keyed by relative path."""
    if report.model != model.name:
        raise ModelError(f"report {report.name!r} is bound to {report.model!r}, not "
                         f"{model.name!r}")
    files = {
        ".platform": platform_json("Report", report.name),
        "definition.pbir": json_bytes({"$schema": PBIR_SCHEMA, "version": "4.0",
                                       "datasetReference": {"byPath": {
                                           "path": f"../{model_folder}"}}}),
        "definition/version.json": json_bytes({"$schema": VERSION_SCHEMA, "version": "2.0.0"}),
        "definition/report.json": json_bytes(report_json()),
        f"StaticResources/SharedResources/BaseThemes/{THEME}.json": THEME_FILE.read_bytes(),
    }
    order = []
    names = set()
    for page in report.pages:
        if page.name in names:
            raise ModelError(f"report {report.name!r} has two pages named {page.name!r}")
        names.add(page.name)
        pid = short_id("page", report.name, page.name)
        order.append(pid)
        rows = (len(page.visuals) + 1) // 2
        files[f"definition/pages/{pid}/page.json"] = json_bytes({
            "$schema": PAGE_SCHEMA, "name": pid, "displayName": page.name,
            "displayOption": "FitToPage", "height": max(720, 20 + rows * 230), "width": 1280})
        visual_names = set()
        for index, visual in enumerate(page.visuals):
            if visual.name in visual_names:
                raise ModelError(f"page {page.name!r} has two visuals named {visual.name!r}")
            visual_names.add(visual.name)
            body = visual_json(model, report, page, index, visual)
            files[f"definition/pages/{pid}/visuals/{body['name']}/visual.json"] = json_bytes(body)
    pages: dict[str, Any] = {"$schema": PAGES_SCHEMA, "pageOrder": order}
    if order:
        pages["activePageName"] = order[0]
    files["definition/pages/pages.json"] = json_bytes(pages)
    return files


# ======================================================================
# Reading
# ======================================================================
class ReportReader:
    def __init__(self, model: Model, info: SemanticModelInfo):
        self.model = model
        self.info = info
        self.tables = {t.name: t for t in model.tables}
        self.measures = {m.name: m for m in model.measures}

    def column(self, table: str, column: str) -> Col:
        t = self.tables.get(table)
        if t is None or column not in {c.name for c in t.columns}:
            raise ModelError(f"the report refers to {table}[{column}], which is not in the "
                             f"semantic model")
        return Col(table, column)

    def data_type(self, col: Col) -> str:
        for c in self.tables[col.table].columns:
            if c.name == col.column:
                return c.data_type
        return "string"    # pragma: no cover

    def field_expr(self, field: dict, where: str) -> Expr | Col:
        if "Column" in field:
            col = field["Column"]
            return self.column(entity_of(col), col["Property"])
        if "Measure" in field:
            m = field["Measure"]
            name, entity = m["Property"], entity_of(m)
            measure = self.measures.get(name)
            if measure is None or measure.table != entity:
                raise ModelError(f"{where}: measure {entity}[{name}] is not in the semantic "
                                 f"model")
            return MeasureRef(name)
        if "Aggregation" in field:
            agg = field["Aggregation"]
            inner = agg["Expression"]
            if "Column" not in inner:
                raise unsupported("aggregation over anything but a column", where)
            col = self.column(entity_of(inner["Column"]), inner["Column"]["Property"])
            code = agg["Function"]
            if code == 2:
                raise unsupported("Count (Distinct) implicit aggregation",
                                  "it counts BLANK as a value")
            if code not in AGG_FROM_CODE:
                raise unsupported(f"implicit aggregation function {code}", where)
            return Agg(AGG_FROM_CODE[code], col)
        if "HierarchyLevel" in field:
            raise unsupported("hierarchy level field", where)
        raise unsupported(f"field kind {next(iter(field), '?')}", where)

    def filters(self, config: dict | None, where: str) -> tuple[list, list]:
        """filterConfig -> (FilterValues/FilterCompare list, TopN filters)."""
        mods, tops = [], []
        for flt in (config or {}).get("filters", []):
            if "filter" not in flt:
                continue                      # a field in the pane with nothing selected
            kind = flt.get("type", "Categorical")
            if "Measure" in flt.get("field", {}):
                raise unsupported("filter on a measure value", where)
            if kind == "TopN":
                tops.append(flt)
                continue
            if kind not in ("Categorical", "Advanced", "Include", "Exclude"):
                raise unsupported(f"{kind} filter", where)
            definition = flt["filter"]
            alias = {s["Name"]: s.get("Entity") for s in definition.get("From", [])}
            for clause in definition.get("Where", []):
                mods.append(self.condition(clause["Condition"], alias, where))
        return mods, tops

    def condition(self, cond: dict, alias: dict, where: str):
        negate = False
        if "Not" in cond:
            cond, negate = cond["Not"]["Expression"], True
        if "In" in cond:
            spec = cond["In"]
            if "Table" in spec or len(spec.get("Expressions", [])) != 1:
                raise unsupported("tuple filter", where)
            col = self.source_column(spec["Expressions"][0], alias, where)
            dtype = self.data_type(col)
            values = []
            for row in spec.get("Values", []):
                if len(row) != 1 or "Literal" not in row[0]:
                    raise unsupported("filter value that is not a literal", where)
                values.append(parse_literal(row[0]["Literal"]["Value"], dtype))
            return FilterValues(col, tuple(values), negate=negate)
        if "Comparison" in cond:
            spec = cond["Comparison"]
            col = self.source_column(spec["Left"], alias, where)
            if "Literal" not in spec["Right"]:
                raise unsupported("filter value that is not a literal", where)
            value = parse_literal(spec["Right"]["Literal"]["Value"], self.data_type(col))
            op = COMPARISON_FROM.get(spec["ComparisonKind"])
            if op is None:
                raise unsupported(f"comparison kind {spec['ComparisonKind']}", where)
            if negate:
                if op != "=":
                    raise unsupported("negated comparison filter", where)
                op = "<>"
            return FilterCompare(col, op, value)
        if "And" in cond or "Or" in cond:
            raise unsupported("advanced filter with two conditions", where)
        raise unsupported(f"filter condition {next(iter(cond), '?')}", where)

    def source_column(self, expr: dict, alias: dict, where: str) -> Col:
        if "Column" not in expr:
            raise unsupported("filter on anything but a column", where)
        col = expr["Column"]
        ref = col["Expression"]["SourceRef"]
        entity = ref.get("Entity") or alias.get(ref.get("Source"))
        if entity is None:
            raise ModelError(f"{where}: filter source {ref} is not declared in From")
        return self.column(entity, col["Property"])

    def visual(self, body: dict, inherited: list, where: str) -> Visual | None:
        config = body.get("visual")
        if config is None:
            if "visualGroup" in body:
                return None
            raise ModelError(f"{where}: visual.json has neither visual nor visualGroup")
        visual_type = config.get("visualType", "")
        if visual_type in DECORATIVE or "query" not in config:
            return None
        if visual_type == "slicer":
            general = config.get("objects", {}).get("general", [])
            if any("filter" in g.get("properties", {}) for g in general):
                raise unsupported("slicer selection", where)
            return None
        name = title_of(config) or body.get("name") or where
        state = config["query"].get("queryState", {})
        groups: list[tuple[int, int, Col, dict]] = []
        values: list[tuple[int, int, str, Expr, dict]] = []
        for role, spec in state.items():
            if role in SKIPPED_ROLES:
                continue
            for k, proj in enumerate(spec.get("projections", [])):
                field = proj["field"]
                expr = self.field_expr(field, where)
                if isinstance(expr, Col):
                    if proj.get("active") is False:
                        continue
                    rank = GROUP_ROLES.index(role) if role in GROUP_ROLES else len(GROUP_ROLES)
                    groups.append((rank, k, expr, normalise(field)))
                else:
                    rank = VALUE_ROLES.index(role) if role in VALUE_ROLES else len(VALUE_ROLES)
                    label = proj.get("displayName") or proj.get("nativeQueryRef") or (
                        field["Measure"]["Property"] if "Measure" in field else proj["queryRef"])
                    values.append((rank, k, label, expr, normalise(field)))
        groups.sort(key=lambda g: (g[0], g[1]))
        values.sort(key=lambda v: (v[0], v[1]))
        group_by = tuple(g[2] for g in groups)
        labelled: dict[str, Any] = {}
        for g in groups:
            if g[2].column in labelled:
                raise ModelError(f"{where}: two grouping fields named {g[2].column!r}")
            labelled[g[2].column] = g[3]
        for v in values:
            if v[2] in labelled:
                raise ModelError(f"{where}: two fields labelled {v[2]!r}")
            labelled[v[2]] = v[4]
        label_of = {json.dumps(f, sort_keys=True): label for label, f in labelled.items()}

        mods, tops = self.filters(body.get("filterConfig"), where)
        mods = inherited + mods
        seen = set()
        for mod in mods:
            if mod.column in seen:
                raise unsupported("two filters on one field",
                                  "Power BI intersects them; a Visual's filters replace")
            seen.add(mod.column)

        sort_def = config["query"].get("sortDefinition")
        sort_entries = (sort_def or {}).get("sort", [])
        order_by: list[tuple[str, bool]] = []
        for entry in sort_entries:
            key = json.dumps(normalise(entry["field"]), sort_keys=True)
            if key not in label_of:
                raise unsupported("sort by a field the visual does not show", where)
            order_by.append((label_of[key], entry["direction"] == "Descending"))
        if not sort_entries and group_by and visual_type not in DEFAULT_SORT_IS_GROUPS:
            raise unsupported(f"default sort of a {visual_type} visual", where)
        if order_by == [(c.column, False) for c in group_by]:
            order_by = []
        sorted_columns = [c for c in group_by if (c.table, c.column) in self.info.sort_by
                          and (not order_by or any(lbl == c.column for lbl, _ in order_by))]
        if sorted_columns:
            raise unsupported("sortByColumn", f"{where}: {sorted_columns[0].table}"
                                              f"[{sorted_columns[0].column}] sorts by another "
                                              f"column")
        top_n = None
        if tops:
            if len(tops) > 1:
                raise unsupported("more than one Top N filter", where)
            top_n, top_label, top_desc = self.top_n(tops[0], group_by, label_of, where)
            if not order_by or order_by[0] != (top_label, top_desc):
                raise unsupported("Top N filter ordered differently from the visual's sort",
                                  where)
        kind = KIND_OF.get(visual_type, visual_type)
        return Visual(name, group_by=group_by, values=tuple((v[2], v[3]) for v in values),
                      filters=tuple(mods), order_by=tuple(order_by), top_n=top_n, kind=kind)

    def top_n(self, flt: dict, group_by, label_of, where) -> tuple[int, str, bool]:
        definition = flt["filter"]
        subquery = None
        for source in definition.get("From", []):
            if source.get("Type") == 2:
                subquery = source["Expression"]["Subquery"]["Query"]
        if subquery is None or "Top" not in subquery or not subquery.get("OrderBy"):
            raise unsupported("Top N filter without a ranked subquery", where)
        alias = {s["Name"]: s.get("Entity") for s in subquery.get("From", [])}
        column = self.source_column(subquery["Select"][0], alias, where)
        if tuple(group_by) != (column,):
            raise unsupported("Top N filter on a field other than the visual's only grouping",
                              where)
        order = subquery["OrderBy"][0]
        key = json.dumps(normalise(order["Expression"], alias), sort_keys=True)
        if key not in label_of:
            raise unsupported("Top N filter ranked by a field the visual does not show", where)
        return int(subquery["Top"]), label_of[key], order["Direction"] == 2

    def report(self, files: dict[str, bytes], name: str) -> Report:
        report_doc = json_document(files["definition/report.json"], "definition/report.json") \
            if "definition/report.json" in files else {}
        report_mods, report_tops = self.filters(report_doc.get("filterConfig"), "report.json")
        if report_tops:
            raise unsupported("report-level Top N filter")
        pages_meta = json_document(files["definition/pages/pages.json"], "pages.json") \
            if "definition/pages/pages.json" in files else {}
        page_docs = {}
        for path in files:
            parts = path.split("/")
            if len(parts) == 4 and parts[:2] == ["definition", "pages"] and parts[3] == "page.json":
                page_docs[parts[2]] = json_document(files[path], path)
        order = [p for p in pages_meta.get("pageOrder", []) if p in page_docs]
        order += sorted((p for p in page_docs if p not in order),
                        key=lambda p: page_docs[p].get("displayName", p))
        pages = []
        for pid in order:
            doc = page_docs[pid]
            where = f"pages/{pid}"
            page_mods, page_tops = self.filters(doc.get("filterConfig"), where)
            if page_tops:
                raise unsupported("page-level Top N filter", where)
            prefix = f"definition/pages/{pid}/visuals/"
            bodies = []
            for path in sorted(files):
                if path.startswith(prefix) and path.endswith("/visual.json") \
                        and path.count("/") == 5:
                    bodies.append((path, json_document(files[path], path)))
            bodies.sort(key=lambda b: visual_order(b[1]))
            visuals = []
            for path, body in bodies:
                visual = self.visual(body, report_mods + page_mods, path)
                if visual is not None:
                    visuals.append(visual)
            names = [v.name for v in visuals]
            if len(set(names)) != len(names):
                raise ModelError(f"{where}: two visuals share the title "
                                 f"{next(n for n in names if names.count(n) > 1)!r}")
            pages.append(Page(doc.get("displayName", pid), tuple(visuals)))
        return Report(name, self.model.name, tuple(pages))


def entity_of(node: dict) -> str:
    ref = node["Expression"]["SourceRef"]
    if "Entity" not in ref:
        raise ModelError(f"field reference {ref} has no Entity")
    return ref["Entity"]


def normalise(field: Any, alias: dict | None = None) -> Any:
    """A field with Source aliases resolved to entities and display-only keys
    dropped, so a sort entry, a projection and a Top N order compare equal."""
    if isinstance(field, dict):
        out = {}
        for key, value in field.items():
            if key in ("Name", "NativeReferenceName", "Annotations"):
                continue
            if key == "SourceRef" and "Source" in value and alias is not None:
                out[key] = {"Entity": alias.get(value["Source"], value["Source"])}
            else:
                out[key] = normalise(value, alias)
        return out
    if isinstance(field, list):
        return [normalise(v, alias) for v in field]
    return field


def title_of(config: dict) -> str | None:
    for entry in config.get("visualContainerObjects", {}).get("title", []):
        text = entry.get("properties", {}).get("text", {}).get("expr", {}).get("Literal", {})
        value = text.get("Value")
        if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == "'":
            return value[1:-1].replace("''", "'")
    return None


def visual_order(body: dict) -> tuple:
    pos = body.get("position", {})
    tab = pos.get("tabOrder")
    return (tab if tab is not None else float("inf"), pos.get("z", 0), pos.get("y", 0),
            pos.get("x", 0), body.get("name", ""))


def read_report(files: dict[str, bytes], model: Model, info: SemanticModelInfo,
                fallback_name: str) -> Report:
    name = fallback_name
    if ".platform" in files:
        platform = json_document(files[".platform"], ".platform")
        name = platform.get("metadata", {}).get("displayName") or name
    return ReportReader(model, info).report(files, name)


def dataset_path(files: dict[str, bytes]) -> str:
    """definition.pbir's byPath target (relative to the report folder)."""
    if "definition.pbir" not in files:
        raise ModelError("Cannot read 'definition.pbir': the file is missing")
    doc = json_document(files["definition.pbir"], "definition.pbir")
    ref = doc.get("datasetReference") or {}
    if ref.get("byConnection"):
        raise unsupported("live connection report (byConnection)",
                          "the report is bound to a published semantic model, not the "
                          "project's")
    path = (ref.get("byPath") or {}).get("path")
    if not path:
        raise ModelError("Cannot read 'definition.pbir'. Either byPath or byConnection must "
                         "be populated in 'definition.pbir'.")
    return path


__all__ = ["dataset_path", "literal", "parse_literal", "read_report", "report_files"]
