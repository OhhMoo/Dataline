"""tool_tour.py: probe loading, the verdict, truth and the world seed - no docker.

The live tour is `tool_tour.py --topology container` (evidence/tool_tour_container.json).
"""
from __future__ import annotations

import json
import re
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

import episode
import tool_tour as tt
from tool_tour import Probe

ELEVEN = {"duckdb", "dbt", "mf", "jupyter", "postgres", "snowflake", "powerbi", "tableau",
          "sigma", "human", "sched"}
SF = Probe(id="snowflake.connector.net_revenue_feb_2026", profile="da/snowflake", plugs={},
           command="true", expect_tools=("snowflake.statement",),
           truth_sql="SELECT 2361224.00", instruction="February 2026 Net Revenue.")
ALLOWED = {("snowflake.internal", 8443), ("jira.internal", 8080)}


@pytest.fixture
def audit(tmp_path, monkeypatch):
    """Canned 9.2.2 rows, written by the real native writer into a scratch log."""
    from alkera_gateway.native import audit as native_audit

    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(tmp_path / "actions.jsonl"))
    monkeypatch.setenv("ALKERA_VTS", "2026-03-02T00:00:00Z")

    def row(tool: str, **kw) -> dict:
        return native_audit.record(tool=tool, args=kw.pop("args", {"sql": "SELECT 1"}),
                                   op_class=kw.pop("op_class", "read"),
                                   targets=kw.pop("targets", ["warehouse.fct_orders"]), **kw)
    return row


def judge(probe=SF, *, stdout='{"value": "2361224.0"}\n', rows=(), destinations=None,
          truth=Decimal("2361224.00"), returncode=0):
    return tt.verdict(probe, returncode=returncode, stdout=stdout, rows=list(rows),
                      destinations=destinations or {}, allowed=ALLOWED, truth=truth)


# --------------------------------------------------------------------------- loading
def test_load_probes_finds_every_module():
    files = {p.stem for p in tt.PROBES_DIR.glob("*.py") if p.stem != "__init__"}
    probes = tt.load_probes()
    assert {p.tool for p in probes} == files == set(tt.probe_modules())
    assert ELEVEN <= files


def test_probe_ids_unique():
    ids = [p.id for p in tt.load_probes()]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+", i) for i in ids), ids


def test_load_probes_filters_and_refuses_unknown_tools():
    assert {p.tool for p in tt.load_probes(["sched", "duckdb"])} == {"sched", "duckdb"}
    with pytest.raises(ValueError, match="redshiftt"):
        tt.load_probes(["redshiftt"])


def test_every_probe_is_runnable_as_written():
    """Its profile resolves (no planned plug), its command is valid bash ending in the
    answer line, and its brief does not hand over the command."""
    for probe in tt.load_probes():
        assert episode.environment_profile(probe.profile, dict(probe.plugs)) is not None
        done = subprocess.run(["bash", "-n"], input=probe.command, text=True,
                              capture_output=True)
        assert done.returncode == 0, (probe.id, done.stderr)
        assert probe.expect_tools and probe.instruction.strip(), probe.id
        assert probe.command.strip().splitlines()[0] not in probe.instruction, probe.id


def test_jupyter_probe_notebook_is_valid_and_its_cell_compiles():
    probe = next(p for p in tt.load_probes(["jupyter"]))
    text = re.search(r"<<'NB'\n(.*?)\nNB\n", probe.command, re.S).group(1)
    notebook = json.loads(text)
    assert notebook["metadata"]["kernelspec"]["name"] == "alkera-python"
    compile(notebook["cells"][0]["source"], "cell", "exec")


def test_known_findings_name_real_probes():
    ids = {p.id for p in tt.load_probes()}
    known = tt.known_findings()
    assert set(known) <= ids
    assert "Task 9" in known["postgres.psql.items_count"]


# --------------------------------------------------------------------------- verdict
def test_verdict_passes_with_the_row_the_value_and_an_allowed_destination(audit):
    rows = [audit("snowflake.statement"), audit("shim.python", op_class="exec", targets=[])]
    result = judge(rows=rows, destinations={("snowflake.internal", 8443): 1})
    assert result["verdict"] == "PASS", result["failures"]
    assert result["rows"] == 2 and result["value_ok"] is True


