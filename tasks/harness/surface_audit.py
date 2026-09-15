"""Pre-episode surface audit: which of this world's surfaces are actually there.

WHY THIS EXISTS. GT004 ran five scored Opus rollouts in a world where eight of
the agent's surfaces answered `no_issue_tracker` / `no_chat` /
`no_knowledge_base` / `no_observability` / `no_ingest_state`. The harness
PRINTED that - at the bottom of the report, under the number - and did nothing
with it. The audit log shows the subject calling four of those surfaces exactly
once, getting nothing, and never looking again. The scores were real; the world
they were measured in was half absent, and a Pass@1 measured in a half-absent
world looks exactly like a Pass@1 measured in a whole one.

So a dead surface is a run OUTCOME here, not a footnote:

  required + dead   the rollout never starts. `episode.HarnessError`, classified
                    `harness_error`, never scored. This follows blindness.py's
                    precedent rather than inventing a mechanism: a compromised
                    episode is quarantined instead of being let into the number.
                    The justification is different from blindness's but no
                    weaker - the subject was never given the world the task
                    describes, so whatever it scored is not a measurement OF
                    that task.

  dead_by_design    declared in task.toml, with a reason. A deliberately minimal
                    world must be declarable or it is flagged forever, and a
                    flag that fires forever stops being read.

  dead, neither     the rollout RUNS - we do not know the surface mattered - but
                    the run is NOT a clean measurement, and report.py says so
                    above the rate instead of below it. Silence in exactly this
                    cell is what produced GT004.

THE CEILING. `surface_probes` is what the task thought to check; it is a floor,
not a ceiling. Every gateway tool the task does not probe is swept too, called
with no arguments, so a dead surface cannot hide behind a tool nobody declared.
A tool that needs arguments cannot be swept blind - that is recorded as
`unreachable`, never silently counted as live. Measured basis for the sweep
being read-only in fact (gateway/.venv, 2026-09-14): of the gateway's 63 tools
exactly 23 validate against `{}`, and of those only dbt_run and dbt_test mutate
or cost anything, so both are denied below. Every other tool rejects `{}` at
pydantic validation with `invalid_arguments` - before any handler runs.

Probes, swept or declared, run against a THROWAWAY COPY of the world with their
own audit directory (episode.probe_surfaces / container_shim.cmd_probe). That is
what makes the sweep safe structurally rather than by naming convention.
"""
from __future__ import annotations

import re

# An error code that says THE WORLD HAS NO SUCH SURFACE, as opposed to "your
# call was wrong". Read out of the gateway, not guessed:
#   gateway/src/alkera_gateway/ingest.py:34          no_ingest_state
#   gateway/src/alkera_gateway/observability.py:538  no_observability
#   gateway/src/alkera_gateway/workflow.py:118       no_issue_tracker
#   gateway/src/alkera_gateway/workflow.py:296       no_chat
#   gateway/src/alkera_gateway/workflow.py:433       no_knowledge_base
ABSENT_SURFACE_CODES = frozenset({
    "no_ingest_state", "no_observability", "no_issue_tracker",
    "no_chat", "no_knowledge_base",
})

# A `no_*` code this table does not know is SUSPECT, never automatically fatal.
# The gateway also raises `no_output` for a scheduler run that captured none -
# a fact about one run, not about an absent surface - so a regex that gated on
# its own would quarantine a healthy world. Suspects are reported and reviewed.
SUSPECT_ABSENT = re.compile(r"^no_[a-z0-9_]+$")

# The probe call could not be FORMED, so it says nothing about the world.
INCONCLUSIVE_CODES = frozenset({"invalid_arguments", "unknown_tool",
                                "request_too_large"})

# Swept with `{}`, these would actually run: dbt_run/dbt_test mutate the copy and
# cost minutes. dbt_compile already speaks for both via `covers` on every task
# that probes dbt at all.
SWEEP_DENY = frozenset({"dbt_run", "dbt_test"})


def declared_tools(probes: list[dict]) -> set[str]:
    """Every tool a task's own probes speak for, directly or via `covers`."""
    out = set()
    for p in probes or ():
        out.add(p["tool"])
        out.update(p.get("covers") or ())
    return out


def sweep_probes(toolbelt, probes: list[dict]) -> list[dict]:
    """Zero-argument probes for every tool the task did not declare.

    Marked `declared: false` so the audit never confuses "the task checked this"
    with "we checked this because the task did not".
    """
    covered = declared_tools(probes)
    return [{"label": f"~{tool}", "tool": tool, "args": {}, "declared": False}
            for tool in sorted(set(toolbelt) - covered - SWEEP_DENY)]


def all_probes(toolbelt, probes: list[dict]) -> list[dict]:
    declared = [{**p, "declared": True} for p in (probes or ())]
    return declared + sweep_probes(toolbelt, probes)


