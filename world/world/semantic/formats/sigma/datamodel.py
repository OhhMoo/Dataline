"""A Sigma data model's code representation <-> the semantic Model.

The representation is what `GET /v2/dataModels/{dataModelId}/spec` returns and
`PUT /v2/dataModels/{dataModelId}/spec` accepts (GA; Sigma Computing Public
REST API 2.0.0+20260930, schema `DataModelSpec`; examples in
help.sigmacomputing.com/docs/data-model-representation-example-library):

    {dataModelId, name, url, documentVersion, latestDocumentVersion, ownerId,
     folderId, createdBy, updatedBy, createdAt, updatedAt, description?,
     schemaVersion: 1,
     pages: [{id, name, elements: [
        {id, kind: "table", name?, description?, visibleAsSource?,
         source: {connectionId, kind: "warehouse-table", path: [DB, SCHEMA, TABLE]}
               | {connectionId, kind: "sql", statement}
               | {kind: "join", joins: [{left, right, columns: [{left, right, op?}],
                                         joinType?, name?}], primarySource?},
         columns: [{id, formula, name?, description?, hidden?, format?}],
         order: [column ids],
         metrics: [{id, formula, name?, description?, format?}],
         relationships: [{id, targetElementId, keys: [{sourceColumnId,
                          targetColumnId}], name?, description?}]}]}]}

MAPPING (element = Table, metric = Measure, relationship = Relationship):

- A column whose formula is `[<SOURCE>/<Display Name>]` reads the source. For a
  warehouse table SOURCE is the table name and the warehouse column is the
  suffix of an `inode-.../COLUMN` id, else the column whose Sigma title is the
  display name. For custom SQL SOURCE is "Custom SQL" and the name is the raw
  output column. Any other formula is a calculated column, read in the
  element's calculated-column scope (formula.py), its type inferred.
- A relationship is declared on its SOURCE (many side) element and points at a
  TARGET (one side): Sigma "only support[s] many-to-one or one-to-one joins"
  and carries no cardinality (docs/define-relationships-in-data-models), so
  every relationship reads as many_to_one, single direction, active; writing
  anything else is refused. Single-key only: the model's Relationship has one
  column pair.
- A join element (`source.kind = "join"`) becomes a Table over SQL that
  performs exactly that join - INNER/LEFT/RIGHT/FULL with `=` or
  `is-not-distinct-from` keys - so a fan-out the join causes is kept, as
  Sigma keeps it (a join "is performed in the underlying SQL every time").
  Writing such a table produces a custom SQL element: the join is lowered,
  not re-derived.
- Element filters, groupings, child (`kind: "table"`) and every other source
  kind, controls, and multi-key relationships are refused by name.

FORMAT STRINGS. Measure.format_string uses the spreadsheet syntax the other
front-ends share ("$#,0.00", "0.0%"); Sigma formats with d3-format strings
plus prefix/suffix/currency keys. The subset `[prefix]#,0|0[.0..][%][suffix]`
<-> `[$][,].Nf|%` converts exactly both ways; anything else is refused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...errors import ModelError, Unsupported
from ...expr import Expr
from ...model import Column, Measure, Model, Relationship, Table
from ._common import (
    CONNECTION_ID,
    CUSTOM_SQL,
    EPOCH,
    FOLDER_ID,
    OWNER_ID,
    SCHEMA_VERSION,
    base62,
    display_name,
    document_url,
    inode_column_id,
    source_of_path,
    stable_uuid,
    table_path,
    warehouse_name,
)
from .catalog import Catalog
from .formula import (
    FormatScope,
    Scope,
    default_relationship_names,
    format_formula,
    format_row_formula,
    infer_type,
    lower,
    parse_ast,
    unsupported,
)

PAGE_NAME = "Page 1"
_SOURCE_REF = re.compile(r"^\[([^\[\]/]+)/([^\[\]/]+)\]$")


# ----------------------------------------------------------- number formats
_EXCEL = re.compile(r"^(?P<pre>[^#0.%]*)(?P<core>#,0|0)(?:\.(?P<dec>0+))?(?P<pct>%)?"
                    r"(?P<suf>[^#0.%]*)$")
_D3 = re.compile(r"^(?P<cur>\$)?(?P<group>,)?\.(?P<dec>\d+)(?P<type>[f%])$")
_LOCALE_DEFAULTS = {"decimalSymbol": ".", "digitGroupingSymbol": ",", "digitGroupingSize": [3]}


def sigma_format(format_string: str | None) -> dict | None:
    if format_string is None:
        return None
    m = _EXCEL.match(format_string)
    if not m:
        raise unsupported(f"format string {format_string!r}",
                          "only [prefix]#,0|0[.00][%][suffix] converts exactly to d3")
    decimals = len(m.group("dec") or "")
    spec = ("," if m.group("core") == "#,0" else "") + f".{decimals}" + \
        ("%" if m.group("pct") else "f")
    out: dict = {"kind": "number"}
    pre, suf = m.group("pre"), m.group("suf")
    if pre == "$":
        out["formatString"] = "$" + spec
        out["currencySymbol"] = "$"
    else:
        out["formatString"] = spec
        if pre:
            out["prefix"] = pre
    if suf:
        out["suffix"] = suf
    return out


def model_format(fmt: dict | None) -> str | None:
    if not fmt:
        return None
    extra = set(fmt) - {"kind", "formatString", "prefix", "suffix", "currencySymbol",
                        *_LOCALE_DEFAULTS}
    for key, default in _LOCALE_DEFAULTS.items():
        if key in fmt and fmt[key] != default:
            extra.add(key)
    if fmt.get("kind") != "number" or extra:
        raise unsupported(f"format {fmt!r}", "only number formats with a d3 .Nf/.N% "
                                             "spec, prefix, suffix and $ convert exactly")
    m = _D3.match(fmt.get("formatString", ""))
    if not m:
        raise unsupported(f"d3 format {fmt.get('formatString')!r}")
    prefix = fmt.get("prefix", "")
    if m.group("cur"):
        if prefix:
            raise unsupported("currency format with a prefix")
        prefix = fmt.get("currencySymbol", "$")
    elif "currencySymbol" in fmt:
        raise unsupported("currencySymbol without the $ flag")
    decimals = int(m.group("dec"))
    core = ("#,0" if m.group("group") else "0") + ("." + "0" * decimals if decimals else "")
    return prefix + core + ("%" if m.group("type") == "%" else "") + fmt.get("suffix", "")


# ---------------------------------------------------------------- reading
@dataclass
class DataModelIndex:
    """What a workbook needs to resolve references into a data model."""
    data_model_id: str
    name: str
    element_table: dict[str, str] = field(default_factory=dict)       # element id -> table
    table_element: dict[str, str] = field(default_factory=dict)       # table -> element id
    relationship_names: dict[tuple[str, str], Relationship] = field(default_factory=dict)
    metrics: dict[str, set[str]] = field(default_factory=dict)        # table -> metric names


def _title(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return None


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "") or ""
    return ""


_IGNORED_KINDS = {"text", "image", "divider", "container", "tabbed-container", "empty"}


@dataclass
class _Pending:
    """A table being read: warehouse columns are known, calculated ones wait."""
    table_name: str
    element: dict
    source: str | None
    source_sql: str | None
    columns: list = field(default_factory=list)      # (id, Column | raw dict for calc)


def read_data_model(doc: dict, catalog: Catalog) -> tuple[Model, DataModelIndex]:
    if doc.get("schemaVersion", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise unsupported(f"schemaVersion {doc.get('schemaVersion')}")
    index = DataModelIndex(doc.get("dataModelId", ""), doc.get("name", ""))
    if not index.name:
        raise ModelError("data model has no name")
    elements: list[dict] = []
    for page in doc.get("pages", []):
        for el in page.get("elements", []):
            kind = el.get("kind")
            if kind == "table":
                elements.append(el)
            elif kind in _IGNORED_KINDS:
                continue
            elif kind == "control":
                raise unsupported("control element in a data model",
                                  "a control filters elements; it is not modelled")
            else:
                raise unsupported(f"{kind} element in a data model")
    by_id = {el["id"]: el for el in elements}
    names = {el["id"]: _element_name(el, by_id) for el in elements}
    if len(set(names.values())) != len(names):
        raise ModelError("two data model elements share a name "
                         "(Sigma does not support managing them in code either)")
    for el_id, name in names.items():
        index.element_table[el_id] = name
        index.table_element[name] = el_id

    pending = [_read_columns(el, names[el["id"]], by_id, names, catalog) for el in elements]
    column_name_by_id: dict[str, tuple[str, str]] = {}
    for p in pending:
        for col_id, col in p.columns:
            name = col.name if isinstance(col, Column) else col["name"]
            column_name_by_id[col_id] = (p.table_name, name)

    relationships: list[Relationship] = []
    for p in pending:
        for rel in p.element.get("relationships", []) or []:
            target = names.get(rel.get("targetElementId"))
            if target is None:
                raise ModelError(f"relationship {rel.get('id')} targets an unknown element")
            keys = rel.get("keys", [])
            if len(keys) != 1:
                raise unsupported("relationship with several keys" if keys else "relationship "
                                  "without keys", f"{p.table_name} -> {target}")
            src = column_name_by_id.get(keys[0]["sourceColumnId"])
            dst = column_name_by_id.get(keys[0]["targetColumnId"])
            if src is None or src[0] != p.table_name or dst is None or dst[0] != target:
                raise ModelError(f"relationship {p.table_name} -> {target}: a key column "
                                 f"is not a column of its element")
            r = Relationship(p.table_name, src[1], target, dst[1])
            name = rel.get("name") or target
            if (p.table_name, name) in index.relationship_names:
                raise ModelError(f"{p.table_name} has two relationships named {name!r}")
            index.relationship_names[(p.table_name, name)] = r
            relationships.append(r)

    tables = _resolve_calculated(pending, relationships, index, doc.get("name"))

    measures: list[Measure] = []
    seen_measures: set[str] = set()
    for p in pending:
        for metric in p.element.get("metrics", []) or []:
            name = metric.get("name")
            if not name:
                raise ModelError(f"a metric on {p.table_name} has no name")
            if name in seen_measures:
                raise ModelError(f"two metrics are named {name!r} (measure names are unique "
                                 f"across the model)")
            seen_measures.add(name)
            index.metrics.setdefault(p.table_name, set()).add(name)
    # Metrics may refer to each other (`[Metrics/Net Revenue]`), only on their
    # own element (docs/create-and-manage-metrics); the names are enough to
    # resolve a reference, so the metrics are read against the tables alone.
    columns_only = Model(index.name, tables, tuple(relationships))
    for p in pending:
        mine = frozenset(index.metrics.get(p.table_name, set()))
        for metric in p.element.get("metrics", []) or []:
            scope = Scope(columns_only, "metric", p.table_name, index.relationship_names,
                          metric_names=mine)
            expr = lower(parse_ast(metric["formula"]), scope, "measure")
            measures.append(Measure(metric["name"], p.table_name, expr,
                                    format_string=model_format(metric.get("format")),
                                    description=metric.get("description", "") or ""))
    model = Model(index.name, tables, tuple(relationships), tuple(measures),
                  null_semantics="sql", description=doc.get("description", "") or "")
    return model, index


def _element_name(el: dict, by_id: dict) -> str:
    name = _title(el.get("name"))
    if name:
        return name
    source = el.get("source", {})
    kind = source.get("kind")
    if kind == "warehouse-table":
        return source["path"][-1]
    if kind == "sql":
        return CUSTOM_SQL
    if kind == "join":
        primary = source.get("primarySource") or _primary(source)
        return source.get("name") or _source_title(primary, by_id)
    raise unsupported(f"{kind} source in a data model element")


def _source_title(src: dict, by_id: dict) -> str:
    if src.get("kind") == "warehouse-table":
        return src["path"][-1]
    if src.get("kind") == "table":
        target = by_id.get(src.get("elementId"))
        if target is None:
            raise ModelError(f"join input {src.get('elementId')} is not an element")
        return _element_name(target, by_id)
    raise unsupported(f"{src.get('kind')} join input")


def _primary(source: dict) -> dict:
    lefts = [j["left"] for j in source["joins"]]
    rights = [j["right"] for j in source["joins"]]
    heads = [s for s in lefts if s not in rights]
    unique = []
    for s in heads:
        if s not in unique:
            unique.append(s)
    if len(unique) != 1:
        raise ModelError("join has no unique primary source")
    return unique[0]


def _read_columns(el: dict, table_name: str, by_id: dict, names: dict,
                  catalog: Catalog) -> _Pending:
    source = el.get("source") or {}
    kind = source.get("kind")
    for key in ("filters", "groupings"):
        if el.get(key):
            raise unsupported(f"{'element filter' if key == 'filters' else 'grouping'} in a "
                              f"data model element", table_name)
    if kind == "warehouse-table":
        p = _Pending(table_name, el, source_of_path(source["path"]), None)
        _warehouse_columns(p, source["path"], catalog)
    elif kind == "sql":
        p = _Pending(table_name, el, None, source["statement"])
        _sql_columns(p, catalog)
    elif kind == "join":
        sql, outputs = _join_sql(el, source, by_id, names, catalog)
        p = _Pending(table_name, el, None, sql)
        for col in el.get("columns", []):
            col_id = col.get("id") or base62("column", table_name, col["formula"])
            m = _SOURCE_REF.match(col["formula"].strip())
            if m and (m.group(1), m.group(2)) in outputs:
                raw_type = outputs[(m.group(1), m.group(2))]
                name = col.get("name") or m.group(2)
                p.columns.append((col_id, _column(col, name, name, raw_type)))
            else:
                p.columns.append((col_id, _calc(col, table_name)))
    else:
        raise unsupported(f"{kind} source in a data model element", table_name)
    return p


def _column(col: dict, name: str, source: str, dtype: str) -> Column:
    return Column(name=name, source=source, data_type=dtype, hidden=bool(col.get("hidden")),
                  description=col.get("description", "") or "")


def _calc(col: dict, table_name: str) -> dict:
    if not col.get("name"):
        raise ModelError(f"a calculated column on {table_name} has no name "
                         f"({col.get('formula')!r})")
    return col


def _warehouse_columns(p: _Pending, path: list, catalog: Catalog) -> None:
    head = path[-1]
    known = catalog.tables.get(p.source, {})
    by_display = {display_name(raw.upper()): raw for raw in known}
    for col in p.element.get("columns", []):
        col_id = col.get("id") or base62("column", p.table_name, col["formula"])
        m = _SOURCE_REF.match(col["formula"].strip())
        if m and m.group(1) == head:
            display = m.group(2)
            if col_id.startswith("inode-") and "/" in col_id:
                raw = col_id.split("/", 1)[1].lower()
            else:
                raw = by_display.get(display) or warehouse_name(display)
            dtype = catalog.column_type(p.source, raw)
            p.columns.append((col_id, _column(col, col.get("name") or display, raw, dtype)))
        else:
            p.columns.append((col_id, _calc(col, p.table_name)))


def _sql_columns(p: _Pending, catalog: Catalog) -> None:
    types = catalog.statement_types(p.source_sql)
    folded = {k.lower(): (k, v) for k, v in types.items()}
    for col in p.element.get("columns", []):
        col_id = col.get("id") or base62("column", p.table_name, col["formula"])
        m = _SOURCE_REF.match(col["formula"].strip())
        if m and m.group(1) == CUSTOM_SQL:
            raw = m.group(2)
            if raw in types:
                dtype = types[raw]
            elif raw.lower() in folded:
                raw, dtype = folded[raw.lower()]
            else:
                raise ModelError(f"custom SQL on {p.table_name} has no output column {raw!r}")
            p.columns.append((col_id, _column(col, col.get("name") or raw, raw, dtype)))
        else:
            p.columns.append((col_id, _calc(col, p.table_name)))


_JOIN_TYPES = {"inner": "INNER JOIN", "left-outer": "LEFT JOIN", "right-outer": "RIGHT JOIN",
               "full-outer": "FULL OUTER JOIN"}
_JOIN_OPS = {"=": "=", "is-not-distinct-from": "IS NOT DISTINCT FROM"}


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _join_sql(el: dict, source: dict, by_id: dict, names: dict, catalog: Catalog):
    """SQL for a join element, and {(input name, column title): type} it outputs."""
    primary = source.get("primarySource") or _primary(source)
    inputs: list[tuple[str, dict]] = [(_source_title(primary, by_id), primary)]
    for j in source["joins"]:
        title = j.get("name") or _source_title(j["right"], by_id)
        inputs.append((title, j["right"]))
    if len({t for t, _ in inputs}) != len(inputs):
        raise ModelError("two join inputs share a name; name the joins")
    aliases: dict[str, str] = {}
    relations: dict[str, str] = {}
    columns: dict[str, dict[str, tuple[str, str]]] = {}     # input -> title -> (raw, type)
    for i, (title, src) in enumerate(inputs):
        aliases[title] = f"t{i}"
        relations[title], columns[title] = _join_input(src, by_id, names, catalog)

    def input_of(src: dict) -> str:
        for title, s in inputs:
            if s == src:
                return title
        raise ModelError("a join's left side is neither the primary source nor an "
                         "earlier join's right side")

    def key(input_name: str, text: str) -> str:
        m = re.match(r"^\[([^\[\]/]+)\]$", text.strip())
        if not m:
            raise unsupported("join key that is a formula", text)
        found = columns[input_name].get(m.group(1))
        if found is None:
            raise ModelError(f"join input {input_name} has no column {m.group(1)!r}")
        return f"{aliases[input_name]}.{_quote_ident(found[0])}"

    first = inputs[0][0]
    sql = [f"FROM {relations[first]} AS {aliases[first]}"]
    for j, (title, _src) in zip(source["joins"], inputs[1:], strict=True):
        jt = j.get("joinType", "inner")
        if jt not in _JOIN_TYPES:
            raise unsupported(f"{jt} join")
        left = input_of(j["left"])
        conds = []
        for pair in j.get("columns", []):
            op = pair.get("op", "=")
            if op not in _JOIN_OPS:
                raise unsupported(f"join operator {op}")
            conds.append(f"{key(left, pair['left'])} {_JOIN_OPS[op]} {key(title, pair['right'])}")
        if not conds:
            raise ModelError("a join has no key columns")
        sql.append(f"{_JOIN_TYPES[jt]} {relations[title]} AS {aliases[title]} ON "
                   + " AND ".join(conds))

    outputs: dict[tuple[str, str], str] = {}
    select = []
    for col in el.get("columns", []):
        m = _SOURCE_REF.match(col["formula"].strip())
        if not m or m.group(1) not in columns:
            continue
        found = columns[m.group(1)].get(m.group(2))
        if found is None:
            raise ModelError(f"join input {m.group(1)} has no column {m.group(2)!r}")
        alias = col.get("name") or m.group(2)
        select.append(f"{aliases[m.group(1)]}.{_quote_ident(found[0])} AS {_quote_ident(alias)}")
        outputs[(m.group(1), m.group(2))] = found[1]
    if not select:
        raise ModelError("a join element selects no columns")
    return "SELECT " + ", ".join(select) + "\n" + "\n".join(sql), outputs


def _join_input(src: dict, by_id: dict, names: dict, catalog: Catalog):
    kind = src.get("kind")
    if kind == "warehouse-table":
        relation = source_of_path(src["path"])
        known = catalog.tables.get(relation)
        if known is None:
            raise ModelError(f"table {relation} is not in the connection's catalog")
        return relation, {display_name(raw.upper()): (raw, t) for raw, t in known.items()}
    if kind == "table":
        target = by_id.get(src.get("elementId"))
        if target is None:
            raise ModelError(f"join input {src.get('elementId')} is not an element")
        tsrc = target.get("source", {})
        p = _read_columns(target, names[target["id"]], by_id, names, catalog)
        relation = p.source if tsrc.get("kind") == "warehouse-table" else f"({p.source_sql})"
        out = {}
        for _cid, col in p.columns:
            if isinstance(col, Column):
                out[col.name] = (col.source, col.data_type)
        return relation, out
    raise unsupported(f"{kind} join input")


def _resolve_calculated(pending: list[_Pending], relationships, index, name) -> tuple:
    """Type and lower the calculated columns. Their types depend on each other
    (and on Lookup targets), so iterate to a fixed point from a neutral guess."""
    guess: dict[tuple[str, str], str] = {}
    for p in pending:
        for _cid, col in p.columns:
            if not isinstance(col, Column):
                guess[(p.table_name, col["name"])] = "decimal"

    def build(types) -> tuple[Table, ...]:
        tables = []
        for p in pending:
            cols = []
            for _cid, col in p.columns:
                if isinstance(col, Column):
                    cols.append(col)
                else:
                    cols.append(Column(col["name"], col["name"], types[(p.table_name,
                                                                        col["name"])],
                                       hidden=bool(col.get("hidden")),
                                       description=col.get("description", "") or ""))
            tables.append(_table(p, tuple(cols)))
        return tuple(tables)

    for _round in range(len(guess) + 2):
        tables = build(guess)
        model = Model(name, tables, tuple(relationships))
        new = dict(guess)
        exprs: dict[tuple[str, str], Expr] = {}
        for p in pending:
            for _cid, col in p.columns:
                if isinstance(col, Column):
                    continue
                scope = Scope(model, "column", p.table_name, index.relationship_names)
                expr = lower(parse_ast(col["formula"]), scope, "row")
                exprs[(p.table_name, col["name"])] = expr
                t = infer_type(expr, model)
                if t is None:
                    raise ModelError(f"cannot tell the type of calculated column "
                                     f"{p.table_name}.{col['name']}")
                new[(p.table_name, col["name"])] = t
        if new == guess:
            final = []
            for table, p in zip(tables, pending, strict=True):
                cols = tuple(
                    Column(c.name, c.source, c.data_type, c.hidden,
                           exprs.get((p.table_name, c.name)) if (p.table_name, c.name) in exprs
                           else None, c.description)
                    for c in table.columns)
                final.append(Table(table.name, table.source, cols, table.source_sql,
                                   hidden=table.hidden, description=table.description))
            return tuple(final)
        guess = new
    raise ModelError("calculated column types do not settle (a reference cycle?)")


def _table(p: _Pending, columns: tuple[Column, ...]) -> Table:
    el = p.element
    names = [c.name for c in columns]
    if len(set(names)) != len(names):
        raise ModelError(f"element {p.table_name} has two columns with the same name")
    return Table(p.table_name, p.source, columns, p.source_sql,
                 hidden=el.get("visibleAsSource") is False,
                 description=_text(el.get("description")))


# ---------------------------------------------------------------- writing
def data_model_id(model: Model) -> str:
    return stable_uuid("data-model", model.name)


def element_id(model: Model, table: str) -> str:
    return base62("element", model.name, table)


def column_id(model: Model, table: Table, column: Column) -> str:
    if column.expr is None and table.source is not None:
        schema, relation = table.source.split(".")
        return inode_column_id(schema, relation, column.source)
    return base62("column", model.name, table.name, column.name)


def check_model(model: Model) -> None:
    """Refuse what a Sigma data model cannot say, before writing anything."""
    if model.null_semantics != "sql":
        raise Unsupported("DAX blank arithmetic (null_semantics='dax')", "Sigma",
                          "Sigma arithmetic propagates Null like SQL")
    for t in model.tables:
        if t.is_date_table:
            raise Unsupported("marked date table", "Sigma",
                              f"{t.name}: Sigma has no date-table marking")
        if t.source is not None:
            table_path(t.source)
    for r in model.relationships:
        if r.cardinality != "many_to_one":
            raise Unsupported(f"{r.cardinality} relationship cardinality", "Sigma",
                              "a Sigma relationship carries no cardinality and reads back "
                              "as many-to-one")
        if r.cross_filter != "single":
            raise Unsupported("bidirectional relationship", "Sigma",
                              "Sigma relationships are directional")
        if not r.active:
            raise Unsupported("inactive relationship", "Sigma",
                              "Sigma has no inactive relationships")


def write_data_model(model: Model) -> dict:
    check_model(model)
    elements = []
    # Written relationships are named after their target element, which is
    # also what Sigma calls an unnamed one.
    rel_names = default_relationship_names(model)
    homes = {m.name: m.table for m in model.measures}
    for table in model.tables:
        cols, order = [], []
        for column in table.columns:
            cid = column_id(model, table, column)
            if column.expr is None:
                if table.source is not None:
                    relation = table.source.split(".")[1].upper()
                    display = display_name(column.source.upper())
                    entry = {"id": cid, "formula": f"[{relation}/{display}]"}
                    if column.name != display:
                        entry["name"] = column.name
                else:
                    entry = {"id": cid, "formula": f"[{CUSTOM_SQL}/{column.source}]"}
                    if column.name != column.source:
                        entry["name"] = column.name
            else:
                inferred = infer_type(column.expr, model)
                if inferred != column.data_type:
                    raise ModelError(f"calculated column {table.name}.{column.name}: Sigma "
                                     f"infers {inferred} from the formula, the model says "
                                     f"{column.data_type}")
                scope = FormatScope(model, "column", table.name, rel_names)
                entry = {"id": cid, "formula": format_row_formula(column.expr, scope),
                         "name": column.name}
            if column.hidden:
                entry["hidden"] = True
            if column.description:
                entry["description"] = column.description
            cols.append(entry)
            order.append(cid)
        if table.source is not None:
            source = {"connectionId": CONNECTION_ID, "kind": "warehouse-table",
                      "path": table_path(table.source)}
        else:
            source = {"connectionId": CONNECTION_ID, "kind": "sql", "statement": table.source_sql}
        element = {"id": element_id(model, table.name), "kind": "table", "name": table.name,
                   "source": source, "columns": cols, "order": order}
        if table.description:
            element["description"] = table.description
        if table.hidden:
            element["visibleAsSource"] = False
        metrics = []
        for m in model.measures:
            if m.table != table.name:
                continue
            _check_metric_refs(m, homes)
            scope = FormatScope(model, "metric", table.name, rel_names)
            entry = {"id": base62("metric", model.name, m.name),
                     "formula": format_formula(m.expr, scope), "name": m.name}
            if m.description:
                entry["description"] = m.description
            fmt = sigma_format(m.format_string)
            if fmt:
                entry["format"] = fmt
            metrics.append(entry)
        if metrics:
            element["metrics"] = metrics
        rels = []
        for r in model.relationships:
            if r.from_table != table.name:
                continue
            target = _table_named(model, r.to_table)
            src_col = _column_named(table, r.from_column)
            dst_col = _column_named(target, r.to_column)
            rels.append({"id": base62("relationship", model.name, r.from_table, r.from_column,
                                      r.to_table, r.to_column),
                         "targetElementId": element_id(model, r.to_table),
                         "keys": [{"sourceColumnId": column_id(model, table, src_col),
                                   "targetColumnId": column_id(model, target, dst_col)}],
                         "name": r.to_table})
        if rels:
            element["relationships"] = rels
        elements.append(element)
    doc = {
        "dataModelId": data_model_id(model),
        "name": model.name,
        "url": document_url("data-model", model.name, base62("url", "data-model", model.name,
                                                             length=22)),
        "documentVersion": 1,
        "latestDocumentVersion": 1,
        "ownerId": OWNER_ID,
        "folderId": FOLDER_ID,
        "createdBy": OWNER_ID,
        "updatedBy": OWNER_ID,
        "createdAt": EPOCH,
        "updatedAt": EPOCH,
        "schemaVersion": SCHEMA_VERSION,
        "pages": [{"id": base62("page", model.name, PAGE_NAME), "name": PAGE_NAME,
                   "elements": elements}],
    }
    if model.description:
        doc["description"] = model.description
    return doc


def _check_metric_refs(measure: Measure, homes: dict[str, str]) -> None:
    from ...expr import MeasureRef

    def walk(e):
        if isinstance(e, MeasureRef):
            if e.name not in homes:
                raise ModelError(f"measure {measure.name} refers to unknown {e.name!r}")
            if homes[e.name] != measure.table:
                raise unsupported("metric referencing another element's metric",
                                  f"{measure.name} ({measure.table}) -> {e.name} "
                                  f"({homes[e.name]})")
            return
        for value in vars(e).values():
            if isinstance(value, tuple):
                for v in value:
                    if hasattr(v, "__dataclass_fields__"):
                        walk(v)
            elif hasattr(value, "__dataclass_fields__"):
                walk(value)

    walk(measure.expr)


def _table_named(model: Model, name: str) -> Table:
    for t in model.tables:
        if t.name == name:
            return t
    raise ModelError(f"unknown table {name!r}")


def _column_named(table: Table, name: str) -> Column:
    for c in table.columns:
        if c.name == name:
            return c
    raise ModelError(f"{table.name} has no column {name!r}")
