"""Tableau data sources (`<datasource>`, the root of a .tds) <-> the semantic Model.

The mapping, element by element (Tableau 2020.2+ "logical layer" format, as in
the MIT-licensed samples of github.com/tableau/document-api-python and
github.com/tableau/server-client-python, and the Apache-2.0 schema of
github.com/tableau/tableau-document-schemas):

    <datasource caption=..>                    Model (name = caption, else file stem)
      <connection class='federated'>
        <named-connections>                    exactly one warehouse connection; the
                                               class is metadata (CONNECTIONS below)
        <metadata-records>                     the columns: remote-name -> Column.source,
                                               local-name -> the field's name, object-id
                                               -> which logical table
      <column caption hidden datatype ..>      Column.name (caption), hidden, description;
        <calculation class='tableau' formula>  calculated field: a measure when it
                                               aggregates, else a calculated Column
      <object-graph><objects><object caption>  Table (name = caption); its physical
        <properties><relation type='table'>    Table.source  "schema.table"
                    <relation type='text'>     Table.source_sql (custom SQL)
                    <relation type='join'>     Table.source_sql: the JOIN written out
      <relationships><relationship>            Relationship; the side whose end point
                                               has unique-key='true' is the ONE side

PHYSICAL JOINS ARE KEPT AS JOINS. A logical table built from a physical join
(`<relation type='join'>`, Tableau's pre-2020.2 data model and still the
"double-click into a logical table" canvas) is lowered to `source_sql` that
performs exactly that join. That is where real fan-out double counting lives:
SUM([order_amount]) over fct_orders INNER JOIN fct_order_lines counts each order
once per line, in Tableau and here, because both run the same join.

CARDINALITY. A Tableau relationship is not directional; the model's is
(filters flow from the one side to the many side). The direction comes from
the relationship's Performance Options (`unique-key='true'` on an end point is
cardinality One). With neither side set to One - Tableau's default
Many-to-Many - there is no direction to read, and guessing one could silently
turn a filter around, so it is refused. `write` always sets it.

EXTRACTS. There is no Hyper API for Linux arm64, so nothing here reads a .hyper
file. A data source whose ONLY connection is an extract (`class='hyper'`) is
refused. A federated data source with `<extract enabled='true'>` keeps its live
connection definition beside the extract, and that definition IS the model:
the extract is a copy of the same query taken at refresh time. Whether a view
reads the copy or the warehouse is the server's business (the fake Tableau
Server keeps the copy as a DuckDB snapshot); `DataSource.extract` reports it.

WAREHOUSE CONNECTION CLASSES. Tableau has no DuckDB connector. The laptop stack
(DuckDB) is written as a generic ODBC connection through the DuckDB ODBC
driver, which is how DuckDB is reached from Tableau on a workstation; the
server-side warehouses use Tableau's native connectors (`CONNECTIONS`). The
class never changes a value: Table.source is warehouse-relative either way.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace

from ...errors import ModelError, Unsupported
from ...expr import Col, Expr, MeasureRef
from ...model import Column, DataType, Measure, Model, Relationship, Table
from . import calc
from .xmlio import FILE_VERSION, sub

V = "Tableau"


# ============================================================== connections
@dataclass(frozen=True)
class Connection:
    """The warehouse a data source points at, as Tableau spells it."""
    cls: str
    caption: str
    attrs: tuple[tuple[str, str], ...]


def _conn(cls: str, caption: str, **attrs: str) -> Connection:
    keep = {"tableName"}       # Desktop's own camelCase attribute
    return Connection(cls, caption, tuple(sorted(
        (k if k in keep else k.replace("_", "-"), v) for k, v in attrs.items())))


#: warehouse backend (GatewayConfig.warehouse_backend / the profile's warehouse
#: plug) -> the connection a data engineer's Tableau data source would carry.
#: Hosts are the episode network's names; credentials are never written (Tableau
#: does not store passwords in a .tds).
CONNECTIONS: dict[str, Connection] = {
    # genericodbc as Desktop 2025.1 writes it, with the DuckDB ODBC driver. The
    # database path is where the world's warehouse file is on the box (the
    # world root is /world; Power BI's duckdb connector names the same file).
    "duckdb": _conn("genericodbc", "DuckDB", dbname="warehouse",
                    odbc_connect_string_extras="database=/world/var/warehouse/warehouse.duckdb",
                    odbc_dbms_name="DuckDB", odbc_driver="DuckDB Driver", odbc_dsn="",
                    odbc_suppress_connection_pooling="", odbc_use_connection_pooling="",
                    port="", schema="warehouse", server="", username=""),
    # postgres / snowflake / databricks / bigquery / redshift: the attribute sets
    # of real 2024-2025 saves (MIT-licensed workbooks on GitHub).
    "postgres": _conn("postgres", "postgres.internal", authentication="username-password",
                      dbname="alkera", odbc_native_protocol="yes", one_time_sql="",
                      port="5432", server="postgres.internal", server_oauth="",
                      username="alkera", workgroup_auth_mode="prompt"),
    "snowflake": _conn("snowflake", "acme.snowflakecomputing.com",
                       authentication="Username Password", dbname="ANALYTICS",
                       max_varchar_size="", odbc_connect_string_extras="", one_time_sql="",
                       schema="WAREHOUSE", server="acme.snowflakecomputing.com",
                       service="TRANSFORMER", temp_table_detection="optimized",
                       username="alkera", warehouse="TRANSFORMING"),
    "redshift": _conn("redshift", "redshift.internal", authentication="auth-user-pass",
                      dbname="dev", odbc_connect_string_extras="", one_time_sql="",
                      port="5439", schema="warehouse", server="redshift.internal",
                      single_node="", sslmode="require", username="alkera"),
    "databricks": _conn("databricks", "databricks.internal", authentication="auth-pass",
                        authentication_type="", dbname="hive_metastore",
                        odbc_connect_string_extras="", one_time_sql="", schema="warehouse",
                        server="databricks.internal", server_oauth="", tableName="",
                        username="token", v_http_path="/sql/1.0/warehouses/alkera"),
    "bigquery": _conn("bigquery", "BigQuery", CATALOG="alkera-analytics",
                      EXECCATALOG="alkera-analytics",
                      authentication="auth-oauth-service-account",
                      connection_dialect="google-sql", project="alkera-analytics",
                      schema="warehouse", server_oauth="",
                      username="tableau@alkera-analytics.iam.gserviceaccount.com"),
}
#: Connection classes whose relations are SQL tables in a warehouse. Anything else
#: (Excel, text files, Google Sheets, Salesforce, a bare extract) is refused.
SQL_CLASSES = {c.cls for c in CONNECTIONS.values()} | {
    "sqlserver", "mysql", "oracle", "teradata", "vertica", "athena", "exasol",
    "presto", "trino", "spark", "hive", "impala", "saphana", "db2", "netezza",
    "genericjdbc", "duckdb_jdbc", "azure_sqldb", "azure_synapse", "postgres_jdbc",
}


# ====================================================================== ids
def _digest(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


def base36(key: str, n: int = 28) -> str:
    """Desktop's 28-character lowercase id (`federated.1s4nxn20cywkdv13ql0yk0g1mpdx`)."""
    v = int.from_bytes(_digest(key), "big")
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    for _ in range(n):
        v, r = divmod(v, 36)
        out.append(chars[r])
    return "".join(out)


