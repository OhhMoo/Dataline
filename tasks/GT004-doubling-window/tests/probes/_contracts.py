#!/usr/bin/env python3
"""
LAYER 1 WIRING - GT004's lifecycle gates, decided by `tasks/contracts`.

    from ._contracts import evaluate_contract, remap, params_for

`tasks/contracts` (README.md) holds seven invariants that were LIFTED OUT of
the checks GT004-GT008 each wrote one-off.  Two of them came out of this task:
`liveness` out of G2, and `exactly_once` out of G1b and G3.  A library lifted
from a task and then never called BY that task is a second copy of the rule, and
this repository has already paid for second copies (`_score.py`'s header, the
scoring fold, three times).  So the probes no longer decide anything: they
configure the contract, hand it the world, and re-weight the clauses it returns
onto the rubric line the manifest owns.

THE DIVISION OF LABOUR, and it is exactly the one grade.py already states:

  the CONTRACT decides the verdict         (passed / failed, and why)
  this task decides what the verdict COSTS (the clause weights, from the
                                            manifest's probe_clauses block)

That split is why the clause weights are NOT taken from the contract.  GT004's
G3 weights its three late-arrival clauses 0.45 / 0.30 / 0.25 and the contract
weights the same three 0.45 / 0.30 / 0.30 - the contract is describing an
invariant, this task is describing a rubric, and the rubric is the manifest's to
set.  `remap` therefore reads the verdict off the contract clause and the weight
off the caller, so the points a baseline scores do not move by a hundredth.

NOT-EVALUABLE IS A PACKAGING FAULT HERE.  A contract that cannot decide returns
one clause `<name>.not_evaluable` (_types.not_evaluable).  Everything GT004 can
put it in that state - a plant date the pipeline has already seen, a predicate
that does not isolate the planted rows, a missing pipeline step - is a FIXTURE
defect, and the probe has always reported those as `probe not configured`, which
grade.py turns into an exit-2 packaging error rather than a low score.  `remap`
preserves that: it is the one branch where the mapping is not a re-weighting.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ._common import Clause, ProbeNotConfigured, not_configured, score_clauses

TASK_ROOT = Path(__file__).resolve().parents[2]
REPO = TASK_ROOT.parent.parent


def _contracts():
    """`tasks.contracts`, imported by repository path.

    The probes are loaded by path (grade.py's `load_probe`), so the repository
    root is not necessarily importable when this module is reached.  Inserting
    it here rather than at import time keeps the package importable in a
    checkout where `tasks/contracts/` has not been built yet - the failure then
    lands on the probe that asked for it, with a message, instead of on every
    probe at once.
    """
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        from tasks import contracts                              # noqa: PLC0415
    except Exception as exc:                                     # noqa: BLE001
        raise ProbeNotConfigured(
            f"tasks/contracts is not importable from {REPO} "
            f"({type(exc).__name__}: {exc}); GT004 declares its invariants in "
            f"task.toml [composition].contracts and does not carry a second "
            f"copy of them") from None
    return contracts


def params_for(world: dict, cfg: dict, steps: list, kind: str) -> dict:
    """The contract's `params` block, built from GT004's own probe config.

    Nothing is invented: `world` is `_common.world_config`'s output (the fact
    table, business key, date column, audit columns and window that grade.py
    pinned into every probe), `cfg` is `CONTRACT["probes"][kind]` overlaid by
    the manifest - the same dict the probe used to read directly - and `steps`
    is whatever `_common.normalise_steps` resolved, which is the one piece the
    probe resolves through three fallbacks and the contract must not re-derive.

    `kind` is "liveness" (the plant) or "exactly_once" (the late payload).
    """
    shared = {
        "table": world["fact_table"],
        "business_key": list(world["business_key"]),
        "date_column": world["date_column"],
        "audit_columns": list(world.get("audit_columns") or []),
        "window": dict(world["window"]),
        "pipeline": {"steps": list(steps)},
    }
    if kind == "liveness":
        return {**shared, "plant": cfg["plant"], "runner": "pipeline"}
    if kind == "exactly_once":
        payload = dict(cfg["payload"])
        return {**shared, "late": {"predicate": payload.pop("predicate"),
                                   "advance_to": cfg.get("advance_to"),
                                   "payload": payload}}
    raise ProbeNotConfigured(f"no contract params shape for {kind!r}")


def evaluate_contract(name: str, world_root: Path, params: dict,
                      before: dict | None = None) -> dict:
    """{clause id -> Clause}, from `tasks.contracts.REGISTRY[name]`.

    The module's `evaluate` never raises - every contract wraps its own body and
    turns a refusal into a `not_evaluable` clause - so the caller always gets a
    dict and decides what an absent clause means.
    """
    contracts = _contracts()
    from tasks.contracts._types import Context                    # noqa: PLC0415

    ctx = Context(world_root=Path(world_root), before=dict(before or {}),
                  params=dict(params))
    try:
        return {c.id: c for c in contracts.REGISTRY[name].evaluate(ctx)}
    finally:
        ctx.close()


def not_evaluable_detail(name: str, clauses: dict) -> str | None:
    """The refusal message, when the contract declined to decide. Else None."""
    key = f"{name}.not_evaluable"
    return clauses[key].detail if key in clauses else None


def remap(probe_id: str, name: str, clauses: dict, spec: list,
          max_points: float, extra: dict | None = None):
    """Fold contract clauses onto this task's rubric line.

    `spec` is [(gt004 clause name, contract clause id, weight, fatal)] in the
    order the manifest's `probe_clauses` block lists them, and the fold is
    `_common.score_clauses` - the same one the probes have always used, and the
    one `tasks/contracts/tests/test_score_equivalence.py` proves the contract
    package's own copy agrees with.

    A contract clause the caller asked for and the contract did not emit is
    scored FAILING with a message naming it, never skipped.  That is the case a
    fatal gate creates on purpose: `exactly_once` returns its `stressed` gate
    alone when the shipped world carried no duplicate key, and `liveness`
    returns `ingests` alone when the fact table is not there.  Skipping the
    absent clauses would rescale the fold over whatever survived and pay for a
    world the contract refused to score.
    """
    refusal = not_evaluable_detail(name, clauses)
    if refusal is not None:
        return not_configured(probe_id, max_points, refusal)
    folded = []
    for local, cid, weight, fatal in spec:
        got = clauses.get(cid)
        if got is None:
            folded.append(Clause(local, False, weight,
                                 f"the {name} contract emitted no clause {cid} "
                                 f"for this world; the clauses it did emit are "
                                 f"{sorted(clauses)}", fatal=fatal))
        else:
            folded.append(Clause(local, bool(got.passed), weight, got.detail,
                                 fatal=fatal))
    detail = {"contract": name,
              "contract_clauses": {cid: c.as_dict() for cid, c in clauses.items()}}
    detail.update(extra or {})
    return score_clauses(probe_id, folded, max_points, detail)
