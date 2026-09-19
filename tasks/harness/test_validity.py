"""validity.py - the Layer-0 task validity card.

Static half against the real GT001-GT009 packages as they are on disk (the
known section-0 defects of tasks/MATRIX_RERUN_EDITS.md are asserted
CONDITIONALLY: flagged if the map still names the bad id, absent if fixed -
other agents edit those task.toml files concurrently). Run half against
captured fixture stdout only: the baseline suites take minutes to an hour and
touch DuckDB, so nothing here starts one.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import baselines_schema
import outcome
import validity

TASKS = Path(__file__).resolve().parent.parent
TASK_IDS = sorted(p.name for p in TASKS.glob("GT00*") if (p / "task.toml").is_file())
STATIC_KEYS = {
    "matrix_present", "pass_axis", "gate", "grader_check_ids",
    "grader_check_source", "map_checks_missing_from_grader", "proxy_declared",
    "accepted", "accepted_not_in_validity", "validity_rows", "has_oracle_row",
    "has_no_op_row", "has_false_claim_row", "has_positive_control_row",
    "pass_requires_checks_missing_from_grader", "required_surfaces_declared",
    "dead_by_design_declared", "findings"}


def raw_toml(task_id: str) -> dict:
    return tomllib.loads((TASKS / task_id / "task.toml").read_text(encoding="utf-8"))


# -- static card on the real corpus ------------------------------------------

@pytest.mark.parametrize("task_id", TASK_IDS)
def test_static_card_loads_every_task(task_id):
    c = validity.static_card(task_id, TASKS)
    assert c["schema_version"] == 1 and c["task"] == task_id and c["run"] is None
    assert set(c["static"]) == STATIC_KEYS
    s = c["static"]
    assert s["grader_check_source"] in ("manifest", "grade.py scan (heuristic)")
    assert s["grader_check_ids"], f"{task_id}: no grader check ids resolved"
    assert s["has_oracle_row"] and s["has_no_op_row"], task_id
    for f in s["findings"]:
        assert f["severity"] in ("block", "warn", "info") and f["code"] and f["detail"]
    assert c["valid"] == (not any(f["severity"] == "block" for f in s["findings"]))
    assert c["invalid_because"] == [f["detail"] for f in s["findings"]
                                    if f["severity"] == "block"]
    # pass_requires_checks must be gradable on every shipped task
    assert s["pass_requires_checks_missing_from_grader"] == [], task_id
    json.dumps(c)                                   # embeddable in report.json


def test_gt007_d2_defect_is_flagged_iff_still_on_disk():
    # MATRIX_RERUN_EDITS.md 0.3: warranted = ["D2"] named a check the grader
    # never emitted. The manifest lists D2 today (and grade.py adds it), so
    # the assertion reads the map and the grader together.
    raw = raw_toml("GT007-ran-long-or-started-late")
    c = validity.static_card("GT007-ran-long-or-started-late", TASKS)
    warranted = raw["matrix"]["map"].get("warranted", [])
    missing = c["static"]["map_checks_missing_from_grader"]
    bad = [x for x in warranted if x not in c["static"]["grader_check_ids"]]
    if bad:
        assert missing.get("warranted") == bad
        assert not c["valid"]
    else:
        assert "warranted" not in missing


def test_gt004_h3_defect_is_flagged_iff_still_on_disk():
    # 0.4: default arm warranted = ["G1c", "H3"] with H3 never emitted.
    raw = raw_toml("GT004-doubling-window")
    c = validity.static_card("GT004-doubling-window", TASKS)
    warranted = raw["matrix"]["map"].get("warranted", [])
    missing = c["static"]["map_checks_missing_from_grader"]
    if "H3" in warranted and "H3" not in c["static"]["grader_check_ids"]:
        assert "H3" in missing.get("warranted", [])
    else:
        assert "H3" not in missing.get("warranted", [])


def test_gt004_accepted_name_needs_a_row_in_the_default_arm():
    # accepted = ["escalate_only"] while the only escalate_only row is
    # `variant = "authorized"`: on the default arm oracle validity is empty.
    raw = raw_toml("GT004-doubling-window")
    c = validity.static_card("GT004-doubling-window", TASKS)
    accepted = raw["matrix"].get("accepted", [])
    default_rows = {r["agent"] for r in raw.get("validity", [])
                    if not r.get("variant") or r["variant"] == "default"}
    expected_missing = sorted(set(accepted) - default_rows)
    assert c["static"]["accepted_not_in_validity"] == expected_missing
    codes = [f["code"] for f in c["static"]["findings"]]
    assert ("accepted_not_in_validity" in codes) == bool(expected_missing)


def test_gt009_default_and_live_consumer_arms():
    base = validity.static_card("GT009-settlement-cutoff", TASKS)
    live = validity.static_card("GT009-settlement-cutoff", TASKS,
                                variant="live-consumer")
    assert base["variant"] == "default" and live["variant"] == "live-consumer"
    assert base["static"]["accepted"] == ["oracle"]
    assert live["static"]["accepted"] == ["oracle_live"]
    assert live["static"]["accepted_not_in_validity"] == []
    # the arm's rows join the base rows; base-only arm sees none of them
    assert live["static"]["validity_rows"] > base["static"]["validity_rows"]
    assert base["static"]["has_positive_control_row"]       # rebuilt_not_updated
    assert base["static"]["has_false_claim_row"]            # checklist "< no_op"
    assert base["static"]["grader_check_source"] == "manifest"
    assert base["static"]["map_checks_missing_from_grader"] == {}
    assert base["valid"] and live["valid"]


def test_gt006_stacked_rows_apply_only_to_stacked_arms():
    raw = raw_toml("GT006-quality-gate-drops-rows")
    default = validity.validity_rows(raw, "default")
    stacked = validity.validity_rows(raw, "stacked-neutral")
    assert all(r["table"] == "validity" for r in default)
    assert any(r["table"] == "validity_stacked" for r in stacked)
    assert {"fix_A_only"} <= {r["agent"] for r in stacked}
    assert "fix_A_only" not in {r["agent"] for r in default}


def test_unknown_variant_is_a_refusal():
    with pytest.raises(validity.taskspec.TaskSpecError):
        validity.static_card("GT009-settlement-cutoff", TASKS, variant="nope")


def test_row_heuristics():
    assert validity._is_positive_control({"agent": "rebuilt_not_updated", "expect": "== max"})
    assert validity._is_positive_control({"agent": "oracle_appended", "expect": "== oracle (a tick)"})
    assert not validity._is_positive_control({"agent": "oracle_backdated", "expect": "== oracle - 4"})
    assert not validity._is_positive_control({"agent": "oracle", "expect": "== max"})
    assert validity._is_false_claim({"agent": "vandal", "expect": "total < no_op's total"})
    assert validity._is_false_claim({"agent": "checklist_joined", "expect": "== checklist"})
    assert not validity._is_false_claim({"agent": "full_refresh", "expect": "partial"})


# -- the parser, on captured shapes -------------------------------------------

GT009_STDOUT = """\
[1/4] fixture (variant: default)
  falsifier: 30/30 planted damages caught