def object_id(table: Table, key: str) -> str:
    """A logical table's id as Desktop 2025 writes it for a warehouse table,
    `<relation> (<schema>.<relation>)_<32 hex>`, or `Custom SQL Query_<32 hex>`."""
    if table.source:
        base = f"{table.source.split('.')[-1]} ({table.source})"
    else:
        base = "Custom SQL Query"
    return f"{base}_{_digest(key).hex()[:32].upper()}"


def guid(key: str) -> str:
    h = _digest(key).hex().upper()
    return "{" + f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}" + "}"


# ============================================================ data types
# ADO type codes in <remote-type> (Tableau writes the OLE DB / ADO DataTypeEnum).
_REMOTE_TYPE = {"int": "3", "decimal": "131", "float": "5", "string": "130", "bool": "11",
                "date": "133", "timestamp": "135"}
_FROM_REMOTE = {
    **dict.fromkeys(("2", "3", "16", "17", "18", "19", "20", "21"), "int"),
    **dict.fromkeys(("4", "5"), "float"),
    **dict.fromkeys(("6", "14", "131", "139"), "decimal"),
    **dict.fromkeys(("8", "129", "130", "200", "201", "202", "203"), "string"),
    "11": "bool", "7": "date", "133": "date", "135": "timestamp",
}
_FROM_LOCAL = {"integer": "int", "real": "float", "string": "string", "boolean": "bool",
               "date": "date", "datetime": "timestamp"}


def _datatype(remote_type: str | None, local_type: str | None, what: str) -> DataType:
    if remote_type in _FROM_REMOTE:
        return _FROM_REMOTE[remote_type]
    if local_type in _FROM_LOCAL:
        return _FROM_LOCAL[local_type]
    raise Unsupported(f"column type {local_type or remote_type}", V, what)


# ========================================================= format strings
def format_to_tableau(fs: str | None) -> str | None:
    """Model format string -> Tableau `default-format` (a type letter + the
    Excel-style pattern, e.g. `p0.0%`, `c"$"#,##0.00`)."""
    if not fs:
        return None
    if fs.endswith("%"):
        return "p" + fs
    if "$" in fs or "€" in fs or "£" in fs:
        return "c" + fs
    return "n" + fs


def format_from_tableau(df: str | None) -> str | None:
    if not df:
        return None
    return df[1:] if df[0] in "ncpe*" else df


# ================================================================ reading
@dataclass
class DataSource:
    """One `<datasource>` read into the model, plus what the views need to bind
    to it."""
    model: Model
    names: calc.Names
    name: str                       # 'federated.xxx' / 'sqlproxy.xxx'
    caption: str
    connection_class: str
    extract: bool = False
    published: str | None = None    # sqlproxy: the published data source's name
    #: calcs present in the file but outside the subset (non-strict reads): name -> error
    deferred: dict[str, Unsupported] = field(default_factory=dict)
    #: workbook-local calculations (a twb over a published source): name -> (caption, expr)
    local_calcs: dict[str, tuple[str, Expr]] = field(default_factory=dict)
    #: object id -> logical table name
    objects: dict[str, str] = field(default_factory=dict)


def _unbracket(text: str) -> str:
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        return text[1:-1].replace("]]", "]")
    return text


def _split_qualified(text: str) -> list[str]:
    """`[warehouse].[fct_orders]` -> ['warehouse', 'fct_orders']."""
    return [p.replace("]]", "]") for p in re.findall(r"\[((?:[^\]]|\]\])*)\]", text)] \
        or text.split(".")


@dataclass
class _Meta:
    remote: str
    local: str
    parent: str
    object: str | None
    dtype: DataType


