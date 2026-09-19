"""progress.py - the bounded product-progress term (DECISION_TEMPLATE §9).

What is proved: normalisation between the two measured anchors, the [0, 1)
ceiling, skip-not-zero on an absent quantity with renormalised weights, every
refusal branch of the loader, anchors read from the baseline JSON, and the
reachability check.
"""
from __future__ import annotations

import pytest

from progress import (ProgressSpec, anchors_from_baselines, episode_progress,
                      parse_progress_spec, reachability)

RAW = {"rows_repaired": {"check": "R1", "field": "repaired", "noop": 0, "oracle": "max"},
       "weights": {"rows_repaired": 1.0}}

TWO = {"rows_repaired": {"check": "R1", "field": "repaired", "noop": 0, "oracle": "max"},
       "nights_correct": {"check": "R2", "field": "correct", "noop": 0, "oracle": 4},
       "weights": {"rows_repaired": 0.7, "nights_correct": 0.3}}


def row(passed: bool, **detail) -> dict:
    return {"points": 2 if passed else 0, "max": 2, "passed": passed,
            "detail": detail}


# -- the given cases ------------------------------------------------------------

def test_progress_normalised_between_noop_and_oracle():
    spec = parse_progress_spec(RAW, known_ids=["R1"])
    anchors = {"rows_repaired": {"noop": 0.0, "oracle": 40.0}}
    checks = {"R1": {"points": 1, "max": 2, "passed": False, "detail": {"repaired": 10}}}
    assert episode_progress(spec, checks, anchors) == pytest.approx(0.25)


def test_progress_clamped_below_one():
    spec = parse_progress_spec(RAW, known_ids=["R1"])
    anchors = {"rows_repaired": {"noop": 0.0, "oracle": 40.0}}
    checks = {"R1": {"points": 2, "max": 2, "passed": True, "detail": {"repaired": 40}}}
    assert episode_progress(spec, checks, anchors) < 1.0


def test_progress_none_when_quantity_absent():
    spec = parse_progress_spec(RAW, known_ids=["R1"])
    assert episode_progress(spec, {"R1": {"points": 0, "max": 2, "passed": False}},
                            {"rows_repaired": {"noop": 0, "oracle": 40}}) is None


def test_refuses_unknown_check_id():
    with pytest.raises(ValueError):
        parse_progress_spec(RAW, known_ids=["X9"])


def test_refuses_weights_not_summing_to_one():
    bad = dict(RAW, weights={"rows_repaired": 0.5})
    with pytest.raises(ValueError):
        parse_progress_spec(bad, known_ids=["R1"])


def test_anchors_from_baseline_json():
    spec = parse_progress_spec(RAW, known_ids=["R1"])
    bj = {"baselines": {"no_op": {"checks": {"R1": {"detail": {"repaired": 0}}}},
                        "oracle": {"checks": {"R1": {"detail": {"repaired": 40}}}}}}
    assert anchors_from_baselines(spec, bj) == {"rows_repaired": {"noop": 0.0, "oracle": 40.0}}


def test_reachability_flags_degenerate():
    assert reachability([1.0, 1.0, 1.0])["distinct"] == 1
    assert reachability([0.0, 0.25, 1.0])["var"] > 0


# -- the loader -----------------------------------------------------------------

def test_loads_into_a_progress_spec():
    spec = parse_progress_spec(TWO, known_ids={"R1", "R2"})
    assert isinstance(spec, ProgressSpec)
    assert set(spec.quantities) == {"rows_repaired", "nights_correct"}
    assert spec.quantities["rows_repaired"]["oracle"] == "max"
    assert spec.quantities["nights_correct"]["oracle"] == 4.0
    assert spec.weights == {"rows_repaired": 0.7, "nights_correct": 0.3}


def test_single_quantity_defaults_its_weight_to_one():
    raw = {"rows_repaired": RAW["rows_repaired"]}
    assert parse_progress_spec(raw, known_ids=["R1"]).weights == {"rows_repaired": 1.0}


def test_two_quantities_without_weights_are_refused():
    raw = {k: v for k, v in TWO.items() if k != "weights"}
    with pytest.raises(ValueError, match="weights"):
        parse_progress_spec(raw, known_ids=["R1", "R2"])


