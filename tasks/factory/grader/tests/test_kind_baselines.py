"""baseline_runner: the five canonical baselines built, applied and graded
end to end on the synthetic declarative task (test_kind_engine.make_task),
the baselines file validated by tasks/harness/baselines_schema, and the
expectation / agreement readers on their own.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.append(str(REPO))

from tasks.factory.grader import kind_baselines as br               # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_syn_task", Path(__file__).resolve().parent / "test_kind_engine.py")
syn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(syn)

sys.path.insert(0, str(REPO / "tasks" / "harness"))
import baselines_schema                                                # noqa: E402


@pytest.fixture()
def task(tmp_path):
    return syn.make_task(tmp_path)


def test_canonical_baselines_end_to_end(task, tmp_path):
    out = tmp_path / "baselines"
    doc = br.run(task, "default", out, scratch=tmp_path / "scratch")
    assert doc["task_valid"] is True, {k: v["misses"] for k, v in doc["baselines"].items()}
    assert list(doc["baselines"]) == list(br.CANONICAL)
    rows = doc["baselines"]
    assert rows["oracle"]["total"] == rows["oracle"]["max"] == 9 and rows["oracle"]["passed"]
    assert rows["no_op"]["total"] == 1.0
    assert rows["wrong_precedence"]["total"] == 6.0 and not rows["wrong_precedence"]["passed"]
    assert rows["duplicate_counted"]["total"] == 1.0
    assert rows["boundary_violated"]["total"] == 0.0
    assert rows["boundary_violated"]["checks"]["P1"]["detail"].startswith(
        "could not be evaluated")
    written = json.loads((out / "default.json").read_text())
    assert baselines_schema.validate(written) == []
    assert written["grader"] == "tasks/factory/grader/declarative_grade.py"


def test_a_missed_expectation_invalidates_the_task(task, tmp_path):
    spec = json.loads((task / "tests" / "spec.json").read_text())
    spec["baselines"]["expect"]["no_op"]["total_le"] = 0.0
    (task / "tests" / "spec.json").write_text(json.dumps(spec))
    doc = br.run(task, "default", tmp_path / "out", only=["no_op"],
                 scratch=tmp_path / "scratch")
    assert doc["task_valid"] is False
    assert doc["baselines"]["no_op"]["misses"] == ["total 1.0 > 0.0"]


def test_expectations_reader():
    score = {"total": 5.0, "max": 9.0, "passed": False,
             "outcome_evidence": {"outcome": "acted_unresolved"},
             "checks": [{"id": "A", "points": 2.0, "passed": True}]}
    assert br.expectations(score, {"total": "max"}) == ["total 5.0 != max 9.0"]
    assert br.expectations(score, {"total_le": 5.0, "passed": False,
                                   "outcome": "acted_unresolved",
                                   "checks": {"A": {"points": 2.0}}}) == []
    assert br.expectations(score, {"outcome": "resolved"}) == [
        "outcome 'acted_unresolved' != 'resolved'"]
    assert br.expectations(score, {"checks": {"B": {"passed": True}}}) == ["B not emitted"]


def test_agreement_reader():
    a = {"checks": [{"id": "A", "points": 1.0, "passed": True},
                    {"id": "B", "points": 0.0, "passed": False}]}
    b = {"checks": [{"id": "A", "points": 1.0, "passed": True},
                    {"id": "C", "points": 0.0, "passed": False}]}
    assert br.agreement(a, a) == []
    assert br.agreement(a, b) == ["B: legacy (0.0, False) declarative None",
                                  "C: legacy None declarative (0.0, False)"]


def test_wrapper_detection_and_artifact_path(task):
    assert br._is_wrapper(task / "tests" / "grade.py")
    assert not br._is_wrapper(task / "tests" / "build_world.py")
    raw = br._task_toml(task)
    assert br.artifact_path(task, raw, {"workspace": "/w"}) == Path("/w/final_report.json")
    raw["grading"]["artifact"] = "/workspace/out/report.json"
    assert br.artifact_path(task, raw, {"workspace": "/w"}) == Path("/w/out/report.json")


GT009 = REPO / "tasks" / "GT009-settlement-cutoff"


@pytest.mark.skipif(not (GT009 / "tests" / "run_baselines.py").is_file(),
                    reason="GT009 not in this tree")
def test_gt009_baseline_config_resolves():
    cfg = br._baseline_cfg(GT009, "default",
                           REPO / "tasks" / "factory" / "grader" / "specs" / "gt009.spec.json")
    assert set(cfg["agents"]) == set(br.CANONICAL)
    spec = importlib.util.spec_from_file_location("_gt009_agents",
                                                  GT009 / "tests" / "run_baselines.py")
    assert spec is not None
    text = (GT009 / "tests" / "run_baselines.py").read_text()
    for name, fn in cfg["agents"].items():
        assert fn == "solution" or f"def {fn}(" in text, name