def read(elem: ET.Element, *, name_hint: str, strict: bool = True,
         resolve_published=None) -> DataSource:
    """`<datasource>` -> DataSource. `resolve_published(name)` returns the
    DataSource a `sqlproxy` connection points at (a workbook over a published
    data source); without it such a workbook cannot be read."""
    what = f"data source {elem.get('caption') or name_hint!r}"
    conn = elem.find("connection")
    if conn is None:
        if elem.get("hasconnection") == "false" or elem.get("name") == "Parameters":
            raise Unsupported("parameter", V, f"{what} is the Parameters pseudo-source")
        raise ModelError(f"{what}: no <connection>")
    caption = elem.get("caption") or name_hint
    ds_name = elem.get("name") or elem.get("formatted-name") or caption
    if elem.find("filter") is not None:
        raise Unsupported("data source filter", V,
                          f"{what}: applied before FIXED level of detail expressions and to "
                          f"every table of the source; the model has no such filter")
    if conn.get("class") == "sqlproxy":
        return _read_sqlproxy(elem, conn, caption, ds_name, what, strict, resolve_published)

    extract = elem.find("extract")
    has_extract = extract is not None and extract.get("enabled", "true") == "true"
    warehouse_class, connections = _connections(conn, what)
    metas = _metadata(conn, what)
    tables, objects, rel_of_object = _logical_tables(elem, conn, metas, connections, what,
                                                     caption)
    columns_by_name = {c.get("name"): c for c in elem.findall("column")}
    names = calc.Names()
    built: list[Table] = []
    for obj_id, (table_name, source, source_sql, members) in tables.items():
        joined = _is_join(rel_of_object[obj_id])
        cols = []
        remote_counts: dict[str, int] = {}
        for m in members:
            remote_counts[m.remote] = remote_counts.get(m.remote, 0) + 1
        for m in members:
            col_elem = columns_by_name.get(f"[{m.local}]")
            cap = col_elem.get("caption") if col_elem is not None else None
            if col_elem is not None and col_elem.find("aliases/alias") is not None:
                raise Unsupported("aliases", V, f"{what}: field [{m.local}] has member aliases, "
                                  f"which change the values a view shows")
            if col_elem is not None and col_elem.find("calculation") is not None:
                continue
            # The model name: the caption the author gave the field, else its
            # warehouse name (unique within one logical table, unless a physical
            # join brought the same name in twice - then Tableau's own
            # disambiguated `name (relation)`).
            model_name = cap or (m.remote if remote_counts[m.remote] == 1 else m.local)
            cols.append(Column(
                name=model_name,
                # A joined table's columns are the aliases its lowered SQL selects.
                source=m.local if joined else m.remote,
                data_type=m.dtype,
                hidden=col_elem is not None and col_elem.get("hidden") == "true",
                description=_desc(col_elem)))
            names.add(m.local, Col(table_name, model_name), caption=model_name)
        built.append(Table(name=table_name, source=source, columns=tuple(cols),
                           source_sql=source_sql))
        names.add(obj_id, calc.CountRef(table_name), caption=f"{table_name} (Count)")
    model = Model(name=caption, tables=tuple(built),
                  relationships=_relationships(elem, names, objects, what))
    ds = DataSource(model=model, names=names, name=ds_name, caption=caption,
                    connection_class=warehouse_class, extract=has_extract,
                    objects={i: t[0] for i, t in tables.items()})
    _calculations(ds, elem, strict, what)
    return ds


def _is_join(rel: ET.Element) -> bool:
    return rel.get("type") == "join"


def _desc(col_elem) -> str:
    if col_elem is None:
        return ""
    runs = col_elem.findall("desc/formatted-text/run")
    return "".join(r.text or "" for r in runs).strip()


def _connections(conn: ET.Element, what: str) -> tuple[str, dict[str, str]]:
    """-> (the warehouse class, named-connection name -> class)."""
    cls = conn.get("class")
    if cls != "federated":
        if cls in ("hyper", "dataengine"):
            raise Unsupported("extract-only data source (Hyper file)", V,
                              f"{what}: no live connection to read the model from, and no "
                              f"Hyper API on this platform")
        if cls not in SQL_CLASSES:
            raise Unsupported(f"{cls} connection", V, f"{what}: not a SQL warehouse")
        return cls, {"": cls}
    named = conn.findall("named-connections/named-connection")
    if not named:
        raise ModelError(f"{what}: a federated connection with no named connections")
    classes = {}
    for nc in named:
        inner = nc.find("connection")
        icls = inner.get("class") if inner is not None else None
        if icls in ("hyper", "dataengine"):
            raise Unsupported("extract-only data source (Hyper file)", V,
                              f"{what}: no live connection to read the model from, and no "
                              f"Hyper API on this platform")
        if icls not in SQL_CLASSES:
            raise Unsupported(f"{icls} connection", V, f"{what}: not a SQL warehouse")
        classes[nc.get("name")] = icls
    if len(classes) > 1:
        raise Unsupported("cross-database join", V,
                          f"{what} connects to {len(classes)} databases "
                          f"({', '.join(sorted(set(classes.values())))})")
    return next(iter(classes.values())), classes


def _metadata(conn: ET.Element, what: str) -> list[_Meta]:
    out = []
    for rec in conn.findall("metadata-records/metadata-record"):
        if rec.get("class") != "column":
            continue
        remote = rec.findtext("remote-name")
        local = _unbracket(rec.findtext("local-name") or f"[{remote}]")
        parent = _unbracket(rec.findtext("parent-name") or "")
        obj = rec.findtext("object-id")
        out.append(_Meta(remote=remote, local=local, parent=parent,
                         object=_unbracket(obj) if obj else None,
                         dtype=_datatype(rec.findtext("remote-type"),
                                         rec.findtext("local-type"),
                                         f"{what}: [{local}]")))
    if not out:
        raise ModelError(f"{what}: no <metadata-records>; Tableau writes the column list "
                         f"there and nothing else names the warehouse columns")
    return out


