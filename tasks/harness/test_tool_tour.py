"""tool_tour.py / tool_tour_core.py: probe loading and shapes, the verdict, truth, the
drive (teardown rows), the episode checks and the world seed - no docker.

The live tour is `tool_tour.py --topology container` (evidence/tool_tour_container.json).
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys
from decimal import Decimal

import bi_e2e_check as bi
import episode
import pytest
import tool_tour as tt
import yaml
from tool_tour import Probe

ELEVEN = {"duckdb", "dbt", "mf", "jupyter", "postgres", "snowflake", "powerbi", "tableau",
          "sigma", "human", "sched"}
SF = Probe(id="snowflake.connector.net_revenue_feb_2026", profile="da/snowflake", plugs={},
           command="true", expect_tools=("snowflake.statement",),
           truth_sql="SELECT 2361224.00", instruction="February 2026 Net Revenue.")
PG = Probe(id="postgres.psql.items_count", profile="de/postgres", plugs={},
           command="true", expect_tools=("psql.statement",), truth_sql=None,
           instruction="Count warehouse.items.", rows_at="teardown")
ALLOWED = {("snowflake.internal", 8443), ("jira.internal", 8080)}


@pytest.fixture
def audit(tmp_path, monkeypatch):
    """Canned 9.2.2 rows, written by the real native writer into tmp_path/actions.jsonl."""
    from alkera_gateway.native import audit as native_audit

    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(tmp_path / "actions.jsonl"))
    monkeypatch.setenv("ALKERA_VTS", "2026-03-02T00:00:00Z")

    def row(tool: str, **kw) -> dict:
        return native_audit.record(tool=tool, args=kw.pop("args", {"sql": "SELECT 1"}),
                                   op_class=kw.pop("op_class", "read"),
                                   targets=kw.pop("targets", ["warehouse.fct_orders"]), **kw)
    return row


def judge(probe=SF, *, stdout='{"value": "2361224.0"}\n', rows=(), destinations=None,
          truth=Decimal("2361224.00"), truth_error=None, returncode=0):
    return tt.verdict(probe, returncode=returncode, stdout=stdout, rows=list(rows),
                      destinations=destinations or {}, allowed=ALLOWED, truth=truth,
                      truth_error=truth_error, validate_row=bi.validate_row)


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


@pytest.fixture
def probe_package(tmp_path, monkeypatch):
    """A scratch probes package, so load_probes can be fed broken modules."""
    package = tmp_path / "scratch_probes"
    package.mkdir()
    (package / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(tt, "PROBES_DIR", package)
    monkeypatch.setattr(tt, "PROBES_PACKAGE", "scratch_probes")
    yield package
    for name in [m for m in sys.modules if m.startswith("scratch_probes")]:
        del sys.modules[name]


PROBE_SRC = ("from tool_tour import Probe\nPROBES = [{probes}]\n")
ONE = ('Probe(id="{id}", profile="de/laptop", plugs={plugs}, command="true", '
       'expect_tools={tools}, truth_sql=None, instruction="x")')


def test_load_probes_names_the_module_of_a_misshapen_probe(probe_package):
    (probe_package / "wrong.py").write_text(PROBE_SRC.format(probes=ONE.format(
        id="wrong.a.b", plugs="42", tools='("x.y",)')))
    with pytest.raises(ValueError, match=r"wrong\.py.*plugs must be a mapping"):
        tt.load_probes(["wrong"])
    (probe_package / "empty.py").write_text(PROBE_SRC.format(probes=ONE.format(
        id="empty.a.b", plugs="{}", tools="()")))
    with pytest.raises(ValueError, match=r"empty\.py.*expect_tools is empty"):
        tt.load_probes(["empty"])
    (probe_package / "twice.py").write_text(PROBE_SRC.format(probes=", ".join(
        [ONE.format(id="twice.a.b", plugs="{}", tools='("x.y",)')] * 2)))
    with pytest.raises(ValueError, match="duplicate probe ids"):
        tt.load_probes(["twice"])
    (probe_package / "stray.py").write_text(PROBE_SRC.format(probes=ONE.format(
        id="other.a.b", plugs="{}", tools='("x.y",)')))
    with pytest.raises(ValueError, match="filed under stray.py"):
        tt.load_probes(["stray"])


def test_probe_shapes_are_normalised_and_hashable():
    probe = dataclasses.replace(SF, plugs={"warehouse": "snowflake", "bi": ["sigma"]})
    assert probe.plugs == (("bi", ("sigma",)), ("warehouse", ("snowflake",)))
    assert probe.plug_map() == {"bi": ("sigma",), "warehouse": ("snowflake",)}
    assert dataclasses.replace(probe, id="snowflake.a.b").plugs == probe.plugs
    assert len({probe, dataclasses.replace(probe), SF}) == 2
    assert dataclasses.replace(SF, expect_tools=["a.b"]).expect_tools == (("a.b",),)
    assert dataclasses.replace(SF, expect_tools="a.b").expect_tools == (("a.b",),)
    grouped = dataclasses.replace(SF, expect_tools=[["a.get", "a.list"], "b.run"],
                                  expect_targets=["*.xlsx"])
    assert grouped.expect_tools == (("a.get", "a.list"), ("b.run",))
    assert grouped.expect_targets == ("*.xlsx",) and SF.expect_targets == ()
    assert dataclasses.replace(grouped) == grouped
    assert hash(dataclasses.replace(grouped)) == hash(grouped)
    for bad in ({"bi": 3}, {"bi": ()}, ["bi", "sigma"], "bi=sigma", {"bi": [""]}):
        with pytest.raises(TypeError):
            dataclasses.replace(SF, plugs=bad)
    with pytest.raises(ValueError, match="expect_tools is empty"):
        dataclasses.replace(SF, expect_tools=())
    with pytest.raises(ValueError, match="rows_at"):
        dataclasses.replace(SF, rows_at="later")
    with pytest.raises(ValueError, match="<tool>.<client>.<what>"):
        dataclasses.replace(SF, id="snowflake")


def test_every_probe_is_runnable_as_written():
    """Its profile resolves (no planned plug), its command is valid bash that starts in
    the workspace, and its brief does not hand over the command."""
    for probe in tt.load_probes():
        assert episode.environment_profile(probe.profile, probe.plug_map()) is not None
        assert probe.command.startswith("cd /workspace\n"), probe.id
        done = subprocess.run(["bash", "-n"], input=probe.command, text=True,
                              capture_output=True, check=False)
        assert done.returncode == 0, (probe.id, done.stderr)
        handed = [line.strip() for line in probe.command.splitlines()
                  if len(line.strip()) > 20 and line.strip() in probe.instruction]
        assert not handed, (probe.id, handed)
        assert len(probe.instruction) < 400, probe.id


def test_dbt_and_mf_probes_name_their_subcommand():
    by_id = {p.id: p for p in tt.load_probes(["dbt", "mf"])}
    assert by_id["dbt.show.net_revenue"].expect_args == ("show",)
    assert by_id["mf.query.net_revenue_feb_2026"].expect_args == ("query",)


def test_jupyter_probe_notebook_is_valid_and_its_cell_compiles():
    probe = next(p for p in tt.load_probes(["jupyter"])
                 if p.id == "jupyter.kernel.completed_orders_feb_2026")
    text = re.search(r"<<'NB'\n(.*?)\nNB\n", probe.command, re.DOTALL).group(1)
    notebook = json.loads(text)
    assert notebook["metadata"]["kernelspec"]["name"] == "alkera-python"
    compile(notebook["cells"][0]["source"], "cell", "exec")


def test_default_kernel_probe_runs_python3_with_no_kernel_flag():
    """What a live agent does (the 10-10 Claude run: NotebookClient(kernel_name="python3")):
    a notebook naming `python3`, run with no --kernel_name. Its cell must be audited too
    (Task 1's audited default kernel); on an image without it this probe FAILs."""
    probes = {p.id: p for p in tt.load_probes(["jupyter"])}
    probe = probes["jupyter.default_kernel.completed_orders_feb_2026"]
    text = re.search(r"<<'NB'\n(.*?)\nNB\n", probe.command, re.DOTALL).group(1)
    notebook = json.loads(text)
    assert notebook["metadata"]["kernelspec"]["name"] == "python3"
    compile(notebook["cells"][0]["source"], "cell", "exec")
    assert "jupyter execute " in probe.command and "--kernel_name" not in probe.command
    assert probe.expect_tools == (("jupyter.cell",),)
    audited = probes["jupyter.kernel.completed_orders_feb_2026"]
    assert probe.truth_sql == audited.truth_sql and probe.plugs == audited.plugs
    assert tt.group_key(probe) == tt.group_key(audited)


def test_route_probes_name_groups_and_targets():
    """The routes the 10-10 live run took are accepted: Unity Catalog through either the
    table lookup or the table listing (each naming fct_orders), and Excel by the .xlsx
    the python row wrote - a `python3 -` heredoc keeps no source in argv."""
    probes = {p.id: p for p in tt.load_probes(["databricks", "excel"])}
    uc = probes["databricks.uc.fct_orders_columns"]
    assert uc.expect_tools == (("databricks.table_get", "databricks.tables_list"),)
    assert uc.expect_targets == ("main.warehouse.fct_orders",)
    excel = probes["excel.openpyxl.net_revenue_feb_2026_roundtrip"]
    assert excel.expect_tools == (("shim.python",),)
    assert excel.expect_args == () and excel.expect_targets == ("*.xlsx",)
    # under /workspace, so the shim's workspace diff names the workbook it wrote
    assert "d=/workspace/tour_excel\n" in excel.command
    assert "/tmp/tour_excel" not in excel.command


def test_postgres_probes_are_teardown_probes_over_the_seeded_world():
    """Task 9 wired the tailer into down() and seeds a de/postgres box from the world,
    so the psql probe reads a world fact and carries no KNOWN finding any more."""
    from tool_tour_probes import NET_REVENUE_FEB_2026_SQL

    probes = tt.load_probes(["postgres", "postgres_dbt"])
    assert {p.id for p in probes} == {"postgres.psql.net_revenue_feb_2026",
                                      "postgres_dbt.run.net_revenue_feb_2026"}
    for probe in probes:
        assert probe.rows_at == "teardown" and probe.profile == "de/postgres"
        assert probe.truth_sql == NET_REVENUE_FEB_2026_SQL
        assert ("psql.statement",) in probe.expect_tools
    known = tt.known_findings()
    assert set(known) <= {p.id for p in tt.load_probes()}
    assert not {p.id for p in probes} & set(known)


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
    assert result["missing_tools"] == [["snowflake.statement"]]
    assert judge(rows=[])["verdict"] == "FAIL"


def test_verdict_checks_expect_args_in_an_expected_rows_argv(audit):
    probe = Probe(id="dbt.show.net_revenue", profile="da/laptop", plugs={},
                  command="true", expect_tools=("shim.dbt",), truth_sql=None,
                  instruction="x", expect_args=("show",))
    parse = audit("shim.dbt", op_class="exec", targets=[], args={"argv": ["dbt", "parse"]})
    show = audit("shim.dbt", op_class="exec", targets=[],
                 args={"argv": ["dbt", "--quiet", "show", "--inline", "select 1"]})
    elsewhere = audit("shim.python", op_class="exec", targets=[],
                      args={"argv": ["python3", "show"]})
    ok = judge(probe, rows=[parse, show], stdout='{"value": 1}\n', truth=None)
    assert ok["verdict"] == "PASS", ok["failures"]
    bad = judge(probe, rows=[parse, elsewhere], stdout='{"value": 1}\n', truth=None)
    assert bad["verdict"] == "FAIL" and bad["missing_args"] == ["show"]


def test_verdict_accepts_any_tool_of_a_group(audit):
    probe = dataclasses.replace(SF, expect_tools=(("a.get", "a.list"),))
    result = judge(probe, rows=[audit("a.list")])
    assert result["verdict"] == "PASS", result["failures"]
    assert result["expect_tools"] == [["a.get", "a.list"]] and result["missing_tools"] == []
    # groups are all-of among themselves: each needs a row of its own
    two = dataclasses.replace(SF, expect_tools=(("a.get", "a.list"), "b.run"))
    result = judge(two, rows=[audit("a.list")])
    assert result["verdict"] == "FAIL" and result["missing_tools"] == [["b.run"]]
    assert judge(two, rows=[audit("a.get"), audit("b.run")])["verdict"] == "PASS"


def test_alternative_tool_row_must_succeed(audit):
    probe = dataclasses.replace(SF, expect_tools=(("a.get", "a.list"),))
    failed = audit("a.list", exit="error", error="no such table")
    result = judge(probe, rows=[failed])
    assert result["verdict"] == "FAIL"
    assert any("no audit row for" in f for f in result["failures"]), result["failures"]
    assert result["missing_tools"] == [["a.get", "a.list"]]
    # one alternative that succeeded beside it is the proof
    assert judge(probe, rows=[failed, audit("a.get")])["verdict"] == "PASS"
    # a one-tool group is held to the same rule, and a failed row's argv and targets
    # prove nothing either
    single = dataclasses.replace(SF, expect_args=("show",),
                                 expect_targets=("warehouse.fct_orders",))
    errored = audit("snowflake.statement", exit="error", args={"argv": ["snow", "show"]})
    result = judge(single, rows=[errored])
    assert result["verdict"] == "FAIL" and result["missing_tools"] == [["snowflake.statement"]]
    assert result["missing_args"] == ["show"]
    assert result["missing_targets"] == ["warehouse.fct_orders"]


def test_expect_targets_match_a_target_of_an_expected_row(audit):
    probe = dataclasses.replace(SF, expect_tools=("shim.python",), expect_targets=("*.xlsx",))
    wrote = audit("shim.python", op_class="exec", targets=["output/close.xlsx"])
    result = judge(probe, rows=[wrote])
    assert result["verdict"] == "PASS", result["failures"]
    assert result["expect_targets"] == ["*.xlsx"] and result["missing_targets"] == []
    bare = judge(probe, rows=[audit("shim.python", op_class="exec", targets=[])])
    assert bare["verdict"] == "FAIL" and bare["missing_targets"] == ["*.xlsx"]
    assert any("*.xlsx" in f for f in bare["failures"]), bare["failures"]
    # the target must be on a row that satisfies a group, not on any row
    elsewhere = judge(probe, rows=[audit("shim.python", op_class="exec", targets=[]),
                                   audit("shim.git", op_class="exec",
                                         targets=["output/close.xlsx"])])
    assert elsewhere["verdict"] == "FAIL" and elsewhere["missing_targets"] == ["*.xlsx"]
    # globs are all-of, and each may be met by a different expected row
    both = dataclasses.replace(probe, expect_targets=("*.xlsx", "output/*.csv"))
    assert judge(both, rows=[wrote])["missing_targets"] == ["output/*.csv"]
    csv = audit("shim.python", op_class="exec", targets=["output/close.csv"])
    assert judge(both, rows=[wrote, csv])["verdict"] == "PASS"


def test_probe_refuses_an_empty_alternative_group():
    with pytest.raises(ValueError, match="empty"):
        dataclasses.replace(SF, expect_tools=(("a.get", "a.list"), ()))
    with pytest.raises(ValueError, match="empty"):
        dataclasses.replace(SF, expect_tools=[[]])
    for bad in ((("a.get", 3),), (("a.get", ""),), (3,)):
        with pytest.raises(TypeError):
            dataclasses.replace(SF, expect_tools=bad)
    for bad in (("",), (3,), 3):
        with pytest.raises(TypeError):
            dataclasses.replace(SF, expect_targets=bad)


#: Audit rows recorded by the 10-10 live Claude tour (harbor/evidence/
#: tooltour-claude-f6943a1.json), copied verbatim from each trial's
#: artifacts/audit/actions.jsonl (harbor/dist is gitignored): excel's shim.python
#: rows (seq 22, 39), databricks.uc's first warehouse table listing (seq 37), and
#: jupyter's shim.python rows (seq 28-35; the whole trial left no jupyter.cell row).
LIVE = tt.HERE / "testdata" / "tooltour_live_f6943a1"


def _live_rows(name: str) -> list[dict]:
    return [json.loads(line)
            for line in (LIVE / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]


def test_the_live_misses_judge_as_their_routes_deserve():
    """The three right answers the old probes failed: Excel and Unity Catalog took a
    route the probe now accepts; Jupyter's kernel wrote no audit row at all, which is a
    real miss and stays one (for both jupyter probes)."""
    probes = {p.id: p for p in tt.load_probes(["excel", "databricks", "jupyter"])}
    dbx = ("databricks.internal", 443)
    cases = [  # probe, rows, the trial's answer and sealed truth, its connections, verdict
        ("excel.openpyxl.net_revenue_feb_2026_roundtrip", "excel", 2361224.0,
         Decimal("2361224.00"), {}, "PASS"),
        ("databricks.uc.fct_orders_columns", "databricks_uc", 6, 6, {dbx: 8}, "PASS"),
        ("jupyter.kernel.completed_orders_feb_2026", "jupyter", 1068, 1068, {}, "FAIL"),
        ("jupyter.default_kernel.completed_orders_feb_2026", "jupyter", 1068, 1068, {},
         "FAIL"),
    ]
    for probe_id, name, answer, fact, destinations, want in cases:
        probe, rows = probes[probe_id], _live_rows(name)
        result = tt.verdict(probe, returncode=0, stdout=json.dumps({"value": answer}),
                            rows=rows, destinations=destinations, allowed={dbx},
                            truth=fact, validate_row=bi.validate_row)
        assert result["verdict"] == want, (probe_id, result["failures"])
        assert result["value_ok"] is True, probe_id
        if want == "FAIL":
            assert result["missing_tools"] == [["jupyter.cell"]], result
            assert result["failures"] == [f for f in result["failures"]
                                          if "no audit row for" in f]
    # and the probes as they were recorded (probe.json of the 10-10 export) failed
    # excel and databricks.uc on the same rows
    old = {"excel.openpyxl.net_revenue_feb_2026_roundtrip": (
               "excel", {"expect_tools": ("shim.python",), "expect_args": ("read_excel",),
                         "expect_targets": ()}, 2361224.0, Decimal("2361224.00"), {}),
           "databricks.uc.fct_orders_columns": (
               "databricks_uc", {"expect_tools": ("databricks.table_get",),
                                 "expect_targets": ()}, 6, 6, {dbx: 8})}
    for probe_id, (name, fields, answer, fact, destinations) in old.items():
        before = dataclasses.replace(probes[probe_id], **fields)
        result = tt.verdict(before, returncode=0, stdout=json.dumps({"value": answer}),
                            rows=_live_rows(name), destinations=destinations,
                            allowed={dbx}, truth=fact, validate_row=bi.validate_row)
        assert result["verdict"] == "FAIL", probe_id


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


def test_verdict_refuses_value_and_doc_together(audit):
    result = judge(rows=[audit("snowflake.statement")],
                   stdout='{"value": "2361224.00", "doc": "also refused"}\n')
    assert result["verdict"] == "FAIL"
    assert any("both `value` and `doc`" in f for f in result["failures"])


def test_verdict_doc_when_the_path_is_refused_by_name(audit):
    result = judge(rows=[audit("snowflake.statement")],
                   stdout='{"doc": "dbt-databricks refused by name"}\n')
    assert result["verdict"] == "DOC" and result["doc"] == "dbt-databricks refused by name"
    assert judge(rows=[], stdout='{"doc": "x"}\n')["verdict"] == "FAIL"


def test_verdict_without_truth_records_the_value(audit):
    probe = dataclasses.replace(PG, rows_at="call")
    result = judge(probe, rows=[audit("psql.statement")], stdout='{"value": 2}\n', truth=None)
    assert result["verdict"] == "PASS" and result["value"] == 2 and result["value_ok"] is None


def test_a_null_truth_fails_the_probe(audit, tmp_path):
    import duckdb

    (tmp_path / "var" / "warehouse").mkdir(parents=True)
    duckdb.connect(str(tmp_path / tt.WAREHOUSE_REL)).close()
    with pytest.raises(ValueError, match="NULL"):
        tt.truth("SELECT NULL", tmp_path)
    facts = tt.world_truths([SF, dataclasses.replace(SF, id="snowflake.a.null",
                                                     truth_sql="SELECT NULL")], tmp_path)
    assert facts[SF.id][0] == Decimal("2361224.00") and facts[SF.id][1] is None
    fact, error = facts["snowflake.a.null"]
    result = judge(rows=[audit("snowflake.statement")], truth=fact, truth_error=error)
    assert result["verdict"] == "FAIL" and "NULL" in " ".join(result["failures"])
    assert not tt.same_value(None, None)


def test_same_value_compares_numbers_as_numbers():
    assert tt.same_value("2361224.00", Decimal("2361224.00"))
    assert tt.same_value(2361224.0, Decimal("2361224.00"))
    assert tt.same_value(33733790, Decimal("33733790.00"))
    assert tt.same_value(" 5 ", 5)
    assert not tt.same_value("2361224.01", Decimal("2361224.00"))
    assert tt.same_value("todo", "todo") and not tt.same_value("done", "todo")
    assert not tt.same_value(None, Decimal("1")) and not tt.same_value(True, 1)


def test_normalise_host_agrees_with_agent_under_test():
    import agent_under_test as aut

    for host in ("localhost", "127.0.0.1", "::1", "jira.internal", "0.0.0.0"):
        assert tt.normalise_host(host) == aut.normalise_host(host)


def test_core_imports_nothing_of_the_harness():
    """tool_tour_core ships alone in Task 11's verifier: stdlib + duckdb only."""
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); import tool_tour_core; "
             "print(sorted({m.split('.')[0] for m in sys.modules}))")
    done = subprocess.run([sys.executable, "-c", probe, str(tt.HERE)], capture_output=True,
                          text=True, check=True)
    loaded = set(json.loads(done.stdout.replace("'", '"')))
    harness = {p.stem for p in tt.HERE.glob("*.py")} - {"tool_tour_core"}
    assert not loaded & (harness | {"alkera_gateway", "world", "yaml"}), loaded & harness


