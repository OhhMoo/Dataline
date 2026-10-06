#!/usr/bin/env python3
"""Run the native oracle (solution/solve.sh) as the scripted subject of ONE real
harness rollout, host or container topology. Not a measurement; no model.

    # host: the oracle needs curl, git and the gateway's python - nothing else
    cd gateway && uv run python \\
        ../tasks/BI002-powerbi-share-and-stale-refresh/tests/oracle_rollout.py \\
        --topology host --out ~/alkera-rollouts

    # container: docker access required; the image must carry this tree's
    # gateway/src and world/ (DESIGN.md §9)
    sg docker -c "cd gateway && uv run python \\
        ../tasks/BI002-powerbi-share-and-stale-refresh/tests/oracle_rollout.py \\
        --topology container --image alkera-episode:head-da --out ~/alkera-rollouts"

WHY. `run_rollouts.py --model scripted` runs a fixed plumbing script
(tasks/harness/native_stack.py SCRIPTED_SUBJECT / CONTAINER_SUBJECTS), which
proves the stack, not the task. This puts the task's own oracle in that slot -
the world built by tests/build_world.py, the Power BI service up with the
task's profile, the surface gate, the oracle working only through the
subject's shell, teardown, `{bi_state}` staged by tasks/harness/bistate.py,
grade twice, classify - so "the oracle resolves through the harness" is a
thing that ran, on each topology.

The subject script is self-extracting: it writes solve.sh and fix_pbi.py into a
fresh temporary directory where it runs (the host's /tmp, or the episode
container's) and executes solve.sh from there, so nothing of the task package
is mounted or visible to the episode. The Harbor canary comment is left out
of the copy: it is not this harness's canary, and a planted-token detector
must never be argued with.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
HARNESS = TASK.parent / "harness"
sys.path.insert(0, str(HARNESS))

import container_episode                                        # noqa: E402
import native_stack                                             # noqa: E402
import rollout                                                  # noqa: E402

SUBJECT = "bi002-oracle"


def _body(path: Path) -> str:
    return "".join(line for line in path.read_text(encoding="utf-8").splitlines(True)
                   if not line.startswith("# alkera-canary"))


def subject_script() -> str:
    solution = TASK / "solution"
    return "\n".join([
        "#!/bin/sh",
        "set -eu",
        'ORACLE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/bi002-oracle.XXXXXX")"',
        "cat > \"$ORACLE_DIR/fix_pbi.py\" <<'ALKERA_FIX_PBI_EOF'",
        _body(solution / "fix_pbi.py").rstrip("\n"),
        "ALKERA_FIX_PBI_EOF",
        "cat > \"$ORACLE_DIR/solve.sh\" <<'ALKERA_SOLVE_EOF'",
        _body(solution / "solve.sh").rstrip("\n"),
        "ALKERA_SOLVE_EOF",
        'cd "$ALKERA_WORKSPACE"',
        'bash "$ORACLE_DIR/solve.sh"',
        'status=$?',
        'rm -rf "$ORACLE_DIR"',
        'exit $status',
        ""])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topology", choices=["host", "container"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--image", default=container_episode.DEFAULT_IMAGE)
    ap.add_argument("--variant", default="default")
    a = ap.parse_args(argv)
    script = subject_script()
    native_stack.SCRIPTED_SUBJECT = script
    native_stack.CONTAINER_SUBJECTS[SUBJECT] = script
    os.environ[native_stack.SUBJECT_ENV] = SUBJECT
    os.environ["ALKERA_TASK_VARIANT"] = a.variant
    run_id = a.run_id or f"oracle-{a.topology}"
    # rollout.py's --out is the ROLLOUT directory; lay it out as run_rollouts does.
    out = Path(a.out).expanduser() / TASK.name / run_id / "r0"
    sys.argv = ["rollout.py", "--task", TASK.name, "--out", str(out),
                "--driver", "native", "--model", "scripted", "--topology", a.topology,
                "--run-id", run_id, "--variant", a.variant, "--image", a.image,
                "--subject-sandbox", "host"]
    return rollout.main()


if __name__ == "__main__":
    raise SystemExit(main())
