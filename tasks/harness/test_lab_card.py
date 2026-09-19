"""lab_card.py - the A1-A12 / O1-O8 requirement card.

Rows are pinned against GT009 AS IT IS ON DISK. Rows other agents are moving
concurrently (role=, [matrix.progress], DESIGN.md §7, export.rft_grader) are
asserted conditionally on the file state, the same way test_validity.py
treats the section-0 defects.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

import lab_card
from lab_card import REQUIREMENTS, RUN_DEPENDENT, evaluate, render

TASKS = Path(__file__).resolve().parent.parent
GT009 = "GT009-settlement-cutoff"
IDS = {f"A{i}" for i in range(1, 13)} | {f"O{i}" for i in range(1, 9)}


def test_every_requirement_has_text_source_and_probe():
    assert set(REQUIREMENTS) == IDS
    for rid, req in REQUIREMENTS.items():
        assert req["text"].strip(), rid
        assert req["source"] in (lab_card.ANTHROPIC, lab_card.OPENAI), rid
        assert (req["source"] == lab_card.ANTHROPIC) == rid.startswith("A")
        assert callable(req["probe"]), rid
    assert set(RUN_DEPENDENT) <= IDS


def test_evaluate_returns_a_status_for_every_row():
    rows = evaluate(GT009, TASKS)
    assert set(rows) == IDS
    for rid, row in rows.items():
        assert row["status"] in lab_card.STATUSES, rid
        assert row["detail"].strip(), rid
        assert row["text"] == REQUIREMENTS[rid]["text"]
    json.dumps(rows)


def test_gt009_static_rows_met_today():
    rows = evaluate(GT009, TASKS)
    # deterministic grader (no judge import; mode = deterministic) - A6 and O1
    assert rows["A6"]["status"] == "met" and rows["O1"]["status"] == "met"
    assert "same probe as A6" in rows["O1"]["detail"]
    # outcome graded, not the path: grade.py never reads the action log
    assert rows["A1"]["status"] == "met"
    # oracle row at max on both arms
    assert rows["A2"]["status"] == "met"
    # per-trial world + determinism recorded
    assert rows["A5"]["status"] == "met"
    # falsifier recorded, false_claim / boundary_violated charged - A8 and O6
    assert rows["A8"]["status"] == "met" and rows["O6"]["status"] == "met"
    # pass axis mapped, withheld_with_evidence declared
    assert rows["O4"]["status"] == "met"


def test_gt009_run_dependent_rows_need_a_run():
    rows = evaluate(GT009, TASKS, stored_runs=None)
    for rid in RUN_DEPENDENT:
        assert rows[rid]["status"] == "needs_run", (rid, rows[rid])
        assert "report.json" in rows[rid]["detail"]
    # a stored-runs dir with nothing for the task is still needs_run, and says where it looked
    rows = evaluate(GT009, TASKS, stored_runs=TASKS / "nowhere")
    for rid in RUN_DEPENDENT:
        assert rows[rid]["status"] == "needs_run"
        assert "no report.json under" in rows[rid]["detail"]


def test_gt009_two_arms_and_roles_read_from_disk():
    raw = tomllib.loads((TASKS / GT009 / "task.toml").read_text(encoding="utf-8"))
    roles = {a: b.get("role") for a, b in raw["variants"].items()}
    rows = evaluate(GT009, TASKS)
    assert len(roles) >= 2
    if {"control", "decision"} <= set(roles.values()) and all(roles.values()):
        assert rows["A4"]["status"] == "met"
    else:
        assert rows["A4"]["status"] == "unmet" and "role" in rows["A4"]["detail"]


def test_gt009_progress_rows_follow_the_toml():
    raw = tomllib.loads((TASKS / GT009 / "task.toml").read_text(encoding="utf-8"))
    has_progress = all(
        isinstance(((raw.get("variants") or {}).get(a) or {}).get("matrix", {})
                   .get("progress", (raw.get("matrix") or {}).get("progress")), dict)
        for a in raw["variants"])
    rows = evaluate(GT009, TASKS)
    assert rows["A7"]["status"] == rows["O5"]["status"]
    assert "same probe as A7" in rows["O5"]["detail"]
    if has_progress:
        assert rows["A7"]["status"] == "met"
    else:
        assert rows["A7"]["status"] == "unmet"
        assert "no progress quantities declared" in rows["A7"]["detail"]


def test_gt009_preregistration_row_follows_design_md():
    design = (TASKS / GT009 / "DESIGN.md").read_text(encoding="utf-8")
    rows = evaluate(GT009, TASKS)
    prereg = re.search(r"pre-?regist|predicted band|failure hypothesis", design, re.I)
    if prereg and re.search(r"^##\s*7\b", design, re.M):
        assert rows["A3"]["status"] == "met"
    else:
        assert rows["A3"]["status"] == "unmet" and "DESIGN.md" in rows["A3"]["detail"]


def test_o8_reads_the_harness_export_module():
    src = (Path(__file__).resolve().parent / "export.py").read_text(encoding="utf-8")
    rows = evaluate(GT009, TASKS)
    expected = "met" if re.search(r"^\s*def\s+rft_grader\s*\(", src, re.M) else "unmet"
    assert rows["O8"]["status"] == expected


def test_render_is_a_markdown_table():
    out = render(GT009, TASKS, None)
    assert out.startswith("| id | requirement | status | detail |\n|---|---|---|---|\n")
    for rid in IDS:
        assert f"| {rid} |" in out
    assert "A12" in out and "O8" in out
    assert lab_card.ANTHROPIC in out and lab_card.OPENAI in out


def test_unknown_task_is_a_refusal():
    with pytest.raises(lab_card.validity.taskspec.TaskSpecError):
        evaluate("GT000-nope", TASKS)


# -- probes against a synthetic package and a synthetic report ---------------

def _package(tmp_path: Path, *, grade_imports="import duckdb\n",
             design="# D\n\n## 2. staged\nowner boundary consumer cost\n\n"
                    "## 7. Pre-registration\npredicted band: control\n",
             validity_md="falsifier 30/30; determinism: two builds agree\n",
             toml_extra="") -> Path:
    root = tmp_path / "GT0XX-synthetic"
    (root / "tests").mkdir(parents=True)
    (root / "fixtures").mkdir()
    (root / "tests" / "grade.py").write_text(grade_imports, encoding="utf-8")
    (root / "DESIGN.md").write_text(design, encoding="utf-8")
    (root / "tests" / "VALIDITY.md").write_text(validity_md, encoding="utf-8")
    (root / "tests" / "run_baselines.py").write_text(
        'ap.add_argument("--json")\n', encoding="utf-8")
    (root / "task.toml").write_text(f"""