# --------------------------------------------------------------------------- timeouts
def test_a_probe_that_times_out_on_the_host_fails_alone(audit):
    class Topo:
        def run(self, command, timeout=900):
            raise subprocess.TimeoutExpired(["bash"], timeout, output=b'{"value": 1}\n')

    outcome = tt.run_probe(Topo(), SF)
    assert outcome.returncode == 124 and "timed out" in outcome.stderr
    result = judge(rows=[audit("snowflake.statement")], stdout=outcome.stdout,
                   returncode=outcome.returncode)
    assert result["verdict"] == "FAIL" and "exit 124" in result["failures"]


def test_tour_topology_runs_each_command_under_timeout_in_the_episode():
    topo = object.__new__(tt.TourTopology)
    seen = {}

    def exec_raw(argv, *, workdir=None, timeout=900):
        seen.update(argv=argv, workdir=workdir, timeout=timeout)
        return subprocess.CompletedProcess(argv, 0, "out", "err")

    topo.exec_raw = exec_raw
    outcome = topo.run("echo hi", timeout=60)
    assert seen["argv"] == ["timeout", "-k", "10", "60", "bash", "-c", "echo hi"]
    assert seen["workdir"] == "/workspace" and seen["timeout"] == 60 + tt.HOST_GRACE_S
    assert outcome.returncode == 0 and outcome.stdout == "out"