def _logical_tables(elem, conn, metas, connections, what, caption):
    """object id -> (table name, source, source_sql, metadata of its columns)."""
    graph = elem.find("object-graph")
    out: dict[str, tuple] = {}
    rel_of: dict[str, ET.Element] = {}
    objects: dict[str, str] = {}
    if graph is not None and graph.find("objects/object") is not None:
        for obj in graph.findall("objects/object"):
            oid = obj.get("id")
            rel = next((p.find("relation") for p in obj.findall("properties")
                        if p.get("context", "") == ""), None)
            if rel is None:
                raise ModelError(f"{what}: logical table {obj.get('caption')!r} has no relation")
            name = obj.get("caption") or oid
            members = [m for m in metas if m.object == oid]
            if not members and len(graph.findall("objects/object")) == 1:
                members = list(metas)
            rel_of[oid] = rel
            objects[oid] = name
            out[oid] = (name, *_physical(rel, members, connections, f"{what}: {name!r}"),
                        members)
        return out, objects, rel_of
    # Pre-2020.2 file: one relation under the connection is the one table.
    rel = conn.find("relation")
    if rel is None:
        raise ModelError(f"{what}: no relation")
    name = rel.get("name") if rel.get("type") == "table" else caption
    oid = name
    rel_of[oid] = rel
    objects[oid] = name
    out[oid] = (name, *_physical(rel, list(metas), connections, f"{what}: {name!r}"),
                list(metas))
    return out, objects, rel_of


# ------------------------------------------------------------ physical layer
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def quote(name: str) -> str:
    return name if _IDENT.match(name) else '"' + name.replace('"', '""') + '"'


def _relation_ref(rel: ET.Element, what: str) -> str:
    parts = _split_qualified(rel.get("table") or "")
    if not parts:
        raise ModelError(f"{what}: relation {rel.get('name')!r} names no table")
    return ".".join(parts)


def _physical(rel: ET.Element, members: list[_Meta], connections, what: str):
    """-> (source, source_sql) for one logical table's physical relation."""
    kind = rel.get("type")
    if rel.get("connection") not in (None, *connections):
        raise Unsupported("cross-database join", V, what)
    if kind == "table":
        return _relation_ref(rel, what), None
    if kind == "text":
        text = (rel.text or "").strip()
        if "<[Parameters]." in text:
            raise Unsupported("parameter in custom SQL", V, what)
        return None, text
    if kind == "join":
        return None, _join_sql(rel, members, connections, what)
    if kind in ("union", "pivot", "collection", "project", "subquery"):
        raise Unsupported(f"{kind} relation", V, what)
    raise Unsupported(f"relation type {kind!r}", V, what)


def _join_from(rel: ET.Element, connections, what: str) -> str:
    kind = rel.get("type")
    if rel.get("connection") not in (None, *connections):
        raise Unsupported("cross-database join", V, what)
    if kind == "table":
        return f"{_qualified(_relation_ref(rel, what))} AS {quote(rel.get('name'))}"
    if kind == "text":
        return f"(\n{(rel.text or '').strip()}\n) AS {quote(rel.get('name'))}"
    if kind == "join":
        children = [c for c in rel if c.tag == "relation"]
        clause = rel.find("clause")
        if len(children) != 2 or clause is None:
            raise ModelError(f"{what}: a join needs two relations and a clause")
        how = {"inner": "INNER JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN",
               "full": "FULL OUTER JOIN"}.get(rel.get("join", "inner"))
        if how is None:
            raise Unsupported(f"{rel.get('join')} join", V, what)
        cond = _join_condition(clause.find("expression"), what)
        return (f"{_join_from(children[0], connections, what)}\n"
                f"{how} {_join_from(children[1], connections, what)} ON {cond}")
    raise Unsupported(f"{kind} relation inside a join", V, what)


def _qualified(ref: str) -> str:
    return ".".join(quote(p) for p in ref.split("."))


_JOIN_OPS = {"=": "=", "<>": "<>", "<": "<", ">": ">", "<=": "<=", ">=": ">=",
             "AND": "AND", "OR": "OR"}


def _join_condition(expr: ET.Element | None, what: str) -> str:
    if expr is None:
        raise ModelError(f"{what}: join clause without an expression")
    op = expr.get("op")
    kids = expr.findall("expression")
    if not kids:
        parts = _split_qualified(op)
        if len(parts) != 2 or not op.startswith("["):
            raise Unsupported("join calculation", V, f"{what}: join on {op!r}")
        return f"{quote(parts[0])}.{quote(parts[1])}"
    sql_op = _JOIN_OPS.get(op.upper() if op.isalpha() else op)
    if sql_op is None or len(kids) != 2:
        raise Unsupported("join calculation", V, f"{what}: join operator {op!r}")
    left, right = (_join_condition(k, what) for k in kids)
    return f"({left} {sql_op} {right})" if sql_op in ("AND", "OR") else f"{left} {sql_op} {right}"


def _join_sql(rel: ET.Element, members: list[_Meta], connections, what: str) -> str:
    select = ",\n  ".join(
        f"{quote(m.parent)}.{quote(m.remote)} AS {quote(m.local)}" for m in members)
    return f"SELECT\n  {select}\nFROM {_join_from(rel, connections, what)}"


