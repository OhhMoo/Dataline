#!/usr/bin/env python3
"""
GT004 gate G3 - late arrivals land INSIDE the window the agent already repaired.

    from probes import late_arrival
    result = late_arrival.run(world_root, contract=CONTRACT,
                              config=manifest.get("late_arrival_probe"),
                              max_points=7)

This is the gate that makes GT004 long-horizon.  The agent cannot finish, because
finishing changes what "finished" means: the repair has to still hold after the
world moved.

WHO DECIDES WHAT, since 2026-09-16.  The three clauses are `tasks/contracts/
exactly_once.py`, which was lifted out of this probe and G1b when the contract
library was built (`exactly_once.DOC`: "LIFTED FROM GT004 G1b (duplicate keys)
and G3 (late arrivals)").  This module CONFIGURES that contract from
`CONTRACT["probes"]["late_arrival"]` and re-weights the clauses it returns onto
G3's rubric line:

    G3.incorporation   <- exactly_once.late_incorporated_once  0.45, FATAL
    G3.no-re-doubling  <- exactly_once.no_re_doubling          0.30
    G3.stability       <- exactly_once.stable_on_replay        0.25

The weights are the manifest's, not the contract's, and they deliberately differ
from it: `exactly_once` weights the same three 0.45 / 0.30 / 0.30 because it is
describing an invariant, while this rubric line is 0.45 / 0.30 / 0.25 because
G2 already pays for stability against a NEW partition and G3's own contribution
is the re-doubling.  Reading the verdict from one place and the weight from the
other is what lets the library be shared without moving a single score.

The contract also emits `exactly_once.stressed` (the gate that refuses to pay
for a world that never carried a duplicate key) and `exactly_once.
no_duplicate_keys` (G1b's clause, scored in grade.py against the harness-side
connection).  Neither is mapped here; a clause scored twice would be the error
agent.md 8.10 records as G4 restating G1c.

The three clauses, unchanged:

  * INCORPORATION (fatal).  The late events are in the fact table exactly once.
    A repair that hardcoded "delete the window, reinsert from the snapshot I saw"
    wipes them on its next run and lands zero - it fails here, which is the whole
    point of the gate.  A genuine merge survives.
  * NO RE-DOUBLING.  Every day of the repaired window ends at
    (its pre-arrival count + that day's late events) and not a row more.  The
    original defect - an append-mode re-run doubling a window - reappears here if
    the agent hardened the arithmetic without hardening the write mode.
  * STABILITY.  Running again after the arrival still changes nothing.  G2 proves
    idempotency against a NEW partition; this proves it against a partition that
    already exists, which is the harder case and the one a merge key gets wrong.

RESTATEMENTS.  A payload event may carry `adds` as well as `rows`: `rows` is what
is DELIVERED and `adds` is what a correct merge puts INTO the fact table, which
is zero for a redelivery of a key the table already holds.  The contract defaults
`adds` to `rows` and GT004's payload publishes both, because counting the 70
restatements as arrivals made the gate expect 196 rows where a correct merge
produces 126 and zeroed G3 for every baseline including the oracle (measured
2026-09-14).

The `late_arrival` FAULT KIND is inert in this environment - `_kind_scan` globs
the cumulative folder with `min: 1`, so after day one a poll cannot fail.  Late
arrival is therefore modelled where it actually lives: in the ARRIVAL STREAM
(WORLD's payload, inserted into the raw landing table) and in the CLOCK
(var/sched/state.json's v-now).  No fault is injected and no job is triggered.
"""
from __future__ import annotations

from pathlib import Path

from ._common import (ProbeNotConfigured, crashed, normalise_steps,
                      not_configured, probe_config, require, world_config)
from ._contracts import evaluate_contract, params_for, remap

PROBE_ID = "G3"
CONTRACT_NAME = "exactly_once"

# (local clause name, contract clause id, weight, fatal) - the manifest's
# `checks[id=G3].probe_clauses` block, in the order it lists them.
CLAUSES = [
    ("incorporation", "exactly_once.late_incorporated_once", 0.45, True),
    ("no-re-doubling", "exactly_once.no_re_doubling", 0.30, False),
    ("stability", "exactly_once.stable_on_replay", 0.25, False),
]

# Where the contract reads "was this invariant ever stressed" from.
STRESSED_BY = ("defect_1", "duplicated_business_keys")


