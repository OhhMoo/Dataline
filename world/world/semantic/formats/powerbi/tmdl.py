"""TMDL: the semantic-model half of a Power BI project, read and written.

A `<Name>.SemanticModel/` folder is what Power BI Desktop saves in developer
mode and what Fabric's getDefinition / updateDefinition exchange as base64
"parts" (same relative paths):

    .platform                     Fabric git-integration metadata (type, displayName, logicalId)
    definition.pbism              {"$schema", "version", "settings"}
    definition/database.tmdl      compatibilityLevel
    definition/model.tmdl         model properties, annotations, `ref table` ordering
    definition/expressions.tmdl   shared M expressions: the connector parameters
    definition/relationships.tmdl one `relationship <guid>` per relationship
    definition/tables/<T>.tmdl    columns, measures, the M partition

THE PARSER IS GENERIC TMDL, the model mapping is strict. `parse_document`
implements the language rules of the TMDL reference (indentation by tabs -
four spaces accepted as one level - object headers `keyword name [= default]`,
`key: value` properties, boolean shortcuts, `///` descriptions, multi-line
expressions two levels deeper than their object, ``` blocks, quoted names with
'' escapes), so any file Desktop or Tabular Editor writes parses. The mapping
onto `Model` then refuses what the contract cannot carry exactly:
row-level security roles, calculation groups, calculated tables, more than one
partition per table, many-to-many relationships, DAX user-defined functions,
legacy (provider) data sources, TMSL `model.bim` - each as
errors.Unsupported(<Power BI's name for it>, "Power BI"). Display-only
metadata (hierarchies, display folders, cultures, perspectives, annotations,
lineage tags, summarizeBy, format strings on columns) is read and ignored,
because it changes no value. `sortByColumn` is returned to the report reader,
because it does change the row order of a visual sorted by that column.

THE WRITER EMITS WHAT DESKTOP EMITS for the same model: tab indentation,
measures before columns before the partition, `lineageTag`s (deterministic,
ids.py), `summarizeBy`, `annotation SummarizationSetBy = Automatic`, the
`UnderlyingDateTimeDataType = Date` annotation that distinguishes a date from
a datetime column (both are TMDL `dateTime`), `dataCategory: Time` plus `isKey`
for a table marked as date table (TOM's representation of "Mark as date
table"), and relationship defaults omitted (fromCardinality many, toCardinality
one, crossFilteringBehavior oneDirection, isActive true).

Sources: TMDL overview and how-to (learn.microsoft.com/analysis-services/tmdl/
tmdl-overview); TOM reference for Column.DataType, Relationship
(SingleColumnRelationship: FromCardinality, ToCardinality,
CrossFilteringBehavior, IsActive), Table.DataCategory; Power BI Desktop project
semantic model folder (learn.microsoft.com/power-bi/developer/projects/
projects-dataset); schemas github.com/microsoft/json-schemas
(fabric/item/semanticModel/definitionProperties/1.0.0,
fabric/gitIntegration/platformProperties/2.0.0); Desktop-written samples in
github.com/RuiRomano/pbip-demo-agentic and github.com/microsoft/fabric-toolbox
(MIT; vendored in world/tests/fixtures/powerbi/samples).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ...errors import ModelError, Unsupported
from ...expr import Lit
from ...model import Column, Measure, Model, Relationship, Table
from . import dax, mquery
from .ids import guid

VENDOR = "Power BI"
SCHEMA_ROOT = "https://developer.microsoft.com/json-schemas/fabric"
PLATFORM_SCHEMA = f"{SCHEMA_ROOT}/gitIntegration/platformProperties/2.0.0/schema.json"
PBISM_SCHEMA = f"{SCHEMA_ROOT}/item/semanticModel/definitionProperties/1.0.0/schema.json"
COMPATIBILITY_LEVEL = 1601


def unsupported(feature: str, detail: str = "") -> Unsupported:
    return Unsupported(feature, VENDOR, detail)


# ======================================================================
# Generic TMDL documents
# ======================================================================
@dataclass
class TNode:
    """One TMDL member: an object (`table Sales`), a property (`dataType:
    int64`), a boolean shortcut (`isHidden`) or an expression property
    (`source = ...`)."""
    keyword: str
    name: str | None = None
    value: str | None = None
    colon: bool = False
    equals: bool = False
    children: list[TNode] = field(default_factory=list)
    description: str = ""
    line: int = 0
    file: str = ""

    def where(self) -> str:
        return f"{self.file}:{self.line}" if self.file else f"line {self.line}"

    def prop(self, key: str) -> str | None:
        for child in self.children:
            if child.colon and child.keyword.lower() == key.lower():
                return child.value
        return None

    def flag(self, key: str) -> bool:
        for child in self.children:
            if child.keyword.lower() != key.lower():
                continue
            if child.colon:
                return (child.value or "").lower() == "true"
            if child.name is None and not child.equals:
                return True
        return False

    def objects(self, keyword: str) -> list[TNode]:
        return [c for c in self.children
                if not c.colon and c.keyword.lower() == keyword.lower()
                and (c.name is not None or c.equals or c.children)]

    def expression(self, key: str) -> TNode | None:
        for child in self.children:
            if not child.colon and child.keyword.lower() == key.lower() and child.name is None:
                return child
        return None

    def annotation(self, name: str) -> str | None:
        for child in self.objects("annotation"):
            if child.name == name:
                return child.value
        return None


def indent_units(line: str) -> tuple[int, int]:
    """(indentation levels, characters consumed): a tab or four spaces each."""
    units = i = 0
    while i < len(line):
        if line[i] == "\t":
            units, i = units + 1, i + 1
        elif line.startswith("    ", i):
            units, i = units + 1, i + 4
        else:
            break
    return units, i


def dedent(line: str, levels: int) -> str:
    i = 0
    for _ in range(levels):
        if line.startswith("\t", i):
            i += 1
        elif line.startswith("    ", i):
            i += 4
        else:
            break
    return line[i:]


def unquote_name(text: str) -> tuple[str, str]:
    """A possibly-quoted object name at the start of `text` -> (name, rest)."""
    if text.startswith("'"):
        out, i = [], 1
        while i < len(text):
            if text[i] == "'":
                if text.startswith("''", i):
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), text[i + 1:]
            out.append(text[i])
            i += 1
        raise ModelError(f"unterminated quoted name: {text}")
    i = 0
    while i < len(text) and not text[i].isspace() and text[i] != "=":
        i += 1
    return text[:i], text[i:]


def unquote_value(value: str) -> str:
    """Property values may be wrapped in double quotes ("" escapes a quote);
    a value that merely starts with a quote (`"€"#,0`) is taken as written."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        if inner.replace('""', "").count('"') == 0:
            return inner.replace('""', '"')
    return value


