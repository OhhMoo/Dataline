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

`--regrade-run <run-dir>` does not run a model at all: it re-grades every rN in
a run that has already happened, from the files those episodes left behind, and
rewrites the report. For a run whose grader was broken, or whose harness died
after teardown. See `rollout.py --regrade`.

`--variant <name>` selects one of the task's declared context variants
(task.toml `[variants]`). It is exported as ALKERA_TASK_VARIANT for every child
process, because the world builder, the surface stager and the grader are
subprocesses whose argv this file does not own; it is written into every rollout
record; and it is appended to the DEFAULT run id, so two arms measured minutes
apart cannot land in the same run directory. An explicit --run-id is left
exactly as given - a caller who names the directory has said what it is.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import container_episode  # noqa: E402
import episode      # noqa: E402
import native_stack  # noqa: E402
import report       # noqa: E402
import rollout      # noqa: E402
import taskspec     # noqa: E402

HERE = Path(__file__).resolve().parent
# OUTSIDE the repo, deliberately. A world built inside the source tree puts the
# subject one `..` from the grader, and "it cannot get there anyway" is exactly
# the assumption this harness refuses to make.
DEFAULT_OUT = Path(tempfile.gettempdir()) / "alkera-rollouts"


def _default_run_id(variant: str) -> str:
    """`<utc>` for a one-context task, `<utc>-<variant>` for an arm.

    The run id is what the run DIRECTORY is called and what every recorded
    number is filed under. Two arms of one task measured the same afternoon
    would otherwise be two directories distinguishable only by their contents.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{stamp}-{variant}" if variant and variant != "default" else stamp


def _stamp_variant(rec: dict, variant: str, path: Path) -> dict:
    """Put the arm INTO the rollout record, on disk as well as in memory.

    rollout.py is owned by another builder and does not yet record it (see
    VARIANTS_REQUESTS.md); this file is the one that knows which arm it asked
    for, so it writes it. When rollout.py starts recording it itself this
    becomes a check rather than a write - which is why a DIFFERENT recorded
    value is a hard error and not a silent overwrite.
    """
    if not variant:
        return rec
    recorded = rec.get("variant")
    if recorded and recorded != variant:
        raise SystemExit(
            f"{path} records variant {recorded!r} and this run asked for "
            f"{variant!r}. One of the two built the world; the harness will not "
            f"guess which, and a rate averaged over two contexts is not a rate.")
    if recorded:
        return rec
    rec["variant"] = variant
    path.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="")
    return rec


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
        rec = _stamp_variant(rec, getattr(args, "variant_resolved", ""), rec_path)
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
        "variant": getattr(args, "variant_resolved", ""),
        "blindness": {"quarantined": False, "hard_count": 0, "soft_count": 0,
                      "kinds": [], "hard_kinds": [], "violations": []},
    }


def regrade_run(run_dir: Path, task: str | None, tasks_dir: str | None) -> int:
    """Re-grade every rollout in a run directory and rewrite its report.

    The run-level half of `rollout.py --regrade`. It exists because a report is
    not optional: five re-graded records beside a report.json aggregated from
    the crashed run would be five right numbers under one wrong one, and the
    report is the artefact anyone actually reads.

    Nothing is re-run and nothing is rebuilt - see rollout.py's regrade block.
    Each rollout is re-graded IN PLACE in the run directory it names, so a run
    that must be preserved is copied first; that is the caller's call and the
    harness does not make it for them.
    """
    rollout_dirs = sorted((p for p in run_dir.iterdir()
                           if p.is_dir() and re.fullmatch(r"r\d+", p.name)),
                          key=lambda p: int(p.name[1:]))
    if not rollout_dirs:
        print(f"  {run_dir} holds no rN rollout directories")
        return 2
    print(f"\n  regrading {len(rollout_dirs)} rollout(s) in {run_dir}")
    records, refused = [], []
    for path in rollout_dirs:
        try:
            rec = rollout.regrade(path, task=task, tasks_dir=tasks_dir)
        except rollout.RegradeRefused as exc:
            # A refusal is a result. It is printed, counted, and the rollout is
            # left exactly as it was - the alternative, writing a record for a
            # directory we could not describe, is the thing this refuses to do.
            refused.append((path.name, str(exc)))
            print(f"  [{path.name}] REFUSED  {exc}")
            continue
        records.append(rec)
        print(f"  [{path.name}] {rec['classification']:<20} "
              f"regrade={rec['regrade']['status']:<8} "
              f"was={rec['regraded_from']} "
              f"total={(rec.get('score') or {}).get('total')}")
    if not records:
        print("  nothing was re-graded, so the report is left alone")
        return 2
    task_id = task or records[0].get("task") or run_dir.parent.name
    agg = report.write(run_dir, task_id, run_dir.name, records)
    print(f"\n  {report.render(task_id, run_dir.name, agg, records)}")
    if refused:
        print(f"  {len(refused)} rollout(s) refused and are NOT in the report: "
              f"{[r[0] for r in refused]}")
    print(f"  report: {run_dir / 'report.md'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=False)
    ap.add_argument("--regrade-run", default=None,
                    help="re-grade every rN in an EXISTING run directory from "
                         "the files those episodes left behind, then rewrite "
                         "report.json and report.md. No model is run and no "
                         "world is rebuilt; see rollout.py --regrade. Re-grades "
                         "IN PLACE - copy the run first if it must be kept.")
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
    ap.add_argument("--variant", default=None,
                    help="context variant from the task's [variants] table. "
                         "Omitted means `default` - the shipped context. A task "
                         "that declares no [variants] refuses this flag rather "
                         "than ignoring it.")
    ap.add_argument("--concurrency", type=int, default=0,
                    help="0 = all K at once")
    ap.add_argument("--wall-clock", type=int, default=None)
    ap.add_argument("--max-tool-calls", type=int, default=None)
    ap.add_argument("--adversarial-suffix", default=None,
                    help="red-teams the harness's own detectors; a run using it "
                         "is marked and must never be reported as a measurement")
    a = ap.parse_args()
    if a.regrade_run:
        return regrade_run(Path(a.regrade_run).resolve(), a.task, a.tasks_dir)
    if not a.task:
        ap.error("--task is required unless --regrade-run is given")

    spec = taskspec.load(a.task,
                         Path(a.tasks_dir).resolve() if a.tasks_dir else None,
                         variant=a.variant)
    # Every child process - rollout.py, the world builder it starts, the surface
    # stager and the grader - reads the arm from here. Set even when it resolved
    # to `default`, so a stale value inherited from the shell cannot quietly
    # move the world out from under a run.
    a.variant_resolved = spec.variant
    if spec.variant:
        os.environ[taskspec.VARIANT_ENV] = spec.variant
    run_id = a.run_id or _default_run_id(spec.variant)
    run_dir = Path(a.out).resolve() / a.task / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  task {a.task} · K={a.k} · model {a.model} · driver {a.driver}"
          f" · topology {a.topology}")
    if a.driver == "native" and a.topology == "host":
        # Once, at the start, from the module that owns the topology - see
        # tasks/harness/NATIVE_CONTAINER.md. A run that is not measurement_valid
        # has to say so before it produces a number, not in a footnote under one.
        print(native_stack.HOST_TOPOLOGY_WARNING)
    if spec.variant:
        print(f"  variant {spec.variant}: "
              f"{spec.variant_note.strip().splitlines()[0]}")
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
