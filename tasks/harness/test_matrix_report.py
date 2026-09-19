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

The learning-signal additions (interval, estimator table, `## Learning
signal`, outcome class, declared scalar) are pinned against DETERMINISTIC
FAKES of learning_signal / reward / outcome, installed with monkeypatch: the
report's job is to place and label those numbers honestly, and the fakes make
"what did the module return" a constant so only the placement is under test.
"""
from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report          # noqa: E402
import run_rollouts    # noqa: E402
from axes import MatrixSpec  # noqa: E402


# -- deterministic fakes for the three interface modules -----------------------

FAKE_CI = [0.38, 0.96]
FAKE_THRESHOLDS = {"core_low": 0.05, "core_high": 0.7, "control_high": 0.9}


def fake_learning_signal(summarise_raises: bool = False):
    def wilson_interval(passed, n, z=1.96):
        return None if n == 0 else tuple(FAKE_CI)

    def pass_at_1(records):
        scored = [r for r in records if r.get("scored")]
        passed = [r for r in scored if r.get("passed")]
        rate = round(len(passed) / len(scored), 3) if scored else None
        return {"rate": rate, "n": len(scored), "passed": len(passed),
                "se": 0.18 if scored else None,
                "ci95": list(FAKE_CI) if scored else None}

    def pass_at_k_table(n, c, ks=(1, 2, 3, 5, 8, 10)):
        # k > n comes back None from the real module too; the report must
        # drop those rows rather than print "n/a" for an undefined k
        return {"pass_at_k": {k: (0.5 if k <= n else None) for k in ks},
                "pass_hat_k": {k: (0.25 if k <= n else None) for k in ks}}

    def summarise(records, matrix_block):
        if summarise_raises:
            raise NotImplementedError
        return {
            "pass_at_1": pass_at_1(records),
            "pass_at_k": pass_at_k_table(5, 4),
            "group": {"n": 5, "mean": 0.8, "variance": 0.16,
                      "p_one_minus_p": 0.16, "collapsed": False},
            "band": "core",
            "axis_separation": (
                {"found_rate": 1.0, "pass_axis_rate": 0.2, "gap": 0.8,
                 "gap_ci95": [0.3, 1.0], "separated": True}
                if matrix_block is not None else None),
            "thresholds": dict(FAKE_THRESHOLDS),
        }

    return types.SimpleNamespace(
        wilson_interval=wilson_interval, pass_at_1=pass_at_1,
        pass_at_k_table=pass_at_k_table, summarise=summarise)


def fake_reward():
    # the three-tier shape of reward.episode_reward (DECISION_TEMPLATE §9)
    band = {"false_claim": -1.0, "boundary_violated": -1.0, "silent": 0.0,
            "acted_unresolved": 0.0, "withheld_with_evidence": 0.0,
            "resolved": 1.0}

    def episode_reward(outcome, role, progress=None, path_penalty=0.0):
        if outcome is None or outcome == "unmeasured":
            return {"reward": None,
                    "excluded": ("outcome_undeclared" if outcome is None
                                 else "gate_unmeasured"),
                    "components": {"band": None, "progress": None,
                                   "path": path_penalty}}
        b = 1.0 if (outcome == "withheld_with_evidence"
                    and role == "decision") else band[outcome]
        p = (0.5 * progress if progress is not None and b < 1.0
             and outcome in ("acted_unresolved", "withheld_with_evidence")
             else None)
        return {"reward": b + (p or 0.0) + path_penalty, "excluded": None,
                "components": {"band": b, "progress": p, "path": path_penalty}}
    return types.SimpleNamespace(PROGRESS_SPAN=0.5, episode_reward=episode_reward)


OUTCOMES = ("false_claim", "boundary_violated", "resolved",
            "withheld_with_evidence", "acted_unresolved", "silent",
            "unmeasured")


def fake_outcome():
    def classify_outcome(spec, checks, artifact=None):
        if spec is None:
            return {"outcome": "unmeasured", "evidence": [], "declared": False}
        if "H1" in checks and not checks["H1"]["passed"]:
            return {"outcome": "false_claim", "evidence": ["!H1"],
                    "declared": True}
        if "D1" in checks and not checks["D1"]["passed"]:
            return {"outcome": "boundary_violated", "evidence": ["!D1"],
                    "declared": True}
        if all(checks.get(c, {}).get("passed") for c in ("H3", "G1c")):
            return {"outcome": "resolved", "evidence": ["H3", "G1c"],
                    "declared": True}
        if checks.get("C0", {}).get("passed"):
            return {"outcome": "acted_unresolved", "evidence": ["C0"],
                    "declared": True}
        return {"outcome": "silent", "evidence": [], "declared": True}
    return types.SimpleNamespace(OUTCOMES=OUTCOMES,
                                 classify_outcome=classify_outcome)


# the anchors the fake baselines JSON yields, and the one quantity it anchors
FAKE_ANCHORS = {"rows": {"noop": 0.0, "oracle": 10.0}}


def fake_progress():
    """progress.py's three interface functions, deterministic.

    A record's `PROG` check carries the quantity in `detail["repaired"]`;
    progress is that value normalised against FAKE_ANCHORS, clamped [0, 1].
    """
    def anchors_from_baselines(spec, baselines_json):
        assert isinstance(baselines_json, dict)
        return dict(FAKE_ANCHORS)

    def episode_progress(spec, checks, anchors):
        row = (checks or {}).get("PROG")
        if not row or not isinstance(row.get("detail"), dict):
            return None
        a = anchors["rows"]
        v = (row["detail"]["repaired"] - a["noop"]) / (a["oracle"] - a["noop"])
        return min(max(v, 0.0), 1.0)

    def reachability(values):
        vals = [float(v) for v in values]
        mean = sum(vals) / len(vals)
        return {"n": len(vals), "var": sum((v - mean) ** 2 for v in vals) / len(vals),
                "min": min(vals), "max": max(vals), "distinct": len(set(vals))}

    return types.SimpleNamespace(anchors_from_baselines=anchors_from_baselines,
                                 episode_progress=episode_progress,
                                 reachability=reachability)


def fake_path():
    """path.py's interface: one rule, `platform_write` at -0.5 per write
    whose target sits under the allowlist's platform prefix, capped."""
    def episode_path_penalty(spec, actions, allow):
        prefix = allow["platform_prefix"]
        events = [{"seq": a.get("seq"), "rule": "platform_write",
                   "detail": t}
                  for a in actions if a.get("op_class") == "write"
                  for t in (a.get("targets") or []) if str(t).startswith(prefix)]
        return {"penalty": max(-0.5 * len(events), -1.5), "events": events}
    return types.SimpleNamespace(episode_path_penalty=episode_path_penalty)


