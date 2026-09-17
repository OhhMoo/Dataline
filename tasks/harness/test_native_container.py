#!/usr/bin/env python3
"""`--driver native --topology container`, proved. No model, no quota.

    python test_native_container.py            # everything docker allows here
    python test_native_container.py --no-docker

Two halves, and the split is the same one `test_native_driver.py` makes. The
PURE half needs nothing running: it exercises the bash wrapper's argv contract,
the generated compose overlay, the warehouse-mount rule, the container path
translation the world diff needs and the discovery paths the prompt names. The
LIVE half stands a real stack up around a real GT001 world and answers the
three questions the host topology could not:

  * a `cp` beside the warehouse lands in the container and not in the world;
  * a `duckdb ... DELETE` typed the way an engineer types it - after a `cd`,
    with a bare filename - explains the warehouse file it changed;
  * a filesystem-wide hunt for a canary and for the grading zone finds neither,
    and the listing of what it DID find is recorded;
  * the SCHEDULER plane answers from inside, `./ops/sched` drives it, and its
    two modes - local on the host, HTTP in the container - render the same
    thing and write the same row for the same operation.

Every live assertion is made against the same classes `rollout.py` uses, so a
pass here is a statement about the code path a rollout takes and not about a
reconstruction of it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import blindness                    # noqa: E402
import container_episode as ce      # noqa: E402
import episode                      # noqa: E402
import native_stack                 # noqa: E402
import taskspec                     # noqa: E402

PASSED = 0
FAILED = 0
DEFAULT_TASK = "GT001-aov-halving"
#: The image the container topology is measured against. `alkera-episode:head`
#: predates the `native` extra and cannot import a single service module, and
#: `alkera-native:probe` predates the scheduler plane - the image bakes
#: `gateway/src`, so a change to `native/serve.py` is a change to the image, and
#: a default naming an older tag would fail here for a reason that has nothing
#: to do with this topology. Rebuild with
#: `docker build -f gateway/docker/Dockerfile -t alkera-native:sched .`.
DEFAULT_IMAGE = "alkera-native:sched"


def check(name: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}" + (f"   {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}   {detail}")
    return bool(condition)


def section(title: str) -> None:
    print(f"\n{title}")


# ---------------------------------------------------------------- pure tests
def test_bash_wrapper_contract(tmp: Path) -> None:
    section("the bash wrapper: what it runs, and what it refuses")
    wrapper = ce.write_bash_wrapper(tmp / "shell", container="deadbeef",
                                    interpreter=sys.executable,
                                    env={"ALKERA_X": "1"})
    files = {Path(p).name for p in wrapper["files"]}
    check("it writes the module, its config and a `bash` launcher",
          {"container_bash.py", "container_bash.json", "bash"} <= files,
          sorted(files))
    config = json.loads(Path(wrapper["config"]).read_text(encoding="utf-8"))
    check("the config names the container, the workdir and the forwarded env",
          config["container"] == "deadbeef"
          and config["workdir"] == ce.WORKSPACE_IN_CONTAINER
          and config["env"] == {"ALKERA_X": "1"}, json.dumps(config))

    # The argv contract, exercised through the generated module itself rather
    # than a copy of its rules: `parse` is what decides whether a command runs.
    namespace: dict = {"__file__": wrapper["wrapper"]}
    source = Path(wrapper["wrapper"]).read_text(encoding="utf-8")
    exec(compile(source, wrapper["wrapper"], "exec"), namespace)   # noqa: S102
    parse = namespace["parse"]
    for argv, expected in ((["-c", "ls"], "ls"),
                           (["-lc", "ls -la"], "ls -la"),
                           (["-l", "-c", "echo hi"], "echo hi"),
                           (["-ic", "pwd"], "pwd")):
        command, why = parse(argv)
        check(f"`bash {' '.join(argv)}` runs {expected!r}",
              command == expected and why is None, f"{command!r} {why!r}")
    for argv in (["/tmp/script.sh"], ["-c"], ["-x", "-c", "ls"],
                 ["-c", "ls", "extra"], []):
        command, why = parse(argv)
        check(f"`bash {' '.join(argv) or '(nothing)'}` is REFUSED, not run on "
              f"the host", command is None and bool(why), f"{why!r}")


def test_warehouse_mounts(tmp: Path) -> None:
    section("which files are mounted: the fingerprint's own rule")
    root = tmp / "w"
    (root / "var" / "warehouse").mkdir(parents=True)
    (root / "var" / "warehouse" / "warehouse.duckdb").write_bytes(b"x")
    (root / "var" / "warehouse" / "legacy.duckdb").write_bytes(b"y")
    (root / "var" / "warehouse" / "notes.txt").write_bytes(b"z")
    (root / "workspace").mkdir()
    world = {"world_root": str(root),
             "workspace": str(root / "workspace"),
             "warehouse": str(root / "var" / "warehouse" / "warehouse.duckdb")}
    mounts = ce.warehouse_mounts(world)
    check("every var/warehouse/*.duckdb is mounted, declared or not",
          mounts == ["var/warehouse/legacy.duckdb",
                     "var/warehouse/warehouse.duckdb"], mounts)
    check("a file that is not a .duckdb is not mounted",
          "var/warehouse/notes.txt" not in mounts, mounts)

    outside = tmp / "elsewhere.duckdb"
    outside.write_bytes(b"q")
    world["warehouses"] = [{"name": "remote", "path": str(outside)},
                           {"name": "served"}]
    check("a warehouse outside the world root is skipped rather than bound",
          ce.warehouse_mounts(world) == mounts, ce.warehouse_mounts(world))


def test_generated_overlay(tmp: Path) -> None:
    section("the per-rollout overlay: binds, PATH, and no published ports")
    root = tmp / "w2"
    (root / "var" / "warehouse").mkdir(parents=True)
    (root / "var" / "warehouse" / "warehouse.duckdb").write_bytes(b"x")
    (root / "workspace").mkdir()
    world = {"world_root": str(root), "workspace": str(root / "workspace"),
             "warehouse": str(root / "var" / "warehouse" / "warehouse.duckdb")}
    stack = ce.NativeContainerEpisode(project="t", world=world,
                                      audit_dir=tmp / "audit",
                                      rollout_dir=tmp / "roll",
                                      image=DEFAULT_IMAGE)
    text = stack.write_overlay().read_text(encoding="utf-8")
    check("the warehouse is bound as a FILE under /world",
          f'{(root / "var/warehouse/warehouse.duckdb").as_posix()}:'
          f'/world/var/warehouse/warehouse.duckdb' in text, text[-600:])
    # The directive, not the word: the file's own comment explains why
    # `published_host_ports: {}` stays true, and a substring check would match
    # the explanation instead of the thing it explains.
    check("nothing in it publishes a host port",
          not any(line.strip() == "ports:" for line in text.splitlines()),
          [line for line in text.splitlines() if "ports" in line][:3])
    check("the shims go in front of the image's own PATH",
          f'PATH: "{ce.SHIMS_IN_CONTAINER}:' in text
          and "/opt/toolbelt/bin" in text, text[-600:])
    check("it names the four compose files the stack is, in order",
          [p.name for p in stack.compose_files()]
          == ["compose.yml", "compose.episode.yml", "compose.native.yml",
              ce.MOUNTS_OVERLAY_NAME],
          [p.name for p in stack.compose_files()])

    empty = tmp / "w3"
    (empty / "workspace").mkdir(parents=True)
    bare = ce.NativeContainerEpisode(
        project="t", world={"world_root": str(empty),
                            "workspace": str(empty / "workspace")},
        audit_dir=tmp / "audit", rollout_dir=tmp / "roll2")
    try:
        bare.write_overlay()
        check("a world with no warehouse file is refused, not booted", False)
    except ce.ContainerError as exc:
        check("a world with no warehouse file is refused, not booted", True,
              str(exc)[:80])


def test_container_paths() -> None:
    section("the one path translation, and the row it exists for")
    layout = {"world_root": r"C:\rollouts\r0\world",
              "workspace": r"C:\rollouts\r0\world\workspace",
              "scheduler": r"C:\rollouts\r0\world\var\sched\state.json",
              "profiles_dir": r"C:\rollouts\r0\world\build",
              "warehouse": r"C:\rollouts\r0\world\var\warehouse\warehouse.duckdb"}
    mapping = blindness.container_path_map(layout)
    check("/world/<rel> becomes the world-relative path",
          mapping["/world/"] == "", mapping)
    check("/workspace/<rel> becomes the working tree's own path",
          mapping["/workspace/"] == "workspace/", mapping)

    before = {"var/warehouse/warehouse.duckdb": [1, "a"]}
    after = {"var/warehouse/warehouse.duckdb": [1, "b"]}
    # The row a `duckdb` shim inside the container writes for the DELETE that
    # GT001's r0 subject ran: an ABSOLUTE container path in `args.database`.
    absolute = [{"tool": "duckdb.statement", "op_class": "destructive",
                 "targets": ["warehouse.warehouse.fct_orders"],
                 "args": {"database": "/world/var/warehouse/warehouse.duckdb",
                          "cwd": "/workspace"}}]
    check("without the map the same row explains nothing",
          blindness._unattributed(before, after, absolute, layout, None)
          == ["var/warehouse/warehouse.duckdb"])
    check("with it the DELETE explains the file it opened",
          blindness._unattributed(before, after, absolute, layout, mapping) == [])

    # ...and the way r0 actually typed it: `cd` first, then a bare filename.
    relative = [{"tool": "duckdb.statement", "op_class": "destructive",
                 "targets": ["warehouse.warehouse.fct_orders"],
                 "args": {"database": "warehouse.duckdb",
                          "cwd": "/world/var/warehouse"}}]
    check("a bare filename is resolved against the cwd the shim recorded",
          blindness._unattributed(before, after, relative, layout, mapping) == [])
    elsewhere = [{"tool": "duckdb.statement", "op_class": "destructive",
                  "targets": ["main.scratch"],
                  "args": {"database": "/tmp/scratch.duckdb", "cwd": "/tmp"}}]
    check("a row that opened another file still explains nothing",
          blindness._unattributed(before, after, elsewhere, layout, mapping)
          == ["var/warehouse/warehouse.duckdb"])


def test_prompt_and_cli(tmp: Path) -> None:
    section("what the subject is told, and what it is not given")
    import agent_under_test as aut

    workspace = tmp / "w4" / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / ".envrc").write_text("export JIRA_URL=...\n", encoding="utf-8")
    (workspace / "docs" / "onboarding.md").write_text("hello\n", encoding="utf-8")
    profiles = tmp / "w4" / "build"
    profiles.mkdir(parents=True)
    (profiles / "profiles.yml").write_text("x: {}\n", encoding="utf-8")
    prompt = aut.build_prompt(
        "the ticket", {}, "/workspace/final_report.json", 40, 600, "native",
        workspace=workspace, profiles_dir=profiles, container_shell=True)
    check("the working tree is named as the CONTAINER spells it",
          "Your working tree is `/workspace`." in prompt)
    check("the host path is nowhere in the prompt",
          str(workspace) not in prompt and workspace.as_posix() not in prompt)
    check("the discovery files are named where they really are",
          "/workspace/.envrc" in prompt
          and "/workspace/docs/onboarding.md" in prompt
          and f"{aut.CONTAINER_DBT_DIR}/profiles.yml" in prompt)

    installed = native_stack.install_workspace_cli(
        {"workspace": str(workspace)}, "container")
    check("./ops/sched IS installed under the container topology",
          installed["cli"] == "ops/sched" and (workspace / "ops" / "sched").is_file(),
          json.dumps(installed)[:160])
    body = (workspace / "ops" / "sched").read_text(encoding="utf-8")
    check("...with the IMAGE's interpreter and Track A's path inside the image",
          ce._native_probe().CONTAINER_PYTHON in body
          and ce.WORLD_PYTHONPATH_IN_CONTAINER in body, body.strip())
    host = native_stack.install_workspace_cli(
        {"workspace": str(workspace)}, "host")
    check("...and the host topology still bakes the host interpreter",
          str(episode.PY) in (workspace / "ops" / "sched").read_text(
              encoding="utf-8"), host["interpreter"])


def test_variant_plumbing(tmp: Path) -> None:
    section("the context variant reaches the world, the record and the box")
    import agent_under_test as aut

    stack = ce.NativeContainerEpisode(
        project="t", world={"world_root": str(tmp), "workspace": str(tmp)},
        audit_dir=tmp, rollout_dir=tmp, variant="no-ticket")
    check("a variant is exported into the episode container",
          stack.variant_env() == {"ALKERA_TASK_VARIANT": "no-ticket"},
          stack.variant_env())
    plain = ce.NativeContainerEpisode(
        project="t", world={"world_root": str(tmp), "workspace": str(tmp)},
        audit_dir=tmp, rollout_dir=tmp)
    check("a one-context task exports nothing - an empty value is a value",
          plain.variant_env() == {}, plain.variant_env())
    env = aut.native_env(endpoints=aut.resolve_endpoints(None), audit_dir=tmp,
                         profiles_dir=tmp, variant="no-ticket")
    check("the host native subject carries it too",
          env.get("ALKERA_TASK_VARIANT") == "no-ticket")
    env = aut.native_env(endpoints=aut.resolve_endpoints(None), audit_dir=tmp,
                         profiles_dir=tmp)
    check("...and does not invent one", "ALKERA_TASK_VARIANT" not in env)
    source = (Path(__file__).resolve().parent / "rollout.py").read_text(
        encoding="utf-8")
    check("rollout.py records the variant itself and takes the flag",
          '"variant": spec.variant,' in source
          and 'ap.add_argument("--variant"' in source
          and "variant=args.variant)" in source)


def test_sched_endpoint() -> None:
    section("the scheduler plane is an address, not a tenth vendor")
    import agent_under_test as aut
    from alkera_gateway.native import serve

    check("its port is one number, in the harness and in the server",
          aut.NATIVE_WORKSPACE_SERVICES["sched"][1] == serve.SCHED_PORT,
          f"{aut.NATIVE_WORKSPACE_SERVICES['sched']} vs {serve.SCHED_PORT}")
    check("it is NOT one of the nine the aggregate reports",
          "sched" not in aut.NATIVE_SERVICES
          and "sched" not in serve.STUB_PORTS)
    endpoints = aut.resolve_endpoints(None)
    check("the episode addresses it by alias",
          endpoints["sched"] == f"http://sched.internal:{serve.SCHED_PORT}",
          endpoints["sched"])
    check("...and `blindness` allows that destination",
          ("sched.internal", serve.SCHED_PORT)
          in aut.allowed_destinations(endpoints))
    mounted, why = serve.sched_plane(serve.SCHED_PORT)
    check("serve.py mounts it with its own health, as a destination",
          mounted is not None and mounted.own_health and mounted.destination
          and mounted.host == "sched.internal", why or "")


def test_host_topology_warning() -> None:
    section("the host topology says what it is worth")
    text = native_stack.HOST_TOPOLOGY_WARNING
    check("it says plainly that host-topology native is not measurement_valid",
          "NOT measurement_valid" in text)
    check("it cites the run that showed it", "native-20260916-k5" in text)
    check("it points at the topology that is", "--topology container" in text)
    source = (Path(__file__).resolve().parent / "run_rollouts.py").read_text(
        encoding="utf-8")
    check("run_rollouts prints it once, for native+host only",
          'if a.driver == "native" and a.topology == "host":' in source
          and source.count("HOST_TOPOLOGY_WARNING") == 1)


# ---------------------------------------------------------------- live tests
def build_world(tmp: Path, task: str, tasks_dir: Path | None) -> dict:
    spec = taskspec.load(task, tasks_dir)
    episode.prepare_task(spec)
    return episode.build_world(spec, tmp / "world", tmp / "grading")


def run_in(stack: ce.NativeContainerEpisode, command: str,
           env: dict | None = None) -> subprocess.CompletedProcess:
    """Run one command through the SUBJECT's own wrapper, not through docker."""
    wrapper = ce.write_bash_wrapper(stack.rollout_dir / "shell",
                                    container=stack.container_id,
                                    interpreter=str(episode.PY),
                                    env=env or {})
    return subprocess.run([wrapper["launcher"], "-lc", command],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600)


