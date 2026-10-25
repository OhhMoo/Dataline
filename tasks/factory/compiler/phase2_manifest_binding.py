"""Bind Phase 2 governance probes to tables and columns in the built world."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

ROLES = ("raw", "fact", "handoff", "close", "corrections")
GENERIC = frozenset({
    "raw.factory_events", "warehouse.fct_factory_events",
    "warehouse.factory_handoff", "warehouse.factory_close",
    "warehouse.factory_corrections",
})
TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


def _day(columns: list[str], *, preferred: str | None = None) -> str:
    if preferred in columns:
        return str(preferred)
    candidates = [column for column in columns
                  if column == "day" or column.endswith(("_day", "_date"))]
    if not candidates:
        raise ValueError(f"native table has no day/date column: {columns}")
    return candidates[0]


def _columns(con, table: str) -> list[str]:
    if not TABLE_RE.fullmatch(table):
        raise ValueError(f"unsafe or non-schema-qualified native table: {table!r}")
    try:
        cursor = con.execute(f"SELECT * FROM wh.{table} LIMIT 0")
    except Exception as exc:
        raise ValueError(f"native Phase 2 table {table} unavailable: {exc}") from exc
    return [item[0] for item in cursor.description]


def resolve_manifest_binding(world: Path, physical: Mapping,
                             declared_tables: list[str], contract: Mapping) -> dict:
    """Resolve H2/D1 SQL from actual native columns, with no sidecar defaults.

    A theme may omit handoff, close, or corrections from its base contract.
    In that case the composer must create a domain-named task table and declare
    it in its grader spec before the task can pass this gate.
    """
    tables = {}
    for role in ROLES:
        table = physical.get(role)
        if not isinstance(table, str) or table in GENERIC or table not in declared_tables:
            raise ValueError(f"Phase 2 {role} requires a declared native table, got {table!r}")
        tables[role] = table
    warehouse = Path(world) / "var/warehouse/warehouse.duckdb"
    if not warehouse.is_file():
        raise ValueError(f"Phase 2 world has no warehouse at {warehouse}")
    import duckdb
    con = duckdb.connect(":memory:")
    try:
        safe = str(warehouse).replace("'", "''")
        con.execute(f"ATTACH '{safe}' AS wh (READ_ONLY)")
        columns = {role: _columns(con, table) for role, table in tables.items()}
    finally:
        con.close()

    handoff = columns["handoff"]
    day = _day(handoff)
    grain = contract.get("grain_keys") or []
    entity = next((key for key in grain if key in handoff and key != day), None)
    if entity is None:
        entity = next((key for key in handoff if key.endswith("_id") and key != day), None)
    if entity is None:
        raise ValueError(f"native handoff {tables['handoff']} has no entity grain: {handoff}")
    correction_ref = next((key for key in ("correction_ref", "approval_ref", "ticket_ref")
                           if key in handoff), None)
    if correction_ref is None:
        raise ValueError(f"native handoff {tables['handoff']} has no correction reference")
    count = next((key for key in ("payments", "record_count", "event_count", "count")
                  if key in handoff), None)
    amount = next((key for key in ("net_cents", contract.get("amount_column"),
                                   "amount_cents", "total_cents") if key in handoff), None)
    if amount is None:
        amount = next((key for key in handoff if key.endswith("_cents")), None)
    if amount is None:
        raise ValueError(f"native handoff {tables['handoff']} has no monetary measure")
    correction_day = _day(columns["corrections"], preferred=day)
    close_day = _day(columns["close"], preferred=day)
    for required in ("ticket", "approved_by"):
        if required not in columns["corrections"]:
            raise ValueError(f"native corrections {tables['corrections']} lacks {required}")
    compare = [day, entity]
    if count and count not in compare:
        compare.append(count)
    if amount not in compare:
        compare.append(amount)
    close_columns = columns["close"]
    close_compare = compare if all(name in close_columns for name in compare) else None
    return {
        "raw_table": tables["raw"], "fact_table": tables["fact"],
        "handoff_table": tables["handoff"], "close_table": tables["close"],
        "corrections_table": tables["corrections"],
        "day_column": day, "entity_column": entity,
        "count_column": count, "amount_column": amount,
        "correction_ref_column": correction_ref,
        "corrections_day_column": correction_day,
        "close_day_column": close_day,
        "handoff_compare_columns": compare,
        "close_compare_columns": close_compare,
        "boundary_join_columns": [day, entity] if entity in close_columns else [day],
        "physical_columns": columns,
    }