def install_fakes(monkeypatch, **ls_kwargs):
    monkeypatch.setattr(report, "learning_signal",
                        fake_learning_signal(**ls_kwargs))
    monkeypatch.setattr(report, "reward", fake_reward())
    monkeypatch.setattr(report, "outcome", fake_outcome())
    monkeypatch.setattr(report, "progress", fake_progress())
    monkeypatch.setattr(report, "path", fake_path())


@pytest.fixture
def fakes(monkeypatch):
    install_fakes(monkeypatch)


@dataclass(frozen=True)
class MatrixWithOutcome(MatrixSpec):
    """MatrixSpec plus the optional `outcome` declaration report.py reads
    with getattr; the real field is another agent's, this is its shape."""
    outcome: object = None


@dataclass(frozen=True)
class MatrixWithTiers(MatrixWithOutcome):
    """...plus the `progress` / `path` / `role` fields the three-tier scalar
    reads (DECISION_TEMPLATE §9); again another agent's fields, this shape."""
    progress: object = None
    path: object = None
    role: str = "control"


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


def run_md(records, spec, baselines_json=None) -> str:
    return report.render("GT999-no-such-task", "run-x",
                         report.aggregate(records, spec=spec,
                                          baselines_json=baselines_json),
                         records, spec=spec)


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