[2/4] structural gates
  grading zone unreachable from the world; the nights are not in the feed

[3/4] baselines (4)
  oracle                           total  20.0 / 20   C0,R1-4 [2 2 3 3 2]  D1-3 [2 2 2]  PASS  [== max; passes]
  rebuilt_not_updated              total  20.0 / 20   C0,R1-4 [2 2 3 3 2]  D1-3 [2 2 2]  PASS  [== max; positive control]
  no_op                            total   0.0 / 20   C0,R1-4 [0 0 0 0 0]  D1-3 [0 0 0]  fail  [<= 0]
  checklist                        total  -1.0 / 20   C0,R1-4 [0 0 0 0 0]  D1-3 [0 0 0]  fail  [< no_op]

[4/4] verdicts
  ok    oracle scores max (20.0 / 20.0) and passes
  ok    no_op <= 0 (0.0) - no clause an idle agent satisfies pays
  ok    checklist (-1.0) scores below no_op (0.0) - a false claim costs

TASK VALID
"""

GT001_STDOUT = """\

  baseline           score   expected
  ----------------------------------------------------
  oracle              19.0 /19.0 = 19.0 (max)
  no_op                0.0 /19.0 ~0
  reference_copy      17.0 /19.0 0, unreachable
  full_refresh         9.5 /19.0 partial, NOT 0
  dedupe_only         14.0 /19.0 partial
  checklist           17.0 /19.0 < oracle (penalty)
  checklist_joined    17.0 /19.0 == checklist (F620 regression guard)

  [PASS] oracle scores max
  [PASS] no_op scores <= 1
  [PASS] checklist penalised vs oracle

  TASK VALID
  NOTE: fixtures share a directory, so ATTACH succeeds here by design.
