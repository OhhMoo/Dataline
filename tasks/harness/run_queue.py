"""Run several K-episode measurements back to back, and stop when the account does.

A queue is a list of `run_rollouts.py` invocations run one after another in
this process's foreground. Between runs it reads the `report.json` the run
just wrote and decides whether the NEXT run is worth starting:

  - the run's own quota probe was refused (`run_rollouts.py` exited 3)  -> stop
  - `quota_exhausted` episodes in the run                               -> stop
  - anything else, including a 0.0, a crash, or `harness_error`s        -> continue

Stopping is the whole point. Five K=5 runs queued at midnight against an
account that dies at 00:41 are twenty-five `quota_exhausted` records and a
fixture build each; the queue turns that into one lost run plus a line in
`queue.json` saying where it stopped and why. The harness never publishes a
rate from a run with excluded episodes anyway (`report.py`), so nothing
here changes what a number means - only how many are attempted.

Every run in the queue shares --driver, --model, --k, --out and --topology.
What differs per entry is the task, the context variant and (optionally) a
run-id suffix. Runs are sequential, not concurrent, because two K=5s in
flight against one account are the failure mode this script exists to
prevent; --concurrency is passed through so the K episodes WITHIN a run
still fan out.

Usage (the 2026-09-17 plan, STATUS.md section 6):

    python tasks/harness/run_queue.py --model opus \
        GT004-doubling-window:authorized \
        GT004-doubling-window \
        GT009-settlement-cutoff \
        GT003-tuesday-figure

Each positional is `TASK` or `TASK:VARIANT`. `--queue-id` names the
`queue.json` record written under --out; default is the start timestamp.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_ROLLOUTS = HERE / "run_rollouts.py"
DEFAULT_OUT = HERE.parent / "runs"

EXIT_PROBE_REFUSED = 3  # run_rollouts.py's own code for a refused quota probe


def parse_entry(s: str) -> tuple[str, str | None]:
    task, _, variant = s.partition(":")
    if not task:
        raise argparse.ArgumentTypeError(f"empty task in queue entry {s!r}")
    return task, (variant or None)


def _default_run_id(variant: str | None, suffix: str, k: int) -> str:
    stamp = datetime.now().strftime("%Y%m%d")
    base = f"{variant or 'clean'}-{stamp}-k{k}"
    return f"{base}-{suffix}" if suffix else base


def _read_report(run_dir: Path) -> dict | None:
    p = run_dir / "report.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def decide(rc: int, report: dict | None) -> tuple[bool, str]:
    """(continue?, why). Only a refused account stops the queue."""
    if rc == EXIT_PROBE_REFUSED:
        return False, "quota probe refused before the first episode"
    if report is None:
        # The run died before it could aggregate - a harness problem, not the
        # account's. The next run may well work; let it try.
        return True, f"no report.json (run_rollouts exit {rc}); continuing"
    counts = (report.get("summary") or {}).get("classification_counts") or {}
    q = int(counts.get("quota_exhausted") or 0)
    if q:
        return False, f"{q} quota_exhausted episode(s) in the run"
    return True, "ok"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("entries", nargs="+", type=parse_entry,
                    metavar="TASK[:VARIANT]")
    ap.add_argument("--driver", default="native",
                    choices=["mcp", "cli", "scripted", "kimi", "native"],
                    help="measurement substrate is native; mcp is the "
                         "comparison arm (default: native)")
    ap.add_argument("--model", default="opus",
                    help="the subject's model id in the driver's vocabulary")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=0,
                    help="episodes in flight WITHIN one run; 0 = all K")
    ap.add_argument("--topology", default="container", choices=["host", "container"],
                    help="container is the measurement topology; host is not "
                         "measurement_valid for native (default: container)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--run-id-suffix", default="",
                    help="appended to every run id, e.g. `rerun`, so a queued "
                         "run never lands in an existing run directory")
    ap.add_argument("--queue-id", default=None)
    ap.add_argument("--python", default=sys.executable,
                    help="interpreter for run_rollouts.py (the gateway venv "
                         "for native)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the commands and exit")
    a = ap.parse_args(argv)

    out = Path(a.out).resolve()
    queue_id = a.queue_id or datetime.now().strftime("queue-%Y%m%d-%H%M%S")
    record = {
        "queue_id": queue_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "driver": a.driver, "model": a.model, "k": a.k,
        "topology": a.topology,
        "entries": [{"task": t, "variant": v} for t, v in a.entries],
        "runs": [],
        "stopped_because": None,
    }
    rec_path = out / f"{queue_id}.json"

    def save() -> None:
        out.mkdir(parents=True, exist_ok=True)
        rec_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    for i, (task, variant) in enumerate(a.entries):
        run_id = _default_run_id(variant, a.run_id_suffix, a.k)
        run_dir = out / task / run_id
        cmd = [a.python, str(RUN_ROLLOUTS), "--task", task, "--k", str(a.k),
               "--driver", a.driver, "--model", a.model,
               "--topology", a.topology, "--out", str(out),
               "--run-id", run_id, "--concurrency", str(a.concurrency)]
        if variant:
            cmd += ["--variant", variant]
        print(f"\n[{i + 1}/{len(a.entries)}] {task}"
              + (f" · variant {variant}" if variant else "")
              + f" · run {run_id}")
        print("  " + " ".join(cmd))
        if a.dry_run:
            continue
        t0 = time.time()
        rc = subprocess.run(cmd, cwd=str(HERE.parent.parent)).returncode
        report = _read_report(run_dir)
        go_on, why = decide(rc, report)
        summary = (report or {}).get("summary") or {}
        entry = {
            "task": task, "variant": variant, "run_id": run_id,
            "run_dir": str(run_dir), "exit_code": rc,
            "wall_clock_s": round(time.time() - t0, 1),
            "pass_at_1": summary.get("pass_at_1"),
            "scored": summary.get("scored"),
            "classification_counts": summary.get("classification_counts"),
            "measurement_valid": summary.get("measurement_valid"),
            "decision": "continue" if go_on else "stop", "why": why,
        }
        record["runs"].append(entry)
        save()
        print(f"  -> exit {rc} · pass@1 {entry['pass_at_1']} over "
              f"{entry['scored']} scored · {why}")
        if not go_on:
            record["stopped_because"] = why
            left = [f"{t}:{v}" if v else t for t, v in a.entries[i + 1:]]
            record["not_started"] = left
            save()
            print(f"\n  STOPPING the queue: {why}. Not started: {left or 'nothing'}."
                  f"\n  Record: {rec_path}")
            return 3

    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    if not a.dry_run:
        save()
        print(f"\n  queue complete. Record: {rec_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