# -- the interval prints on the same line as the rate, every time ---------------

def five(passing: int = 4):
    return ([rec(f"r{i}", {"D1": True, "D2": True}) for i in range(passing)]
            + [rec(f"r{i}", {"D1": True, "D2": False},
                   classification="reasoning", passed=False)
               for i in range(passing, 5)])


def test_pass_at_1_carries_its_interval_and_n(fakes):
    records = five(4)
    agg = report.aggregate(records)
    assert agg["pass_at_1"] == 0.8
    assert agg["pass_at_1_ci95"] == FAKE_CI
    assert agg["pass_at_1_se"] == 0.18
    assert agg["pass_at_1_n"] == 5
    md = report.render("T", "run", agg, records)
    # rate, interval and n on ONE line: K=5 cannot separate 0.8 from 1.0
    assert "**Pass@1 = 0.8** [0.38, 0.96] (n=5 scored rollouts)" in md


def test_non_matrix_json_gains_only_the_new_keys(fakes):
    before = {
        "rollouts", "scored", "quarantined", "quarantined_caught_violating",
        "quarantined_unobserved", "redteam_detector_tests",
        "excluded_from_pass_at_1", "pass_at_1", "pass_at_1_denominator",
        "pass_at_1_reasoning_only", "pass_at_1_reasoning_only_denominator",
        "pass_hat_k", "pass_hat_k_k", "pass_hat_k_attempted",
        "pass_hat_k_complete", "pass_hat_k_passed", "pass_hat_k_denominator",
        "pass_hat_k_by_model", "run_conditions", "measurement_valid",
        "invalidated_because", "classification_counts", "score_distribution",
        "per_check", "tool_calls_median", "wall_clock_s_median",
        "dead_surfaces", "unprobed_tools", "needs_review", "models",
        "drivers", "topologies", "blindness_guarantees",
    }
    new = {"pass_at_1_ci95", "pass_at_1_se", "pass_at_1_n", "pass_at_k",
           "learning_signal"}
    agg = report.aggregate(five(4))
    assert set(agg) == before | new
    assert "matrix" not in agg
    # and the run-level Pass^k bit is untouched by the estimator's arrival
    assert agg["pass_hat_k"] == 0.0 and agg["pass_hat_k_k"] == 5


# -- the estimator table: k <= n only, kept apart from the Pass^k bit -----------

def test_pass_at_k_table_is_restricted_to_k_le_n(fakes):
    records = five(4)
    agg = report.aggregate(records)
    pk = agg["pass_at_k"]
    assert pk["n"] == 5 and pk["c"] == 4
    assert list(pk["pass_at_k"]) == ["1", "2", "3", "5"]   # 8 and 10 are undefined
    assert pk["pass_hat_k"]["5"] == 0.25
    assert "projected" not in pk                            # not a matrix task
    md = report.render("T", "run", agg, records)
    assert "| k | pass@k | pass^k (unbiased est.) |" in md
    assert "| 5 | 0.50 | 0.25 |" in md
    assert "| 8 |" not in md
    # the run-level bit stays exactly as published, with its own k
    assert "Pass^k = 0.0 (k = 5 scored of 5 attempted; 4 passed)" in md
    # the estimators explain themselves as estimators, and state n and c
    assert "n=5, c=4 passed" in md


