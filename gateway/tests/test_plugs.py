import pytest

from alkera_gateway.native import services
from alkera_gateway.plugs import (
    PLUGS,
    ROLES,
    SLOT_TOOLS,
    SLOTS,
    STACKS,
    ProfileError,
    main,
    parse_plug_args,
    readiness,
    resolve,
)
from alkera_gateway.tools import TOOLS


def test_slot_tools_partition_the_mcp_table():
    owned = [t for ts in SLOT_TOOLS.values() for t in ts]
    assert len(owned) == len(set(owned))
    assert set(owned) == set(TOOLS)
    assert set(SLOT_TOOLS) == set(SLOTS)


def test_plug_services_partition_native_names():
    owned = [s for p in PLUGS.values() for s in p.services]
    assert len(owned) == len(set(owned))
    assert set(owned) == set(services.NAMES)


def test_every_plug_names_a_known_slot():
    assert {p.slot for p in PLUGS.values()} <= set(SLOTS)
    assert all(p.status in ("shipped", "partial", "planned") for p in PLUGS.values())


def test_meeting_list_is_in_the_catalog():
    assert {"jupyter", "ml_workers", "redshift", "postgres", "databricks", "snowflake",
            "airflow", "dbt", "bigquery", "powerbi", "sigma", "tableau",
            "workspace"} <= set(PLUGS)


def test_stacks_name_a_plug_for_every_slot():
    for stack, slots in STACKS.items():
        assert set(slots) == set(SLOTS), stack
        for slot, plugs in slots.items():
            assert plugs and all(PLUGS[p].slot == slot for p in plugs), (stack, slot)


def test_de_laptop_is_todays_surface():
    p = resolve("de/laptop")
    assert p.tools == frozenset(TOOLS)
    assert p.excluded_tools == frozenset()
    assert set(p.services) == set(services.NAMES)  # order = catalog, not NAMES
    assert p.planned == ()


def test_da_laptop_with_and_without_its_notebook():
    assert resolve("da/laptop").slots["notebook"] == ("jupyter",)
    p = resolve("da/laptop", {"notebook": []})
    assert "notebook" not in p.slots
    assert {"sched_list", "deploy_run", "ingest_list", "metrics_list",
            "s3_put"} <= p.excluded_tools
    assert "sql_query" in p.tools and "bi_render" in p.tools
    assert p.services == ("metabase", "jira", "slack", "confluence")


def test_planned_refusal_names_every_planned_plug():
    with pytest.raises(ProfileError) as e:
        resolve("ds/databricks")
    for name in ("ml_workers", "mlflow", "databricks"):
        assert name in str(e.value)


def test_allow_planned_resolves_and_lists_them():
    assert set(resolve("de/aws", allow_planned=True).planned) == {"redshift", "tableau"}


def test_override_adds_a_slot_and_checks_plug_slot():
    p = resolve("de/laptop", {"notebook": "jupyter"}, allow_planned=True)
    assert p.slots["notebook"] == ("jupyter",)
    with pytest.raises(ProfileError, match="airflow.*orchestrator"):
        resolve("de/laptop", {"warehouse": ["airflow"]})


def test_override_replaces_stack_default():
    p = resolve("de/laptop", {"warehouse": ["postgres"]})
    assert p.slots["warehouse"] == ("postgres",)
    assert "postgres" in p.surfaces and "duckdb" not in p.surfaces


def test_refusal_lists_every_problem_at_once():
    with pytest.raises(ProfileError) as e:
        resolve("de/laptop", {"nosuchslot": [], "warehouse": ["nosuchplug"]})
    assert "nosuchslot" in str(e.value) and "nosuchplug" in str(e.value)


def test_unknown_names_are_refused():
    for bad in ("xx/laptop", "de/xx", "de", "de/laptop/x", ""):
        with pytest.raises(ProfileError):
            resolve(bad)
    with pytest.raises(ProfileError, match="nosuchslot"):
        resolve("de/laptop", {"nosuchslot": []})


def test_parse_plug_args():
    assert parse_plug_args(["notebook=", "warehouse=duckdb,postgres"]) == {
        "notebook": (), "warehouse": ("duckdb", "postgres")}
    with pytest.raises(ProfileError):
        parse_plug_args(["notebook"])


def test_readiness_matrix_covers_every_role_and_stack():
    rows = {r["profile"]: r for r in readiness()}
    assert len(rows) == len(ROLES) * len(STACKS)
    assert rows["de/laptop"]["status"] == "runnable"
    assert rows["de/postgres"]["status"] == "runnable"
    assert rows["de/snowflake"]["status"] == "planned"
    assert rows["de/snowflake"]["planned"] == ["sigma"]
    assert rows["da/laptop"]["status"] == "runnable"     # jupyter is partial, not planned


def test_main_matrix_and_show(capsys):
    assert main(["matrix"]) == 0
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == len(ROLES) * len(STACKS)
    assert main(["show", "da/laptop", "--plug", "notebook="]) == 0
    assert "metabase" in capsys.readouterr().out
    assert main(["show", "ds/laptop"]) == 2
    assert "ml_workers" in capsys.readouterr().err


def test_gateway_cli_delegates_plugs(capsys):
    from alkera_gateway.cli import main as cli_main

    assert cli_main(["plugs", "matrix"]) == 0
    assert "de/laptop" in capsys.readouterr().out
