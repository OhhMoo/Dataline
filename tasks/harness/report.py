"""Aggregate rollout records into the per-task difficulty report.

The number on its own is not the deliverable. Pass@1 without the failure
breakdown, the quarantine count and the per-check pass rates is a story; with
them it is a measurement. So the report refuses to print a bare pass rate:

  * the DENOMINATOR is stated every time, and there are three of them
  * quarantined rollouts are counted and excluded, never silently dropped
  * per-check pass rates say which sub-checks models actually cleared, which is
    what tells us how to harden the task

RUN CONDITIONS, and why they are above the number rather than below it. GT004's
report printed `dead surfaces in this world: [alarms, chat, dbt, deploy, ingest,
issues, kb, metrics]` - eight of the agent's surfaces absent - at the BOTTOM,
under a Pass@1 that read like any other Pass@1. A pass rate whose world is not
described is not a result, and describing it after the reader has taken the
number is the same as not describing it. So the conditions block comes first and
the rate carries its live-surface set inline, and when the conditions are bad
enough the rate is WITHHELD rather than printed with a caveat.

The prompt variant belongs in the same block for the same reason
(`GT004/prompts/variant.py identify` already establishes that a run must name
which ticket produced it): world and ticket are the two halves of "what was this
measured in".

PASS^K. Pass@1 says how often the model gets it right. Pass^k says whether it
gets it right EVERY time, which is the property a product needs and the one that
has already discriminated here: on GT001 Claude scored Pass@1 1.0 and Pass^5
1.0, while Kimi scored Pass@1 0.5-0.75 and Pass^k 0. It costs nothing - the
world is byte-identical across K by construction - so the only reason it was
missing is that nobody computed it.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

CLASSES = ("pass", "reasoning", "tool_gap", "timeout", "budget",
           "grader_defect", "harness_error", "blindness_violation", "unobserved")


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


def _pass_hat_k(records: list[dict]) -> dict:
    """Did the model pass ALL of them?

    Over SCORED rollouts, and k is stated, because the alternative definitions
    are both wrong. Counting a quarantined rollout as a failure blames the model
    for the harness; counting it as a pass is obvious nonsense. So k shrinks, and
    the report says when k < K - a Pass^3 of a K=5 run is a weaker claim than the
    run set out to make, and the reader has to be able to see that.
    """
    scored = [r for r in records if r.get("scored")]
    passed = [r for r in scored if r.get("passed")]
    return {
        "pass_hat_k": (None if not scored
                       else (1.0 if len(passed) == len(scored) else 0.0)),
        "pass_hat_k_k": len(scored),
        "pass_hat_k_attempted": len(records),
        "pass_hat_k_complete": bool(scored) and len(scored) == len(records),
        "pass_hat_k_passed": len(passed),
    }


def _run_conditions(records: list[dict]) -> dict:
    """What this run was measured IN. Everything here can invalidate the rate."""
    audits = [r.get("surface_audit") for r in records]
    have = [a for a in audits if a]
    missing = len(records) - len(have)

    def union(key):
        return sorted({x for a in have for x in (a.get(key) or ())})

    # The world is byte-identical across K by construction, so the live set
    # should be too. If it is not, that is a determinism defect and it is louder
    # than anything the surfaces themselves say.
    live_sets = {tuple(a.get("live") or ()) for a in have}
    variants = sorted({r.get("prompt_variant") for r in records
                       if r.get("prompt_variant")})
    prompt_hashes = sorted({r.get("prompt_sha256") for r in records
                            if r.get("prompt_sha256")})
    gates = sorted({a.get("gate") for a in have if a.get("gate")})

    conditions = {
        "prompt_variants": variants,
        "prompt_sha256": prompt_hashes,
        "surface_gate": gates,
        "surfaces_live": union("live"),
        "surfaces_dead": union("dead"),
        "surfaces_suspect": union("suspect"),
        "surfaces_dead_required": union("dead_required"),
        "surfaces_dead_by_design": union("dead_by_design"),
        "surfaces_dead_undeclared": union("dead_undeclared"),
        "surfaces_dead_by_design_but_live": union("dead_by_design_but_live"),
        "surfaces_required": union("required_labels"),
        "surfaces_unreachable_by_sweep": union("unreachable_by_sweep"),
        "rollouts_without_surface_audit": missing,
        "live_set_identical_across_k": len(live_sets) <= 1,
        "declared_probes": max([a.get("declared_probes", 0) for a in have] or [0]),
        "swept_probes": max([a.get("swept_probes", 0) for a in have] or [0]),
    }

    # WHY THE RATE MAY NOT BE PUBLISHED. Each entry is a fact about the run, not
    # a judgement about the model.
    bad: list[str] = []
    if any(r.get("redteam_detector_test") for r in records):
        bad.append("the run contains red-team DETECTOR TESTS, which are not "
                   "measurements of anything")
    if conditions["surfaces_dead_required"]:
        bad.append(f"required surfaces were DEAD: "
                   f"{conditions['surfaces_dead_required']}. Those rollouts were "
                   f"never given the world the task describes.")
    if conditions["surfaces_dead_undeclared"]:
        bad.append(f"surfaces were dead that the task neither requires nor "
                   f"declares dead by design: "
                   f"{conditions['surfaces_dead_undeclared']}. Until the task "
                   f"says which, a failure here is unattributable.")
    if "unprobed" in gates:
        bad.append("the task declares no surface_probes, so no surface was "
                   "verified live before the subject was handed the terminal")
    if missing:
        bad.append(f"{missing} rollout(s) carry no surface audit at all, so the "
                   f"world they ran in is undescribed")
    if not conditions["live_set_identical_across_k"]:
        bad.append("the live-surface set DIFFERS between rollouts of a run whose "
                   "world is supposed to be byte-identical across K")
    if len(variants) > 1 or len(prompt_hashes) > 1:
        bad.append(f"the run mixes prompts ({variants or prompt_hashes}); one "
                   f"rate over two tickets is not a rate for either")
    if any(v.startswith("UNLOCKED") for v in variants):
        bad.append(f"the brief matches no locked prompt variant ({variants}), so "
                   f"this run cannot be attributed to a ticket")

    # NOT an invalidation, but it must be said: a task that declares no
    # required_surfaces cannot fail the gate, so "gate: pass" is unavailable to
    # it and the absence of a dead-required finding means nothing.
    caveats = []
    if "not_declared" in gates:
        caveats.append("this task declares no [harness].required_surfaces, so "
                       "the surface gate had nothing to enforce")
    if conditions["surfaces_dead_by_design_but_live"]:
        caveats.append(f"dead_by_design names surfaces that are actually live "
                       f"({conditions['surfaces_dead_by_design_but_live']}); the "
                       f"declaration is stale")
    if conditions["surfaces_suspect"]:
        caveats.append(f"surfaces answered an absence-shaped error this harness "
                       f"does not know: {conditions['surfaces_suspect']}")
    conditions["caveats"] = caveats
    conditions["measurement_valid"] = not bad
    conditions["invalidated_because"] = bad
    return conditions


def _surface_tag(c: dict) -> str:
    """The live-surface set, short enough to sit at the end of every rate."""
    if c.get("rollouts_without_surface_audit") and not c["surfaces_live"]:
        return "surfaces: NOT AUDITED"
    n_live, n_dead = len(c["surfaces_live"]), len(c["surfaces_dead"])
    # named only while naming them stays readable; the full lists are in the
    # conditions block a few lines above, and a tag repeated on every rate line
    # has to be short enough that it is still read on the third one
    dead = c["surfaces_dead"]
    tail = f" ({', '.join(dead)})" if 0 < n_dead <= 4 else ""
    return f"surfaces: {n_live} live / {n_dead} dead{tail}"


def aggregate(records: list[dict]) -> dict:
    by_class = {c: 0 for c in CLASSES}
    for r in records:
        by_class[r.get("classification", "harness_error")] = \
            by_class.get(r.get("classification", "harness_error"), 0) + 1

    scored = [r for r in records if r.get("scored")]
    passed = [r for r in scored if r.get("passed")]
    reasoning = [r for r in scored if r.get("classification") == "reasoning"]
    quarantined = [r for r in records
                   if r.get("classification") in ("blindness_violation", "unobserved")]
    caught = [r for r in records if r.get("classification") == "blindness_violation"]
    unobserved = [r for r in records if r.get("classification") == "unobserved"]
    excluded = [r for r in records if not r.get("scored")]

    # per-check pass rates, over scored rollouts only
    checks: dict[str, dict] = {}
    for r in scored:
        for cid, c in (r.get("checks") or {}).items():
            e = checks.setdefault(cid, {"max": c["max"], "passed": 0, "n": 0,
                                        "points": []})
            e["n"] += 1
            e["passed"] += 1 if c["passed"] else 0
            e["points"].append(c["points"])
    for cid, e in checks.items():
        e["pass_rate"] = round(e["passed"] / e["n"], 3) if e["n"] else None
        e["mean_points"] = round(sum(e["points"]) / len(e["points"]), 2) if e["points"] else None
        e.pop("points")

    totals = [(r.get("score") or {}).get("total") for r in scored]
    totals = [t for t in totals if t is not None]
    mx = next((( r.get("score") or {}).get("max") for r in scored
               if r.get("score")), None)

    unprobed = sorted({t for r in records for t in (r.get("unprobed_tools") or [])})
    conditions = _run_conditions(records)
    surfaces_dead = conditions["surfaces_dead"]

    # Pass^k per model as well as overall: one Pass^k over a run that mixed two
    # models says nothing about either, and the GT001 comparison this metric
    # exists for (Claude 1.0 vs Kimi 0) is exactly that comparison.
    by_model = {}
    for m in sorted({r.get("model") for r in records if r.get("model")}):
        by_model[m] = _pass_hat_k([r for r in records if r.get("model") == m])

    return {
        "rollouts": len(records),
        "scored": len(scored),
        "quarantined": len(quarantined),
        "quarantined_caught_violating": len(caught),
        "quarantined_unobserved": len(unobserved),
        "redteam_detector_tests": len([r for r in records
                                       if r.get("redteam_detector_test")]),
        "excluded_from_pass_at_1": len(excluded),
        "pass_at_1": round(len(passed) / len(scored), 3) if scored else None,
        "pass_at_1_denominator": "scored rollouts (attempted, not quarantined, "
                                 "no harness or grader defect)",
        "pass_at_1_reasoning_only": (
            round(len(passed) / (len(passed) + len(reasoning)), 3)
            if (len(passed) + len(reasoning)) else None),
        "pass_at_1_reasoning_only_denominator":
            "passes + reasoning failures only; excludes timeout, budget and "
            "tool_gap. Upper bound on what the task measures about reasoning.",
        **_pass_hat_k(records),
        "pass_hat_k_denominator":
            "1.0 iff EVERY one of the k scored rollouts passed, else 0.0. k is "
            "the scored count, not K: a quarantined rollout counted as a failure "
            "would blame the model for the harness. When k < K the claim is "
            "weaker than the run intended and `pass_hat_k_complete` is false.",
        "pass_hat_k_by_model": by_model,
        # Nothing below this line may be read without the block above it.
        "run_conditions": conditions,
        "measurement_valid": conditions["measurement_valid"],
        "invalidated_because": conditions["invalidated_because"],
        "classification_counts": by_class,
        "score_distribution": {
            "max": mx,
            "totals": sorted(totals),
            "median": _median(totals),
            "mean": round(sum(totals) / len(totals), 2) if totals else None,
        },
        "per_check": checks,
        "tool_calls_median": _median([r.get("tool_calls") for r in scored]),
        "wall_clock_s_median": _median([r.get("wall_clock_s") for r in scored]),
        "dead_surfaces": surfaces_dead,
        "unprobed_tools": unprobed,
        "needs_review": [r["rollout_id"] for r in records if r.get("needs_review")],
        "models": sorted({r.get("model") for r in records if r.get("model")}),
        "drivers": sorted({r.get("driver") for r in records if r.get("driver")}),
        "topologies": sorted({r.get("topology", "host") for r in records}),
        "blindness_guarantees": sorted({r.get("blindness_guarantee")
                                        for r in records if r.get("blindness_guarantee")}),
    }


def render(task: str, run_id: str, agg: dict, records: list[dict]) -> str:
    L = [f"# {task} - rollout report ({run_id})", ""]
    L.append(f"subject: {', '.join(agg['models']) or 'unknown'} "
             f"via driver {', '.join(agg['drivers'])} "
             f"in topology {', '.join(agg.get('topologies') or ['host'])} "
             f"(blindness: {', '.join(agg['blindness_guarantees'])})")
    L.append("")
    L.append(f"K = {agg['rollouts']} rollouts, {agg['scored']} scored, "
             f"{agg['quarantined']} quarantined "
             f"({agg['quarantined_caught_violating']} caught reaching outside the "
             f"toolbelt, {agg['quarantined_unobserved']} unobservable), "
             f"{agg['excluded_from_pass_at_1'] - agg['quarantined']} otherwise excluded")
    if agg["redteam_detector_tests"]:
        L.append("")
        L.append(f"**{agg['redteam_detector_tests']} of these are red-team DETECTOR "
                 "TESTS, not measurements.** A run containing any of them is not a "
                 "Pass@1 measurement of anything.")

    # --- run conditions. ABOVE the number, deliberately. ------------------
    c = agg["run_conditions"]
    L.append("")
    L.append("## Run conditions")
    L.append("")
    L.append(f"- prompt variant: {', '.join(c['prompt_variants']) or 'not recorded'}")
    L.append(f"- surfaces live ({len(c['surfaces_live'])}): {c['surfaces_live']}")
    if c["surfaces_dead"]:
        L.append(f"- surfaces DEAD ({len(c['surfaces_dead'])}): {c['surfaces_dead']}")
    L.append(f"- surface gate: {', '.join(c['surface_gate']) or 'not run'}"
             + (f" · required {c['surfaces_required']}"
                if c["surfaces_required"] else " · no required_surfaces declared"))
    if c["surfaces_dead_required"]:
        L.append(f"- **required surfaces dead: {c['surfaces_dead_required']}** - "
                 "those rollouts are harness errors, not scores")
    if c["surfaces_dead_by_design"]:
        L.append(f"- dead by design (declared): {c['surfaces_dead_by_design']}")
    if c["surfaces_dead_undeclared"]:
        L.append(f"- **dead and undeclared: {c['surfaces_dead_undeclared']}** - "
                 "the task has not said whether these matter")
    L.append(f"- probes: {c['declared_probes']} declared by the task, "
             f"{c['swept_probes']} swept by the harness over the rest of the "
             f"toolbelt")
    for x in c["caveats"]:
        L.append(f"- caveat: {x}")

    L.append("")
    p = agg["pass_at_1"]
    tag = _surface_tag(c)
    if not agg["measurement_valid"]:
        L.append(f"**Pass@1 = WITHHELD - this run is not a clean measurement.** "
                 f"· {tag}")
        for why in agg["invalidated_because"]:
            L.append(f"  - {why}")
        L.append("")
        L.append(f"  (provisional, do not publish: {p if p is not None else 'n/a'} "
                 f"over {agg['scored']} scored rollouts · {tag})")
    else:
        L.append(f"**Pass@1 = {p if p is not None else 'n/a'}** "
                 f"over {agg['scored']} scored rollouts · {tag}")
    if agg["pass_at_1_reasoning_only"] is not None:
        L.append(f"Pass@1 (reasoning-attributable only) = "
                 f"{agg['pass_at_1_reasoning_only']} · {tag}")
    pk = agg["pass_hat_k"]
    L.append(f"Pass^k = {pk if pk is not None else 'n/a'} "
             f"(k = {agg['pass_hat_k_k']} scored of "
             f"{agg['pass_hat_k_attempted']} attempted; "
             f"{agg['pass_hat_k_passed']} passed) · {tag}")
    if not agg["pass_hat_k_complete"]:
        L.append("  - k < K: the excluded rollouts were not observed to pass, so "
                 "this is a weaker consistency claim than the run intended")
    if len(agg["pass_hat_k_by_model"]) > 1:
        L.append("")
        L.append("| model | Pass^k | k |")
        L.append("|---|---|---|")
        for m, e in agg["pass_hat_k_by_model"].items():
            L.append(f"| {m} | {e['pass_hat_k']} | {e['pass_hat_k_k']} |")
    L.append("")
    L.append("## Failure classification")
    L.append("")
    L.append("| class | n |")
    L.append("|---|---|")
    for c, n in agg["classification_counts"].items():
        if n:
            L.append(f"| {c} | {n} |")
    L.append("")
    L.append("## Score distribution")
    sd = agg["score_distribution"]
    L.append("")
    L.append(f"max {sd['max']} · median {sd['median']} · mean {sd['mean']} · "
             f"totals {sd['totals']}")
    L.append("")
    L.append("## Per-check pass rate (scored rollouts)")
    L.append("")
    L.append("| check | max | pass rate | mean points |")
    L.append("|---|---|---|---|")
    for cid in sorted(agg["per_check"]):
        e = agg["per_check"][cid]
        L.append(f"| {cid} | {e['max']} | {e['pass_rate']} | {e['mean_points']} |")
    L.append("")
    L.append(f"median tool calls {agg['tool_calls_median']} · "
             f"median wall clock {agg['wall_clock_s_median']}s")
    if agg["dead_surfaces"]:
        L.append("")
        L.append(f"dead surfaces in this world: {agg['dead_surfaces']} - any "
                 "failure attributable to these is a world gap, not difficulty. "
                 "See **Run conditions** above for which of them the task "
                 "requires, declares, or has said nothing about.")
    if agg["unprobed_tools"]:
        L.append("")
        L.append(f"**{len(agg['unprobed_tools'])} tools whose liveness is unknown:** "
                 f"{agg['unprobed_tools']} - neither declared by the task nor "
                 "reachable by the zero-argument sweep, so a failure on any of "
                 "them cannot be attributed to a dead surface and will land on "
                 "the agent as `reasoning`.")
    if agg["needs_review"]:
        L.append("")
        L.append(f"**needs human review:** {agg['needs_review']}")
    L.append("")
    L.append("## Per rollout")
    L.append("")
    L.append("| id | class | total | tool calls | wall s | why |")
    L.append("|---|---|---|---|---|---|")
    for r in records:
        L.append(f"| {r.get('rollout_id')} | {r.get('classification')} | "
                 f"{(r.get('score') or {}).get('total')} | {r.get('tool_calls')} | "
                 f"{r.get('wall_clock_s')} | {str(r.get('why'))[:110]} |")
    q = [r for r in records
         if r.get("classification") in ("blindness_violation", "unobserved")]
    if q:
        L.append("")
        L.append("## Quarantined")
        L.append("")
        for r in q:
            L.append(f"- `{r['rollout_id']}` ({r['classification']}): "
                     f"{r['blindness']['hard_kinds']}")
            for x in r["blindness"]["violations"][:6]:
                if x["hard"]:
                    L.append(f"  - [{x['detector']}] {x['evidence'][:200]}")
    return "\n".join(L) + "\n"


def write(out_dir: Path, task: str, run_id: str, records: list[dict]) -> dict:
    agg = aggregate(records)
    (out_dir / "report.json").write_text(
        json.dumps({"task": task, "run_id": run_id, "summary": agg,
                    "rollouts": records}, indent=2, default=str), encoding="utf-8")
    (out_dir / "report.md").write_text(render(task, run_id, agg, records),
                                       encoding="utf-8")
    return agg