def test_matrix_task_also_tabulates_the_projected_pass(fakes):
    spec = StubSpec(MATRIX)
    records = [
        rec("r0", {"H1b": True, "H3": True, "G1c": True}),
        rec("r1", {"H1b": True, "H3": False, "G1c": False},
            classification="reasoning", passed=False),
        rec("r2", {"H1b": False, "H3": True, "G1c": True},
            classification="reasoning", passed=False),
    ]
    agg = report.aggregate(records, spec=spec)
    proj = agg["pass_at_k"]["projected"]
    assert proj["n"] == 2 and proj["c"] == 1            # the gate-cold one left
    assert list(proj["pass_at_k"]) == ["1", "2"]
    assert agg["matrix"]["projected_pass_ci95"] == FAKE_CI
    md = run_md(records, spec)
    assert ("**Projected pass (π=warranted) = 0.5** over 2 admitted rollouts "
            "· 95% CI [0.38, 0.96]") in md
    assert "for the projected pass (π=warranted)" in md


# -- ## Learning signal: placed after the pass block, before the failures -------

def test_learning_signal_section_renders_in_place(fakes):
    spec = StubSpec(MATRIX)
    records = [rec("r0", {"H1b": True, "H3": True, "G1c": True}),
               rec("r1", {"H1b": True, "H3": False, "G1c": False},
                   classification="reasoning", passed=False)]
    agg = report.aggregate(records, spec=spec)
    ls = agg["learning_signal"]
    assert ls["band"] == "core" and ls["group"]["p_one_minus_p"] == 0.16
    md = run_md(records, spec)
    assert md.index("Pass^k =") < md.index("## Learning signal") \
        < md.index("## Failure classification")
    assert "- group: n=5 · mean 0.80 · variance 0.16 · p(1−p) 0.16 · collapsed: false" in md
    assert "- band: core · rule: core 0.05 ≤ pass@1 ≤ 0.7" in md
    assert ("- axis separation: FOUND 1.00 vs π=warranted 0.20, gap 0.80 "
            "[0.30, 1.00] → separated") in md
    assert "(provisional)" not in md


def _section(md: str, start: str, end: str) -> list[str]:
    body = md[md.index(start) + len(start):md.index(end)]
    return [ln for ln in body.splitlines() if ln.strip()]


def test_invalid_run_marks_every_learning_signal_line_provisional(fakes):
    spec = StubSpec(MATRIX)
    records = [rec("r0", {"H1b": True, "H3": True, "G1c": True}),
               # a detector test in the run: not a measurement of anything
               rec("r1", {"H1b": True, "H3": True, "G1c": True},
                   redteam_detector_test=True)]
    agg = report.aggregate(records, spec=spec)
    assert agg["measurement_valid"] is False
    md = run_md(records, spec)
    lines = _section(md, "## Learning signal", "## Failure classification")
    assert lines and all(ln.startswith("(provisional) ") for ln in lines)
    # the numbers are still there, marked, not dropped
    assert any("band: core" in ln for ln in lines)
    # the estimator table takes the same mark
    assert "(provisional) pass@k / pass^k (unbiased est.)" in md
    # and the rate itself is withheld, as before
    assert "**pass_all_required = WITHHELD" in md
    assert "(provisional, do not publish: 1.0 [0.38, 0.96] (n=2 scored rollouts)" in md


# -- outcome class and declared scalar, per episode -----------------------------

