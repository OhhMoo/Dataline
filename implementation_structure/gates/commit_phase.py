#!/usr/bin/env python3
"""
Commit a swarm phase with the machine verdict embedded in the commit message.

The point: `git log` becomes the provenance trail. You can read back months later
which phase produced what, and whether the gates passed at the moment it landed --
without re-running anything.

    python implementation_structure/gates/commit_phase.py R001 phase1-literature
    python implementation_structure/gates/commit_phase.py R001 phase4-build --online
    python implementation_structure/gates/commit_phase.py R001 phase1-literature --dry-run

Never pushes. Never force-commits past a failure -- it records the failure instead,
which is the whole idea.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMPL = HERE.parent
REPO = IMPL.parent


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("round", help="round id, e.g. R001")
    ap.add_argument("phase", help="phase slug, e.g. phase1-literature")
    ap.add_argument("--online", action="store_true", help="include G1/G2 network gates")
    ap.add_argument("--dry-run", action="store_true", help="print the message, commit nothing")
    args = ap.parse_args()

    # 1. run the gates and capture the verdict
    cmd = [sys.executable, str(HERE / "run_gates.py"), "--json"]
    if args.online:
        cmd.append("--online")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=IMPL)
    try:
        verdict = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("could not parse gate output:\n", proc.stdout, proc.stderr, file=sys.stderr)
        return 2

    # 2. persist the raw verdict next to the round's report (never on a dry run)
    if not args.dry_run:
        outdir = IMPL / "rounds" / args.round
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / f"gates-{args.phase}.json").write_text(
            json.dumps(verdict, indent=2), encoding="utf-8")

    # 3. build a commit message that carries the verdict
    counts = {}
    for d in ("knowledge", "claims", "inventory", "archetypes", "failures", "verified"):
        p = IMPL / "state" / d
        counts[d] = len([f for f in p.glob("*.yaml")]) if p.exists() else 0

    status = "PASS" if verdict["passed"] else "FAIL"
    lines = [f"{args.round} {args.phase}: gates {status}", ""]
    for g in verdict["gates"]:
        mark = "PASS" if g["passed"] else "FAIL"
        lines.append(f"  {g['gate']:<3} {g['name']:<38} {mark}  "
                     f"{g['checked']:>3} checked, {len(g['failures'])} fail, "
                     f"{len(g['warnings'])} warn")
    lines += ["", "state: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v)]

    if not verdict["passed"]:
        lines += ["", "Open failures:"]
        for g in verdict["gates"]:
            for f in g["failures"][:10]:
                lines.append(f"  - [{g['gate']}] {f}")
        lines += ["", "Committed anyway: a failing gate is a recorded result, not a",
                  "reason to withhold the artifacts. Repair lands in the next commit."]

    msg = "\n".join(lines)

    if args.dry_run:
        print(msg)
        return 0

    run(["git", "add", "-A"])
    staged = run(["git", "diff", "--cached", "--name-only"]).stdout.strip()
    if not staged:
        print("nothing staged - no changes to commit")
        return 0

    c = run(["git", "commit", "-F", "-"], input=msg)
    print(c.stdout or c.stderr)
    print(run(["git", "log", "--oneline", "-1"]).stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
