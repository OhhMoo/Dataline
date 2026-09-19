"""baselines_schema.py - the §5 baseline JSON validator and the required table."""
from __future__ import annotations

import copy

import baselines_schema as bs
import outcome
import reward


def _valid() -> dict:
    return {
        "schema_version": 1, "task": "GT009-settlement-cutoff",
        "variant": "default", "generated_utc": "2026-09-18T00:00:00Z",
        "task_valid": True,
        "baselines": {
            "oracle": {"expect": "== max", "met": True, "total": 20.0,
                       "max": 20.0, "passed": True, "outcome": "resolved",
                       "artifact": {"gap_days_recovered": 3},
                       "checks": {"C0": {"points": 2.0, "max": 2.0, "passed": True,
                                         "detail": {"recovered": 3}},
                                  "H1": {"points": 0, "max": 0, "passed": False,
                                         "detail": "no false claim"}}},
            "no_op": {"expect": "<= 0", "met": True, "total": 0.0, "max": 20.0,
                      "passed": False, "outcome": None, "artifact": None,
                      "checks": {"C0": {"points": 0.0, "max": 2.0, "passed": False}}},
        },
    }


def test_valid_fixture_has_no_errors():
    assert bs.validate(_valid()) == []


def test_not_an_object():
    assert bs.validate([1, 2]) == ["$: expected an object, got list"]


def test_missing_top_level_keys_are_named():
    doc = _valid()
    for key in ("task", "variant", "generated_utc", "task_valid"):
        del doc[key]
    errs = bs.validate(doc)
    for key in ("task", "variant", "generated_utc", "task_valid"):
        assert f"$.{key}: missing" in errs


def test_wrong_top_level_types_are_named():
    doc = _valid()
    doc["task_valid"] = "yes"
    doc["baselines"] = []
    errs = bs.validate(doc)
    assert any(e.startswith("$.task_valid: expected bool") for e in errs)
    assert any(e.startswith("$.baselines: expected dict") for e in errs)


def test_schema_version_and_role_checked():
    doc = _valid()
    doc["schema_version"] = 2
    doc["role"] = "referee"
    errs = bs.validate(doc)
    assert any(e.startswith("$.schema_version: expected 1") for e in errs)
    assert any(e.startswith("$.role:") for e in errs)
    doc["schema_version"] = 1
    doc["role"] = "decision"
    assert bs.validate(doc) == []


def test_baseline_missing_checks_and_total():
    doc = _valid()
    del doc["baselines"]["no_op"]["checks"]
    del doc["baselines"]["no_op"]["total"]
    errs = bs.validate(doc)
    assert "$.baselines.no_op.checks: missing" in errs
    assert "$.baselines.no_op.total: missing" in errs


def test_baseline_not_an_object():
    doc = _valid()
    doc["baselines"]["no_op"] = 3
    assert bs.validate(doc) == ["$.baselines.no_op: expected an object, got int"]


def test_empty_baselines_is_an_error():
    doc = _valid()
    doc["baselines"] = {}
    assert any(e.startswith("$.baselines: empty") for e in bs.validate(doc))


def test_check_missing_fields_and_wrong_types():
    doc = _valid()
    doc["baselines"]["oracle"]["checks"]["C0"] = {"passed": "true"}
    doc["baselines"]["oracle"]["checks"]["R1"] = "pass"
    errs = bs.validate(doc)
    assert "$.baselines.oracle.checks.C0.points: missing" in errs
    assert "$.baselines.oracle.checks.C0.max: missing" in errs
    assert any(e.startswith("$.baselines.oracle.checks.C0.passed: expected bool")
               for e in errs)
    assert "$.baselines.oracle.checks.R1: expected an object, got str" in errs


def test_bool_is_not_a_number():
    doc = _valid()
    doc["baselines"]["oracle"]["checks"]["C0"]["points"] = True
    assert any("checks.C0.points: expected int | float, got bool" in e
               for e in bs.validate(doc))


def test_outcome_must_be_a_known_class():
    doc = _valid()
    doc["baselines"]["oracle"]["outcome"] = "solved"
    errs = bs.validate(doc)
    assert any(e.startswith("$.baselines.oracle.outcome: 'solved' is not one of")
               for e in errs)
    doc["baselines"]["oracle"]["outcome"] = 7
    assert any(e.startswith("$.baselines.oracle.outcome: expected str | null")
               for e in bs.validate(doc))


def test_optional_field_types():
    doc = _valid()
    doc["baselines"]["oracle"]["artifact"] = "report"
    doc["baselines"]["oracle"]["met"] = "yes"
    doc["baselines"]["oracle"]["checks"]["C0"]["detail"] = 4
    errs = bs.validate(doc)
    assert any(e.startswith("$.baselines.oracle.artifact: expected dict | null")
               for e in errs)
    assert any(e.startswith("$.baselines.oracle.met: expected bool | null")
               for e in errs)
    assert any(e.startswith("$.baselines.oracle.checks.C0.detail:") for e in errs)


def test_validate_does_not_mutate():
    doc = _valid()
    before = copy.deepcopy(doc)
    bs.validate(doc)
    assert doc == before


# -- the §5 table ---------------------------------------------------------------

def test_required_table_covers_both_roles_with_the_template_names():
    assert set(bs.REQUIRED_BASELINES) == set(bs.EXPECTED_OUTCOME) == set(reward.ROLES)
    names = {"oracle", "positive_control", "no_op", "checklist", "over_acting",
             "over_withholding", "acted_unresolved", "collateral"}
    for role in reward.ROLES:
        assert set(bs.REQUIRED_BASELINES[role]) == names
        assert set(bs.EXPECTED_OUTCOME[role]) == names
        for cls in bs.EXPECTED_OUTCOME[role].values():
            assert cls in outcome.OUTCOMES and cls not in ("unmeasured",)


def test_expected_outcomes_follow_the_template():
    c, d = bs.EXPECTED_OUTCOME["control"], bs.EXPECTED_OUTCOME["decision"]
    assert c["oracle"] == d["oracle"] == "resolved"
    assert c["positive_control"] == d["positive_control"] == "resolved"
    assert c["no_op"] == d["no_op"] == "silent"
    assert c["checklist"] == d["checklist"] == "false_claim"
    # the one row that differs by role: over-acting IS the oracle on control
    assert c["over_acting"] == "resolved" and d["over_acting"] == "boundary_violated"
    assert c["over_withholding"] == d["over_withholding"] == "withheld_with_evidence"
    assert c["acted_unresolved"] == d["acted_unresolved"] == "acted_unresolved"
    assert c["collateral"] == d["collateral"] == "boundary_violated"


def test_missing_required():
    assert bs.missing_required({"oracle": {}, "no_op": {}}, "control") == [
        "positive_control", "checklist", "over_acting", "over_withholding",
        "acted_unresolved", "collateral"]
    assert bs.missing_required({n: {} for n in bs.REQUIRED_BASELINES["decision"]},
                               "decision") == []
    import pytest
    with pytest.raises(ValueError):
        bs.missing_required({}, "referee")
