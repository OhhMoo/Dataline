"""Databricks SQL on a seeded DuckDB lakehouse: translation, loud fences, the catalog.

The fake Databricks workspace (`native/services/databricks.py`) answers SQL the way a
Databricks SQL warehouse does, on a COPY of the world's warehouse kept in the service's
own state. This module is that SQL layer; it knows nothing about HTTP.

THE NAMESPACE. Unity Catalog's three levels, `catalog.schema.table`. There is one
catalog, `main`; `hive_metastore` is an alias for it (a workspace's legacy catalog name,
which dbt-databricks and older notebooks still type). Each schema of the world's
warehouse is a schema of `main` - `main.warehouse.fct_orders` is the world's
`warehouse.fct_orders` - plus `default`, which every Databricks catalog has and a session
lands in, and `information_schema`, Unity Catalog's view of the catalog itself
(`system.information_schema.*` reads the same views).

DuckDB cannot attach a database under the name `main` (reserved), so the copy is
attached as `ENGINE_CATALOG` and every catalog reference is rewritten onto it; no answer
and no error message ever names it (`SqlError` maps it back).

TRANSLATION (`translate`) is sqlglot's `databricks` -> `duckdb`, on the parsed tree, with
sqlglot told to RAISE on anything it cannot express - a construct the translator would
drop is refused by name instead of answered wrongly. Before generation the tree is
fenced and rewritten:

  refused by name (`Unsupported`): Delta-only SQL, because there is no Delta log and an
    answer would be invented - DESCRIBE HISTORY, DESCRIBE DETAIL, VERSION AS OF,
    TIMESTAMP AS OF, OPTIMIZE (and its ZORDER BY), VACUUM, RESTORE, SHALLOW/DEEP CLONE,
    table_changes(), CONVERT TO DELTA; and anything that reaches outside the
    lakehouse - COPY INTO, LOCATION (external tables), a non-Delta `USING <format>`,
    system tables other than information_schema;
  accepted and dropped: the clauses that only decide a Delta table's physical layout or
    its metadata - USING DELTA, PARTITIONED BY, CLUSTER BY, TBLPROPERTIES, a column or
    table COMMENT in CREATE TABLE - none of which changes what a query returns;
  FAIL-CLOSED for everything else (`alkera_gateway.databricks_functions`): every
    function and expression must be on the vetted lists - translated as sqlglot does
    it where that equals Databricks' documented result, rewritten where it does not
    (integral CAST, dayofweek/weekday, date_add, timestampdiff, substring, division by
    zero, current_date on the world's clock) - and anything off them is refused naming
    it;
  qualified: an unqualified table name is the session's schema in catalog `main` -
    unless a CTE of that name is visible in the reference's OWN scope - and a two-part
    name a schema of `main`: never one of the engine's own catalogs, nor a DuckDB table
    function called without parentheses;
  typed: the statement is typed as Spark types it first (databricks_types), so the
    rewrites land on Spark's result types and rounding (DECIMAL division and avg exact,
    HALF_UP), and unaliased results keep Spark's column names (`spark_name`).

STATEMENTS a warehouse runs here (`check_statement`): queries, DML, CREATE/DROP of a
table, view or schema, TRUNCATE TABLE, COMMENT ON - anything else sqlglot parses (a
DuckDB SEQUENCE, MACRO, SECRET, a TEMPORARY view, PRAGMA, ATTACH) is refused by name.

COMMANDS a SQL warehouse answers that are not queries - USE, SET, SHOW
CATALOGS/SCHEMAS/TABLES/VIEWS/COLUMNS, DESCRIBE TABLE - are answered by `Lakehouse`
in Databricks' own column names (`databaseName`, `tableName`, `isTemporary`, ...), from
the same information_schema views a query reads, so SHOW, information_schema and the
Unity Catalog REST API can never disagree.

THE WORLD IS NEVER WRITTEN. `Lakehouse.seed` copies the world's warehouse file table by
table (a view's rows, too: its definition names the world's own catalog) into the copy,
reading the world file READ_ONLY; every statement after that runs on the copy. The
connection then has external access switched off and its configuration locked, so no
statement can ATTACH, read or write a file - the world's included.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, ParseError, UnsupportedError

from . import databricks_functions as dbf
from . import databricks_types as dbt

#: The one Unity Catalog catalog, and the legacy name that means the same.
CATALOG = "main"
CATALOG_ALIASES = ("main", "hive_metastore")
#: The schema a session starts in, which every Databricks catalog has.
DEFAULT_SCHEMA = "default"
#: Where the copy is attached: DuckDB reserves the name `main`.
ENGINE_CATALOG = "dbx_main"
#: The information_schema views, in the engine's in-memory catalog.
INFO_CATALOG, INFO_SCHEMA = "memory", "dbx_information_schema"
INFO_VIEWS = ("catalogs", "schemata", "tables", "views", "columns")
#: DuckDB's own default schema in a database file. Never a Databricks schema: a world
#: table in it is copied into `default`.
ENGINE_DEFAULT_SCHEMA = "main"
#: Who every statement runs as: the workspace's token owner (the box's one fake
#: identity, `native_workspace.FAKE_USER`).
USER = "alkera-local-dev"
#: Statement kinds a SQL warehouse runs. Anything else sqlglot parses (a DuckDB-only
#: ATTACH, PRAGMA, INSTALL, EXPORT ...) is not Databricks SQL and is refused.
QUERY_KINDS = (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Values)
DML_KINDS = (exp.Insert, exp.Update, exp.Delete, exp.Merge)
DDL_KINDS = (exp.Create, exp.Drop, exp.TruncateTable, exp.Comment)


class Unsupported(Exception):
    """A Databricks feature this lakehouse refuses, by name."""

    def __init__(self, feature: str, why: str = ""):
        self.feature = feature
        self.why = why
        super().__init__(f"Databricks feature not available in this environment: {feature}"
                         + (f" - {why}" if why else ""))


class SqlError(Exception):
    """A statement Databricks would fail, in its spelling: `[CLASS] message SQLSTATE: x`."""

    def __init__(self, error_class: str, message: str, sqlstate: str = "42000"):
        self.error_class = error_class
        self.sqlstate = sqlstate
        self.detail = message
        super().__init__(f"[{error_class}] {message} SQLSTATE: {sqlstate}")


@dataclass(frozen=True)
class Context:
    """A session's place in the namespace: what an unqualified name resolves in, and
    the world's clock (`now`, ISO, the episode's virtual time) that current_date and
    current_timestamp read - never the host's."""

    catalog: str = CATALOG
    schema: str = DEFAULT_SCHEMA
    user: str = USER
    now: str | None = None


@dataclass(frozen=True)
class Column:
    """One result column, in the Statement Execution API's manifest terms."""

    name: str
    type_name: str           # INT, LONG, DECIMAL, STRING, DATE, TIMESTAMP, ...
    type_text: str           # int, bigint, decimal(18,2), string, ...
    type_precision: int | None = None
    type_scale: int | None = None


@dataclass
class Result:
    columns: list[Column] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    #: rows a DML statement changed, None for anything else
    rows_affected: int | None = None


