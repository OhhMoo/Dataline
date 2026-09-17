#!/usr/bin/env python3
"""Run P5 - and only P5 - of containment.py, and write its evidence file.

    python ledger_probe.py [--project NAME] [--out PATH]

Why this exists rather than `python containment.py`: the full battery brings up
the stack, exercises every backend under load, tears the stack down and starts
the grading zone, which is minutes of machine and is not what a change to the
ledger needs re-proved. P5 needs the episode container running and nothing
else. The property itself lives in containment.py, in the same style as P1-P4
and with the same pass conditions; this file only chooses which one to run, so
there is no second copy of the claim to drift.

The evidence it writes says `property: p5_ledger_protected` and carries the
same section containment.py would have written. A run of the full battery
supersedes it.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import containment                      # noqa: E402

HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="alkera-ledger-p5")
    parser.add_argument("--out", type=Path,
                        default=HERE / "evidence" / "containment-ledger.json")
    parser.add_argument("--keep-up", action="store_true")
    args = parser.parse_args()

    stack = containment.Stack(args.project, [])
    evidence = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "property": "p5_ledger_protected",
        "project": args.project,
        "compose_file": str(containment.COMPOSE_FILE),
        "scope": "P5 only; P1-P4 come from a full containment.py run",
        "host": {
            "platform": platform.platform(),
            "docker": containment.out(
                ["docker", "version", "--format", "{{.Server.Version}}"]),
        },
    }
    try:
        stack.compose("up", "-d", "--wait")
        section = containment.property_5_ledger(stack, evidence)
    finally:
        if not args.keep_up:
            stack.compose("down", "-v", "-t", "5", check=False)

    for key in [k for k in evidence if k.startswith("_")]:
        evidence.pop(key)
    evidence["passed"] = section["passed"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True),
                        encoding="utf-8")
    print(json.dumps({"artifact": str(args.out),
                      "p5_ledger_protected": section["passed"],
                      "shell_read": section["shell"]["read"]["result"],
                      "shell_write": section["shell"]["write"]["result"],
                      "escapes_that_worked": section["tools"]["escapes_that_worked"]}, indent=2))
    return 0 if section["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