def test_known_ids_none_skips_the_check_id_validation():
    assert parse_progress_spec(RAW, known_ids=None).quantities["rows_repaired"]["check"] == "R1"


def test_refuses_unknown_check_names_the_quantity():
    with pytest.raises(ValueError, match="rows_repaired.*'R1'"):
        parse_progress_spec(RAW, known_ids=["X9"])


def test_refuses_weight_for_undeclared_quantity():
    bad = dict(RAW, weights={"rows_repaired": 0.5, "ghost": 0.5})
    with pytest.raises(ValueError, match="ghost"):
        parse_progress_spec(bad, known_ids=["R1"])


def test_refuses_weights_omitting_a_quantity():
    bad = dict(TWO, weights={"rows_repaired": 1.0})
    with pytest.raises(ValueError, match="nights_correct"):
        parse_progress_spec(bad, known_ids=["R1", "R2"])


def test_refuses_non_positive_weight():
    bad = dict(TWO, weights={"rows_repaired": 1.0, "nights_correct": 0.0})
    with pytest.raises(ValueError, match="nights_correct.*> 0"):
        parse_progress_spec(bad, known_ids=["R1", "R2"])
    bad = dict(TWO, weights={"rows_repaired": 1.2, "nights_correct": -0.2})
    with pytest.raises(ValueError, match="nights_correct"):
        parse_progress_spec(bad, known_ids=["R1", "R2"])


def test_refuses_non_numeric_weight():
    bad = dict(RAW, weights={"rows_repaired": "all"})
    with pytest.raises(ValueError, match="rows_repaired.*number"):
        parse_progress_spec(bad, known_ids=["R1"])


def test_weights_within_tolerance_are_accepted():
    ok = dict(TWO, weights={"rows_repaired": 0.7, "nights_correct": 0.3 + 5e-7})
    assert parse_progress_spec(ok, known_ids=["R1", "R2"]).weights["nights_correct"] > 0.3


@pytest.mark.parametrize("anchor", ["noop", "oracle"])
def test_refuses_anchor_that_is_neither_number_nor_literal(anchor):
    bad = {"rows_repaired": dict(RAW["rows_repaired"], **{anchor: "median"}),
           "weights": {"rows_repaired": 1.0}}
    with pytest.raises(ValueError, match=f"rows_repaired.{anchor}"):
        parse_progress_spec(bad, known_ids=["R1"])
    bad = {"rows_repaired": dict(RAW["rows_repaired"], **{anchor: True}),
           "weights": {"rows_repaired": 1.0}}
    with pytest.raises(ValueError, match=f"rows_repaired.{anchor}"):
        parse_progress_spec(bad, known_ids=["R1"])


@pytest.mark.parametrize("anchor", ["noop", "oracle"])
def test_refuses_missing_anchor(anchor):
    q = {k: v for k, v in RAW["rows_repaired"].items() if k != anchor}
    with pytest.raises(ValueError, match=f"rows_repaired.*`{anchor}`"):
        parse_progress_spec({"rows_repaired": q}, known_ids=["R1"])


@pytest.mark.parametrize("key", ["check", "field"])
def test_refuses_quantity_lacking_check_or_field(key):
    q = {k: v for k, v in RAW["rows_repaired"].items() if k != key}
    with pytest.raises(ValueError, match=f"rows_repaired.*`{key}`"):
        parse_progress_spec({"rows_repaired": q}, known_ids=["R1"])
    q = dict(RAW["rows_repaired"], **{key: ""})
    with pytest.raises(ValueError, match=f"rows_repaired.*`{key}`"):
        parse_progress_spec({"rows_repaired": q}, known_ids=["R1"])


def test_refuses_quantity_that_is_not_a_table():
    with pytest.raises(ValueError, match="rows_repaired"):
        parse_progress_spec({"rows_repaired": "R1"}, known_ids=["R1"])


def test_refuses_unknown_quantity_key():
    q = dict(RAW["rows_repaired"], brevity=1)
    with pytest.raises(ValueError, match="rows_repaired.*brevity"):
        parse_progress_spec({"rows_repaired": q}, known_ids=["R1"])


