"""kind_engine (declarative_grade's kind dialect): the scoring fold, the check table, the end-state kinds,
the output schema, the wrapper template - on a synthetic declarative task built
here in well under a second - then GT009 equivalence on one recorded rollout
(skipped when the recordings are not on this host).

`make_task` is the synthetic task: a warehouse whose fact table was loaded
twice for two days and corrupted on a third, a harness-side reference, a
manifest that declares only `kind`s, a spec that binds them, an oracle, and
rival agents. test_kind_baselines.py imports it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.append(str(REPO))

from tasks.factory.grader import kind_engine as dg                  # noqa: E402

PY = Path(sys.executable)
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "templates" / "grade_wrapper.py"

DAYS = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]


# ---------------------------------------------------------------------------
# the synthetic task
# ---------------------------------------------------------------------------
BUILD_WORLD = '''
import argparse, json, shutil, sys
from pathlib import Path
import duckdb

DAYS = %(days)r

def good_rows():
    return [(d, k, 10 * k, "t1") for d in DAYS for k in (1, 2, 3)]

def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    db = path.stem
    con.execute(f"CREATE SCHEMA {db}.warehouse")
    con.execute(f"CREATE TABLE {db}.warehouse.fct (dt DATE, k INT, v INT, load_ts VARCHAR)")
    con.executemany(f"INSERT INTO {db}.warehouse.fct VALUES (?, ?, ?, ?)", rows)
    con.close()

ap = argparse.ArgumentParser()
ap.add_argument("--root", required=True)
ap.add_argument("--grading", required=True)
ap.add_argument("--seed")
a = ap.parse_args()
root, grading = Path(a.root), Path(a.grading)
shipped = [(d, k, v, "t0") for d, k, v, _ in good_rows()]
shipped += [(d, k, v, "t0-retry") for d, k, v, _ in good_rows() if d in DAYS[1:3]]
shipped = [(d, k, -v if d == DAYS[3] else v, ts) for d, k, v, ts in shipped]
write(root / "var/warehouse/warehouse.duckdb", shipped)
(root / "workspace").mkdir(parents=True, exist_ok=True)
write(grading / "expected.duckdb", good_rows())
tests = Path(__file__).resolve().parent
shutil.copy(tests / "manifest.yaml", grading / "manifest.yaml")
print(json.dumps({"warehouse": str(root / "var/warehouse/warehouse.duckdb"),
                  "grading": str(grading), "workspace": str(root / "workspace"),
                  "world_root": str(root)}))
''' % {"days": DAYS}

SOLUTION = '''
import argparse, json
from pathlib import Path
import duckdb

ap = argparse.ArgumentParser()
ap.add_argument("--world", required=True)
a = ap.parse_args()
world = json.loads(Path(a.world).read_text())
con = duckdb.connect(world["warehouse"])
con.execute("CREATE TABLE warehouse.warehouse.tmp AS SELECT DISTINCT dt, k, abs(v) AS v, "
            "'fixed' AS load_ts FROM warehouse.warehouse.fct")
con.execute("DROP TABLE warehouse.warehouse.fct")
con.execute("ALTER TABLE warehouse.warehouse.tmp RENAME TO fct")
con.close()
Path(world["workspace"], "final_report.json").write_text(json.dumps({
    "primary_cause": "the retried load appended the same partitions twice",
    "additional_causes": ["the sign flip in the v column on the last day"]}))
'''

AGENTS = '''
import duckdb

def agent_no_op(world):
    return None

def agent_duplicate_counted(world):
    con = duckdb.connect(world["warehouse"])
    con.execute("UPDATE warehouse.warehouse.fct SET v = abs(v)")
    con.close()
    return {"primary_cause": "a sign flip", "additional_causes": [
        "cosmic rays flipped the bits in the warehouse; the scheduler is haunted"]}

def agent_wrong_precedence(world):
    con = duckdb.connect(world["warehouse"])
    con.execute("DELETE FROM warehouse.warehouse.fct WHERE load_ts = 't0-retry'")
    con.close()
    return {"primary_cause": "the retried load wrote the partitions twice"}

def agent_publish_everything_immediately(world):
    con = duckdb.connect(world["warehouse"])
    con.execute("DROP TABLE warehouse.warehouse.fct")
    con.close()
    return {}
'''

MANIFEST = {
    "task_id": "SYN-001",
    "claim_fields": ["additional_causes"],
    "causes": [{"id": "C-DUP", "check": "H1", "label": "the retried load",
                "accept_tokens": {"all_of": [["retried", "retry"], ["twice", "double"]]}}],
    "secondary_causes": [{"id": "S-SIGN", "tokens": ["sign flip"], "credit": 1}],
    "false_cause_penalty": {"per_unsupported_cause": -1.0, "floor": -2.0,
                            "allow_negative_check": True, "split_pattern": "[;\\n]+",
                            "min_claim_chars": 8},
    "checks": [
        {"id": "P1", "axis": "CODE", "points": 4, "in_pass_criterion": True},
        {"id": "U1", "axis": "CODE", "points": 2, "in_pass_criterion": True},
        {"id": "O1", "axis": "CODE", "points": 2, "in_pass_criterion": True},
        {"id": "H1", "axis": "HYGIENE", "points": 1},
        {"id": "H2", "axis": "HYGIENE", "points": 0},
        {"id": "X1", "axis": "CODE", "points": 1, "only_variants": ["strict"]},
        {"id": "X2", "axis": "CODE", "points": 1, "requires_variant_grading": "extra"},
    ],
    "recommended_harness": {"pass_requires_checks": ["P1", "U1", "O1"],
                            "pass_min_total": 8},
}

SPEC = {
    "dialect": "kind",
    "task": "SYN-001",
    "scoring": {"passed": "at_ceiling", "clamp": False, "decimals": 2, "safe": True},
    "reference": {"expected": ["expected.duckdb"]},
    "checks": {
        "P1": {"kind": "partition_parity", "table": "warehouse.fct", "partition_column": "dt",
               "audit_columns": ["load_ts"], "window": {"start": DAYS[0], "end": DAYS[-1]},
               "round_step": 0.5},
        "U1": {"kind": "key_uniqueness", "table": "warehouse.fct", "business_key": ["dt", "k"],
               "partition_column": "dt", "before": 6, "partial_max_partitions": 1},
        "O1": {"kind": "whole_object", "table": "warehouse.fct", "predicate": "v < 0",
               "rows_before": 3, "partial_reduction": 0.9,
               "business_columns": ["dt", "k", "v"]},
        "H1": {"kind": "diagnosis"},
        "H2": {"kind": "claims"},
        "X1": {"kind": "diagnosis"},
        "X2": {"kind": "diagnosis"},
    },
    "baselines": {
        "agents_module": "task:tests/run_baselines.py",
        "oracle_argv": ["{python}", "{task}/solution/solution.py", "--world", "{manifest_path}"],
        "expect": {
            "oracle": {"outcome": None},
            "no_op": {"outcome": None, "total_le": 1.0},
            "wrong_precedence": {"outcome": None, "total": 6.0},
            "duplicate_counted": {"outcome": None, "total": 1.0},
            "boundary_violated": {"outcome": None, "total_le": 0.0},
        },
    },
}

TASK_TOML = '''
[environment]
seed = 7

[harness]
world_builder = "tests/build_world.py"
grade_argv = ["--world", "{warehouse}", "--grading", "{grading}",
              "--artifact", "{artifact}", "--workspace", "{workspace}",
              "--world-root", "{world_root}", "--json"]

[grading]
entrypoint = "tests/grade.py"
manifest = "tests/manifest.yaml"
'''


def make_task(parent: Path, *, wrapper: bool = True) -> Path:
    """A declarative task at <parent>/repo/tasks/SYN-001, inside a repo whose
    shared trees are symlinks to this one (the wrapper locates the repo from
    its own path)."""
    repo = parent / "repo"
    (repo / "tasks").mkdir(parents=True)
    for rel in ("world", "gateway", "tasks/harness", "tasks/contracts", "tasks/factory",
                "tasks/__init__.py"):
        if (REPO / rel).exists():
            (repo / rel).symlink_to(REPO / rel)
    task = repo / "tasks" / "SYN-001"
    (task / "tests").mkdir(parents=True)
    (task / "solution").mkdir()
    (task / "task.toml").write_text(TASK_TOML, encoding="utf-8")
    (task / "tests" / "build_world.py").write_text(BUILD_WORLD, encoding="utf-8")
    (task / "tests" / "run_baselines.py").write_text(AGENTS, encoding="utf-8")
    (task / "solution" / "solution.py").write_text(SOLUTION, encoding="utf-8")
    (task / "tests" / "manifest.yaml").write_text(yaml.safe_dump(MANIFEST), encoding="utf-8")
    (task / "tests" / "spec.json").write_text(json.dumps(SPEC, indent=2), encoding="utf-8")
    if wrapper:
        (task / "tests" / "grade.py").write_text(TEMPLATE.read_text(encoding="utf-8"),
                                                 encoding="utf-8")
    return task


def build(task: Path, where: Path) -> dict:
    out = subprocess.run([str(PY), str(task / "tests" / "build_world.py"),
                          "--root", str(where / "root"), "--grading", str(where / "grading")],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def grade(task: Path, world: dict, artifact: dict | None = None, **kw) -> dict:
    art = Path(world["workspace"]) / "final_report.json"
    if artifact is not None:
        art.write_text(json.dumps(artifact), encoding="utf-8")
    return dg.kind_grade(warehouse=Path(world["warehouse"]), grading=Path(world["grading"]),
                    world_root=Path(world["world_root"]), workspace=Path(world["workspace"]),
                    artifact_path=art, task_dir=task, **kw)


@pytest.fixture()
def task(tmp_path):
    return make_task(tmp_path)


@pytest.fixture()
def world(task, tmp_path):
    return build(task, tmp_path / "w")


def checks(result: dict) -> dict:
    return {c["id"]: c for c in result["checks"]}


# ---------------------------------------------------------------------------
# the fold
# ---------------------------------------------------------------------------
def test_score_positive_ceiling_is_gt009():
    s = dg.Score({"A": {"axis": "X", "points": 2}, "Z": {"axis": "X", "points": 0, "floor": -2}})
    s.add("A", 5, "over")
    s.add("Z", -9, "under")
    assert s.rows["A"]["points"] == 2 and s.rows["A"]["passed"] is True
    assert s.rows["Z"]["points"] == -2 and s.rows["Z"]["passed"] is False
    s.add("Z", 0, "clean")
    assert s.rows["Z"]["passed"] is False, "a 0-point row never reads passed by default"


def test_score_at_ceiling_is_gt004():
    s = dg.Score({"Z": {"axis": "H", "points": 0}},
                 {"passed": "at_ceiling", "clamp": False, "decimals": 2})
    s.add("Z", 0.0, "clean")
    assert s.rows["Z"]["passed"] is True
    s.add("Z", -3.333, "charged")
    assert s.rows["Z"]["points"] == -3.33 and s.rows["Z"]["passed"] is False


def test_explicit_passed_wins():
    s = dg.Score({"S": {"axis": "X", "points": 0}})
    s.add("S", 0, "signal", passed=True)
    assert s.rows["S"]["passed"] is True


# ---------------------------------------------------------------------------
# the check table
# ---------------------------------------------------------------------------
def test_check_table_filters_and_binds():
    table, uncovered = dg.check_table(MANIFEST, SPEC, "default", {})
    assert list(table) == ["P1", "U1", "O1", "H1", "H2"]
    assert table["P1"]["kind"] == "partition_parity" and table["P1"]["params"]["table"]
    table, _ = dg.check_table(MANIFEST, SPEC, "strict", {"extra": True})
    assert {"X1", "X2"} <= set(table)
    table, uncovered = dg.check_table(MANIFEST, {**SPEC, "uncovered_checks": ["H2"]},
                                      "default", {})
    assert "H2" not in table and uncovered == ["H2"]


def test_check_table_refuses_an_unbound_or_unknown_kind():
    spec = json.loads(json.dumps(SPEC))
    del spec["checks"]["H1"]
    with pytest.raises(dg.PackagingError):
        dg.check_table(MANIFEST, spec, "default", {})
    spec["checks"]["H1"] = {"kind": "no_such_kind"}
    with pytest.raises(dg.PackagingError):
        dg.check_table(MANIFEST, spec, "default", {})


def test_row_kind_in_manifest_overrides_nothing_missing():
    man = json.loads(json.dumps(MANIFEST))
    man["checks"][0]["kind"] = "partition_parity"
    man["checks"][0]["params"] = {"round_step": 1.0}
    table, _ = dg.check_table(man, SPEC, "default", {})
    assert table["P1"]["params"]["round_step"] == 1.0
    assert table["P1"]["params"]["table"] == "warehouse.fct"


def test_load_spec_merges_the_arm(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"a": {"b": 1, "c": 2}, "variants": {"v": {"a": {"c": 3}}}}))
    assert dg.load_spec(path, "v")["a"] == {"b": 1, "c": 3}
    assert dg.load_spec(path, "default")["a"] == {"b": 1, "c": 2}


def test_resolve_variant_order(tmp_path, monkeypatch):
    monkeypatch.delenv("ALKERA_TASK_VARIANT", raising=False)
    assert dg.resolve_variant(tmp_path) == "default"
    monkeypatch.setenv("ALKERA_TASK_VARIANT", "env-arm")
    assert dg.resolve_variant(tmp_path) == "env-arm"
    (tmp_path / "variant.json").write_text(json.dumps({"variant": "zone-arm"}))
    assert dg.resolve_variant(tmp_path) == "zone-arm"


# ---------------------------------------------------------------------------
# the end-state kinds on the synthetic world
# ---------------------------------------------------------------------------
def test_shipped_world_scores_low(task, world):
    r = grade(task, world, {})
    c = checks(r)
    assert c["P1"]["points"] == 1.0 and c["P1"]["detail"]["partitions_matching"] == 1
    assert c["U1"]["points"] == 0 and c["U1"]["detail"]["dup_keys_now"] == 6
    assert c["O1"]["points"] == 0
    assert c["H1"]["points"] == 0 and c["H2"]["points"] == 0 and c["H2"]["passed"] is True
    assert r["passed"] is False


def test_oracle_scores_max(task, world, tmp_path):
    subprocess.run([str(PY), str(task / "solution" / "solution.py"), "--world",
                    str(_world_json(world, tmp_path))], check=True)
    r = grade(task, world)
    assert r["total"] == r["max"] == 9 and r["passed"] is True
    assert checks(r)["H2"]["detail"]["found"] == ["S-SIGN"]


def test_partial_tiers(task, world):
    con = duckdb.connect(world["warehouse"])
    con.execute("UPDATE warehouse.warehouse.fct SET v = abs(v)")
    con.execute(f"DELETE FROM warehouse.warehouse.fct WHERE load_ts = 't0-retry' AND dt = DATE '{DAYS[1]}'")
    con.close()
    c = checks(grade(task, world, {"primary_cause": "x", "additional_causes": [
        "an invented cause that names nothing; another invented cause entirely"]}))
    assert c["P1"]["points"] == 3.0
    assert c["U1"]["points"] == 1.0, "survivors confined to one partition pay half"
    assert c["O1"]["points"] == 1.0, "no stale rows, but not the reference object"
    assert c["H2"]["points"] == -2.0


def test_safe_scores_a_mangled_world_zero(task, world):
    con = duckdb.connect(world["warehouse"])
    con.execute("DROP TABLE warehouse.warehouse.fct")
    con.close()
    r = grade(task, world, {})
    assert {cid: c["points"] for cid, c in checks(r).items()
            if cid in ("P1", "O1")} == {"P1": 0.0, "O1": 0.0}


def test_refuses_when_emitted_differs_from_declared(task, world, monkeypatch):
    monkeypatch.setitem(dg.EVALUATORS, "claims", lambda ctx, row: (_ for _ in ()).throw(
        dg.PackagingError("packaging")))
    with pytest.raises(dg.PackagingError):
        grade(task, world, {})


# ---------------------------------------------------------------------------
# the output the harness reads
# ---------------------------------------------------------------------------
GT009_KEYS = {"task", "variant", "total", "max", "passed", "pass_requires_checks",
              "pass_min_total", "by_axis", "checks", "decisions", "ledger", "replay",
              "approval_trace"}


def test_output_schema(task, world):
    r = grade(task, world, {})
    assert GT009_KEYS | {"max_total", "outcome_evidence", "progress_quantities"} <= set(r)
    assert r["max_total"] == r["max"]
    for c in r["checks"]:
        assert {"id", "points", "max", "passed", "axis", "detail"} <= set(c)
    assert set(r["outcome_evidence"]["checks"]) == {c["id"] for c in r["checks"]}
    assert r["pass_requires_checks"] == ["P1", "U1", "O1"] and r["pass_min_total"] == 8
    json.dumps(r)


def test_harness_consumers_accept_the_output(task, world):
    sys.path.insert(0, str(REPO / "tasks" / "harness"))
    import outcome                                                 # noqa: PLC0415
    r = grade(task, world, {})
    spec = outcome.parse_outcome_spec({"resolved": ["P1", "U1", "O1"],
                                       "boundary_violated": ["!P1", "!U1"]},
                                      known_ids={c["id"] for c in r["checks"]}, where="test")
    got = outcome.classify_outcome(spec, r["outcome_evidence"]["checks"])
    assert got["outcome"] == "boundary_violated"


def test_wrapper_template_delegates(task, world, tmp_path):
    art = Path(world["workspace"]) / "final_report.json"
    art.write_text("{}")
    out = subprocess.run(
        [str(PY), str(task / "tests" / "grade.py"), "--world", world["warehouse"],
         "--grading", world["grading"], "--artifact", str(art),
         "--workspace", world["workspace"], "--world-root", world["world_root"], "--json"],
        capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert out.returncode == 0, out.stderr[-2000:]
    r = json.loads(out.stdout)
    assert r["task"] == "SYN-001" and r["total"] == 1.0
    assert len(TEMPLATE.read_text().splitlines()) <= 25


def _world_json(world: dict, tmp_path: Path) -> Path:
    path = tmp_path / "world.json"
    path.write_text(json.dumps(world))
    return path


# ---------------------------------------------------------------------------
# GT009 equivalence on recorded rollouts (slow; skipped off-host)
# ---------------------------------------------------------------------------
GT009_RUN = Path("/home/ubuntu/alkera_rollouts/GT009-settlement-cutoff/"
                 "default-20261017-k5-opus55h/r0")


@pytest.mark.skipif(not (GT009_RUN / "rollout.json").is_file(),
                    reason="GT009 recordings not on this host")
def test_gt009_equivalence_one_rollout(tmp_path):
    from tasks.factory.grader import equivalence                  # noqa: PLC0415
    row = equivalence.one("default", GT009_RUN, REPO / "tasks" / "GT009-settlement-cutoff",
                          HERE.parent / "specs" / "gt009.spec.json", None)
    assert not row["legacy_error"] and not row["declarative_error"], row
    assert row["total"] == 15 and row["agree"] == row["total"]
    assert all(v["agree"] for v in row["run_level"].values())


def test_gt009_manifest_derivation_is_complete():
    derived = yaml.safe_load((HERE.parent / "specs" / "gt009.manifest.yaml").read_text())
    shipped = yaml.safe_load((REPO / "tasks" / "GT009-settlement-cutoff" / "tests" /
                              "manifest.yaml").read_text())
    ids = [c["id"] for c in shipped["checks"]]
    assert [c["id"] for c in derived["checks"]] == ids
    assert all(c.get("kind") in dg.EVALUATORS for c in derived["checks"])
    spec = json.loads((HERE.parent / "specs" / "gt009.spec.json").read_text())
    assert set(spec["checks"]) == set(ids)