# --------------------------------------------------------------------------- truth, files
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


def test_read_destinations_sums_files_and_counts_a_folded_row_once(tmp_path):
    assert tt.read_destinations(tmp_path) is None
    jira = {"count": 1, "host": "jira.internal", "port": 8080}
    sf = {"count": 3, "host": "snowflake.internal", "port": 8443}
    (tmp_path / "destinations.jsonl").write_text(json.dumps(jira) + "\n")
    (tmp_path / "destinations.snowflake.jsonl").write_text(json.dumps(sf) + "\n")
    (tmp_path / "destinations.other.jsonl").write_text(
        json.dumps({**jira, "count": 2}) + "\n")

    def counts():
        return {(r["host"], r["port"]): r["count"] for r in tt.read_destinations(tmp_path)}

    assert counts() == {("jira.internal", 8080): 3, ("snowflake.internal", 8443): 3}
    # teardown folds the snowflake row into destinations.jsonl verbatim: still 3, not 6
    (tmp_path / "destinations.jsonl").write_text(json.dumps(jira) + "\n" + json.dumps(sf))
    assert counts() == {("jira.internal", 8080): 3, ("snowflake.internal", 8443): 3}


# --------------------------------------------------------------------------- drive
class FakeTopo:
    """A stack whose psql row lands either as the command runs or in down()."""

    def __init__(self, audit_dir, write, row_lands):
        self.audit_dir, self.write, self.row_lands = audit_dir, write, row_lands
        self.downs = 0

    def run(self, command, timeout=600):
        if self.row_lands == "run":
            self.write("psql.statement")
        return bi.Outcome(0, '{"value": 2}\n', "", 0.1)

    def down(self):
        self.downs += 1
        if self.row_lands == "down":
            self.write("psql.statement")


