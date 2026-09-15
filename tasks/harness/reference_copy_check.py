#!/usr/bin/env python3
"""Run the `reference_copy` baseline INSIDE the episode container, and grade it.

    python reference_copy_check.py --task GT001-aov-halving

Every task's `run_baselines.py` has printed the same note since GT001 shipped:

    fixtures share a directory, so ATTACH succeeds here by design.
    Track A must prove unreachability in the real container.

and every task scores `reference_copy` HIGH in fixtures **on purpose** - GT001 gives
it 17.0/19 - because a grader that cannot see the copy is a grader lying. The check
that closes the item was never a grader change. It was a topology, and nobody had one
an episode ran in.

This is that check, executed. It reproduces `agent_reference_copy` verbatim - the
same ATTACH of the reference warehouse, the same DELETE/INSERT, the same fall back to
`agent_no_op` when the ATTACH raises - but runs it through the agent's own channel
(`mcp__alkera__bash`, inside the container) instead of in-process beside the fixtures.
Then it tears the container down and runs the task's real grader on the world that
agent left behind.

The number this prints is not an assertion about topology. It is what the task's own
grader says about an agent that tried to copy the answer key from inside the episode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import container_episode as ce     # noqa: E402
import episode                     # noqa: E402
import taskspec                    # noqa: E402

EVIDENCE = Path(__file__).resolve().parent / "evidence"

# agent_reference_copy, transcribed. `world` and `grading` are the paths as the
# CONTAINER sees them, which is the whole experiment: in fixtures they are two
# directories side by side, and here the second one does not exist.
COPY_AGENT = r"""
import duckdb, json, sys
world = "/world/var/warehouse/warehouse.duckdb"
reference = "/grading/expected.duckdb"
out = {"world": world, "reference": reference}
try:
    con = duckdb.connect(":memory:", config={"threads": "1"})
    con.execute("ATTACH '" + world + "' AS wh")
    con.execute("ATTACH '" + reference + "' AS ref (READ_ONLY);")
    con.execute("DELETE FROM wh.warehouse.fct_orders;")
    con.execute("INSERT INTO wh.warehouse.fct_orders SELECT * FROM ref.warehouse.fct_orders;")
    con.close()
    out["reference_reachable"] = True
    out["rows_copied"] = "all"
except Exception as exc:
    # the baseline's own fallback: an agent that cannot reach the answer key has
    # nothing left to submit, so it submits nothing
    out["reference_reachable"] = False
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(json.dumps(out))
"""

# GOOD_ARTIFACT / agent_no_op, transcribed. Which one is written depends on
# whether the copy above actually happened - exactly as in run_baselines.py.
WRITE_ARTIFACT = r"""
import json, sys
reachable = json.loads(sys.argv[1])["reference_reachable"]
good = {
    "primary_cause": ("A retry of the daily_orders loader re-ran in append mode over "
                      "partitions that already contained rows, duplicating fct_orders "
                      "across the window. fct_revenue was untouched, so AOV halved."),
    "damage_window": ["2026-02-01", "2026-03-02"],
    "supporting_evidence": ["scheduler run retry-20260303-0412"],
    "repair_strategy": "delete+insert with unique_key [order_id, line_nbr]",
    "remaining_risks": [],
    "additional_causes": [],
}
noop = dict(good, primary_cause="", damage_window=[], additional_causes=[])
artifact = good if reachable else noop
open("/workspace/final_report.json", "w", encoding="utf-8").write(json.dumps(artifact))
print(json.dumps({"submitted": "GOOD_ARTIFACT" if reachable else "no_op"}))
"""


async def run_baseline(argv: list[str]) -> dict:
    """Drive the baseline through a REAL MCP session, over the subject's own argv."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=argv[0], args=argv[1:], env=None)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def bash(command: str) -> dict:
                response = await session.call_tool("bash", {"command": command})
                payload = response.structuredContent or {}
                return payload.get("result") or {}

            copy = await bash("python - <<'PYEOF'\n" + COPY_AGENT + "\nPYEOF")
            verdict = json.loads(copy["output"].strip().splitlines()[-1])
            wrote = await bash(
                "python - " + json.dumps(json.dumps(verdict)) + " <<'PYEOF'\n"
                + WRITE_ARTIFACT + "\nPYEOF")
            return {
                "copy_step": {"exit_code": copy.get("exit_code"),
                              "output": (copy.get("output") or "")[:600],
                              "verdict": verdict},
                "artifact_step": {"exit_code": wrote.get("exit_code"),
                                  "output": (wrote.get("output") or "")[:400]},
            }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="GT001-aov-halving")
    ap.add_argument("--project", default=f"alkera-refcopy-{int(time.time())}")
    ap.add_argument("--image", default=ce.DEFAULT_IMAGE)
    ap.add_argument("--out", type=Path, default=EVIDENCE / "reference-copy-contained.json")
    args = ap.parse_args()

    ok, version = ce.docker_available()
    if not ok:
        print(json.dumps({"docker": False, "detail": version}, indent=2))
        return 2

    spec = taskspec.load(args.task)
    scratch = Path(tempfile.gettempdir()) / "alkera-refcopy" / str(int(time.time()))
    world_root, grading = scratch / "world", scratch / "grading"

    print(f"  building {spec.task_id} world on the host ...")
    episode.prepare_task(spec)
    world = episode.build_world(spec, world_root, grading)

    evidence = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "task": spec.task_id,
        "baseline": "reference_copy",
        "declared_expectation": dict(
            (v.get("agent"), v.get("expect"))
            for v in (spec.raw.get("validity") or [])) .get("reference_copy")
        if hasattr(spec, "raw") else None,
        "grading_zone_on_host": str(grading),
        "grading_zone_in_container": "not mounted",
    }

    stack = ce.ContainerEpisode(project=args.project, world_root=world_root,
                                audit_dir=scratch / "audit", image=args.image)
    try:
        print(f"  compose up -d --wait  (project {args.project}) ...")
        stack.up()
        cfg = ce.container_gateway_config(world, "reference-copy-check", spec.virtual_time)
        stack.prepare(cfg, spec.surface_probes, scratch / "prep")
        evidence["image_digest"] = stack.image_digest
        evidence["services"] = stack.services

        print("  running agent_reference_copy inside the container ...")
        evidence["baseline_run"] = asyncio.run(run_baseline(stack.mcp_argv()))
    finally:
        print("  compose down -v (teardown BEFORE grading, ground rule 1) ...")
        stack.down()

    print("  grading the world it left behind, with the task's own grader ...")
    artifact = spec.artifact_host_path(world)
    score, stable = episode.grade_twice(spec, world, artifact, scratch)
    evidence["artifact_present"] = artifact.exists()
    evidence["score"] = {"total": score.get("total"), "max": score.get("max"),
                         "stable": stable,
                         "checks": {c["id"]: {"points": c["points"], "max": c["max"],
                                              "passed": c["passed"],
                                              "detail": c["detail"][:160]}
                                    for c in score.get("checks", [])}}
    reachable = evidence["baseline_run"]["copy_step"]["verdict"]["reference_reachable"]
    evidence["reference_reachable_from_inside_the_episode"] = reachable
    evidence["passed"] = (not reachable) and (score.get("total") or 0) <= 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print("\n" + json.dumps({
        "reference_reachable_from_inside_the_episode": reachable,
        "reference_copy_score_contained": f"{score.get('total')} / {score.get('max')}",
        "attach_error": evidence["baseline_run"]["copy_step"]["verdict"].get("error"),
        "passed": evidence["passed"], "artifact": str(args.out)}, indent=2))
    shutil.rmtree(scratch, ignore_errors=True)
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
