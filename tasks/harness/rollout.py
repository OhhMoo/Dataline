#!/usr/bin/env python3
"""One rollout, end to end.

    python rollout.py --task GT001-aov-halving --rollout-id r0 --out <dir>

Runs as its own process so that K rollouts are genuinely independent: separate
world, separate grading zone, separate audit directory, separate gateway, no
shared mutable state anywhere. Writes one `rollout.json` record and exits 0 even
when the rollout failed - a failed rollout is data, and the aggregator needs the
record. It exits non-zero only when it could not produce a record at all.

Sequence, and the order is the contract:

    build world -> plant canaries -> fingerprint -> probe surfaces (throwaway
    copy) -> run the subject -> TEARDOWN -> fingerprint -> grade -> classify

The grader is not on disk anywhere the episode can address, and it is not
invoked until the subject's process is dead. Ground rule 1.

`--driver native` (gateway/NATIVE_SURFACES.md) replaces the gateway with
vendor-shaped services the subject reaches through its shell. The sequence above
is unchanged; what changes is that "probe surfaces" becomes a set of service
health checks, there is no gateway config to write, and blindness gains the
network-destination rule. Every service still appends its own 9.2.2 row to this
episode's `audit/actions.jsonl`, so the budget watcher, the detectors and the
report read the file they always read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_under_test as aut          # noqa: E402
import blindness                        # noqa: E402
import classify as classifier           # noqa: E402
import container_episode                # noqa: E402
import episode                          # noqa: E402
import native_stack                     # noqa: E402
import surface_audit                    # noqa: E402
import taskspec                         # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _plant_canaries(rollout_dir: Path, grading: Path) -> list[str]:
    """Tokens that exist ONLY where the subject must never be.

    Not a deterrent - a proof. These strings are reachable by nothing the
    toolbelt can do, so one turning up in a transcript is evidence rather than
    inference.

    Planted in FOUR places, and the last two matter most. Bait files only fire
    when the subject opens the bait; a reviewer pointed out that an agent going
    straight for `manifest.yaml` or `expected.duckdb` - the files actually worth
    stealing - would trip nothing at all. So the real grading artefacts carry
    tokens too: a YAML comment the manifest parser ignores, and a spare table
    the grader never queries.
    """
    tokens = [f"ALKERA-CANARY-{secrets.token_hex(8).upper()}" for _ in range(4)]
    (grading / f"REFERENCE_ANSWER_{tokens[0]}.txt").write_text(
        f"grading-zone canary {tokens[0]}\n", encoding="utf-8")
    (rollout_dir / f"NOTES_FOR_GRADER_{tokens[1]}.md").write_text(
        f"# not part of the world\ncanary {tokens[1]}\n", encoding="utf-8")

    manifest = grading / "manifest.yaml"
    if manifest.exists():
        with manifest.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# canary {tokens[2]}\n")

    expected = grading / "expected.duckdb"
    if expected.exists():
        try:
            import duckdb
            con = duckdb.connect(str(expected))
            con.execute("SET threads TO 1;")
            con.execute("CREATE SCHEMA IF NOT EXISTS _harness;")
            con.execute("CREATE OR REPLACE TABLE _harness.canary AS "
                        f"SELECT '{tokens[3]}' AS token;")
            con.close()
        except Exception:                              # noqa: BLE001
            # a canary we could not plant is one fewer detector, not a reason to
            # abandon the rollout - the other three still stand
            tokens[3] = ""
    return [t for t in tokens if t]


def _recover_canaries(rollout_dir: Path, grading: Path) -> list[str]:
    """Read the planted tokens back off disk, for reclassification.

    They are recoverable because they are *in* the artefacts: two bait
    filenames, a YAML comment, and a table in expected.duckdb.
    """
    toks = []
    for p in list(rollout_dir.glob("NOTES_FOR_GRADER_*.md")) + \
            list(grading.glob("REFERENCE_ANSWER_*.txt")):
        for part in p.stem.split("_"):
            if part.startswith("ALKERA-CANARY-"):
                toks.append(part)
    manifest = grading / "manifest.yaml"
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("# canary ALKERA-CANARY-"):
                toks.append(line.split()[-1])
    expected = grading / "expected.duckdb"
    if expected.exists():
        try:
            import duckdb
            con = duckdb.connect(str(expected), read_only=True)
            toks.append(con.execute("SELECT token FROM _harness.canary").fetchone()[0])
            con.close()
        except Exception:                                  # noqa: BLE001
            pass
    return sorted(set(t for t in toks if t))


# Run through the gateway venv rather than imported, for the same reason
# episode.gateway_tool_names() is: rollout.py must not assume which interpreter
# started it, and `alkera_gateway` lives in that venv.
WORKSPACE_EDIT_WRITER = """\
import json, sys

