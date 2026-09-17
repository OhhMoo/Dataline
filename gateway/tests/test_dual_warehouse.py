"""Two live warehouses in one episode.

The first three tests are the ones that matter, and they are negative: they
assert that a world declaring ONE warehouse is not merely similar to today but
identical to it - the same tool table object, the same JSON schema bytes, the
same canonical action-log bytes. Every existing task and all four themes ride on
that, so it is checked first and checked by identity rather than by equality of
a hand-copied literal.

The rest establish what two warehouses can actually do, and - equally important
- what they cannot: no ATTACH the world did not declare, no write to a
read-only warehouse, and no pretence that Snowflake can be joined to anything.
"""

import json

import duckdb
import pytest
from conftest import config

from alkera_gateway import Gateway
from alkera_gateway.audit import read_rows
from alkera_gateway.config import GatewayConfig
from alkera_gateway.records import canonical, operation_record
from alkera_gateway.tools import TOOLS
from alkera_gateway.warehouse import DuckDBWarehouse
from alkera_gateway.warehouse_spec import WarehouseSpec
from alkera_gateway.warehouses import build_tools, create_registry, warehouse_specs

# The new modules are additive, but the four-hunk patch set they need is applied
# separately. Until it lands, skip rather than fail: a checkout mid-migration
# should report an honest gap, not thirteen red tests that say nothing.
pytestmark = pytest.mark.skipif(
    "warehouses" not in GatewayConfig.model_fields,
    reason="dual-warehouse patch set is not applied to this checkout",
)

DECIMAL_10_50 = {"type": "decimal", "value": "10.50"}
DECIMAL_20_25 = {"type": "decimal", "value": "20.25"}


def assert_ok(result):
    assert result["ok"], result["error"]
    return result["result"]


def make_legacy(root, name="legacy.duckdb", amounts=((1, "10.50"), (2, "99.99"))):
    """A second DuckDB warehouse beside the primary, seeded WITHOUT the connector.

    This is the shape world seeding has to take: the file is written by the
    duckdb driver directly, exactly as `world/world/themes/*/gen.py` already
    writes the primary. Nothing here goes through `alkera_gateway`.
    """
    path = root / "var" / "warehouse" / name
    with duckdb.connect(str(path)) as conn:
        conn.execute("CREATE SCHEMA warehouse")
        conn.execute("CREATE TABLE warehouse.items(id INTEGER, amount DECIMAL(10,2))")
        conn.execute(
            "INSERT INTO warehouse.items VALUES "
            + ", ".join(f"({i}, {a})" for i, a in amounts)
        )
    return path


def dual_config(root, audit, **overrides):
    spec = overrides.pop("spec", None) or WarehouseSpec(
        name="legacy", backend="duckdb", path=make_legacy(root)
    )
    return config(root, audit, warehouses=[spec], **overrides)


# --------------------------------------------------------- one warehouse is today


def test_one_warehouse_serves_the_tool_table_object_itself(world, tmp_path):
    """Identity, not equality. A copy could drift; this cannot."""
    single = config(world, tmp_path / "a")
    assert build_tools(single) is TOOLS
    assert list(warehouse_specs(single)) == ["primary"]


def test_one_warehouse_leaves_the_json_schema_untouched(world, tmp_path):
    """What the agent is served by list_tools, byte for byte."""
    single = config(world, tmp_path / "a")
    served = build_tools(single)
    for name in ("sql_query", "sql_execute"):
        assert canonical(served[name][0].model_json_schema()) == canonical(
            TOOLS[name][0].model_json_schema()
        )
        assert served[name][1] == TOOLS[name][1]
    assert len(served) == len(TOOLS)


def test_declaring_one_warehouse_sets_no_new_config_field():
    """A world that says nothing about warehouses still serialises as it did."""
    dumped = GatewayConfig().model_dump(exclude_unset=True)
    assert "warehouse_backend" not in dumped
    assert "warehouses" not in dumped
    assert "warehouse_name" not in dumped


