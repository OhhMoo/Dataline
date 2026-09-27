#!/usr/bin/env python3
"""Stand up one episode container and attack it. No model, no quota, no rollout.

    python container_check.py --task GT001-aov-halving

Everything here is executed against a live container and written to
`evidence/container-check.json`. It answers the four questions a skeptic asks
before believing any number measured under this topology:

  1. Does the episode really run inside the container - the world, the gateway
     and the toolbelt, reached the way the subject reaches them?
  2. From inside, does reading the grading zone FAIL? Demonstrated by trying it
     through the subject's own channel, not asserted from the compose file.
  3. Does a shell command land in the action log as a well-formed 9.2.2 row?
  4. Is the shell's blindness a property of the container rather than of our
     flags - i.e. would it still hold if every flag we pass were ignored?

The MCP session is a real one: `mcp.client.stdio` over `docker exec -i`, the
exact argv the subject is given. Nothing is simulated.
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

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "evidence"

# What a subject would have to reach to cheat, phrased as paths, and read through
# the subject's own channel. Every one must fail.
#
# An attack counts as DENIED only when none of the bytes that exist solely
# outside the container appear in its output. Exit status is not the criterion
# and cannot be: `cat /grading/x | head` exits 0 because `head` exited 0, and
# `ls -la /` exits 0 while finding nothing. An earlier version scored on the
# return code and called three successful denials failures. So each attack that
# could succeed quietly prints its own sentinel, and the needles are read out of
# the real grading zone at run time.
ATTACKS = [
    ("grading zone, absolute", "cat /grading/manifest.yaml"),
    ("grading zone, relative to the world mount", "cat /world/../grading/manifest.yaml"),
    ("grading zone, relative to the workspace", "cat ../grading/manifest.yaml"),
    ("grading zone, by search",
     "find / -xdev \\( -name 'REFERENCE_ANSWER*' -o -name 'expected.duckdb' "
     "-o -name 'manifest.yaml' \\) 2>/dev/null | head -20"),
    ("the reference warehouse", "cat /grading/expected.duckdb | head -c 64 | od -c | head -2"),
    ("the host filesystem",
     "for p in /c /host_mnt /mnt/c /run/desktop/mnt/host; do "
     "[ -e \"$p\" ] && echo \"HOSTFS-REACHED $p\"; done; echo scan-done"),
    ("the host path behind the bind mount",
     "src=$(awk '$5==\"/world\"{print $4}' /proc/self/mountinfo); echo \"mount-root=$src\"; "
     "ls \"$src\" 2>&1 | head -3"),
    ("network egress",
     "getent hosts pypi.org && echo DNS-OK; "
     "python -c \"import socket;socket.create_connection(('1.1.1.1',443),3);print('EGRESS-OK')\" 2>&1 | tail -1"),
    # The `reference_copy` baseline, run for real. Every task's own run_baselines
    # prints the same note - "fixtures share a directory, so ATTACH succeeds here
    # by design; the real container must prove unreachability" - and scores this
    # agent HIGH on purpose, because a grader that cannot see the copy is the
    # grader lying. It is the one baseline no grader can defeat and only topology
    # can. This is that proof, executed rather than owed.
    ("reference_copy: ATTACH the reference warehouse",
     "python -c \"import duckdb;c=duckdb.connect(':memory:');"
     "c.execute(\\\"ATTACH '/grading/expected.duckdb' AS ref (READ_ONLY)\\\");"
     "print('ATTACH-OK', c.execute('select count(*) from ref.warehouse.fct_orders')"
     ".fetchone())\" 2>&1 | tail -2"),
]

# Strings that must never appear in the subject's channel. Sentinels the attacks
# print on success, plus content read at run time out of the real grading zone.
SENTINELS = ("HOSTFS-REACHED", "DNS-OK", "EGRESS-OK", "REFERENCE_ANSWER",
             "expected.duckdb", "ATTACH-OK")

# A needle inside an error message about the path we just typed is not a leak:
# `cat: /grading/expected.duckdb: No such file` contains the filename precisely
# because the file is not there. Lines like this are excluded before matching.
NOT_A_LEAK = ("no such file", "cannot access", "not found", "permission denied",
              "no such device", "cannot open")

# Shell work a legitimate agent would do. These must SUCCEED and must appear in
# the action log with their targets attributed.
LEGITIMATE = [
    ("orientation", "pwd && ls -la && git log --oneline -3"),
    # psql is checked separately and non-fatally: it is NOT in the image, which
    # is a gap worth recording rather than a reason to fail the check. GT001's
    # warehouse is a DuckDB file, so nothing here needs it.
    ("the real toolchain",
     "which git dbt python bash && python -V && dbt --version 2>&1 | head -2; "
     "which psql || echo 'psql: NOT INSTALLED'"),
    ("read the world through the filesystem", "ls -la /world/var/warehouse /world/var/sched"),
    ("write a file, the way a repair would", "printf 'shell reached the world\\n' > /workspace/.container-check && wc -c /workspace/.container-check"),
]


async def mcp_probe(argv: list[str], calls: list[tuple[str, str]],
                    attacks: list[tuple[str, str]], needles: list[str]) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=argv[0], args=argv[1:], env=None)
    out: dict = {"argv": argv, "attacks": [], "legitimate": []}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            out["server_name"] = init.serverInfo.name
            out["tools"] = sorted(t.name for t in tools.tools)
            out["tool_count"] = len(out["tools"])
            out["bash_present"] = "bash" in out["tools"]

            async def call(command: str) -> dict:
                response = await session.call_tool("bash", {"command": command})
                payload = response.structuredContent or {}
                result = payload.get("result") or {}
                return {
                    "ok": bool(payload.get("ok")),
                    "exit_code": result.get("exit_code"),
                    "output": (result.get("output") or "")[:800],
                    "error": payload.get("error"),
                    "evidence_id": payload.get("evidence_id"),
                }

            for label, command in attacks:
                row = await call(command)
                row.update({"attack": label, "command": command})
                surviving = "\n".join(
                    ln for ln in row["output"].splitlines()
                    if not any(m in ln.lower() for m in NOT_A_LEAK))
                leaked = sorted({n for n in needles if n and n in surviving})
                row["leaked_needles"] = leaked
                row["denied"] = not leaked
                out["attacks"].append(row)
            for label, command in calls:
                row = await call(command)
                row.update({"step": label, "command": command})
                out["legitimate"].append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="GT001-aov-halving")
    ap.add_argument("--project", default=f"alkera-check-{int(time.time())}")
    ap.add_argument("--image", default=ce.DEFAULT_IMAGE)
    ap.add_argument("--keep-up", action="store_true")
    ap.add_argument("--out", type=Path, default=EVIDENCE / "container-check.json")
    args = ap.parse_args()

    ok, version = ce.docker_available()
    if not ok:
        print(json.dumps({"docker": False, "detail": version}, indent=2))
        return 2

    spec = taskspec.load(args.task)
    # OUTSIDE the repo. A bind mount publishes its host path in the container's
    # mountinfo, so a scratch directory inside the source tree would have this
    # check hand the container the one string it is checking for.
    scratch = Path(tempfile.gettempdir()) / "alkera-container-check" / str(int(time.time()))
    shutil.rmtree(scratch, ignore_errors=True)
    world_root, grading = scratch / "world", scratch / "grading"

    print(f"  building {spec.task_id} world on the host ...")
    episode.prepare_task(spec)
    world = episode.build_world(spec, world_root, grading)
    # bait the zone the way a rollout does, so the read attempts have something
    # real to fail against rather than a path that is merely absent
    (grading / "REFERENCE_ANSWER_CONTAINER_CHECK.txt").write_text(
        "container-check canary\n", encoding="utf-8")

    evidence = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "docker_server": version,
        "task": spec.task_id,
        "host_world_root": str(world_root),
        "host_grading_zone": str(grading),
        "compose_files": [str(ce.COMPOSE_BASE), str(ce.COMPOSE_OVERLAY)],
    }

    stack = ce.ContainerEpisode(project=args.project, world_root=world_root,
                                audit_dir=scratch / "audit", image=args.image)
    try:
        print(f"  compose up -d --wait  (project {args.project}) ...")
        stack.up()
        evidence["services"] = stack.services
        evidence["image_digest"] = stack.image_digest

        cfg = ce.container_gateway_config(world, "container-check", spec.virtual_time)
        stack.prepare(cfg, spec.surface_probes, scratch / "prep")
        evidence["gateway_config_in_container"] = cfg

        print("  containment probe, from inside the container ...")
        evidence["containment"] = stack.containment_probe(
            forbidden_files=[f"{d}/manifest.yaml" for d in ce.FORBIDDEN_DIRS]
            + [str(grading / "manifest.yaml").replace("\\", "/"),
               str(grading / "expected.duckdb").replace("\\", "/")],
            host_paths=[str(world_root).replace("\\", "/"),
                        str(grading).replace("\\", "/"),
                        str(ce.REPO).replace("\\", "/"), "/c", "/host_mnt", "/mnt/c"],
            repo_root=ce.REPO)
        print(f"     contained={evidence['containment']['contained']} "
              f"egress_blocked={evidence['containment']['egress_blocked']}")

        print("  toolbelt, read from the container ...")
        evidence["toolbelt"] = sorted(stack.toolbelt())
        print(f"     {len(evidence['toolbelt'])} tools, bash "
              f"{'present' if 'bash' in evidence['toolbelt'] else 'MISSING'}")

        print("  surface probes, inside the container, on a throwaway copy ...")
        evidence["surfaces"] = stack.probe_surfaces()

        print("  real MCP session over `docker exec -i`: attacks, then real work ...")
        # needles read out of the real zone, so "denied" means the bytes that live
        # only there never crossed into the subject's channel
        manifest = (grading / "manifest.yaml").read_text(encoding="utf-8").splitlines()
        needles = list(SENTINELS) + [
            "container-check canary",
            str(ce.REPO).replace("\\", "/"),
        ] + [ln.strip() for ln in manifest if len(ln.strip()) > 24][:5]
        evidence["denial_needles"] = needles
        evidence["mcp"] = asyncio.run(
            mcp_probe(stack.mcp_argv(), LEGITIMATE, ATTACKS, needles))

        rows = []
        actions = scratch / "audit" / "actions.jsonl"
        if actions.exists():
            for line in actions.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        evidence["action_log"] = {
            "path": str(actions),
            "rows": len(rows),
            "bash_rows": sum(1 for r in rows if r.get("tool") == "bash"),
            "contract_9_2_2_keys_present": sorted(
                {"seq", "vts", "wts", "transport", "tool", "args_hash",
                 "args_summary", "op_class", "target", "rows_affected",
                 "result_hash", "result_bytes", "latency_ms", "exit", "error"}
                - set(rows[0])) if rows else ["<no rows>"],
            "sequence_contiguous": [r["seq"] for r in rows] == list(range(1, len(rows) + 1)),
            "sample_bash_rows": [
                {k: v for k, v in r.items() if k != "result"}
                for r in rows if r.get("tool") == "bash"][:6],
        }
        # the gateway's own validator: seq contiguity plus args/result hashes
        validated = ce._run(
            [str(episode.PY), "-m", "alkera_gateway.cli", "trace", str(scratch / "audit")],
            check=False)
        evidence["action_log"]["gateway_trace_validates"] = validated.returncode == 0
        evidence["action_log"]["gateway_trace_error"] = (
            validated.stderr or "")[-400:] if validated.returncode else None
    finally:
        if not args.keep_up:
            print("  compose down -v ...")
            stack.down()

    denied = [a for a in evidence.get("mcp", {}).get("attacks", []) if a["denied"]]
    worked = [c for c in evidence.get("mcp", {}).get("legitimate", [])
              if c["exit_code"] == 0]
    evidence["summary"] = {
        "episode_ran_inside_the_container": bool(evidence.get("mcp", {}).get("tool_count")),
        "bash_is_on_the_toolbelt": "bash" in evidence.get("toolbelt", []),
        "attacks_attempted": len(ATTACKS),
        "attacks_denied": len(denied),
        "every_attack_denied": len(denied) == len(ATTACKS),
        "legitimate_shell_calls_worked": f"{len(worked)}/{len(LEGITIMATE)}",
        "bash_rows_in_action_log": evidence.get("action_log", {}).get("bash_rows", 0),
        "action_log_validates": evidence.get("action_log", {}).get("gateway_trace_validates"),
        "containment_probe_passed": evidence.get("containment", {}).get("contained"),
    }
    evidence["passed"] = bool(
        evidence["summary"]["episode_ran_inside_the_container"]
        and evidence["summary"]["bash_is_on_the_toolbelt"]
        and evidence["summary"]["every_attack_denied"]
        and evidence["summary"]["bash_rows_in_action_log"] > 0
        and evidence["summary"]["action_log_validates"]
        and evidence["summary"]["containment_probe_passed"])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print("\n" + json.dumps({**evidence["summary"], "passed": evidence["passed"],
                             "artifact": str(args.out)}, indent=2))
    shutil.rmtree(scratch, ignore_errors=True)
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
