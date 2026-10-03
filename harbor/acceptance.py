#!/usr/bin/env python3
"""The day-one acceptance gate a lab runs on this dataset, run before we ship.

    python3 harbor/acceptance.py --dataset harbor/dist/alkera-de [-n 4]

Two real Harbor jobs over the generated dataset, on the local docker provider:

  oracle   every task's `solution/solve.sh` (the native oracle, run as the
           agent user through the agent's own surface) must score exactly 1;
  nop      doing nothing must score exactly 0.

Either job failing a task, raising a trial exception, or skipping a task that
`dataset.toml` lists fails the gate. The verdict of every trial (outcome class,
path penalty, the grader's misses) is read back from its
`verifier/grading.json`, so a failure says why, not only that.

Stdlib only; needs Python >= 3.11 and the `harbor` CLI. `--sg docker` runs
Harbor under `sg docker -c` for a shell that is not yet in the docker group.
Exit status: 0 = accepted, 1 = rejected, 2 = could not run.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path

EXPECTED = {"oracle": 1.0, "nop": 0.0}


def dataset_tasks(dataset: Path) -> list[str]:
    """Task names `dataset.toml` pins (what Harbor must run)."""
    manifest = tomllib.loads((dataset / "dataset.toml").read_text(encoding="utf-8"))
    return sorted(t["name"] for t in manifest.get("tasks", []))


def harbor_argv(args, agent: str, job_name: str) -> list[str]:
    argv = [*shlex.split(args.harbor), "run", "-p", str(args.dataset), "-a", agent,
            "-o", str(args.jobs_dir), "--job-name", job_name,
            "-n", str(args.concurrency), "-k", str(args.attempts), "-q"]
    for pattern in args.include:
        argv += ["-i", pattern]
    if args.sg:
        return ["sg", args.sg, "-c", shlex.join(argv)]
    return argv


def verdict_of(trial: Path) -> dict:
    """The verifier's own account of a trial (grading.json), when it wrote one."""
    path = trial / "verifier" / "grading.json"
    try:
        grading = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    verdict = grading.get("verdict") or {}
    missed = [cid for cid, row in (grading.get("checks") or {}).items()
              if isinstance(row, dict) and not row.get("passed")]
    return {"outcome": verdict.get("outcome"), "path_penalty": verdict.get("path_penalty"),
            "missed": missed}


def trials(job_dir: Path) -> list[dict]:
    """One row per trial: task, reward, exception, verdict."""
    rows = []
    for result in sorted(job_dir.glob("*/result.json")):
        data = json.loads(result.read_text(encoding="utf-8"))
        rewards = (data.get("verifier_result") or {}).get("rewards") or {}
        exc = data.get("exception_info") or {}
        rows.append({
            "task": data.get("task_name"),
            "trial": data.get("trial_name"),
            "reward": rewards.get("reward"),
            "exception": (f"{exc.get('exception_type')}: "
                          f"{(exc.get('exception_message') or '')[:300]}") if exc else None,
            **verdict_of(result.parent),
        })
    return rows


def judge(agent: str, rows: list[dict], expected_tasks: list[str]) -> list[str]:
    """Every reason this job fails the gate (empty = it passes)."""
    problems = []
    seen = {r["task"] for r in rows}
    for task in expected_tasks:
        if task not in seen:
            problems.append(f"{agent}: {task}: no trial ran")
    for r in rows:
        if r["exception"]:
            problems.append(f"{agent}: {r['task']}: exception {r['exception']}")
        elif r["reward"] != EXPECTED[agent]:
            why = f"outcome={r.get('outcome')} path={r.get('path_penalty')} missed={r.get('missed')}"
            problems.append(f"{agent}: {r['task']}: reward {r['reward']} != "
                            f"{EXPECTED[agent]:g} ({why})")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", type=Path, default=Path("harbor/dist/alkera-de"))
    ap.add_argument("--jobs-dir", type=Path, default=Path("harbor/dist/acceptance-jobs"))
    ap.add_argument("--harbor", default="harbor",
                    help="the harbor command (default: `harbor` on PATH)")
    ap.add_argument("--sg", metavar="GROUP", default=None,
                    help="run harbor under `sg GROUP -c` (e.g. docker)")
    ap.add_argument("-n", "--concurrency", type=int, default=4)
    ap.add_argument("-k", "--attempts", type=int, default=1,
                    help="trials per task; >1 also proves the oracle is deterministic")
    ap.add_argument("-i", "--include", action="append", default=[],
                    help="only tasks whose directory name matches this glob, "
                         "e.g. 'gt007*' (repeatable)")
    ap.add_argument("--agents", default="oracle,nop")
    args = ap.parse_args(argv)

    if not (args.dataset / "dataset.toml").is_file():
        print(f"acceptance: no dataset.toml under {args.dataset}", file=sys.stderr)
        return 2
    # Harbor's -i matches a local dataset's task DIRECTORY names (the part of
    # `[task].name` after the org), so --include globs are spelled that way too.
    expected = [t for t in dataset_tasks(args.dataset)
                if not args.include
                or any(fnmatch.fnmatch(t.split("/", 1)[-1], p) for p in args.include)]
    args.jobs_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    report = {"dataset": str(args.dataset), "stamp": stamp, "tasks": expected, "jobs": {}}
    problems: list[str] = []

    for agent in [a.strip() for a in args.agents.split(",") if a.strip()]:
        if agent not in EXPECTED:
            print(f"acceptance: no expectation for agent {agent!r}", file=sys.stderr)
            return 2
        job = f"acceptance-{agent}-{stamp}"
        print(f"== {agent}: {len(expected)} task(s) x {args.attempts}", flush=True)
        started = time.monotonic()
        run = subprocess.run(harbor_argv(args, agent, job))
        rows = trials(args.jobs_dir / job)
        job_problems = judge(agent, rows, expected)
        if run.returncode != 0:
            job_problems.append(f"{agent}: harbor exited {run.returncode}")
        problems += job_problems
        report["jobs"][agent] = {"job": job, "seconds": round(time.monotonic() - started),
                                 "trials": rows, "problems": job_problems}
        for r in sorted(rows, key=lambda r: r["task"] or ""):
            mark = "ok " if (not r["exception"] and r["reward"] == EXPECTED[agent]) else "BAD"
            print(f"  {mark} {r['task']:<55} reward={r['reward']} outcome={r.get('outcome')}")

    out = args.jobs_dir / f"acceptance-{stamp}.json"
    report["accepted"] = not problems
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n{'ACCEPTED' if not problems else 'REJECTED'}: {len(expected)} task(s); report {out}")
    for p in problems:
        print(f"  - {p}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