def test_one_warehouse_action_rows_are_byte_identical(gateway):
    """The frozen 9.2.2 row. `args` carries no warehouse key at all.

    This is the assertion the naive design fails: an optional field on the
    shared `Query`/`SQL` models lands in `model_dump()` as `"warehouse": null`
    for every call in every existing task, which changes `args`, `args_hash`,
    `args_summary` and therefore the canonical bytes of every sql row ever
    recorded.
    """
    assert_ok(gateway.call("sql_query", {"sql": "SELECT id FROM warehouse.items ORDER BY id"}))
    update = "UPDATE warehouse.items SET amount = 1 WHERE id = 1"
    assert_ok(gateway.call("sql_execute", {"sql": update}))
    rows = [operation_record(row) for row in read_rows(gateway.audit.actions)]
    assert rows[0]["args"] == {"sql": "SELECT id FROM warehouse.items ORDER BY id", "limit": 1000}
    assert rows[1]["args"] == {"sql": update}
    # Unqualified, because there is nothing to disambiguate against.
    assert rows[0]["read_targets"] == ["warehouse.items"]
    assert rows[1]["targets"] == ["warehouse.items"]


def test_one_warehouse_registry_is_the_frozen_factory(world, tmp_path):
    registry = create_registry(config(world, tmp_path / "a"))
    assert type(registry.primary) is DuckDBWarehouse
    assert registry.multi is False
    assert registry.get(None) is registry.primary


# ------------------------------------------------------------- two warehouses work


def test_the_second_warehouse_is_addressable(world, tmp_path):
    instance = Gateway(dual_config(world, tmp_path / "audit"))
    try:
        primary = assert_ok(
            instance.call("sql_query", {"sql": "SELECT amount FROM warehouse.items WHERE id = 2"})
        )
        legacy = assert_ok(
            instance.call(
                "sql_query",
                {"sql": "SELECT amount FROM warehouse.items WHERE id = 2", "warehouse": "legacy"},
            )
        )
        assert primary["rows"] == [[DECIMAL_20_25]]
        assert legacy["rows"] == [[{"type": "decimal", "value": "99.99"}]]
    finally:
        instance.close()


def test_reconciliation_happens_in_one_statement(world, tmp_path):
    """The point of the whole build: compare the two marts in SQL.

    A DuckDB peer is attached into the primary's catalog before the connection
    is locked down, so a FULL OUTER JOIN across the two is a single
    `sql_query`, not a two-call pull-and-diff.
    """
    instance = Gateway(dual_config(world, tmp_path / "audit"))
    try:
        result = assert_ok(
            instance.call(
                "sql_query",
                {
                    "sql": "SELECT p.id, p.amount AS primary_amount, l.amount AS legacy_amount "
                    "FROM wh.warehouse.items p "
                    "FULL OUTER JOIN legacy.warehouse.items l USING (id) "
                    "WHERE p.amount IS DISTINCT FROM l.amount ORDER BY 1"
                },
            )
        )
        assert result["rows"] == [[2, DECIMAL_20_25, {"type": "decimal", "value": "99.99"}]]
    finally:
        instance.close()


def test_every_sql_row_names_its_warehouse(world, tmp_path):
    """The grader reads the action log, so the log has to say where each statement went."""
    instance = Gateway(dual_config(world, tmp_path / "audit"))
    try:
        instance.call("sql_query", {"sql": "SELECT id FROM warehouse.items"})
        instance.call("sql_query", {"sql": "SELECT id FROM warehouse.items", "warehouse": "legacy"})
        instance.call("sql_execute", {"sql": "DELETE FROM warehouse.items WHERE id = 1"})
        rows = [operation_record(row) for row in read_rows(instance.audit.actions)]
    finally:
        instance.close()
    assert [row["args"]["warehouse"] for row in rows] == ["primary", "legacy", "primary"]
    # Distinct args_hash for the same SQL against different warehouses, so the
    # doom-loop signal does not read a dual-run as a repeat.
    assert rows[0]["args_hash"] != rows[1]["args_hash"]
    # Qualified targets: a write to the primary is not credited with a read of
    # the legacy table of the same name.
    assert rows[0]["read_targets"] == ["primary:warehouse.items"]
    assert rows[1]["read_targets"] == ["legacy:warehouse.items"]
    assert rows[2]["targets"] == ["primary:warehouse.items"]


