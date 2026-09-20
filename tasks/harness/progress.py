"""Bounded, product-only PROGRESS inside the two partial outcome classes
(docs/evalScoringRedesign.md §3, tasks/contracts/DECISION_TEMPLATE.md §9).

reward.py lands an `acted_unresolved` episode (and a control-arm
`withheld_with_evidence`) at `0 + PROGRESS_SPAN * progress`. This module is
where `progress` comes from, and the two things it refuses to be are the two
failure modes the literature measured:

  * it is never a fraction of checks passed (VeRPO's cardinality bias: eight
    report keys out-vote one row count in any mean). Progress is a weighted
    mean over QUANTITIES the task declares - rows repaired, nights correct,
    receipts matching - each read out of one grader check's `detail` dict
    under a named field, and weighted by quantity, not by number of checks;
  * it is never typed. `noop` and `oracle` are the anchors of a RE-Bench style
    normalisation `(x - noop) / (oracle - noop)`, and they are READ from the
    baseline JSON `run_baselines.py --json` writes (DECISION_TEMPLATE §5), so
    an author cannot set the ceiling where the number looks best.

The result is clamped to [0, 0.999]: progress lives INSIDE the partial band and
can never lift an episode across a band boundary (the CoRPO ordering
invariant, by construction rather than by test).

Declared per arm:

    [variants.decision.matrix.progress]
    rows_repaired  = {check = "R_rows",   field = "repaired", noop = 0, oracle = "max"}
    nights_correct = {check = "R_nights", field = "correct",  noop = 0, oracle = "max"}
    weights        = {rows_repaired = 0.7, nights_correct = 0.3}     # sum to 1

`noop` / `oracle` are either a number (used as-is) or the literal "max" /
"min", which takes the max / min of that field across EVERY baseline in the
file - the oracle's value is usually the max, but the declaration says
"whatever the best baseline reached", not a number somebody remembered.

WHAT `parse_progress_spec` REFUSES (ValueError, naming the key): a `check`
the caller cannot vouch for; a quantity lacking `check` or `field`; a `noop`
/ `oracle` that is neither a number nor "max"/"min"; `weights` naming a
quantity that does not exist or omitting one that does; weights that do not
sum to 1 (± 1e-6); a weight <= 0; a table with no quantities. `weights` may
be omitted only when there is exactly one quantity (it is then 1.0).

`reachability` is the RLVP check (docs/evalScoringRedesign.md §3): a quantity
that takes a single value across every baseline is a bit in disguise and
validity.py --ladder refuses to pay for it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

ANCHOR_LITERALS = ("max", "min")
WEIGHTS_KEY = "weights"
QUANTITY_KEYS = ("check", "field", "noop", "oracle")
_TOL = 1e-6
_DP = 4
# Progress may approach the band boundary but never reach it: 0.999 * SPAN
# stays strictly below the next band.
PROGRESS_CEILING = 0.999


@dataclass(frozen=True)
class ProgressSpec:
    """One arm's `[matrix.progress]` table.

    quantities  name -> {"check": str, "field": str,
                          "noop": float | "max" | "min",
                          "oracle": float | "max" | "min"}
    weights     name -> weight, summing to 1 over `quantities`
    """
    quantities: dict[str, dict]
    weights: dict[str, float]


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _anchor_value(value, where: str, key: str):
    if _is_number(value):
        return float(value)
    if isinstance(value, str) and value in ANCHOR_LITERALS:
        return value
    raise ValueError(
        f"{where}.{key} must be a number or one of {list(ANCHOR_LITERALS)} "
        f"(the max/min of the field across every baseline), got {value!r}")


def parse_progress_spec(raw: dict, known_ids, where: str = "[matrix.progress]"
                        ) -> ProgressSpec:
    """A task.toml `[matrix.progress]` table -> ProgressSpec, or ValueError.

    `known_ids`: the grader check ids the caller can vouch for (taskspec
    passes the union of `[matrix.map]` and the grader manifest's check
    table). A quantity reading a check the grader never emits would be
    unmeasured on every episode, so it is refused here rather than printed
    as None forever.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a table of quantity -> "
                         f"{{check, field, noop, oracle}}, got {raw!r}")
    known = set(known_ids) if known_ids is not None else None

    quantities: dict[str, dict] = {}
    for name, entry in raw.items():
        if name == WEIGHTS_KEY:
            continue
        if not isinstance(entry, dict):
            raise ValueError(
                f"{where}.{name} must be a table with check, field, noop and "
                f"oracle, got {entry!r}")
        extra = sorted(set(entry) - set(QUANTITY_KEYS))
        if extra:
            raise ValueError(
                f"{where}.{name} has unknown key(s) {extra}; a quantity is "
                f"exactly {list(QUANTITY_KEYS)}")
        for required in ("check", "field"):
            if not isinstance(entry.get(required), str) \
                    or not entry[required].strip():
                raise ValueError(
                    f"{where}.{name} lacks a `{required}` string. A progress "
                    f"quantity is one grader check's detail field, so both "
                    f"have to be named.")
        for anchor in ("noop", "oracle"):
            if anchor not in entry:
                raise ValueError(
                    f"{where}.{name} lacks `{anchor}`. The normalisation is "
                    f"(x - noop) / (oracle - noop); state a number or "
                    f"\"max\"/\"min\".")
        check = entry["check"]
        if known is not None and check not in known:
            raise ValueError(
                f"{where}.{name} reads check {check!r}, which the grader "
                f"does not emit. Known: {sorted(known)}. A quantity on a "
                f"check that never appears is unmeasured on every episode.")
        quantities[str(name)] = {
            "check": check,
            "field": entry["field"],
            "noop": _anchor_value(entry["noop"], f"{where}.{name}", "noop"),
            "oracle": _anchor_value(entry["oracle"], f"{where}.{name}",
                                    "oracle"),
        }
    if not quantities:
        raise ValueError(
            f"{where} declares no quantities. A progress table with nothing "
            f"to measure would pay a constant; drop the table instead.")

    raw_weights = raw.get(WEIGHTS_KEY)
    if raw_weights is None:
        if len(quantities) != 1:
            raise ValueError(
                f"{where} declares {len(quantities)} quantities "
                f"{sorted(quantities)} and no `weights`. Weights may be "
                f"omitted only for a single quantity; with several, the "
                f"split is a declaration, not a default.")
        raw_weights = {next(iter(quantities)): 1.0}
    if not isinstance(raw_weights, dict):
        raise ValueError(f"{where}.weights must be a table of quantity -> "
                         f"weight, got {raw_weights!r}")
    unknown = sorted(set(raw_weights) - set(quantities))
    if unknown:
        raise ValueError(
            f"{where}.weights names {unknown}, which are not declared "
            f"quantities. Declared: {sorted(quantities)}.")
    missing = sorted(set(quantities) - set(raw_weights))
    if missing:
        raise ValueError(
            f"{where}.weights omits {missing}. Every quantity carries a "
            f"declared weight; an implied one would be a silent share of "
            f"the score.")
    weights: dict[str, float] = {}
    for name, w in raw_weights.items():
        if not _is_number(w):
            raise ValueError(
                f"{where}.weights.{name} must be a number, got {w!r}")
        if w <= 0:
            raise ValueError(
                f"{where}.weights.{name} = {w}; a weight must be > 0. A "
                f"quantity that weighs nothing should not be declared, and "
                f"a negative one would be a penalty in disguise.")
        weights[str(name)] = float(w)
    total = sum(weights.values())
    if abs(total - 1.0) > _TOL:
        raise ValueError(
            f"{where}.weights sum to {total!r}, not 1. The weighted mean is "
            f"only bounded in [0,1) when the weights are a partition.")
    return ProgressSpec(quantities=quantities, weights=weights)