def _drive(tmp_path, audit, probe, row_lands):
    (tmp_path / "destinations.jsonl").write_text("")
    topo = FakeTopo(tmp_path, audit, row_lands)
    results: list[dict] = []
    episode_ = tt.drive(topo, [probe], world={"world_root": str(tmp_path),
                                              "scheduler": str(tmp_path / "none.json")},
                        facts={probe.id: (None, None)}, allowed=set(), known={},
                        results=results)
    assert topo.downs == 1
    return results[0], episode_


def test_teardown_rows_pass_only_a_teardown_probe(tmp_path, audit):
    result, episode_ = _drive(tmp_path, audit, PG, row_lands="down")
    assert result["verdict"] == "PASS", result["failures"]
    assert result["rows"] == 1 and episode_["rows_in_probes"] == 1
    assert episode_["no_stray_rows"] and episode_["ok"]


def test_teardown_rows_fail_a_call_probe(tmp_path, audit):
    result, episode_ = _drive(tmp_path, audit, dataclasses.replace(PG, rows_at="call"),
                              row_lands="down")
    assert result["verdict"] == "FAIL" and result["missing_tools"] == [["psql.statement"]]
    assert not episode_["no_stray_rows"] and not episode_["ok"]


def test_call_rows_pass_a_call_probe(tmp_path, audit):
    result, episode_ = _drive(tmp_path, audit, dataclasses.replace(PG, rows_at="call"),
                              row_lands="run")
    assert result["verdict"] == "PASS" and episode_["ok"]