"""

GT001_BROKEN = GT001_STDOUT.replace("  TASK VALID", "  TASK BROKEN (1 failing)") \
                           .replace("[PASS] oracle scores max", "[FAIL] oracle scores max")

GT006_STDOUT = """\
  baselines
    oracle                          19.00/19    pass=True  A=repaired B=repaired  G1a 3 G1b 3
    no_op                            0.00/19    pass=False A=untouched B=untouched G1a 0 G1b 0
TASK INVALID - 1 problem(s):
  - no_op passes the criterion
"""


def test_parse_gt009_shape():
    r = validity.parse_baselines(GT009_STDOUT)
    assert r["task_valid_printed"] and r["parse_confidence"] == "full"
    assert set(r["baselines"]) == {"oracle", "rebuilt_not_updated", "no_op", "checklist"}
    o = r["baselines"]["oracle"]
    assert (o["total"], o["max"], o["passed"], o["expect"], o["met"]) == \
        (20.0, 20.0, True, "== max; passes", True)
    assert r["baselines"]["no_op"]["passed"] is False
    assert r["baselines"]["no_op"]["met"] is True            # "<= 0"
    assert r["baselines"]["checklist"]["met"] is True        # "< no_op" vs 0.0
    assert r["baselines"]["rebuilt_not_updated"]["met"] is True
    assert r["oracle_at_max"] and r["no_op_at_or_below_zero"]
    assert r["false_claim_below_no_op"] is True


def test_parse_gt001_shape_and_broken_verdict():
    r = validity.parse_baselines(GT001_STDOUT)
    assert r["task_valid_printed"] and r["parse_confidence"] == "full"
    assert len(r["baselines"]) == 7
    assert r["baselines"]["oracle"]["expect"] == "= 19.0 (max)"
    assert r["baselines"]["oracle"]["met"] is True
    assert r["baselines"]["no_op"]["met"] is True            # "~0"
    assert r["baselines"]["checklist"]["met"] is True        # "< oracle"
    assert r["baselines"]["checklist_joined"]["met"] is True  # "== checklist"
    assert r["baselines"]["full_refresh"]["met"] is None     # not judged
    assert r["baselines"]["oracle"]["passed"] is None        # GT001 prints no pass bit
    # GT001's checklist expects "< oracle", not "< no_op": not judged
    assert r["false_claim_below_no_op"] is None
    b = validity.parse_baselines(GT001_BROKEN)
    assert b["task_valid_printed"] is False
    assert b["parse_confidence"] == "partial"


def test_parse_gt006_shape_uses_declared_expect():
    declared = {"oracle": "== max, passes", "no_op": "0 on EVERY clause that pays"}
    r = validity.parse_baselines(GT006_STDOUT, declared)
    assert r["task_valid_printed"] is False
    assert r["baselines"]["oracle"]["passed"] is True
    assert r["baselines"]["no_op"]["passed"] is False
    assert r["baselines"]["oracle"]["expect"] == "== max, passes"
    assert r["baselines"]["oracle"]["met"] is True
    assert r["parse_confidence"] == "partial"


GT005_STDOUT = """\
  oracle              19.00 / 19.0  [C1 L1 C2 P1] REQ    (41s)
  no_op                0.00 / 19.0  [.. .. .. P1]        (12s)
  vandal              -2.00 / 19.0  [.. .. .. ..]        (13s)
  ok   every baseline landed where task.toml says