# --------------------------------------------------------------- relationships
def _relationships(elem, names: calc.Names, objects, what) -> tuple[Relationship, ...]:
    out = []
    for rel in elem.findall("object-graph/relationships/relationship"):
        expr = rel.find("expression")
        first, second = rel.find("first-end-point"), rel.find("second-end-point")
        if expr is None or first is None or second is None:
            raise ModelError(f"{what}: malformed relationship")
        if expr.get("op") != "=" or len(expr.findall("expression")) != 2:
            raise Unsupported("relationship on more than one field pair or a non-equality",
                              V, f"{what}: relationship operator {expr.get('op')!r}")
        refs = []
        for side in expr.findall("expression"):
            if side.findall("expression"):
                raise Unsupported("relationship calculation", V, what)
            ref = names.resolve(_unbracket(side.get("op")), allow_captions=False)
            if not isinstance(ref, Col):
                raise ModelError(f"{what}: relationship on a non-column {side.get('op')}")
            refs.append(ref)
        a_obj, b_obj = objects.get(first.get("object-id")), objects.get(second.get("object-id"))
        a, b = refs
        if {a.table, b.table} != {a_obj, b_obj}:
            # The expression's sides follow the end points in Desktop's files;
            # re-pair them by table rather than trust the order.
            if a.table == b_obj and b.table == a_obj:
                a, b = b, a
            else:
                raise ModelError(f"{what}: relationship fields are not in its tables")
        elif a.table != a_obj:
            a, b = b, a
        a_one = first.get("unique-key") == "true"
        b_one = second.get("unique-key") == "true"
        if a_one and b_one:
            out.append(Relationship(a.table, a.column, b.table, b.column,
                                    cardinality="one_to_one"))
        elif b_one:
            out.append(Relationship(a.table, a.column, b.table, b.column))
        elif a_one:
            out.append(Relationship(b.table, b.column, a.table, a.column))
        else:
            raise Unsupported(
                "Many-to-Many relationship cardinality", V,
                f"{what}: {a.table}.{a.column} = {b.table}.{b.column} has no side set to "
                f"One (Performance Options), so the direction filters flow in is unknown; "
                f"set unique-key='true' on the end point whose key is unique")
    return tuple(out)


# ------------------------------------------------------------- calculations
def _calculations(ds: DataSource, elem: ET.Element, strict: bool, what: str) -> None:
    """Lower every calculated field, in dependency order, into the model."""
    calcs: dict[str, ET.Element] = {}
    for c in elem.findall("column"):
        calc_elem = c.find("calculation")
        if calc_elem is None:
            continue
        name = _unbracket(c.get("name"))
        cls = calc_elem.get("class")
        if cls != "tableau":
            feature = {"categorical-bin": "group", "bin": "bin"}.get(cls, f"{cls} calculation")
            _defer(ds, name, Unsupported(feature, V, f"{what}: [{name}]"), strict)
            continue
        calcs[name] = c
    # `<group>` elements are sets - user-made ones, and the hidden ones Desktop
    # creates for actions and exclusions. Defining one changes no number, so a set
    # is refused only when something uses it, strict or not.
    for g in elem.findall("group"):
        name = _unbracket(g.get("name") or "")
        if name:
            _defer(ds, name, Unsupported("set", V, f"{what}: [{name}]"), strict=False)
    trees = {}
    for name, c in calcs.items():
        try:
            trees[name] = calc.parse_tree(c.find("calculation").get("formula") or "")
        except (Unsupported, ModelError) as exc:
            if isinstance(exc, ModelError) and strict:
                raise
            _defer(ds, name, exc if isinstance(exc, Unsupported)
                   else Unsupported("calculation that does not parse", V, str(exc)), strict)
    order = _topo(trees, what)
    single = ds.model.tables[0].name if len(ds.model.tables) == 1 else None
    for name in order:
        _lower_calc(ds, name, calcs[name], trees[name], single, strict, what)
    # Lowered in dependency order, kept in document order: Desktop writes the
    # calcs sorted by name, and its names grow with creation time.
    position = {name: i for i, name in enumerate(calcs)}
    measure_pos = {ds.names.refs[n].name: position[n] for n in calcs
                   if isinstance(ds.names.refs.get(n), MeasureRef)}
    ds.model = replace(ds.model, measures=tuple(sorted(
        ds.model.measures, key=lambda m: measure_pos.get(m.name, -1))))
    ds.model = replace(ds.model, tables=tuple(
        replace(t, columns=tuple(sorted(
            t.columns, key=lambda c: (c.expr is not None, position.get(c.source, -1)))))
        for t in ds.model.tables))


def _lower_calc(ds, name, c, tree, single, strict, what):
    """Lower one calculated field into a measure or a calculated column."""
    cap = c.get("caption") or name
    try:
        for ref in calc.field_refs(tree):
            if ref.qualifier is None and ref.name in ds.deferred:
                raise ds.deferred[ref.name]
        lowering = calc.Lowering(ds.model, ds.names, what=f"{what}: [{cap}]")
        expr, level = lowering.lower(tree)
    except Unsupported as exc:
        _defer(ds, name, exc, strict)
        return
    if level == "agg":
        measure = Measure(name=cap, table=_home_of_measure(expr, ds.model), expr=expr,
                          format_string=format_from_tableau(c.get("default-format")),
                          description=_desc(c))
        ds.model = replace(ds.model, measures=ds.model.measures + (measure,))
        ds.names.add(name, MeasureRef(cap), caption=cap)
        return
    tables = calc.col_tables(expr)
    if not tables:
        if single is None:
            _defer(ds, name, Unsupported(
                "row-level calculation with no field in a multi-table data source", V,
                f"{what}: [{cap}] \"lives in the unassigned area\" of the data model "
                f"and has no table of its own"), strict)
            return
        home = single
    else:
        try:
            home = calc.home_table(tables, ds.model, f"{what}: [{cap}]")
            expr = calc.relatedify(expr, home)
            calc.check_cross_table(expr, f"{what}: [{cap}]")
        except Unsupported as exc:
            _defer(ds, name, exc, strict)
            return
    dtype = _calc_type(c.get("datatype"), calc.infer_type(expr, ds.model))
    column = Column(name=cap, source=name, data_type=dtype,
                    hidden=c.get("hidden") == "true", expr=expr, description=_desc(c))
    tables_new = tuple(replace(t, columns=t.columns + (column,)) if t.name == home else t
                       for t in ds.model.tables)
    ds.model = replace(ds.model, tables=tables_new)
    ds.names.add(name, Col(home, cap), caption=cap)