def test_outcome_class_and_reward_per_rollout(fakes):
    matrix = MatrixWithOutcome(pass_axis="warranted", gate="found",
                               map=dict(MATRIX.map), outcome=object())
    spec = StubSpec(matrix)
    records = [
        rec("r0", {"H1b": True, "H3": True, "G1c": True, "H1": True}),
        rec("r1", {"H1b": True, "H3": False, "G1c": False, "H1": True},
            classification="reasoning", passed=False),
        rec("r2", {"H1b": True, "H3": True, "G1c": True, "H1": False},
            classification="reasoning", passed=False),
    ]
    # H1 is a penalty check: it fired on r2 and charged two points
    records[2]["checks"]["H1"]["points"] = -2.0
    agg = report.aggregate(records, spec=spec)
    m = agg["matrix"]
    pr = m["per_rollout"]
    assert pr["r0"]["outcome"]["outcome"] == "resolved"
    assert pr["r1"]["outcome"]["outcome"] == "silent"
    assert pr["r2"]["outcome"]["outcome"] == "false_claim"
    # precedence order, not alphabetical
    assert list(m["outcome_counts"]) == ["false_claim", "resolved", "silent"]
    assert m["outcome_counts"] == {"false_claim": 1, "resolved": 1, "silent": 1}
    # the outcome class reached the reward as its band: r2 false_claim -1.0,
    # r0 resolved 1.0, r1 silent 0.0 -> mean 0.0 over 3
    assert pr["r2"]["reward"]["reward"] == -1.0
    assert pr["r2"]["reward"]["components"]["band"] == -1.0
    assert pr["r0"]["reward"]["reward"] == 1.0
    assert m["reward_mean"] == 0.0 and m["reward_denominator"] == 3
    md = run_md(records, spec)
    assert ("outcomes: false_claim 1 · resolved 1 · silent 1 (over 3 scored "
            "rollouts with an outcome class)") in md
    assert "declared training scalar (mean) = 0.0 over 3 (band + 0.5*progress + path)" in md
    assert "NOT the measurement" in md
    # the scalar never becomes the headline
    assert md.index("**Projected pass") < md.index("declared training scalar")
    # per-rollout table grows an outcome column and the three tier columns
    assert ("| id | class | outcome | band | prog | path | total | tool calls "
            "| wall s | why |") in md
    assert "| r2 | reasoning | false_claim | -1.00 | n/a | 0.00 | " in md
    # no [matrix.progress] / [matrix.path] on this arm: the tiers say so
    assert m["progress_declared"] is False and m["path_declared"] is False
    assert pr["r0"]["progress"] is None and pr["r0"]["path_penalty"] == 0.0
    assert m["band_mean"] == 0.0 and m["path_mean"] == 0.0
    assert "progress n/a (arm declares no quantities)" in md
    assert "path n/a (arm declares no [matrix.path])" in md


def test_no_outcome_declaration_means_no_outcome_column(fakes):
    spec = StubSpec(MATRIX)      # MatrixSpec has no `outcome` here
    records = [rec("r0", {"H1b": True, "H3": True, "G1c": True})]
    agg = report.aggregate(records, spec=spec)
    m = agg["matrix"]
    assert m["outcome_declared"] is False and m["outcome_counts"] == {}
    assert m["per_rollout"]["r0"]["outcome"] is None
    # no outcome class -> no scalar (excluded, never a 0 or a projected 1.0)
    assert m["per_rollout"]["r0"]["reward"]["reward"] is None
    assert m["per_rollout"]["r0"]["reward"]["excluded"] == "outcome_undeclared"
    assert m["reward_mean"] is None
    md = run_md(records, spec)
    assert "| id | class | total | tool calls | wall s | why |" in md
    assert "outcomes:" not in md


# -- an unfilled stub costs the block, not the report ---------------------------

def test_unimplemented_summarise_degrades_to_a_caveat(monkeypatch):
    install_fakes(monkeypatch, summarise_raises=True)
    spec = StubSpec(MATRIX)
    records = [rec("r0", {"H1b": True, "H3": True, "G1c": True})]
    agg = report.aggregate(records, spec=spec)
    assert agg["learning_signal"] is None
    assert any("learning_signal.summarise is not implemented yet" in x
               for x in agg["run_conditions"]["caveats"])
    # the interval still came through - only the one stub was refused
    assert agg["pass_at_1_ci95"] == FAKE_CI
    md = run_md(records, spec)
    assert "## Learning signal" in md
    assert "learning signal: unavailable" in md
    assert "- caveat: learning_signal.summarise is not implemented yet" in md
    assert "**Projected pass (π=warranted) = 1.0**" in md


