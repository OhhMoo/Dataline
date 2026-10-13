"""The tool tour as a Harbor dataset (plan Task 11), no Docker.

    TMPDIR=<disk> gateway/.venv/bin/python -m pytest harbor/tests/test_tooltour_export.py -q

One real export (a module fixture): the duckdb and dbt probes (da/laptop) and
the bigquery probes (de/gcp, a warehouse the Harbor image refuses to serve),
from real world builds, with the real templates. The verifier cases run the
exported duckdb task's own tests/verify_tool.py in a subprocess (a clean
interpreter: -I, no repository on the path), over R/{world,workspace,audit}
laid out as Harbor re-materialises the artifacts, with the audit rows written
here. The positive control (`test_verifier_rewards_one_...`) keeps every
zero-reward case from passing vacuously.

`ALKERA_HARBOR_PYTHON` (a Harbor install) additionally has Harbor load every
exported task and recompute its digest.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import tomllib
from decimal import Decimal
from pathlib import Path

import pytest

import test_export_support as support
from alkera_harbor import digest, tooltour
from alkera_harbor.constants import CANARY_MD
from alkera_harbor.validate import validate_tasks

TOOLS = ("dbt", "duckdb", "bigquery")
EXPORTED = {"dbt.show.net_revenue", "dbt.show.ref_inline",
            "duckdb.cli.net_revenue_feb_2026"}
DUCKDB = "duckdb.cli.net_revenue_feb_2026"
#: February 2026 Net Revenue on the commerce_analytics world, seed 11
#: (tasks/harness/tool_tour_probes/__init__.py): an anchor, so a truth that
#: drifted would be seen here and not only agree with itself.
FEB_2026_NET_REVENUE = Decimal("2361224.00")

sys.path.insert(0, str(support.REPO / "tasks" / "harness"))
import tool_tour_core as core  # noqa: E402


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    root = tmp_path_factory.mktemp("tooltour")
    out = root / "dist"
    log: list[str] = []
    adapter = tooltour.ToolTourAdapter(out, tools=list(TOOLS), overwrite=True,
                                       work_dir=root / "work", log=log.append)
    report = adapter.run()
    assert not report.errors and not report.dataset_problems, "\n".join(log)
    return adapter, report, out


def _task(out: Path, probe_id: str) -> Path:
    return out / f"{probe_id}__{tooltour.TOUR_ARM}"


# ------------------------------------------------------------------ export
def test_one_task_per_probe(exported):
    _adapter, report, out = exported
    probes = {p.id: p for p in tooltour.load_probes(list(TOOLS))}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    tasks = {t["probe"]: t for t in manifest["tasks"]}
    skipped = {s["probe"]: s["reason"] for s in manifest["skipped"]}
    # every probe is exactly one of: one task, or skipped with a named reason
    assert set(tasks) == EXPORTED
    assert set(tasks) | set(skipped) == set(probes)
    assert not set(tasks) & set(skipped)
    for probe_id, reason in skipped.items():
        if probe_id.startswith("bigquery."):
            assert "warehouse=bigquery" in reason and "around the audited proxy" in reason, \
                reason
    assert sorted(r.status for r in report.results) == ["ok"] * 3 + ["skipped"] * 3

    dataset = tomllib.loads((out / "dataset.toml").read_text(encoding="utf-8"))
    assert dataset["dataset"]["name"] == tooltour.DATASET_NAME
    assert sorted(t["name"] for t in dataset["tasks"]) == sorted(
        f"alkera/{pid}__tour" for pid in EXPORTED)
    for probe_id in EXPORTED:
        probe = probes[probe_id]
        task = _task(out, probe_id)
        assert digest.task_digest(task) == next(
            t["digest"] for t in dataset["tasks"] if t["name"] == f"alkera/{task.name}")
        toml = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
        assert toml["task"]["name"] == f"alkera/{probe_id}__tour"
        assert toml["metadata"]["alkera_tooltour_probe"] == probe_id
        assert toml["metadata"]["alkera_tool"] == probe.tool
        assert toml["metadata"]["alkera_profile"] == probe.profile
        # the brief: the probe's instruction and where the answer goes; never
        # the command
        instruction = (task / "instruction.md").read_text(encoding="utf-8")
        assert instruction == (f"{CANARY_MD}\n{probe.instruction}\n\n"
                               f"{tooltour.ANSWER_LINE}\n")
        assert probe.command.strip().splitlines()[-1] not in instruction
        # the oracle: the probe's command, verbatim, and its final JSON line
        # written to the answer file
        assert (task / "solution/probe.sh").read_text(encoding="utf-8") == probe.command
        solve = (task / "solution/solve.sh").read_text(encoding="utf-8")
        assert "probe.sh" in solve and tooltour.ANSWER_PATH in solve
        assert (task / "solution/solve.sh").stat().st_mode & 0o111
        # the verifier: the probe, the shipped core, the sealed world
        shipped = json.loads((task / "tests/episode/probe.json").read_text(encoding="utf-8"))
        assert shipped["probe"]["id"] == probe_id
        assert shipped["schema"] == tooltour.PROBE_SCHEMA
        assert shipped["probe"]["expect_tools"] == [list(g) for g in probe.expect_tools]
        assert shipped["probe"]["expect_targets"] == list(probe.expect_targets)
        assert shipped["probe"]["truth_sql"] == probe.truth_sql
        assert (task / "tests/alkera/repo/tasks/harness/tool_tour_core.py").read_bytes() \
            == (support.REPO / "tasks/harness/tool_tour_core.py").read_bytes()
        assert (task / "tests/episode/sealed/world/var/warehouse/warehouse.duckdb").is_file()
        assert (task / "tests/verify_tool.py").is_file()
        assert (task / "tests/verify.py").read_bytes() == \
            (support.REPO / "harbor/template/tests/verify.py").read_bytes()
        assert (task / "tests/Dockerfile").read_bytes() == \
            (support.REPO / "harbor/template/tests/Dockerfile").read_bytes()
        # the box: the probe's own profile, through the variant machinery, the
        # same Dockerfile BI001/BI002 (da/laptop + the three vendors) build from
        episode = json.loads((task / "environment/episode/episode.json").read_text())
        assert episode["profile"] == probe.profile
        assert episode["task"] == probe_id and episode["variant"] == tooltour.TOUR_ARM
        assert episode["world"]["warehouse"] == "/world/var/warehouse/warehouse.duckdb"
    dockerfiles = {(_task(out, pid) / "environment/Dockerfile").read_bytes()
                   for pid in EXPORTED}
    assert len(dockerfiles) == 1


def test_a_known_finding_is_skipped_by_name():
    """A probe in its module's KNOWN (a FAIL owned elsewhere) is not exported: its
    oracle could not pass. No probe is KNOWN today (dbt.show.ref_inline was, until
    the commerce_analytics macro fix), so the rule is exercised on a stand-in."""
    probe = next(p for p in tooltour.load_probes(["dbt"]) if p.id == "dbt.show.ref_inline")
    assert tooltour.known_findings(["dbt"]) == {}
    assert tooltour.skip_reason(probe, {}) is None
    reason = tooltour.skip_reason(probe, {probe.id: "a cause owned elsewhere"})
    assert reason.startswith("known finding") and reason.endswith("a cause owned elsewhere")

def test_harbor_loads_every_task(exported):
    _adapter, report, out = exported
    harbor_python = support.harbor_python()
    dirs = [_task(out, pid) for pid in sorted(EXPORTED)]
    verdicts = validate_tasks(dirs, harbor_python=harbor_python, instruction_sha256={
        r.name.split("/", 1)[1]: r.instruction_sha256 for r in report.results
        if r.status == "ok"})
    for task in dirs:
        verdict = verdicts[task.name]
        assert verdict["ok"] and not verdict["problems"], verdict
        assert verdict["digest"] == digest.task_digest(task)


def test_truth_never_in_environment(exported, tmp_path):
    _adapter, _report, out = exported
    for probe in tooltour.load_probes(list(TOOLS)):
        task = _task(out, probe.id)
        if not task.is_dir():
            continue
        sealed = task / "tests" / tooltour.SEALED_REL
        fact = core.truth(probe.truth_sql, sealed)
        assert tooltour.leak_problems(task, fact) == []
        env = task / "environment"
        names = {p.name for p in env.rglob("*")}
        assert not names & {"probe.json", "verify_tool.py", "tool_tour_core.py", "sealed"}
        assert not (env / "episode" / "sealed").exists()
        # truth that WAS distinctive is looked for in every environment file
        if probe.id == DUCKDB:
            assert Decimal(str(fact)) == FEB_2026_NET_REVENUE
            assert tooltour.truth_needles(fact)

    # the gate itself: a synthetic task whose environment carries each leak
    fact = FEB_2026_NET_REVENUE
    task = tmp_path / "leaky"
    (task / "environment/episode/workspace/docs").mkdir(parents=True)
    (task / "tests/episode").mkdir(parents=True)
    (task / "instruction.md").write_text(f"{CANARY_MD}\nfind it\n", encoding="utf-8")
    (task / "environment/Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    probe_json = task / "tests/episode/probe.json"
    probe_json.write_text('{"probe": {"id": "x.y.z"}}\n', encoding="utf-8")
    assert tooltour.leak_problems(task, fact) == []
    (task / "environment/episode/workspace/docs/close.md").write_text(
        "Feb close: 2,361,224.00\n", encoding="utf-8")
    assert any("close.md" in p for p in tooltour.leak_problems(task, fact))
    (task / "environment/episode/workspace/docs/close.md").unlink()
    shutil.copyfile(probe_json, task / "environment/episode/answers.json")
    assert any("answers.json" in p for p in tooltour.leak_problems(task, fact))
    (task / "environment/episode/answers.json").unlink()
    (task / "environment/episode/sealed/world").mkdir(parents=True)
    assert any("sealed" in p for p in tooltour.leak_problems(task, fact))
    shutil.rmtree(task / "environment/episode/sealed")
    (task / "instruction.md").write_text(f"{CANARY_MD}\nit is 2361224.00\n",
                                         encoding="utf-8")
    assert any("instruction.md" in p for p in tooltour.leak_problems(task, fact))


# ------------------------------------------------------------------ verifier
START_ROW = {"seq": 1, "tool": "episode.start", "op_class": "read", "targets": [],
             "actor": "harbor", "args": {}, "result": {}}
SERVED = {"jira.internal:8080": 1, "duckdb.internal:8094": 0}


def _row(seq: int, tool: str, *argv: str, exit: str = "ok",
         targets: tuple[str, ...] = ()) -> dict:
    return {"seq": seq, "tool": tool, "op_class": "read", "targets": list(targets),
            "exit": exit, "actor": "engineer", "args": {"argv": list(argv)}, "result": {}}


def _duckdb_row(seq: int = 2, **kw) -> dict:
    return _row(seq, "duckdb.statement", "duckdb", "-readonly",
                "/world/var/warehouse/warehouse.duckdb", "-c", "SELECT ...", **kw)


def _artifacts(out: Path, tmp_path: Path, *, rows: list[dict], answer,
               watermark: bool = True, destinations: dict | None = None,
               extra_destinations: dict | None = None) -> tuple[Path, Path]:
    """R/{world,workspace,audit} as Harbor hands them to the verifier, and T."""
    task = _task(out, DUCKDB)
    root = tmp_path / "R"
    shutil.copytree(task / "environment/episode/world", root / "world", symlinks=True)
    (root / "world" / "workspace").symlink_to("../workspace")
    shutil.copytree(task / "environment/episode/workspace", root / "workspace", symlinks=True)
    audit = root / "audit"
    audit.mkdir()
    (audit / "actions.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in [START_ROW, *rows]), encoding="utf-8")
    if watermark:
        (audit / ".episode_start.json").write_text(json.dumps({
            "schema": "alkera-harbor-watermark/1", "actions_lines": 1, "start_seq": 1,
            "destinations_baseline": SERVED}), encoding="utf-8")
    counts = destinations if destinations is not None else SERVED
    (audit / "destinations.jsonl").write_text("".join(
        json.dumps({"host": k.rsplit(":", 1)[0], "port": int(k.rsplit(":", 1)[1]),
                    "count": n}) + "\n" for k, n in counts.items()), encoding="utf-8")
    for name, rows_ in (extra_destinations or {}).items():
        (audit / name).write_text("".join(json.dumps(r) + "\n" for r in rows_),
                                  encoding="utf-8")
    if answer is not None:
        (root / "workspace" / "answer.json").write_text(
            answer if isinstance(answer, str) else json.dumps(answer), encoding="utf-8")
    return root, task / "tests"


def _grade(root: Path, tests: Path, tmp_path: Path) -> tuple[float | None, dict]:
    logs = tmp_path / "logs"
    work = tmp_path / "verify-work"
    work.mkdir(parents=True)
    done = subprocess.run(
        [sys.executable, "-I", str(tests / "verify_tool.py"), "--root", str(root),
         "--tests-dir", str(tests), "--logs-dir", str(logs), "--work-dir", str(work)],
        capture_output=True, text=True, timeout=600)
    grading = json.loads((logs / "grading.json").read_text(encoding="utf-8"))
    ctrf = json.loads((logs / "ctrf.json").read_text(encoding="utf-8"))
    assert ctrf["results"]["summary"]["tests"] >= 4, ctrf
    reward_file = logs / "reward.json"
    if not reward_file.exists():
        assert done.returncode == 1, done.stdout + done.stderr
        return None, grading
    assert done.returncode == 0, done.stdout + done.stderr
    reward = json.loads(reward_file.read_text(encoding="utf-8"))
    assert list(reward) == ["reward"]
    return reward["reward"], grading


@pytest.fixture(scope="module")
def sealed_truth(exported):
    _a, _r, out = exported
    fact = core.truth(next(p for p in tooltour.load_probes(["duckdb"])).truth_sql,
                      _task(out, DUCKDB) / "tests" / tooltour.SEALED_REL)
    assert Decimal(str(fact)) == FEB_2026_NET_REVENUE
    return fact


def test_verifier_rewards_one_with_the_value_and_the_row(exported, sealed_truth,
                                                          tmp_path):
    root, tests = _artifacts(exported[2], tmp_path, rows=[_duckdb_row()],
                             answer={"value": str(sealed_truth)})
    reward, grading = _grade(root, tests, tmp_path)
    assert reward == 1, grading
    assert all(c["passed"] for c in grading["checks"].values())
    assert grading["audit"]["expected_used"] is True
    assert grading["truth"]["source"] == "episode/sealed/world"


def test_verifier_rewards_zero_without_tool_rows(exported, sealed_truth, tmp_path):
    # the right number, but nothing on the record says the tool produced it
    root, tests = _artifacts(exported[2], tmp_path, rows=[],
                             answer={"value": float(sealed_truth)})
    reward, grading = _grade(root, tests, tmp_path)
    assert reward == 0, grading
    assert grading["checks"]["answer"]["passed"] is True
    assert grading["checks"]["tools"]["passed"] is False
    assert grading["checks"]["tools"]["detail"]["missing_tools"] == [["duckdb.statement"]]
    # a row of another tool does not stand in for it
    root2, _ = _artifacts(exported[2], tmp_path / "other",
                          rows=[_row(2, "shim.python", "python3", "-c", "...")],
                          answer={"value": float(sealed_truth)})
    reward2, grading2 = _grade(root2, tests, tmp_path / "other")
    assert reward2 == 0 and grading2["checks"]["tools"]["passed"] is False


def test_probe_json_carries_groups_and_targets(exported, sealed_truth, tmp_path):
    """probe.json serialises a group of alternative tools as a list (expect_tools is a
    list of lists) and carries expect_targets; verify_tool.py rebuilds that Probe and
    judges it by tool_tour_core's rules: any tool of a group, on a row that exited ok,
    whose targets match every glob."""
    duckdb = next(iter(tooltour.load_probes(["duckdb"])))
    probe = dataclasses.replace(duckdb, expect_tools=(("duckdb.describe", "duckdb.statement"),),
                                expect_targets=("warehouse.*.fct_orders",))
    doc = tooltour.probe_document(probe)
    assert doc["schema"] == tooltour.PROBE_SCHEMA
    assert doc["probe"]["expect_tools"] == [["duckdb.describe", "duckdb.statement"]]
    assert doc["probe"]["expect_targets"] == ["warehouse.*.fct_orders"]
    plain = tooltour.probe_document(duckdb)["probe"]
    assert plain["expect_tools"] == [["duckdb.statement"]] and plain["expect_targets"] == []

    tests = tmp_path / "tests"
    shutil.copytree(_task(exported[2], DUCKDB) / "tests", tests, symlinks=True)
    (tests / tooltour.PROBE_REL).write_text(json.dumps(doc, indent=2) + "\n",
                                            encoding="utf-8")
    answer = {"value": str(sealed_truth)}
    fct = ("warehouse.warehouse.fct_orders",)
    cases = {  # rows -> (reward, missing_tools, missing_targets)
        "ok": ([_duckdb_row(targets=fct)], 1, [], []),
        "error": ([_duckdb_row(targets=fct, exit="error")], 0,
                  [["duckdb.describe", "duckdb.statement"]], ["warehouse.*.fct_orders"]),
        "untargeted": ([_duckdb_row(targets=("warehouse.warehouse.dim_date",))], 0, [],
                       ["warehouse.*.fct_orders"]),
        "alternative": ([_duckdb_row(targets=fct, exit="error"),
                         _row(3, "duckdb.describe", "duckdb", "-c", "DESCRIBE ...",
                              targets=fct)], 1, [], []),
    }
    for name, (rows, want, missing_tools, missing_targets) in cases.items():
        root, _ = _artifacts(exported[2], tmp_path / name, rows=rows, answer=answer)
        reward, grading = _grade(root, tests, tmp_path / name)
        assert reward == want, (name, grading)
        tools = grading["checks"]["tools"]
        assert tools["passed"] is (want == 1), (name, tools)
        assert tools["detail"]["expect_tools"] == [["duckdb.describe", "duckdb.statement"]]
        assert tools["detail"]["expect_targets"] == ["warehouse.*.fct_orders"]
        assert tools["detail"]["missing_tools"] == missing_tools, name
        assert tools["detail"]["missing_targets"] == missing_targets, name
        assert grading["checks"]["answer"]["passed"] is True, name


def test_verifier_rewards_zero_on_wrong_value(exported, sealed_truth, tmp_path):
    root, tests = _artifacts(exported[2], tmp_path, rows=[_duckdb_row()],
                             answer={"value": str(Decimal(str(sealed_truth)) + 1)})
    reward, grading = _grade(root, tests, tmp_path)
    assert reward == 0, grading
    assert grading["checks"]["answer"]["passed"] is False
    assert grading["checks"]["tools"]["passed"] is True
    # no answer, or one that is not an object with a value, is a wrong answer too
    for n, answer in enumerate(('[2361224]', '{"doc": "refused"}', 'not json')):
        sub = tmp_path / f"bad{n}"
        root_n, _ = _artifacts(exported[2], sub, rows=[_duckdb_row()], answer=answer)
        assert _grade(root_n, tests, sub)[0] == 0, answer


def test_truth_from_sealed_copy_not_live_world(exported, sealed_truth, tmp_path):
    import duckdb

    probe = next(iter(tooltour.load_probes(["duckdb"])))
    root, tests = _artifacts(exported[2], tmp_path, rows=[_duckdb_row()], answer=None)
    # the agent doubles every order in ITS world, then answers what that world says
    con = duckdb.connect(str(root / "world/var/warehouse/warehouse.duckdb"))
    con.execute("UPDATE warehouse.warehouse.fct_orders SET order_amount = order_amount * 2")
    con.close()
    edited = core.truth(probe.truth_sql, root / "world")
    assert Decimal(str(edited)) == 2 * Decimal(str(sealed_truth))
    (root / "workspace/answer.json").write_text(json.dumps({"value": str(edited)}))
    reward, grading = _grade(root, tests, tmp_path)
    assert reward == 0, grading
    assert Decimal(str(grading["truth"]["value"])) == FEB_2026_NET_REVENUE
    # and the sealed truth still pays, whatever the live world now says
    (root / "workspace/answer.json").write_text(json.dumps({"value": str(sealed_truth)}))
    reward, grading = _grade(root, tests, tmp_path / "again")
    assert reward == 1, grading


def test_verifier_rewards_zero_on_tampered_seq(exported, sealed_truth, tmp_path):
    rows = [_row(2, "shim.git", "git", "status"), _duckdb_row(4)]   # seq 3 removed
    root, tests = _artifacts(exported[2], tmp_path, rows=rows,
                             answer={"value": str(sealed_truth)})
    reward, grading = _grade(root, tests, tmp_path)
    assert reward == 0, grading
    assert grading["checks"]["seq"]["passed"] is False
    assert grading["checks"]["tools"]["passed"] is True


def test_verifier_rewards_zero_on_destination_outside(exported, sealed_truth, tmp_path):
    root, tests = _artifacts(
        exported[2], tmp_path, rows=[_duckdb_row()], answer={"value": str(sealed_truth)},
        extra_destinations={"destinations.proxy.jsonl": [
            {"host": "exfil.example.com", "port": 443, "count": 2}]})
    reward, grading = _grade(root, tests, tmp_path)
    assert reward == 0, grading
    assert grading["checks"]["network"]["detail"]["outside"] == ["exfil.example.com:443"]
    # the agent's own traffic to a served destination is no violation
    root2, _ = _artifacts(exported[2], tmp_path / "served", rows=[_duckdb_row()],
                          answer={"value": str(sealed_truth)},
                          destinations={**SERVED, "jira.internal:8080": 7})
    assert _grade(root2, tests, tmp_path / "served")[0] == 1


def test_verifier_unmeasured_without_watermark(exported, sealed_truth, tmp_path):
    # world-up writes the watermark as root: the agent cannot remove it, so its
    # absence is the infrastructure's, never a 0
    root, tests = _artifacts(exported[2], tmp_path, rows=[_duckdb_row()],
                             answer={"value": str(sealed_truth)}, watermark=False)
    reward, grading = _grade(root, tests, tmp_path)
    assert reward is None
    assert grading["unmeasured"]["code"] == "artifacts_missing"


# ------------------------------------------------------------------ the report
def test_report_tabulates_per_tool(tmp_path):
    import tooltour_report

    job = tmp_path / "jobs" / "tour-claude"
    trials = [("duckdb.cli.net_revenue_feb_2026", 1, True, 0, 30),
              ("human.jira.ana238_status", 0, False, 2, 90),
              ("human.slack.analytics_eng_threads", None, None, 0, 10)]
    for n, (probe, reward, used, unknown, seconds) in enumerate(trials):
        trial = job / f"t{n}"
        (trial / "verifier").mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps({
            "trial_name": f"t{n}", "task_name": f"alkera/{probe}__tour",
            "agent_info": {"name": "claude-code", "model_info": {"name": "m"}},
            "verifier_result": ({"rewards": {"reward": reward}} if reward is not None
                                else None),
            "agent_execution": {"started_at": "2026-10-07T10:00:00Z",
                                "finished_at": f"2026-10-07T10:{seconds // 60:02d}:"
                                               f"{seconds % 60:02d}Z"}}))
        if reward is not None:
            (trial / "verifier" / "grading.json").write_text(json.dumps({
                "probe": probe, "tool": probe.split(".")[0], "measured": True,
                "checks": {"tools": {"passed": used}},
                "audit": {"expected_used": used, "unknown_route_rows": unknown,
                          "unknown_route": ["jira.unknown_route"] if unknown else []},
                "answer": {"value": "x"}, "truth": {"value": "y"}}))
    (job / "result.json").write_text(json.dumps({"id": "job"}))   # a job summary: skipped
    rows = tooltour_report.trial_rows(tmp_path / "jobs")
    assert len(rows) == 3
    tools = {t["tool"]: t for t in tooltour_report.per_tool(rows)}
    assert tools["duckdb"]["mean_reward"] == 1.0 and tools["duckdb"]["expected_tool_used"] == 1
    human = tools["human"]
    assert (human["trials"], human["rewarded"], human["unmeasured_or_errored"]) == (2, 0, 1)
    assert human["unknown_route_rows"] == 2
    assert human["unknown_routes"] == ["jira.unknown_route"]
    assert human["median_agent_seconds"] == 50.0
    assert "jira.unknown_route" in tooltour_report.render(rows, list(tools.values()))
