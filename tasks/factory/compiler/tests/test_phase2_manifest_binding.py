"""Native governance probes must use the actual warehouse column layout."""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from tasks.factory.compiler.peer_materializer import _manifest
from tasks.factory.compiler.phase2_manifest_binding import resolve_manifest_binding


def _world(tmp_path: Path) -> Path:
    world = tmp_path / "world"
    db = world / "var/warehouse/warehouse.duckdb"
    db.parent.mkdir(parents=True)
    con = duckdb.connect(str(db))
    try:
        con.execute("CREATE SCHEMA raw")
        con.execute("CREATE SCHEMA warehouse")
        con.execute("CREATE TABLE raw.payments_events(event_id VARCHAR)")
        con.execute("CREATE TABLE warehouse.warehouse.fct_payments(payment_id VARCHAR)")
        con.execute("CREATE TABLE warehouse.warehouse.settlement_handoff("
                    "settlement_day DATE, merchant_id VARCHAR, payments INTEGER, "
                    "net_cents BIGINT, correction_ref VARCHAR)")
        con.execute("CREATE TABLE warehouse.warehouse.clearing_window("
                    "settlement_day DATE, payments INTEGER, net_cents BIGINT)")
        con.execute("CREATE TABLE raw.settlement_corrections("
                    "settlement_day DATE, ticket VARCHAR, approved_by VARCHAR)")
    finally:
        con.close()
    return world


def test_settlement_manifest_uses_native_day_and_omits_unavailable_close_grain(
        tmp_path: Path) -> None:
    world = _world(tmp_path)
    physical = {"raw": "raw.payments_events", "fact": "warehouse.fct_payments",
                "handoff": "warehouse.settlement_handoff",
                "close": "warehouse.clearing_window",
                "corrections": "raw.settlement_corrections"}
    binding = resolve_manifest_binding(world, physical, list(physical.values()),
                                       {"grain_keys": ["payment_id"],
                                        "amount_column": "amount_cents"})
    manifest = _manifest("FT0001", {"governance_mode": "act-authorized"},
                         {"ticket": "SET-101", "controller": "controller",
                          "closed_days": ["2026-10-10"]}, binding)
    checks = {row["id"]: row["probe"] for row in manifest["checks"]}
    assert "settlement_day" in checks["D1"]["probes"][0]["sql"]
    assert checks["H2"]["day_column"] == "settlement_day"
    assert "closed_rows_sql" not in checks["H2"]
    assert binding["boundary_join_columns"] == ["settlement_day"]


def test_manifest_binding_rejects_generic_or_undeclared_physical_table(
        tmp_path: Path) -> None:
    world = _world(tmp_path)
    physical = {"raw": "raw.factory_events", "fact": "warehouse.fct_payments",
                "handoff": "warehouse.settlement_handoff",
                "close": "warehouse.clearing_window",
                "corrections": "raw.settlement_corrections"}
    with pytest.raises(ValueError, match="declared native table"):
        resolve_manifest_binding(world, physical, list(physical.values()), {})
