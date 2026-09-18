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