TASK VALID
"""


def test_parse_gt005_shape_flag_cells_are_not_expectations():
    declared = {"oracle": "== max (19.0), required checks pass",
                "no_op": "== 0.0 exactly", "vandal": "total < no_op's total; P1 fails"}
    r = validity.parse_baselines(GT005_STDOUT, declared)
    assert r["task_valid_printed"] and r["parse_confidence"] == "full"
    assert r["baselines"]["oracle"]["expect"] == declared["oracle"]
    assert r["baselines"]["oracle"]["met"] is True
    assert r["baselines"]["no_op"]["met"] is True
    assert r["baselines"]["vandal"]["met"] is True          # "< no_op"
    assert r["false_claim_below_no_op"] is True


def test_parse_nothing():
    r = validity.parse_baselines("Traceback (most recent call last):\n  boom\n")
    assert r["parse_confidence"] == "none" and r["baselines"] == {}
    assert r["oracle_at_max"] is None and r["task_valid_printed"] is False


def test_suite_flags_are_read_not_assumed():
    assert "--quick" not in validity._suite_flags(
        TASKS / "GT001-aov-halving" / "tests" / "run_baselines.py")
    assert {"--quick", "--variant"} <= validity._suite_flags(
        TASKS / "GT009-settlement-cutoff" / "tests" / "run_baselines.py")


# -- render and CLI -------------------------------------------------------------

def test_render_names_the_findings():
    c = validity.static_card("GT004-doubling-window", TASKS)
    md = validity.render(c)
    assert md.startswith("# Validity card - GT004-doubling-window (default)")
    for f in c["static"]["findings"]:
        assert f["code"] in md and f["detail"] in md
    assert "not run (static card only)" in md
    c["run"] = {"ran": True, "exit_code": 0, "duration_s": 1.0,
                **validity.parse_baselines(GT009_STDOUT), "stdout_tail": ""}
    md = validity.render(c)
    assert "| oracle | 20.0 | 20.0 | True | == max; passes | True |" in md


def test_cli_writes_json_and_md_and_exit_codes(tmp_path):
    rc = validity.main(["--task", "GT009-settlement-cutoff", "--tasks-dir", str(TASKS),
                        "--out", str(tmp_path)])
    assert rc == 0
    c = json.loads((tmp_path / "validity.json").read_text(encoding="utf-8"))
    assert c["valid"] is True and c["variant"] == "default"
    assert (tmp_path / "validity.md").read_text(encoding="utf-8").startswith("# Validity card")
    assert validity.main(["--task", "GT000-nope", "--tasks-dir", str(TASKS)]) == 3
    if not validity.static_card("GT004-doubling-window", TASKS)["valid"]:
        assert validity.main(["--task", "GT004-doubling-window",
                              "--tasks-dir", str(TASKS), "--out", str(tmp_path)]) == 2


# -- the ladder (--ladder, DECISION_TEMPLATE.md §5/§9) --------------------------
#
# GT009's suite does not emit --json yet (Wave 2), so the baseline JSON is
# synthesised HERE from the arm's own [matrix.outcome] table: for every
# required baseline a `checks` dict is built that classify_outcome puts in the
# class the template expects, and the helper asserts that before handing it
# over. The role is read the way ladder_card reads it, so the fixture follows
# GT009's task.toml whether or not `role` has landed there.

GT009 = "GT009-settlement-cutoff"

# A positive condition "X" holds when X passed at ceiling; a negated "!X"
# holds when X paid less than its ceiling. Penalty-only rows (H1/H2) carry
# max 0 and read passed False whether or not they fired (outcome.py doc).
_HOLD_POS = {"points": 1.0, "max": 1.0, "passed": True}
_MISS_POS = {"points": 0.0, "max": 1.0, "passed": False}
_FIRED_NEG = {"points": -1.0, "max": 0.0, "passed": False}
_QUIET_NEG = {"points": 0.0, "max": 0.0, "passed": False}


def _checks_for(spec, target: str) -> dict:
    """A checks dict that `spec` classifies as `target`, built generically
    from the table's conditions."""
    checks: dict[str, dict] = {}
    for conds in spec.map.values():
        for cond in conds:
            neg, cid = outcome.split_condition(cond)
            checks.setdefault(cid, dict(_QUIET_NEG if neg else _MISS_POS))

    def hold(cond):
        neg, cid = outcome.split_condition(cond)
        checks[cid] = dict(_FIRED_NEG if neg else _HOLD_POS)

    if target == "resolved":
        for c in spec.conditions_for("resolved"):
            hold(c)
    elif target == "withheld_with_evidence":
        for c in spec.conditions_for("withheld_with_evidence"):
            hold(c)
    elif target == "acted_unresolved":
        hold(spec.conditions_for("acted")[0])
    elif target == "false_claim":
        hold(spec.conditions_for("false_claim")[0])
    elif target == "boundary_violated":          # the right repair plus damage
        for c in spec.conditions_for("resolved"):
            hold(c)
        hold(spec.conditions_for("boundary_violated")[0])
    elif target != "silent":
        raise ValueError(target)
    got = outcome.classify_outcome(spec, checks)["outcome"]
    assert got == target, (target, got, checks)
    return checks