# --------------------------------------------------------------------------- types
_DECIMAL = re.compile(r"DECIMAL\((\d+),\s*(\d+)\)")
_SIMPLE_TYPES = {
    "BOOLEAN": ("BOOLEAN", "boolean"),
    "TINYINT": ("BYTE", "tinyint"), "UTINYINT": ("SHORT", "smallint"),
    "SMALLINT": ("SHORT", "smallint"), "USMALLINT": ("INT", "int"),
    "INTEGER": ("INT", "int"), "UINTEGER": ("LONG", "bigint"),
    # A DuckDB sum() over integers is HUGEINT; Databricks' is bigint.
    "BIGINT": ("LONG", "bigint"), "HUGEINT": ("LONG", "bigint"),
    "UBIGINT": ("DECIMAL", "decimal(20,0)"),
    "FLOAT": ("FLOAT", "float"), "DOUBLE": ("DOUBLE", "double"),
    "VARCHAR": ("STRING", "string"), "UUID": ("STRING", "string"),
    "DATE": ("DATE", "date"),
    "TIMESTAMP": ("TIMESTAMP", "timestamp"), "TIMESTAMP WITH TIME ZONE": ("TIMESTAMP", "timestamp"),
    "TIMESTAMP_S": ("TIMESTAMP", "timestamp"), "TIMESTAMP_MS": ("TIMESTAMP", "timestamp"),
    "TIMESTAMP_NS": ("TIMESTAMP", "timestamp"),
    "BLOB": ("BINARY", "binary"), "INTERVAL": ("INTERVAL", "interval"),
    "TIME": ("STRING", "string"), "NULL": ("NULL", "void"),
}


def databricks_type(duck: str) -> tuple[str, str, int | None, int | None]:
    """A DuckDB type -> (type_name, type_text, precision, scale) as Databricks names it."""
    duck = str(duck).strip().upper()
    found = _DECIMAL.fullmatch(duck)
    if found:
        precision, scale = int(found.group(1)), int(found.group(2))
        return "DECIMAL", f"decimal({precision},{scale})", precision, scale
    if duck == "DECIMAL":
        return "DECIMAL", "decimal(18,3)", 18, 3
    if duck in _SIMPLE_TYPES:
        name, text = _SIMPLE_TYPES[duck]
        return name, text, None, None
    if duck.endswith("[]"):
        inner = databricks_type(duck[:-2])[1]
        return "ARRAY", f"array<{inner}>", None, None
    if duck.startswith("STRUCT"):
        return "STRUCT", duck.lower().replace("struct(", "struct<").rstrip(")") + ">", None, None
    if duck.startswith("MAP"):
        return "MAP", duck.lower().replace("map(", "map<").rstrip(")") + ">", None, None
    return "STRING", "string", None, None


def column(name: str, duck: str) -> Column:
    type_name, type_text, precision, scale = databricks_type(duck)
    return Column(name, type_name, type_text, precision, scale)


def json_value(value):
    """One cell as the JSON_ARRAY format carries it: a string, or null."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.UTC).replace(tzinfo=None)
        return value.isoformat() + "Z"
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, default=str, separators=(",", ":"))
    if isinstance(value, float):
        return dbf.java_double(value)
    return str(value)


def render(value, col: Column | None = None):
    """One cell of a result column as the JSON_ARRAY format carries it: a FLOAT as
    Java's Float.toString writes it (`1.1`, not the DOUBLE it widens to), a DOUBLE as
    Double.toString (`1.0E20`), everything else as `json_value`."""
    if isinstance(value, float) and col is not None and col.type_name == "FLOAT":
        return dbf.java_double(value, single=True)
    return json_value(value)


# ----------------------------------------------------------------------- fences
#: Leading-verb and anywhere-in-text fences, matched on the statement with comments and
#: string literals blanked out: (pattern, feature, why). First match wins, so the
#: ZORDER form is named before plain OPTIMIZE.
_DELTA_LOG = "this lakehouse keeps no Delta transaction log"
_TEXT_FENCES = [
    (r"^\s*DESC(?:RIBE)?\s+HISTORY\b", "DESCRIBE HISTORY", _DELTA_LOG),
    (r"^\s*DESC(?:RIBE)?\s+DETAIL\b", "DESCRIBE DETAIL", "no Delta files to describe"),
    (r"^\s*OPTIMIZE\b.*\bZORDER\b", "OPTIMIZE ... ZORDER BY", "no Delta files to rewrite"),
    (r"^\s*OPTIMIZE\b", "OPTIMIZE", "no Delta files to compact"),
    (r"^\s*VACUUM\b", "VACUUM", "no Delta files to remove"),
    (r"^\s*RESTORE\b", "RESTORE", _DELTA_LOG),
    (r"^\s*CONVERT\s+TO\s+DELTA\b", "CONVERT TO DELTA", "no files outside the lakehouse"),
    (r"^\s*COPY\s+INTO\b", "COPY INTO", "no cloud storage to load from"),
    (r"^\s*(?:FSCK|MSCK)\b", "FSCK/MSCK REPAIR", "no file layout to repair"),
    (r"\b(?:SHALLOW|DEEP)\s+CLONE\b", "SHALLOW/DEEP CLONE", _DELTA_LOG),
    (r"\bCLONE\b", "CLONE", _DELTA_LOG),
    # VERSION/TIMESTAMP AS OF are found on the parsed tree (`exp.Version`), where an
    # alias or a cast cannot be mistaken for them.
    (r"\btable_changes\s*\(", "table_changes (Change Data Feed)", _DELTA_LOG),
    (r"\bread_files\s*\(", "read_files", "no cloud storage to read"),
]
_TEXT_FENCES = [(re.compile(p, re.I | re.S), f, w) for p, f, w in _TEXT_FENCES]
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_STRINGS = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"", re.S)


def _bare(sql: str) -> str:
    """The statement with comments removed and string literals blanked."""
    return _STRINGS.sub("''", _COMMENTS.sub(" ", sql))


def fence(sql: str) -> None:
    """Refuse Delta-only and outside-the-lakehouse SQL by name, before parsing."""
    bare = _bare(sql)
    for pattern, feature, why in _TEXT_FENCES:
        if pattern.search(bare):
            if feature == "CLONE" and not re.match(r"^\s*(CREATE|REPLACE)\b", bare, re.I):
                continue
            raise Unsupported(feature, why)


def _fence_tree(tree: exp.Expression) -> None:
    for node in tree.find_all(exp.Version):
        raise Unsupported(f"{str(node.args.get('this')).upper()} AS OF (time travel)",
                          _DELTA_LOG)
    for _ in tree.find_all(exp.Clone):
        raise Unsupported("SHALLOW/DEEP CLONE", _DELTA_LOG)
    for node in tree.find_all(exp.FileFormatProperty):
        fmt = node.name.upper() if node.this is not None else ""
        if fmt and fmt != "DELTA":
            raise Unsupported(f"USING {fmt} (a non-Delta data source)",
                              "tables here are managed Delta tables only")
    for _ in tree.find_all(exp.LocationProperty):
        raise Unsupported("LOCATION (an external table)", "no cloud storage to point at")
    for table in tree.find_all(exp.Table):
        if (table.catalog or "").lower() == "system" and \
                (table.db or "").lower() != "information_schema":
            raise Unsupported(f"system.{table.db} (system tables other than "
                              "information_schema)", "no account usage or billing is kept")


#: Properties that only decide a Delta table's layout or its metadata, dropped. Matched
#: by EXACT type: `exp.Property` is also every other property's base class, and a
#: TEMPORARY or LIKE property changes what the statement does. (`exp.Property` itself
#: is a TBLPROPERTIES entry; a non-Delta USING was refused before this runs.)
_LAYOUT_PROPERTIES = frozenset(
    getattr(exp, name) for name in (
        "FileFormatProperty", "PartitionedByProperty", "Property", "ClusteredByProperty",
        "ClusterProperty", "SchemaCommentProperty") if hasattr(exp, name))


def _strip_layout(tree: exp.Expression) -> None:
    for node in list(tree.find_all(exp.Property)):
        if type(node) in _LAYOUT_PROPERTIES and isinstance(node.parent, exp.Properties):
            node.pop()
    for node in list(tree.find_all(exp.CommentColumnConstraint)):
        parent = node.parent
        node.pop()
        if isinstance(parent, exp.ColumnConstraint) and parent.args.get("kind") is None:
            parent.pop()


# ------------------------------------------------------------------ translation
def split(sql: str) -> exp.Expression:
    """Exactly one parsed Databricks statement, or a SqlError."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise SqlError("PARSE_SYNTAX_ERROR", "Syntax error: the statement is empty.", "42601")
    try:
        trees = [t for t in sqlglot.parse(text, read="databricks") if t is not None]
    except ParseError as exc:
        first = str(exc).splitlines()[0]
        raise SqlError("PARSE_SYNTAX_ERROR", f"Syntax error: {first}", "42601") from None
    if len(trees) != 1:
        raise SqlError("PARSE_SYNTAX_ERROR",
                       f"The Statement Execution API runs one statement per request; this "
                       f"request has {len(trees)}.", "42601")
    return trees[0]