def test_verdict_fails_when_expected_tool_missing(audit):
    rows = [audit("shim.python", op_class="exec", targets=[])]
    result = judge(rows=rows)
    assert result["verdict"] == "FAIL"
    assert result["missing_tools"] == ["snowflake.statement"]
    assert judge(rows=[])["verdict"] == "FAIL"


def test_verdict_fails_on_value_mismatch(audit):
    result = judge(rows=[audit("snowflake.statement")], stdout='{"value": "2386405.00"}\n')
    assert result["verdict"] == "FAIL" and result["value_ok"] is False
    assert any("!= truth" in f for f in result["failures"])


def test_verdict_fails_on_unallowed_destination(audit):
    result = judge(rows=[audit("snowflake.statement")],
                   destinations={("snowflake.internal", 8443): 1, ("example.com", 443): 1})
    assert result["verdict"] == "FAIL"
    assert any("example.com:443" in f for f in result["failures"])
    # a recorded-but-never-dialled address is not a connection
    assert judge(rows=[audit("snowflake.statement")],
                 destinations={("example.com", 443): 0})["verdict"] == "PASS"


def test_verdict_fails_on_a_bad_row_exit_or_answer_line(audit):
    row = audit("snowflake.statement")
    broken = {**row, "args_hash": "0" * 64}
    assert judge(rows=[broken])["verdict"] == "FAIL"
    assert judge(rows=[row], returncode=1)["verdict"] == "FAIL"
    assert judge(rows=[row], stdout="2361224.00\n")["verdict"] == "FAIL"
    assert judge(rows=[row], stdout='{"answer": 1}\n')["verdict"] == "FAIL"


def test_verdict_doc_when_the_path_is_refused_by_name(audit):
    result = judge(rows=[audit("snowflake.statement")],
                   stdout='{"doc": "dbt-databricks refused by name"}\n')
    assert result["verdict"] == "DOC" and result["doc"] == "dbt-databricks refused by name"
    assert judge(rows=[], stdout='{"doc": "x"}\n')["verdict"] == "FAIL"


def test_verdict_without_truth_records_the_value(audit):
    probe = Probe(id="postgres.psql.items_count", profile="de/postgres", plugs={},
                  command="true", expect_tools=("psql.statement",), truth_sql=None,
                  instruction="Count warehouse.items.")
    result = judge(probe, rows=[audit("psql.statement")], stdout='{"value": 2}\n', truth=None)
    assert result["verdict"] == "PASS" and result["value"] == 2 and result["value_ok"] is None


def test_same_value_compares_numbers_as_numbers():
    assert tt.same_value("2361224.00", Decimal("2361224.00"))
    assert tt.same_value(2361224.0, Decimal("2361224.00"))
    assert tt.same_value(33733790, Decimal("33733790.00"))
    assert not tt.same_value("2361224.01", Decimal("2361224.00"))
    assert tt.same_value("todo", "todo") and not tt.same_value("done", "todo")
    assert not tt.same_value(None, Decimal("1"))


# --------------------------------------------------------------------------- truth
def test_truth_reads_the_warehouse_and_world_files(tmp_path):
    import duckdb

    root = tmp_path / "world"
    (root / "var" / "warehouse").mkdir(parents=True)
    (root / "var" / "workflow").mkdir(parents=True)
    con = duckdb.connect(str(root / tt.WAREHOUSE_REL))
    con.execute("CREATE SCHEMA warehouse")
    con.execute("CREATE TABLE warehouse.warehouse.fct_orders AS SELECT * FROM (VALUES "
                "(100.50, 'completed'), (20.00, 'cancelled'), (9.50, 'completed')) "
                "t(order_amount, order_status)")
    con.close()
    (root / "var" / "workflow" / "issues.json").write_text(json.dumps(
        {"issues": [{"id": "ANA-1", "status": "todo"}, {"id": "ANA-2", "status": "done"}]}))
    assert tt.truth("SELECT SUM(order_amount) FROM warehouse.warehouse.fct_orders "
                    "WHERE order_status = 'completed'", root) == Decimal("110.00")
    assert tt.truth("SELECT i.status FROM (SELECT unnest(issues) AS i FROM "
                    "read_json('var/workflow/issues.json')) WHERE i.id = 'ANA-2'",
                    root) == "done"
    with pytest.raises(ValueError, match="one scalar"):
        tt.truth("SELECT order_status FROM warehouse.warehouse.fct_orders", root)


