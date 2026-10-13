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

Two paths re-open a rollout that has already been run, and they are not the
same. `--reclassify` re-derives the verdict and keeps the stored score verbatim,
for applying a detector fix. `--regrade` re-runs every stage AFTER teardown, the
grader included, for a run whose grader was broken or whose harness died before
it wrote the record - see the regrade block below. Neither ever re-runs the
subject, and neither rebuilds the world.

`--driver native` (gateway/NATIVE.md) replaces the gateway with
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
import os
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


def _max_seq(actions: Path) -> int:
    """The highest `seq` in an action log, or 0 for one that is not there yet.

    Read rather than counted: `seq` is the log's own contiguous sequence
    (contract 9.2.2) and a count would be wrong the moment a row failed to
    parse.
    """
    high = 0
    try:
        text = actions.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            high = max(high, int(json.loads(line).get("seq") or 0))
        except (ValueError, TypeError, AttributeError):
            continue
    return high


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


def _refresh_grading_manifest(spec, grading: Path) -> dict:
    """Bring the grading zone's manifest.yaml up to the task's current one,
    for a regrade. Returns what was done, for the record.

    The canary comment rollout.py appended at episode time is carried over
    byte for byte: `_recover_canaries` reads the token back off this file, and
    a refreshed copy that dropped it would turn the transcript detector blind
    to the one bait that guards the manifest itself. Nothing else from the old
    copy survives - the whole point is that the old copy is what was wrong.

    When the task declares no `[grading].manifest`, or the zone holds no copy
    (a task whose grader reads none), nothing is written and the record says
    so; the hash is then of nothing, and `regrade_grader_sha256` reduces to
    grade.py alone, which is what it always was for such a task.
    """
    copy = grading / "manifest.yaml"
    src = getattr(spec, "grade_manifest", None)
    if src is None or not Path(src).exists():
        # `sha256` is None and not the copy's hash on purpose: the copy carries
        # a per-rollout canary, and a per-rollout grader hash would make
        # report.py read one run as five graders.
        return {"source": "task declares no [grading].manifest", "refreshed": False,
                "sha256": None}
    if not copy.exists():
        return {"source": "grading zone holds no manifest.yaml copy",
                "refreshed": False, "sha256": None}
    canary_lines = [
        line for line in copy.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip().startswith("# canary ALKERA-CANARY-")]
    fresh = Path(src).read_text(encoding="utf-8")
    before = hashlib.sha256(copy.read_bytes()).hexdigest()
    new_text = fresh if fresh.endswith("\n") else fresh + "\n"
    if canary_lines:
        new_text += "\n" + "\n".join(canary_lines) + "\n"
    changed = new_text.encode("utf-8") != copy.read_bytes()
    if changed:
        copy.write_text(new_text, encoding="utf-8")
    return {"source": f"refreshed from {src}" if changed else f"already current with {src}",
            "refreshed": changed, "sha256": hashlib.sha256(fresh.encode("utf-8")).hexdigest(),
            "previous_sha256": before, "canary_lines_kept": len(canary_lines)}


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
        normalise_host=aut.normalise_host,
        # Stored on the record by the run that made it, never re-derived: the
        # translation is a property of the container that rollout ran in, and
        # inventing one here would be reclassifying against a topology the
        # rollout may not have had.
        container_paths=rec.get("container_paths"))

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
        containment=stored_containment,
        # Stored by the run that took it. A reclassification cannot re-take a
        # measurement of a container that is long gone, and it must not be able
        # to promote a record into the strong claim by forgetting to ask.
        container_execution=rec.get("container_execution_proof"))
    rec.update(classifier.classify(
        spec=spec, score=rec.get("score"), score_stable=rec.get("score_stable", True),
        grader_crashed=bool(rec.get("grader_crashed")),
        harness_failure=rec.get("harness_failure"),
        agent_exit_reason=(rec.get("agent") or {}).get("exit_reason", "not_started"),
        # so a record made before `quota_exhausted` existed can be relabelled
        # from the limit message its subject left in final_text
        agent_final_text=(rec.get("agent") or {}).get("final_text") or "",
        agent_stderr_tail=(rec.get("agent") or {}).get("stderr_tail") or "",
        blindness=rec["blindness"],
        action_rows=blindness.subject_rows(
            action_rows, rec.get("action_log_watermark", 0)),
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


# ------------------------------------------------------------------- regrade
#
# The episode is the expensive half. Everything after teardown is not, and all
# of it is a function of files that are already on disk. The rollout that forced
# this was GT009: five episodes ran to completion - transcript, action log,
# world, grading zone, artefact, all written - and rollout.py then died at
# record time on `c["id"]`, because that task's grader emitted `check` instead.
# Re-running them would have spent ~55 minutes of model quota re-doing the one
# part that had not failed.
#
# It is NOT `--reclassify`. Reclassify re-derives the verdict and keeps the
# stored score verbatim, because a detector fix is all it exists to apply. This
# re-runs the GRADER as well - which is the point when the grader is what was
# broken - so it needs the world, the grading zone and the artefact as well as
# the transcript and the action log, and it refuses a directory missing any of
# them by name.
#
# THE RULE THAT MAKES IT HONEST. Every input describing the world BEFORE the
# episode - the tree manifest the off-channel detector diffs against, the
# fingerprint, the action-log watermark, the surface probe results, the toolbelt
# the subject was handed - comes from what rollout.py wrote AT THE TIME. Nothing
# here rebuilds the world, re-probes it or re-runs the subject: a "before"
# picture taken now is a picture of the world the episode has already changed,
# and grading against one would be inventing evidence. What was NOT saved is
# named on the record under `regrade.missing`, the rollout is marked
# `regrade_partial`, and the consequence of each absent piece is written out. It
# is never quietly filled in with a plausible value.
#
# `before_episode.json` - written by run_rollout below, immediately before the
# subject is handed the terminal - is what makes a regrade COMPLETE. Rollouts
# recorded before that file existed are still regradable, and they say what it
# cost them.
REGRADE_SCHEMA = 1
BEFORE_EPISODE_FILE = "before_episode.json"

#: The prefix blindness.detect builds its mcp allowlist with (its `mcp_server`
#: default). Restated rather than imported for the one place below that has to
#: read a granted tool name back apart.
MCP_TOOL_PREFIX = "mcp__alkera__"

#: What a completed episode leaves behind. A directory missing any of these is
#: refused rather than regraded around: each is an input to a stage below, and a
#: stage that runs without its input produces a verdict about nothing.
REGRADE_INPUTS = (
    ("transcript.jsonl", "file", "the episode transcript - every detector reads it"),
    ("audit/actions.jsonl", "file",
     "the contract-9.2.2 action log - the tool-call count, the off-channel "
     "detector and the classifier all read it"),
    ("world", "dir", "the world the episode ended in - what the grader grades"),
    ("grading", "dir",
     "the grading zone - the rubric, the reference and the recoverable canaries"),
)


class RegradeRefused(Exception):
    """This directory cannot be regraded, and the message names why.

    A refusal rather than a partial: the pieces it fires on have no honest
    substitute, and producing a record without them would be producing a number
    for an episode the harness cannot describe.
    """


def _read_json(path: Path):
    """The JSON at `path`, or None when it is absent or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _first(*candidates):
    """The first `(source, value)` whose value is not None, else `(None, None)`.

    `None` means ABSENT and nothing else does: an action-log watermark of 0 and
    a variant of "" are both real values a truthiness test would throw away.
    """
    for source, value in candidates:
        if value is not None:
            return source, value
    return None, None


def _driver_from_disk(rollout_dir: Path) -> tuple[str | None, str]:
    """Which driver ran here, read off files the harness wrote at episode time.

    Only ever reached for a rollout with no record at all. The driver decides
    the tool ALLOWLIST, so a guess here is a libel - an mcp episode judged
    against native's allowlist would have every gateway call it made flagged -
    and this fails closed: unrecognised evidence returns None and the regrade
    refuses.
    """
    argv = _read_json(rollout_dir / "agent_argv.json")
    mcp = _read_json(rollout_dir / "mcp.json")
    if not isinstance(mcp, dict):
        return None, "no mcp.json in the rollout directory"
    servers = mcp.get("mcpServers") or {}
    exe = (Path(str(argv[0])).name.lower()
           if isinstance(argv, list) and argv else "")
    if servers:
        if "kimi" in exe:
            return "kimi", (f"mcp.json declares {sorted(servers)} and "
                            f"agent_argv.json starts {exe}")
        if "cursor" in exe:
            return "cursor", (f"mcp.json declares {sorted(servers)} and "
                              f"agent_argv.json starts {exe}")
        return "mcp", f"mcp.json declares the gateway server(s) {sorted(servers)}"
    return "native", ("mcp.json declares NO mcp servers, which is native mode's "
                      "empty config - a shell and nothing else")


def _topology_from_disk(rollout_dir: Path) -> tuple[str, str]:
    """`container` only on evidence of a container; `host` otherwise.

    Both container topologies leave a directory behind - `container/` for the
    in-container gateway config and probe set, `shell/` for the bash wrapper.
    Absence of that evidence reads as `host`, which is the weaker of the two
    claims (`blindness.guarantee` gives host no filesystem isolation), so the
    fallback cannot launder a rollout into a stronger guarantee than it earned.
    """
    for name in ("container", "shell"):
        if (rollout_dir / name).is_dir():
            return "container", (f"{name}/ is present, and only the container "
                                 f"topology writes it")
    return "host", ("no container/ or shell/ directory: nothing on disk says a "
                    "container ran, and host is the weaker of the two claims")


def _model_from_disk(rollout_dir: Path) -> str | None:
    """The model string out of the argv `agent_under_test` recorded."""
    argv = _read_json(rollout_dir / "agent_argv.json")
    if not isinstance(argv, list):
        return None
    for i, a in enumerate(argv[:-1]):
        if a == "--model":
            return str(argv[i + 1])
    return None


def _world_from_gateway_config(rollout_dir: Path) -> dict | None:
    """The world manifest, rebuilt from the gateway config rollout.py wrote.

    `write_gateway_config` copies the world manifest's paths into `gateway.json`
    before the episode, so for a rollout whose record was never written this is
    the surviving statement of where that world's parts are - and one the
    harness wrote at the time, not one inferred now. The grading zone is not in
    it by design (the gateway cannot address the grading zone at all); it is the
    rollout directory's own `grading/` sibling, which is rollout.py's layout
    rather than a guess.
    """
    cfg = _read_json(rollout_dir / "gateway.json")
    if not isinstance(cfg, dict) or not cfg.get("world_root"):
        return None
    keys = ("workspace", "warehouse", "scheduler", "dashboards", "profiles_dir",
            "world_root", "virtual_time", *episode.WORLD_PLANE_KEYS,
            *episode.WORLD_WAREHOUSE_KEYS)
    world = {k: cfg[k] for k in keys if cfg.get(k) is not None}
    world["grading"] = str(rollout_dir / "grading")
    return world


def _reroot_world(world: dict, rollout_dir: Path) -> tuple[dict, str | None]:
    """The saved world manifest, moved onto the directory actually being regraded.

    A run that must be preserved is re-graded from a COPY, and a copy's world
    root is not the one the record names. Without this the grader would be
    pointed back at the original - reading a world the caller asked not to
    touch, and staging this regrade's ledger into that run's grading zone. The
    paths are relocated, never re-derived: same tree, new prefix.
    """
    old, new = Path(world["world_root"]), rollout_dir / "world"
    if os.path.normcase(str(old)) == os.path.normcase(str(new)):
        return world, None
    prefix = os.path.normcase(str(old)) + os.sep
    out: dict = {}
    for key, value in world.items():
        if isinstance(value, str) and os.path.normcase(value).startswith(prefix):
            out[key] = str(new / value[len(str(old)) + 1:])
        else:
            out[key] = value
    out["world_root"] = str(new)
    out["grading"] = str(rollout_dir / "grading")
    if world.get("warehouses"):
        out["warehouses"] = episode.reroot_warehouses(
            world["warehouses"], old, new.as_posix())
    return out, (f"the rollout directory has moved since the run, so the saved "
                 f"world manifest was re-rooted from {old} onto {new}. Nothing "
                 f"in the original directory is read or written.")


def _grade_argv_placeholders(spec) -> set[str]:
    """The `{name}`s this task's grade_argv substitutes."""
    return {m.group(1) for a in spec.grade_argv
            for m in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", a)}


def _write_before_episode(rollout_dir: Path, record: dict,
                          tree_manifest: dict) -> Path:
    """Save the BEFORE-EPISODE state, immediately before the subject runs.

    Written because `--regrade` has to be able to say what the world looked like
    at t=0 without rebuilding it, and until this file existed the only copy of
    that state was the record - which a harness crash between teardown and the
    record write destroys, which is the exact case regrade exists for.

    It holds nothing the grading zone holds: paths, hashes, the probe verdicts
    and the toolbelt. No canary token goes in it - those are recovered from the
    artefacts they were planted in, as `--reclassify` already does - and no
    reference answer, because under the native and container topologies the
    subject has a shell and this file sits in the rollout directory beside the
    world.
    """
    payload = {
        "schema_version": REGRADE_SCHEMA,
        "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "why": ("the before-episode state `rollout.py --regrade` reads instead "
                "of re-taking it; see the regrade block in rollout.py"),
        "tree_manifest": tree_manifest,
    }
    for key in ("task", "tasks_dir", "variant", "run_id", "rollout_id", "model",
                "driver", "topology", "seed", "limits", "prompt_sha256",
                "prompt_variant", "episode_id", "episode_label", "world",
                "world_fp_before", "action_log_watermark", "surfaces",
                "toolbelt", "undeclared_tools", "unprobed_tools",
                "native_endpoints", "container_paths", "containment"):
        payload[key] = record.get(key)
    path = rollout_dir / BEFORE_EPISODE_FILE
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _refuse_incomplete(rollout_dir: Path) -> None:
    """Refuse a directory that is not a completed episode, naming what is absent."""
    absent = []
    for rel, kind, why in REGRADE_INPUTS:
        path = rollout_dir / rel
        present = path.is_file() if kind == "file" else path.is_dir()
        if not present:
            absent.append(f"{rel} - {why}")
    if absent:
        raise RegradeRefused(
            f"{rollout_dir} is not a completed episode, so there is nothing to "
            f"re-grade. Missing: " + "; ".join(absent) + ". --regrade re-runs "
            f"the stages that come AFTER teardown; it cannot run an episode.")


def _stored_world_diff(summary: dict | None) -> list | None:
    """The world-diff violations a run's own record carries, or None.

    None means "this record states no world-diff verdict", which is a different
    thing from "it states there was none" - the first cannot be carried forward
    and the second can. A violation the record spells in a shape this version of
    `Violation` does not take is treated as the first: a verdict we cannot read
    is not a verdict we may repeat.
    """
    if not isinstance(summary, dict) or "violations" not in summary:
        return None
    out = []
    for row in summary.get("violations") or ():
        if not isinstance(row, dict) or row.get("detector") != "world_diff":
            continue
        try:
            out.append(blindness.Violation(
                **{k: v for k, v in row.items()
                   if k in ("kind", "hard", "detector", "evidence", "where")}))
        except TypeError:
            return None
    return out


def _read_action_rows(path: Path) -> list[dict]:
    """The action log as rows, skipping lines that are not JSON objects."""
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def regrade(rollout_dir, task: str | None = None,
            tasks_dir: str | Path | None = None) -> dict:
    """Re-run one COMPLETED rollout's post-episode stages, from the files on disk.

    The stages, in `run_rollout`'s own order and for its own reasons:

        workspace-edit staging (native only) -> action log -> fingerprint ->
        artefact -> blindness.detect -> episode.grade_twice -> classify -> record

    The subject is never re-run and the world is never rebuilt. Returns the
    record it wrote. Raises `RegradeRefused` when the directory is not a
    completed episode, or when an input with no honest substitute is absent.
    """
    rollout_dir = Path(rollout_dir).resolve()
    _refuse_incomplete(rollout_dir)

    world_root = rollout_dir / "world"
    grading = rollout_dir / "grading"
    audit_dir = rollout_dir / "audit"

    prior = _read_json(rollout_dir / "rollout.json")
    prior = prior if isinstance(prior, dict) else None
    before = _read_json(rollout_dir / BEFORE_EPISODE_FILE)
    before = before if isinstance(before, dict) else None
    P, B = prior or {}, before or {}

    sources: dict[str, str] = {}
    missing: list[dict] = []
    carried: list[str] = []

    def take(piece: str, *candidates, consequence: str):
        """One before-episode input, or a record that it was never saved."""
        source, value = _first(*candidates)
        if source is None:
            sources[piece] = "NOT SAVED"
            missing.append({"piece": piece, "consequence": consequence})
            return None
        sources[piece] = source
        return value

    # --- identity. Refusals rather than degradations: a regrade that does not
    # know which task this was cannot load a rubric, and one that does not know
    # the driver cannot say what the subject was allowed to hold.
    src, task_id = _first(("rollout.json", P.get("task")),
                          (BEFORE_EPISODE_FILE, B.get("task")),
                          ("--task", task),
                          ("run directory layout",
                           rollout_dir.parent.parent.name or None))
    if not task_id:
        raise RegradeRefused(
            f"{rollout_dir} has no rollout.json and no {BEFORE_EPISODE_FILE}, "
            f"and its path does not name a task. Pass --task.")
    sources["task"] = src

    src, tdir = _first(("rollout.json", P.get("tasks_dir")),
                       (BEFORE_EPISODE_FILE, B.get("tasks_dir")),
                       ("--tasks-dir", str(tasks_dir) if tasks_dir else None))
    sources["tasks_dir"] = src or "the default tasks/ package directory"

    variant = _first(("rollout.json", P.get("variant")),
                     (BEFORE_EPISODE_FILE, B.get("variant")))[1]
    if variant:
        # every child process - the grader above all - resolves the arm from
        # here, exactly as run_rollout exports it
        os.environ[taskspec.VARIANT_ENV] = variant
    try:
        spec = taskspec.load(task_id, Path(tdir) if tdir else None,
                             variant=variant or None)
    except Exception as exc:                        # noqa: BLE001 - named
        raise RegradeRefused(
            f"the task package for {task_id!r} will not load, so this rollout "
            f"cannot be graded against it: {type(exc).__name__}: {exc}") from exc

    src, driver = _first(("rollout.json", P.get("driver")),
                         (BEFORE_EPISODE_FILE, B.get("driver")))
    if driver is None:
        driver, evidence = _driver_from_disk(rollout_dir)
        if driver is None:
            raise RegradeRefused(
                f"{rollout_dir} records no driver and nothing on disk names one "
                f"({evidence}). The driver decides the tool allowlist every "
                f"blindness detector is judged against, and the wrong one "
                f"accuses the subject of holding tools the harness gave it.")
        src = f"inferred: {evidence}"
    sources["driver"] = src

    src, topology = _first(("rollout.json", P.get("topology")),
                           (BEFORE_EPISODE_FILE, B.get("topology")))
    if topology is None:
        topology, evidence = _topology_from_disk(rollout_dir)
        src = f"inferred: {evidence}"
    sources["topology"] = src

    # --- the world manifest. No world, no grading: a refusal.
    src, world = _first(("rollout.json", P.get("world")),
                        (BEFORE_EPISODE_FILE, B.get("world")),
                        ("gateway.json, written at episode time",
                         _world_from_gateway_config(rollout_dir)))
    if not world:
        raise RegradeRefused(
            f"{rollout_dir} has no saved world manifest and no gateway.json to "
            f"rebuild one from, so the grader cannot be told where this world's "
            f"warehouse, workspace and grading zone are.")
    sources["world"] = src
    world = dict(world)
    world.setdefault("grading", str(grading))
    world, moved = _reroot_world(world, rollout_dir)
    if moved:
        carried.append(moved)
    absent = sorted(_grade_argv_placeholders(spec)
                    - {"ledger", "bi_state", "artifact"} - set(world))
    if absent:
        raise RegradeRefused(
            f"{task_id}'s grade_argv names {absent}, which the saved world "
            f"manifest for this rollout does not carry ({sources['world']}). "
            f"Substituting a path the harness never recorded would be grading "
            f"against a world we invented.")

    artifact_path = spec.artifact_host_path(world)
    if not artifact_path.exists():
        raise RegradeRefused(
            f"{rollout_dir} has no final artefact at {artifact_path}. An episode "
            f"that wrote none is a real outcome and run_rollout scores it as "
            f"one, but it is not the completed episode --regrade reproduces: "
            f"use --reclassify, or re-run it.")

    # --- the toolbelt. It IS the allowlist under mcp and kimi, so a missing one
    # cannot be defaulted to empty - that would flag every gateway call the
    # subject made. When it was not saved it is re-read from the gateway venv
    # and VERIFIED to cover every tool this episode's own init event says it was
    # granted; a gateway that has since lost a tool this subject held fails the
    # check and the regrade refuses rather than inventing a violation.
    transcript = blindness.load_transcript(rollout_dir / "transcript.jsonl")
    init = blindness.init_event(transcript)
    granted = sorted((init or {}).get("tools") or [])
    src, toolbelt = _first(("rollout.json", P.get("toolbelt")),
                           (BEFORE_EPISODE_FILE, B.get("toolbelt")))
    if toolbelt is None:
        if driver in blindness.MCP_SURFACE_DRIVERS:
            try:
                current = sorted(episode.gateway_tool_names())
            except episode.HarnessError as exc:
                raise RegradeRefused(
                    f"the toolbelt this episode was handed was not saved and the "
                    f"gateway's own toolbelt cannot be read either: {exc}") from exc
            uncovered = [t for t in granted if t.startswith(MCP_TOOL_PREFIX)
                         and t[len(MCP_TOOL_PREFIX):] not in set(current)]
            if uncovered:
                raise RegradeRefused(
                    f"the toolbelt this episode was handed was not saved, and "
                    f"the gateway's toolbelt today does not cover {uncovered} - "
                    f"tools the transcript's own init event says the subject was "
                    f"given. Judging it against today's allowlist would "
                    f"quarantine it for holding a tool the harness has since "
                    f"removed.")
            toolbelt = current
            src = ("re-read from the gateway venv, verified to cover every tool "
                   "the transcript's init event records as granted")
            missing.append({
                "piece": "toolbelt",
                "consequence": (
                    "the tool allowlist was re-read from the gateway venv at "
                    "regrade time rather than taken from what this episode was "
                    "handed. It is verified to cover every granted tool, so "
                    "neither the capability nor the tool-use detector can fire "
                    "spuriously; a tool ADDED to the gateway since would widen "
                    "the allowlist, and only the gateway can serve an "
                    f"{MCP_TOOL_PREFIX} name.")})
        else:
            # cli, native and scripted are judged against {"Bash"} whatever the
            # gateway holds (blindness.expected_tool_surface), so an unsaved
            # toolbelt costs those rollouts nothing
            toolbelt = []
            src = "not needed: this driver's allowlist is Bash, not the toolbelt"
    sources["toolbelt"] = src

    # --- the rest of the before-episode state -----------------------------
    tree_before = take(
        "world_tree_before",
        (BEFORE_EPISODE_FILE, B.get("tree_manifest")),
        consequence=(
            "the per-file world inventory taken before the episode was never "
            "written to disk, so the off-channel-change detector has nothing to "
            "diff against. It is NOT re-taken from the world as it stands now: "
            "that world is the one the episode already changed, and a 'before' "
            "picture taken after the fact would report every change as none. "
            "blindness.detect's evidence gate therefore quarantines this "
            "rollout as unobserved."))
    watermark = take(
        "action_log_watermark",
        ("rollout.json", P.get("action_log_watermark")),
        (BEFORE_EPISODE_FILE, B.get("action_log_watermark")),
        consequence=(
            "the action log's high-water mark at the moment the subject was "
            "handed the terminal was not saved, so rows the harness cannot "
            "identify by NAME are counted as the subject's. It can only "
            "over-count the subject's tool calls, never under-count them."))
    surfaces = take(
        "surfaces",
        ("rollout.json", P.get("surfaces")),
        (BEFORE_EPISODE_FILE, B.get("surfaces")),
        consequence=(
            "the pre-episode surface probe results were not saved and cannot be "
            "re-taken - the world has been worked in and graded against since. "
            "The surface audit reports `unprobed`, report.py withholds Pass@1, "
            "and a failure on a dead surface cannot be attributed."))
    fp_before = take(
        "world_fp_before",
        ("rollout.json", P.get("world_fp_before")),
        (BEFORE_EPISODE_FILE, B.get("world_fp_before")),
        ("audit/session.json, written by the gateway at boot",
         (_read_json(audit_dir / "session.json") or {}).get("initial_world_fp")),
        consequence=("the world's semantic fingerprint before the episode was "
                     "not saved, so `world_changed` cannot be stated"))
    exit_reason = take(
        "agent_exit_reason",
        ("rollout.json", (P.get("agent") or {}).get("exit_reason")),
        consequence=(
            "how the subject's process ended was not saved, so `timeout` and "
            "`budget` cannot be told apart from finishing normally. Recorded as "
            "`unrecorded`, which matches no classifier branch, rather than as "
            "`not_started` - which would be a claim, and a false one."))
    limits = take(
        "limits",
        ("rollout.json", P.get("limits")),
        (BEFORE_EPISODE_FILE, B.get("limits")),
        consequence=("the ceilings this rollout actually ran under were not "
                     "saved, so the task's declared defaults are used; a run "
                     "made with --wall-clock or --max-tool-calls is then "
                     "described by ceilings it did not have"))
    limits = limits or {"wall_clock_seconds": spec.wall_clock_seconds,
                        "max_tool_calls": spec.max_tool_calls}
    native_endpoints = (
        take("native_endpoints",
             ("rollout.json", P.get("native_endpoints")),
             (BEFORE_EPISODE_FILE, B.get("native_endpoints")),
             consequence=("the endpoint map this native episode was given was "
                          "not saved, so no destination in the connection log "
                          "can be judged allowed; the network detector reports "
                          "unobserved and the rollout quarantines"))
        if driver == "native" else None)
    container_paths = _first(("rollout.json", P.get("container_paths")),
                             (BEFORE_EPISODE_FILE, B.get("container_paths")))[1]
    containment = [c for c in (P.get("containment"), B.get("containment"),
                               P.get("containment_after")) if c]
    if topology == "container" and not containment:
        missing.append({
            "piece": "containment",
            "consequence": ("no containment probe evidence was saved for this "
                            "container, so the boundary cannot be asserted; "
                            "blindness records it unverified and the rollout "
                            "quarantines as unobserved")})

    # --- STAGE 1: the workspace plane's rows (native only) ----------------
    tree_after = episode.tree_manifest(world_root)
    action_rows = _read_action_rows(audit_dir / "actions.jsonl")
    workspace_edits = P.get("workspace_edits")
    if driver == "native":
        staged = [r for r in action_rows if r.get("tool") == "workspace.edit"]
        if staged or workspace_edits:
            carried.append(
                f"workspace.edit staging: {len(staged)} row(s) are already in "
                f"the action log from the run itself, so they are NOT staged "
                f"again - a second staging would double every row and inflate "
                f"the set of world files the detectors think was touched")
            workspace_edits = workspace_edits or {
                "rows": len(staged),
                "paths": sorted({(r.get("args") or {}).get("path")
                                 for r in staged} - {None})}
        elif tree_before is not None:
            workspace_edits = _stage_workspace_edits(
                audit_dir, world, tree_before, tree_after)
            action_rows = _read_action_rows(audit_dir / "actions.jsonl")
        else:
            missing.append({
                "piece": "workspace_edits",
                "consequence": ("this native rollout has no workspace.edit rows "
                                "and no before-episode tree manifest to derive "
                                "them from, so a working-tree change the subject "
                                "made has no row in the log accounting for it")})

    subject_rows = blindness.subject_rows(action_rows, watermark or 0)

    # --- the world diff, which is the one stage whose INPUT can be lost while
    # its OUTPUT survives. When the before-episode inventory was never written
    # to disk but a record from the run WAS, that record already carries the
    # verdict the detector reached with the real manifests in hand; it is
    # carried forward verbatim - `--reclassify`'s precedent, and the same
    # sentence it uses - and the detector is handed the same tree on both sides
    # so it adds nothing on top. When there is NO record either, nothing was
    # observed and nothing is invented: `world_before` stays None, detect's
    # evidence gate fires, and the rollout quarantines as unobserved.
    carried_world_diff: list = []
    tree_for_detect = tree_before
    entry = next((m for m in missing if m["piece"] == "world_tree_before"), None)
    stored_diff = _stored_world_diff(P.get("blindness"))
    if tree_before is None and stored_diff is not None:
        carried_world_diff = stored_diff
        tree_for_detect = tree_after
        carried.append(
            f"world-diff verdict ({len(carried_world_diff)} violation(s)) carried "
            f"forward from the record this run wrote; the world cannot be "
            f"re-inventoried after the fact")
        if entry:
            entry["consequence"] = (
                "the per-file world inventory taken before the episode was never "
                "written to disk, so the off-channel-change detector could not be "
                "re-run. Its verdict is carried forward from the record the run "
                "itself wrote - the detector DID run then, with the real "
                "manifests - and the world is not re-inventoried now, because the "
                "world as it stands is the one the episode already changed.")

    # --- STAGE 2: the episode-level readings ------------------------------
    rec: dict = dict(prior or {})
    for key, value in (("schema_version", 1), ("task", task_id),
                       ("rollout_id", rollout_dir.name),
                       ("run_id", rollout_dir.parent.name),
                       ("driver", driver), ("topology", topology),
                       ("seed", spec.seed), ("limits", limits),
                       ("tasks_dir", str(tdir) if tdir else None),
                       ("redteam_detector_test", driver == "scripted"),
                       ("model", _model_from_disk(rollout_dir)),
                       ("episode_id", (_read_json(audit_dir / "session.json")
                                       or {}).get("episode_id"))):
        if rec.get(key) is None and value is not None:
            rec[key] = value
    if rec.get("variant") is None:
        rec["variant"] = spec.variant
    rec.setdefault("episode_label",
                   f"{task_id}-{rollout_dir.parent.name}-{rollout_dir.name}")
    # The episode id the gateway wrote is sha256(episode_label)[:24], so a task,
    # run id and rollout id recovered from the directory layout can be CHECKED
    # rather than trusted. A mismatch means this directory is not the episode its
    # path says it is, and everything below would be filed under the wrong name.
    expected_id = hashlib.sha256(
        rec["episode_label"].encode("utf-8")).hexdigest()[:24]
    recorded_id = rec.get("episode_id")
    if recorded_id and recorded_id != expected_id:
        raise RegradeRefused(
            f"the episode id recorded at run time ({recorded_id}) is not "
            f"sha256({rec['episode_label']!r})[:24] = {expected_id}. This "
            f"directory is not the episode its path and record say it is.")

    # Unconditional, unlike the identity fields above: these name the directory
    # being regraded, and on a copy that is not the directory the record names.
    rec["world"] = world
    rec["rollout_dir"] = str(rollout_dir)
    rec["world_fp_before"] = fp_before
    rec["world_fp_after"] = episode.fingerprint(world_root)
    rec["world_changed"] = (None if fp_before is None
                            else rec["world_fp_after"] != fp_before)
    rec["artifact_path"] = str(artifact_path)
    artifact_text = artifact_path.read_text(encoding="utf-8", errors="replace")
    rec["artifact_bytes"] = len(artifact_text)
    rec["artifact_present"] = True
    try:
        rec["artifact"] = json.loads(artifact_text)
        rec.pop("artifact_unparseable", None)
    except ValueError:
        rec["artifact"] = None
        rec["artifact_unparseable"] = True
    if workspace_edits is not None:
        rec["workspace_edits"] = workspace_edits
    rec["surfaces"] = surfaces or {"probed": False, "surfaces": {}}
    rec["toolbelt"] = sorted(toolbelt or [])
    rec["action_log_watermark"] = watermark or 0
    rec["action_log"] = {
        "path": str(audit_dir / "actions.jsonl"),
        "rows": len(subject_rows),
        "rows_including_harness_authored": len(action_rows),
        "harness_authored_rows": len(action_rows) - len(subject_rows),
        "watermark_before_episode": watermark or 0,
        "gateway_started": (None if driver == "native"
                            else (audit_dir / "session.json").exists()),
    }
    rec["granted_tools"] = granted
    rec["mcp_servers"] = (init or {}).get("mcp_servers") or []
    rec["tool_uses_in_transcript"] = len(blindness.tool_uses(transcript))

    # --- STAGE 3: blindness ------------------------------------------------
    destinations = None
    network_allow = None
    if driver == "native":
        destinations = blindness.load_destinations(audit_dir)
        network_allow = (aut.allowed_destinations(native_endpoints)
                         if native_endpoints else None)
        rec["destinations"] = {
            "path": str(audit_dir / blindness.DESTINATIONS_FILE),
            "present": destinations is not None,
            "rows": len(destinations or []),
            "allowed": sorted(f"{h}:{p}" for h, p in (network_allow or ())),
        }
    violations = blindness.detect(
        transcript=transcript, driver=driver,
        gateway_tools=sorted(toolbelt or []),
        canaries=_recover_canaries(rollout_dir, grading),
        repo_root=REPO, action_rows=action_rows,
        # `tree_before` is None exactly when it was never saved, and detect's own
        # evidence gate then quarantines the rollout as unobserved. That is the
        # right answer and it is not softened here: an off-channel change to a
        # world nobody photographed cannot be ruled out, and `regrade.missing`
        # names the photograph that was never taken.
        world_before=tree_for_detect, world_after=tree_after,
        world_layout=world, artifact_text=artifact_text,
        topology=topology, containment=containment,
        destinations=destinations, network_allow=network_allow,
        normalise_host=aut.normalise_host, container_paths=container_paths)
    # appended, not merged: detect() emits the world diff last, so a carried
    # verdict lands exactly where the run's own record has it
    violations.extend(carried_world_diff)
    rec["blindness"] = blindness.summarise(violations)
    rec["blindness_guarantee"] = blindness.guarantee(
        driver=driver, topology=topology, containment=containment,
        container_execution=rec.get("container_execution_proof"))

    # --- STAGE 4: grade, twice, exactly as run_rollout does ---------------
    # The grader is TWO files: grade.py and the manifest it reads out of the
    # grading zone. build_world.py copied the manifest there at episode time
    # and rollout.py appended a canary comment to that copy; grade.py reads
    # the copy, never the task tree. A regrade exists because the grader was
    # wrong, and half of what makes it wrong lives in the manifest - GT003's
    # 2026-09-17 fix moved token lists and added cause families there - so
    # the copy is refreshed from the task's current manifest, the canary
    # comment is carried over verbatim (it is what _recover_canaries reads
    # back), and the hash below covers both halves. MEASURED before this:
    # a regrade of mcp-20260917-k5 re-ran the new grade.py against the old
    # manifest and reported the old F-LATE penalty on all five episodes.
    manifest_refresh = _refresh_grading_manifest(spec, Path(world["grading"]))
    sources["grading_manifest"] = manifest_refresh["source"]
    grade_sha = hashlib.sha256(
        spec.grade_entrypoint.read_bytes()
        + (manifest_refresh["sha256"] or "").encode("ascii")).hexdigest()
    score, score_stable, grader_crashed = None, True, False
    try:
        score, score_stable = episode.grade_twice(
            spec, world, artifact_path, rollout_dir)
        rec.pop("grader_error", None)
    except episode.HarnessError as exc:
        grader_crashed = True
        rec["grader_error"] = str(exc)
    rec["score"] = score
    rec["score_stable"] = score_stable
    rec["grader_crashed"] = grader_crashed
    rec["checks"] = ({c["id"]: {"points": c["points"], "max": c["max"],
                                "passed": c["passed"], "detail": c["detail"]}
                      for c in score.get("checks", [])} if score else {})

    # --- STAGE 5: classify -------------------------------------------------
    rec["surface_audit"] = surface_audit.audit(
        spec=spec, surfaces=rec["surfaces"], toolbelt=sorted(toolbelt or []))
    rec["undeclared_tools"] = sorted(
        set(toolbelt or []) - surface_audit.declared_tools(spec.surface_probes))
    rec["unprobed_tools"] = sorted(
        set(rec["surface_audit"]["unprobed_tools"])
        | set(rec["surface_audit"]["unswept_tools"]))
    was = P.get("classification") or "no_record"
    rec.update(classifier.classify(
        spec=spec, score=score, score_stable=score_stable,
        grader_crashed=grader_crashed, harness_failure=rec.get("harness_failure"),
        agent_exit_reason=exit_reason or "unrecorded",
        agent_final_text=(P.get("agent") or {}).get("final_text") or "",
        agent_stderr_tail=(P.get("agent") or {}).get("stderr_tail") or "",
        blindness=rec["blindness"], action_rows=subject_rows,
        surfaces=rec["surfaces"].get("surfaces", {}), artifact_present=True,
        wall_clock_s=limits["wall_clock_seconds"],
        max_tool_calls=limits["max_tool_calls"],
        unprobed_tools=rec["unprobed_tools"],
        surface_audit=rec["surface_audit"]))
    rec["tool_calls"] = len(subject_rows)

    # --- STAGE 6: the record -----------------------------------------------
    rec["regraded_from"] = was
    rec["regraded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rec["regrade_grader_sha256"] = grade_sha
    rec["regrade_partial"] = bool(missing)
    rec["regrade"] = {
        "schema_version": REGRADE_SCHEMA,
        "status": "partial" if missing else "complete",
        "grade_entrypoint": str(spec.grade_entrypoint),
        "grade_sha256": grade_sha,
        "grading_manifest": manifest_refresh,
        "sources": sources,
        "missing": missing,
        "carried_forward": carried,
        "identity_verified_against_episode_id": bool(recorded_id),
    }
    (rollout_dir / "rollout.json").write_text(
        json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return rec


def postgres_fold_failure(folded: dict | None) -> str | None:
    """The harness failure of a container rollout whose Postgres statements did not
    reach its action log (`NativeContainerEpisode.postgres_statements`, written by
    `down()`), or None. A log missing those rows is not the episode's record, so
    such a rollout is a harness failure - never graded, never counted."""
    error = (folded or {}).get("error")
    return (f"postgres statement log not folded into the action log: {error}"
            if error else None)


def run_rollout(args) -> dict:
    tasks_dir = Path(args.tasks_dir).resolve() if args.tasks_dir else None
    spec = taskspec.load(args.task, tasks_dir, variant=args.variant)
    # The environment profile (alkera_gateway.plugs), resolved for SERVING:
    # the task loader accepts plugs that are planned, not built, and this is
    # where a rollout of one stops - before a world, a process or a container,
    # with the catalog's own reason. None for an unprofiled task.
    profile_keys = episode.spec_profile_keys(spec)
    profile = episode.environment_profile(**profile_keys)
    # A profiled task runs on its role's image unless --image named one.
    args.image = container_episode.profile_image(
        getattr(args, "image", container_episode.DEFAULT_IMAGE), profile)
    if spec.variant:
        # Exported before anything forks, for the same reason `run_rollouts.py`
        # exports it: the world builder, the fixture stager and the grader are
        # all child processes and all of them resolve the arm from here. Set
        # even when `--variant` was not given and the value came from the
        # environment already, so a child cannot inherit a different one.
        os.environ[taskspec.VARIANT_ENV] = spec.variant
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
        "blindness_guarantee": ("tool_surface_revoked"
                                if args.driver in blindness.MCP_DRIVERS
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
        # The CONTEXT variant, recorded by the file that BUILDS the world
        # (docs/implementation_plan/harness/VARIANTS_REQUESTS.md item 5). `run_rollouts.py` also stamps it and
        # refuses to overwrite a different value, so the two cannot disagree
        # silently; what this line fixes is a bare `rollout.py` invocation of an
        # arm, which used to produce a record with no `variant` at all and made
        # `report.py` print "n/a (task declares one context)" for a run that had
        # one. "" when the task declares no `[variants]`.
        "variant": spec.variant,
        "limits": {"wall_clock_seconds": wall, "max_tool_calls": budget},
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rollout_dir": str(rollout_dir),
        "tasks_dir": str(tasks_dir) if tasks_dir else None,
        # Only for a task that declares one, so an unprofiled record is unchanged.
        **({"environment_profile": {**profile_keys,
                                    "services": list(profile.services)}}
           if profile is not None else {}),
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
    # docs/implementation_plan/harness/NATIVE_CONTAINER.md.
    native_container: container_episode.NativeContainerEpisode | None = None
    container_shell: dict | None = None
    containment: dict | None = None
    container_paths: dict | None = None

    try:
        # --- world -------------------------------------------------------
        world = episode.build_world(spec, world_root, grading)
        record["world"] = world
        # The world's planes must be the ones the task's profile serves tools
        # over, before anything is staged into it or served from it.
        episode.check_world_profile(Path(world["world_root"]), profile)
        if args.driver == "native":
            # `./ops/sched` goes into the working tree HERE, before the
            # fingerprint and before `tree_manifest`, because the workspace is
            # both fingerprinted and diffed: a file that appeared in it after
            # the "before" picture was taken is a world change with no row to
            # explain it. A team's own ops CLI is in the repo before anyone
            # clones it, and this is the same fact stated as an ordering.
            record["native_workspace_cli"] = native_stack.install_workspace_cli(
                world, args.topology)
            record["native_discovery"] = native_stack.stage_discovery(world)
            # The author-side material comes OUT of the staged world here, for
            # the same ordering reason: a native subject reads files, not tool
            # responses, and the strip must be part of the "before" picture or
            # the fingerprint hashes a world the subject never saw (STATUS
            # 4.3). No-op in the container topology, where these files are
            # never mounted - but the strip is what keeps that guarantee if the
            # mount set ever grows.
            record["native_world_sanitised"] = native_stack.sanitize_world_files(
                world)
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

        if args.topology == "container" and args.driver == "native":
            # NATIVE MODE IN THE CONTAINER. The same ordering as the mcp branch
            # below - containment first, the terminal second - with one
            # difference that is the whole reason this branch exists: here
            # containment IS the mount set, because native mode has no gateway
            # to be the boundary. gateway/docker/native_episode_probe.py is
            # containment P7 and it runs before the subject and again after.
            ok, docker_version = container_episode.docker_available()
            if not ok:
                raise episode.HarnessError(f"docker is not usable: {docker_version}")
            project = re.sub(r"[^a-z0-9_-]+", "-",
                             f"alkera-nc-{args.run_id}-{args.rollout_id}".lower())
            native_container = container_episode.NativeContainerEpisode(
                project=project, world=world, audit_dir=audit_dir,
                rollout_dir=rollout_dir, image=args.image,
                variant=spec.variant, **profile_keys,
                subject_sandbox=args.subject_sandbox == "container",
                subject_image=args.subject_image)
            record["image_gate"] = container_episode.assert_image_current(args.image)
            record["subject_sandbox_mode"] = args.subject_sandbox
            if native_container.subject_sandbox:
                record["subject_image"] = container_episode.subject_image_state(
                    args.subject_image)
            native_container.up()
            record["native_container"] = native_container.prepare()
            record["native_stack"] = {**native_container.record(),
                                      "docker_server": docker_version}
            # Two endpoint maps, and they are not interchangeable. The EPISODE
            # dials `<name>.internal`, which is what the service side records
            # and therefore what `blindness` must be judged against; the
            # HARNESS's surface gate runs in this process and dials the
            # published loopback ports, because it cannot resolve a name that
            # only exists inside the episode network.
            native_endpoints = native_container.episode_endpoints()
            record["native_endpoints"] = native_endpoints
            # A row written inside the container spells the warehouse
            # `/world/var/warehouse/warehouse.duckdb`; the world diff walks the
            # host tree. One declared translation, never a guess.
            container_paths = blindness.container_path_map(world)
            record["container_paths"] = container_paths
            containment = native_container.containment_probe(
                when="before_episode", canaries=canaries, grading=grading,
                rollout_dir=rollout_dir)
            record["containment"] = containment
            # THE SHELL IS PROVED BEFORE IT IS HANDED OVER. Run-id
            # `native-container-20260917-k5` recorded `filesystem_isolated` for
            # two episodes whose model ran on the HOST: the redirect was
            # ignored, nothing noticed, and a false guarantee is worse than
            # none. So the harness now runs `hostname` through the subject's
            # own shell path and compares it with the container's, and a
            # rollout whose shell does not reach the container does not start.
            if native_container.subject_sandbox:
                # The sandbox probe is told where the host keeps what the
                # subject must never see, and checks none of it exists inside.
                container_shell = native_container.subject_shell(
                    host_paths=[str(world_root), str(grading), str(rollout_dir),
                                str(REPO), str(Path.home())])
                record["subject_sandbox"] = container_shell["proof"]["sandbox"]
            else:
                container_shell = native_container.shell(rollout_dir / "shell")
            record["container_execution_proof"] = container_shell["proof"]
            if not container_shell["proof"]["verified"]:
                raise episode.HarnessError(
                    f"the subject's shell does not reach the episode "
                    f"container, so this rollout would measure the host: "
                    f"{container_shell['proof']}")
            toolbelt = {}
            surfaces = surface_audit.probe_native(
                probes=spec.surface_probes,
                endpoints=native_container.gate_endpoints(), world=world,
                schemas=surface_audit.warehouse_schemas(spec),
                # The gate asks the plane the SUBJECT can reach. Here the
                # scheduler is served at `sched.internal` and the world's state
                # file is not on the episode's filesystem at all, so reading it
                # would report a surface the subject cannot touch.
                served_planes=container_episode.SERVED_PLANES)
            gateway_cfg = None
        elif args.topology == "container":
            # Containment FIRST. The terminal is handed over only after this
            # rollout's own container has been probed and found sealed; if the
            # probe does not run or does not pass, blindness.detect() quarantines
            # the rollout rather than letting it into the number.
            if args.driver != "mcp":
                raise episode.HarnessError(
                    f"--topology container is wired for the mcp and native "
                    f"drivers only, not {args.driver!r}.")
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
                world, episode_id, spec.virtual_time, **profile_keys)
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
            toolbelt = episode.profile_toolbelt(stack.toolbelt(), profile)
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
                native_endpoints = aut.profile_endpoints(
                    aut.resolve_endpoints(args.native_endpoints), profile)
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
            toolbelt = episode.profile_toolbelt(episode.gateway_tool_names(),
                                                profile)
            surfaces = episode.probe_surfaces(
                spec, world, scratch,
                surface_audit.all_probes(toolbelt, spec.surface_probes))
            gateway_cfg = episode.write_gateway_config(
                rollout_dir / "gateway.json", world, audit_dir, episode_id,
                spec.virtual_time, **profile_keys, service_state=True)

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

        # The action log's high-water mark BEFORE the subject is handed the
        # terminal. Everything at or below it is the harness's - the surface
        # gate's health polls, and under the container topology the s3 check's
        # signed HEAD/LIST, whose rows arrive through MinIO's audit webhook
        # named after the S3 call rather than after us. See
        # `blindness.subject_rows`.
        record["action_log_watermark"] = _max_seq(audit_dir / "actions.jsonl")

        # The last thing written before the subject exists: everything that
        # describes the world at t=0, on disk, so that `--regrade` never has to
        # re-take a picture of a world the episode has since changed - and so
        # that a harness crash between teardown and the record write (GT009)
        # costs the episode nothing but the record. See the regrade block above.
        _write_before_episode(rollout_dir, record, tree_before)

        instruction = spec.instruction
        if args.adversarial_suffix:
            instruction += "\n\n" + Path(args.adversarial_suffix).read_text(
                encoding="utf-8")
            record["adversarial_suffix"] = str(args.adversarial_suffix)
            # this rollout exists to prove a detector fires. It is never a
            # measurement, and the report must not treat it as one.
            record["redteam_detector_test"] = True

        if (args.driver == "native" and args.topology == "container"
                and args.model == "scripted"):
            # The acceptance subject for the container topology. It runs THROUGH
            # the bash wrapper - the same launcher a model would be handed - so
            # the only untested link on the model path is `claude` itself.
            agent = native_stack.run_scripted_container(
                rollout_dir=rollout_dir, world=world, audit_dir=audit_dir,
                stack=native_container, wall_clock_s=wall,
                instruction=instruction, variant=spec.variant)
        elif args.driver == "native" and args.model == "scripted":
            # INTEGRATE-STACK: the Phase 3 acceptance subject. `aut.run` starts
            # `claude`, and the native driver had no way to be run end to end
            # without spending model quota; `--model scripted` runs a real bash
            # process through the real native environment instead. It is a
            # plumbing fixture, never a measurement, and `report.py` sees it as
            # the model string it is.
            agent = native_stack.run_scripted(
                rollout_dir=rollout_dir, world=world, audit_dir=audit_dir,
                endpoints=native_endpoints or {}, wall_clock_s=wall,
                instruction=instruction, variant=spec.variant,
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
                                       if native is not None else None),
                variant=spec.variant,
                container_shell=container_shell)
        record["agent"] = asdict(agent)

        if native_container is not None:
            # The same container, after the subject has had a root shell in it.
            # Containment that held at t=0 and not at t=end is not containment,
            # and under this topology containment is the only boundary there is.
            after = native_container.containment_probe(
                when="after_episode", canaries=canaries, grading=grading,
                rollout_dir=rollout_dir)
            record["containment_after"] = after

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
        if native_container is not None:
            try:
                native_container.down()
            except Exception as exc:                   # noqa: BLE001 - recorded
                record.setdefault("teardown_errors", []).append(
                    f"{type(exc).__name__}: {exc}")
            record["postgres_statements"] = native_container.postgres_statements
            harness_failure = harness_failure or postgres_fold_failure(
                native_container.postgres_statements)
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
    subject_action_rows = blindness.subject_rows(
        action_rows, record.get("action_log_watermark", 0))
    record["action_log"] = {
        "path": str(actions_path),
        "rows": len(subject_action_rows),
        "rows_including_harness_authored": len(action_rows),
        "harness_authored_rows": len(action_rows) - len(subject_action_rows),
        "watermark_before_episode": record.get("action_log_watermark", 0),
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
        normalise_host=aut.normalise_host, container_paths=container_paths)
    record["blindness"] = blindness.summarise(violations)
    # The guarantee is now READ OFF the evidence rather than declared from the
    # flags we passed. Under `container` it is only the stronger claim when this
    # rollout's own container was probed, before and after, and was sealed both
    # times; otherwise it stays what it can defend and the rollout quarantines.
    record["blindness_guarantee"] = blindness.guarantee(
        driver=args.driver, topology=args.topology,
        containment=[c for c in (record.get("containment"),
                                 record.get("containment_after")) if c],
        container_execution=record.get("container_execution_proof"))

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
        agent_final_text=(agent.final_text if agent else ""),
        agent_stderr_tail=(agent.stderr_tail if agent else ""),
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
    ap.add_argument("--regrade", default=None,
                    help="re-run the POST-EPISODE stages for a COMPLETED rollout "
                         "directory - workspace-edit staging, the action log, "
                         "the fingerprint, the artefact, blindness, the GRADER "
                         "(twice) and the classifier - from the files the "
                         "episode left behind. Never re-runs the subject and "
                         "never rebuilds the world. For a run whose grader was "
                         "broken, or whose harness died after teardown.")
    ap.add_argument("--tasks-dir", default=None,
                    help="directory holding the task package, when it is not "
                         "tasks/. Used by surface_gate_check.py to stand up "
                         "fixture packages outside the repo.")
    ap.add_argument("--variant", default=None,
                    help="context variant from the task's [variants] table "
                         "(docs/implementation_plan/harness/VARIANTS_REQUESTS.md item 5). Defaults to "
                         "ALKERA_TASK_VARIANT, which is what `run_rollouts.py` "
                         "exports; a task declaring no [variants] refuses the "
                         "flag rather than ignoring it.")
    ap.add_argument("--rollout-id", default="r0")
    ap.add_argument("--run-id", default="adhoc")
    ap.add_argument("--out")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--driver", default="native",
                    choices=["mcp", "cli", "scripted", "kimi", "cursor", "native"],
                    help="measurement substrate is native; mcp is the "
                         "comparison arm (default: native)")
    ap.add_argument("--native-endpoints", default=None,
                    help="JSON file mapping native surface names onto reachable "
                         "addresses, for `--driver native` off the episode "
                         "network. Defaults are the `<name>.internal` aliases; "
                         "see agent_under_test.resolve_endpoints.")
    ap.add_argument("--topology", default="container",
                    choices=["host", "container"],
                    help="where the WORLD and the TOOLS run. `host`: the gateway "
                         "is a host subprocess beside the grading zone, and "
                         "blindness rests on the tool surface being revoked. "
                         "`container`: the world, the gateway, the audit log and "
                         "a real shell live inside the episode service of "
                         "gateway/docker/compose.yml, and blindness rests on the "
                         "container - measured per rollout, never assumed. "
                         "(default: container)")
    ap.add_argument("--image", default=container_episode.DEFAULT_IMAGE,
                    help="image for the episode service under --topology container")
    ap.add_argument("--subject-image", default=container_episode.SUBJECT_IMAGE)
    ap.add_argument("--subject-sandbox", default="container",
                    choices=["container", "host"],
                    help="where the MODEL's CLI runs under --driver native "
                         "--topology container. `container` (default): in its "
                         "own locked-down container whose only way out is an "
                         "allowlisting proxy to the inference API "
                         "(tasks/harness/README.md#the-subject-sandbox). `host`: on the host, with "
                         "its bash redirected into the episode - detection only.")
    ap.add_argument("--wall-clock", type=int, default=None,
                    help="override the task's wall-clock ceiling (seconds)")
    ap.add_argument("--max-tool-calls", type=int, default=None,
                    help="override the task's tool-call ceiling")
    ap.add_argument("--adversarial-suffix", default=None,
                    help="file appended to the ticket - for red-teaming the "
                         "harness's own detectors, never for a measured run")
    a = ap.parse_args()
    if a.regrade:
        try:
            rec = regrade(Path(a.regrade), task=a.task, tasks_dir=a.tasks_dir)
        except RegradeRefused as exc:
            # A refusal is the result, not a crash: it names the input that is
            # not there, and a caller regrading a whole run needs to be able to
            # tell it apart from a rollout that regraded badly.
            print(json.dumps({"regrade": "refused", "reason": str(exc)}, indent=2))
            return 2
        print(json.dumps({"rollout_id": rec["rollout_id"],
                          "regraded_from": rec["regraded_from"],
                          "regrade": rec["regrade"]["status"],
                          "missing": [m["piece"] for m in rec["regrade"]["missing"]],
                          "classification": rec["classification"],
                          "passed": rec["passed"], "scored": rec["scored"],
                          "total": (rec.get("score") or {}).get("total"),
                          "grade_sha256": rec["regrade_grader_sha256"][:12],
                          "why": rec["why"]}, indent=2))
        return 0
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
        ap.error("--task and --out are required unless --reclassify or "
                 "--regrade is given")
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