def _before(contract: dict | None) -> dict:
    """`{"duplicate_keys_before": N}` - the fact `exactly_once.stressed` reads.

    Reported for the record, and 0 when the fixture does not publish it, because
    G3 passes `allow_unstressed` (see `run`).  The gate that must NOT be allowed
    to pass unstressed is G1b's, and grade.py enforces that one twice over: its
    `preflight` refuses to run the whole grader unless the contract says the
    shipped world carries duplicate keys, and it evaluates the same contract
    with the gate armed.
    """
    node = contract or {}
    for part in STRESSED_BY:
        if not isinstance(node, dict) or part not in node:
            return {"duplicate_keys_before": 0}
        node = node[part]
    return {"duplicate_keys_before": int(node)}


def run(world_root, *, contract: dict | None = None, config: dict | None = None,
        max_points: float = 7.0, keep_dir: Path | None = None) -> "object":
    """Gate G3.  Same signature shape as every other probe in this package.

    world_root  - post-episode world root; the contract copies it, never writes
                  to it.
    contract    - CONTRACT from fixtures/fixture_contract.py.
    config      - manifest `late_arrival_probe` block, overlaying
                  CONTRACT['probes']['late_arrival'].
    max_points  - the gate's rubric weight.
    keep_dir    - accepted for signature compatibility and unused; the contract
                  owns its own scratch directory.
    """
    del keep_dir                       # the contract owns its scratch directory
    world_root = Path(world_root)
    try:
        w = world_config(contract, config)
        cfg = probe_config(contract, config, "late_arrival")
        require(cfg, "payload", where="probes.late_arrival")
        payload = cfg["payload"]
        require(payload, "events", "predicate", where="probes.late_arrival.payload")
        steps = normalise_steps(cfg.get("steps")
                                or ((contract or {}).get("probes") or {})
                                .get("pipeline", {}).get("steps")
                                or (config or {}).get("steps"))
        if not steps:
            return not_configured(PROBE_ID, max_points,
                                  "no pipeline steps given (probes.pipeline.steps)")
        # The one guard that stays HERE rather than moving into the contract:
        # `exactly_once` is happy to incorporate an event anywhere in the table,
        # and G3 only means something for events that land inside the window the
        # agent has already repaired.  That is a property of THIS rubric line.
        win = w["window"]
        outside = sorted({str(ev["date"]) for ev in payload["events"]
                          if not (str(win["start"]) <= str(ev["date"])
                                  <= str(win["end"]))})
        if outside:
            return not_configured(
                PROBE_ID, max_points,
                f"late_arrival payload dates {outside} fall OUTSIDE the "
                f"repaired window {win['start']}..{win['end']} - G3 only means "
                "anything for events that land inside it")
        params = params_for(w, cfg, steps, CONTRACT_NAME)
        # THE STRESSOR THIS GATE GRADES IS THE DELIVERY, NOT THE DOUBLING.
        # `exactly_once.stressed` is a fatal gate that refuses to pay for an
        # invariant the shipped world never stressed, and the thing it measures
        # - how many duplicate business keys the world carried before the
        # episode - is G1b's stressor.  G3's stressor is the late payload, which
        # the contract PLANTS itself, so it is stressed by construction and the
        # gate would only ever be deciding G1b's question a second time.  G1b
        # arms it (grade.py, `g1b_duplicates`), so the no-free-points rule is
        # kept exactly once rather than dropped or double-counted.
        params["allow_unstressed"] = True
        before = _before(contract)
    except ProbeNotConfigured as exc:
        return not_configured(PROBE_ID, max_points, str(exc))

    try:
        clauses = evaluate_contract(CONTRACT_NAME, world_root, params, before)
        delivered = sum(int(ev["rows"]) for ev in payload["events"])
        adds = sum(int(ev.get("adds", ev["rows"])) for ev in payload["events"])
        return remap(PROBE_ID, CONTRACT_NAME, clauses, CLAUSES, max_points,
                     {"late_rows_delivered": delivered,
                      "late_rows_expected": adds,
                      "advance_to": cfg.get("advance_to"),
                      "steps": [s["path"] for s in steps]})
    except Exception as exc:                            # noqa: BLE001 - scored
        return crashed(PROBE_ID, max_points, exc)