def split_reference(text: str) -> list[str]:
    """`'Order Lines'.order_id` -> ["Order Lines", "order_id"]."""
    parts, rest = [], text.strip()
    while rest:
        name, rest = unquote_name(rest) if rest.startswith("'") else _until_dot(rest)
        parts.append(name)
        rest = rest.strip()
        if rest.startswith("."):
            rest = rest[1:].strip()
        elif rest:
            raise ModelError(f"malformed object reference: {text}")
    return parts


def _until_dot(text: str) -> tuple[str, str]:
    i = text.find(".")
    return (text, "") if i < 0 else (text[:i], text[i:])


class DocumentParser:
    def __init__(self, text: str, file: str = ""):
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.file = file
        self.pos = 0

    def error(self, message: str) -> ModelError:
        return ModelError(f"{self.file}:{self.pos + 1}: {message}" if self.file
                          else f"line {self.pos + 1}: {message}")

    def parse(self) -> list[TNode]:
        nodes = self.block(0)
        while self.pos < len(self.lines) and not self.lines[self.pos].strip():
            self.pos += 1
        if self.pos < len(self.lines):
            raise self.error("unexpected indentation")
        return nodes

    def block(self, level: int) -> list[TNode]:
        nodes: list[TNode] = []
        description: list[str] = []
        while self.pos < len(self.lines):
            raw = self.lines[self.pos]
            if not raw.strip():
                self.pos += 1
                continue
            units, consumed = indent_units(raw)
            if units < level:
                break
            if units > level:
                raise self.error("unexpected indentation")
            text = raw[consumed:].strip()
            if text.startswith("///"):
                body = text[3:]
                description.append(body[1:] if body.startswith(" ") else body)
                self.pos += 1
                continue
            node = self.header(text)
            node.description = "\n".join(description)
            description = []
            self.pos += 1
            if node.equals and node.value == "```":
                node.value = self.backticks()
            elif node.equals and node.value == "":
                node.value = self.expression(level + 2)
            node.children = self.block(level + 1)
            nodes.append(node)
        return nodes

    def header(self, text: str) -> TNode:
        line = self.pos + 1
        i = 0
        while i < len(text) and not text[i].isspace() and text[i] not in ":=":
            i += 1
        keyword, rest = text[:i], text[i:].strip()
        if not keyword:
            raise self.error(f"cannot read '{text}'")
        node = TNode(keyword, line=line, file=self.file)
        if keyword == "ref":
            kind, _, name = rest.partition(" ")
            node.name = unquote_name(name.strip())[0]
            node.value = kind
            return node
        if rest.startswith(":"):
            node.colon = True
            node.value = unquote_value(rest[1:].strip())
            return node
        if rest.startswith("="):
            node.equals = True
            node.value = rest[1:].strip()
            return node
        if not rest:
            return node
        name, after = unquote_name(rest)
        node.name = name
        after = after.strip()
        if after.startswith("="):
            node.equals = True
            node.value = after[1:].strip()
        elif after:
            raise self.error(f"unexpected '{after}' after '{keyword} {name}'")
        return node

    def expression(self, level: int) -> str:
        buf: list[str] = []
        while self.pos < len(self.lines):
            raw = self.lines[self.pos]
            if not raw.strip():
                buf.append("")
                self.pos += 1
                continue
            if indent_units(raw)[0] < level:
                break
            buf.append(dedent(raw, level).rstrip())
            self.pos += 1
        while buf and not buf[-1]:
            buf.pop()
        while buf and not buf[0]:
            buf.pop(0)
        # Rewind over trailing blank lines so the next member sees them.
        while self.pos > 0 and not self.lines[self.pos - 1].strip():
            self.pos -= 1
        return "\n".join(buf)

    def backticks(self) -> str:
        buf: list[str] = []
        while self.pos < len(self.lines):
            raw = self.lines[self.pos]
            self.pos += 1
            if raw.strip() == "```":
                margin = raw[:len(raw) - len(raw.lstrip())]
                return "\n".join(line[len(margin):] if line.startswith(margin) else line.lstrip()
                                 for line in buf)
            buf.append(raw)
        raise self.error("unterminated ``` expression")


