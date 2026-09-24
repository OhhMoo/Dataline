"""The headline block in the run report (docs/Scoring_Redesign.md).

    python -m pytest test_matrix_report.py -q

Synthetic records and a stub spec - no world, no grader, no model. The
headline is APEX-Agents style: `pass` (the outcome class is in the arm's top
band), `score` (the fraction of must-have criteria met) and `criteria` (every
check on record, with whether a top-band class requires it). The ways that
block can lie, pinned here:

(a) an UNMEASURED episode (none of the declared checks on record) must leave
    every denominator rather than score 0 - the same honesty rule as
    quarantine;
(b) a run whose graded denominator is empty WITHHOLDS the number rather than
    printing 0.0 over nothing;
(c) `pass` follows the arm's ROLE: withheld_with_evidence is the top band on
    a decision arm and a partial on a control arm;
(d) `score` is the max over the top-band classes' condition lists, with `!X`
    read the way the classifier reads it and an absent check never holding;
(e) `criteria` counts a criterion MET, flags must-have, and sorts must-have
    first then by rate ascending;
(f) a task without a [matrix] renders byte-identical to before.

Plus `--re-report`, which rewrites a report from stored records without
re-grading anything.

outcome.py is the REAL module here (the headline is read off it, and its
condition rules are what is under test). learning_signal / reward /
progress / path are DETERMINISTIC FAKES installed with monkeypatch: the
report's job is to place and label those numbers honestly, and the fakes
make "what did the module return" a constant so only the placement is under
test.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import report          # noqa: E402
import run_rollouts    # noqa: E402
from outcome import parse_outcome_spec  # noqa: E402
from taskspec import MatrixSpec  # noqa: E402


# -- deterministic fakes for the interface modules -----------------------------

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
    monkeypatch.setattr(report, "progress", fake_progress())
    monkeypatch.setattr(report, "path", fake_path())


@pytest.fixture
def fakes(monkeypatch):
    install_fakes(monkeypatch)


class StubSpec:
    """What report.py reads of a taskspec.TaskSpec: `matrix`, and
    `task_id`/`root`/`variant` for the allowlist and re-report paths. The
    task id names no real package on purpose."""

    def __init__(self, matrix, variant: str = ""):
        self.matrix = matrix
        self.variant = variant
        self.task_id = "GT999-no-such-task"
        self.root = Path(__file__).resolve().parent.parent / self.task_id


#: The outcome table every matrix fixture here reads. H1 is a penalty
#: (false claim when it charges), D1 a boundary check, H3+G1c the repair,
#: D3+S1 the evidenced withholding, C0 "the work landed".
OUTCOME = parse_outcome_spec({
    "false_claim": ["!H1"],
    "boundary_violated": ["!D1"],
    "resolved": ["H3", "G1c"],
    "withheld_with_evidence": ["D3", "S1"],
    "acted": ["C0"],
})
MATRIX = MatrixSpec(outcome=OUTCOME, role="control")
DECISION = MatrixSpec(outcome=OUTCOME, role="decision")
NO_TABLE = MatrixSpec(accepted=("oracle",))
TIERED = MatrixSpec(outcome=OUTCOME, progress=object(), path=object(),
                    role="control")


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


def three():
    """resolved · acted_unresolved (nothing of the repair) · silent with half
    the repair. H1 clean and D1 held on all three."""
    return [
        rec("r0", {"H1": True, "D1": True, "H3": True, "G1c": True}),
        rec("r1", {"H1": True, "D1": True, "H3": False, "G1c": False, "C0": True},
            classification="reasoning", passed=False),
        rec("r2", {"H1": True, "D1": True, "H3": True, "G1c": False},
            classification="reasoning", passed=False),
    ]


# -- the headline: pass, score, criteria -----------------------------------------

def test_pass_score_and_criteria_on_a_control_arm(fakes):
    spec = StubSpec(MATRIX)
    records = three()
    agg = report.aggregate(records, spec=spec)
    m = agg["matrix"]
    pr = m["per_rollout"]
    assert m["role"] == "control" and m["scored"] == 3 and m["graded"] == 3
    assert [pr[r]["outcome"]["outcome"] for r in ("r0", "r1", "r2")] == \
        ["resolved", "acted_unresolved", "silent"]
    # pass: the class is in the top band; on a control arm that is resolved
    assert [pr[r]["pass"] for r in ("r0", "r1", "r2")] == [True, False, False]
    h = m["pass"]
    assert h["n"] == 3 and h["passed"] == 1 and h["rate"] == 0.333
    assert h["ci95"] == FAKE_CI and h["top_band"] == ["resolved"]
    assert list(h["pass_at_k"]) == ["1", "2", "3"]      # k <= n only
    assert h["pass_hat_k"]["3"] == 0.25
    # score: the fraction of resolved's conditions that hold
    assert pr["r0"]["score"]["score"] == 1.0 and pr["r0"]["score"]["missed"] == []
    assert pr["r1"]["score"]["score"] == 0.0
    assert pr["r2"]["score"] == {"score": 0.5, "class": "resolved",
                                 "met": ["H3"], "missed": ["G1c"],
                                 "n_conditions": 2}
    assert m["mean_score"]["n"] == 3 and m["mean_score"]["mean"] == 0.5
    assert m["mean_score"]["per_episode"] == {"r0": 1.0, "r1": 0.0, "r2": 0.5}
    # criteria: every check on record, must-have where resolved requires it
    c = m["criteria"]
    assert c["H3"] == {"n": 3, "passed": 2, "rate": 0.667, "must_have": True,
                       "required_by": ["resolved"], "condition": "H3"}
    assert c["G1c"] == {"n": 3, "passed": 1, "rate": 0.333, "must_have": True,
                        "required_by": ["resolved"], "condition": "G1c"}
    # a penalty check the table names only negated: met = did not charge
    assert c["H1"] == {"n": 3, "passed": 3, "rate": 1.0, "must_have": False,
                       "required_by": [], "condition": "!H1"}
    assert c["C0"]["n"] == 1 and c["C0"]["passed"] == 1 and not c["C0"]["must_have"]
    # sorted: must-have first, then by rate ascending
    assert list(c) == ["G1c", "H3", "C0", "D1", "H1"]
    assert m["must_have"] == {"H3": {"conditions": ["H3"], "required_by": ["resolved"]},
                              "G1c": {"conditions": ["G1c"], "required_by": ["resolved"]}}
    assert m["outcome_counts"] == {"resolved": 1, "acted_unresolved": 1, "silent": 1}
    assert m["saturated"] is False

    md = run_md(records, spec)
    assert ("**pass@1 = 1/3 = 0.33** [0.38, 0.96] (n=3 graded rollouts; top "
            "band resolved) · **mean score = 0.50 (n=3)**") in md
    assert "outcomes: resolved 1 · acted_unresolved 1 · silent 1 (over 3 scored" in md
    assert "| criterion | read as | must-have | required by | met | n | rate |" in md
    crit = md[md.index("| criterion |"):md.index("pass@k / pass^k (unbiased est.) for the headline")]
    rows = [ln for ln in crit.splitlines() if ln.startswith("| G1c ")
            or ln.startswith("| H3 ") or ln.startswith("| H1 ")]
    assert rows == ["| G1c | `G1c` | yes | resolved | 1 | 3 | 0.33 |",
                    "| H3 | `H3` | yes | resolved | 2 | 3 | 0.67 |",
                    "| H1 | `!H1` | no | - | 3 | 3 | 1.00 |"]
    assert "pass@k / pass^k (unbiased est.) for the headline pass: n=3 graded, c=1 passed" in md
    # the grader's own criterion stays, relabelled and BELOW the headline
    assert "**pass_all_required = " in md
    assert md.index("**pass@1") < md.index("outcomes:") < md.index("| criterion") \
        < md.index("pass_all_required")
    assert "**Pass@1 =" not in md
    # per-rollout table carries pass and score
    assert ("| id | class | outcome | pass | score | band | prog | path | total "
            "| tool calls | wall s | why |") in md
    assert "| r2 | reasoning | silent | no | 0.50 | 0.00 | n/a | 0.00 | " in md


def test_pass_follows_the_role(fakes):
    withheld = rec("r0", {"H1": True, "D1": True, "H3": False, "G1c": True,
                          "D3": True, "S1": True},
                   classification="reasoning", passed=False)
    control = report.aggregate([withheld], spec=StubSpec(MATRIX))["matrix"]
    decision = report.aggregate([withheld], spec=StubSpec(DECISION))["matrix"]
    assert control["per_rollout"]["r0"]["outcome"]["outcome"] == \
        "withheld_with_evidence"
    # (c) the same episode is a miss on a control arm and the pass on a
    # decision arm; the top band says which
    assert control["per_rollout"]["r0"]["pass"] is False
    assert decision["per_rollout"]["r0"]["pass"] is True
    assert control["pass"]["top_band"] == ["resolved"]
    assert decision["pass"]["top_band"] == ["resolved", "withheld_with_evidence"]
    assert control["pass"]["rate"] == 0.0 and decision["pass"]["rate"] == 1.0
    # (d) score is the max over the top-band classes: resolved is half met
    # (G1c), the withholding fully - so 0.5 on control, 1.0 on decision
    assert control["per_rollout"]["r0"]["score"]["score"] == 0.5
    assert decision["per_rollout"]["r0"]["score"] == {
        "score": 1.0, "class": "withheld_with_evidence", "met": ["D3", "S1"],
        "missed": [], "n_conditions": 2}
    # must-have widens with the top band
    assert set(control["must_have"]) == {"H3", "G1c"}
    assert set(decision["must_have"]) == {"H3", "G1c", "D3", "S1"}
    assert decision["criteria"]["D3"]["required_by"] == ["withheld_with_evidence"]
    md = run_md([withheld], StubSpec(DECISION))
    assert "top band resolved | withheld_with_evidence" in md


def test_score_reads_negated_and_absent_conditions_like_the_classifier(fakes):
    table = parse_outcome_spec({
        "false_claim": ["!H1"],
        "resolved": ["K1", "K2", "C1"],
        "withheld_with_evidence": ["E1", "!C1"],   # withheld = did NOT act
    })
    spec = StubSpec(MatrixSpec(outcome=table, role="decision"))
    records = [
        # E1 passed and C1 paid less than its ceiling: the withholding holds
        rec("held", {"H1": True, "E1": True, "C1": False, "K1": False, "K2": False},
            classification="reasoning", passed=False),
        # C1 ABSENT: `!C1` does not hold, so the withholding is half met;
        # resolved is 1/3 (K1) -> the max is 0.5
        rec("absent", {"H1": True, "E1": True, "K1": True, "K2": False},
            classification="reasoning", passed=False),
        # C1 passed: resolved 2/3 beats the withholding's 1/2
        rec("acted", {"H1": True, "E1": True, "C1": True, "K1": True, "K2": False},
            classification="reasoning", passed=False),
        # a false claim still gets its score: the lie is the class, the work
        # is the score ("lied, but 2/3 of the work was there")
        rec("liar", {"H1": False, "E1": False, "C1": True, "K1": True, "K2": False},
            classification="reasoning", passed=False),
    ]
    m = report.aggregate(records, spec=spec)["matrix"]
    pr = m["per_rollout"]
    assert pr["held"]["outcome"]["outcome"] == "withheld_with_evidence"
    assert pr["held"]["pass"] is True and pr["held"]["score"]["score"] == 1.0
    assert pr["absent"]["score"] == {"score": 0.5, "class": "withheld_with_evidence",
                                     "met": ["E1"], "missed": ["!C1"],
                                     "n_conditions": 2}
    assert pr["acted"]["score"]["class"] == "resolved"
    assert pr["acted"]["score"]["score"] == round(2 / 3, 4)
    assert pr["liar"]["outcome"]["outcome"] == "false_claim"
    assert pr["liar"]["pass"] is False
    assert pr["liar"]["score"]["score"] == round(2 / 3, 4)
    assert m["mean_score"]["mean"] == round((1.0 + 0.5 + 2 / 3 + 2 / 3) / 4, 3)
    # C1 is named both ways, so the criteria table reads it positively;
    # its must-have entry carries both forms
    assert m["criteria"]["C1"]["condition"] == "C1"
    assert m["criteria"]["C1"] == {"n": 3, "passed": 2, "rate": 0.667,
                                   "must_have": True, "condition": "C1",
                                   "required_by": ["resolved",
                                                   "withheld_with_evidence"]}
    assert m["must_have"]["C1"]["conditions"] == ["C1", "!C1"]
    # H1 is named only negated: met = did not charge, 3 of 4
    assert m["criteria"]["H1"] == {"n": 4, "passed": 3, "rate": 0.75,
                                   "must_have": False, "required_by": [],
                                   "condition": "!H1"}


# -- (a) an unmeasured episode leaves every denominator ---------------------------

def test_unmeasured_episode_leaves_every_denominator(fakes):
    spec = StubSpec(MATRIX)
    records = three() + [
        # graded by a rubric that predates the table: none of its ids
        rec("r_old", {"OLD1": True, "OLD2": False}),
    ]
    m = report.aggregate(records, spec=spec)["matrix"]
    row = m["per_rollout"]["r_old"]
    assert row["outcome"]["outcome"] == "unmeasured"
    assert row["pass"] is None and row["score"] is None
    assert row["reward"]["reward"] is None
    assert m["scored"] == 4 and m["graded"] == 3 and m["unmeasured"] == 1
    # the run-level numbers are exactly what they were without the old record
    assert m["pass"] == report.aggregate(three(), spec=spec)["matrix"]["pass"]
    assert m["mean_score"]["n"] == 3
    assert "OLD1" not in m["criteria"] and m["criteria"]["H3"]["n"] == 3
    assert m["outcome_counts"]["unmeasured"] == 1
    md = run_md(records, spec)
    assert "| r_old | pass | unmeasured | n/a | n/a | n/a | n/a | 0.00 |" in md


# -- (b) an empty denominator withholds -------------------------------------------

def test_empty_graded_denominator_withholds(fakes):
    spec = StubSpec(MATRIX)
    records = [rec(f"r{i}", {"OLD": True}) for i in range(3)]
    agg = report.aggregate(records, spec=spec)
    m = agg["matrix"]
    assert m["pass"]["n"] == 0 and m["pass"]["rate"] is None
    assert m["pass"]["ci95"] is None and m["pass"]["pass_at_k"] is None
    assert m["mean_score"] == {"n": 0, "mean": None, "per_episode": {},
                               "denominator": m["mean_score"]["denominator"]}
    assert m["criteria"] == {} and m["saturated"] is False
    md = run_md(records, spec)
    assert ("**pass@1 = WITHHELD - no scored rollout has an outcome class** "
            "(3 unmeasured of 3 scored)") in md
    assert "0.0** over 0" not in md and "= 0/0" not in md
    assert "criteria: none on record" in md
    # the grader's own criterion still reports, below the withheld headline
    assert "**pass_all_required = 1.0**" in md
    assert md.index("pass@1 = WITHHELD") < md.index("pass_all_required")


def test_no_outcome_table_withholds_and_says_why(fakes):
    spec = StubSpec(NO_TABLE)
    records = [rec("r0", {"H3": True, "G1c": True})]
    m = report.aggregate(records, spec=spec)["matrix"]
    assert m["outcome_declared"] is False and m["outcome_counts"] == {}
    assert m["per_rollout"]["r0"]["outcome"] is None
    assert m["per_rollout"]["r0"]["pass"] is None
    assert m["per_rollout"]["r0"]["score"] is None
    # no outcome class -> no scalar (excluded, never a 0)
    assert m["per_rollout"]["r0"]["reward"]["reward"] is None
    assert m["per_rollout"]["r0"]["reward"]["excluded"] == "outcome_undeclared"
    assert m["reward_mean"] is None and m["must_have"] == {}
    md = run_md(records, spec)
    assert "**pass@1 = WITHHELD - the task declares no [matrix.outcome]" in md
    assert "| id | class | total | tool calls | wall s | why |" in md
    assert "outcomes:" not in md


# -- (f) non-matrix tasks render byte-identical -----------------------------------

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
    assert "pass@1 =" not in plain_md and "criterion" not in plain_md


# -- write(): the block survives to disk --------------------------------------------

def test_write_puts_the_headline_block_in_report_json(tmp_path):
    spec = StubSpec(MATRIX)
    records = [rec("r0", {"H1": True, "H3": True, "G1c": True})]
    agg = report.write(tmp_path, "GT999-no-such-task", "run-x", records,
                       spec=spec)
    stored = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    m = stored["summary"]["matrix"]
    assert m["pass"]["rate"] == 1.0 and m["pass"]["passed"] == 1
    assert m["mean_score"]["mean"] == 1.0
    assert m["per_rollout"]["r0"]["pass"] is True
    assert m["per_rollout"]["r0"]["score"]["score"] == 1.0
    assert m["criteria"]["H3"]["must_have"] is True
    md = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "**pass@1 = 1/1 = 1.00**" in md
    # and the stored agg renders identically without re-deriving anything
    assert md == report.render("GT999-no-such-task", "run-x", agg, records)


# -- --re-report: new report, old records, nothing re-graded ----------------------

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


def test_re_report_reads_an_old_run_under_a_new_table(tmp_path, monkeypatch):
    records = [rec("r0", {"H1": True, "H3": True, "G1c": True}),
               rec("r1", {"H1": True, "H3": True, "G1c": False},
                   classification="reasoning", passed=False)]
    run = _run_dir(tmp_path, records)
    monkeypatch.setattr(run_rollouts.taskspec, "load",
                        lambda *a, **k: StubSpec(MATRIX))
    assert run_rollouts.re_report(run, None, None) == 0
    stored = json.loads((run / "report.json").read_text(encoding="utf-8"))
    m = stored["summary"]["matrix"]
    assert m["pass"]["passed"] == 1 and m["pass"]["n"] == 2
    assert m["mean_score"]["mean"] == 0.75


def test_re_report_refuses_a_run_with_no_records(tmp_path):
    empty = tmp_path / "GT999-no-such-task" / "run-empty"
    empty.mkdir(parents=True)
    assert run_rollouts.re_report(empty, None, None) == 2
    assert not (empty / "report.json").exists()


# -- the interval prints on the same line as the rate, every time -----------------

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


def test_non_matrix_json_has_exactly_these_keys(fakes):
    keys = {
        "rollouts", "scored", "quarantined", "quarantined_caught_violating",
        "quarantined_unobserved", "redteam_detector_tests",
        "excluded_from_pass_at_1", "pass_at_1", "pass_at_1_denominator",
        "pass_hat_k", "pass_hat_k_k", "pass_hat_k_attempted",
        "pass_hat_k_complete", "pass_hat_k_passed", "pass_hat_k_denominator",
        "pass_hat_k_by_model", "run_conditions", "measurement_valid",
        "invalidated_because", "classification_counts", "score_distribution",
        "per_check", "tool_calls_median", "wall_clock_s_median",
        "dead_surfaces", "unprobed_tools", "needs_review", "models",
        "drivers", "topologies", "blindness_guarantees",
        "pass_at_1_ci95", "pass_at_1_se", "pass_at_1_n", "pass_at_k",
        "learning_signal",
    }
    agg = report.aggregate(five(4))
    assert set(agg) == keys
    assert "matrix" not in agg
    # the reasoning-only rate is gone with the axes
    assert not any(k.startswith("pass_at_1_reasoning") for k in agg)
    # and the run-level Pass^k bit is untouched
    assert agg["pass_hat_k"] == 0.0 and agg["pass_hat_k_k"] == 5


def test_matrix_block_has_exactly_these_keys(fakes):
    m = report.aggregate(three(), spec=StubSpec(TIERED))["matrix"]
    assert set(m) == {
        "role", "scored", "graded", "unmeasured",
        "pass", "mean_score", "criteria", "criteria_denominator", "must_have",
        "outcome_declared", "outcome_counts", "outcome_counts_denominator",
        "saturated",
        "reward_progress_span", "reward_mean", "reward_var",
        "reward_denominator", "reward_note",
        "band_mean", "progress_mean", "progress_denominator",
        "progress_declared", "progress_anchored", "progress_reachability",
        "progress_reachability_paid", "progress_paid_denominator",
        "path_mean", "path_declared", "path_event_counts", "path_allow",
        "rollouts_without_audit_log", "per_rollout",
    }
    assert set(m["pass"]) == {"n", "passed", "rate", "ci95", "pass_at_k",
                              "pass_hat_k", "top_band", "denominator"}
    assert set(m["mean_score"]) == {"n", "mean", "per_episode", "denominator"}
    assert set(m["criteria"]["H3"]) == {"n", "passed", "rate", "must_have",
                                        "required_by", "condition"}
    assert set(m["per_rollout"]["r0"]) == {
        "outcome", "pass", "score", "band", "progress", "path_penalty",
        "path_events", "reward", "tracked_metrics"}


# -- the estimator table: k <= n only, kept apart from the Pass^k bit --------------

def test_pass_at_k_table_is_restricted_to_k_le_n(fakes):
    records = five(4)
    agg = report.aggregate(records)
    pk = agg["pass_at_k"]
    assert pk["n"] == 5 and pk["c"] == 4
    assert list(pk["pass_at_k"]) == ["1", "2", "3", "5"]   # 8 and 10 are undefined
    assert pk["pass_hat_k"]["5"] == 0.25
    md = report.render("T", "run", agg, records)
    assert "| k | pass@k | pass^k (unbiased est.) |" in md
    assert "| 5 | 0.50 | 0.25 |" in md
    assert "| 8 |" not in md
    # the run-level bit stays exactly as published, with its own k
    assert "Pass^k = 0.0 (k = 5 scored of 5 attempted; 4 passed)" in md
    # the estimators explain themselves as estimators, and state n and c
    assert "n=5, c=4 passed" in md


def test_matrix_task_tabulates_the_headline_pass_over_graded(fakes):
    spec = StubSpec(MATRIX)
    records = three() + [rec("r_old", {"OLD": True})]
    agg = report.aggregate(records, spec=spec)
    h = agg["matrix"]["pass"]
    assert h["n"] == 3 and h["passed"] == 1            # the unmeasured one left
    assert list(h["pass_at_k"]) == ["1", "2", "3"]
    md = run_md(records, spec)
    assert ("pass@k / pass^k (unbiased est.) for the headline pass: n=3 "
            "graded, c=1 passed") in md
    # the grader's own table is still there, labelled for what it is
    assert "pass@k / pass^k (unbiased est.) over scored rollouts (pass_all_required)" in md


# -- ## Learning signal: placed after the pass block, before the failures ----------

def test_learning_signal_section_renders_in_place(fakes):
    spec = StubSpec(MATRIX)
    records = three()
    agg = report.aggregate(records, spec=spec)
    ls = agg["learning_signal"]
    assert ls["band"] == "core" and ls["group"]["p_one_minus_p"] == 0.16
    md = run_md(records, spec)
    assert md.index("Pass^k =") < md.index("## Learning signal") \
        < md.index("## Failure classification")
    assert "the headline pass bit (outcome class in the arm's top band)" in md
    assert "- group: n=5 · mean 0.80 · variance 0.16 · p(1−p) 0.16 · collapsed: false" in md
    assert "- band: core · rule: core 0.05 ≤ pass@1 ≤ 0.7" in md
    assert "axis separation" not in md
    assert "(provisional)" not in md


def _section(md: str, start: str, end: str) -> list[str]:
    body = md[md.index(start) + len(start):md.index(end)]
    return [ln for ln in body.splitlines() if ln.strip()]


def test_invalid_run_marks_every_learning_signal_line_provisional(fakes):
    spec = StubSpec(MATRIX)
    records = [rec("r0", {"H1": True, "H3": True, "G1c": True}),
               # a detector test in the run: not a measurement of anything
               rec("r1", {"H1": True, "H3": True, "G1c": True},
                   redteam_detector_test=True)]
    agg = report.aggregate(records, spec=spec)
    assert agg["measurement_valid"] is False
    md = run_md(records, spec)
    lines = _section(md, "## Learning signal", "## Failure classification")
    assert lines and all(ln.startswith("(provisional) ") for ln in lines)
    # the numbers are still there, marked, not dropped
    assert any("band: core" in ln for ln in lines)
    # the estimator tables take the same mark
    assert "(provisional) pass@k / pass^k (unbiased est.) over scored" in md
    assert "(provisional) pass@k / pass^k (unbiased est.) for the headline pass" in md
    # and both rates are withheld, the provisional numbers named as such
    assert "**pass@1 = WITHHELD - this run is not a clean measurement.**" in md
    assert "(provisional, do not publish: pass@1 = 2/2 [0.38, 0.96] · mean score = 1.00 (n=2))" in md
    assert "**pass_all_required = WITHHELD" in md
    assert "(provisional, do not publish: 1.0 [0.38, 0.96] (n=2 scored rollouts)" in md


# -- outcome class and declared scalar, per episode -----------------------------------

def test_outcome_class_and_reward_per_rollout(fakes):
    spec = StubSpec(MATRIX)
    records = [
        rec("r0", {"H3": True, "G1c": True, "H1": True}),
        rec("r1", {"H3": False, "G1c": False, "H1": True},
            classification="reasoning", passed=False),
        rec("r2", {"H3": True, "G1c": True, "H1": False},
            classification="reasoning", passed=False),
    ]
    # H1 is a penalty check: it fired on r2 and charged two points
    records[2]["checks"]["H1"]["points"] = -2.0
    records[2]["checks"]["H1"]["max"] = 0.0
    agg = report.aggregate(records, spec=spec)
    m = agg["matrix"]
    pr = m["per_rollout"]
    assert pr["r0"]["outcome"]["outcome"] == "resolved"
    assert pr["r1"]["outcome"]["outcome"] == "silent"
    assert pr["r2"]["outcome"]["outcome"] == "false_claim"
    # precedence order, not alphabetical
    assert list(m["outcome_counts"]) == ["false_claim", "resolved", "silent"]
    assert m["outcome_counts"] == {"false_claim": 1, "resolved": 1, "silent": 1}
    # the false claim fails the headline and still gets its score: all of
    # the repair was there
    assert pr["r2"]["pass"] is False and pr["r2"]["score"]["score"] == 1.0
    assert m["pass"]["rate"] == 0.333 and m["mean_score"]["mean"] == 0.667
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
    assert md.index("**pass@1") < md.index("declared training scalar")
    assert "| r2 | reasoning | false_claim | no | 1.00 | -1.00 | n/a | 0.00 | " in md
    # no [matrix.progress] / [matrix.path] on this arm: the tiers say so
    assert m["progress_declared"] is False and m["path_declared"] is False
    assert pr["r0"]["progress"] is None and pr["r0"]["path_penalty"] == 0.0
    assert m["band_mean"] == 0.0 and m["path_mean"] == 0.0
    assert "progress n/a (arm declares no quantities)" in md
    assert "path n/a (arm declares no [matrix.path])" in md


# -- an unfilled stub costs the block, not the report -----------------------------

def test_unimplemented_summarise_degrades_to_a_caveat(monkeypatch):
    install_fakes(monkeypatch, summarise_raises=True)
    spec = StubSpec(MATRIX)
    records = [rec("r0", {"H1": True, "H3": True, "G1c": True})]
    agg = report.aggregate(records, spec=spec)
    assert agg["learning_signal"] is None
    assert any("learning_signal.summarise is not implemented yet" in x
               for x in agg["run_conditions"]["caveats"])
    # the interval still came through - only the one stub was refused
    assert agg["pass_at_1_ci95"] == FAKE_CI
    assert agg["matrix"]["pass"]["ci95"] == FAKE_CI
    md = run_md(records, spec)
    assert "## Learning signal" in md
    assert "learning signal: unavailable" in md
    assert "- caveat: learning_signal.summarise is not implemented yet" in md
    assert "**pass@1 = 1/1 = 1.00**" in md


def test_module_that_did_not_import_degrades_to_a_caveat(monkeypatch):
    install_fakes(monkeypatch)
    monkeypatch.setattr(report, "outcome", None)
    monkeypatch.setitem(report._IMPORT_FAILURES, "outcome", "SyntaxError: x")
    records = [rec("r0", {"H1": True, "H3": True, "G1c": True})]
    agg = report.aggregate(records, spec=StubSpec(MATRIX))
    m = agg["matrix"]
    assert m["outcome_declared"] is True and m["outcome_counts"] == {}
    assert m["per_rollout"]["r0"]["outcome"] is None
    assert m["per_rollout"]["r0"]["pass"] is None
    assert m["pass"]["n"] == 0 and m["criteria"] == {}
    assert any("outcome.classify_outcome unavailable" in x
               for x in agg["run_conditions"]["caveats"])
    md = run_md(records, StubSpec(MATRIX))
    assert "outcomes: unavailable" in md
    assert "**pass@1 = WITHHELD - no scored rollout has an outcome class**" in md


# -- the three tiers: band + progress-in-band + path, per episode and aggregated

def tiered_records():
    """resolved · acted_unresolved at progress 0.5 · boundary_violated with
    one write under .platform/ (path -0.5)."""
    resolved = rec("r0", {"H1": True, "H3": True, "G1c": True, "D1": True})
    acted = rec("r1", {"H1": True, "H3": False, "G1c": False, "D1": True,
                       "C0": True, "PROG": True},
                classification="reasoning", passed=False)
    acted["checks"]["PROG"]["detail"] = {"repaired": 5}      # 5 of oracle 10
    violated = rec("r2", {"H1": True, "H3": True, "G1c": True, "D1": False},
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
    # the headline: one of three in the top band; the violator's repair was
    # all there, so it scores 1.0 while failing
    assert m["pass"]["passed"] == 1 and m["pass"]["n"] == 3
    assert [pr[r]["score"]["score"] for r in ("r0", "r1", "r2")] == [1.0, 0.0, 1.0]
    assert m["mean_score"]["mean"] == 0.667
    assert m["criteria"]["D1"] == {"n": 3, "passed": 2, "rate": 0.667,
                                   "must_have": False, "required_by": [],
                                   "condition": "!D1"}
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
    assert ("saturated: no (control arm, top band resolved; pass@1 0.33 over "
            "3 graded)") in md
    assert "progress reachability: n=1 · var 0.00 · range [0.50, 0.50] · 1 distinct" in md
    assert ("path events: platform_write 1 (over 1 of 3 scored rollouts with "
            "an audit log on record)") in md
    # the scalar and its tiers never climb above the headline
    assert md.index("**pass@1") < md.index("declared training scalar") \
        < md.index("tiers: band")
    # per-rollout tier columns
    assert "| r1 | reasoning | acted_unresolved | no | 0.00 | 0.00 | 0.50 | 0.00 | " in md
    assert "| r2 | reasoning | boundary_violated | no | 1.00 | -1.00 | n/a | -0.50 | " in md


def test_saturated_arm_is_named(fakes):
    spec = StubSpec(TIERED)
    records = [rec(f"r{i}", {"H1": True, "H3": True, "G1c": True, "D1": True})
               for i in range(5)]
    m = report.aggregate(records, spec=spec)["matrix"]
    assert m["pass"]["rate"] == 1.0 and m["saturated"] is True
    assert ("saturated: yes (control arm, top band resolved; pass@1 1.00 over "
            "5 graded) - >= 0.9 or <= 0.1") in run_md(records, spec)
    # a decision arm's TOP BAND is resolved AND the withholding: three
    # resolved + two withheld pass 5/5 and the arm IS saturated
    mixed = records[:3] + [
        rec(f"r{i}", {"H1": True, "H3": False, "G1c": False, "D1": True,
                      "D3": True, "S1": True}) for i in range(3, 5)]
    m2 = report.aggregate(mixed, spec=StubSpec(DECISION))["matrix"]
    assert m2["outcome_counts"] == {"resolved": 3, "withheld_with_evidence": 2}
    assert m2["pass"]["rate"] == 1.0 and m2["saturated"] is True
    # the same five on a control arm: 3/5, not saturated
    m3 = report.aggregate(mixed, spec=StubSpec(MATRIX))["matrix"]
    assert m3["pass"]["rate"] == 0.6 and m3["saturated"] is False
    # and an arm nobody passes is saturated too
    m4 = report.aggregate(
        [rec(f"r{i}", {"H1": True, "H3": False, "G1c": False},
             classification="reasoning", passed=False) for i in range(5)],
        spec=spec)["matrix"]
    assert m4["pass"]["rate"] == 0.0 and m4["saturated"] is True


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
    assert "**pass@1 = 1/3 = 0.33**" in md


def test_path_allowlist_prefers_the_task_declaration():
    class Declared(StubSpec):
        path_allow = {"platform_prefix": "platform/", "grading_prefixes": ["g/"],
                      "consumer_objects": ["consumer.book"]}
    assert report._path_allowlist(Declared(TIERED)) == Declared.path_allow
    # a [matrix.path] that lists consumer objects contributes them
    listed = MatrixSpec(path=types.SimpleNamespace(
        consumer_objects=("finance.book", "ar.ledger")))
    assert report._path_allowlist(StubSpec(listed))["consumer_objects"] == \
        ["ar.ledger", "finance.book"]
    assert report._path_allowlist(None)["consumer_objects"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([str(Path(__file__).resolve()), "-q"]))