def _detail_value(checks: dict, check: str, field: str):
    """`checks[check]["detail"][field]` when present and numeric, else None."""
    if not isinstance(checks, dict):
        return None
    row = checks.get(check)
    if not isinstance(row, dict):
        return None
    detail = row.get("detail")
    if not isinstance(detail, dict):
        return None
    value = detail.get(field)
    return float(value) if _is_number(value) else None


def episode_progress(spec: ProgressSpec, checks: dict,
                     anchors: dict) -> float | None:
    """One episode's progress in [0, 0.999], or None when nothing was usable.

    checks   a grader / rollout record `checks` mapping: id -> {"points",
             "max", "passed", "detail": {...}}
    anchors  quantity -> {"noop": float, "oracle": float}, from
             `anchors_from_baselines`

    A quantity is skipped - not zeroed - when its value is absent from the
    record, when its anchor is missing, or when `oracle == noop` (the
    baselines do not separate on it, so no normalisation exists). The weights
    are RENORMALISED over the quantities that were usable, so a missing
    quantity does not silently read as zero progress on it.
    """
    weighted, weight_sum = 0.0, 0.0
    for name, q in spec.quantities.items():
        anchor = anchors.get(name) if isinstance(anchors, dict) else None
        if not isinstance(anchor, dict):
            continue
        noop, oracle = anchor.get("noop"), anchor.get("oracle")
        if not _is_number(noop) or not _is_number(oracle) or oracle == noop:
            continue
        value = _detail_value(checks, q["check"], q["field"])
        if value is None:
            continue
        x = (value - float(noop)) / (float(oracle) - float(noop))
        x = min(max(x, 0.0), PROGRESS_CEILING)
        w = spec.weights[name]
        weighted += w * x
        weight_sum += w
    if weight_sum == 0.0:
        return None
    return round(min(weighted / weight_sum, PROGRESS_CEILING), _DP)