def _defer(ds: DataSource, name: str, exc: Unsupported, strict: bool) -> None:
    if strict:
        raise exc
    ds.deferred[name] = exc
    ds.names.unsupported[name] = exc


def _calc_type(declared: str | None, inferred: DataType | None) -> DataType:
    if declared == "real":
        return inferred if inferred in ("decimal", "float") else "float"
    if declared in _FROM_LOCAL:
        return _FROM_LOCAL[declared]
    return inferred or "string"


def _home_of_measure(expr: Expr, model: Model) -> str:
    tables = calc.base_tables(expr, model)
    for t in model.tables:
        if t.name in tables:
            return t.name
    return model.tables[0].name


def _topo(trees: dict, what: str) -> list[str]:
    order, state = [], {}

    def visit(name, path):
        if state.get(name) == "done":
            return
        if state.get(name) == "active":
            raise ModelError(f"{what}: calculated fields reference each other in a cycle: "
                             f"{' -> '.join(path + [name])}")
        state[name] = "active"
        for ref in calc.field_refs(trees[name]):
            if ref.qualifier is None and ref.name in trees:
                visit(ref.name, path + [name])
        state[name] = "done"
        order.append(name)

    for name in trees:
        visit(name, [])
    return order


# ------------------------------------------------------------------ sqlproxy
def _read_sqlproxy(elem, conn, caption, ds_name, what, strict, resolve_published):
    published = conn.get("dbname") or caption
    loc = elem.find("repository-location")
    if loc is not None and loc.get("id"):
        published = loc.get("id")
    if resolve_published is None:
        raise ModelError(f"{what} is a published data source ({published!r}); read the "
                         f"workbook together with that data source's .tds")
    base = resolve_published(published)
    if base is None:
        raise ModelError(f"{what}: published data source {published!r} not found")
    ds = DataSource(model=base.model, names=_copy_names(base.names), name=ds_name,
                    caption=caption, connection_class=base.connection_class,
                    extract=base.extract, published=published,
                    deferred=dict(base.deferred), objects=dict(base.objects))
    # Calculations the workbook adds on top of the published source (Desktop
    # caches the published ones too; the published definition wins).
    for c in elem.findall("column"):
        calc_elem = c.find("calculation")
        if calc_elem is None:
            continue
        name = _unbracket(c.get("name"))
        if name in ds.names.refs or name in ds.deferred:
            continue
        cap = c.get("caption") or name
        try:
            if calc_elem.get("class") != "tableau":
                raise Unsupported({"categorical-bin": "group", "bin": "bin"}.get(
                    calc_elem.get("class"), "calculation"), V, f"{what}: [{cap}]")
            expr, level, _home = calc.lower_measure(calc_elem.get("formula") or "", ds.model,
                                                    ds.names, f"{what}: [{cap}]")
        except Unsupported as exc:
            _defer(ds, name, exc, strict)
            continue
        if level != "agg":
            _defer(ds, name, Unsupported(
                "row-level calculation added to a published data source in a workbook", V,
                f"{what}: [{cap}]; define it in the published data source"), strict)
            continue
        ds.local_calcs[name] = (cap, expr)
    return ds


def _copy_names(names: calc.Names) -> calc.Names:
    out = calc.Names()
    out.refs, out.of_col = dict(names.refs), dict(names.of_col)
    out.of_measure, out.of_count = dict(names.of_measure), dict(names.of_count)
    out.captions, out.unsupported = dict(names.captions), dict(names.unsupported)
    out.counts = dict(names.counts)
    return out


# ================================================================ writing
def names_for(model: Model) -> calc.Names:
    """The field names `write` gives `model`, count fields included."""
    names = calc.Names.for_model(model)
    for table in model.tables:
        names.add(object_id(table, f"{model.name}/{table.name}"), calc.CountRef(table.name),
                  caption=f"{table.name} (Count)")
    return names


def content_url(name: str) -> str:
    """Tableau Server's contentUrl for a name: spaces and punctuation removed
    ("Executive Overview" -> "ExecutiveOverview")."""
    return re.sub(r"[^A-Za-z0-9_-]", "", name) or "untitled"