def _engine(identifier: exp.Identifier | None) -> exp.Identifier | None:
    if identifier is not None and identifier.name.lower() in CATALOG_ALIASES:
        return exp.Identifier(this=ENGINE_CATALOG, quoted=identifier.args.get("quoted"))
    return identifier


def _written(tree: exp.Expression) -> list[exp.Table]:
    """The relations a statement writes: a DROP's or a TRUNCATE's every table, another
    write's target."""
    if isinstance(tree, (*QUERY_KINDS, exp.With)):
        return []
    if isinstance(tree, (exp.Drop, exp.TruncateTable)):
        return list(tree.find_all(exp.Table))
    target = tree.this
    if isinstance(target, exp.Table):
        return [target]
    found = target.find(exp.Table) if isinstance(target, exp.Expression) else None
    return [found] if found is not None else []


#: What CREATE and DROP make or remove here. Everything else sqlglot parses - a DuckDB
#: SEQUENCE, INDEX, TYPE, MACRO, SECRET, a FUNCTION - is refused by its own name,
#: never run on the engine.
_OBJECT_KINDS = ("TABLE", "VIEW", "SCHEMA", "DATABASE")
#: Schema names the engine keeps for itself: DuckDB's default schema of the copy, and
#: the name the catalog's own views answer to.
RESERVED_SCHEMAS = (ENGINE_DEFAULT_SCHEMA, "information_schema")


def check_statement(tree: exp.Expression, sql: str) -> None:
    """The statement allowlist: queries, DML, CREATE/DROP of a table, view or schema,
    TRUNCATE TABLE and COMMENT ON - anything else refused naming its leading words."""
    verb = re.match(r"\s*([A-Za-z]+)", _bare(sql))
    verb = verb.group(1).upper() if verb else type(tree).__name__.upper()
    if not isinstance(tree, (*QUERY_KINDS, *DML_KINDS, *DDL_KINDS, exp.With)):
        # named by the statement's own leading word (INSTALL, DETACH, PRAGMA), not by
        # whatever node sqlglot made of it
        raise Unsupported(f"{verb} statements", "not Databricks SQL this warehouse runs")
    if isinstance(tree, (exp.Create, exp.Drop)):
        kind = str(tree.args.get("kind") or "").upper()
        if kind not in _OBJECT_KINDS:
            raise Unsupported(f"{verb} {kind or type(tree.this).__name__.upper()} statements",
                              "served: CREATE/DROP TABLE, VIEW, SCHEMA")
        if tree.find(exp.TemporaryProperty) is not None or tree.args.get("temporary"):
            raise Unsupported("TEMPORARY views and tables",
                              "a statement here runs in no session that could keep one")
        targets = [tree.this] if isinstance(tree, exp.Create) else \
            (tree.args.get("tables") or [tree.this])
        for target in targets if kind in ("SCHEMA", "DATABASE") else []:
            name = target.args.get("db") or target.args.get("this") \
                if isinstance(target, exp.Table) else None
            if isinstance(name, exp.Identifier) and name.name.lower() in RESERVED_SCHEMAS:
                raise Unsupported(f"schema name `{name.name}`",
                                  "reserved by this lakehouse's engine")
    if isinstance(tree, exp.Comment) and \
            str(tree.args.get("kind") or "").upper() not in ("TABLE", "COLUMN", "VIEW"):
        raise Unsupported(f"COMMENT ON {str(tree.args.get('kind') or '').upper()}",
                          "served: COMMENT ON TABLE, VIEW, COLUMN")


def _rewrite(tree: exp.Expression, context: Context) -> exp.Expression:
    for target in _written(tree):
        if (target.db or "").lower() == "information_schema":
            # The views are the catalog describing itself, not tables to write.
            raise SqlError("INSUFFICIENT_PERMISSIONS",
                           f"information_schema.{target.name.lower()} is read-only.", "42501")

    schema_ddl = isinstance(tree, (exp.Create, exp.Drop)) and \
        str(tree.args.get("kind") or "").upper() in ("SCHEMA", "DATABASE")
    # The Table nodes that name a CTE visible IN THEIR OWN SCOPE: a WITH inside a
    # subquery exempts nothing outside it. Every other unqualified name is qualified.
    cte_refs = dbt.cte_references(tree)

    def visit(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table):
            schema = (node.db or "").lower()
            if schema == "information_schema" and not schema_ddl and \
                    (node.catalog or "").lower() in ("", "system", *CATALOG_ALIASES):
                view = node.name.lower()
                if view not in INFO_VIEWS:
                    raise Unsupported(f"information_schema.{view}",
                                      f"served views: {', '.join(INFO_VIEWS)}")
                node.set("catalog", exp.to_identifier(INFO_CATALOG))
                node.set("db", exp.to_identifier(INFO_SCHEMA))
                node.set("this", exp.to_identifier(view))
                return node
            catalog = (node.catalog or "").lower()
            if catalog and catalog not in CATALOG_ALIASES:
                # Not a catalog of this workspace - and never one of the engine's
                # own (`memory`, `temp`, `system`, the copy's internal name), which a
                # statement must not be able to reach.
                raise _not_found([catalog, node.db.lower(), node.name.lower()])
            if schema_ddl:
                # CREATE/DROP SCHEMA [catalog.]name: `db` is the schema's own name, kept
                # as typed (a schema may be called `main`); only the catalog is the
                # engine's.
                node.set("catalog", exp.to_identifier(ENGINE_CATALOG))
                return node
            if node.db and not catalog:
                # A two-part name is schema.table IN THE CURRENT CATALOG. DuckDB would
                # also read it as catalog.table (`memory.x`, `temp.x`): say which.
                node.set("catalog", exp.to_identifier(ENGINE_CATALOG))
                return node
            if not node.db and isinstance(node.this, exp.Identifier) \
                    and id(node) not in cte_refs:
                # An unqualified name is a relation of the session's schema - never a
                # DuckDB table function called without parentheses
                # (`FROM pragma_database_list`).
                node.set("db", exp.Identifier(this=context.schema, quoted=True))
                node.set("catalog", exp.to_identifier(ENGINE_CATALOG))
                return node
            node.set("catalog", _engine(node.args.get("catalog")))
            return node
        if isinstance(node, exp.Column) and node.args.get("catalog") is not None:
            node.set("catalog", _engine(node.args.get("catalog")))
            return node
        return dbf.rewrite(node, context)

    tree = _post_order(tree, visit)
    if schema_ddl and str(tree.args.get("kind")).upper() == "DATABASE":
        tree.set("kind", "SCHEMA")
    return tree


_OPERATOR_NAMES = {exp.Add: "+", exp.Sub: "-", exp.Mul: "*", exp.Div: "/", exp.Mod: "%",
                   exp.IntDiv: "div", exp.EQ: "=", exp.NEQ: "!=", exp.GT: ">",
                   exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=", exp.NullSafeEQ: "<=>",
                   exp.And: "AND", exp.Or: "OR"}