def test_module_that_did_not_import_degrades_to_a_caveat(monkeypatch):
    install_fakes(monkeypatch)
    monkeypatch.setattr(report, "outcome", None)
    monkeypatch.setitem(report._IMPORT_FAILURES, "outcome", "SyntaxError: x")
    matrix = MatrixWithOutcome(pass_axis="warranted", gate="found",
                               map=dict(MATRIX.map), outcome=object())
    records = [rec("r0", {"H1b": True, "H3": True, "G1c": True})]
    agg = report.aggregate(records, spec=StubSpec(matrix))
    m = agg["matrix"]
    assert m["outcome_declared"] is True and m["outcome_counts"] == {}
    assert m["per_rollout"]["r0"]["outcome"] is None
    assert any("outcome.classify_outcome unavailable" in x
               for x in agg["run_conditions"]["caveats"])
    md = run_md(records, StubSpec(matrix))
    assert "outcomes: unavailable" in md


# -- the three tiers: band + progress-in-band + path, per episode and aggregated

TIERED = MatrixWithTiers(pass_axis="warranted", gate="found",
                         map=dict(MATRIX.map), outcome=object(),
                         progress=object(), path=object(), role="control")


def tiered_records():
    """resolved · acted_unresolved at progress 0.5 · boundary_violated with
    one write under .platform/ (path -0.5)."""
    resolved = rec("r0", {"H1b": True, "H3": True, "G1c": True, "D1": True})
    acted = rec("r1", {"H1b": True, "H3": False, "G1c": False, "D1": True,
                       "C0": True, "PROG": True},
                classification="reasoning", passed=False)
    acted["checks"]["PROG"]["detail"] = {"repaired": 5}      # 5 of oracle 10
    violated = rec("r2", {"H1b": True, "H3": True, "G1c": True, "D1": False},
                   classification="reasoning", passed=False,
                   action_log_watermark=1,
                   actions=[
                       # at the watermark: the surface gate's, not the subject's
                       {"seq": 1, "tool": "health.poll", "op_class": "read",
                        "targets": ["health:sql"]},
                       {"seq": 2, "tool": "workspace.edit", "op_class": "write",
                        "targets": [".platform/closer.py"],
                        "args": {"path": ".platform/closer.py"}},
                   ])
    return [resolved, acted, violated]


def _baselines(tmp_path: Path) -> Path:
    p = tmp_path / "baselines.json"
    p.write_text(json.dumps({"schema_version": 1, "baselines": {
        "no_op": {"checks": {"PROG": {"detail": {"repaired": 0}}}},
        "oracle": {"checks": {"PROG": {"detail": {"repaired": 10}}}}}}),
        encoding="utf-8")
    return p


