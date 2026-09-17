#!/usr/bin/env python3
"""Executed proof of the host-topology native stack. No model, no quota.

    gateway/.venv/Scripts/python.exe tasks/harness/test_native_stack.py

WHAT IS REALLY EXECUTED. A real world is built (the world builder of whichever
task is named, GT001 by default), a real `NativeStack` is brought up against it
- nine real service modules in a real child process on real loopback ports -
the scripted subject runs as a real bash process through the shims this module
installed, and the rows it left are read off disk. Then the stack comes down
and the ports are checked to be free again. Nothing here is asserted from a
literal.

WHAT IS NOT EXECUTED. MinIO (no task in the corpus REQUIRES the s3 surface, so
`needs_object_storage` is exercised as a pure function and the container is
not started here), and the Postgres-served warehouse, which native mode does
not use for the existing tasks. `gateway/NATIVE_INTEGRATION.md` records both.

It is a script and not a pytest module for the reason `test_native_driver.py`
is: it starts processes and binds ports, and a run that half-failed must print
what it got rather than a collection error.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_under_test as aut          # noqa: E402
import blindness                        # noqa: E402
import episode                          # noqa: E402
import native_stack                     # noqa: E402
import surface_audit                    # noqa: E402
import taskspec                         # noqa: E402
from alkera_gateway.native.shims import sched_cli as schedcli  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DEFAULT_TASK = "GT001-aov-halving"

OK: list[str] = []
BAD: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    (OK if cond else BAD).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


def _refused(port: int, deadline_s: float = 10.0) -> bool:
    """Does 127.0.0.1:port refuse connections, within `deadline_s`?

    The teardown check CONNECTS rather than binds: a port whose listener is
    gone can still be unbindable for a couple of minutes, because the
    connections the gate and the subject made leave the server's local port in
    TIME_WAIT. "Nothing is listening" is the property; "nothing can bind it
    yet" is the operating system's bookkeeping, and `choose_offset` treats the
    latter as busy on purpose. Safe to dial: the destination log died with the
    process, so a refused connection is recorded nowhere.

    It retries because the listening sockets outlive the process by a moment.
    Measured: immediately after `down()` returned, the first four of eleven
    ports still accepted and the rest already refused - a race, not a leak.
    """
    end = time.monotonic() + deadline_s
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1.0)
            try:
                probe.connect(("127.0.0.1", port))
            except OSError:
                return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.25)


def rows_of(audit_dir: Path) -> list[dict]:
    path = audit_dir / "actions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _plan_with_trigger_all(world: dict) -> list[str]:
    """`trigger_plan` under `ALKERA_TRIGGER_ALL`, with the env put back.

    The lever is read out of the process environment because the operator sets
    it on the `run_rollouts.py` command line, so the only honest way to test it
    is to set it.
    """
    previous = os.environ.get(native_stack.TRIGGER_ALL_ENV)
    os.environ[native_stack.TRIGGER_ALL_ENV] = "1"
    try:
        return native_stack.trigger_plan(world)
    finally:
        if previous is None:
            os.environ.pop(native_stack.TRIGGER_ALL_ENV, None)
        else:
            os.environ[native_stack.TRIGGER_ALL_ENV] = previous


def _refuses_argv(argv: list[str]) -> bool:
    """Does `./ops/sched` refuse this command line outright?

    argparse exits 2 on an unknown subcommand, which is what the CLI's own
    documented exit code for a bad command line is. Asserted by running the
    parser, not by reading the source, so a subcommand re-added by accident
    fails this.
    """
    try:
        schedcli.build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code != 0
    return False


def _default_virtual_time() -> str:
    """What a native row's `vts` says when nothing told it the world's clock."""
    result = subprocess.run(
        [str(episode.PY), "-c",
         "from alkera_gateway.config import GatewayConfig;"
         "print(GatewayConfig().virtual_time)"],
        capture_output=True, text=True, encoding="utf-8",
        env=episode.venv_env())
    return result.stdout.strip()


def _bash() -> str:
    """The POSIX shell `./ops/sched` runs under. Required, not optional."""
    found = shutil.which("bash")
    if found is None:
        raise RuntimeError("no bash on PATH; this test needs a real shell")
    return found


def sched_cases(world: dict) -> list[tuple[str, list[str], str, dict]]:
    """One case per subcommand with an MCP equivalent: (name, argv, tool, args).

    The values are read out of THIS world - the first job it declares, the
    first run in its history - so the two transports are asked the same
    question about the same world rather than a question the test invented.
    """
    state = json.loads(Path(world["scheduler"]).read_text(encoding="utf-8"))
    jobs = state["jobs"]
    job = (sorted(jobs)[0] if isinstance(jobs, dict)
           else str(jobs[0].get("id", jobs[0])))
    day = str(state.get("vnow", ""))[:10]
    runs = state.get("runs") or []
    run_id = f"run-{len(runs):06d}" if runs else ""
    cases = [("list", ["list"], "sched_list", {}),
             ("logs", ["logs"], "sched_logs", {"run_id": None})]
    if run_id:
        cases.append(("logs_one", ["logs", run_id], "sched_logs",
                      {"run_id": run_id}))
    cases.append(("trigger", ["trigger", job, "--date", day], "sched_trigger",
                  {"job_id": job, "execution_date": day, "variables": {}}))
    return cases


def _clone_world(tmp: Path, world: dict, name: str) -> dict:
    """A byte copy of the world, with every path in the manifest re-rooted.

    The same recipe `episode.probe_surfaces` uses for its throwaway copy, and
    for the same reason: the two transports must be compared over two worlds
    that started identical, because `sched_trigger` EXECUTES and would
    otherwise be comparing a run against the world its own predecessor left.
    """
    copy_root = tmp / name
    shutil.rmtree(copy_root, ignore_errors=True)
    shutil.copytree(world["world_root"], copy_root)
    clone = {k: (str(copy_root / Path(v).relative_to(world["world_root"]))
                 if k != "grading" and Path(v).is_relative_to(world["world_root"])
                 else v)
             for k, v in world.items() if isinstance(v, str)}
    clone["world_root"] = str(copy_root)
    if world.get("warehouses"):
        clone["warehouses"] = episode.reroot_warehouses(
            world["warehouses"], world["world_root"], copy_root.as_posix())
    return clone


def sched_row(tmp: Path, world: dict, audit_dir: Path, endpoints: dict,
              argv: list[str], clone_name: str = "sched_native") -> dict:
    """Run `./ops/sched argv` over a clone and return the row it wrote.

    Its own audit directory, so the row is read without having to find it among
    the episode's, and its own clone, so a `trigger` cannot move the world the
    rest of this test is measuring.
    """
    clone = _clone_world(tmp, world, clone_name)
    native_stack.install_workspace_cli(clone)
    audit = tmp / f"{clone_name}_audit"
    audit.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUTF8": "1",
           **aut.native_env(endpoints=endpoints, audit_dir=audit,
                            profiles_dir=clone.get("profiles_dir") or "",
                            scheduler=clone["scheduler"])}
    cli = Path(clone["workspace"]) / native_stack.SCHED_CLI_PATH
    if not cli.exists():
        print(f"    (the CLI was not materialised at {cli})")
        return {}
    # The materialised wrapper, run by a real shell from the working tree, so
    # what is under test is `./ops/sched` as the agent would type it - the baked
    # PYTHONPATH included, without which the CLI cannot reach Track A's
    # scheduler at all.
    proc = subprocess.run([_bash(), cli.as_posix(), *argv],
                          cwd=clone["workspace"], env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print(f"    (./ops/sched {' '.join(argv)} exited {proc.returncode}: "
              f"{(proc.stderr or '').strip()[:300]})")
    rows = rows_of(audit)
    return rows[-1] if rows else {}


def sched_pair(tmp: Path, world: dict, audit_dir: Path, endpoints: dict,
               argv: list[str], tool: str, args: dict) -> tuple[dict, dict]:
    """(the CLI's row, the MCP gateway's row) for one operation, both executed."""
    native_row = sched_row(tmp, world, audit_dir, endpoints, argv,
                           f"sched_native_{argv[0]}_{len(argv)}")
    clone = _clone_world(tmp, world, f"sched_mcp_{argv[0]}_{len(argv)}")
    audit = tmp / f"sched_mcp_{argv[0]}_{len(argv)}_audit"
    audit.mkdir(parents=True, exist_ok=True)
    config = episode.write_gateway_config(
        tmp / f"sched_mcp_{argv[0]}_{len(argv)}.json", clone, audit, "mcp-ref",
        clone.get("virtual_time") or "")
    script = tmp / "mcp_call.py"
    script.write_text(MCP_CALL, encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [str(episode.PY), str(script), str(config), tool, json.dumps(args)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=episode.venv_env())
    if proc.returncode != 0:
        print(f"    (the mcp reference call for {tool} exited "
              f"{proc.returncode}: {(proc.stderr or '').strip()[-300:]})")
        return (native_row, {})
    rows = rows_of(audit)
    return (native_row, rows[-1] if rows else {})


#: The MCP reference call, in a child process: `Gateway` takes an exclusive
#: lock on its audit directory and injects the same executing scheduler
#: `alkera_gateway.cli serve` does, and running it in-process would put this
#: test's own imports between the two transports being compared.
MCP_CALL = '''\
"""One `Gateway.call`, with the scheduler an episode gets. Writes one row."""
import json
import sys
from pathlib import Path

from alkera_gateway.config import GatewayConfig
from alkera_gateway.core import Gateway
from alkera_gateway.scheduling import episode_scheduler

config = GatewayConfig.load(Path(sys.argv[1]))
gateway = Gateway(config, scheduler=episode_scheduler(config))
try:
    print(json.dumps(gateway.call(sys.argv[2], json.loads(sys.argv[3])))[:400])
finally:
    gateway.close()
'''


def main() -> int:
    print("\n  native stack: ports, bring-up, shims, the scripted subject, teardown")
    spec = taskspec.load(DEFAULT_TASK)
    tmp = Path(tempfile.mkdtemp(prefix="alkera-native-stack-"))
    stack: native_stack.NativeStack | None = None
    try:
        # --- 1. ports, as pure functions --------------------------------
        print("\n  port allocation")
        ports = native_stack.service_ports()
        check("every port serve.py binds is in the block the allocator checks",
              {8080, 8081, 8090, 3000, 9090, 9093, 3100, 8001, 8085,
               8099, 8110} == set(ports), str(ports))
        first = native_stack.choose_offset("run/r0", ports)
        check("the offset is derived from the rollout's identity, not allocated",
              first == native_stack.choose_offset("run/r0", ports), str(first))
        # Two identities MAY land on one block when earlier blocks are occupied
        # (a live campaign holds them): the guarantee is that an occupied block
        # is skipped, not that hashes never converge. Test the scan directly.
        with socket.create_server(("127.0.0.1", first + min(ports))):
            moved = native_stack.choose_offset("run/r0", ports)
        check("...and an occupied block is skipped for the same identity",
              moved != first, f"{first} -> {moved}")
        check("an offset that would put a service outside the usable range is "
              "never a candidate",
              all(0 < offset + max(ports) < 65536
                  for offset in native_stack.OFFSET_CANDIDATES))
        check("MinIO is started only for a task that REQUIRES the s3 surface, "
              "and GT001 requires none",
              not native_stack.needs_object_storage(spec),
              str(list(getattr(spec, "required_surfaces", []) or [])))

        # --- 2. a real world, a real stack -------------------------------
        print("\n  a real world and a real stack")
        world_root, grading = tmp / "world", tmp / "grading"
        audit_dir, rollout_dir = tmp / "audit", tmp / "rollout"
        audit_dir.mkdir(parents=True)
        rollout_dir.mkdir(parents=True)
        world = episode.build_world(spec, world_root, grading)
        stack = native_stack.NativeStack(world=world, audit_dir=audit_dir,
                                         rollout_dir=rollout_dir, spec=spec,
                                         key="test/native-stack")
        endpoints = stack.up()
        check("every one of the nine services is addressed on loopback at its "
              "own port plus the offset",
              all(endpoints[name] ==
                  f"http://127.0.0.1:{port + stack.offset}"
                  for name, (_, port) in aut.NATIVE_SERVICES.items()),
              f"offset={stack.offset}")
        check("the endpoint map went through resolve_endpoints, so a key this "
              "module invented would have been refused",
              set(aut.NATIVE_SERVICES) <= set(endpoints)
              and "postgres" in endpoints and "s3_bucket" in endpoints)
        check("up() is idempotent: a second call starts nothing and returns "
              "the same map", stack.up() == endpoints)

        # --- 3. the destination log --------------------------------------
        print("\n  the destination log the detectors read")
        destinations = blindness.load_destinations(audit_dir)
        check("destinations.jsonl exists before the episode - an absent one is "
              "`unobserved_network`, hard, and throws the rollout away",
              destinations is not None and len(destinations) >= 10,
              str(len(destinations or [])))
        allowed = aut.allowed_destinations(endpoints)
        check("the harness's own readiness check dialled NOTHING: every row is "
              "still at count 0",
              all(row.get("count") == 0 for row in destinations or []),
              json.dumps([r for r in (destinations or []) if r.get("count")]))
        check("every row names the host the EPISODE addresses, not the DNS "
              "alias it would have inside the episode network",
              all(row.get("host") == "127.0.0.1" for row in destinations or []))
        check("MinIO's audit receiver is served and is NOT a destination - the "
              "stack dials it, the episode never does",
              all(row.get("port") != 8110 + stack.offset
                  for row in destinations or []))
        check("...and no row that IS a destination is outside the episode's "
              "own surfaces but the aggregate, which stays at 0",
              not blindness.network_violations(destinations, allowed,
                                               aut.normalise_host))

        # --- 4. the shims -------------------------------------------------
        print("\n  the PATH-front shims")
        check("the duckdb shim is in front of PATH for anything this process "
              "starts",
              shutil.which("duckdb") is not None
              and Path(shutil.which("duckdb")).parent == stack.shim_dir,
              str(shutil.which("duckdb")))
        check("dbt, python and git are wrapped too; psql and aws are NOT - "
              "their rows are the tailers'",
              all((stack.shim_dir / name).exists()
                  for name in ("duckdb", "dbt", "python", "git"))
              and not (stack.shim_dir / "psql").exists()
              and not (stack.shim_dir / "aws").exists())

        # --- 5. the scripted subject -------------------------------------
        print("\n  a scripted subject, a real shell, real rows")
        # `rollout.py` does this right after the world is built and before the
        # fingerprint; here it is done before the "before" picture for the same
        # reason - the CLI is part of the repo the agent is handed, not
        # something that appears in the tree mid-episode.
        cli = native_stack.install_workspace_cli(world)
        check("./ops/sched is materialised INTO the working tree, the way a "
              "team's own ops CLI is in its repo",
              (Path(world["workspace"]) / native_stack.SCHED_CLI_PATH).is_file(),
              json.dumps(cli["files"]))
        before = episode.tree_manifest(world_root)
        # The surface gate first, exactly as `rollout.py` runs it: its nine
        # `/health` polls are the rows `blindness.is_harness_row` must strip,
        # and a test that never made them would prove the stripping on an
        # empty set.
        gate = surface_audit.probe_native(
            probes=spec.surface_probes, endpoints=endpoints, world=world,
            schemas=surface_audit.warehouse_schemas(spec))
        check("the gate asked every declared surface and every service the "
              "task did not speak for",
              gate["probed"] and len(gate["surfaces"]) >= 9,
              str(len(gate["surfaces"])))
        agent = native_stack.run_scripted(
            rollout_dir=rollout_dir, world=world, audit_dir=audit_dir,
            endpoints=endpoints, wall_clock_s=300)
        after = episode.tree_manifest(world_root)
        check("the subject's shell ran the whole script and exited 0",
              agent.returncode == 0 and agent.exit_reason == "completed",
              (agent.final_text + agent.stderr_tail)[-300:])
        check("...and it is reported as the native driver with a model name "
              "nobody can mistake for a measurement",
              agent.driver == "native"
              and agent.model == "scripted-native-subject")
        rows = rows_of(audit_dir)
        subject = blindness.subject_rows(rows)
        tools = [row["tool"] for row in subject]
        check("the duckdb CLI left a row through the shim",
              any(t == "duckdb.statement" for t in tools), str(tools))
        duckdb_row = next((r for r in subject if r["tool"] == "duckdb.statement"),
                          {})
        check("...whose targets are TABLE names from sql.inspect_sql, not the "
              "warehouse file - the parity the amendment asks for",
              bool(duckdb_row.get("targets"))
              and all("." in t for t in duckdb_row.get("targets") or []),
              json.dumps(duckdb_row.get("targets")))
        check("a vendor service left a row of its own",
              any(t.startswith("jira.") for t in tools), str(tools))
        check("every row carries the 23 agent.md 9.2.2 keys and no others",
              rows and len({len(row) for row in rows}) == 1
              and len(rows[0]) == 23,
              str(sorted({len(row) for row in rows})))
        check("the surface gate's own polls are harness rows, not the "
              "subject's", len(rows) > len(subject)
              and all(t.endswith(".health") for t in
                      {r["tool"] for r in rows} - {r["tool"] for r in subject}),
              f"{len(subject)} subject of {len(rows)} total")
        check("the subject's `sed` really changed the working tree",
              any(k for k in set(before) | set(after)
                  if before.get(k) != after.get(k)))

        # --- 5b. `./ops/sched` against a real MCP gateway -----------------
        # The equivalence claim is that a subcommand's row is the row the MCP
        # tool of that name writes. It is checked by RUNNING both: the CLI over
        # this world, an `alkera_gateway.Gateway` with the same injected
        # scheduler over a byte-copy of it, and the two rows compared.
        print("\n  ./ops/sched vs the MCP gateway, over a clone of one world")
        for subcommand, argv, tool, args in sched_cases(world):
            native_row, mcp_row = sched_pair(tmp, world, audit_dir, endpoints,
                                             argv, tool, args)
            check(f"`./ops/sched {' '.join(argv)}` -> {native_row.get('tool')}, "
                  f"op_class and targets identical to {tool}",
                  (native_row.get("tool") == f"sched.{tool}"
                   and native_row.get("op_class") == mcp_row.get("op_class")
                   and native_row.get("targets") == mcp_row.get("targets")
                   and native_row.get("read_targets")
                   == mcp_row.get("read_targets")),
                  f"native {native_row.get('op_class')} "
                  f"{native_row.get('targets')} / mcp "
                  f"{mcp_row.get('op_class')} {mcp_row.get('targets')}")
        check("EVERY subcommand has an mcp equivalent - there is no `advance`, "
              "because `Operations` has none and a native-only way to move the "
              "world's clock would be a capability the mcp agent lacks",
              set(schedcli.SUBCOMMANDS) == {"list", "logs", "trigger"}
              and schedcli.build_parser().parse_known_args(
                  ["list"])[0].subcommand == "list"
              and _refuses_argv(["advance", "1"]),
              str(sorted(schedcli.SUBCOMMANDS)))
        state_vnow = json.loads(Path(world["scheduler"]).read_text(
            encoding="utf-8"))["vnow"]
        vts_row = sched_row(tmp, world, audit_dir, endpoints, ["list"],
                            "sched_vts")
        check("a row carries the WORLD's vnow, read out of the file "
              "ALKERA_SCHEDULER names - not the GatewayConfig default a shim "
              "with no scheduler in its environment would stamp",
              vts_row.get("vts") == state_vnow
              and vts_row.get("vts") != _default_virtual_time(),
              f"{vts_row.get('vts')} vs state {state_vnow} "
              f"vs default {_default_virtual_time()}")
        declared = native_stack.scheduler_jobs(world)
        check("the jobs the subject may trigger are read off the world's own "
              "state file, in DECLARATION order",
              declared == list(json.loads(Path(world["scheduler"]).read_text(
                  encoding="utf-8"))["jobs"]),
              str(declared))
        check(f"...and `{native_stack.TRIGGER_ALL_ENV}` is what turns the "
              f"subject's one trigger into all {len(declared)} of them",
              native_stack.trigger_plan(world) == declared[:1]
              and _plan_with_trigger_all(world) == declared,
              f"{native_stack.trigger_plan(world)} -> "
              f"{_plan_with_trigger_all(world)}")
        job = sched_cases(world)[-1][1][1]
        by_job = sched_row(tmp, world, audit_dir, endpoints, ["logs", job],
                           "sched_by_job")
        check(f"`./ops/sched logs {job}` - the spelling `python -m "
              f"world.scheduler <root> logs <job>` uses - is still ONE "
              f"sched_logs, targeted at what was asked for",
              by_job.get("tool") == "sched.sched_logs"
              and by_job.get("targets") == [f"scheduler:{job}"]
              and by_job.get("exit") == "ok",
              json.dumps({k: by_job.get(k)
                          for k in ("tool", "targets", "exit")}))
        check("the scheduler surface is LIVE for this world and its gate is "
              "the state file, not a service",
              surface_audit._scheduler_state(world["scheduler"])[0]
              and surface_audit.native_surface_for("sched_logs") == "sched",
              str(surface_audit._scheduler_state(world["scheduler"])[2]))

        # --- 6. teardown ---------------------------------------------------
        print("\n  teardown")
        stack.down()
        still_open = [port + stack.offset for port in ports
                      if not _refused(port + stack.offset)]
        check("every port the stack bound now refuses a connection",
              not still_open, str(still_open))
        stack.down()
        check("down() twice is not an error", True)
        stack = None
    finally:
        if stack is not None:
            stack.down()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  {len(OK)} passed, {len(BAD)} failed")
    for label in BAD:
        print(f"    FAILED: {label}")
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