def test_refuses_zero_quantities():
    with pytest.raises(ValueError, match="no quantities"):
        parse_progress_spec({}, known_ids=["R1"])
    with pytest.raises(ValueError, match="no quantities"):
        parse_progress_spec({"weights": {}}, known_ids=["R1"])


def test_refuses_non_table():
    with pytest.raises(ValueError):
        parse_progress_spec(["rows_repaired"], known_ids=["R1"])


# -- episode_progress -------------------------------------------------------------

ANCHORS_TWO = {"rows_repaired": {"noop": 0.0, "oracle": 40.0},
               "nights_correct": {"noop": 0.0, "oracle": 4.0}}


def test_weighted_mean_over_two_quantities():
    spec = parse_progress_spec(TWO, known_ids=["R1", "R2"])
    checks = {"R1": row(False, repaired=20), "R2": row(False, correct=1)}
    # 0.7 * 0.5 + 0.3 * 0.25
    assert episode_progress(spec, checks, ANCHORS_TWO) == pytest.approx(0.425)


def test_absent_quantity_renormalises_the_weights():
    spec = parse_progress_spec(TWO, known_ids=["R1", "R2"])
    # nights_correct has no detail: the 0.3 weight is dropped, not zeroed,
    # so rows_repaired alone decides and 20/40 reads 0.5 rather than 0.35.
    checks = {"R1": row(False, repaired=20), "R2": row(False)}
    assert episode_progress(spec, checks, ANCHORS_TWO) == pytest.approx(0.5)
    # the check row itself missing is the same as its detail missing
    assert episode_progress(spec, {"R1": row(False, repaired=20)},
                            ANCHORS_TWO) == pytest.approx(0.5)


def test_missing_anchor_skips_the_quantity():
    spec = parse_progress_spec(TWO, known_ids=["R1", "R2"])
    checks = {"R1": row(False, repaired=20), "R2": row(False, correct=1)}
    only_rows = {"rows_repaired": ANCHORS_TWO["rows_repaired"]}
    assert episode_progress(spec, checks, only_rows) == pytest.approx(0.5)


def test_degenerate_anchor_skips_the_quantity():
    spec = parse_progress_spec(TWO, known_ids=["R1", "R2"])
    checks = {"R1": row(False, repaired=20), "R2": row(False, correct=1)}
    anchors = dict(ANCHORS_TWO, nights_correct={"noop": 4.0, "oracle": 4.0})
    assert episode_progress(spec, checks, anchors) == pytest.approx(0.5)
    # both degenerate -> nothing usable
    both = {"rows_repaired": {"noop": 1, "oracle": 1},
            "nights_correct": {"noop": 4, "oracle": 4}}
    assert episode_progress(spec, checks, both) is None


def test_below_noop_clamps_to_zero():
    spec = parse_progress_spec(RAW, known_ids=["R1"])
    checks = {"R1": row(False, repaired=-5)}
    assert episode_progress(spec, checks, {"rows_repaired": {"noop": 0, "oracle": 40}}) == 0.0


def test_above_oracle_clamps_to_the_ceiling():
    spec = parse_progress_spec(RAW, known_ids=["R1"])
    checks = {"R1": row(True, repaired=400)}
    assert episode_progress(spec, checks, {"rows_repaired": {"noop": 0, "oracle": 40}}) == 0.999


def test_inverted_anchors_normalise_the_other_way():
    # oracle below noop (a relative error that the oracle drives to 0)
    spec = parse_progress_spec(
        {"rel_err": {"check": "R1", "field": "err", "noop": 1.0, "oracle": 0.0}},
        known_ids=["R1"])
    anchors = {"rel_err": {"noop": 1.0, "oracle": 0.0}}
    assert episode_progress(spec, {"R1": row(False, err=0.25)}, anchors) == pytest.approx(0.75)
    assert episode_progress(spec, {"R1": row(False, err=0.0)}, anchors) == 0.999
    assert episode_progress(spec, {"R1": row(False, err=2.0)}, anchors) == 0.0


