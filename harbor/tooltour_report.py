#!/usr/bin/env python3
"""Tabulate a Harbor jobs directory of the tool tour, per tool and per probe.

    python3 harbor/tooltour_report.py harbor/dist/tooltour-jobs [--json out.json]
    python3 harbor/tooltour_report.py harbor/dist/tooltour-jobs/<job-name>

For every trial (a `result.json` with a `trial_name`) under the directory,
from Harbor's result.json and the tool-tour verifier's own account
(`verifier/grading.json`, harbor/template/tooltour_tests/verify_tool.py):

  reward          Harbor's verifier reward (none: the trial was not measured -
                  a verifier error - or raised)
  expected tool   did every expected tool leave an audit row above the
                  watermark (grading.json audit.expected_used / checks.tools)
  unknown_route   audit rows whose tool ends in `.unknown_route`: the agent
                  called a vendor route the fake does not serve - a fidelity
                  gap, whatever the reward
  wall time       the agent phase (agent_execution), in seconds

Per tool: trials, mean reward, how many used the expected tool, the
unknown_route rows (and which routes), median agent wall time. Stdlib only.
Exit 0 (it reports; it does not judge).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path


def _seconds(span: dict | None) -> float | None:
    if not isinstance(span, dict) or not span.get("started_at") or not span.get("finished_at"):
        return None
    try:
        start = datetime.fromisoformat(span["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(span["finished_at"].replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((end - start).total_seconds(), 1)


def _load(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def trial_rows(jobs: Path) -> list[dict]:
    rows = []
    for result_path in sorted(jobs.rglob("result.json")):
        result = _load(result_path)
        if not result.get("trial_name"):
            continue                                  # a job-level summary
        trial = result_path.parent
        grading = _load(trial / "verifier" / "grading.json")
        audit = grading.get("audit") or {}
        checks = grading.get("checks") or {}
        task = str(result.get("task_name") or "")
        probe = grading.get("probe") or task.split("/", 1)[-1].rsplit("__", 1)[0]
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        exc = result.get("exception_info") or {}
        expected = audit.get("expected_used")
        if expected is None and "tools" in checks:
            expected = bool(checks["tools"].get("passed"))
        rows.append({
            "job": trial.parent.name, "trial": result.get("trial_name"), "task": task,
            "probe": probe, "tool": grading.get("tool") or probe.split(".", 1)[0],
            "agent": (result.get("agent_info") or {}).get("name"),
            "model": ((result.get("agent_info") or {}).get("model_info") or {}).get("name"),
            "reward": rewards.get("reward"),
            "measured": grading.get("measured"),
            "unmeasured": (grading.get("unmeasured") or {}).get("code"),
            "exception": (f"{exc.get('exception_type')}: "
                          f"{(exc.get('exception_message') or '')[:200]}") if exc else None,
            "expected_tool_used": expected,
            "checks": {k: bool(v.get("passed")) for k, v in checks.items()
                       if isinstance(v, dict)},
            "tools_used": audit.get("tools") or [],
            "unknown_route_rows": int(audit.get("unknown_route_rows") or 0),
            "unknown_routes": audit.get("unknown_route") or [],
            "answer": (grading.get("answer") or {}).get("value"),
            "truth": (grading.get("truth") or {}).get("value"),
            "agent_seconds": _seconds(result.get("agent_execution")),
            "trial_seconds": _seconds(result),
        })
    return rows


def per_tool(rows: list[dict]) -> list[dict]:
    out = []
    for tool in sorted({r["tool"] for r in rows}):
        mine = [r for r in rows if r["tool"] == tool]
        rewards = [float(r["reward"]) for r in mine if isinstance(r["reward"], (int, float))]
        seconds = [r["agent_seconds"] for r in mine if r["agent_seconds"] is not None]
        out.append({
            "tool": tool, "trials": len(mine),
            "probes": sorted({r["probe"] for r in mine}),
            "mean_reward": round(sum(rewards) / len(rewards), 3) if rewards else None,
            "rewarded": sum(1 for x in rewards if x == 1),
            "unmeasured_or_errored": sum(1 for r in mine if r["reward"] is None),
            "expected_tool_used": sum(1 for r in mine if r["expected_tool_used"]),
            "unknown_route_rows": sum(r["unknown_route_rows"] for r in mine),
            "unknown_routes": sorted({u for r in mine for u in r["unknown_routes"]}),
            "median_agent_seconds": statistics.median(seconds) if seconds else None,
        })
    return out


def _fmt(value, width: int) -> str:
    text = "-" if value is None else str(value)
    return text[:width].ljust(width)


def render(rows: list[dict], tools: list[dict]) -> str:
    lines = [f"{len(rows)} trial(s), {len(tools)} tool(s)", "",
             f"{'tool':<12}{'trials':>7}{'reward':>8}{'=1':>5}{'used':>6}"
             f"{'unk.rt':>8}{'agent s':>9}  unknown routes"]
    for t in tools:
        lines.append(f"{t['tool']:<12}{t['trials']:>7}{_fmt(t['mean_reward'], 7):>8}"
                     f"{t['rewarded']:>5}{t['expected_tool_used']:>6}"
                     f"{t['unknown_route_rows']:>8}{_fmt(t['median_agent_seconds'], 8):>9}"
                     f"  {', '.join(t['unknown_routes']) or '-'}")
    lines += ["", f"{'probe':<48}{'reward':>7}{'used':>6}{'unk':>5}{'agent s':>9}  "
              f"answer / truth / note"]
    for r in sorted(rows, key=lambda r: (r["probe"], r["trial"] or "")):
        note = r["exception"] or (f"unmeasured: {r['unmeasured']}" if r["unmeasured"]
                                  else f"{r['answer']!r} / {r['truth']!r}")
        lines.append(f"{r['probe'][:47]:<48}{_fmt(r['reward'], 6):>7}"
                     f"{_fmt(r['expected_tool_used'], 5):>6}{r['unknown_route_rows']:>5}"
                     f"{_fmt(r['agent_seconds'], 8):>9}  {str(note)[:80]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("jobs", type=Path, help="a jobs directory (-o) or one job in it")
    ap.add_argument("--json", type=Path, default=None, help="also write the tables here")
    args = ap.parse_args(argv)
    if not args.jobs.is_dir():
        print(f"tooltour_report: {args.jobs} is not a directory", file=sys.stderr)
        return 2
    rows = trial_rows(args.jobs)
    tools = per_tool(rows)
    print(render(rows, tools))
    if args.json:
        args.json.write_text(json.dumps({"jobs": str(args.jobs), "tools": tools,
                                         "trials": rows}, indent=2) + "\n",
                             encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
