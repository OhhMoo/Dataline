"""Column types: what Sigma's connection knows about the warehouse.

WHY THIS FILE EXISTS. A Sigma code representation carries no column types.
Data model and workbook columns are `{id, formula, name?}`; the type is
whatever the warehouse says when Sigma runs the formula, and Sigma itself
collapses it further: "All data represented as integers, fixed-point decimals,
or floating-point numbers in your connected CDW or DBMS map to Sigma's number
data type" (help.sigmacomputing.com/docs/data-types-and-formats). The model
contract (`Column.data_type`) needs int vs decimal vs float and date vs
timestamp, so a Sigma front-end must take types from the warehouse, as Sigma
does.

Two sources, in this order:

1. The warehouse itself (`Catalog.from_warehouse`): the DuckDB catalog of the
   world's warehouse file, read-only. Exact. This is what the fake service and
   any grader that has the warehouse use.
2. A connection snapshot in the project (`connections/<connection>.json`):
   the connection as `GET /v2/connections/{connectionId}` returns it, plus the
   tables Sigma has synced with their columns, each as
   `GET /v2/connections/tables/{tableId}/columns` lists it (`name`, `type`,
   `visibility`) and one key Sigma does not serve, `nativeType`, the
   warehouse's own type - because Sigma's `type` (`number`, `datetime`)
   cannot tell decimal from float or date from timestamp. `write_project`
   writes it so a project reads back without a warehouse.

A column neither source knows is a ModelError naming it: in Sigma a warehouse
column is unknown until the connection is synced
(`POST /v2/connections/{connectionId}/sync`), and the fake service behaves the
same way.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ...errors import ModelError
from ._common import (
    CONNECTION_ID,
    CONNECTION_NAME,
    CONNECTION_TYPE,
    EPOCH,
    ORGANIZATION_ID,
    OWNER_ID,
    base62,
    table_path,
)

#: model data type -> (Sigma type object, native type written to the snapshot)
_SIGMA_TYPE = {
    "int": ({"type": "integer"}, "INTEGER"),
    "decimal": ({"type": "number"}, "DECIMAL(38,9)"),
    "float": ({"type": "number"}, "DOUBLE"),
    "string": ({"type": "text"}, "VARCHAR"),
    "bool": ({"type": "boolean"}, "BOOLEAN"),
    "date": ({"type": "datetime"}, "DATE"),
    "timestamp": ({"type": "datetime"}, "TIMESTAMP"),
}

_INT = re.compile(r"^(TINYINT|SMALLINT|INTEGER|INT|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER"
                  r"|UBIGINT|INT[1248]|BYTEINT)$")
_FIXED = re.compile(r"^(DECIMAL|NUMERIC|NUMBER)(\((\d+)(,\s*(\d+))?\))?$")


def model_type(native: str) -> str:
    """A warehouse type name (DuckDB or Snowflake spelling) -> model data type."""
    t = native.strip().upper()
    if _INT.match(t):
        return "int"
    m = _FIXED.match(t)
    if m:
        scale = m.group(5)
        # Snowflake spells every integer NUMBER(38,0) (and bare NUMBER is that).
        # DuckDB's DECIMAL(p,0) still returns Decimal values, so it stays decimal.
        return "int" if m.group(1) == "NUMBER" and scale in (None, "0") else "decimal"
    if t in ("DOUBLE", "FLOAT", "REAL", "FLOAT4", "FLOAT8", "DOUBLE PRECISION"):
        return "float"
    if t.startswith(("VARCHAR", "TEXT", "STRING", "CHAR")):
        return "string"
    if t in ("BOOLEAN", "BOOL"):
        return "bool"
    if t == "DATE":
        return "date"
    if t.startswith("TIMESTAMP") or t == "DATETIME":
        return "timestamp"
    raise ModelError(f"warehouse type {native!r} has no model data type")


@dataclass
class Catalog:
    #: "schema.relation" -> {column (lower case): model data type}
    tables: dict[str, dict[str, str]] = field(default_factory=dict)
    #: custom SQL statement text -> {output column: model data type}
    statements: dict[str, dict[str, str]] = field(default_factory=dict)
    warehouse: Path | None = None

    def column_type(self, source: str, column: str) -> str:
        cols = self.tables.get(source)
        if cols is None:
            raise ModelError(f"table {source} is not in the connection's catalog "
                             f"(sync the connection)")
        if column not in cols:
            raise ModelError(f"column {column} of {source} is not in the connection's "
                             f"catalog (sync the connection)")
        return cols[column]

    def statement_types(self, statement: str) -> dict[str, str]:
        if statement in self.statements:
            return self.statements[statement]
        if self.warehouse is not None:
            types = describe(self.warehouse, statement)
            self.statements[statement] = types
            return types
        raise ModelError("custom SQL output columns are unknown: the statement is not in the "
                         "connection snapshot and no warehouse was given")

    @classmethod
    def from_warehouse(cls, warehouse: Path) -> Catalog:
        import duckdb

        tables: dict[str, dict[str, str]] = {}
        with duckdb.connect() as con:
            con.execute(f"ATTACH '{_quote(warehouse)}' AS wh (READ_ONLY)")
            rows = con.execute(
                "SELECT table_schema, table_name, column_name, data_type "
                "FROM information_schema.columns WHERE table_catalog = 'wh' "
                "ORDER BY table_schema, table_name, ordinal_position").fetchall()
        for schema, table, column, dtype in rows:
            try:
                kind = model_type(dtype)
            except ModelError:
                continue  # a type Sigma would show as text; nothing models it
            tables.setdefault(f"{schema}.{table}".lower(), {})[column.lower()] = kind
        return cls(tables=tables, warehouse=Path(warehouse))

    @classmethod
    def from_snapshots(cls, documents: list[dict]) -> Catalog:
        tables: dict[str, dict[str, str]] = {}
        statements: dict[str, dict[str, str]] = {}
        for doc in documents:
            for entry in doc.get("tables", []):
                path = entry["path"]
                source = ".".join(p.lower() for p in path[-2:])
                tables[source] = {c["name"].lower(): model_type(c["nativeType"])
                                  for c in entry.get("columns", [])}
            for entry in doc.get("statements", []):
                statements[entry["statement"]] = {
                    c["name"]: model_type(c["nativeType"]) for c in entry.get("columns", [])}
        return cls(tables=tables, statements=statements)

    def merged(self, other: Catalog) -> Catalog:
        """`other` wins: a live warehouse over a snapshot."""
        tables = {**self.tables, **other.tables}
        statements = {**self.statements, **other.statements}
        return Catalog(tables, statements, other.warehouse or self.warehouse)


def _quote(path: Path) -> str:
    return str(path).replace("'", "''")


def describe(warehouse: Path, statement: str) -> dict[str, str]:
    """Output column -> model type of a custom SQL statement, by asking DuckDB."""
    import duckdb

    with duckdb.connect() as con:
        con.execute(f"ATTACH '{_quote(warehouse)}' AS wh (READ_ONLY)")
        con.execute("USE wh")
        try:
            rows = con.execute(f"DESCRIBE SELECT * FROM ({statement}) AS q").fetchall()
        except duckdb.Error as exc:
            raise ModelError(f"custom SQL does not run: {exc}") from None
    return {name: model_type(dtype) for name, dtype, *_ in rows}


def connection_document(model) -> dict:
    """The connection snapshot for a model's warehouse tables (see the module
    docstring). Deterministic: tables in model order, columns in table order."""
    tables, statements = [], []
    seen: set[str] = set()
    for table in model.tables:
        columns = [{
            "name": (c.source.upper() if table.source else c.source),
            "type": _SIGMA_TYPE[c.data_type][0],
            "nativeType": _SIGMA_TYPE[c.data_type][1],
            "visibility": "included",
        } for c in table.columns if c.expr is None]
        if table.source is not None:
            if table.source in seen:
                continue
            seen.add(table.source)
            path = table_path(table.source)
            tables.append({"path": path, "kind": "table",
                           "inodeId": base62("inode", *table.source.split("."), length=22),
                           "columns": columns})
        else:
            statements.append({"statement": table.source_sql, "columns": columns})
    return {
        "organizationId": ORGANIZATION_ID,
        "connectionId": CONNECTION_ID,
        "isSample": False,
        "isAuditLog": False,
        "lastActiveAt": EPOCH,
        "name": CONNECTION_NAME,
        "type": CONNECTION_TYPE,
        "useOauth": False,
        "account": "alkera-local-dev",
        "warehouse": "ANALYTICS_WH",
        "user": "SIGMA_SERVICE",
        "role": "SIGMA_READER",
        "createdBy": OWNER_ID,
        "updatedBy": OWNER_ID,
        "createdAt": EPOCH,
        "updatedAt": EPOCH,
        "isArchived": False,
        "tables": tables,
        "statements": statements,
    }