def spark_name(node: exp.Expression) -> str:
    """The column name Spark gives an unaliased projection (its pretty SQL): `(d / 3)`,
    `count(1)`, `year(dt)` - so a rewrite's DuckDB form never names the column."""
    if isinstance(node, exp.Column):
        return node.name
    if isinstance(node, exp.Literal):
        return node.name
    if isinstance(node, exp.Null):
        return "NULL"
    if isinstance(node, exp.Boolean):
        return "true" if node.this else "false"
    if isinstance(node, exp.Paren):
        return spark_name(node.this)
    if isinstance(node, exp.Count) and isinstance(node.this, exp.Star):
        return "count(1)"
    if type(node) in _OPERATOR_NAMES:
        return (f"({spark_name(node.this)} {_OPERATOR_NAMES[type(node)]} "
                f"{spark_name(node.expression)})")
    if isinstance(node, exp.Neg):
        return f"(- {spark_name(node.this)})"
    if isinstance(node, exp.Not):
        return f"(NOT {spark_name(node.this)})"
    try:
        return node.sql(dialect="databricks", normalize_functions="lower")
    except Exception:  # noqa: BLE001 - a node sqlglot cannot write back
        return type(node).__name__.lower()


def _name_projections(tree: exp.Expression) -> None:
    """Alias the statement's unaliased result expressions with Spark's names, before
    the rewrite makes them DuckDB's (`CAST(length(s) AS INTEGER)`)."""
    query = tree.expression if isinstance(tree, exp.Create) else tree
    while isinstance(query, exp.SetOperation):
        query = query.left
    if not isinstance(query, exp.Select):
        return
    named = []
    for projection in query.expressions:
        if not isinstance(projection, (exp.Alias, exp.Column, exp.Star)):
            projection = exp.Alias(this=projection, alias=exp.Identifier(
                this=spark_name(projection), quoted=True))
        named.append(projection)
    query.set("expressions", named)


def _post_order(tree: exp.Expression, visit) -> exp.Expression:
    """`visit` every node, children before their parent, replacing each node with what
    it returns. A replacement may wrap the (already visited) original children, and
    keeps the original's Databricks type (databricks_types)."""
    root = tree
    for node in reversed(list(tree.walk(bfs=False))):
        # where the node sits, taken BEFORE the visit: a replacement that wraps the
        # node (CAST(node AS ...)) has already made itself the node's parent
        parent, key, index = node.parent, node.arg_key, node.index
        new = visit(node)
        if new is node:
            continue
        if "dbx_type" in node.meta and "dbx_type" not in new.meta:
            new.meta["dbx_type"] = node.meta["dbx_type"]
        if node is root:
            root = new
        else:
            parent.set(key, new, index)
    return root


def translate(sql: str, context: Context | None = None,
              lookup: dbt.Lookup | None = None) -> str:
    """One Databricks SQL statement -> the DuckDB SQL that runs it on the lakehouse.

    FAIL-CLOSED (alkera_gateway.databricks_functions): every function and expression in
    the statement must be on the vetted lists, each translated either as sqlglot does it
    (checked against Databricks' documented results) or by a rewrite with Databricks'
    semantics - for which the statement is first typed the way Spark types it
    (databricks_types; `lookup(schema, table)` gives a lakehouse table's columns);
    anything else is refused naming it. Raises `Unsupported(feature)` for what is
    refused by name (the module docstring), and `SqlError` for SQL Databricks itself
    would reject (a parse error, more than one statement)."""
    context = context or Context()
    fence(sql)
    tree = split(sql)
    check_statement(tree, sql)
    _fence_tree(tree)
    _strip_layout(tree)
    dbf.mark_two_arg_datediff(tree, sql)
    typer = None
    try:
        dbf.vet(tree)
        _name_projections(tree)
        typer = dbt.Typer(tree, lookup, context.schema)
        typer.annotate(tree)
        tree = _rewrite(tree, context)
    except dbf.Refused as refused:
        if lookup is not None and typer is not None and typer.missing and \
                "unknown type" in refused.feature:
            # the type is unknown because the table is: say that, as Databricks would
            raise _not_found(typer.missing[0]) from None
        raise Unsupported(refused.feature, refused.why) from None
    try:
        return tree.sql(dialect="duckdb", unsupported_level=ErrorLevel.RAISE)
    except UnsupportedError as exc:
        raise Unsupported(str(exc).splitlines()[0],
                          "the translator cannot express it on this engine") from None


# ------------------------------------------------------------------ the engine
def _pattern(raw: str | None, *, jdbc: bool = False) -> str | None:
    """A SHOW ... LIKE pattern (`*` any, `|` or, case-insensitive) as an RE2 regex.
    `jdbc`: the drivers' metadata form (`SHOW TABLES IN CATALOG ... SCHEMA LIKE`),
    whose patterns are JDBC's - `%` any run, `_` any one character."""
    if raw is None:
        return None
    raw = raw.strip().strip("'\"")
    parts = []
    for part in raw.split("|"):
        regex = re.escape(part.strip()).replace(r"\*", ".*")
        if jdbc:
            regex = regex.replace("%", ".*").replace("_", ".")
        parts.append(regex)
    return "(" + "|".join(parts) + ")"


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _ident(raw: str) -> list[str]:
    """`a`.`b`.c -> ["a", "b", "c"] (lower-cased: Databricks names are case-insensitive)."""
    parts = re.findall(r"`((?:[^`]|``)*)`|([^.\s`]+)", raw.strip())
    return [(quoted.replace("``", "`") if quoted else bare).lower() for quoted, bare in parts]


#: The SQL drivers' metadata calls over the Statement Execution API (the connector's
#: `cursor.tables()` / `cursor.columns()` with use_sea=True): `SHOW TABLES|COLUMNS IN
#: CATALOG c|IN ALL CATALOGS [SCHEMA LIKE 'p'] [TABLE LIKE 'p'] [LIKE 'p']`.
_SHOW_CATALOG = re.compile(
    r"^\s*SHOW\s+(?P<what>TABLES|COLUMNS)\s+IN\s+(?:ALL\s+CATALOGS|CATALOG\s+"
    r"(?P<catalog>`[^`]+`|[\w]+))(?P<rest>.*?)\s*;?\s*$", re.I | re.S)
_CATALOG_LIKE = re.compile(r"\s*(?:(?P<scope>SCHEMA|TABLE)\s+)?LIKE\s+'(?P<pattern>[^']*)'",
                           re.I)
_SHOW = re.compile(
    r"^\s*SHOW\s+(?P<what>CATALOGS|SCHEMAS|DATABASES|TABLES|VIEWS|COLUMNS|"
    r"TABLE\s+EXTENDED)\b(?P<rest>.*?)\s*;?\s*$", re.I | re.S)
_SHOW_IN = re.compile(r"^\s*(?:IN|FROM)\s+(?P<name>[`\w.]+(?:`[^`]*`)?[`\w.]*)", re.I)
_SHOW_LIKE = re.compile(r"^\s*(?:LIKE\s+)?(?P<pattern>'[^']*'|\"[^\"]*\")\s*$", re.I)
_DESCRIBE = re.compile(
    r"^\s*DESC(?:RIBE)?\s+(?:(?P<kind>TABLE|SCHEMA|DATABASE|CATALOG|QUERY|FUNCTION)\s+)?"
    r"(?P<extended>EXTENDED\s+|FORMATTED\s+)?(?P<name>.+?)(?P<json>\s+AS\s+JSON)?\s*;?\s*$",
    re.I | re.S)
_USE = re.compile(r"^\s*USE\s+(?:(?P<kind>CATALOG|SCHEMA|DATABASE|NAMESPACE)\s+)?"
                  r"(?P<name>[^;]+?)\s*;?\s*$", re.I | re.S)
_SET = re.compile(r"^\s*(?P<verb>SET|RESET)\b(?P<rest>.*?)\s*;?\s*$", re.I | re.S)
#: Metadata-only statements a warehouse accepts and that change no answer here.
_NOOPS = [re.compile(p, re.I | re.S) for p in (
    r"^\s*ALTER\s+(TABLE|VIEW)\s+\S+\s+(SET|UNSET)\s+TBLPROPERTIES\b",
    r"^\s*ALTER\s+(TABLE|VIEW)\s+\S+\s+(ALTER|CHANGE)\s+(COLUMN\s+)?\S+\s+COMMENT\b",
    r"^\s*ALTER\s+(TABLE|VIEW|SCHEMA|CATALOG)\s+\S+\s+(SET\s+)?OWNER\s+TO\b",
    r"^\s*ALTER\s+(TABLE|VIEW)\s+\S+\s+SET\s+TAGS\b",
    r"^\s*ANALYZE\s+TABLE\b", r"^\s*(UN)?CACHE\s+(LAZY\s+)?TABLE\b",
    r"^\s*REFRESH\s+(TABLE|FUNCTION)\b", r"^\s*(GRANT|REVOKE)\b",
)]