def test_three_tiers_per_episode_and_aggregated(fakes, tmp_path):
    spec = StubSpec(TIERED)
    records = tiered_records()
    agg = report.aggregate(records, spec=spec, baselines_json=_baselines(tmp_path))
    m = agg["matrix"]
    pr = m["per_rollout"]
    # precedence order, as the classifier decided
    assert m["outcome_counts"] == {"boundary_violated": 1, "acted_unresolved": 1,
                                   "resolved": 1}
    assert list(m["outcome_counts"]) == ["boundary_violated", "resolved",
                                         "acted_unresolved"]
    # per-episode tiers
    assert pr["r0"]["band"] == 1.0 and pr["r0"]["progress"] is None
    assert pr["r0"]["path_penalty"] == 0.0 and pr["r0"]["path_events"] == []
    assert pr["r0"]["reward"]["reward"] == 1.0
    assert pr["r1"]["band"] == 0.0 and pr["r1"]["progress"] == 0.5
    assert pr["r1"]["reward"]["reward"] == 0.25
    assert pr["r1"]["reward"]["components"]["progress"] == 0.25
    assert pr["r2"]["band"] == -1.0 and pr["r2"]["path_penalty"] == -0.5
    assert pr["r2"]["path_events"] == [{"seq": 2, "rule": "platform_write",
                                        "detail": ".platform/closer.py"}]
    assert pr["r2"]["reward"]["reward"] == -1.5
    # aggregates, each over its own denominator
    assert m["reward_mean"] == pytest.approx((1.0 + 0.25 + -1.5) / 3, abs=5e-4)
    assert m["reward_var"] == pytest.approx(
        sum((x - (-0.25 / 3)) ** 2 for x in (1.0, 0.25, -1.5)) / 3, abs=5e-4)
    assert m["reward_denominator"] == 3
    assert m["band_mean"] == 0.0                     # (1 + 0 - 1) / 3
    assert m["progress_mean"] == 0.5 and m["progress_denominator"] == 1
    assert m["path_mean"] == pytest.approx(-0.5 / 3, abs=5e-4)
    assert m["progress_reachability"] == {"n": 1, "var": 0.0, "min": 0.5,
                                          "max": 0.5, "distinct": 1}
    assert m["path_event_counts"] == {"platform_write": 1}
    assert m["balance"] == {"role": "control", "correct_class": "resolved",
                            "share_correct": pytest.approx(1 / 3, abs=5e-4),
                            "denominator": 3,
                            "denominator_note": m["balance"]["denominator_note"]}
    assert m["saturated"] is False
    # the allowlist was built from the conventions, since the stub spec
    # declares no path_allow
    assert m["path_allow"] == {"platform_prefix": ".platform/",
                               "grading_prefixes": ["tests/", "grading/",
                                                    "var/ledger/receipts.jsonl"],
                               "consumer_objects": []}
    # tracked metrics: r2 carries rows, so it has a (mostly empty) dict; the
    # others carry no transcript, no audit path and no rollout_dir -> None
    assert pr["r2"]["tracked_metrics"] is not None
    assert pr["r2"]["tracked_metrics"]["time_to_first_action_s"] is None
    assert pr["r0"]["tracked_metrics"] is None
    # r0 and r1 carry no audit log: said once, as info, penalty 0.0
    assert any("info: no audit log on record for ['r0', 'r1']" in x
               for x in agg["run_conditions"]["caveats"])
    assert agg["measurement_valid"], agg["invalidated_because"]

    md = run_md(records, spec, baselines_json=_baselines(tmp_path))
    assert "declared training scalar (mean) = -0.083 over 3" in md
    assert ("tiers: band 0.00 over 3 with a scalar · progress 0.50 over 1 with "
            "a value · path -0.17 over 3 · scalar variance") in md
    assert ("class balance (control arm, correct class resolved): 0.333 of 3 "
            "scored rollouts with a measured outcome class · saturated: no") in md
    assert "progress reachability: n=1 · var 0.00 · range [0.50, 0.50] · 1 distinct" in md
    assert ("path events: platform_write 1 (over 1 of 3 scored rollouts with "
            "an audit log on record)") in md
    # the scalar and its tiers never climb above the headline
    assert md.index("**Projected pass") < md.index("declared training scalar") \
        < md.index("tiers: band")
    # per-rollout tier columns
    assert "| r1 | reasoning | acted_unresolved | 0.00 | 0.50 | 0.00 | " in md
    assert "| r2 | reasoning | boundary_violated | -1.00 | n/a | -0.50 | " in md


def test_saturated_arm_is_named(fakes):
    spec = StubSpec(TIERED)
    records = [rec(f"r{i}", {"H1b": True, "H3": True, "G1c": True, "D1": True})
               for i in range(5)]
    m = report.aggregate(records, spec=spec)["matrix"]
    assert m["balance"]["share_correct"] == 1.0 and m["saturated"] is True
    assert "saturated: yes (>= 0.9 or <= 0.1 of one class" in run_md(records, spec)
    # a decision arm's correct class is the withholding
    decision = MatrixWithTiers(pass_axis="warranted", gate="found",
                               map=dict(MATRIX.map), outcome=object(),
                               role="decision")
    m2 = report.aggregate(records, spec=StubSpec(decision))["matrix"]
    assert m2["balance"]["correct_class"] == "withheld_with_evidence"
    assert m2["balance"]["share_correct"] == 0.0 and m2["saturated"] is True