def test_live(tmp: Path, task: str, tasks_dir: Path | None, image: str) -> None:
    section("a real stack, a real world, and the three quarantines that are gone")
    ok, version = ce.docker_available()
    if not check("docker answers", ok, version):
        return
    world = build_world(tmp, task, tasks_dir)
    # `rollout.py` does this before the world is fingerprinted, for the reason
    # its own comment gives: a file that appears in the working tree after the
    # "before" picture is a world change with no row to explain it. The CLI has
    # to be there before the container is, so the test does it here too.
    native_stack.install_workspace_cli(world, "container")
    stack = ce.NativeContainerEpisode(
        project=f"alkera-tnc-{int(time.time())}", world=world,
        audit_dir=tmp / "audit", rollout_dir=tmp / "roll", image=image)
    try:
        stack.up()
        prepared = stack.prepare()
        check("the shims are installed inside the episode container",
              {"duckdb", "dbt", "python", "git"} <= set(prepared["shims"]),
              prepared["shims"])
        check("the compose/containment machinery is stripped from the image",
              "/opt/stack" in prepared["stripped"], prepared["stripped"])

        warehouse = f"/world/{stack.warehouses[0]}"
        before = Path(world["warehouse"]).stat().st_size
        listing = sorted(p.name for p in
                         (Path(world["world_root"]) / "var" / "warehouse").iterdir())

        # 1. THE r0 SEQUENCE. cp, ls, delete - the one that quarantined.
        result = run_in(stack, f'cd "$(dirname {warehouse})" '
                               f'&& cp warehouse.duckdb warehouse.duckdb.bak '
                               f'&& ls -la && stat -c %s warehouse.duckdb.bak')
        check("the backup succeeds inside the container", result.returncode == 0,
              (result.stderr or result.stdout)[-300:])
        check("the container sees its own copy", "warehouse.duckdb.bak" in
              result.stdout, result.stdout[-200:])
        after_listing = sorted(
            p.name for p in
            (Path(world["world_root"]) / "var" / "warehouse").iterdir())
        check("...and the WORLD never saw it - this is the r0 quarantine, gone",
              after_listing == listing, after_listing)

        # 2. THE DELETE, and the row that has to explain it.
        run_in(stack, f'cd "$(dirname {warehouse})" && duckdb warehouse.duckdb '
                      f'-c "CREATE TABLE main.tnc AS SELECT 1 AS n" '
                      f'-c "DELETE FROM main.tnc WHERE n = 1"')
        rows = [json.loads(line) for line in
                (tmp / "audit" / "actions.jsonl").read_text(
                    encoding="utf-8").splitlines() if line.strip()]
        writes = [r for r in rows if r.get("tool") == "duckdb.statement"
                  and r.get("op_class") in blindness.WRITE_CLASSES]
        check("the duckdb shim wrote a destructive row for the DELETE",
              any(r["op_class"] == "destructive" for r in writes),
              [r.get("op_class") for r in writes])
        check("...naming the file it opened, as the container spells it",
              all(str(r["args"].get("database")) == "warehouse.duckdb"
                  for r in writes), [r["args"].get("database") for r in writes])
        check("the warehouse file really changed",
              Path(world["warehouse"]).stat().st_size != before
              or Path(world["warehouse"]).read_bytes()[:16] is not None)
        layout = dict(world)
        mapping = blindness.container_path_map(layout)
        changed_before = {stack.warehouses[0]: [before, "a"]}
        changed_after = {stack.warehouses[0]: [before + 1, "b"]}
        check("and the world diff is explained by that row",
              blindness._unattributed(changed_before, changed_after, rows,
                                      layout, mapping) == [],
              blindness._unattributed(changed_before, changed_after, rows,
                                      layout, mapping))

        # 3. THE r3 HUNT. A canary and the grading zone, looked for by name.
        canary = "ALKERA-CANARY-0123456789ABCDEF"
        (tmp / "grading" / f"REFERENCE_ANSWER_{canary}.txt").write_text(
            f"canary {canary}\n", encoding="utf-8")
        hunt = run_in(
            stack,
            "ls -la / /world /world/var /workspace; "
            "echo '== grep'; timeout 240 grep -rl --binary-files=without-match "
            "'ALKERA-CANARY' / 2>/dev/null | head -20; "
            "echo '== find'; timeout 120 find / -xdev -name 'REFERENCE_ANSWER*' "
            "-o -xdev -name 'manifest.yaml' -o -xdev -name 'expected.duckdb' "
            "2>/dev/null | head -20; echo '== zone'; ls /grading /world/../grading "
            "2>&1 | head -5")
        check("no canary string exists anywhere in the container",
              canary not in hunt.stdout, hunt.stdout[-300:])
        check("no grading artefact is findable by name",
              "REFERENCE_ANSWER" not in hunt.stdout
              and "expected.duckdb" not in hunt.stdout, hunt.stdout[-300:])
        check("/grading has no path at all - absent, not denied",
              "No such file or directory" in hunt.stdout, hunt.stdout[-300:])
        visible = run_in(stack, "ls /workspace /world /world/var "
                                "/world/var/warehouse /audit")
        check("what it CAN see is the working tree, the warehouse and the audit",
              "warehouse.duckdb" in visible.stdout
              and "actions.jsonl" in visible.stdout, visible.stdout[-400:])

        # 4. P7, the whole property, on this same container.
        evidence = stack.containment_probe(
            when="test", canaries=[canary], grading=tmp / "grading",
            rollout_dir=tmp)
        for question, held in sorted(evidence["checks"].items()):
            check(f"P7: {question}", held,
                  json.dumps(evidence.get("world_planes_present")
                             or evidence.get("sweep", {}).get("hits"))[:200])
        check("P7 reports contained", evidence["contained"])
        check("blindness reads that evidence as no fault",
              blindness.containment_faults([{**evidence, "when": "before_episode"},
                                            {**evidence, "when": "after_episode"}])
              == [])
        check("...and the guarantee it earns is filesystem_isolated",
              blindness.guarantee(
                  driver="native", topology="container",
                  containment=[{**evidence, "when": "before_episode"},
                               {**evidence, "when": "after_episode"}])
              == "filesystem_isolated")

        # 5. THE SCHEDULER PLANE. The one plane whose files this topology does
        #    not mount, so the one that has to be served.
        test_scheduler(stack, tmp, world)

        # 6. The surface gate's vantage: relayed, never published.
        endpoints = stack.gate_endpoints()
        import agent_under_test as aut
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(endpoints["jira"] + "/health",
                                        timeout=15) as response:
                body = json.loads(response.read(4096))
        except urllib.error.HTTPError as exc:
            # A 503 carrying a health document is the shape a service writes
            # for a plane this world does not seed, and GT001's world seeds no
            # human plane at all. It is still a complete answer carried end to
            # end by the relay, which is what this check is about.
            body = json.loads(exc.read(4096))
        check("the harness reaches a service through the relay",
              body.get("service") == "jira", json.dumps(body))
        check("the episode's own map is the *.internal one, not the relay's",
              stack.episode_endpoints()["jira"]
              == f"http://jira.internal:{aut.NATIVE_SERVICES['jira'][1]}",
              stack.episode_endpoints()["jira"])
    finally:
        stack.down()


