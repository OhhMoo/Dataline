#!/usr/bin/env python3
"""K independent rollouts of one task, then the report.

    python run_rollouts.py --task GT001-aov-halving --k 5

Under `--driver native` there is no gateway: the subject reaches vendor-shaped
services through its shell and every service writes its own 9.2.2 row into the
rollout's audit directory. `--native-endpoints` is passed straight through, so K
native rollouts share one endpoint map and nothing else.

Each rollout is its own process with its own world copy, grading zone, audit
directory and gateway. Nothing is shared but the read-only task package and the
cached generator fixture, which is built ONCE here before any rollout forks -
K generator processes racing over the same output directory is exactly how a
stale reference once got written under a validated run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import container_episode  # noqa: E402
import episode      # noqa: E402
import report       # noqa: E402
import taskspec     # noqa: E402

HERE = Path(__file__).resolve().parent
# OUTSIDE the repo, deliberately. A world built inside the source tree puts the
# subject one `..` from the grader, and "it cannot get there anyway" is exactly
# the assumption this harness refuses to make.
DEFAULT_OUT = Path(tempfile.gettempdir()) / "alkera-rollouts"


def _one(args, run_dir: Path, idx: int) -> dict:
    rid = f"r{idx}"
    out = run_dir / rid
    cmd = [sys.executable, str(HERE / "rollout.py"),
           "--task", args.task, "--rollout-id", rid, "--run-id", run_dir.name,
           "--out", str(out), "--model", args.model, "--driver", args.driver,
           "--topology", args.topology, "--image", args.image]
    if args.tasks_dir:
        cmd += ["--tasks-dir", args.tasks_dir]
    if args.native_endpoints:
        cmd += ["--native-endpoints", args.native_endpoints]
    if args.wall_clock:
        cmd += ["--wall-clock", str(args.wall_clock)]
    if args.max_tool_calls:
        cmd += ["--max-tool-calls", str(args.max_tool_calls)]
    if args.adversarial_suffix:
        cmd += ["--adversarial-suffix", args.adversarial_suffix]
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    rec_path = out / "rollout.json"
    if rec_path.exists():
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        print(f"  [{rid}] {rec['classification']:<20} "
              f"total={(rec.get('score') or {}).get('total')} "
              f"calls={rec.get('tool_calls')} "
              f"wall={rec.get('wall_clock_s')}s")
        return rec
    # the rollout process itself died before writing a record: that is our bug
    print(f"  [{rid}] ROLLOUT PROCESS FAILED (exit {proc.returncode})")
    return {
        "schema_version": 1, "task": args.task, "rollout_id": rid,
        "run_id": run_dir.name, "model": args.model, "driver": args.driver,
        "classification": "harness_error", "passed": False, "scored": False,
        "needs_review": True,
        "why": f"rollout process exited {proc.returncode} without a record",
        "stderr_tail": (proc.stderr or "")[-4000:],
        "stdout_tail": (proc.stdout or "")[-2000:],
        "wall_clock_s": round(time.time() - started, 2), "tool_calls": 0,
        "blindness": {"quarantined": False, "hard_count": 0, "soft_count": 0,
                      "kinds": [], "hard_kinds": [], "violations": []},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--tasks-dir", default=None,
                    help="directory holding the task package, when it is not tasks/")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--driver", default="mcp",
                    choices=["mcp", "cli", "scripted", "kimi", "native"])
    ap.add_argument("--native-endpoints", default=None,
                    help="JSON file mapping native surface names onto reachable "
                         "addresses, for `--driver native` off the episode "
                         "network (see rollout.py --native-endpoints)")
    ap.add_argument("--topology", default="host", choices=["host", "container"],
                    help="`container` runs each rollout's world, gateway, audit "
                         "log and shell inside the episode service of "
                         "gateway/docker/compose.yml. One compose project per "
                         "rollout, so K rollouts stay genuinely independent.")
    ap.add_argument("--image", default=container_episode.DEFAULT_IMAGE)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--concurrency", type=int, default=0,
                    help="0 = all K at once")
    ap.add_argument("--wall-clock", type=int, default=None)
    ap.add_argument("--max-tool-calls", type=int, default=None)
    ap.add_argument("--adversarial-suffix", default=None,
                    help="red-teams the harness's own detectors; a run using it "
                         "is marked and must never be reported as a measurement")
    a = ap.parse_args()

    spec = taskspec.load(a.task,
                         Path(a.tasks_dir).resolve() if a.tasks_dir else None)
    run_id = a.run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = Path(a.out).resolve() / a.task / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  task {a.task} · K={a.k} · model {a.model} · driver {a.driver}"
          f" · topology {a.topology}")
    print(f"  run  {run_dir}")
    print(f"  pass criterion: all of {spec.pass_requires_checks} "
          f"and total >= {spec.pass_min_total}\n")

    print("  preparing the task's cached world fixture ...")
    episode.prepare_task(spec)

    workers = a.concurrency or a.k
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(lambda i: _one(a, run_dir, i), range(a.k)))

    agg = report.write(run_dir, a.task, run_id, records)
    print(f"\n  {report.render(a.task, run_id, agg, records)}")
    print(f"  wall clock for the whole run: {round(time.time() - started, 1)}s")
    print(f"  report: {run_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
