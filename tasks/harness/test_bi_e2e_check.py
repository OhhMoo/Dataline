"""bi_e2e_check.py: the pure parts, and one real host-topology run.

    cd gateway && uv run pytest ../tasks/harness/test_bi_e2e_check.py -m "not slow"
    cd gateway && uv run --with tabcmd --with tableauserverclient \\
        pytest ../tasks/harness/test_bi_e2e_check.py -m slow

The pure parts are the rules the accuracy table is computed with - the 9.2.2
row validator, the expected-row matcher, the destination delta, the world-change
attribution and Tableau's number formats - each tested against a case that
must pass and one that must not, so a check that silently accepts everything
fails here first. The slow test builds a commerce_analytics world, brings the
host-topology native stack up and drives every tool (`bi_e2e_check.main`); it
needs tabcmd and tableauserverclient, which the gateway venv does not carry.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bi_e2e_check as check  # noqa: E402


# --------------------------------------------------------------------------
# the seed
# --------------------------------------------------------------------------
def test_the_seed_is_the_example_plus_the_da_laptop_profile_with_three_vendors():
    seed = yaml.safe_load(check.seed_text(check.SEED_EXAMPLE.read_text("utf-8")))
    example = yaml.safe_load(check.SEED_EXAMPLE.read_text("utf-8"))
    assert {k: v for k, v in seed.items()
            if k not in ("native", "files", "profile")} == example
    assert seed["native"] is True
    assert seed["files"] == {"formats": ["parquet"]}
    assert seed["profile"] == {
        "name": "da/laptop",
        "slots": ["files", "notebook", "warehouse", "transform", "bi", "human"],
        "plugs": {"bi": ["powerbi", "tableau", "sigma"]}}


def test_the_profile_serves_the_three_vendors_and_excludes_the_rest():
    from alkera_gateway.plugs import resolve

    profile = resolve(check.PROFILE_NAME, check.PLUGS)
    assert set(check.PLUGGED_HTTP) == set(profile.services)
    assert not set(check.EXCLUDED_HTTP) & set(profile.services)
    assert "orchestrator" not in profile.slots


# --------------------------------------------------------------------------
# (b) the 9.2.2 row validator
# --------------------------------------------------------------------------
@pytest.fixture
def real_row(tmp_path, monkeypatch) -> dict:
    """A row written by the one native writer, read back off disk."""
    from alkera_gateway.native import audit

    log = tmp_path / "actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(log))
    monkeypatch.setenv("ALKERA_VTS", "2026-03-02T00:00:00Z")
    audit.record(tool="powerbi.datasets_list", args={"groupId": "g"}, op_class="read",
                 targets=["powerbi:semantic_model:Commerce"], result={"n": 1})
    return json.loads(log.read_text("utf-8").splitlines()[0])


def test_a_row_the_native_writer_wrote_validates(real_row):
    assert check.validate_row(real_row, vnow="2026-03-02T00:00:00Z") == []
    assert check.validate_row(real_row, vnow=("2026-03-01T00:00:00Z",
                                              "2026-03-02T00:00:00Z")) == []


@pytest.mark.parametrize("tamper, needle", [
    (lambda r: r["args"].update(groupId="other"), "args_hash"),
    (lambda r: r.update(result={"n": 2}), "result_hash"),
    (lambda r: r.update(extra=1), "keys"),
    (lambda r: r.update(exit="error"), "exit error with error None"),
    (lambda r: r.update(op_class="mutate"), "op_class"),
    (lambda r: r.update(target="something-else"), "targets[0]"),
    (lambda r: r.update(transport="mcp"), "transport"),
    (lambda r: r.update(tool="no-dot"), "<service>.<operation>"),
    (lambda r: r.update(seq="1"), "seq"),
    (lambda r: r.update(vts="2026-03-05T09:00:00Z"), "vnow"),
])
def test_a_tampered_row_does_not(real_row, tamper, needle):
    tamper(real_row)
    problems = check.validate_row(real_row, vnow="2026-03-02T00:00:00Z")
    assert problems and any(needle in p for p in problems), problems


# --------------------------------------------------------------------------
# (a)/(c) expected rows
# --------------------------------------------------------------------------
def _row(tool, op_class="read", targets=(), read_targets=(), exit="ok"):
    return {"tool": tool, "op_class": op_class, "targets": list(targets),
            "read_targets": list(read_targets), "exit": exit, "error": None}


def test_expected_rows_are_matched_one_for_one_in_order():
    expect = [check.Expect("jira.issues_get", "read", check.exactly("issue:ANA-1"),
                           check.exactly("issue:ANA-1")),
              check.Expect("shim.python", "exec", check.exactly())]
    rows = [_row("jira.issues_get", "read", ["issue:ANA-1"], ["issue:ANA-1"]),
            _row("shim.python", "exec")]
    assert check.match_rows(expect, rows, {}) == []
    # one row too many, one too few, the wrong order: all refused
    assert check.match_rows(expect, rows + [_row("shim.python", "exec")], {})
    assert check.match_rows(expect, rows[:1], {})
    assert check.match_rows(expect, rows[::-1], {})


def test_a_wrong_classification_is_named():
    expect = [check.Expect("powerbi.datasets_list", "read", check.exactly("pbi:sm"),
                           check.exactly("pbi:sm"))]
    problems = check.match_rows(
        expect, [_row("powerbi.datasets_list", "read", ["pbi:sm"], [])], {})
    assert problems == ["powerbi.datasets_list: read_targets []"]
    problems = check.match_rows(
        expect, [_row("powerbi.datasets_list", "write", ["pbi:sm"], ["pbi:sm"])], {})
    assert problems == ["powerbi.datasets_list: op_class write != read"]
    problems = check.match_rows(
        [check.Expect("tableau.datasources_refresh", "write", exit="error")],
        [_row("tableau.datasources_refresh", "write")], {})
    assert problems and "exit ok != error" in problems[0]


def test_relations_tolerate_the_files_own_catalog_and_nothing_else():
    same = check.relation("warehouse.fct_orders")
    assert same(["warehouse.fct_orders"], {})
    assert same(["warehouse.warehouse.fct_orders"], {})
    assert not same(["other.warehouse.fct_orders"], {})
    assert not same(["warehouse.fct_orders", "warehouse.dim_date"], {})
    assert check.strip_catalog("warehouse.warehouse.fct_orders") == "warehouse.fct_orders"
    assert check.strip_catalog("main.e2e_scratch") == "main.e2e_scratch"


# --------------------------------------------------------------------------
# (d) destinations
# --------------------------------------------------------------------------
def test_the_destination_delta_is_per_host_and_port():
    before = [{"host": "127.0.0.1", "port": 38080, "count": 2},
              {"host": "127.0.0.1", "port": 38095, "count": 0}]
    after = [{"host": "127.0.0.1", "port": 38080, "count": 2},
             {"host": "127.0.0.1", "port": 38095, "count": 3}]
    assert check.destination_delta(before, after) == {("127.0.0.1", 38095): 3}
    assert check.destination_delta(None, after) == {("127.0.0.1", 38080): 2,
                                                    ("127.0.0.1", 38095): 3}
    assert check.destination_delta(after, after) == {}


# --------------------------------------------------------------------------
# (f) world changes
# --------------------------------------------------------------------------
@pytest.fixture
def layout(tmp_path) -> dict:
    root = tmp_path / "world"
    (root / "var" / "warehouse").mkdir(parents=True)
    (root / "var" / "warehouse" / "warehouse.duckdb").write_bytes(b"x")
    return check.manifest(root, tmp_path / "grading")


WAREHOUSE = "var/warehouse/warehouse.duckdb"


def test_a_warehouse_write_is_explained_by_the_row_that_opened_the_file(layout):
    before, after = {WAREHOUSE: [1, "a"]}, {WAREHOUSE: [1, "b"]}
    row = {**_row("duckdb.statement", "write", ["main.t"]),
           "args": {"database": layout["warehouse"]}}
    assert check.explain_world(before, after, [row], layout, None)["ok"]
    # a READ of the same file explains nothing
    read = {**_row("duckdb.statement", "read", ["main.t"]),
            "args": {"database": layout["warehouse"]}}
    judged = check.explain_world(before, after, [read], layout, None)
    assert not judged["ok"] and judged["unexplained_outside_workspace"] == [WAREHOUSE]
    # ...and a python shim row that named the file does
    shim = _row("shim.python", "exec", [layout["warehouse"]])
    shim["result"] = {"watched": [layout["warehouse"]]}
    assert check.explain_world(before, after, [shim], layout, None)["ok"]


def test_a_row_that_names_the_file_explains_it_even_where_blindness_does_not(layout):
    """A notebook cell row names the warehouse it rewrote (the shim's watch), and
    `blindness._unattributed` - whose WAREHOUSE_SHIMS lists duckdb/dbt/python
    only - does not accept `jupyter.cell` as the explanation. The check's rule
    is the row; the harness's second opinion is recorded beside it."""
    before, after = {WAREHOUSE: [1, "a"]}, {WAREHOUSE: [1, "b"]}
    cell = _row("jupyter.cell", "exec", [layout["warehouse"]])
    judged = check.explain_world(before, after, [cell], layout, None)
    assert judged["ok"]
    assert judged["named_by_a_row_but_unattributed_by_blindness"] == [WAREHOUSE]
    # ...and spelled the container's way, through the topology's path map
    container = _row("jupyter.cell", "exec", ["/world/" + WAREHOUSE])
    paths = {"/world/": "", "/workspace/": "workspace/"}
    assert check.explain_world(before, after, [container], layout, paths)["ok"]


def test_working_tree_changes_must_be_named_by_a_shim_that_ran(layout):
    before = {"workspace/a.txt": [1, "a"], "workspace/dbt_project/target/m.json": [1, "a"]}
    after = {"workspace/a.txt": [1, "b"], "workspace/dbt_project/target/m.json": [1, "b"],
             "workspace/b.txt": [1, "c"]}
    shim = _row("shim.python", "exec", ["a.txt"])
    judged = check.explain_world(before, after, [shim], layout, None)
    assert judged["unnamed_in_workspace"] == ["workspace/b.txt"]
    assert judged["skip_dir_churn"] == ["workspace/dbt_project/target/m.json"]
    assert not judged["ok"]
    # an unshimmed binary (only service rows, or none): the transcript's plane
    judged = check.explain_world(before, after, [_row("tableau.auth_signin")], layout, None)
    assert judged["ok"] and judged["transcript_audited"] == ["workspace/a.txt",
                                                             "workspace/b.txt"]


# --------------------------------------------------------------------------
# (e) values
# --------------------------------------------------------------------------
def test_tableau_number_formats_round_half_away_from_zero():
    assert check.money(Decimal("33733790.00")) == "$33,733,790.00"
    assert check.money(Decimal("2221.815")) == "$2,221.82"
    assert check.money(Decimal("-0.005")) == "-$0.01"
    assert check.pct(Decimal("0.79435")) == "79.4%"
    assert check.pct(Decimal("0.02225")) == "2.2%"
    assert check.pct(Decimal("0.02250")) == "2.3%"
    assert check.integer(15183) == "15,183"
    assert check.same_number("2221.813212145162", Decimal(33733790) / 15183)
    assert not check.same_number("2221.81", Decimal(33733790) / 15183)
    assert not check.same_number(None, 1)


def test_a_client_steps_result_line_is_found_after_any_noise():
    out = "warning\nE2E-RESULT {\"a\": 1}\ntrailing log\n"
    assert check.parse_result(out) == {"a": 1}
    assert check.parse_result("nothing") is None


# --------------------------------------------------------------------------
# the engineer's scripts
# --------------------------------------------------------------------------
def test_the_client_script_compiles_and_names_every_step_the_drive_runs():
    compile(check.CLIENTS_PY, "clients.py", "exec")
    namespace: dict = {"__name__": "clients"}
    exec(compile(check.CLIENTS_PY, "clients.py", "exec"), namespace)  # noqa: S102
    for step in ("py_select", "py_insert", "jira_get", "jira_comment", "slack_history",
                 "slack_post", "conf_get", "conf_edit", "conf_by_world_id", "pbi_step",
                 "tsc_view", "sigma_step", "jupyter_server_cell"):
        assert callable(namespace[step]), step


def test_the_notebook_is_three_cells_on_the_audited_kernel(tmp_path):
    import nbformat

    cells = check.notebook_cells("/workspace", "/world/var/warehouse/warehouse.duckdb",
                                 "/workspace/data/out.json")
    path = check.write_notebook(tmp_path / "e2e.ipynb", cells)
    nb = nbformat.read(str(path), as_version=4)
    nbformat.validate(nb)
    assert nb.metadata["kernelspec"]["name"] == "alkera-python"
    assert [c.source for c in nb.cells] == cells
    for source in cells:
        compile(source, "cell", "exec")


# --------------------------------------------------------------------------
# verdicts and the table
# --------------------------------------------------------------------------
class _Topo:
    name = "host"

    def destination(self, service):
        return ("127.0.0.1", {"jira": 38080}[service])


def _judge(action, rows, dest, world_ok=True, rc=0, transcript=()):
    ctx = {"vnow_window": ("2026-03-02T00:00:00Z",),
           "world_judgement": {"ok": world_ok, "transcript_audited": list(transcript)}}
    outcome = check.Outcome(rc, "", "", 0.1)
    return check.judge(action, outcome, rows, dest, {}, ctx, _Topo())


def test_verdicts(real_row):
    good = dict(real_row, vts="2026-03-02T00:00:00Z")
    action = check.Action(id="X1", family="jira", label="x", command="true",
                          expect=[check.Expect("powerbi.datasets_list", "read",
                                               check.exactly("powerbi:semantic_model:Commerce"))],
                          services=("jira",))
    dest = {("127.0.0.1", 38080): 1}
    assert _judge(action, [good], dest)["verdict"] == "PASS"
    assert _judge(action, [good], {})["verdict"] == "FAIL"              # no connection
    assert _judge(action, [], dest)["verdict"] == "FAIL"                # no row
    assert _judge(action, [good], dest, world_ok=False)["verdict"] == "FAIL"
    assert _judge(action, [good], dest, rc=1)["verdict"] == "FAIL"
    documented = check.Action(**{**action.__dict__, "doc": "coarser by design"})
    assert _judge(documented, [good], dest)["verdict"] == "DOC"
    assert _judge(action, [good], dest, transcript=["workspace/x.log"])["verdict"] == \
        "FINDING"
    wrong = check.Action(**{**action.__dict__,
                            "value": lambda o, ctx: (False, "mismatch")})
    assert _judge(wrong, [good], dest)["verdict"] == "FAIL"


def test_the_table_has_one_line_per_action_and_every_dimension():
    result = {"id": "W1", "family": "warehouse", "label": "duckdb", "rows_expected": 1,
              "rows_got": 1, "rows": [{"op_class": "read"}], "schema_ok": True,
              "classification_ok": True, "destinations_ok": True, "value_ok": None,
              "world": {"ok": True}, "verdict": "PASS"}
    table = check.render_table([result]).splitlines()
    assert table[0].split() == ["id", "tool", "/", "action", "rows", "op_class", "schema",
                                "class", "dest", "value", "world", "verdict"]
    assert table[2].split()[-7:] == ["read", "ok", "ok", "ok", "n/a", "ok", "PASS"]


# --------------------------------------------------------------------------
# the real thing, on the host topology
# --------------------------------------------------------------------------
CLIENTS = all(importlib.util.find_spec(name) is not None
              for name in ("tabcmd", "tableauserverclient", "jira", "slack_sdk",
                           "atlassian", "nbclient"))


@pytest.mark.slow
@pytest.mark.skipif(not CLIENTS, reason=(
    "needs tabcmd and tableauserverclient: cd gateway && uv run --with tabcmd "
    "--with tableauserverclient pytest ../tasks/harness/test_bi_e2e_check.py -m slow"))
def test_every_tool_is_accurate_on_the_host_topology(tmp_path):
    out = tmp_path / "bi_e2e_host.json"
    code = check.main(["--topology", "host", "--out", str(out),
                       "--work", str(check.WORK_ROOT / "pytest-host")])
    evidence = json.loads(out.read_text("utf-8"))
    failed = {r["id"]: r["failures"] for r in evidence["actions"] if r["verdict"] == "FAIL"}
    assert code == 0 and not failed, failed
    assert evidence["episode"]["ok"], evidence["episode"]
    by_id = {r["id"]: r for r in evidence["actions"]}
    # every action left exactly the rows it should, and every row validated
    assert all(r["rows_got"] == r["rows_expected"] for r in evidence["actions"])
    assert all(r["schema_ok"] for r in evidence["actions"])
    # the fixes this check found, held
    assert by_id["W5"]["tools"] == ["shim.python"]                         # python3
    assert by_id["D3"]["rows"][0]["targets"] == [evidence["world"]["warehouse"]]
    assert by_id["U1"]["rows_got"] == 6                                     # unrouted
    assert by_id["T5"]["rows"][-1]["read_targets"] == ["warehouse.fct_orders"]
    assert all(row["read_targets"] == row["targets"] for row in by_id["P2"]["rows"]
               if row["tool"].startswith("powerbi."))