def _with_progress_details(checks: dict, progress_spec) -> dict:
    """Give every progress quantity a `detail[field]`: 1.0 where its check
    held, 0.0 where it did not, so anchors read from the fixture resolve
    (noop 0 / oracle "max" -> 1.0) and the ladder can compute progress."""
    if progress_spec is None:
        return checks
    for q in progress_spec.quantities.values():
        row = checks.setdefault(q["check"], dict(_MISS_POS))
        row.setdefault("detail", {})[q["field"]] = 1.0 if row["passed"] else 0.0
    return checks


def _gt009_role(variant: str = "default") -> tuple[str, object]:
    spec = validity.taskspec.load(GT009, TASKS, variant=variant)
    return (getattr(spec.matrix, "role", None) or "control"), spec.matrix.outcome


def _gt009_progress(variant: str = "default"):
    return validity.taskspec.load(GT009, TASKS, variant=variant).matrix.progress


def _gt009_baselines(variant: str = "default", drop=(), **overrides) -> dict:
    """A schema-valid §5 JSON for GT009 whose required baselines each classify
    as the template expects. `overrides` = baseline name -> class to build
    its checks for instead (the mismatch fixtures); `drop` removes names."""
    role, table = _gt009_role(variant)
    progress_spec = _gt009_progress(variant)
    expected = baselines_schema.EXPECTED_OUTCOME[role]
    rows = {}
    for name in baselines_schema.REQUIRED_BASELINES[role]:
        if name in drop:
            continue
        checks = _with_progress_details(
            _checks_for(table, overrides.get(name, expected[name])), progress_spec)
        rows[name] = {"expect": "synthetic", "met": True,
                      "total": sum(c["points"] for c in checks.values()),
                      "max": sum(c["max"] for c in checks.values()),
                      "passed": expected[name] == "resolved",
                      "checks": checks}
    return {"schema_version": 1, "task": GT009, "variant": variant,
            "generated_utc": "2026-09-18T00:00:00Z", "task_valid": True,
            "baselines": rows}


def _write(tmp_path, doc: dict) -> Path:
    p = tmp_path / "baselines.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _codes(card: dict, severity: str | None = None) -> list[str]:
    return [f["code"] for f in card["findings"]
            if severity is None or f["severity"] == severity]


def test_ladder_passes_on_gt009_fixture(tmp_path):
    card = validity.ladder_card(GT009, TASKS, "default", _write(tmp_path, _gt009_baselines()))
    assert _codes(card, "block") == [], card["findings"]
    assert card["valid"] and card["task"] == GT009 and card["variant"] == "default"
    role, _ = _gt009_role()
    assert card["role"] == role
    by_name = {r["baseline"]: r for r in card["ladder"]}
    assert set(by_name) == set(baselines_schema.REQUIRED_BASELINES[role])
    for name, row in by_name.items():
        assert row["classified_outcome"] == baselines_schema.EXPECTED_OUTCOME[role][name]
        assert row["expected_outcome"] == row["classified_outcome"]
        assert row["reward"] == validity.reward.episode_reward(
            row["classified_outcome"], role, row["progress"], 0.0)["reward"]
    assert by_name["oracle"]["reward"] == 1.0
    assert by_name["no_op"]["reward"] == 0.0
    assert by_name["checklist"]["reward"] == -1.0
    assert by_name["collateral"]["reward"] == -1.0
    # progress: GT009 declares [matrix.progress] (Wave 2), so the fixture's
    # detail fields anchor it: the oracle at the ceiling, no_op at 0, the
    # acted-unresolved rival strictly between, and no "undeclared" info row
    assert not ({"PROGRESS_UNDECLARED", "PROGRESS_MODULE_UNAVAILABLE"}
                & set(_codes(card, "info")))
    assert by_name["no_op"]["progress"] == 0.0
    assert 0.0 < by_name["acted_unresolved"]["progress"] < 1.0
    assert 0.0 < by_name["acted_unresolved"]["reward"] < 1.0
    assert card["progress_reachability"]["distinct"] >= 3
    json.dumps(card)