def write(model: Model, connection: Connection, *, root: bool, extract: bool = False,
          local_calcs: dict[str, tuple[str, Expr]] | None = None) -> ET.Element:
    """The `<datasource>` element for `model`: the root of a .tds when `root`,
    else the copy a workbook embeds."""
    _check_writable(model)
    names = names_for(model)
    ds_id = "federated." + base36(f"datasource/{model.name}")
    conn_name = f"{connection.cls}.{base36(f'connection/{model.name}')}"
    attrs = {"inline": "true", "version": FILE_VERSION}
    if root:
        attrs.update({"formatted-name": ds_id, "source-platform": "win"})
    else:
        attrs.update({"caption": model.name, "name": ds_id})
    ds = ET.Element("datasource", attrs)
    if root:
        manifest = sub(ds, "document-format-change-manifest")
        features = ["ObjectModelEncapsulateLegacy", "ObjectModelExtractV2",
                    "ObjectModelTableType", "SchemaViewerObjectModel"]
        if model.relationships:
            features.insert(2, "ObjectModelRelationshipPerfOptions")
        for feature in features:
            sub(manifest, feature)
    conn = sub(ds, "connection", {"class": "federated"})
    ncs = sub(conn, "named-connections")
    nc = sub(ncs, "named-connection", {"caption": connection.caption, "name": conn_name})
    sub(nc, "connection", {"class": connection.cls, **dict(connection.attrs)})
    oids = {t.name: object_id(t, f"{model.name}/{t.name}") for t in model.tables}
    relnames = calc.relation_names(model)
    conn.append(_top_relation(model, conn_name, relnames))
    if len(model.tables) > 1:
        cols = sub(conn, "cols")
        maps = sorted((calc.bracket(names.of_col[(t.name, c.name)]),
                       f"{calc.bracket(relnames[t.name])}.{calc.bracket(c.source)}")
                      for t in model.tables for c in t.columns if c.expr is None)
        for key, value in maps:
            sub(cols, "map", {"key": key, "value": value})
    records = sub(conn, "metadata-records")
    ordinal = 0
    for table in model.tables:
        for column in table.columns:
            if column.expr is not None:
                continue
            _metadata_record(records, column.source, column.data_type,
                             names.of_col[(table.name, column.name)], relnames[table.name],
                             ordinal, oids[table.name])
            ordinal += 1
    sub(ds, "aliases", {"enabled": "yes"})
    entries = _column_entries(model, names, local_calcs or {})
    for table in model.tables:
        entries.append((f"[{calc.COUNT_QUALIFIER}].{calc.bracket(oids[table.name])}",
                        ET.Element("column", {
                            "caption": table.name, "datatype": "table",
                            "name": f"[{calc.COUNT_QUALIFIER}].{calc.bracket(oids[table.name])}",
                            "role": "measure", "type": "quantitative"})))
    for _, elem in sorted(entries, key=lambda kv: kv[0]):
        ds.append(elem)
    # The XSD's order: columns, ..., extract, layout, semantic-values, object-graph.
    if extract:
        ext = sub(ds, "extract", {"count": "-1", "enabled": "true", "object-id": "",
                                  "units": "records"})
        econn = sub(ext, "connection", {
            "access_mode": "readonly", "author-locale": "en_US", "class": "hyper",
            "dbname": f"Data/Extracts/{content_url(model.name)}.hyper",
            "default-settings": "hyper", "schema": "Extract", "sslmode": "",
            "tablename": "Extract", "username": "tableau_internal_user"})
        coll = sub(econn, "relation", {"type": "collection"})
        for table in model.tables:
            sub(coll, "relation", {"name": oids[table.name],
                                   "table": f"[Extract].{calc.bracket(oids[table.name])}",
                                   "type": "table"})
    sub(ds, "layout", {"dim-ordering": "alphabetic", "measure-ordering": "alphabetic",
                       "show-structure": "true"})
    sv = sub(ds, "semantic-values")
    sub(sv, "semantic-value", {"key": "[Country].[Name]", "value": '"United States"'})
    graph = sub(ds, "object-graph")
    objs = sub(graph, "objects")
    for table in model.tables:
        obj = sub(objs, "object", {"caption": table.name, "id": oids[table.name]})
        props = sub(obj, "properties", {"context": ""})
        props.append(_table_relation(table, conn_name, relnames[table.name]))
        if extract:
            eprops = sub(obj, "properties", {"context": "extract"})
            sub(eprops, "relation", {"name": oids[table.name],
                                     "table": f"[Extract].{calc.bracket(oids[table.name])}",
                                     "type": "table"})
    if model.relationships:
        rels = sub(graph, "relationships")
        for r in model.relationships:
            rel = sub(rels, "relationship")
            e = sub(rel, "expression", {"op": "="})
            sub(e, "expression", {"op": calc.bracket(names.of_col[(r.from_table, r.from_column)])})
            sub(e, "expression", {"op": calc.bracket(names.of_col[(r.to_table, r.to_column)])})
            first = {"object-id": oids[r.from_table]}
            if r.cardinality == "one_to_one":
                first["unique-key"] = "true"
            sub(rel, "first-end-point", first)
            sub(rel, "second-end-point", {"object-id": oids[r.to_table], "unique-key": "true"})
    return ds


def write_sqlproxy(model: Model, *, server: str, port: int, channel: str = "http",
                   local_calcs: dict[str, tuple[str, Expr]] | None = None) -> ET.Element:
    """The copy of a PUBLISHED data source a workbook carries: a `sqlproxy`
    connection to the server plus the field list Desktop caches, every field
    flattened onto the one `[sqlproxy]` relation."""
    names = names_for(model)
    url = content_url(model.name)
    ds = ET.Element("datasource", {"caption": model.name, "inline": "true",
                                   "name": "sqlproxy." + base36(f"sqlproxy/{model.name}"),
                                   "version": FILE_VERSION})
    sub(ds, "repository-location", {"derived-from": f"/datasources/{url}?rev=1.0", "id": url,
                                    "path": "/datasources", "revision": "1.0"})
    conn = sub(ds, "connection", {
        "channel": channel, "class": "sqlproxy", "dbname": url, "directory": "/dataserver",
        "port": str(port), "server": server, "server-oauth": "", "username": "",
        "workgroup-auth-mode": "prompt"})
    sub(conn, "relation", {"name": "sqlproxy", "table": "[sqlproxy]", "type": "table"})
    records = sub(conn, "metadata-records")
    ordinal = 0
    for table in model.tables:
        for column in table.columns:
            local = names.of_col[(table.name, column.name)]
            _metadata_record(records, local, column.data_type, local, "sqlproxy", ordinal, None)
            ordinal += 1
    sub(ds, "aliases", {"enabled": "yes"})
    for _, elem in sorted(_column_entries(model, names, local_calcs or {}),
                          key=lambda kv: kv[0]):
        ds.append(elem)
    sub(ds, "layout", {"dim-ordering": "alphabetic", "measure-ordering": "alphabetic",
                       "show-structure": "true"})
    sv = sub(ds, "semantic-values")
    sub(sv, "semantic-value", {"key": "[Country].[Name]", "value": '"United States"'})
    return ds