def test_the_schema_enumerates_the_declared_warehouses(world, tmp_path):
    served = build_tools(dual_config(world, tmp_path / "a"))
    schema = served["sql_query"][0].model_json_schema()
    warehouse = schema["properties"]["warehouse"]
    assert warehouse["default"] == "primary"
    assert set(warehouse["enum"]) == {"primary", "legacy"}
    assert "legacy" in served["sql_query"][1]
    assert set(served) == set(TOOLS)  # no new tool names


def test_a_write_to_the_secondary_is_visible_to_the_next_join(world, tmp_path):
    """Sequencing safety: Gateway.call serialises, so the attachments never race."""
    instance = Gateway(dual_config(world, tmp_path / "audit"))
    try:
        assert_ok(
            instance.call(
                "sql_execute",
                {"sql": "UPDATE warehouse.items SET amount = 20.25 WHERE id = 2",
                 "warehouse": "legacy"},
            )
        )
        result = assert_ok(
            instance.call(
                "sql_query",
                {"sql": "SELECT count(*) AS n FROM wh.warehouse.items p "
                        "JOIN legacy.warehouse.items l USING (id) WHERE p.amount = l.amount"},
            )
        )
        assert result["rows"] == [[2]]
    finally:
        instance.close()


def test_the_attached_catalog_is_a_reconciliation_view_not_a_write_path(world, tmp_path):
    """One rule for every peer: attached read-only, written through its own name.

    Two write paths onto one warehouse - `UPDATE legacy.warehouse.items` through
    the primary's catalog and `sql_execute(warehouse="legacy")` through the
    peer's own connector - would mean two open handles on the same DuckDB file
    or two Postgres transactions, for no capability a cutover task needs.
    """
    instance = Gateway(dual_config(world, tmp_path / "audit"))
    try:
        rejected = instance.call(
            "sql_execute", {"sql": "UPDATE legacy.warehouse.items SET amount = 0"}
        )
        assert not rejected["ok"]
        assert "read-only mode" in rejected["error"]["message"]
        # The same write, addressed by name, is allowed.
        assert_ok(
            instance.call(
                "sql_execute",
                {"sql": "UPDATE warehouse.items SET amount = 0", "warehouse": "legacy"},
            )
        )
    finally:
        instance.close()


# ------------------------------------------------------------------- and the limits


def test_an_undeclared_warehouse_cannot_be_named(world, tmp_path):
    instance = Gateway(dual_config(world, tmp_path / "audit"))
    try:
        rejected = instance.call("sql_query", {"sql": "SELECT 1", "warehouse": "snowflake"})
        assert rejected["error"]["code"] == "invalid_arguments"
    finally:
        instance.close()


def test_the_agent_cannot_attach_a_warehouse_itself(world, tmp_path):
    """Two independent refusals, and neither is a rule we maintain.

    The tool layer refuses ATTACH as outside the warehouse surface. Underneath,
    DuckDB refuses it too, because the gateway locks the connection down after
    it has attached everything the world declared.
    """
    instance = Gateway(dual_config(world, tmp_path / "audit"))
    other = make_legacy(world, "undeclared.duckdb")
    try:
        rejected = instance.call("sql_execute", {"sql": f"ATTACH '{other}' AS sneaky"})
        assert rejected["error"]["code"] == "unsupported_sql"
        # And the engine, with the tool layer removed entirely:
        connector = instance.operations.warehouses.primary
        with connector.connect(read_only=True) as conn:
            with pytest.raises(duckdb.Error) as excinfo:
                conn.execute(f"ATTACH '{other}' AS sneaky")
        assert "disabled by configuration" in str(excinfo.value)
    finally:
        instance.close()


