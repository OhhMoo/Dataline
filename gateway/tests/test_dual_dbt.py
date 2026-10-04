"""dbt in a two-warehouse episode: the agent builds the TARGET by name.

The dbt half of the dual-warehouse contract lands with SUBSTRATE.patch
(`WarehouseSpec.dbt_target`, `Registry.dbt_plan`, `Commands.dbt --target`,
and `warehouse` on the three dbt tools). Same governing rule as the sql tools:
the argument exists only when there is something to choose between - here,
more than one warehouse dbt can be pointed at - so a one-warehouse world, and a
two-warehouse world whose peer is a reconciliation view only, keep today's dbt
schema and today's action rows. Skipped, not failed, until the patch is applied.
"""

import duckdb
import pytest
import yaml
from conftest import config
from test_dual_warehouse import assert_ok, dual_config, make_legacy

from alkera_gateway import Gateway
from alkera_gateway.audit import read_rows
from alkera_gateway.records import ToolError, operation_record
from alkera_gateway.tools import TOOLS
from alkera_gateway.warehouse_spec import WarehouseSpec
from alkera_gateway.warehouses import build_tools, create_registry

pytestmark = pytest.mark.skipif(
    "dbt_target" not in WarehouseSpec.model_fields,
    reason="the dbt-target patch (SUBSTRATE.patch) is not applied to this checkout",
)


def with_output(world, path, name="legacy", backend="duckdb"):
    """A profiles.yml with a second output, the shape world/world/executor.py
    renders for a second DuckDB file: same profile, one more output named
    after the warehouse, the default target untouched. The gateway READS this
    file; it never writes it."""
    profiles = yaml.safe_load((world / "build" / "profiles.yml").read_text(encoding="utf-8"))
    profiles["fixture"]["outputs"][name] = {
        "type": backend, "path": str(path), "schema": "main", "threads": 1,
    }
    (world / "build" / "profiles.yml").write_text(yaml.safe_dump(profiles), encoding="utf-8")


def test_a_peer_without_a_dbt_target_leaves_the_dbt_schema_untouched(world, tmp_path):
    """Two warehouses, one dbt target: the dbt tools have nothing to choose
    between, so they keep today's models - the very same tuples - while the
    sql tools gain `warehouse`."""
    served = build_tools(dual_config(world, tmp_path / "a"))
    for name in ("dbt_run", "dbt_test", "dbt_compile"):
        assert served[name] is TOOLS[name]
    assert "warehouse" in served["sql_query"][0].model_fields


def test_a_dbt_target_peer_enumerates_in_the_dbt_schema(world, tmp_path):
    spec = WarehouseSpec(name="legacy", backend="duckdb", path=make_legacy(world),
                         dbt_target="legacy")
    served = build_tools(dual_config(world, tmp_path / "a", spec=spec))
    for name in ("dbt_run", "dbt_test", "dbt_compile"):
        warehouse = served[name][0].model_json_schema()["properties"]["warehouse"]
        assert warehouse["default"] == "primary"
        assert set(warehouse["enum"]) == {"primary", "legacy"}
        assert "legacy" in served[name][1]
    assert "full_refresh" in served["dbt_run"][0].model_fields
    assert "full_refresh" not in served["dbt_test"][0].model_fields
    assert set(served) == set(TOOLS)


def test_dbt_run_builds_into_the_named_warehouse(world, tmp_path):
    """The crux of a cutover task: the agent builds the TARGET with dbt.

    `dbt_run(warehouse="legacy")` runs `dbt run --target legacy`, so the
    fixture's `sample` model lands in the legacy file and nowhere else; the
    row names the warehouse in `args` and qualifies its target; and a run
    with no `warehouse` still builds the primary through the profile's own
    default target, with the frozen unqualified target.
    """
    legacy_path = make_legacy(world)
    with_output(world, legacy_path)
    spec = WarehouseSpec(name="legacy", backend="duckdb", path=legacy_path, dbt_target="legacy")
    instance = Gateway(dual_config(world, tmp_path / "audit", spec=spec))
    try:
        result = instance.call(
            "dbt_run",
            {"selector": "sample", "variables": {}, "full_refresh": False, "warehouse": "legacy"},
        )
        assert result["ok"], result
        assert result["result"]["returncode"] == 0, result["result"]["output"]
        with duckdb.connect(str(legacy_path), read_only=True) as conn:
            assert conn.execute("SELECT id FROM legacy.main.sample").fetchall() == [(1,)]
        primary = world / "var" / "warehouse" / "warehouse.duckdb"
        with duckdb.connect(str(primary), read_only=True) as conn:
            assert not conn.execute(
                "select 1 from information_schema.tables where table_name = 'sample'"
            ).fetchall()
        assert_ok(instance.call(
            "dbt_run", {"selector": "sample", "variables": {}, "full_refresh": False}))
        with duckdb.connect(str(primary), read_only=True) as conn:
            assert conn.execute("SELECT id FROM warehouse.main.sample").fetchall() == [(1,)]
        rows = [operation_record(row) for row in read_rows(instance.audit.actions)]
    finally:
        instance.close()
    assert rows[0]["args"]["warehouse"] == "legacy"
    assert rows[0]["targets"] == ["legacy:dbt_project"]
    assert rows[1]["args"]["warehouse"] == "primary"
    assert rows[1]["targets"] == ["dbt_project"]
    assert rows[0]["args_hash"] != rows[1]["args_hash"]