def test_group_key_gives_a_teardown_probe_its_own_stack():
    a = dataclasses.replace(PG, id="postgres.a.b", rows_at="call")
    b = dataclasses.replace(PG, id="postgres.c.d", rows_at="call")
    assert tt.group_key(a) == tt.group_key(b) != tt.group_key(PG)
    assert tt.group_dir(tt.group_key(PG)) == "de-postgres-postgres-psql-items-count"
    assert tt.group_dir(tt.group_key(dataclasses.replace(
        SF, profile="da/laptop", plugs={"bi": ("powerbi", "sigma")}))) == \
        "da-laptop-bi-powerbi-sigma"


# --------------------------------------------------------------------------- episode
def test_episode_checks_catch_a_stray_row_and_a_seq_gap(tmp_path, audit):
    (tmp_path / "destinations.jsonl").write_text("")
    audit("snowflake.statement")
    audit("snowflake.statement")
    clean = tt.episode_checks(tmp_path, [{"rows": 2}], set())
    assert clean["ok"] and clean["trace_valid"] is True and clean["no_stray_rows"]
    stray = tt.episode_checks(tmp_path, [{"rows": 1}], set())
    assert not stray["ok"] and not stray["no_stray_rows"]
    log = tmp_path / "actions.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    rows[1]["seq"] = 3
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    gap = tt.episode_checks(tmp_path, [{"rows": 2}], set())
    assert gap["trace_valid"] is not True and not gap["ok"]