[grading]
entrypoint = "tests/grade.py"
mode = "deterministic"
action_log_required = false
[harness]
world_builder = "tests/build_world.py"
[matrix]
pass_axis = "consumer"
gate = "found"
accepted = ["oracle"]
[matrix.map]
found = ["C0"]
consumer = ["R1"]
[matrix.outcome]
false_claim = ["!H1"]
boundary_violated = ["!H2"]
resolved = ["R1"]
withheld_with_evidence = ["D3"]
acted = ["C0"]
[variants.default]
role = "control"
note = "n"
[variants.decision]
role = "decision"
note = "n"
{toml_extra}
[[validity]]
agent = "oracle"
expect = "== max"
[[validity]]
agent = "twin"
expect = "== max"
""", encoding="utf-8")
    return root


def test_synthetic_package_meets_every_static_row(tmp_path):
    _package(tmp_path, toml_extra="""
[matrix.progress]
rows = {check = "R1", field = "repaired", noop = 0, oracle = "max"}
weights = {rows = 1.0}
""")
    rows = evaluate("GT0XX-synthetic", tmp_path)
    static = IDS - set(RUN_DEPENDENT) - {"O8"}      # O8 reads the harness, not the task
    for rid in static:
        assert rows[rid]["status"] == "met", (rid, rows[rid]["detail"])
    assert "declares --json" in rows["A2"]["detail"]


def test_synthetic_package_unmet_rows_name_the_gap(tmp_path):
    _package(tmp_path, grade_imports="import openai\nfrom judge import score\n",
             design="# D\n## 7. Something else\n",
             validity_md="nothing measured\n",
             toml_extra="")
    rows = evaluate("GT0XX-synthetic", tmp_path)
    assert rows["A6"]["status"] == "unmet" and "openai" in rows["A6"]["detail"]
    assert rows["A3"]["status"] == "unmet" and "not a pre-registration" in rows["A3"]["detail"]
    assert rows["A5"]["status"] == "unmet" and "determinism" in rows["A5"]["detail"]
    assert rows["A8"]["status"] == "unmet" and "falsifier" in rows["A8"]["detail"]
    assert rows["A7"]["status"] == "unmet" and "no progress quantities" in rows["A7"]["detail"]
    assert rows["O3"]["status"] == "unmet" and "boundary" in rows["O3"]["detail"]


def test_run_dependent_rows_read_the_newest_report(tmp_path):
    root = _package(tmp_path)
    runs = tmp_path / "runs"
    old = runs / root.name / "run-old"
    new = runs / root.name / "run-new"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "report.json").write_text(json.dumps({"pass_at_1": 1.0}), encoding="utf-8")
    import os
    import time
    report = {"pass_at_1": 0.4, "pass_at_1_n": 5, "pass_at_1_ci95": [0.1, 0.8],
              "pass_at_k": {"1": 0.4}, "pass_hat_k": 0.0,
              "learning_signal": {"band": "core",
                                  "group": {"collapsed": False, "p_one_minus_p": 0.24}},
              "tracked_metrics": {"n_toolcalls": 12},
              "balance": {"role": "control", "share_correct": 0.4},
              "fairness_read": {"reviewed": 5}}
    (new / "report.json").write_text(json.dumps(report), encoding="utf-8")
    later = time.time() + 5
    os.utime(new / "report.json", (later, later))
    rows = evaluate(root.name, tmp_path, stored_runs=runs)
    for rid in RUN_DEPENDENT:
        assert rows[rid]["status"] == "met", (rid, rows[rid]["detail"])
        assert "run-new" in rows[rid]["detail"] or "report.json" in rows[rid]["detail"]


def test_run_dependent_rows_unmet_when_the_report_lacks_them(tmp_path):
    root = _package(tmp_path)
    runs = tmp_path / "runs"
    (runs / root.name).mkdir(parents=True)
    report = {"pass_at_1": 1.0, "pass_at_1_ci95": [0.5, 1.0], "pass_at_k": {"1": 1.0},
              "learning_signal": {"band": "control",
                                  "group": {"collapsed": True, "p_one_minus_p": 0.0}},
              "tool_calls_median": 30, "matrix": {"outcome_counts": {"resolved": 5}}}
    (runs / root.name / "report.json").write_text(json.dumps(report), encoding="utf-8")
    rows = evaluate(root.name, tmp_path, stored_runs=runs)
    assert rows["A9"]["status"] == "met"
    assert rows["A11"]["status"] == "met" and "control" in rows["A11"]["detail"]
    assert rows["O2"]["status"] == "unmet" and "collapsed" in rows["O2"]["detail"]
    assert rows["A12"]["status"] == "unmet" and "tool_calls_median" in rows["A12"]["detail"]
    assert rows["O7"]["status"] == "unmet" and "outcome_counts" in rows["O7"]["detail"]
    assert rows["A10"]["status"] == "unmet" and "fairness" in rows["A10"]["detail"]