class Lakehouse:
    """The copy, attached under `ENGINE_CATALOG`, and the statements run on it.

    One DuckDB connection per lakehouse file, serialised by a lock: every statement
    first moves the connection to its session's schema (`USE`), and a second
    connection on the same file would be a second writer."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.con = duckdb.connect(":memory:", config={"threads": 1})
        self.con.execute(f"ATTACH {_quote(self.path.as_posix())} AS {ENGINE_CATALOG}")
        # A SQL warehouse's session time zone, UTC: TIMESTAMP literals and
        # date_trunc/timestampadd results are read in it.
        self.con.execute("SET TimeZone = 'UTC'")
        for name, function, parameters, returns in dbf.UDFS:
            self.con.create_function(name, function, parameters, returns,
                                     side_effects=False)
        for macro in dbf.MACROS:
            self.con.execute(macro)
        self._information_schema()
        # Nothing a statement says may reach a file: no ATTACH, no read_csv('/world/..'),
        # no COPY TO - the copy stays the only database this connection writes.
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")

    def close(self) -> None:
        with self.lock:
            try:
                self.con.close()
            except duckdb.Error:  # pragma: no cover - already closed
                pass

    # ------------------------------------------------------------------ seeding
    @staticmethod
    def seed(world_warehouse: Path, path: Path) -> list[dict] | None:
        """Copy the world's warehouse into a new lakehouse file at `path`.

        Every base table and view of every user schema becomes a table of the same
        schema (DuckDB's default schema `main` becomes `default`), and `default` exists
        even when empty. The world file is attached READ_ONLY and never written. Built
        under a temporary name and renamed into place, so a half-built copy is never
        served. Returns what was copied, or None when `path` already exists - the copy
        a previous boot of this rollout seeded, with whatever was written to it since,
        is kept."""
        path = Path(path)
        if path.exists():
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        building = path.with_name(path.name + ".seeding")
        for stale in (building, building.with_name(building.name + ".wal")):
            stale.unlink(missing_ok=True)
        world = Path(world_warehouse)
        # Attached under its own stem, the name its views were defined against
        # (`warehouse.warehouse.fct_orders`), so a view's rows can be read.
        src = world.stem
        con = duckdb.connect(str(building), config={"threads": 1})
        copied = []
        try:
            own = con.execute("SELECT current_database()").fetchone()[0]
            if own == src:  # pragma: no cover - the copy is never named like the world
                raise ValueError(f"the lakehouse may not share the world file's stem {src!r}")
            q_own, q_src = _identifier(own), _identifier(src)
            con.execute(f"ATTACH {_quote(world.as_posix())} AS {q_src} (READ_ONLY)")
            tables = con.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_catalog = ? AND table_schema NOT IN "
                "('information_schema', 'pg_catalog') ORDER BY 1, 2", [src]).fetchall()
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {q_own}.{_identifier(DEFAULT_SCHEMA)}")
            for schema, table in tables:
                target = DEFAULT_SCHEMA if schema == ENGINE_DEFAULT_SCHEMA else schema
                into = f"{q_own}.{_identifier(target)}.{_identifier(table)}"
                con.execute(f"CREATE SCHEMA IF NOT EXISTS {q_own}.{_identifier(target)}")
                con.execute(f"CREATE TABLE {into} AS SELECT * FROM "
                            f"{q_src}.{_identifier(schema)}.{_identifier(table)}")
                rows = con.execute(f"SELECT count(*) FROM {into}").fetchone()[0]
                copied.append({"schema": target, "table": table, "rows": rows})
            con.execute(f"DETACH {q_src}")
            con.execute("CHECKPOINT")
        finally:
            con.close()
        os.replace(building, path)
        return copied

    # -------------------------------------------------------- information_schema
    def _information_schema(self) -> None:
        """Unity Catalog's information_schema views over the copy, created once per
        connection, before its configuration is locked."""
        con = self.con
        con.create_function("dbx_type_name", lambda t: databricks_type(t)[0],
                            ["VARCHAR"], "VARCHAR", null_handling="special")
        con.create_function("dbx_type_text", lambda t: databricks_type(t)[1],
                            ["VARCHAR"], "VARCHAR", null_handling="special")
        info = f"{INFO_CATALOG}.{INFO_SCHEMA}"
        hidden = f"'{ENGINE_DEFAULT_SCHEMA}'"
        con.execute(f"CREATE SCHEMA {info}")
        con.execute(f"""CREATE VIEW {info}.catalogs AS SELECT
            '{CATALOG}' AS catalog_name, '{USER}' AS catalog_owner,
            NULL::VARCHAR AS comment, NULL::TIMESTAMP AS created,
            NULL::VARCHAR AS created_by, NULL::TIMESTAMP AS last_altered,
            NULL::VARCHAR AS last_altered_by""")
        con.execute(f"""CREATE VIEW {info}.schemata AS SELECT * FROM (
            SELECT '{CATALOG}' AS catalog_name, schema_name, '{USER}' AS schema_owner,
                NULL::VARCHAR AS comment, NULL::TIMESTAMP AS created,
                NULL::VARCHAR AS created_by, NULL::TIMESTAMP AS last_altered,
                NULL::VARCHAR AS last_altered_by
            FROM duckdb_schemas() WHERE database_name = '{ENGINE_CATALOG}'
                AND schema_name <> {hidden}
            UNION ALL
            SELECT '{CATALOG}', 'information_schema', 'System user', NULL, NULL, NULL,
                NULL, NULL) ORDER BY schema_name""")
        con.execute(f"""CREATE VIEW {info}.tables AS SELECT * FROM (
            SELECT '{CATALOG}' AS table_catalog, schema_name AS table_schema,
                table_name, 'MANAGED' AS table_type, 'YES' AS is_insertable_into,
                'PRESERVE' AS commit_action, '{USER}' AS table_owner, comment,
                NULL::TIMESTAMP AS created, NULL::VARCHAR AS created_by,
                NULL::TIMESTAMP AS last_altered, NULL::VARCHAR AS last_altered_by,
                'DELTA' AS data_source_format, NULL::VARCHAR AS storage_sub_directory
            FROM duckdb_tables() WHERE database_name = '{ENGINE_CATALOG}'
                AND schema_name <> {hidden}
            UNION ALL
            SELECT '{CATALOG}', schema_name, view_name, 'VIEW', 'NO', 'PRESERVE',
                '{USER}', comment, NULL, NULL, NULL, NULL, NULL, NULL
            FROM duckdb_views() WHERE database_name = '{ENGINE_CATALOG}'
                AND NOT internal AND schema_name <> {hidden}
            ) ORDER BY table_schema, table_name""")
        con.execute(f"""CREATE VIEW {info}.views AS SELECT
            '{CATALOG}' AS table_catalog, schema_name AS table_schema,
            view_name AS table_name, replace(sql, '{ENGINE_CATALOG}', '{CATALOG}')
                AS view_definition, 'NONE' AS check_option, 'NO' AS is_updatable,
            'NO' AS is_insertable_into, NULL::VARCHAR AS sql_path, false AS is_materialized
            FROM duckdb_views() WHERE database_name = '{ENGINE_CATALOG}'
                AND NOT internal AND schema_name <> {hidden}
            ORDER BY table_schema, table_name""")
        con.execute(f"""CREATE VIEW {info}.columns AS SELECT
            '{CATALOG}' AS table_catalog, schema_name AS table_schema, table_name,
            column_name, column_index AS ordinal_position, column_default,
            CASE WHEN is_nullable THEN 'YES' ELSE 'NO' END AS is_nullable,
            dbx_type_text(data_type) AS full_data_type,
            dbx_type_name(data_type) AS data_type,
            character_maximum_length, NULL::BIGINT AS character_octet_length,
            numeric_precision, numeric_precision_radix, numeric_scale,
            NULL::INTEGER AS datetime_precision, comment,
            NULL::INTEGER AS partition_index
            FROM duckdb_columns() WHERE database_name = '{ENGINE_CATALOG}'
                AND schema_name <> {hidden}
            ORDER BY table_schema, table_name, ordinal_position""")

    # ------------------------------------------------------------------ queries
    def _rows(self, sql: str, params: list | None = None) -> list[tuple]:
        return self.con.execute(sql, params or []).fetchall()

    def schemas(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self._rows(
                f"SELECT schema_name FROM {INFO_CATALOG}.{INFO_SCHEMA}.schemata")]

    def schema_named(self, name: str | None) -> str | None:
        """The schema `name` names, as stored, or None: Databricks names are
        case-insensitive, and a session may carry a name lower-cased."""
        wanted = str(name or "").lower()
        return next((s for s in self.schemas() if s.lower() == wanted), None)

    def tables(self, schema: str | None = None) -> list[dict]:
        """information_schema.tables rows (as dicts), one schema or all."""
        where = "" if schema is None else f" WHERE table_schema = {_quote(schema)}"
        with self.lock:
            cur = self.con.execute(f"SELECT * FROM {INFO_CATALOG}.{INFO_SCHEMA}.tables{where}")
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def columns(self, schema: str, table: str) -> list[dict]:
        with self.lock:
            cur = self.con.execute(
                f"SELECT * FROM {INFO_CATALOG}.{INFO_SCHEMA}.columns "
                f"WHERE table_schema = ? AND table_name = ?", [schema, table])
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def count(self) -> int:
        """Tables and views in the copy: what `/health` reports as `items`."""
        return len(self.tables())

    # ---------------------------------------------------------------- execution
    def execute(self, sql: str, context: Context | None = None,
                params: dict | None = None) -> tuple[Result, Context]:
        """Run one Databricks statement in `context`; returns its result and the
        session context after it (a USE moves it). Raises `Unsupported` or `SqlError`."""
        context = context or Context()
        fence(sql)
        with self.lock:
            for handler in (self._use, self._set, self._noop, self._show, self._describe):
                handled = handler(sql, context)
                if handled is not None:
                    return handled
            tree = split(sql)
            check_statement(tree, sql)
            if self.schema_named(context.schema) is None and _unqualified(tree):
                # the engine would resolve the name in its own default schema
                raise SqlError("SCHEMA_NOT_FOUND",
                               f"The schema `{context.catalog}`.`{context.schema}` cannot "
                               f"be found. Verify the spelling and correctness of the "
                               f"schema and catalog.", "42704")
            duck = translate(sql, context, self.column_types)
            return self._run(duck, tree, context, params), context

    def column_types(self, schema: str, table: str) -> dict[str, str]:
        """{column: DuckDB type} of a lakehouse table or view (empty when there is
        none): what the translator types a statement's columns by."""
        with self.lock:
            if str(schema).lower() == "information_schema":
                database, found = INFO_CATALOG, INFO_SCHEMA
            else:
                database, found = ENGINE_CATALOG, self.schema_named(schema)
            if found is None:
                return {}
            return dict(self._rows(
                "SELECT column_name, data_type FROM duckdb_columns() WHERE "
                "database_name = ? AND schema_name = ? AND lower(table_name) = lower(?)",
                [database, found, table]))

    def _enter(self, context: Context) -> None:
        """Point the connection at the session's schema (or its catalog, when the
        schema does not exist: the statement then fails on its own terms)."""
        schema = self.schema_named(context.schema)
        # The name is the session's - whatever a client sent as `schema` - so it is a
        # quoted identifier with its quotes doubled, never SQL.
        target = f"{ENGINE_CATALOG}.{_identifier(schema)}" if schema else ENGINE_CATALOG
        self.con.execute(f"USE {target}")

    def _run(self, duck: str, tree: exp.Expression, context: Context,
             params: dict | None) -> Result:
        try:
            self._enter(context)
            cur = self.con.execute(duck, params or None)
            description = cur.description or []
            rows = cur.fetchall() if description else []
        except duckdb.Error as exc:
            raise engine_error(exc) from None
        if isinstance(tree, DML_KINDS):
            n = int(rows[0][0]) if rows and rows[0] else 0
            if isinstance(tree, exp.Insert):
                return Result([column("num_affected_rows", "BIGINT"),
                               column("num_inserted_rows", "BIGINT")], [(n, n)], n)
            return Result([column("num_affected_rows", "BIGINT")], [(n,)], n)
        if isinstance(tree, exp.Create) and tree.expression is not None and \
                str(tree.args.get("kind") or "").upper() == "TABLE":
            n = int(rows[0][0]) if rows and rows[0] else 0
            return Result([column("num_affected_rows", "BIGINT"),
                           column("num_inserted_rows", "BIGINT")], [(n, n)], n)
        if isinstance(tree, DDL_KINDS):
            return Result()
        return Result([column(d[0], str(d[1])) for d in description], rows)

    # ---------------------------------------------------------------- commands
    def _use(self, sql: str, context: Context):
        found = _USE.match(sql)
        if not found:
            return None
        kind = (found.group("kind") or "").upper()
        parts = _ident(found.group("name"))
        if kind == "CATALOG":
            if len(parts) != 1 or parts[0] not in CATALOG_ALIASES:
                raise SqlError("NO_SUCH_CATALOG_EXCEPTION",
                               f"Catalog '{'.'.join(parts)}' was not found. Please verify "
                               f"the catalog name and then retry the query or command "
                               f"again.", "42704")
            return Result(), replace(context, catalog=parts[0], schema=DEFAULT_SCHEMA)
        catalog = context.catalog
        if len(parts) == 2:
            catalog, parts = parts[0], parts[1:]
            if catalog not in CATALOG_ALIASES:
                raise SqlError("NO_SUCH_CATALOG_EXCEPTION",
                               f"Catalog '{catalog}' was not found.", "42704")
        if len(parts) != 1:
            raise SqlError("PARSE_SYNTAX_ERROR", f"Syntax error at {found.group('name')!r}.",
                           "42601")
        schema = self.schema_named(parts[0])
        if schema is None:
            raise SqlError("SCHEMA_NOT_FOUND",
                           f"The schema `{catalog}`.`{parts[0]}` cannot be found. Verify the "
                           f"spelling and correctness of the schema and catalog.", "42704")
        return Result(), replace(context, catalog=catalog, schema=schema)

    def _set(self, sql: str, context: Context):
        found = _SET.match(sql)
        if not found:
            return None
        rest = found.group("rest").strip()
        if re.match(r"VAR(IABLE)?\b", rest, re.I):
            raise Unsupported("SET VARIABLE (SQL session variables)",
                              "a statement here runs in no session that could keep one")
        columns = [column("key", "VARCHAR"), column("value", "VARCHAR")]
        if found.group("verb").upper() == "RESET" or not rest:
            return Result(columns, []), context
        key, eq, value = rest.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if not eq:
            return Result(columns, [(key, "<undefined>")]), context
        return Result(columns, [(key, value)]), context

    def _noop(self, sql: str, context: Context):
        if any(p.match(sql) for p in _NOOPS):
            return Result(), context
        return None

    def _show_catalog(self, found: re.Match, context: Context):
        """`SHOW TABLES|COLUMNS IN CATALOG ...`: the drivers' JDBC-shaped metadata.

        The columns are those the connector reads back (its SEA table-type filter
        takes index 5 of a SHOW TABLES row); table types are JDBC's, TABLE and VIEW."""
        catalog = found.group("catalog")
        if catalog is not None and _ident(catalog)[0] not in CATALOG_ALIASES:
            return Result([], []), context
        name = _ident(catalog)[0] if catalog else CATALOG
        rest, patterns = found.group("rest"), {}
        while rest.strip():
            part = _CATALOG_LIKE.match(rest)
            if part is None:
                raise SqlError("PARSE_SYNTAX_ERROR", f"Syntax error in SHOW "
                               f"{found.group('what').upper()}: {rest.strip()!r}.", "42601")
            patterns[(part.group("scope") or "").upper()] = _pattern(part.group("pattern"),
                                                                     jdbc=True)
            rest = rest[part.end():]
        info = f"{INFO_CATALOG}.{INFO_SCHEMA}"
        what = found.group("what").upper()
        filtered = ({"SCHEMA": "table_schema", "TABLE": "table_name", "": "column_name"}
                    if what == "COLUMNS" else {"SCHEMA": "table_schema", "": "table_name"})
        where, params = ["true"], []
        for scope, regex in patterns.items():
            if scope not in filtered:
                raise SqlError("PARSE_SYNTAX_ERROR", f"SHOW {what} takes no {scope} LIKE.",
                               "42601")
            where.append(f"regexp_full_match(lower({filtered[scope]}), ?)")
            params.append(regex.lower())
        if what == "TABLES":
            rows = self._rows(
                f"SELECT ?, table_schema, table_name, false, NULL::VARCHAR, "
                f"CASE table_type WHEN 'VIEW' THEN 'VIEW' ELSE 'TABLE' END, comment "
                f"FROM {info}.tables WHERE {' AND '.join(where)} ORDER BY 2, 3",
                [name, *params])
            return Result([column(n, t) for n, t in (
                ("catalogName", "VARCHAR"), ("namespace", "VARCHAR"),
                ("tableName", "VARCHAR"), ("isTemporary", "BOOLEAN"),
                ("information", "VARCHAR"), ("tableType", "VARCHAR"),
                ("remarks", "VARCHAR"))], rows), context
        rows = self._rows(
            f"SELECT column_name, ?, table_schema, table_name, full_data_type, "
            f"coalesce(numeric_precision, character_maximum_length), numeric_scale, "
            f"numeric_precision_radix, is_nullable, comment, ordinal_position, 'NO', 'NO' "
            f"FROM {info}.columns WHERE {' AND '.join(where)} "
            f"ORDER BY 3, 4, ordinal_position", [name, *params])
        return Result([column(n, t) for n, t in (
            ("col_name", "VARCHAR"), ("catalogName", "VARCHAR"), ("namespace", "VARCHAR"),
            ("tableName", "VARCHAR"), ("columnType", "VARCHAR"), ("columnSize", "INTEGER"),
            ("decimalDigits", "INTEGER"), ("radix", "INTEGER"), ("isNullable", "VARCHAR"),
            ("remarks", "VARCHAR"), ("ordinalPosition", "INTEGER"),
            ("isAutoIncrement", "VARCHAR"), ("isGenerated", "VARCHAR"))], rows), context

    def _show(self, sql: str, context: Context):
        in_catalog = _SHOW_CATALOG.match(sql)
        if in_catalog:
            return self._show_catalog(in_catalog, context)
        found = _SHOW.match(sql)
        if not found:
            if re.match(r"^\s*SHOW\b", sql, re.I):
                what = " ".join(sql.split()[1:3]).upper()
                raise Unsupported(f"SHOW {what}", "served: SHOW CATALOGS, SCHEMAS, TABLES, "
                                  "VIEWS, COLUMNS, TABLE EXTENDED")
            return None
        what = re.sub(r"\s+", " ", found.group("what").upper())
        rest = found.group("rest")
        scope = None
        inside = _SHOW_IN.match(rest)
        if inside:
            scope = _ident(inside.group("name"))
            rest = rest[inside.end():]
        if what == "COLUMNS":
            second = _SHOW_IN.match(rest)
            if second:
                scope = _ident(second.group("name")) + (scope or [])[-1:]
                rest = rest[second.end():]
        like = _SHOW_LIKE.match(rest) if rest.strip() else None
        if rest.strip() and like is None:
            raise SqlError("PARSE_SYNTAX_ERROR", f"Syntax error in SHOW {what}: "
                           f"{rest.strip()!r}.", "42601")
        regex = _pattern(like.group("pattern")) if like else None
        info = f"{INFO_CATALOG}.{INFO_SCHEMA}"

        def match(col: str) -> str:
            return f" AND regexp_full_match(lower({col}), {_quote(regex.lower())})" \
                if regex else ""

        if what == "CATALOGS":
            names = [CATALOG] if regex is None or re.fullmatch(regex, CATALOG, re.I) else []
            return Result([column("catalog", "VARCHAR")], [(n,) for n in names]), context
        if what in ("SCHEMAS", "DATABASES"):
            if scope and (len(scope) != 1 or scope[0] not in CATALOG_ALIASES):
                raise SqlError("NO_SUCH_CATALOG_EXCEPTION",
                               f"Catalog '{'.'.join(scope)}' was not found.", "42704")
            rows = self._rows(f"SELECT schema_name FROM {info}.schemata WHERE true"
                              f"{match('schema_name')} ORDER BY 1")
            return Result([column("databaseName", "VARCHAR")], rows), context
        schema = self._scope_schema(scope, context)
        if what in ("TABLES", "TABLE EXTENDED"):
            rows = self._rows(f"SELECT table_schema, table_name, false, table_type FROM "
                              f"{info}.tables WHERE table_schema = ?{match('table_name')} "
                              f"ORDER BY 2", [schema])
            if what == "TABLES":
                return Result([column("database", "VARCHAR"), column("tableName", "VARCHAR"),
                               column("isTemporary", "BOOLEAN")],
                              [r[:3] for r in rows]), context
            information = [
                f"Catalog: {CATALOG}\nDatabase: {s}\nTable: {t}\nOwner: {USER}\n"
                f"Type: {'VIEW' if kind == 'VIEW' else 'MANAGED'}\nProvider: "
                f"{'' if kind == 'VIEW' else 'delta'}\n" for s, t, _, kind in rows]
            return Result([column("database", "VARCHAR"), column("tableName", "VARCHAR"),
                           column("isTemporary", "BOOLEAN"), column("information", "VARCHAR")],
                          [(*r[:3], text) for r, text in zip(rows, information, strict=True)]
                          ), context
        if what == "VIEWS":
            rows = self._rows(f"SELECT table_schema, table_name, false, false FROM "
                              f"{info}.views WHERE table_schema = ?{match('table_name')} "
                              f"ORDER BY 2", [schema])
            return Result([column("namespace", "VARCHAR"), column("viewName", "VARCHAR"),
                           column("isTemporary", "BOOLEAN"),
                           column("isMaterialized", "BOOLEAN")], rows), context
        # COLUMNS
        if not scope:
            raise SqlError("PARSE_SYNTAX_ERROR", "SHOW COLUMNS needs IN <table>.", "42601")
        schema, table = self._resolve(scope, context)
        rows = self._rows(f"SELECT column_name FROM {info}.columns WHERE table_schema = ? "
                          f"AND table_name = ?{match('column_name')} ORDER BY ordinal_position",
                          [schema, table])
        return Result([column("col_name", "VARCHAR")], rows), context

    def _scope_schema(self, scope: list[str] | None, context: Context) -> str:
        if not scope:
            return context.schema
        if len(scope) == 2:
            if scope[0] not in CATALOG_ALIASES:
                raise SqlError("NO_SUCH_CATALOG_EXCEPTION",
                               f"Catalog '{scope[0]}' was not found.", "42704")
            scope = scope[1:]
        found = self.schema_named(scope[0]) if len(scope) == 1 else None
        if found is None:
            raise SqlError("SCHEMA_NOT_FOUND",
                           f"The schema `{context.catalog}`.`{'.'.join(scope)}` cannot be "
                           f"found.", "42704")
        return found

    def _resolve(self, parts: list[str], context: Context) -> tuple[str, str]:
        """A table name in the session -> (schema, table), or TABLE_OR_VIEW_NOT_FOUND."""
        if len(parts) == 3 and parts[0] not in CATALOG_ALIASES:
            raise _not_found(parts)
        schema, table = (parts[-2], parts[-1]) if len(parts) >= 2 else (context.schema, parts[0])
        if not any(t["table_name"] == table for t in self.tables(schema)):
            raise _not_found([context.catalog if len(parts) < 3 else parts[0], schema, table])
        return schema, table

    def _describe(self, sql: str, context: Context):
        found = _DESCRIBE.match(sql)
        if not found:
            return None
        kind = (found.group("kind") or "TABLE").upper()
        name = found.group("name").strip()
        if kind in ("QUERY", "FUNCTION"):
            raise Unsupported(f"DESCRIBE {kind}", "served: DESCRIBE TABLE, SCHEMA, CATALOG")
        if kind == "CATALOG":
            if _ident(name)[-1] not in CATALOG_ALIASES:
                raise SqlError("NO_SUCH_CATALOG_EXCEPTION", f"Catalog '{name}' was not found.",
                               "42704")
            return Result([column("info_name", "VARCHAR"), column("info_value", "VARCHAR")],
                          [("Catalog Name", CATALOG), ("Comment", ""),
                           ("Owner", USER), ("Catalog Type", "Regular")]), context
        if kind in ("SCHEMA", "DATABASE"):
            schema = self._scope_schema(_ident(name), context)
            return Result([column("database_description_item", "VARCHAR"),
                           column("database_description_value", "VARCHAR")],
                          [("Catalog Name", CATALOG), ("Namespace Name", schema),
                           ("Comment", ""), ("Owner", USER)]), context
        schema, table = self._resolve(_ident(name), context)
        cols = self.columns(schema, table)
        table_type = next(t["table_type"] for t in self.tables(schema)
                          if t["table_name"] == table)
        if found.group("json"):
            doc = {"table_name": table, "catalog_name": CATALOG, "namespace": [schema],
                   "schema_name": schema, "type": table_type,
                   "provider": None if table_type == "VIEW" else "delta",
                   "owner": USER,
                   "columns": [{"name": c["column_name"],
                                "type": {"name": c["full_data_type"]},
                                "nullable": c["is_nullable"] == "YES"} for c in cols]}
            return Result([column("json_metadata", "VARCHAR")],
                          [(json.dumps(doc, sort_keys=True),)]), context
        rows = [(c["column_name"], c["full_data_type"], c["comment"]) for c in cols]
        if found.group("extended"):
            rows += [("", "", ""), ("# Detailed Table Information", "", ""),
                     ("Catalog", CATALOG, ""), ("Database", schema, ""),
                     ("Table", table, ""), ("Owner", USER, ""),
                     ("Type", table_type, ""),
                     ("Provider", "" if table_type == "VIEW" else "delta", "")]
        return Result([column("col_name", "VARCHAR"), column("data_type", "VARCHAR"),
                       column("comment", "VARCHAR")], rows), context


def _unqualified(tree: exp.Expression) -> list[str]:
    """Relation names a statement gives without a schema (references to a CTE in their
    own scope excepted): the ones the session's current schema resolves."""
    if isinstance(tree, (exp.Create, exp.Drop)) and \
            str(tree.args.get("kind") or "").upper() in ("SCHEMA", "DATABASE"):
        return []
    cte_refs = dbt.cte_references(tree)
    return [t.name for t in tree.find_all(exp.Table)
            if isinstance(t.this, exp.Identifier) and not t.db
            and id(t) not in cte_refs]


def _not_found(parts: list[str]) -> SqlError:
    name = ".".join(f"`{p}`" for p in parts)
    return SqlError("TABLE_OR_VIEW_NOT_FOUND",
                    f"The table or view {name} cannot be found. Verify the spelling and "
                    f"correctness of the schema and catalog.", "42P01")


def engine_error(exc: duckdb.Error) -> SqlError:
    """A DuckDB error as the Databricks error a warehouse would raise for it, never
    naming the engine's catalog or its information_schema."""
    message = str(exc).replace(f"{INFO_CATALOG}.{INFO_SCHEMA}", "system.information_schema")
    message = message.replace(ENGINE_CATALOG, CATALOG)
    first = message.splitlines()[0] if message else type(exc).__name__
    raised = re.search(r"\[([A-Z_.]+)\] (.*?) SQLSTATE: (\w+)", message)
    if raised:
        # an error a rewrite raises in Databricks' own words (databricks_functions:
        # DIVIDE_BY_ZERO)
        return SqlError(raised.group(1), raised.group(2), raised.group(3))
    if isinstance(exc, duckdb.CatalogException):
        if "already exists" in first:
            return SqlError("TABLE_OR_VIEW_ALREADY_EXISTS", first, "42P07")
        if re.search(r"Schema with name", first):
            return SqlError("SCHEMA_NOT_FOUND", first, "42704")
        return SqlError("TABLE_OR_VIEW_NOT_FOUND", first, "42P01")
    if isinstance(exc, duckdb.ParserException):
        return SqlError("PARSE_SYNTAX_ERROR", first, "42601")
    if isinstance(exc, duckdb.BinderException) and "column" in first.lower():
        return SqlError("UNRESOLVED_COLUMN", first, "42703")
    if isinstance(exc, duckdb.ConversionException):
        return SqlError("CAST_INVALID_INPUT", first, "22018")
    if isinstance(exc, duckdb.ConstraintException):
        return SqlError("CONSTRAINT_VIOLATION", first, "23000")
    if isinstance(exc, duckdb.OutOfRangeException):
        # an INT sum past 2^31, a DECIMAL past its precision: ANSI overflow
        return SqlError("ARITHMETIC_OVERFLOW", first, "22003")
    if isinstance(exc, duckdb.PermissionException):
        return SqlError("INSUFFICIENT_PERMISSIONS", first, "42501")
    return SqlError("SQL_EXECUTION_ERROR", first, "42000")


def parameters(raw: list | None) -> dict:
    """The Statement Execution API's `parameters` ([{name, value, type}]) as DuckDB
    named parameters, typed the way the request types them (untyped is STRING)."""
    out = {}
    if raw is not None and not isinstance(raw, list):
        raise SqlError("INVALID_PARAMETER_VALUE", "parameters must be a list", "22023")
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("name"):
            raise SqlError("INVALID_PARAMETER_VALUE", "every parameter needs a name", "22023")
        value, kind = item.get("value"), str(item.get("type") or "STRING").upper()
        if value is not None:
            try:
                value = _typed(value, kind)
            except (ValueError, TypeError, ArithmeticError) as exc:
                raise SqlError("INVALID_PARAMETER_VALUE",
                               f"Parameter {item['name']!r} = {value!r} is not a valid "
                               f"{kind}: {exc}", "22023") from None
        out[str(item["name"])] = value
    return out


def _typed(value, kind: str):
    if kind in ("INT", "INTEGER", "BIGINT", "LONG", "SMALLINT", "TINYINT", "SHORT", "BYTE"):
        return int(str(value))
    if kind in ("DOUBLE", "FLOAT"):
        return float(value)
    if kind.startswith("DECIMAL"):
        return Decimal(str(value))
    if kind == "BOOLEAN":
        if str(value).lower() not in ("true", "false"):
            raise ValueError("expected true or false")
        return str(value).lower() == "true"
    if kind == "DATE":
        return datetime.date.fromisoformat(str(value))
    if kind.startswith("TIMESTAMP"):
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if kind in ("STRING", "VARCHAR", "CHAR"):
        return str(value)
    raise ValueError(f"parameter type {kind} is not supported")