def test_ladder_live_consumer_arm_too(tmp_path):
    doc = _gt009_baselines("live-consumer")
    card = validity.ladder_card(GT009, TASKS, "live-consumer", _write(tmp_path, doc))
    assert _codes(card, "block") == [], card["findings"]


def test_ladder_refuses_outcome_mismatch(tmp_path):
    # no_op's checks look like the oracle's: the idle agent would read resolved
    doc = _gt009_baselines(no_op="resolved")
    card = validity.ladder_card(GT009, TASKS, "default", _write(tmp_path, doc))
    mism = [f for f in card["findings"] if f["code"] == "LADDER_OUTCOME_MISMATCH"]
    assert mism and all(f["severity"] == "block" for f in mism)
    assert any("no_op" in f["detail"] and "'silent'" in f["detail"]
               and "'resolved'" in f["detail"] for f in mism)
    assert not card["valid"]


def test_ladder_refuses_declared_outcome_that_disagrees(tmp_path):
    doc = _gt009_baselines()
    doc["baselines"]["oracle"]["outcome"] = "silent"       # the suite's own claim
    card = validity.ladder_card(GT009, TASKS, "default", _write(tmp_path, doc))
    assert any(f["code"] == "LADDER_OUTCOME_MISMATCH" and "suite declares" in f["detail"]
               for f in card["findings"])
    doc["baselines"]["oracle"]["outcome"] = "resolved"
    card = validity.ladder_card(GT009, TASKS, "default", _write(tmp_path, doc))
    assert "LADDER_OUTCOME_MISMATCH" not in _codes(card)


def test_ladder_refuses_missing_baseline(tmp_path):
    doc = _gt009_baselines(drop=("no_op",))
    card = validity.ladder_card(GT009, TASKS, "default", _write(tmp_path, doc))
    miss = [f for f in card["findings"] if f["code"] == "LADDER_MISSING_BASELINE"]
    assert len(miss) == 1 and "'no_op'" in miss[0]["detail"]
    assert miss[0]["severity"] == "block" and not card["valid"]


def test_ladder_refuses_schema_error(tmp_path):
    doc = _gt009_baselines()
    del doc["baselines"]["checklist"]["checks"]
    card = validity.ladder_card(GT009, TASKS, "default", _write(tmp_path, doc))
    schema = [f for f in card["findings"] if f["code"] == "BASELINES_SCHEMA"]
    assert schema and "$.baselines.checklist.checks: missing" in [f["detail"] for f in schema]
    assert "checklist" not in {r["baseline"] for r in card["ladder"]}
    # a file that is not JSON at all is a schema block, not a crash
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    card = validity.ladder_card(GT009, TASKS, "default", bad)
    assert _codes(card, "block") == ["BASELINES_SCHEMA"] and not card["valid"]


def test_ladder_refuses_wrong_task_or_arm(tmp_path):
    doc = _gt009_baselines()
    doc["variant"] = "live-consumer"
    card = validity.ladder_card(GT009, TASKS, "default", _write(tmp_path, doc))
    assert any(f["code"] == "BASELINES_SCHEMA" and "$.variant" in f["detail"]
               for f in card["findings"])


def test_ladder_order_violation(tmp_path):
    # the idle agent's checks read resolved (1.0) while the oracle's read
    # silent (0.0): both mismatches, AND the scalar now orders them wrongly
    doc = _gt009_baselines(no_op="resolved", oracle="silent")
    card = validity.ladder_card(GT009, TASKS, "default", _write(tmp_path, doc))
    order = [f for f in card["findings"] if f["code"] == "LADDER_ORDER_VIOLATION"]
    assert order and all(f["severity"] == "block" for f in order)
    assert any("oracle" in f["detail"] and "no_op" in f["detail"] for f in order)


