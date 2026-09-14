"""Aggregate rollout records into the per-task difficulty report.

The number on its own is not the deliverable. Pass@1 without the failure
breakdown, the quarantine count and the per-check pass rates is a story; with
them it is a measurement. So the report refuses to print a bare pass rate:

  * the DENOMINATOR is stated every time, and there are two of them
  * quarantined rollouts are counted and excluded, never silently dropped
  * per-check pass rates say which sub-checks models actually cleared, which is
    what tells us how to harden the task
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
    surfaces_dead = sorted({lbl for r in records
                            for lbl, s in (r.get("surfaces", {}).get("surfaces") or {}).items()
                            if not s.get("live")})

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
        "blindness_guarantees": sorted({r.get("blindness_guarantee")
                                        for r in records if r.get("blindness_guarantee")}),
    }


def render(task: str, run_id: str, agg: dict, records: list[dict]) -> str:
    L = [f"# {task} - rollout report ({run_id})", ""]
    L.append(f"subject: {', '.join(agg['models']) or 'unknown'} "
             f"via driver {', '.join(agg['drivers'])} "
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
    L.append("")
    p = agg["pass_at_1"]
    L.append(f"**Pass@1 = {p if p is not None else 'n/a'}** "
             f"over {agg['scored']} scored rollouts")
    if agg["pass_at_1_reasoning_only"] is not None:
        L.append(f"Pass@1 (reasoning-attributable only) = "
                 f"{agg['pass_at_1_reasoning_only']}")
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
        L.append(f"**dead surfaces in this world:** {agg['dead_surfaces']} - any "
                 "failure attributable to these is a world gap, not difficulty")
    if agg["unprobed_tools"]:
        L.append("")
        L.append(f"**{len(agg['unprobed_tools'])} live tools the task does not probe:** "
                 f"{agg['unprobed_tools']} - a failure on any of these cannot be "
                 "attributed to a dead surface and will land on the agent as "
                 "`reasoning`.")
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