def _metadata_record(records: ET.Element, remote: str, dtype: DataType, local: str,
                     parent: str, ordinal: int, oid: str | None) -> None:
    rec = sub(records, "metadata-record", {"class": "column"})
    sub(rec, "remote-name", text=remote)
    sub(rec, "remote-type", text=_REMOTE_TYPE[dtype])
    sub(rec, "local-name", text=calc.bracket(local))
    sub(rec, "parent-name", text=calc.bracket(parent))
    sub(rec, "remote-alias", text=remote)
    sub(rec, "ordinal", text=str(ordinal))
    sub(rec, "local-type", text=calc.tableau_datatype(dtype))
    sub(rec, "aggregation", text="Sum" if dtype in ("int", "decimal", "float")
        else "Year" if dtype in ("date", "timestamp") else "Count")
    if dtype == "decimal":
        sub(rec, "precision", text="12")
        sub(rec, "scale", text="2")
    sub(rec, "contains-null", text="true")
    if oid is not None:
        sub(rec, "object-id", text=calc.bracket(oid))


def _check_writable(model: Model) -> None:
    for t in model.tables:
        if t.is_date_table:
            raise Unsupported("date table (Mark as Date Table)", V,
                              f"{t.name!r}: Tableau has no date-table setting; drop "
                              f"is_date_table for the Tableau rendition")
        if t.hidden:
            raise Unsupported("hidden logical table", V, t.name)
    for r in model.relationships:
        if not r.active:
            raise Unsupported("inactive relationship", V,
                              f"{r.from_table}.{r.from_column} -> {r.to_table}.{r.to_column}: "
                              f"every Tableau relationship is in use")
        if r.cross_filter == "both":
            raise Unsupported("bidirectional cross-filter", V)


def _table_relation(table: Table, conn_name: str, relname: str) -> ET.Element:
    if table.source is not None:
        return ET.Element("relation", {
            "connection": conn_name, "name": relname, "type": "table",
            "table": ".".join(calc.bracket(p) for p in table.source.split("."))})
    rel = ET.Element("relation", {"connection": conn_name, "name": relname, "type": "text"})
    rel.text = table.source_sql
    return rel


def _top_relation(model: Model, conn_name: str, relnames: dict[str, str]) -> ET.Element:
    """The connection-level relation: the single table, or (several logical
    tables) the `collection` Desktop writes for readers older than 2020.2."""
    if len(model.tables) == 1:
        t = model.tables[0]
        return _table_relation(t, conn_name, relnames[t.name])
    coll = ET.Element("relation", {"type": "collection"})
    for t in model.tables:
        coll.append(_table_relation(t, conn_name, relnames[t.name]))
    return coll


def _column_entries(model: Model, names: calc.Names,
                    local_calcs: dict[str, tuple[str, Expr]]) -> list[tuple[str, ET.Element]]:
    """(name, `<column>`) for renamed/hidden/described fields and every
    calculation; the caller sorts them by name, as Desktop does."""
    formatter = calc.Formatter(model, names)
    entries: list[tuple[str, ET.Element]] = []
    for table in model.tables:
        for column in table.columns:
            local = names.of_col[(table.name, column.name)]
            if column.expr is None and not column.hidden and column.name == column.source \
                    and not column.description:
                continue
            attrs = {"datatype": calc.tableau_datatype(column.data_type),
                     "name": calc.bracket(local), **_role(column.data_type)}
            # The reader names a base column after its warehouse column and a
            # calculation after its field name unless a caption says otherwise.
            if column.name != (local if column.expr is not None else column.source):
                attrs["caption"] = column.name
            if column.hidden:
                attrs["hidden"] = "true"
            elem = ET.Element("column", attrs)
            if column.expr is not None:
                sub(elem, "calculation", {"class": "tableau",
                                          "formula": formatter.fmt(column.expr)})
            _add_desc(elem, column.description)
            entries.append((calc.bracket(local), elem))
    for measure in model.measures:
        local = names.of_measure[measure.name]
        attrs = {"caption": measure.name,
                 "datatype": calc.tableau_datatype(calc.infer_type(measure.expr, model)),
                 "name": calc.bracket(local), "role": "measure", "type": "quantitative"}
        df = format_to_tableau(measure.format_string)
        if df:
            attrs["default-format"] = df
        elem = ET.Element("column", attrs)
        sub(elem, "calculation", {"class": "tableau", "formula": formatter.fmt(measure.expr)})
        _add_desc(elem, measure.description)
        entries.append((calc.bracket(local), elem))
    for local, (caption, expr) in local_calcs.items():
        elem = ET.Element("column", {
            "caption": caption, "datatype": calc.tableau_datatype(calc.infer_type(expr, model)),
            "name": calc.bracket(local), "role": "measure", "type": "quantitative"})
        sub(elem, "calculation", {"class": "tableau", "formula": formatter.fmt(expr)})
        entries.append((calc.bracket(local), elem))
    return entries


def _role(dtype: DataType) -> dict[str, str]:
    if dtype in ("int", "decimal", "float"):
        return {"role": "measure", "type": "quantitative"}
    if dtype in ("date", "timestamp"):
        return {"role": "dimension", "type": "ordinal"}
    return {"role": "dimension", "type": "nominal"}


def _add_desc(elem: ET.Element, text: str) -> None:
    if text:
        ft = sub(sub(elem, "desc"), "formatted-text")
        sub(ft, "run", text=text)