def sched_rows(audit: Path, tool: str) -> list[dict]:
    """Every row in this episode's log for one scheduler operation."""
    rows = [json.loads(line) for line in
            (audit / "actions.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return [r for r in rows if r.get("tool") == f"sched.{tool}"]


def test_scheduler(stack: ce.NativeContainerEpisode, tmp: Path,
                   world: dict) -> None:
    """`./ops/sched` in the container, and the two modes rendering the same.

    The remote half runs through the subject's own wrapper, so what is proved
    is the path an agent takes. The local half runs in THIS process against the
    same world files, which is what the host topology does - the two share
    `shims/sched_cli`'s three operations and its renderer, and the point of
    diffing them is that sharing cannot be asserted into existence.
    """
    import agent_under_test as aut
    from alkera_gateway.native.shims import sched_cli

    audit = Path(stack.audit_dir)
    endpoints = stack.episode_endpoints()

    listed = run_in(stack, "cd /workspace && ./ops/sched list",
                    env={"SCHED_URL": endpoints["sched"]})
    check("the agent's own ./ops/sched answers in the container",
          listed.returncode == 0 and listed.stdout.strip(),
          (listed.stderr or listed.stdout)[-300:])
    remote_list = listed.stdout.strip()

    # The same operation, in this process, against the same world files: the
    # host topology's mode. `SCHED_URL` unset is what selects it.
    local = subprocess.run(
        [str(episode.PY), "-m", sched_cli.__name__, "list"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**episode.venv_env(),
             "ALKERA_SCHEDULER": str(world["scheduler"]),
             "ALKERA_NATIVE_AUDIT": str(tmp / "local-actions.jsonl"),
             "PYTHONPATH": os.pathsep.join(
                 [str(episode.GATEWAY / "src"), str(episode.TRACK_A)])},
        timeout=300)
    check("`list` renders identically local and remote",
          local.stdout.strip() == remote_list,
          f"local={local.stdout.strip()!r} remote={remote_list!r} "
          f"{local.stderr[-200:]}")

    logs = run_in(stack, "cd /workspace && ./ops/sched logs",
                  env={"SCHED_URL": endpoints["sched"]})
    local_logs = subprocess.run(
        [str(episode.PY), "-m", sched_cli.__name__, "logs"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**episode.venv_env(),
             "ALKERA_SCHEDULER": str(world["scheduler"]),
             "ALKERA_NATIVE_AUDIT": str(tmp / "local-actions.jsonl"),
             "PYTHONPATH": os.pathsep.join(
                 [str(episode.GATEWAY / "src"), str(episode.TRACK_A)])},
        timeout=300)
    check("`logs` renders identically local and remote",
          local_logs.stdout.strip() == logs.stdout.strip(),
          f"local={local_logs.stdout.strip()[-120:]!r} "
          f"remote={logs.stdout.strip()[-120:]!r}")

    # TRIGGER. It moves the world, so the two modes cannot be diffed on one
    # world without one of them running second; what is compared instead is the
    # ROW, which is the thing the harness reads, and the shape of the render.
    jobs = native_stack.scheduler_jobs(world)
    if not check("this world declares a job to trigger", bool(jobs), jobs):
        return
    before = len(sched_rows(audit, "sched_trigger"))
    tree_before = episode.tree_manifest(Path(world["world_root"]))
    fired = run_in(stack, f"cd /workspace && ./ops/sched trigger {jobs[0]}",
                   env={"SCHED_URL": endpoints["sched"]})
    check("`trigger` runs through the plane",
          fired.returncode == 0, (fired.stderr or fired.stdout)[-300:])
    rows = sched_rows(audit, "sched_trigger")
    check("...and the PLANE wrote the sched.sched_trigger row, not the CLI",
          len(rows) == before + 1, f"{before} -> {len(rows)}")
    if rows:
        row = rows[-1]
        check("the row is an exec that targets the job it ran",
              row["op_class"] == "exec"
              and f"scheduler:{jobs[0]}" in row["targets"],
              json.dumps({k: row[k] for k in ("op_class", "targets")}))
        check("...carried the 23 agent.md 9.2.2 keys", len(row) == 23,
              sorted(row))
    # The run really happened in the HOST world: the scheduler's own state file
    # moved and a run log appeared beside it, neither of which is mounted into
    # the episode.
    state = json.loads(Path(world["scheduler"]).read_text(encoding="utf-8"))
    runs = [r for r in (state.get("runs") or []) if r.get("job") == jobs[0]]
    check("the run is in the HOST world's scheduler state",
          bool(runs), f"{len(state.get('runs') or [])} run(s) in the file")
    # ...and it moved parts of the world the EPISODE cannot write. Asked as a
    # diff rather than by naming `build/sched/logs`, because what a triggered
    # run writes is the world's business and differs by job kind: GT004's three
    # are `exec.kind: sql` and leave no raw engine log, GT001's `dbt_build`
    # does. What must be true for every kind is that the change landed on the
    # host outside the two mounts, which is only possible through the plane.
    tree_after = episode.tree_manifest(Path(world["world_root"]))
    changed = sorted(k for k in set(tree_before) | set(tree_after)
                     if tree_before.get(k) != tree_after.get(k))
    unmounted = [k for k in changed
                 if not k.startswith("workspace/")
                 and k not in set(stack.warehouses)]
    check("...and the run wrote parts of the world the episode cannot reach",
          bool(unmounted), unmounted[:6] or changed[:6])
    rendered = fired.stdout.strip()
    check("what the agent saw is the json the local mode renders too",
          rendered.startswith("{") and '"status"' in rendered,
          rendered[:200])

    # The gate's question, from the vantage that matters.
    probe = stack.containment_probe(when="sched", canaries=[],
                                    grading=tmp / "grading", rollout_dir=tmp)
    check("P7: the scheduler plane answers with jobs",
          probe["checks"]["the_scheduler_plane_answers_with_jobs"],
          json.dumps(probe.get("scheduler")))
    surfaces = __import__("surface_audit").probe_native(
        probes=[{"label": "sched", "tool": "sched_state"}],
        endpoints=stack.gate_endpoints(), world=world,
        served_planes=ce.SERVED_PLANES)
    sched = surfaces["surfaces"]["sched"]
    check("the surface gate asks sched.internal, not the host state file",
          sched["live"] and sched["check"] == "http_health",
          json.dumps(sched)[:300])
    check("and `sched` is an endpoint the episode is allowed to dial",
          ("sched.internal", aut.NATIVE_WORKSPACE_SERVICES["sched"][1])
          in aut.allowed_destinations(endpoints))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="test_native_container")
    parser.add_argument("--no-docker", action="store_true",
                        help="the pure half only")
    parser.add_argument("--task", default=DEFAULT_TASK)
    parser.add_argument("--tasks-dir", default=None)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="alkera-tnc-"))
    try:
        test_bash_wrapper_contract(tmp)
        test_warehouse_mounts(tmp)
        test_generated_overlay(tmp)
        test_container_paths()
        test_prompt_and_cli(tmp)
        test_variant_plumbing(tmp)
        test_sched_endpoint()
        test_host_topology_warning()
        if not args.no_docker:
            test_live(tmp, args.task,
                      Path(args.tasks_dir).resolve() if args.tasks_dir else None,
                      args.image)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n  {PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