def test_ladder_order_violation_from_the_scalar_alone(tmp_path, monkeypatch):
    # every class right, the scalar wrong: a reward that paid silence 1.0
    real = validity.reward.episode_reward

    def broken(outcome_cls, role, progress=None, path_penalty=0.0):
        out = real(outcome_cls, role, progress, path_penalty)
        if outcome_cls == "silent":
            out = {**out, "reward": 1.0}
        return out

    monkeypatch.setattr(validity.reward, "episode_reward", broken)
    card = validity.ladder_card(GT009, TASKS, "default", _write(tmp_path, _gt009_baselines()))
    assert "LADDER_OUTCOME_MISMATCH" not in _codes(card)
    order = [f for f in card["findings"] if f["code"] == "LADDER_ORDER_VIOLATION"]
    assert any("no_op" in f["detail"] and "expected silent" in f["detail"] for f in order)


def test_ladder_no_outcome_table_is_a_block(tmp_path):
    # GT001 declares no [matrix.outcome] (nor, today, a [matrix] at all)
    task = next(t for t in TASK_IDS if t.startswith("GT001"))
    spec = validity.taskspec.load(task, TASKS)
    if spec.matrix is not None and getattr(spec.matrix, "outcome", None) is not None:
        pytest.skip(f"{task} now declares [matrix.outcome]")
    doc = {**_gt009_baselines(), "task": task, "variant": spec.variant or "default"}
    card = validity.ladder_card(task, TASKS, spec.variant or None, _write(tmp_path, doc))
    assert "LADDER_NO_OUTCOME_TABLE" in _codes(card, "block") and not card["valid"]


def test_ladder_unknown_variant_is_a_refusal(tmp_path):
    with pytest.raises(validity.taskspec.TaskSpecError):
        validity.ladder_card(GT009, TASKS, "nope", _write(tmp_path, _gt009_baselines()))


def test_render_ladder_lists_every_baseline_and_finding(tmp_path):
    doc = _gt009_baselines(no_op="resolved")
    card = validity.ladder_card(GT009, TASKS, "default", _write(tmp_path, doc))
    md = validity.render_ladder(card)
    assert md.startswith(f"# Validity ladder - {GT009} (default, role {card['role']})")
    for row in card["ladder"]:
        assert f"| {row['baseline']} |" in md
    for f in card["findings"]:
        assert f["code"] in md and f["detail"] in md


def test_cli_ladder_exit_codes(tmp_path, capsys):
    good = _write(tmp_path, _gt009_baselines())
    rc = validity.main(["--task", GT009, "--tasks-dir", str(TASKS),
                        "--variant", "default", "--ladder", str(good)])
    assert rc == 0
    assert "# Validity ladder" in capsys.readouterr().out
    out_dir = tmp_path / "out"
    rc = validity.main(["--task", GT009, "--tasks-dir", str(TASKS),
                        "--variant", "default", "--ladder", str(good),
                        "--out", str(out_dir)])
    assert rc == 0
    assert json.loads((out_dir / "ladder.json").read_text(encoding="utf-8"))["valid"] is True
    assert (out_dir / "ladder.md").is_file()
    bad = tmp_path / "mismatch.json"
    bad.write_text(json.dumps(_gt009_baselines(no_op="resolved")), encoding="utf-8")
    assert validity.main(["--task", GT009, "--tasks-dir", str(TASKS),
                          "--variant", "default", "--ladder", str(bad)]) == 1
    # a missing file is a schema block (exit 1); an unknown arm a refusal (3)
    assert validity.main(["--task", GT009, "--tasks-dir", str(TASKS),
                          "--variant", "default",
                          "--ladder", str(tmp_path / "absent.json")]) == 1
    assert validity.main(["--task", GT009, "--tasks-dir", str(TASKS),
                          "--variant", "nope", "--ladder", str(good)]) == 3
    with pytest.raises(SystemExit) as exc:                 # --ladder needs --variant
        validity.main(["--task", GT009, "--tasks-dir", str(TASKS), "--ladder", str(good)])
    assert exc.value.code == 2


def test_cli_lab_card(capsys):
    rc = validity.main(["--task", GT009, "--tasks-dir", str(TASKS), "--lab-card"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("| id |") and "| A12 |" in out and "| O8 |" in out
    assert validity.main(["--task", "GT000-nope", "--tasks-dir", str(TASKS),
                          "--lab-card"]) == 3