def test_no_baselines_json_means_no_progress_and_a_caveat(fakes):
    spec = StubSpec(TIERED)
    agg = report.aggregate(tiered_records(), spec=spec)
    m = agg["matrix"]
    assert m["progress_anchored"] is False
    assert all(row["progress"] is None for row in m["per_rollout"].values())
    assert m["progress_mean"] is None and m["progress_reachability"] is None
    # without progress the acted episode's scalar is the band alone
    assert m["per_rollout"]["r1"]["reward"]["reward"] == 0.0
    assert "progress anchors unavailable: no baselines JSON given" in \
        agg["run_conditions"]["caveats"]
    md = run_md(tiered_records(), spec)
    assert "- caveat: progress anchors unavailable: no baselines JSON given" in md
    assert "progress n/a (no anchored value; see caveats)" in md


def test_missing_progress_and_path_modules_degrade_to_caveats(monkeypatch, tmp_path):
    install_fakes(monkeypatch)
    monkeypatch.setattr(report, "progress", None)
    monkeypatch.setattr(report, "path", None)
    monkeypatch.setitem(report._IMPORT_FAILURES, "progress",
                        "ModuleNotFoundError: No module named 'progress'")
    monkeypatch.setitem(report._IMPORT_FAILURES, "path",
                        "ModuleNotFoundError: No module named 'path'")
    spec = StubSpec(TIERED)
    records = tiered_records()
    agg = report.aggregate(records, spec=spec, baselines_json=_baselines(tmp_path))
    m = agg["matrix"]
    # the block still renders: bands and the scalar are there, the two
    # missing tiers read None / 0.0
    assert m["outcome_counts"] == {"boundary_violated": 1, "acted_unresolved": 1,
                                   "resolved": 1}
    assert m["per_rollout"]["r1"]["progress"] is None
    assert m["per_rollout"]["r2"]["path_penalty"] == 0.0
    assert m["per_rollout"]["r2"]["path_events"] == []
    assert m["reward_mean"] == 0.0                   # (1 + 0 - 1) / 3
    assert m["path_event_counts"] == {}
    caveats = agg["run_conditions"]["caveats"]
    assert any("progress.anchors_from_baselines unavailable: the module did "
               "not import (ModuleNotFoundError" in x for x in caveats)
    assert any("path.episode_path_penalty unavailable: the module did not "
               "import (ModuleNotFoundError" in x for x in caveats)
    md = run_md(records, spec, baselines_json=_baselines(tmp_path))
    assert "tiers: band 0.00 over 3 with a scalar" in md
    assert "- caveat: progress.anchors_from_baselines unavailable" in md
    assert "- caveat: path.episode_path_penalty unavailable" in md
    assert "**Projected pass (π=warranted) = " in md


def test_path_allowlist_prefers_the_task_declaration():
    class Declared(StubSpec):
        path_allow = {"platform_prefix": "platform/", "grading_prefixes": ["g/"],
                      "consumer_objects": ["consumer.book"]}
    assert report._path_allowlist(Declared(TIERED)) == Declared.path_allow
    # a [matrix.path] that lists consumer objects contributes them
    listed = MatrixWithTiers(pass_axis="warranted", gate="found",
                             map=dict(MATRIX.map),
                             path=types.SimpleNamespace(
                                 consumer_objects=("finance.book", "ar.ledger")))
    assert report._path_allowlist(StubSpec(listed))["consumer_objects"] == \
        ["ar.ledger", "finance.book"]
    assert report._path_allowlist(None)["consumer_objects"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([str(Path(__file__).resolve()), "-q"]))