def test_non_numeric_detail_is_absent():
    spec = parse_progress_spec(RAW, known_ids=["R1"])
    anchors = {"rows_repaired": {"noop": 0, "oracle": 40}}
    assert episode_progress(spec, {"R1": row(False, repaired="ten")}, anchors) is None
    assert episode_progress(spec, {"R1": row(False, repaired=True)}, anchors) is None
    assert episode_progress(spec, {"R1": row(False, repaired=None)}, anchors) is None


def test_result_is_rounded_to_four_places():
    spec = parse_progress_spec(RAW, known_ids=["R1"])
    anchors = {"rows_repaired": {"noop": 0, "oracle": 3}}
    assert episode_progress(spec, {"R1": row(False, repaired=1)}, anchors) == 0.3333


# -- anchors_from_baselines ----------------------------------------------------------

BJ = {"schema_version": 1, "task": "GT00x", "variant": "decision",
      "baselines": {
          "no_op": {"checks": {"R1": {"detail": {"repaired": 0}},
                               "R2": {"detail": {"correct": 0}}}},
          "oracle": {"checks": {"R1": {"detail": {"repaired": 40}},
                                "R2": {"detail": {"correct": 4}}}},
          "rival": {"checks": {"R1": {"detail": {"repaired": 55}},
                               "R2": {"detail": {"correct": -1}}}},
      }}


def test_max_and_min_read_across_every_baseline_not_only_the_oracle():
    spec = parse_progress_spec(
        {"rows": {"check": "R1", "field": "repaired", "noop": "min", "oracle": "max"},
         "nights": {"check": "R2", "field": "correct", "noop": "min", "oracle": "max"},
         "weights": {"rows": 0.5, "nights": 0.5}}, known_ids=["R1", "R2"])
    anchors = anchors_from_baselines(spec, BJ)
    assert anchors["rows"] == {"noop": 0.0, "oracle": 55.0}      # rival beat the oracle
    assert anchors["nights"] == {"noop": -1.0, "oracle": 4.0}    # rival below no_op


def test_numeric_anchor_is_used_as_declared():
    spec = parse_progress_spec(TWO, known_ids=["R1", "R2"])
    anchors = anchors_from_baselines(spec, BJ)
    assert anchors["nights_correct"] == {"noop": 0.0, "oracle": 4.0}
    assert anchors["rows_repaired"] == {"noop": 0.0, "oracle": 55.0}


def test_missing_field_in_every_baseline_is_refused_by_quantity_name():
    spec = parse_progress_spec(
        {"ghost": {"check": "R1", "field": "nope", "noop": 0, "oracle": "max"}},
        known_ids=["R1"])
    with pytest.raises(ValueError, match="'ghost'.*oracle"):
        anchors_from_baselines(spec, BJ)


def test_baseline_json_without_baselines_table_is_refused():
    spec = parse_progress_spec(RAW, known_ids=["R1"])
    with pytest.raises(ValueError, match="baselines"):
        anchors_from_baselines(spec, {"schema_version": 1})
    with pytest.raises(ValueError, match="baselines"):
        anchors_from_baselines(spec, None)


def test_anchors_feed_episode_progress_end_to_end():
    spec = parse_progress_spec(RAW, known_ids=["R1"])
    anchors = anchors_from_baselines(spec, BJ)          # oracle = max = 55
    assert episode_progress(spec, {"R1": row(False, repaired=11)}, anchors) == 0.2


# -- reachability ---------------------------------------------------------------------

def test_reachability_shape():
    r = reachability([0.0, 0.25, 1.0])
    assert r["n"] == 3 and r["min"] == 0.0 and r["max"] == 1.0
    assert r["distinct"] == 3
    # population variance of {0, .25, 1}: mean 5/12
    assert r["var"] == pytest.approx(((0 - 5 / 12) ** 2 + (0.25 - 5 / 12) ** 2
                                      + (1 - 5 / 12) ** 2) / 3, abs=1e-6)


def test_reachability_empty():
    assert reachability([]) == {"n": 0, "var": None, "min": None, "max": None,
                                "distinct": None}


def test_reachability_distinct_rounds_to_four_places():
    assert reachability([0.12341, 0.12342, 0.5])["distinct"] == 2