def test_episode_checks_catch_an_unallowed_destination(tmp_path):
    (tmp_path / "destinations.jsonl").write_text(
        '{"count":1,"host":"example.com","port":443}\n')
    checks = tt.episode_checks(tmp_path, [], {("jira.internal", 8080)})
    assert checks["network_violations"] and not checks["ok"]


def test_run_group_records_a_world_build_that_exits(tmp_path, monkeypatch):
    import container_episode as ce

    monkeypatch.setattr(ce, "assert_image_current", lambda image: {"image": image})

    def exits(work):
        raise SystemExit("world.build failed: no theme")

    monkeypatch.setattr(bi, "build_world", exits)
    group, results = tt.run_group([dataclasses.replace(PG, rows_at="call")],
                                  tmp_path / "g", "x:y", {})
    assert "SystemExit" in group["error"] and not group["ok"]
    assert results[0]["verdict"] == "FAIL" and "not run" in results[0]["failures"][0]


# --------------------------------------------------------------------------- the world
@pytest.mark.parametrize("profile,plugs", sorted(
    {(p.profile, json.dumps({s: list(v) for s, v in p.plugs}, sort_keys=True))
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
    tt.load_probes(["duckdb", "snowflake"])
    import duckdb

    assert hasattr(duckdb, "connect")
    assert sys.modules["duckdb"] is not sys.modules["tool_tour_probes.duckdb"]