def anchors_from_baselines(spec: ProgressSpec, baselines_json: dict) -> dict:
    """quantity -> {"noop": float, "oracle": float}, read from the baseline
    JSON `run_baselines.py --json` writes (DECISION_TEMPLATE §5):

        {"baselines": {"no_op": {"checks": {"R1": {"detail": {"repaired": 0}}}},
                       "oracle": {"checks": {...}}, ...}}

    A numeric `noop` / `oracle` in the spec is used as declared; "max"/"min"
    takes the max / min of the field across EVERY baseline in the file.
    Raises ValueError naming the quantity when a needed value is missing -
    an anchor that is not there is a normalisation nobody measured.
    """
    table = (baselines_json or {}).get("baselines") \
        if isinstance(baselines_json, dict) else None
    if not isinstance(table, dict):
        raise ValueError("baseline JSON carries no `baselines` table "
                         "(DECISION_TEMPLATE §5 schema)")

    def read(baseline: str, check: str, field: str, name: str, anchor: str):
        entry = table.get(baseline)
        value = _detail_value((entry or {}).get("checks") or {}, check, field) \
            if isinstance(entry, dict) else None
        if value is None:
            raise ValueError(
                f"progress quantity {name!r}: anchor `{anchor}` needs "
                f"baselines.{baseline}.checks.{check}.detail.{field}, which "
                f"the baseline JSON does not carry (or is not numeric).")
        return value

    def extreme(literal: str, check: str, field: str, name: str, anchor: str):
        values = [v for b in table.values() if isinstance(b, dict)
                  for v in [_detail_value(b.get("checks") or {}, check, field)]
                  if v is not None]
        if not values:
            raise ValueError(
                f"progress quantity {name!r}: anchor `{anchor}` = "
                f"\"{literal}\" but no baseline carries checks.{check}."
                f"detail.{field}.")
        return max(values) if literal == "max" else min(values)

    out: dict[str, dict] = {}
    for name, q in spec.quantities.items():
        anchors = {}
        for anchor, baseline in (("noop", "no_op"), ("oracle", "oracle")):
            declared = q[anchor]
            if isinstance(declared, str):
                anchors[anchor] = extreme(declared, q["check"], q["field"],
                                          name, anchor)
            elif _is_number(declared):
                anchors[anchor] = float(declared)
            else:  # unreachable after parse_progress_spec; kept honest
                anchors[anchor] = read(baseline, q["check"], q["field"],
                                       name, anchor)
        out[name] = anchors
    return out


def reachability(values: list[float]) -> dict:
    """RLVP's Var_G check over one quantity's values across baselines or
    rollouts: {"n", "var" (population), "min", "max", "distinct" (values
    rounded to 4 dp)}. Empty -> n 0 and the rest None."""
    nums = [float(v) for v in (values or []) if _is_number(v)]
    n = len(nums)
    if n == 0:
        return {"n": 0, "var": None, "min": None, "max": None,
                "distinct": None}
    mean = sum(nums) / n
    var = sum((v - mean) ** 2 for v in nums) / n
    return {"n": n, "var": round(var, 6), "min": min(nums), "max": max(nums),
            "distinct": len({round(v, _DP) for v in nums})}
