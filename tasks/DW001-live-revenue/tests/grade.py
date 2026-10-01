#!/usr/bin/env python3
"""DW001 grade step: the agent's damage integral, read off the ledger.

    python grade.py --dynamic <rollout>/dynamic --json

Prints one json object. NOT a pass/fail grader: the dynamic world's pass
criterion is undecided (S needs the no-op and oracle twins, plan Task 5), so
this emits J and the per-contract damage only. `total`/`max` are null and
`checks` is empty; classify.py files the rollout as `dynamic` with
`passed: null`, outside every Pass@1 denominator.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parents[1]
sys.path.insert(0, str(REPO / "world"))


def grade(ledger_dir: Path) -> dict:
    from world import damage                          # noqa: E402

    spec = damage.load_spec(TASK / "damage.yaml")
    schedule = json.loads((ledger_dir / "tick_schedule.json").read_text("utf-8"))
    path = ledger_dir / "damage.jsonl"
    rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()
            if line.strip()] if path.exists() else []
    weights = spec.get("weights") or {}
    episode = [r for r in rows if r.get("phase") == "episode"]
    return {
        "task": "DW001-live-revenue",
        "dynamic": True,
        "J": damage.integral(rows, weights, float(schedule["h_hours"])),
        "J_episode_only": damage.integral(episode, weights,
                                          float(schedule["h_hours"])),
        "ticks": len(rows),
        "ticks_episode": len(episode),
        "V_final": rows[-1]["V"] if rows else {},
        "vnow_final": rows[-1]["vnow"] if rows else schedule.get("vnow_start"),
        "pass": None,
        "total": None,
        "max": None,
        "checks": [],
        "note": "S = (J_noop - J) / (J_noop - J_oracle) needs the twins (Task 5)",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dynamic", required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    print(json.dumps(grade(Path(a.dynamic)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