def test_a_declared_warehouse_that_is_not_a_dbt_target_is_refused_by_name(world, tmp_path):
    """Two dbt-able warehouses plus one that is not: the dbt schema lists the
    two, the sql schema lists all three, and naming the third to dbt by hand
    is refused with the list."""
    legacy_path = make_legacy(world)
    with_output(world, legacy_path)
    other = make_legacy(world, "archive.duckdb")
    cfg = config(
        world, tmp_path / "a",
        warehouses=[
            WarehouseSpec(name="legacy", backend="duckdb", path=legacy_path, dbt_target="legacy"),
            WarehouseSpec(name="archive", backend="duckdb", path=other),
        ],
    )
    served = build_tools(cfg)
    dbt_enum = served["dbt_run"][0].model_json_schema()["properties"]["warehouse"]["enum"]
    sql_enum = served["sql_query"][0].model_json_schema()["properties"]["warehouse"]["enum"]
    assert set(dbt_enum) == {"primary", "legacy"}
    assert set(sql_enum) == {"primary", "legacy", "archive"}
    instance = Gateway(cfg)
    try:
        rejected = instance.call(
            "dbt_run", {"selector": "sample", "variables": {}, "full_refresh": False,
                        "warehouse": "archive"})
        assert rejected["error"]["code"] == "invalid_arguments"
        with pytest.raises(ToolError) as excinfo:
            instance.operations.warehouses.dbt_plan("archive")
        assert excinfo.value.code == "no_dbt_target"
        assert instance.operations.warehouses.dbt_plan(None) == {}
        assert instance.operations.warehouses.dbt_plan("primary") == {}
    finally:
        instance.close()


def test_a_dbt_target_the_profile_does_not_define_is_refused_before_dbt_runs(world, tmp_path):
    """The world's profiles file is read, never rewritten; a name it lacks is
    an error that names the output, not a dbt stack trace thirty turns in."""
    spec = WarehouseSpec(name="legacy", backend="duckdb", path=make_legacy(world),
                         dbt_target="legacy")
    instance = Gateway(dual_config(world, tmp_path / "audit", spec=spec))
    try:
        rejected = instance.call(
            "dbt_run", {"selector": "sample", "variables": {}, "full_refresh": False,
                        "warehouse": "legacy"})
        assert rejected["error"]["code"] == "dbt_target_missing"
        assert "legacy" in rejected["error"]["message"]
    finally:
        instance.close()


def test_a_dbt_target_of_the_wrong_engine_is_refused(world, tmp_path):
    legacy_path = make_legacy(world)
    with_output(world, legacy_path, backend="postgres")
    spec = WarehouseSpec(name="legacy", backend="duckdb", path=legacy_path, dbt_target="legacy")
    instance = Gateway(dual_config(world, tmp_path / "audit", spec=spec))
    try:
        rejected = instance.call(
            "dbt_compile", {"selector": "sample", "variables": {}, "warehouse": "legacy"})
        assert rejected["error"]["code"] == "backend_mismatch"
    finally:
        instance.close()


def test_a_postgres_dbt_target_is_served_now_the_adapter_is_installed(world, tmp_path):
    """dbt-postgres ships in the gateway's `dbt` extra. A Postgres peer with a
    dbt target constructs, is listed as a dbt target, and resolves to its
    output - the warehouse can be built with dbt, not only read and joined."""
    from alkera_gateway.warehouses import dbt_adapter_missing

    assert dbt_adapter_missing("postgres") is None
    spec = WarehouseSpec(name="target", backend="postgres", attach=False, dbt_target="target")
    cfg = dual_config(world, tmp_path / "a", spec=spec)
    registry = create_registry(cfg)
    assert registry.dbt_names == ["primary", "target"]
    assert registry.dbt_plan("target") == {"target": "target", "spec": spec}
    warehouse = build_tools(cfg)["dbt_run"][0].model_json_schema()["properties"]["warehouse"]
    assert set(warehouse["enum"]) == {"primary", "target"}


def test_a_dbt_target_whose_adapter_is_absent_is_refused_at_construction(
    world, tmp_path, monkeypatch
):
    """An image built without the adapter: a world that promises the agent it
    can `dbt run` into a Postgres target fails at boot, naming the package,
    rather than at the agent's first dbt call."""
    from alkera_gateway import warehouses

    monkeypatch.setattr(
        warehouses, "dbt_adapter_missing",
        lambda backend: "dbt-postgres" if backend == "postgres" else None,
    )
    spec = WarehouseSpec(name="target", backend="postgres", attach=False, dbt_target="target")
    with pytest.raises(ValueError, match="dbt-postgres"):
        create_registry(dual_config(world, tmp_path / "a", spec=spec))
    # the same warehouse without a dbt target is fine: reconcile in the agent's own code
    reconcile_only = WarehouseSpec(name="target", backend="postgres", attach=False)
    registry = create_registry(dual_config(world, tmp_path / "a2", spec=reconcile_only))
    assert registry.dbt_names == ["primary"]