def test_a_read_only_warehouse_refuses_every_write(world, tmp_path):
    """The decommissioned writer, as a state of the world rather than a promise."""
    spec = WarehouseSpec(
        name="legacy", backend="duckdb", path=make_legacy(world), read_only=True
    )
    instance = Gateway(dual_config(world, tmp_path / "audit", spec=spec))
    try:
        rejected = instance.call(
            "sql_execute",
            {"sql": "DELETE FROM warehouse.items", "warehouse": "legacy"},
        )
        assert rejected["error"]["code"] == "warehouse_read_only"
        readable = assert_ok(
            instance.call(
                "sql_query", {"sql": "SELECT count(*) AS n FROM warehouse.items",
                              "warehouse": "legacy"}
            )
        )
        assert readable["rows"] == [[2]]
    finally:
        instance.close()


def test_a_snowflake_peer_must_admit_it_cannot_be_joined(world, tmp_path):
    """A task that assumes a join the stack cannot do fails at boot, not at turn 30."""
    spec = WarehouseSpec(name="target", backend="snowflake")
    with pytest.raises(ValueError, match="cannot be attached"):
        create_registry(dual_config(world, tmp_path / "a", spec=spec))
    allowed = WarehouseSpec(name="target", backend="snowflake", attach=False)
    registry = create_registry(dual_config(world, tmp_path / "a2", spec=allowed))
    assert registry.names == ["primary", "target"]
    assert registry.attachments["target"] == "attach disabled by the world"


def test_a_secondary_may_not_shadow_the_primary_file(world, tmp_path):
    primary = world / "var" / "warehouse" / "warehouse.duckdb"
    with pytest.raises(ValueError, match="names the primary warehouse file"):
        config(
            world,
            tmp_path / "a",
            warehouses=[WarehouseSpec(name="legacy", backend="duckdb", path=primary)],
        )


def test_a_duplicate_warehouse_name_is_refused(world, tmp_path):
    with pytest.raises(ValueError, match="declared twice"):
        config(
            world,
            tmp_path / "a",
            warehouses=[WarehouseSpec(name="primary", backend="duckdb", path=make_legacy(world))],
        )


def test_a_duckdb_warehouse_needs_a_path_and_a_remote_one_must_not_have_one():
    with pytest.raises(ValueError, match="needs a path"):
        WarehouseSpec(name="legacy", backend="duckdb")
    with pytest.raises(ValueError, match="must not carry a path"):
        WarehouseSpec(name="legacy", backend="postgres", path="/tmp/x.duckdb")


def test_no_tool_argument_names_a_socket_primitive(world, tmp_path):
    """The property `gateway/docker/containment.py` asserts, checked here too.

    `warehouse` is a logical name resolved against config. The moment it were a
    DSN or a host:port the gateway would stop being the only channel.
    """
    denied = {"host", "hostname", "port", "url", "uri", "endpoint", "dsn", "server",
              "address", "addr", "proxy", "connection", "conn", "account", "network"}
    for model, _ in build_tools(dual_config(world, tmp_path / "a")).values():
        assert not denied & {name.lower() for name in model.model_fields}


def test_the_config_round_trips_through_json(world, tmp_path):
    """A world declares its warehouses in the episode config file, so it has to load."""
    source = dual_config(world, tmp_path / "a")
    path = tmp_path / "gateway.json"
    path.write_text(source.model_dump_json(), encoding="utf-8")
    loaded = GatewayConfig.load(path)
    assert [spec.name for spec in loaded.warehouses] == ["legacy"]
    assert loaded.warehouses[0].path == source.warehouses[0].path
    assert json.loads(path.read_text(encoding="utf-8"))["warehouses"][0]["backend"] == "duckdb"