def test_read_destinations_merges_backend_files(tmp_path):
    assert tt.read_destinations(tmp_path) is None
    (tmp_path / "destinations.jsonl").write_text(
        '{"count":1,"host":"jira.internal","port":8080}\n')
    (tmp_path / "destinations.snowflake.jsonl").write_text(
        '{"count":3,"host":"snowflake.internal","port":8443}\n')
    got = {(r["host"], r["port"]): r["count"] for r in tt.read_destinations(tmp_path)}
    assert got == {("jira.internal", 8080): 1, ("snowflake.internal", 8443): 3}


# --------------------------------------------------------------------------- the world
@pytest.mark.parametrize("profile,plugs", sorted(
    {(p.profile, json.dumps({s: list(v) for s, v in p.plugs.items()}, sort_keys=True))
     for p in tt.load_probes()}))
def test_world_seed_is_the_box_its_profile_describes(tmp_path, profile, plugs):
    """The seed's `profile:` record, written the way world.build writes it, passes the
    harness's own `episode.check_world_profile` for the profile the stack serves."""
    plugs = json.loads(plugs)
    seed = yaml.safe_load(tt.world_seed(profile, plugs))
    assert seed["theme"] == "commerce_analytics" and seed["seed"] == 11
    assert seed["native"] is True
    record = {"name": seed["profile"]["name"], "slots": seed["profile"]["slots"]}
    if seed["profile"].get("plugs"):
        record["plugs"] = {s: sorted(v) for s, v in seed["profile"]["plugs"].items()}
    path = tmp_path / episode.WORLD_PROFILE_RECORD
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record))
    episode.check_world_profile(tmp_path, episode.environment_profile(profile, plugs))


def test_da_snowflake_seed_carries_its_stack_bi_vendor():
    seed = yaml.safe_load(tt.world_seed("da/snowflake", {}))
    assert seed["profile"]["plugs"] == {"bi": ["sigma"]}
    assert "plugs" not in yaml.safe_load(tt.world_seed("de/laptop", {}))["profile"]


def test_as_profile_restores_bi_e2e_check():
    import bi_e2e_check as bi

    before = (bi.seed_text, bi.PROFILE_NAME, bi.PLUGS)
    with tt.as_profile("de/laptop", {}, "seed"):
        assert bi.PROFILE_NAME == "de/laptop" and bi.PLUGS == {}
        assert bi.seed_text("ignored") == "seed"
    assert (bi.seed_text, bi.PROFILE_NAME, bi.PLUGS) == before


def test_image_for_follows_the_profile_unless_told():
    assert tt.image_for("da/laptop", {"bi": ["powerbi"]}, None) == "alkera-episode:head-da"
    assert tt.image_for("de/postgres", {}, None) == "alkera-episode:head"
    assert tt.image_for("da/snowflake", {}, None) == "alkera-episode:head-da-snowflake"
    assert tt.image_for("de/laptop", {}, "x:y") == "x:y"
    assert tt.image_for("de/laptop", {}, {"de/laptop": "a:b"}) == "a:b"


def test_run_refuses_an_unknown_topology():
    with pytest.raises(ValueError, match="container"):
        tt.run([], topology="host", image=None)


def test_cli_image_flags():
    assert tt._images([]) is None
    assert tt._images(["x:y"]) == "x:y"
    assert tt._images(["da/laptop=a:b", "de/laptop=c:d"]) == {"da/laptop": "a:b",
                                                              "de/laptop": "c:d"}


def test_probe_modules_shadow_nothing():
    """`tool_tour_probes/duckdb.py` (and snowflake.py) are submodules: loading them
    leaves the real packages of the same name importable."""
    import sys

    tt.load_probes(["duckdb", "snowflake"])
    import duckdb

    assert hasattr(duckdb, "connect")
    assert sys.modules["duckdb"] is not sys.modules["tool_tour_probes.duckdb"]
