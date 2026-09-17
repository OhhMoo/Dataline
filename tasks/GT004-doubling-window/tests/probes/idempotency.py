#!/usr/bin/env python3
"""
GT004 gate G2 - idempotency, with a mandatory liveness clause.

    from probes import idempotency
    result = idempotency.run(world_root, contract=CONTRACT,
                             config=manifest.get("idempotency_probe"),
                             max_points=8)

Behavioural, not static.  A copy of the graded world is made, a partition the
pipeline has NEVER seen is planted in it, and the agent's own pipeline files are
executed against the copy - twice.

WHO DECIDES WHAT, since 2026-09-16.  The three clauses are no longer implemented
here: they are `tasks/contracts/liveness.py`, which was lifted out of this very
probe when the contract library was built (`liveness.DOC`: "LIFTED FROM GT004
G2").  A library lifted out of a task and then not called by it is a second copy
of the rule, and second copies in this repository drift.  So this module
CONFIGURES the contract from `CONTRACT["probes"]`, hands it the world, and
re-weights the clauses it returns onto G2's rubric line:

    G2.liveness       <- liveness.ingests        0.45, FATAL
    G2.no-collateral  <- liveness.no_collateral  0.20, FATAL
    G2.idempotence    <- liveness.idempotent     0.35

The weights are the manifest's (`checks[].probe_clauses`), not the contract's:
the contract describes an invariant, the manifest describes a rubric.  The
verdicts are the contract's.  Neither number moves - `tests/VALIDITY.md` carries
the before/after per-check table for all twelve baselines and it is empty.

What the three clauses mean, unchanged:

  * LIVENESS (fatal).  The planted partition is ingested exactly ONCE.  A load
    "fixed" into a permanent skip - a watermark pinned in the past, a
    `WHERE 1=0`, a guard that decides the table is already loaded - is idempotent
    by construction and repairs nothing.  It must score ZERO, not full marks, so
    this clause is fatal and a failure zeroes the gate outright.

  * NO COLLATERAL (fatal, and it GATES without paying).  The window the agent
    repaired is unchanged by a run that had nothing to do with it.  It is true of
    a world nobody repaired - the stock loader only ever touches the newest
    landing day - so paying for it handed the no_op baseline 2.91 of G2's 8
    points (measured 2026-09-14).  As a gate it still carries its meaning in the
    direction that matters: a loader that disturbs the repaired window while
    ingesting an unrelated partition is the original defect recurring.

  * IDEMPOTENCE.  A second identical run changes no BUSINESS column.  Audit
    columns (load_ts, batch_id, ...) are excluded by contract: they legitimately
    differ after any repair, and including them would turn this into "wrote
    nothing", which is the failure the liveness clause exists to catch.

NOT through `sched_trigger`.  `FileScheduler.trigger` reports success without
executing for every command that is not dbt_build/dbt_run
(gateway/src/alkera_gateway/surfaces.py:69-82), so a run count of two there
proves nothing ran at all.  `tasks/contracts/liveness.py` refuses any `runner`
but `pipeline` for the same reason.
"""
from __future__ import annotations

from pathlib import Path

from ._common import (ProbeNotConfigured, crashed, normalise_steps,
                      not_configured, probe_config, require, world_config)
from ._contracts import evaluate_contract, params_for, remap

PROBE_ID = "G2"
CONTRACT_NAME = "liveness"

# (local clause name, contract clause id, weight, fatal) - the manifest's
# `checks[id=G2].probe_clauses` block, in the order it lists them.
CLAUSES = [
    ("liveness", "liveness.ingests", 0.45, True),
    ("no-collateral", "liveness.no_collateral", 0.20, True),
    ("idempotence", "liveness.idempotent", 0.35, False),
]


def run(world_root, *, contract: dict | None = None, config: dict | None = None,
        max_points: float = 8.0, keep_dir: Path | None = None) -> "object":
    """Gate G2.

    world_root  - the post-episode world root (contains var/ and workspace/).
                  Never written to; the contract copies it first.
    contract    - fixtures/fixture_contract.py::CONTRACT, or None.
    config      - the grading manifest's `idempotency_probe` block; overlays
                  CONTRACT['probes']['idempotency'].
    max_points  - the gate's weight in the rubric.
    keep_dir    - accepted for signature compatibility with the other probes and
                  unused: the contract owns its own scratch directory and
                  removes it.  Named rather than dropped because grade.py passes
                  only the kwargs a probe declares, so dropping it would silently
                  change what the grader hands this module.
    """
    del keep_dir                       # the contract owns its scratch directory
    world_root = Path(world_root)
    try:
        w = world_config(contract, config)
        cfg = probe_config(contract, config, "idempotency")
        require(cfg, "plant", where="probes.idempotency")
        require(cfg["plant"], "date", "rows", "predicate",
                where="probes.idempotency.plant")
        steps = normalise_steps(cfg.get("steps")
                                or ((contract or {}).get("probes") or {})
                                .get("pipeline", {}).get("steps")
                                or (config or {}).get("steps"))
        if not steps:
            return not_configured(PROBE_ID, max_points,
                                  "no pipeline steps given (probes.pipeline.steps)")
        params = params_for(w, cfg, steps, CONTRACT_NAME)
    except ProbeNotConfigured as exc:
        return not_configured(PROBE_ID, max_points, str(exc))

    try:
        clauses = evaluate_contract(CONTRACT_NAME, world_root, params)
        return remap(PROBE_ID, CONTRACT_NAME, clauses, CLAUSES, max_points,
                     {"planted": f"{params['plant']['rows']} row(s) for "
                                 f"{params['plant']['date']} "
                                 f"(mode={params['plant'].get('mode', 'sql')})",
                      "steps": [s["path"] for s in steps]})
    except Exception as exc:                            # noqa: BLE001 - scored
        return crashed(PROBE_ID, max_points, exc)
