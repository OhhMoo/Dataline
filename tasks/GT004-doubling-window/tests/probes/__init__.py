"""GT004 lifecycle probes - the five gates G2..G6.

Every probe is an importable module with one entry point, `run(...)`, returning a
`ProbeResult` (points / max / human-readable reason / structured detail).  None
of them mutates the graded world: each copies the world root first, and opens
every read-only handle through an aliased `ATTACH ... (READ_ONLY)`.

    from probes import idempotency, late_arrival, exposure, collateral, regression

    s.checks.append(idempotency.run(world_root, contract=CONTRACT,
                                    config=m.get("idempotency_probe"),
                                    max_points=6).as_check())

`ProbeResult.as_check()` returns the {id, points, max, passed, detail} dict the
GT002/GT003 graders already append to `Score.checks`, so wiring is one line per
gate.  `ProbeResult.detail` carries the per-clause breakdown for the report.

Two environment defects are routed around by every probe here, and neither is
GT004's to fix:
  * `sched_trigger` reports success WITHOUT executing unless the job's command is
    dbt_build/dbt_run or a selector is set (surfaces.py:69-82).  No probe
    triggers a job; they execute the agent's own pipeline files directly.
  * `late_arrival` faults are inert (`_kind_scan` globs with min: 1).  G3 models
    late arrival through the arrival stream and the v-clock instead.
"""
from ._common import (Clause, ProbeNotConfigured, ProbeResult,  # noqa: F401
                      copy_world, open_world, probe_config, run_pipeline,
                      warehouse_path, workspace_path)
from . import (collateral, exposure, idempotency, late_arrival,  # noqa: F401
               ledger, regression)

__all__ = ["idempotency", "late_arrival", "exposure", "collateral", "regression",
           "ledger", "ProbeResult", "Clause", "ProbeNotConfigured", "copy_world",
           "open_world", "probe_config", "run_pipeline", "warehouse_path",
           "workspace_path"]

# The gate -> module map GRADER wires.  Points are the DESIGN.md weights only as
# a default; grade.py owns the rubric and passes max_points explicitly.
GATES = {
    "G2": ("idempotency", "Idempotency, with a mandatory liveness clause"),
    "G3": ("late_arrival", "Late arrivals inside the repaired window"),
    "G4": ("exposure", "The downstream BI exposure resolves to the corrected number"),
    "G5": ("collateral", "Fingerprint section diff: nothing moved that was not asked to"),
    "G6": ("regression", "A test that fails pre-repair and passes post-repair"),
    # LAYER 4, and penalty-only: what a CONSUMER could see, at the moment it
    # could see it. Every other gate here reads the final world, which cannot
    # tell a pipeline that was right all along from one that published a wrong
    # number on Tuesday and corrected it on Thursday.
    "G7": ("ledger", "Every receipt the clock advanced over is what the correct "
                     "pipeline would have published at that vnow"),
}