def parse_document(text: str, file: str = "") -> list[TNode]:
    return DocumentParser(text, file).parse()


# ======================================================================
# Writing TMDL text
# ======================================================================
def quote_name(name: str) -> str:
    if not name or any(ch in ".=:'" or ch.isspace() for ch in name):
        return "'" + name.replace("'", "''") + "'"
    return name


def property_value(value: str) -> str:
    if value != value.strip() or (value.startswith('"') and value.endswith('"')):
        return '"' + value.replace('"', '""') + '"'
    return value


class Writer:
    """Builds one TMDL document; `indent` is the object's level."""

    def __init__(self):
        self.lines: list[str] = []

    def blank(self) -> None:
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def description(self, text: str, level: int) -> None:
        for line in text.split("\n") if text else ():
            self.lines.append("\t" * level + ("/// " + line if line else "///"))

    def header(self, level: int, keyword: str, name: str | None = None,
               value: str | None = None) -> None:
        """`keyword name = value`; a multi-line value goes two levels deeper."""
        head = "\t" * level + keyword + (f" {quote_name(name)}" if name is not None else "")
        if value is None:
            self.lines.append(head)
            return
        if "\n" not in value and value == value.strip():
            self.lines.append(f"{head} = {value}")
            return
        body = value.split("\n")
        if any(line != line.rstrip() for line in body) or value != value.strip():
            self.lines.append(f"{head} = ```")
            self.lines.extend("\t" * (level + 2) + line for line in body)
            self.lines.append("\t" * (level + 2) + "```")
            return
        self.lines.append(f"{head} =")
        self.lines.extend(("\t" * (level + 2) + line) if line else "" for line in body)

    def prop(self, level: int, key: str, value: str | None = None) -> None:
        if value is None:
            self.lines.append("\t" * level + key)
        else:
            self.lines.append("\t" * level + f"{key}: {property_value(value)}")

    def text(self) -> str:
        while self.lines and self.lines[-1] == "":
            self.lines.pop()
        return "\n".join(self.lines) + "\n\n"


