"""Acceptance for the Track B stack: does it work, and is it contained.

Two halves, deliberately separated:

* **function** - the gateway serves both transports over the network, both warehouse
  backends produce identical action-log rows, and the docs/BI/S3 surfaces behave.
  That half lives here and in ``acceptance_inside.py``.
* **containment** - egress, zones, channel and determinism. That half lives in
  ``containment.py``, which owns the stack lifecycle because the grading zone can only
  be probed *after* teardown, and writes ``evidence/containment.json``.

They were one script and the egress logic was written twice. Two implementations of a
safety check is one implementation and one thing that can quietly disagree with it, so
this one now calls the other rather than repeating it.

    python acceptance.py --profile snowflake
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from containment import HERE, Stack


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="alkera-b2b4")
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--skip-containment", action="store_true")
    args = parser.parse_args()

    stack = Stack(args.project, args.profile)
    stack.compose("up", "-d", "--wait")
    report = {}

    # The agent's vantage, over the network, through the MCP transport - not an
    # in-process ASGI client. This is the one channel and it has to work from there.
    report["mcp_over_episode_net"] = stack.compose(
        "run", "--rm", "harness", "alkera-mcp-smoke", "http://episode:8080/mcp/"
    ).stdout.strip()

    # Backend/transport parity and the live surfaces, from inside the episode.
    report["inside"] = json.loads(
        stack.compose("exec", "-T", "episode", "python", "/opt/stack/acceptance_inside.py")
        .stdout.strip().splitlines()[-1]
    )
    print(json.dumps(report, indent=2))

    if args.skip_containment:
        return 0
    # containment.py brings the stack down as its last act, which is the point.
    return subprocess.run(
        [sys.executable, str(HERE / "containment.py"), "--project", args.project]
        + [flag for profile in args.profile for flag in ("--profile", profile)]
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
