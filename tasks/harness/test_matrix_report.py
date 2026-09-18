"""The scoring-matrix row in the run report (new_matrix.md, Layer A).

    python -m pytest test_matrix_report.py -q

Synthetic records and a stub spec - no world, no grader, no model. The four
things pinned here are the four ways a matrix report can lie:

(a) a GATE-COLD episode must leave the other axes' denominators rather than
    score 0 on them - the same honesty rule as quarantine;
(b) an old record that LACKS the mapped checks is n/a on those axes, never 0;
(c) a run whose projected-pass denominator is empty WITHHOLDS the number
    rather than printing 0.0 over nothing;
(d) a task without a [matrix] renders byte-identical to before.

Plus the two run-level wirings: a run whose arms resolve to DIFFERENT pass
axes has no single projection, and `--re-report` rewrites a report from stored
records without re-grading anything.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report          # noqa: E402
import run_rollouts    # noqa: E402
from axes import MatrixSpec  # noqa: E402


class StubSpec:
    """What report.py reads of a taskspec.TaskSpec.

    `matrix` always; `task_id`/`root`/`variant` only when a run mixes arms and
    the other arms' matrices have to be resolved. The task id names no real
    package on purpose: resolving it must fail, which is itself one of the
    cases under test.
    """

    def __init__(self, matrix, variant: str = ""):
        self.matrix = matrix
        self.variant = variant
        self.task_id = "GT999-no-such-task"
        self.root = Path(__file__).resolve().parent.parent / self.task_id


MATRIX = MatrixSpec(pass_axis="warranted", gate="found",
                    map={"found": ["H1b"], "warranted": ["H3", "G1c"],
                         "consumer": ["C1"]})  # scoped deliberately unmapped


def rec(rid: str, checks: dict[str, bool], scored: bool = True,
        classification: str = "pass", passed: bool = True, **extra) -> dict:
    """One rollout record, honest enough to be a clean measurement."""
    r = {
        "rollout_id": rid, "scored": scored, "passed": passed,
        "classification": classification,
        "checks": {cid: {"points": 1.0 if p else 0.0, "max": 1.0, "passed": p,
                         "detail": ""}
                   for cid, p in checks.items()},
        "score": {"total": sum(1.0 for p in checks.values() if p),
                  "max": float(len(checks))},
        "tool_calls": 3, "wall_clock_s": 10.0,
        "model": "stub-model", "driver": "scripted",
        "surface_audit": {"live": ["dbt", "metrics"], "dead": [],
                          "gate": "pass", "declared_probes": 2,
                          "swept_probes": 1},
    }
    r.update(extra)
    return r


def run_md(records, spec) -> str:
    return report.render("GT999-no-such-task", "run-x",
                         report.aggregate(records, spec=spec), records,
                         spec=spec)


# -- (a) a gate-cold episode leaves the denominators --------------------------

def test_gate_cold_episode_leaves_every_denominator():
    spec = StubSpec(MATRIX)
    records = [
        rec("r0", {"H1b": True, "H3": True, "G1c": True, "C1": True}),
        rec("r1", {"H1b": True, "H3": False, "G1c": False, "C1": False},
            classification="reasoning", passed=False),
        # gate cold, yet every other check passed: if this episode entered the
        # warranted/consumer means they would read 1.0 instead of 0.5
        rec("r2", {"H1b": False, "H3": True, "G1c": True, "C1": True},
            classification="reasoning", passed=False),
    ]
    m = report.aggregate(records, spec=spec)["matrix"]
    assert m["scored"] == 3
    assert m["admitted"] == 2 and m["gate_cold"] == 1
    assert m["axis_means"]["found"] == 1.0
    assert m["axis_means"]["warranted"] == 0.5   # (1.0 + 0.0) / 2, not / 3
    assert m["axis_means"]["consumer"] == 0.5
    assert m["axis_means"]["scoped"] is None     # unmapped axis: n/a, never 0
    assert m["projected_pass"] == 0.5
    assert m["projected_pass_denominator"] == 2
    assert m["per_rollout"]["r2"]["admitted"] is False
    assert m["per_rollout"]["r2"]["pass"] is None

    md = run_md(records, spec)
    assert "**Projected pass (π=warranted) = 0.5** over 2 admitted rollouts" in md
    assert "scoped n/a" in md
    # the old criterion stays, relabelled and BELOW the projection
    assert "**pass_all_required = " in md
    assert md.index("Projected pass") < md.index("pass_all_required")
    assert "**Pass@1 =" not in md


# -- (b) missing checks on an old record are n/a, never 0 ---------------------

def test_old_record_without_mapped_checks_is_unmeasured_not_failed():
    spec = StubSpec(MATRIX)
    records = [
        rec("r0", {"H1b": True, "H3": True, "G1c": True, "C1": True}),
        rec("r1", {"H1b": True, "H3": False, "G1c": False, "C1": False},
            classification="reasoning", passed=False),
        # graded by a rubric that predates the matrix: none of the mapped ids
        rec("r_old", {"OLD1": True, "OLD2": False}),
    ]
    m = report.aggregate(records, spec=spec)["matrix"]
    row = m["per_rollout"]["r_old"]
    assert row["axes"] == {"found": None, "warranted": None,
                           "scoped": None, "consumer": None}
    assert row["admitted"] is None and row["pass"] is None
    assert m["gate_unmeasured"] == 1
    # the run-level numbers are exactly what they were without the old record
    assert m["admitted"] == 2 and m["projected_pass"] == 0.5
    assert m["axis_means"]["warranted"] == 0.5
    assert "n/a" in run_md(records, spec)


# -- (c) an empty denominator withholds ----------------------------------------

def test_empty_pass_denominator_withholds():
    spec = StubSpec(MATRIX)
    records = [rec(f"r{i}", {"H1b": False, "H3": True, "G1c": True},
                   classification="reasoning", passed=False)
               for i in range(3)]
    agg = report.aggregate(records, spec=spec)
    m = agg["matrix"]
    assert m["projected_pass"] is None
    assert m["projected_pass_denominator"] == 0
    assert m["gate_cold"] == 3
    md = run_md(records, spec)
    assert ("**Projected pass (π=warranted) = WITHHELD - no scored rollout was "
            "admitted to the denominator**") in md
    assert "0.0** over 0" not in md
    # the old criterion still reports, below the withheld projection
    assert "**pass_all_required = 0.0**" in md
    assert md.index("Projected pass") < md.index("pass_all_required")


# -- (d) non-matrix tasks render byte-identical ---------------------------------

def test_non_matrix_rendering_is_unchanged():
    records = [
        rec("r0", {"D1": True, "D2": True}),
        rec("r1", {"D1": True, "D2": False},
            classification="reasoning", passed=False),
    ]
    plain_agg = report.aggregate(records)
    specless_agg = report.aggregate(records, spec=StubSpec(None))
    assert "matrix" not in plain_agg
    assert json.dumps(plain_agg, sort_keys=True, default=str) == \
        json.dumps(specless_agg, sort_keys=True, default=str)
    plain_md = report.render("T", "run", plain_agg, records)
    assert plain_md == report.render("T", "run", specless_agg, records,
                                     spec=StubSpec(None))
    assert plain_md == report.render("T", "run", plain_agg, records, spec=None)
    assert "**Pass@1 = " in plain_md and "pass_all_required" not in plain_md
    assert "Projected pass" not in plain_md


# -- write(): the row survives to disk -----------------------------------------

def test_write_puts_the_matrix_row_in_report_json(tmp_path):
    spec = StubSpec(MATRIX)
    records = [rec("r0", {"H1b": True, "H3": True, "G1c": True})]
    agg = report.write(tmp_path, "GT999-no-such-task", "run-x", records,
                       spec=spec)
    stored = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert stored["summary"]["matrix"]["projected_pass"] == 1.0
    assert stored["summary"]["matrix"]["per_rollout"]["r0"]["pass"] is True
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "**Projected pass (π=warranted) = 1.0**" in md
    # and the stored agg renders identically without re-deriving anything
    assert md == report.render("GT999-no-such-task", "run-x", agg, records)


# -- a run whose arms project onto different axes has no projection -------------

def test_mixed_pass_axes_invalidate_the_projection():
    spec = StubSpec(MATRIX, variant="a")
    records = [
        rec("r0", {"H1b": True, "H3": True, "G1c": True}, variant="a"),
        rec("r1", {"H1b": True, "H3": True, "G1c": True}, variant="b"),
    ]
    agg = report.aggregate(records, spec=spec)
    m = agg["matrix"]
    # arm "b" names no real package, so its matrix cannot be resolved - and a
    # projection that cannot prove both arms agree is no projection
    assert m["pass_axis_conflict"] is not None
    assert agg["measurement_valid"] is False
    assert m["pass_axis_conflict"] in agg["invalidated_because"]
    md = run_md(records, spec)
    assert "**Projected pass (π=warranted) = WITHHELD" in md
    assert m["pass_axis_conflict"] in md


# -- --re-report: new report, old records, nothing re-graded --------------------

def _run_dir(tmp_path: Path, records) -> Path:
    run = tmp_path / "GT999-no-such-task" / "run-x"
    for i, r in enumerate(records):
        d = run / f"r{i}"
        d.mkdir(parents=True)
        (d / "rollout.json").write_text(json.dumps(r), encoding="utf-8")
    return run


def test_re_report_rewrites_the_report_without_regrading(tmp_path):
    records = [rec("r0", {"D1": True}), rec("r1", {"D1": False}, passed=False)]
    run = _run_dir(tmp_path, records)
    assert run_rollouts.re_report(run, None, None) == 0
    stored = json.loads((run / "report.json").read_text(encoding="utf-8"))
    assert stored["run_id"] == "run-x" and len(stored["rollouts"]) == 2
    # the task names no real package, so the report is honestly matrix-free
    assert "matrix" not in stored["summary"]
    # and the records on disk were READ, not re-graded
    for i in range(2):
        r = json.loads((run / f"r{i}" / "rollout.json").read_text(
            encoding="utf-8"))
        assert "regraded_at" not in r and "regrade" not in r


def test_re_report_projects_an_old_run_under_a_new_matrix(tmp_path, monkeypatch):
    records = [rec("r0", {"H1b": True, "H3": True, "G1c": True}),
               rec("r1", {"H1b": False, "H3": True, "G1c": True},
                   classification="reasoning", passed=False)]
    run = _run_dir(tmp_path, records)
    monkeypatch.setattr(run_rollouts.taskspec, "load",
                        lambda *a, **k: StubSpec(MATRIX))
    assert run_rollouts.re_report(run, None, None) == 0
    stored = json.loads((run / "report.json").read_text(encoding="utf-8"))
    assert stored["summary"]["matrix"]["projected_pass"] == 1.0
    assert stored["summary"]["matrix"]["gate_cold"] == 1


def test_re_report_refuses_a_run_with_no_records(tmp_path):
    empty = tmp_path / "GT999-no-such-task" / "run-empty"
    empty.mkdir(parents=True)
    assert run_rollouts.re_report(empty, None, None) == 2
    assert not (empty / "report.json").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([str(Path(__file__).resolve()), "-q"]))