def _verdict(surface: dict) -> str:
    """live | dead | unreachable | suspect, from one probe result."""
    if surface.get("live"):
        return "live"
    code = surface.get("error_code")
    if code is None:
        # pre-audit rollouts, and transport failures, carry only the json blob
        blob = surface.get("error") or ""
        for known in ABSENT_SURFACE_CODES:
            if known in blob:
                return "dead"
        for known in INCONCLUSIVE_CODES:
            if known in blob:
                return "unreachable" if not surface.get("declared", True) else "dead"
        return "dead" if surface.get("declared", True) else "unreachable"
    if code in ABSENT_SURFACE_CODES:
        return "dead"
    if code in INCONCLUSIVE_CODES:
        # a DECLARED probe that will not even validate is the task's bug and is
        # still a surface nobody can use, so it stays dead. A SWEPT one just
        # means we could not form a blind call.
        return "dead" if surface.get("declared", True) else "unreachable"
    if SUSPECT_ABSENT.match(code):
        return "suspect"
    # any other error on a declared probe: the task asked, the world said no.
    return "dead" if surface.get("declared", True) else "suspect"


def audit(*, spec, surfaces: dict, toolbelt) -> dict:
    """The run condition, computed once per rollout, before the subject runs."""
    got = (surfaces or {}).get("surfaces") or {}
    probed = bool((surfaces or {}).get("probed"))

    by_verdict: dict[str, list[str]] = {"live": [], "dead": [],
                                        "unreachable": [], "suspect": []}
    tools_of: dict[str, set[str]] = {}
    for label, s in got.items():
        by_verdict[_verdict(s)].append(label)
        tools_of[label] = {s.get("tool", label)} | set(s.get("covers") or ())
    for k in by_verdict:
        by_verdict[k].sort()

    dead = set(by_verdict["dead"])

    # required_surfaces may name a probe LABEL or any tool that probe speaks
    # for; taskspec.load has already refused anything that names neither.
    required = list(spec.required_surfaces)
    required_labels = sorted({lbl for lbl in got
                              for r in required
                              if r == lbl or r in tools_of.get(lbl, ())})
    dead_required = sorted(set(required_labels) & dead)

    by_design = {d["label"]: d["reason"] for d in spec.dead_by_design}
    dead_by_design = sorted(set(by_design) & dead)
    stale_by_design = sorted(set(by_design) & set(by_verdict["live"]))
    dead_undeclared = sorted(dead - set(by_design) - set(dead_required))

    covered = declared_tools(spec.surface_probes)
    swept = {s.get("tool") for lbl, s in got.items() if not s.get("declared", True)}
    unprobed = sorted(set(toolbelt) - covered - swept)
    # swept but the call could not be formed: known-unknown, not live
    unswept = sorted({got[l].get("tool", l) for l in by_verdict["unreachable"]})

    if not probed:
        gate, reason = "unprobed", (
            "the task declares no surface_probes, so no surface was verified "
            "live before the subject was handed the terminal")
    elif dead_required:
        gate, reason = "fail", (
            f"required surfaces are DEAD in this world: {dead_required}. The "
            f"subject was never given the world the task describes, so anything "
            f"it scored is not a measurement of this task.")
    elif not required:
        gate, reason = "not_declared", (
            "the task declares no [harness].required_surfaces, so the gate had "
            "nothing to enforce; no dead surface can be fatal here until it does")
    else:
        gate, reason = "pass", (
            f"all {len(required_labels)} required surfaces live: {required_labels}")

    not_clean = []
    if gate == "fail":
        not_clean.append(reason)
    if gate == "unprobed":
        not_clean.append(reason)
    if dead_undeclared:
        not_clean.append(
            f"dead surfaces this task neither requires nor declares dead by "
            f"design: {dead_undeclared}. Each is a hole in the world the agent "
            f"was measured in, and the task has not said whether it matters.")
    if by_verdict["suspect"]:
        not_clean.append(
            f"surfaces that answered an unrecognised absence-shaped error: "
            f"{by_verdict['suspect']}")

    return {
        "probed": probed,
        "gate": gate,
        "gate_reason": reason,
        "clean": not not_clean,
        "not_clean_because": not_clean,
        "live": by_verdict["live"],
        "dead": sorted(dead),
        "suspect": by_verdict["suspect"],
        "unreachable_by_sweep": by_verdict["unreachable"],
        "required": required,
        "required_labels": required_labels,
        "dead_required": dead_required,
        "dead_by_design": dead_by_design,
        "dead_by_design_declared": sorted(by_design),
        "dead_by_design_but_live": stale_by_design,
        "dead_undeclared": dead_undeclared,
        "declared_probes": sum(1 for s in got.values() if s.get("declared", True)),
        "swept_probes": sum(1 for s in got.values() if not s.get("declared", True)),
        "unprobed_tools": unprobed,
        "unswept_tools": unswept,
    }


def gate_error(a: dict) -> str | None:
    """The HarnessError text when this rollout must not start, else None.

    Only `fail` stops a rollout. `unprobed` and `not_declared` are conditions on
    the REPORT, not on the run: refusing to run a task that has not yet declared
    its requirements would make this module decide which tasks exist, and that
    is the task's call, not the harness's.
    """
    if a.get("gate") != "fail":
        return None
    return ("dead required surface: " + a["gate_reason"] +
            " Declare it in [harness].dead_by_design (with a reason) if the "
            "world is meant to be without it, or fix the world builder.")