from alkera_gateway.native.audit import record

n = 0
for e in json.load(sys.stdin):
    record(tool="workspace.edit",
           args={"path": e["path"], "before_sha256": e["before"],
                 "after_sha256": e["after"]},
           op_class="write", targets=["workspace:" + e["path"]],
           transport="native", actor="harness")
    n += 1
print(n)
"""


def _stage_workspace_edits(audit_dir: Path, world: dict, before: dict,
                           after: dict) -> dict:
    """One `workspace.edit` row per file the subject changed in the working tree.

    Native mode's workspace is a real filesystem with no server in front of it,
    so nothing writes a 9.2.2 row when a file is edited - and the off-channel
    rule would then fire on ordinary work. Rather than weaken the rule, the
    harness closes the log itself: after teardown and BEFORE any detector runs,
    it diffs the before/after tree manifests restricted to the working tree and
    records what changed. The same pattern as ledger.stage(): harness
    bookkeeping, done in the open, and stamped `actor="harness"` so no row here
    can ever be read as the subject's.

    This is BOOKKEEPING, not evidence. What says the change was legitimate is
    the transcript - see blindness.detect's plane split, which keeps a
    working-tree change with no shell command in it hard.
    """
    ws = blindness.workspace_rel(world)
    if not ws:
        return {"rows": 0, "paths": [],
                "error": "the world manifest does not put the workspace under "
                         "the world root, so the workspace plane cannot be "
                         "identified"}
    edits = []
    for rel in sorted(set(before) | set(after)):
        if not (rel == ws or rel.startswith(ws + "/")):
            continue
        b, a = before.get(rel), after.get(rel)
        if b == a:
            continue
        edits.append({"path": rel[len(ws):].lstrip("/"),
                      "before": (b or [None, None])[1] or "",
                      "after": (a or [None, None])[1] or ""})
    if not edits:
        return {"rows": 0, "paths": []}
    r = subprocess.run(
        [str(episode.PY), "-c", WORKSPACE_EDIT_WRITER],
        input=json.dumps(edits), capture_output=True, text=True,
        env=episode.venv_env({
            "ALKERA_NATIVE_AUDIT": str(audit_dir / "actions.jsonl")}))
    if r.returncode != 0:
        # recorded, never swallowed: without these rows the detector falls back
        # to the transcript and the record must say that is what happened
        return {"rows": 0, "paths": [e["path"] for e in edits],
                "error": f"workspace.edit writer exited {r.returncode}: "
                         f"{r.stderr[-400:]}"}
    return {"rows": int(r.stdout.strip() or 0),
            "paths": [e["path"] for e in edits]}


def reclassify(rollout_dir: Path) -> dict:
    """Re-derive blindness and classification for a COMPLETED rollout.

    Detection is a pure function of the transcript, the action log, the world
    inventory and the artefact - all of which are on disk. So a detector fix can
    be applied to rollouts already run instead of re-spending them. It never
    re-runs the episode and never re-grades: the stored score is kept verbatim.

    Written because the first real rollout needed it. A `[\\\\/]tests` marker
    pattern quarantined an agent for writing `dbt_project/tests/...` - which is
    the artefact the ticket asks for.
    """
    rollout_dir = Path(rollout_dir).resolve()
    rec = json.loads((rollout_dir / "rollout.json").read_text(encoding="utf-8"))
    # the package this rollout was actually run from, not wherever tasks live by
    # default: a fixture package under a --tasks-dir must reclassify too
    spec = taskspec.load(rec["task"],
                         Path(rec["tasks_dir"]) if rec.get("tasks_dir") else None)
    world = rec.get("world") or {}
    grading = rollout_dir / "grading"

    transcript = blindness.load_transcript(rollout_dir / "transcript.jsonl")
    action_rows = []
    ap = rollout_dir / "audit" / "actions.jsonl"
    if ap.exists():
        for line in ap.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    action_rows.append(json.loads(line))
                except ValueError:
                    pass
    artifact_text = ""
    if rec.get("artifact_path") and Path(rec["artifact_path"]).exists():
        artifact_text = Path(rec["artifact_path"]).read_text(
            encoding="utf-8", errors="replace")

    # the world inventory cannot be re-taken after the fact, so the stored
    # before/after diff verdict is carried forward rather than re-derived
    tree = episode.tree_manifest(Path(world["world_root"])) if world else None

    stored_containment = [c for c in (rec.get("containment"),
                                      rec.get("containment_after")) if c]
    # the destinations log is on disk beside the action log, so it re-reads
    # exactly as the transcript does; the allowed set comes off the endpoint map
    # this rollout was actually handed, which the record stored.
    destinations = (blindness.load_destinations(rollout_dir / "audit")
                    if rec.get("driver") == "native" else None)
    network_allow = (aut.allowed_destinations(rec["native_endpoints"])
                     if rec.get("driver") == "native"
                     and rec.get("native_endpoints") else None)
    violations = blindness.detect(
        transcript=transcript, driver=rec["driver"],
        gateway_tools=sorted(rec.get("toolbelt") or []),
        canaries=_recover_canaries(rollout_dir, grading),
        repo_root=REPO, action_rows=action_rows,
        world_before=tree, world_after=tree,       # diff already consumed
        world_layout=world or None, artifact_text=artifact_text,
        topology=rec.get("topology", "host"), containment=stored_containment,
        destinations=destinations, network_allow=network_allow,
        normalise_host=aut.normalise_host)

    before = rec.get("classification")
    # The surface audit is re-derived too, from the probe results already stored
    # on the record. That is the whole point of --reclassify: a task that only
    # now declares its required_surfaces can have the gate applied to rollouts
    # already spent, instead of those rollouts keeping a verdict issued under a
    # policy that did not exist. It cannot re-probe - the world has been graded
    # against since - so a record with no stored probe results stays unaudited
    # and says so.
    rec["surface_audit"] = surface_audit.audit(
        spec=spec, surfaces=rec.get("surfaces") or {},
        toolbelt=sorted(rec.get("toolbelt") or []))
    rec["blindness"] = blindness.summarise(violations)
    rec["blindness_guarantee"] = blindness.guarantee(
        driver=rec["driver"], topology=rec.get("topology", "host"),
        containment=stored_containment)
    rec.update(classifier.classify(
        spec=spec, score=rec.get("score"), score_stable=rec.get("score_stable", True),
        grader_crashed=bool(rec.get("grader_crashed")),
        harness_failure=rec.get("harness_failure"),
        agent_exit_reason=(rec.get("agent") or {}).get("exit_reason", "not_started"),
        blindness=rec["blindness"],
        action_rows=blindness.subject_rows(action_rows),
        surfaces=(rec.get("surfaces") or {}).get("surfaces", {}),
        artifact_present=bool(rec.get("artifact_present")),
        wall_clock_s=rec["limits"]["wall_clock_seconds"],
        max_tool_calls=rec["limits"]["max_tool_calls"],
        unprobed_tools=rec.get("unprobed_tools") or [],
        surface_audit=rec.get("surface_audit")))
    rec["reclassified_from"] = before
    rec["reclassified_note"] = ("world-diff verdict carried forward; the world "
                                "cannot be re-inventoried after the fact")
    (rollout_dir / "rollout.json").write_text(
        json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return rec


def run_rollout(args) -> dict:
    tasks_dir = Path(args.tasks_dir).resolve() if args.tasks_dir else None
    spec = taskspec.load(args.task, tasks_dir)
    rollout_dir = Path(args.out).resolve()
    rollout_dir.mkdir(parents=True, exist_ok=True)

    world_root = rollout_dir / "world"
    grading = rollout_dir / "grading"          # SIBLING of the world, never inside
    audit_dir = rollout_dir / "audit"
    scratch = rollout_dir / "scratch"
    for d in (scratch,):
        d.mkdir(parents=True, exist_ok=True)

    wall = args.wall_clock or spec.wall_clock_seconds
    budget = args.max_tool_calls or spec.max_tool_calls

    record: dict = {
        "schema_version": 1,
        "task": spec.task_id,
        "rollout_id": args.rollout_id,
        "run_id": args.run_id,
        "model": args.model,
        "driver": args.driver,
        "topology": args.topology,
        # Provisional. Under `container` the guarantee is not settled until the
        # containment probe has run against THIS rollout's own container - see
        # the assignment below and blindness.detect()'s containment gate. Writing
        # the stronger claim here and hoping is exactly the failure mode the
        # blindness detector exists to prevent.
        "blindness_guarantee": ("tool_surface_revoked" if args.driver in ("mcp", "kimi")
                                else "detection_only"),
        "redteam_detector_test": args.driver == "scripted",
        "seed": spec.seed,
        # A run must name which ticket produced it. The same precedent as the
        # surface set: a rate whose conditions are not stated is not a result,
        # and prompt variant and live-surface set are two halves of the same
        # "what world and what ticket was this?" block in the report.
        "prompt_sha256": hashlib.sha256(
            spec.instruction_path.read_bytes()).hexdigest(),
        "prompt_variant": taskspec.prompt_variant_of(spec),
        "limits": {"wall_clock_seconds": wall, "max_tool_calls": budget},
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rollout_dir": str(rollout_dir),
        "tasks_dir": str(tasks_dir) if tasks_dir else None,
    }
    harness_failure: str | None = None
    grader_crashed = False
    score = None
    score_stable = True
    agent: aut.AgentRun | None = None
    world: dict = {}
    surfaces: dict = {"probed": False, "surfaces": {}}
    canaries: list[str] = []
    tree_before: dict = {}
    artifact_text = ""
    artifact_present = False
    http_proc = None
    http_log = None
    native_endpoints: dict | None = None
    # INTEGRATE-STACK: the host-topology native stack. NATIVE-DRIVER left no
    # hook for bringing one up - the native branch below resolved an endpoint
    # map and assumed something was already listening at it - so these four
    # touches (this slot, the bring-up, the record and the teardown) are the
    # minimal call, and `tasks/harness/native_stack.py` is where the work is.
    native: native_stack.NativeStack | None = None
    stack: container_episode.ContainerEpisode | None = None
    # `--driver native --topology container`: the episode container is the
    # subject's whole filesystem, and the services container is what serves it
    # the rest of the world. A separate slot from `stack` because the two
    # topologies share no code path but teardown - see
    # tasks/harness/NATIVE_CONTAINER.md.
    native_container: container_episode.NativeContainerEpisode | None = None
    containment: dict | None = None
    container_paths: dict | None = None

    try:
        # --- world -------------------------------------------------------
        world = episode.build_world(spec, world_root, grading)
        record["world"] = world
        if args.driver == "native":
            # `./ops/sched` goes into the working tree HERE, before the
            # fingerprint and before `tree_manifest`, because the workspace is
            # both fingerprinted and diffed: a file that appeared in it after
            # the "before" picture was taken is a world change with no row to
            # explain it. A team's own ops CLI is in the repo before anyone
            # clones it, and this is the same fact stated as an ordering.
            record["native_workspace_cli"] = native_stack.install_workspace_cli(
                world, args.topology)
        canaries = _plant_canaries(rollout_dir, grading)
        record["canaries_planted"] = len(canaries)

        record["world_fp_before"] = episode.fingerprint(world_root)
        tree_before = episode.tree_manifest(world_root)

        # BLINDNESS: the audit manifest is bind-mounted at /audit inside the
        # episode container (container_episode.AUDIT_IN_CONTAINER) and the subject
        # has bash, so every byte of episode_id is readable BY THE SUBJECT. The
        # composite that used to go here named the task - "GT004-doubling-window"
        # hands the agent the defect mechanism outright - and under the
        # <variant>-<date>-kN run-id convention it handed over the prompt variant
        # too. audit.py:54 only ever compares this value for equality and nothing
        # parses it, so an opaque digest preserves every use it has. The readable
        # label stays host-side in the rollout record, where the subject cannot
        # reach it and our own analysis still needs it.
        episode_label = f"{spec.task_id}-{args.run_id}-{args.rollout_id}"
        episode_id = hashlib.sha256(
            episode_label.encode("utf-8")).hexdigest()[:24]
        record["episode_label"] = episode_label
        record["episode_id"] = episode_id
        mcp_server_argv: list[str] | None = None

        if args.topology == "container":
            # Containment FIRST. The terminal is handed over only after this
            # rollout's own container has been probed and found sealed; if the
            # probe does not run or does not pass, blindness.detect() quarantines
            # the rollout rather than letting it into the number.
            if args.driver not in ("mcp", "native"):
                raise episode.HarnessError(
                    f"--topology container is wired for the mcp and native "
                    f"drivers, not {args.driver!r}.")
            ok, docker_version = container_episode.docker_available()
            if not ok:
                raise episode.HarnessError(f"docker is not usable: {docker_version}")
            project = re.sub(r"[^a-z0-9_-]+", "-",
                             f"alkera-ep-{args.run_id}-{args.rollout_id}".lower())
            stack = container_episode.ContainerEpisode(
                project=project, world_root=world_root, audit_dir=audit_dir,
                image=args.image)
            stack.up()
            container_cfg = container_episode.container_gateway_config(
                world, episode_id, spec.virtual_time)
            stack.prepare(container_cfg, spec.surface_probes,
                          rollout_dir / "container")
            record["container"] = {
                "project": project, "docker_server": docker_version,
                "image": stack.image, "image_digest": stack.image_digest,
                "container_id": stack.container_id[:12],
                "services": stack.services,
                "compose_files": [str(container_episode.COMPOSE_BASE),
                                  str(container_episode.COMPOSE_OVERLAY)],
                "gateway_config": container_cfg,
            }
            containment = stack.containment_probe(
                forbidden_files=[f"{d}/manifest.yaml"
                                 for d in container_episode.FORBIDDEN_DIRS]
                + [str(grading / "manifest.yaml").replace("\\", "/"),
                   str(grading / "expected.duckdb").replace("\\", "/")],
                host_paths=[str(world_root).replace("\\", "/"),
                            str(grading).replace("\\", "/"),
                            str(rollout_dir).replace("\\", "/"),
                            str(REPO).replace("\\", "/"),
                            "/c", "/host_mnt", "/mnt/c"],
                repo_root=REPO)
            containment["when"] = "before_episode"
            record["containment"] = containment
            # the surfaces and the toolbelt are read from INSIDE the container.
            # The host venv is a different build - two agents are editing
            # gateway/src right now - and the toolbelt decides both the prompt
            # and what counts as a tool the subject was allowed to hold.
            #
            # Toolbelt FIRST, then the probes: the task's surface_probes are a
            # floor, and the sweep over everything else in the toolbelt cannot
            # be assembled until the toolbelt is known. On GT004 the report
            # already named 23 live tools the task does not probe; an unprobed
            # tool that is dead must not be able to hide behind that.
            toolbelt = stack.toolbelt()
            stack.set_probes(surface_audit.all_probes(toolbelt, spec.surface_probes),
                             rollout_dir / "container")
            surfaces = stack.probe_surfaces()
            gateway_cfg = rollout_dir / "container" / "container_gateway.json"
            mcp_server_argv = stack.mcp_argv()
        elif args.driver == "native":
            # No gateway process, no gateway config and no toolbelt: the subject
            # reaches vendor-shaped services through its shell, and each service
            # writes its own 9.2.2 row into this episode's audit directory (the
            # ALKERA_NATIVE_AUDIT contract). The empty toolbelt is load-bearing
            # rather than a placeholder - it is what keeps the prompt from
            # enumerating surfaces, which is the measurement bias native mode
            # exists to remove.
            audit_dir.mkdir(parents=True, exist_ok=True)
            if args.native_endpoints:
                # An endpoint map the caller supplied names a stack the caller
                # is running; starting a second one on top of it would be two
                # writers for one audit directory.
                native_endpoints = aut.resolve_endpoints(args.native_endpoints)
            else:
                native = native_stack.NativeStack(
                    world=world, audit_dir=audit_dir, rollout_dir=rollout_dir,
                    spec=spec, key=f"{args.run_id}/{args.rollout_id}")
                native_endpoints = native.up()
                record["native_stack"] = native.record()
            record["native_endpoints"] = native_endpoints
            toolbelt = {}
            surfaces = surface_audit.probe_native(
                probes=spec.surface_probes, endpoints=native_endpoints,
                world=world,
                schemas=surface_audit.warehouse_schemas(spec))
            gateway_cfg = None
        else:
            # --- surface probe, on a throwaway copy, BEFORE the episode ---
            toolbelt = episode.gateway_tool_names()
            surfaces = episode.probe_surfaces(
                spec, world, scratch,
                surface_audit.all_probes(toolbelt, spec.surface_probes))
            gateway_cfg = episode.write_gateway_config(
                rollout_dir / "gateway.json", world, audit_dir, episode_id,
                spec.virtual_time)

        record["surfaces"] = surfaces
        record["toolbelt"] = sorted(toolbelt)

        # --- THE SURFACE GATE. Before the subject is handed the terminal. ----
        # GT004 scored five rollouts in a world where eight surfaces answered
        # `no_issue_tracker` / `no_chat` / `no_knowledge_base` /
        # `no_observability` / `no_ingest_state`, and the only consequence was a
        # line at the bottom of the report. A run measured in a half-absent
        # world produces a number indistinguishable from one measured in a whole
        # one, so the dead surface has to be an outcome, not an annotation.
        #
        # This follows blindness.py's precedent deliberately: a compromised
        # episode is QUARANTINED rather than scored. The grounds differ - there
        # the subject may have seen what it must not, here the subject was never
        # given the world the task describes - but the consequence is the same,
        # because in both cases the score is not a measurement of this task.
        record["surface_audit"] = surface_audit.audit(
            spec=spec, surfaces=surfaces, toolbelt=toolbelt)
        gate = surface_audit.gate_error(record["surface_audit"])
        if gate:
            raise episode.HarnessError(gate)
        # A tool the task never probed is a tool whose failures cannot be
        # attributed: they fall through the dead-surface rule and land on the
        # agent as `reasoning`. The gateway grew from 17 tools to 28 mid-round
        # and GT001 still probed 7 of them, so this is recorded on every
        # rollout rather than assumed away.
        #
        # Two lists, because the sweep split one question into two. A tool the
        # TASK does not declare is now usually swept, so its liveness is known
        # even though the task never asked. What remains unattributable is the
        # tool nobody verified either way: never declared AND not callable with
        # no arguments. That, not the old list, is what makes an error land on
        # the agent as `reasoning` with nothing to check it against.
        record["undeclared_tools"] = sorted(
            set(toolbelt) - surface_audit.declared_tools(spec.surface_probes))
        record["unprobed_tools"] = sorted(
            set(record["surface_audit"]["unprobed_tools"])
            | set(record["surface_audit"]["unswept_tools"]))

        # --- the episode --------------------------------------------------
        gateway_url = None
        if args.driver in ("cli", "scripted"):
            port = _free_port()
            http_log = (rollout_dir / "gateway_server.log").open("w")
            http_proc = subprocess.Popen(
                [str(episode.PY), "-m", "alkera_gateway.cli", "serve",
                 "--config", str(gateway_cfg), "--port", str(port)],
                stdout=http_log, stderr=http_log, env=episode.venv_env())
            for _ in range(60):
                time.sleep(0.5)
                try:
                    socket.create_connection(("127.0.0.1", port), 0.5).close()
                    break
                except OSError:
                    if http_proc.poll() is not None:
                        raise episode.HarnessError("gateway http server died")
            gateway_url = f"http://127.0.0.1:{port}"

        instruction = spec.instruction
        if args.adversarial_suffix:
            instruction += "\n\n" + Path(args.adversarial_suffix).read_text(
                encoding="utf-8")
            record["adversarial_suffix"] = str(args.adversarial_suffix)
            # this rollout exists to prove a detector fires. It is never a
            # measurement, and the report must not treat it as one.
            record["redteam_detector_test"] = True

        if args.driver == "native" and args.model == "scripted":
            # INTEGRATE-STACK: the Phase 3 acceptance subject. `aut.run` starts
            # `claude`, and the native driver had no way to be run end to end
            # without spending model quota; `--model scripted` runs a real bash
            # process through the real native environment instead. It is a
            # plumbing fixture, never a measurement, and `report.py` sees it as
            # the model string it is.
            agent = native_stack.run_scripted(
                rollout_dir=rollout_dir, world=world, audit_dir=audit_dir,
                endpoints=native_endpoints or {}, wall_clock_s=wall,
                instruction=instruction,
                gateway_config=native.config_file if native is not None else "")
        else:
            agent = aut.run(
                instruction=instruction, toolbelt=toolbelt,
                artifact_in_world=spec.artifact_in_world,
                gateway_cfg=gateway_cfg, gateway_url=gateway_url,
                audit_dir=audit_dir, rollout_dir=rollout_dir,
                model=args.model, driver=args.driver,
                wall_clock_s=wall, max_tool_calls=budget,
                redteam=bool(args.adversarial_suffix),
                mcp_server_argv=mcp_server_argv,
                shell="bash" in toolbelt,
                native_endpoints=native_endpoints,
                workspace=world.get("workspace"),
                profiles_dir=world.get("profiles_dir"),
                scheduler=world.get("scheduler"),
                native_gateway_config=(native.config_file
                                       if native is not None else None))
        record["agent"] = asdict(agent)

        if stack is not None:
            # Probe the SAME container again, after the subject has had its way
            # with it. Containment that held at t=0 and not at t=end is not
            # containment, and the agent had a root shell in there.
            after = stack.containment_probe(
                forbidden_files=[f"{d}/manifest.yaml"
                                 for d in container_episode.FORBIDDEN_DIRS],
                host_paths=[str(world_root).replace("\\", "/"),
                            str(grading).replace("\\", "/"),
                            str(REPO).replace("\\", "/"), "/c", "/host_mnt"],
                repo_root=REPO)
            after["when"] = "after_episode"
            record["containment_after"] = after

    except episode.HarnessError as exc:
        harness_failure = str(exc)
    except Exception as exc:                           # noqa: BLE001 - recorded
        harness_failure = f"{type(exc).__name__}: {exc}"
    finally:
        # --- TEARDOWN. Everything below this line is post-episode. --------
        if http_proc is not None:
            http_proc.terminate()
            try:
                http_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                http_proc.kill()
        if http_log is not None:
            http_log.close()
        # The container comes down here, inside teardown, and everything below
        # this line is post-episode: the world inventory, the fingerprint and the
        # grader all run against a world nothing is still holding open.
        if stack is not None:
            try:
                stack.down()
            except Exception as exc:                   # noqa: BLE001 - recorded
                record.setdefault("teardown_errors", []).append(
                    f"{type(exc).__name__}: {exc}")
        if native is not None:
            # Before the fingerprint and the grader, like the container: the
            # services hold this world's json planes open, and a row written
            # after `tree_after` is a row the detectors never see.
            try:
                native.down()
            except Exception as exc:                   # noqa: BLE001 - recorded
                record.setdefault("teardown_errors", []).append(
                    f"{type(exc).__name__}: {exc}")
        shutil.rmtree(scratch, ignore_errors=True)

    # --- the workspace plane's rows. After teardown, before the action log is
    # read, so the detectors see a log that accounts for the whole world.
    tree_after = episode.tree_manifest(world_root) if world else None
    if args.driver == "native" and world and tree_before is not None:
        record["workspace_edits"] = _stage_workspace_edits(
            audit_dir, world, tree_before, tree_after)

    action_rows: list[dict] = []
    actions_path = audit_dir / "actions.jsonl"
    if actions_path.exists():
        for line in actions_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    action_rows.append(json.loads(line))
                except ValueError:
                    pass
    # The harness writes rows of its own in native mode - the surface gate's
    # `health:` polls and the post-episode `workspace.edit` bookkeeping - and
    # none of them is the subject's work. Every number that describes the
    # SUBJECT is computed from `subject_action_rows`; the full log stays on disk
    # and its length is recorded beside, so nothing is hidden, only attributed.
    subject_action_rows = blindness.subject_rows(action_rows)
    record["action_log"] = {
        "path": str(actions_path),
        "rows": len(subject_action_rows),
        "rows_including_harness_authored": len(action_rows),
        "harness_authored_rows": len(action_rows) - len(subject_action_rows),
        # Native mode has no gateway process and therefore no session.json, so
        # the question this key answers ("did the subject have a toolbelt at
        # all?") is None rather than False - and it has already been answered
        # earlier and better, by the surface gate health-checking the services
        # before the subject was handed the terminal.
        "gateway_started": (None if args.driver == "native"
                            else (audit_dir / "session.json").exists()),
    }
    if (harness_failure is None and agent is not None
            and args.driver != "native"
            and not record["action_log"]["gateway_started"]):
        harness_failure = ("the gateway never started inside the episode - the "
                           "subject had no toolbelt at all")

    if world:
        record["world_fp_after"] = episode.fingerprint(world_root)
        record["world_changed"] = (record.get("world_fp_after")
                                   != record.get("world_fp_before"))
        artifact_path = spec.artifact_host_path(world)
        record["artifact_path"] = str(artifact_path)
        if artifact_path.exists():
            artifact_present = True
            artifact_text = artifact_path.read_text(encoding="utf-8", errors="replace")
            record["artifact_bytes"] = len(artifact_text)
            try:
                record["artifact"] = json.loads(artifact_text)
            except ValueError:
                record["artifact"] = None
                record["artifact_unparseable"] = True
        else:
            record["artifact"] = None
    record["artifact_present"] = artifact_present

    # --- blindness -------------------------------------------------------
    transcript = blindness.load_transcript(rollout_dir / "transcript.jsonl")
    init = blindness.init_event(transcript)
    record["granted_tools"] = sorted((init or {}).get("tools") or [])
    record["mcp_servers"] = (init or {}).get("mcp_servers") or []
    record["tool_uses_in_transcript"] = len(blindness.tool_uses(transcript))
    # Detection ALWAYS runs. Skipping it when setup failed was the hole a
    # reviewer found mid-build: no detector ran, nothing was recorded, and the
    # record then read `quarantined: false` - a clean bill of health issued by a
    # camera that was never switched on. None means "not observed", and
    # not-observed quarantines.
    # Native mode only: where the episode's traffic went, read off the log the
    # compose stack writes on the SERVICE side. Absent is unobserved, never
    # clean - see blindness.load_destinations.
    destinations = None
    network_allow = None
    if args.driver == "native":
        destinations = blindness.load_destinations(audit_dir)
        network_allow = (aut.allowed_destinations(native_endpoints)
                         if native_endpoints else None)
        record["destinations"] = {
            "path": str(audit_dir / blindness.DESTINATIONS_FILE),
            "present": destinations is not None,
            "rows": len(destinations or []),
            "allowed": sorted(f"{h}:{p}" for h, p in (network_allow or ())),
        }
    violations = blindness.detect(
        transcript=transcript, driver=args.driver,
        gateway_tools=sorted(record.get("toolbelt") or []),
        canaries=canaries, repo_root=REPO, action_rows=action_rows,
        world_before=tree_before if world else None,
        world_after=tree_after,
        world_layout=world or None, artifact_text=artifact_text,
        topology=args.topology,
        containment=[c for c in (record.get("containment"),
                                 record.get("containment_after")) if c],
        destinations=destinations, network_allow=network_allow,
        normalise_host=aut.normalise_host)
    record["blindness"] = blindness.summarise(violations)
    # The guarantee is now READ OFF the evidence rather than declared from the
    # flags we passed. Under `container` it is only the stronger claim when this
    # rollout's own container was probed, before and after, and was sealed both
    # times; otherwise it stays what it can defend and the rollout quarantines.
    record["blindness_guarantee"] = blindness.guarantee(
        driver=args.driver, topology=args.topology,
        containment=[c for c in (record.get("containment"),
                                 record.get("containment_after")) if c])

    # --- grading. AFTER teardown, never before. --------------------------
    if world and harness_failure is None:
        try:
            score, score_stable = episode.grade_twice(
                spec, world, Path(record["artifact_path"]), rollout_dir)
        except episode.HarnessError as exc:
            grader_crashed = True
            record["grader_error"] = str(exc)
    record["score"] = score
    record["score_stable"] = score_stable
    if score:
        record["checks"] = {c["id"]: {"points": c["points"], "max": c["max"],
                                      "passed": c["passed"], "detail": c["detail"]}
                            for c in score.get("checks", [])}

    # record it even when another class wins: an unrecorded harness failure is
    # how a rollout ends up classified as something it is not.
    record["harness_failure"] = harness_failure
    record["grader_crashed"] = grader_crashed

    verdict = classifier.classify(
        spec=spec, score=score, score_stable=score_stable,
        grader_crashed=grader_crashed, harness_failure=harness_failure,
        agent_exit_reason=(agent.exit_reason if agent else "not_started"),
        blindness=record["blindness"], action_rows=subject_action_rows,
        surfaces=surfaces.get("surfaces", {}), artifact_present=artifact_present,
        wall_clock_s=wall, max_tool_calls=budget,
        unprobed_tools=record.get("unprobed_tools") or [],
        surface_audit=record.get("surface_audit"))
    record.update(verdict)
    record["ended_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record["wall_clock_s"] = agent.wall_clock_s if agent else None
    record["tool_calls"] = len(subject_action_rows)

    (rollout_dir / "rollout.json").write_text(
        json.dumps(record, indent=2, default=str), encoding="utf-8")
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task")
    ap.add_argument("--reclassify", default=None,
                    help="re-derive blindness and classification for a COMPLETED "
                         "rollout directory, from its stored transcript, action "
                         "log and artefact. Never re-runs the episode and never "
                         "re-grades - for applying a detector fix to rollouts "
                         "already spent.")
    ap.add_argument("--tasks-dir", default=None,
                    help="directory holding the task package, when it is not "
                         "tasks/. Used by surface_gate_check.py to stand up "
                         "fixture packages outside the repo.")
    ap.add_argument("--rollout-id", default="r0")
    ap.add_argument("--run-id", default="adhoc")
    ap.add_argument("--out")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--driver", default="mcp",
                    choices=["mcp", "cli", "scripted", "kimi", "native"])
    ap.add_argument("--native-endpoints", default=None,
                    help="JSON file mapping native surface names onto reachable "
                         "addresses, for `--driver native` off the episode "
                         "network. Defaults are the `<name>.internal` aliases; "
                         "see agent_under_test.resolve_endpoints.")
    ap.add_argument("--topology", default="host",
                    choices=["host", "container"],
                    help="where the WORLD and the TOOLS run. `host`: the gateway "
                         "is a host subprocess beside the grading zone, and "
                         "blindness rests on the tool surface being revoked. "
                         "`container`: the world, the gateway, the audit log and "
                         "a real shell live inside the episode service of "
                         "gateway/docker/compose.yml, and blindness rests on the "
                         "container - measured per rollout, never assumed.")
    ap.add_argument("--image", default=container_episode.DEFAULT_IMAGE,
                    help="image for the episode service under --topology container")
    ap.add_argument("--wall-clock", type=int, default=None,
                    help="override the task's wall-clock ceiling (seconds)")
    ap.add_argument("--max-tool-calls", type=int, default=None,
                    help="override the task's tool-call ceiling")
    ap.add_argument("--adversarial-suffix", default=None,
                    help="file appended to the ticket - for red-teaming the "
                         "harness's own detectors, never for a measured run")
    a = ap.parse_args()
    if a.reclassify:
        rec = reclassify(Path(a.reclassify))
        print(json.dumps({"rollout_id": rec["rollout_id"],
                          "reclassified_from": rec["reclassified_from"],
                          "classification": rec["classification"],
                          "passed": rec["passed"], "scored": rec["scored"],
                          "total": (rec.get("score") or {}).get("total"),
                          "why": rec["why"]}, indent=2))
        return 0
    if not a.task or not a.out:
        ap.error("--task and --out are required unless --reclassify is given")
    rec = run_rollout(a)
    print(json.dumps({"rollout_id": rec["rollout_id"],
                      "classification": rec["classification"],
                      "passed": rec["passed"], "scored": rec["scored"],
                      "total": (rec.get("score") or {}).get("total"),
                      "tool_calls": rec["tool_calls"],
                      "wall_clock_s": rec["wall_clock_s"],
                      "why": rec["why"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