# ======================================================================
# Model -> files
# ======================================================================
DATA_TYPES = {"int": "int64", "decimal": "decimal", "float": "double", "string": "string",
              "bool": "boolean", "date": "dateTime", "timestamp": "dateTime"}
FORMATS = {"int": "0", "decimal": "#,0.00", "bool": '"TRUE";"TRUE";"FALSE"',
           "date": "Long Date", "timestamp": "General Date"}


def platform_json(kind: str, display_name: str, description: str = "") -> bytes:
    metadata: dict[str, Any] = {"type": kind, "displayName": display_name}
    if description:
        metadata["description"] = description
    body = {"$schema": PLATFORM_SCHEMA, "metadata": metadata,
            "config": {"version": "2.0", "logicalId": guid(kind, display_name)}}
    return json_bytes(body)


def json_bytes(body: Any) -> bytes:
    """Desktop's layout: two-space indent, keys in the order written."""
    return (json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def summarize_by(model: Model, table: Table, column: Column) -> str:
    keys = {(r.from_table, r.from_column) for r in model.relationships}
    keys |= {(r.to_table, r.to_column) for r in model.relationships}
    if column.data_type in ("int", "decimal", "float") and (table.name, column.name) not in keys:
        return "sum"
    return "none"


def table_document(model: Model, table: Table, connector: str) -> str:
    w = Writer()
    w.description(table.description, 0)
    w.header(0, "table", table.name)
    w.prop(1, "lineageTag", guid(model.name, "table", table.name))
    if table.is_date_table:
        w.prop(1, "dataCategory", "Time")
    if table.hidden:
        w.prop(1, "isHidden")
    for measure in model.measures:
        if measure.table != table.name:
            continue
        w.blank()
        w.description(measure.description, 1)
        w.header(1, "measure", measure.name, dax.format_expression(measure.expr, model))
        if measure.format_string:
            w.prop(2, "formatString", measure.format_string)
        w.prop(2, "lineageTag", guid(model.name, "measure", measure.name))
    for column in table.columns:
        w.blank()
        w.description(column.description, 1)
        if column.data_type not in DATA_TYPES:
            raise ModelError(f"column {table.name}.{column.name}: unknown data type "
                             f"{column.data_type!r}")
        if column.expr is not None:
            w.header(1, "column", column.name,
                     dax.Formatter(model).fmt(column.expr))
        else:
            w.header(1, "column", column.name)
        w.prop(2, "dataType", DATA_TYPES[column.data_type])
        if column.hidden:
            w.prop(2, "isHidden")
        if table.is_date_table and table.date_column == column.name:
            w.prop(2, "isKey")
        if column.data_type in FORMATS:
            w.prop(2, "formatString", FORMATS[column.data_type])
        w.prop(2, "lineageTag", guid(model.name, "column", table.name, column.name))
        w.prop(2, "summarizeBy", summarize_by(model, table, column))
        if column.expr is None:
            w.prop(2, "sourceColumn", column.source)
        w.blank()
        w.header(2, "annotation", "SummarizationSetBy", "Automatic")
        if column.data_type == "date":
            w.blank()
            w.header(2, "annotation", "UnderlyingDateTimeDataType", "Date")
    w.blank()
    w.header(1, "partition", table.name, "m")
    w.prop(2, "mode", "import")
    w.header(2, "source", None, mquery.partition_source(table, connector))
    w.blank()
    w.header(1, "annotation", "PBI_ResultType", "Table")
    return w.text()


def relationships_document(model: Model) -> str:
    w = Writer()
    for rel in model.relationships:
        w.blank()
        w.header(0, "relationship", guid(model.name, "relationship", rel.from_table,
                                         rel.from_column, rel.to_table, rel.to_column))
        if not rel.active:
            w.prop(1, "isActive", "false")
        if rel.cross_filter == "both":
            w.prop(1, "crossFilteringBehavior", "bothDirections")
        if rel.cardinality == "one_to_one":
            w.prop(1, "fromCardinality", "one")
        w.prop(1, "fromColumn", f"{quote_name(rel.from_table)}.{quote_name(rel.from_column)}")
        w.prop(1, "toColumn", f"{quote_name(rel.to_table)}.{quote_name(rel.to_column)}")
    return w.text()


def expressions_document(model: Model, connector: str) -> str:
    w = Writer()
    for name, text in mquery.parameter_expressions(connector):
        w.blank()
        w.header(0, "expression", name, text)
        w.prop(1, "lineageTag", guid(model.name, "expression", name))
        w.blank()
        w.header(1, "annotation", "PBI_ResultType", "Text")
    return w.text()


def model_document(model: Model, connector: str) -> str:
    w = Writer()
    w.description(model.description, 0)
    w.header(0, "model", "Model")
    w.prop(1, "culture", "en-US")
    w.prop(1, "defaultPowerBIDataSourceVersion", "powerBI_V3")
    w.prop(1, "sourceQueryCulture", "en-US")
    w.header(1, "dataAccessOptions")
    w.prop(2, "legacyRedirects")
    w.prop(2, "returnErrorValuesAsNull")
    queries = [name for name, _ in mquery.parameter_expressions(connector)]
    queries += [t.name for t in model.tables]
    w.blank()
    w.header(0, "annotation", "__PBI_TimeIntelligenceEnabled", "0")
    w.blank()
    w.header(0, "annotation", "PBI_QueryOrder",
             json.dumps(queries, separators=(",", ":"), ensure_ascii=False))
    w.blank()
    w.header(0, "annotation", "PBI_ProTooling", '["DevMode"]')
    if len(model.tables) > 1:
        w.blank()
        for table in model.tables:
            w.lines.append(f"ref table {quote_name(table.name)}")
    return w.text()


def file_name(name: str) -> str:
    """Table file names are the table name; characters no filesystem takes are
    replaced (the name inside the file is what is read back)."""
    return "".join("_" if ch in '<>:"/\\|?*' else ch for ch in name)


def semantic_model_files(model: Model, *, connector: str = mquery.DEFAULT_CONNECTOR
                         ) -> dict[str, bytes]:
    """Every file of `<model.name>.SemanticModel/`, keyed by relative path."""
    if model.null_semantics != "dax":
        raise unsupported("SQL null semantics", "Power BI evaluates DAX blank semantics; "
                                                "write the model with null_semantics='dax'")
    validate_model(model)
    files = {
        ".platform": platform_json("SemanticModel", model.name),
        "definition.pbism": json_bytes({"$schema": PBISM_SCHEMA, "version": "4.2",
                                        "settings": {}}),
        "definition/database.tmdl": (f"database\n\tcompatibilityLevel: "
                                     f"{COMPATIBILITY_LEVEL}\n\n").encode(),
        "definition/model.tmdl": model_document(model, connector).encode("utf-8"),
        "definition/expressions.tmdl": expressions_document(model, connector).encode("utf-8"),
    }
    if model.relationships:
        files["definition/relationships.tmdl"] = relationships_document(model).encode("utf-8")
    seen: set[str] = set()
    for table in model.tables:
        path = f"definition/tables/{file_name(table.name)}.tmdl"
        if path.lower() in seen:
            raise ModelError(f"two tables map to the file {path}")
        seen.add(path.lower())
        files[path] = table_document(model, table, connector).encode("utf-8")
    return files


def validate_model(model: Model) -> None:
    """The structural checks Power BI would refuse a deployment for."""
    names = [t.name.lower() for t in model.tables]
    if len(set(names)) != len(names):
        raise ModelError("two tables share a name")
    tables = {t.name: t for t in model.tables}
    for measure in model.measures:
        if measure.table not in tables:
            raise ModelError(f"measure {measure.name!r} has home table {measure.table!r}, "
                             f"which is not in the model")
    lowered = [m.name.lower() for m in model.measures]
    if len(set(lowered)) != len(lowered):
        raise ModelError("two measures share a name")
    for table in model.tables:
        if table.is_date_table:
            col = next((c for c in table.columns if c.name == table.date_column), None)
            if col is None or col.data_type not in ("date", "timestamp"):
                raise ModelError(f"date table {table.name!r} needs a date column")
        if (table.source is None) == (table.source_sql is None):
            raise ModelError(f"table {table.name!r} needs exactly one of source, source_sql")
    for rel in model.relationships:
        for t, c in ((rel.from_table, rel.from_column), (rel.to_table, rel.to_column)):
            if t not in tables or c not in {col.name for col in tables[t].columns}:
                raise ModelError(f"relationship names {t}.{c}, which is not in the model")


# ======================================================================
# Files -> model
# ======================================================================
REFUSED_OBJECTS = {
    "role": "row-level security (roles)",
    "function": "DAX user-defined functions",
    "datasource": "legacy data source",
    "calculationgroup": "calculation group",
}
DATE_ONLY_FORMATS = {"long date", "short date", "yyyy-mm-dd", "dd/mm/yyyy", "mm/dd/yyyy",
                     "d/m/yyyy", "m/d/yyyy", "yyyy-mm"}
TYPES_BACK = {"int64": "int", "decimal": "decimal", "double": "float", "string": "string",
              "boolean": "bool"}


@dataclass
class SemanticModelInfo:
    """What the report reader needs besides the model: which columns sort by
    another column (Power BI orders a visual by the sort-by column)."""
    sort_by: dict[tuple[str, str], str] = field(default_factory=dict)


def json_document(raw: bytes, path: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ModelError(f"Cannot read '{path}': {exc}") from None


def read_semantic_model(files: dict[str, bytes], fallback_name: str = "Model"
                        ) -> tuple[Model, SemanticModelInfo]:
    """The files of one `.SemanticModel` folder (relative paths) -> Model."""
    if "model.bim" in files:
        raise unsupported("TMSL model.bim", "save the project with the TMDL format "
                                            "(definition/ folder)")
    if "definition.pbism" not in files:
        raise ModelError("Cannot read 'definition.pbism': the file is missing")
    json_document(files["definition.pbism"], "definition.pbism")
    name = fallback_name
    if ".platform" in files:
        platform = json_document(files[".platform"], ".platform")
        name = platform.get("metadata", {}).get("displayName") or name
    nodes: list[TNode] = []
    for path in sorted(files):
        if path.startswith("definition/") and path.endswith(".tmdl"):
            if path.startswith(("definition/cultures/", "definition/perspectives/")):
                continue
            text = files[path].decode("utf-8-sig")
            nodes.extend(parse_document(text, path))
    if not nodes:
        raise ModelError("the semantic model has no definition/*.tmdl files")
    return build_model(nodes, name)


def build_model(nodes: list[TNode], name: str) -> tuple[Model, SemanticModelInfo]:
    model_node = None
    table_nodes: dict[str, TNode] = {}
    order: list[str] = []
    rel_nodes: list[TNode] = []
    shared: dict[str, str] = {}
    for node in nodes:
        kind = node.keyword.lower()
        if kind in REFUSED_OBJECTS:
            raise unsupported(REFUSED_OBJECTS[kind], node.where())
        if kind == "model":
            model_node = node
        elif kind == "table":
            if node.name in table_nodes:          # partial declaration: merge
                table_nodes[node.name].children.extend(node.children)
            else:
                table_nodes[node.name] = node
        elif kind == "relationship":
            rel_nodes.append(node)
        elif kind == "expression":
            shared[node.name] = node.value or ""
        elif kind == "ref" and node.value == "table":
            order.append(node.name)
    for node in nodes:
        if node.keyword.lower() == "model":
            for child in node.children:
                if child.keyword.lower() == "ref" and child.value == "table":
                    order.append(child.name)
    names = [n for n in order if n in table_nodes]
    names += [n for n in table_nodes if n not in names]

    info = SemanticModelInfo()
    tables: list[Table] = []
    pending_columns: list[tuple[int, int, str]] = []    # (table idx, col idx, dax)
    pending_measures: list[tuple[str, str, TNode]] = []
    for t_index, table_name in enumerate(names):
        node = table_nodes[table_name]
        for child in node.children:
            if child.keyword.lower() in REFUSED_OBJECTS:
                raise unsupported(REFUSED_OBJECTS[child.keyword.lower()], child.where())
        columns: list[Column] = []
        date_column = None
        for c_index, col in enumerate(node.objects("column")):
            column, is_key, sort_by = read_column(col)
            if sort_by:
                info.sort_by[(table_name, column.name)] = sort_by
            if col.equals and col.value:
                pending_columns.append((t_index, c_index, col.value))
            if is_key:
                date_column = column.name
            columns.append(column)
        source, source_sql = read_partition(node, shared)
        is_date = (node.prop("dataCategory") or "").lower() == "time" and date_column is not None
        tables.append(Table(table_name, source, tuple(columns), source_sql=source_sql,
                            is_date_table=is_date, date_column=date_column if is_date else None,
                            hidden=node.flag("isHidden"), description=node.description))
        for measure in node.objects("measure"):
            if not measure.equals or not measure.value:
                raise ModelError(f"{measure.where()}: measure {measure.name!r} has no "
                                 f"expression")
            pending_measures.append((table_name, measure.value, measure))
    relationships = tuple(read_relationship(n) for n in rel_nodes)
    stub_measures = tuple(Measure(m.name, t, Lit(None)) for t, _, m in pending_measures)
    stub = Model(name, tuple(tables), relationships, stub_measures, null_semantics="dax")
    for t_index, c_index, text in pending_columns:
        table = tables[t_index]
        expr = dax.parse_expression(text, stub, table=table.name)
        columns = list(table.columns)
        columns[c_index] = Column(columns[c_index].name, columns[c_index].source,
                                  columns[c_index].data_type, columns[c_index].hidden, expr,
                                  columns[c_index].description)
        tables[t_index] = Table(table.name, table.source, tuple(columns), table.source_sql,
                                table.is_date_table, table.date_column, table.hidden,
                                table.description)
    stub = Model(name, tuple(tables), relationships, stub_measures, null_semantics="dax")
    measures = []
    for table_name, text, node in pending_measures:
        try:
            expr = dax.parse_expression(text, stub)
        except ModelError as exc:
            raise ModelError(f"{node.where()}: measure {node.name!r}: {exc}") from None
        measures.append(Measure(node.name, table_name, expr,
                                format_string=node.prop("formatString"),
                                description=node.description))
    description = model_node.description if model_node is not None else ""
    model = Model(name, tuple(tables), relationships, tuple(measures), null_semantics="dax",
                  description=description)
    validate_model(model)
    return model, info


def read_column(node: TNode) -> tuple[Column, bool, str | None]:
    data_type = (node.prop("dataType") or "").lower()
    if data_type == "datetime":
        underlying = (node.annotation("UnderlyingDateTimeDataType") or "").strip()
        fmt = (node.prop("formatString") or "").lower()
        kind = "date" if underlying.lower() == "date" or fmt in DATE_ONLY_FORMATS else "timestamp"
    elif data_type in TYPES_BACK:
        kind = TYPES_BACK[data_type]
    else:
        raise unsupported(f"column data type {node.prop('dataType')}", node.where())
    if node.equals and node.value:
        source = node.name
    else:
        source = node.prop("sourceColumn")
        if source is None:
            raise ModelError(f"{node.where()}: column {node.name!r} has neither sourceColumn "
                             f"nor an expression")
    sort_by = node.prop("sortByColumn")
    column = Column(node.name, source, kind, hidden=node.flag("isHidden"),
                    description=node.description)
    is_key = node.flag("isKey") and kind in ("date", "timestamp")
    return column, is_key, unquote_name(sort_by)[0] if sort_by else None


def read_partition(table: TNode, shared: dict[str, str]) -> tuple[str | None, str | None]:
    partitions = table.objects("partition")
    if not partitions:
        raise ModelError(f"{table.where()}: table {table.name!r} has no partition")
    if len(partitions) > 1:
        raise unsupported("multiple partitions (incremental refresh)", table.where())
    part = partitions[0]
    kind = (part.value or "m").lower()
    if kind == "m":
        source = part.expression("source")
        if source is None or not source.value:
            raise ModelError(f"{part.where()}: partition {part.name!r} has no source")
        return mquery.parse_partition(source.value, shared)
    if kind == "entity":
        source = part.expression("source")
        if source is None:
            raise ModelError(f"{part.where()}: entity partition {part.name!r} has no source")
        entity = source.prop("entityName")
        schema = source.prop("schemaName")
        if not entity or not schema:
            raise unsupported("Direct Lake entity without a schemaName", part.where())
        return f"{schema}.{entity}", None
    if kind == "calculated":
        raise unsupported("calculated table", part.where())
    raise unsupported(f"{kind} partition", part.where())


def read_relationship(node: TNode) -> Relationship:
    def end(key):
        value = node.prop(key)
        if not value:
            raise ModelError(f"{node.where()}: relationship without {key}")
        parts = split_reference(value)
        if len(parts) != 2:
            raise ModelError(f"{node.where()}: {key} must be Table.Column")
        return parts
    (ft, fc), (tt, tc) = end("fromColumn"), end("toColumn")
    from_card = (node.prop("fromCardinality") or "many").lower()
    to_card = (node.prop("toCardinality") or "one").lower()
    if from_card == "many" and to_card == "many":
        raise unsupported("many-to-many relationship", node.where())
    if from_card == "one" and to_card == "many":
        (ft, fc), (tt, tc), from_card, to_card = (tt, tc), (ft, fc), "many", "one"
    cardinality = "one_to_one" if from_card == "one" else "many_to_one"
    behaviour = (node.prop("crossFilteringBehavior") or "oneDirection").lower()
    if node.prop("joinOnDateBehavior") and node.prop("joinOnDateBehavior").lower() != "datetime":
        raise unsupported("joinOnDateBehavior datePartOnly", node.where())
    return Relationship(ft, fc, tt, tc, cardinality=cardinality,
                        cross_filter="both" if behaviour == "bothdirections" else "single",
                        active=(node.prop("isActive") or "true").lower() != "false")


__all__ = ["SemanticModelInfo", "TNode", "parse_document", "read_semantic_model",
           "semantic_model_files", "validate_model"]
