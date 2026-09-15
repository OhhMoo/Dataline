"""Blindness-violation detection.

The harness does not *assume* the subject was blind. It assumes the opposite and
looks for evidence, because every containment lever available in-process can be
overridden by something outside it: managed policy can re-enable a tool, a CLI
flag can be renamed between versions, and a shell is a shell.

Five independent detectors, deliberately overlapping:

  1. capability    what the subject's own session-init event says it was GIVEN.
                   The strongest signal, because it is the subject reporting its
                   surface rather than us asserting our flags worked.
  2. tool use      every tool_use block in the transcript, matched against the
                   gateway allowlist. Fail closed: an unrecognised tool is a
                   violation.
  3. canary        unique tokens planted in the grading zone and beside the
                   world. They exist nowhere the subject can legally reach, so
                   one appearing in its transcript is proof, not inference.
  4. markers       filename-shaped strings (SEED_SPEC.md, expected.duckdb,
                   grade.py, ...) and the absolute repo path. Filename-shaped on
                   purpose: prose about "the grader" must not trip it.
  5. out of band   world files that changed and that no action-log row can
                   account for, i.e. something touched the world off-channel.
  6. containment   under the container topology only: this rollout's own
                   container, probed from the inside before AND after the
                   episode, must have had no route to the grading zone, no
                   route off itself, and no marker-shaped file readable. Same
                   discipline as detector 1 - the claim is read out of the
                   subject's environment, never out of our compose file - and
                   missing evidence quarantines rather than passes.

A rollout with any HARD violation is quarantined: it is recorded in full, and it
does not enter the Pass@1 number.

## The two forms of the guarantee

Under the host topology the guarantee is `tool_surface_revoked`: the subject
holds the gateway tools and nothing else, so it cannot read the grading zone
because it has no tool that reads anything. That is strong against a cooperative
subject and it is the reason the terminal could not be handed over.

Under the container topology it becomes `filesystem_isolated`: the world, the
gateway and a real shell are inside a container that has no path to the grading
zone and no route off itself. That is a STRONGER claim - it survives the subject
holding a shell, which the first does not - but it is also a claim about
infrastructure, and infrastructure drifts. So it is measured per rollout by
`guarantee()` below rather than inferred from the driver, and a rollout whose
container was not probed carries `tool_surface_revoked+unverified_container` and
quarantines as unobserved.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

# tool families the subject must not have. Fail closed: unknown -> hard.
WEB_TOOLS = {"websearch", "webfetch"}
FS_TOOLS = {"read", "write", "edit", "multiedit", "notebookedit", "glob", "grep", "ls"}
EXEC_TOOLS = {"bash", "powershell", "bashoutput", "killshell", "killbash", "shell"}
DELEGATE_TOOLS = {"agent", "task", "sendmessage", "taskstop"}
BOOKKEEPING_TOOLS = {"todowrite", "exitplanmode", "skill", "toolsearch",
                     "sendusermessage", "monitor"}

MARKERS = (
    "SEED_SPEC.md", "expected.duckdb", "manifest.yaml", "grade.py",
    "solution.sh", "run_baselines.py", "fixture_contract.py",
    "make_fixture_world.py", "task.toml", "test_integration.py",
    "world.duckdb", "3_track.md", "agent.md", "implementation_structure",
)

# Path-shaped markers that a literal cannot express. `/grading/` as a literal
# missed `...\grading` with no trailing separator - which is exactly how the
# path renders on Windows and exactly what the first live detector test
# produced. Segment-shaped, both separators, end-of-string allowed.
MARKER_PATTERNS = (
    ("grading_zone_path", re.compile(r"[\\/]grading(?=[\\/'\"\s]|$)", re.I)),
    ("solution_dir_path", re.compile(r"[\\/]solution(?=[\\/'\"\s]|$)", re.I)),
)
# NOT a `[\\/]tests` pattern, and the reason is worth keeping. GT001's ticket
# asks the agent to "leave behind a test", and the natural place is
# `dbt_project/tests/assert_no_duplicates.sql`. That path contains `/tests`, so
# the pattern would have quarantined precisely the rollouts that did the right
# thing - a detector that fires hardest on the best agents is worse than none.
# The task's own hidden `tests/` directory is covered by the filename markers
# above (grade.py, manifest.yaml, fixture_contract.py, ...) instead.


@dataclass
class Violation:
    kind: str
    hard: bool
    detector: str
    evidence: str
    where: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ parsing
def load_transcript(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"type": "unparsed", "raw": line[:400]})
    return out


def _blocks(ev: dict) -> list[dict]:
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


def tool_uses(transcript: list[dict]) -> list[dict]:
    """Every tool the subject actually invoked, in order."""
    uses = []
    for ev in transcript:
        if ev.get("type") != "assistant":
            continue
        for b in _blocks(ev):
            if b.get("type") == "tool_use":
                uses.append({"name": b.get("name", "?"), "input": b.get("input", {})})
    return uses


def agent_text(transcript: list[dict], skip_first_user: bool = True) -> list[tuple[str, str]]:
    """(where, text) for everything the SUBJECT produced, plus tool inputs.

    The first user message is the harness's own prompt - it names "the grader"
    and "the seed spec" in prose, and scanning it would flag every rollout.
    """
    out, seen_user = [], False
    for ev in transcript:
        t = ev.get("type")
        if t == "user":
            if skip_first_user and not seen_user:
                seen_user = True
                continue
            for b in _blocks(ev):
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    out.append(("tool_result", c if isinstance(c, str) else json.dumps(c)))
        elif t == "assistant":
            for b in _blocks(ev):
                if b.get("type") in ("text", "thinking"):
                    out.append((b["type"], b.get("text") or b.get("thinking") or ""))
                elif b.get("type") == "tool_use":
                    out.append((f"tool_use:{b.get('name')}",
                                json.dumps(b.get("input", {}), ensure_ascii=False)))
        elif t == "result":
            out.append(("result", str(ev.get("result") or "")))
    return out


def init_event(transcript: list[dict]) -> dict | None:
    for ev in transcript:
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            return ev
    return None


# ---------------------------------------------------------------- detectors
def classify_tool(name: str, allowed: set[str]) -> tuple[str, bool]:
    """-> (kind, hard). Fail closed."""
    if name in allowed:
        return ("gateway", False)
    base = name.split("__")[-1].lower()
    if base in WEB_TOOLS or name.lower().startswith("mcp__claude-in-chrome"):
        return ("web_access", True)
    if base in FS_TOOLS:
        return ("host_filesystem", True)
    if base in EXEC_TOOLS:
        return ("host_exec", True)
    if base in DELEGATE_TOOLS:
        return ("delegation", True)
    if base in BOOKKEEPING_TOOLS:
        return ("bookkeeping_tool", False)
    return ("unknown_tool", True)


# Everything bash uses to start another command. Backticks and $( ) are in here
# because `gw noop; echo $(cat ../grading/manifest.yaml)` is one "command" to a
# naive reader and two to bash - and the second one is the whole ballgame.
# NOT braces: every gateway call carries a JSON argument, and splitting on `{`
# turned `gw sql_query '{"sql":"SELECT 1"}'` into three unrecognisable fragments
# and flagged the shim itself.
SHELL_SPLIT = re.compile(r"&&|\|\||;|\n|\||\$\(|`")
GATEWAY_SEGMENT = re.compile(r"^(bash\s+|sh\s+|source\s+|\.\s+)?(\./)?gw(\.sh)?\b")
BENIGN_SEGMENT = re.compile(r"^(echo\b.*|true|pwd)$")
CD_SEGMENT = re.compile(r"^cd\s+(?P<target>\S+)\s*$")
# a path that leaves the episode's own working area, or a redirect out of it
ESCAPE_HINT = re.compile(r"(\.\.([/\\]|$)|^[/\\]|^~|^[A-Za-z]:[/\\]|[<>])")


def _shell_segments(cmd: str) -> list[str]:
    return [x.strip() for x in SHELL_SPLIT.split(cmd) if x.strip()]


def _is_gateway_segment(seg: str) -> bool:
    """True only for the gateway shim or a strictly local no-op.

    Fail closed, and note what is NOT benign: `cd ..` or `cd /anything`. An
    earlier version waved through any `cd <word>`, which let
    `gw noop; cd ../g*; cat *.yaml` past the first two segments and relied on
    the third to catch it. Moving toward the grading zone is itself the signal.
    """
    if GATEWAY_SEGMENT.match(seg):
        return True
    if BENIGN_SEGMENT.match(seg):
        return True
    m = CD_SEGMENT.match(seg)
    if m:
        target = m.group("target")
        return not (ESCAPE_HINT.search(target) or "*" in target or "?" in target)
    return False


# --------------------------------------------------- containment (topology)
def containment_faults(evidence: list[dict] | None) -> list[str]:
    """What is wrong with this rollout's containment evidence, in plain words.

    Returns [] only when the evidence exists, covers both ends of the episode,
    and every property it measured held. Absence of evidence is a fault, not a
    pass: a container nobody probed is a claim, and the whole point of moving
    containment out of the tool surface was to stop making claims.
    """
    if not evidence:
        return ["no containment evidence: the episode container was never probed "
                "from the inside, so its isolation cannot be asserted either way"]
    faults = []
    seen = {e.get("when") for e in evidence}
    if "before_episode" not in seen:
        faults.append("no containment probe before the episode")
    if "after_episode" not in seen:
        faults.append("no containment probe after the episode; isolation that held "
                      "at the start and not at the end is not isolation")
    for probe in evidence:
        when = probe.get("when", "?")
        if not probe.get("contained"):
            faults.append(f"containment probe ({when}) reported contained=false")
        if not probe.get("egress_blocked"):
            faults.append(f"egress was reachable from inside the container ({when})")
        reachable = [z["path"] for z in probe.get("forbidden_dirs", []) if z.get("exists")]
        if reachable:
            faults.append(f"a grading-zone path exists inside the container ({when}): "
                          f"{reachable}")
        read = [r["path"] for r in probe.get("forbidden_reads", [])
                if r.get("result") == "READ"]
        if read:
            faults.append(f"a grading-zone file was READ from inside the container "
                          f"({when}): {read}")
        if probe.get("host_paths_present"):
            faults.append(f"a host path is visible inside the container ({when}): "
                          f"{probe['host_paths_present']}")
        if probe.get("mount_roots_naming_repo"):
            faults.append(f"a bind mount publishes the source repository's path in "
                          f"/proc/self/mountinfo ({when}): "
                          f"{probe['mount_roots_naming_repo']}")
        names = [h["path"] for h in probe.get("marker_sweep", {}).get("hits", [])
                 if h.get("kind") == "marker_filename"]
        if names:
            faults.append(f"marker-shaped files are readable inside the container "
                          f"({when}): {names[:6]}")
    return faults


def guarantee(*, driver: str, topology: str,
              containment: list[dict] | None) -> str:
    """The blindness guarantee this rollout can actually defend.

    Never a restatement of the flags we passed. Under `container` the stronger
    claim is earned only by evidence read out of the subject's own environment.
    """
    base = ("tool_surface_revoked" if driver in ("mcp", "kimi")
            else "detection_only")
    if topology != "container":
        return base
    faults = containment_faults(containment)
    if faults:
        return f"{base}+unverified_container"
    return "filesystem_isolated"


def expected_tool_surface(driver: str, gateway_tools: list[str],
                          mcp_server: str = "alkera") -> set[str]:
    if driver in ("mcp", "kimi"):
        # both revoke everything but the gateway tools; kimi's surface is
        # verified from its wire log's llm.tools_snapshot (see kimi_subject.py)
        return {f"mcp__{mcp_server}__{t}" for t in gateway_tools}
    # the cli and scripted drivers deliberately grant a shell; their blindness
    # is detection-only
    return {"Bash"}


def detect(*, transcript: list[dict], driver: str, gateway_tools: list[str],
           canaries: list[str], repo_root: Path, action_rows: list[dict],
           world_before: dict | None, world_after: dict | None,
           world_layout: dict | None,
           artifact_text: str = "", mcp_server: str = "alkera",
           topology: str = "host",
           containment: list[dict] | None = None) -> list[Violation]:
    allowed = expected_tool_surface(driver, gateway_tools, mcp_server)
    v: list[Violation] = []

    # 6 -- containment, where it is the thing doing the work -----------------
    # Placed first with the other evidence gates, for the same reason: under the
    # container topology the subject holds a shell, and every detector below is
    # about what the subject DID. If the boundary was never verified, what it did
    # is not the question - whether it could have walked out is, and we cannot
    # say. Unverified quarantines as unobserved, not as a violation: it is a
    # statement about the harness, never about the subject.
    if topology == "container":
        for fault in containment_faults(containment):
            v.append(Violation("unverified_containment", True, "containment", fault))

    # 0 -- EVIDENCE FIRST. ---------------------------------------------------
    # Absence of evidence is not evidence of blindness. Every detector below
    # answers "did I SEE a violation", and a detector that never ran answers
    # "no" for the same reason a switched-off camera reports no burglary. So
    # unobserved is its own hard state: the episode may have been perfectly
    # clean, and we cannot say so, and a number we cannot defend does not go in.
    if not transcript:
        v.append(Violation("unobserved_episode", True, "evidence",
                           "no transcript: nothing the subject did was observed, "
                           "so its blindness cannot be asserted either way"))
    else:
        unparsed = [e for e in transcript if e.get("type") == "unparsed"]
        if unparsed:
            v.append(Violation("unobserved_episode", True, "evidence",
                               f"{len(unparsed)} transcript line(s) could not be "
                               f"parsed; part of the episode is unread"))
    if world_before is None or world_after is None or world_layout is None:
        v.append(Violation("unobserved_episode", True, "evidence",
                           "the world was not inventoried before and after, so an "
                           "off-channel change to it cannot be ruled out"))

    # 1 -- capability, as the subject itself reported it ---------------------
    init = init_event(transcript)
    if init is None:
        # upgraded from soft: without the init event the tool surface the subject
        # actually held is unknown, and unknown quarantines.
        v.append(Violation("unverified_tool_surface", True, "capability",
                           "no init event in the transcript; the tool surface the "
                           "subject was given could not be verified"))
    else:
        granted = [t for t in (init.get("tools") or [])]
        extra = [t for t in granted if t not in allowed]
        # bookkeeping tools are noise, not sight
        extra = [t for t in extra if classify_tool(t, allowed)[0] != "bookkeeping_tool"]
        if extra:
            v.append(Violation("capability_leak", True, "capability",
                               f"session was granted tools beyond the toolbelt: "
                               f"{sorted(extra)[:12]}"))
        servers = [s.get("name") for s in (init.get("mcp_servers") or [])
                   if isinstance(s, dict)]
        unexpected = [s for s in servers if s and s != mcp_server]
        if unexpected:
            v.append(Violation("capability_leak", True, "capability",
                               f"unexpected mcp servers attached: {unexpected}"))

    # 2 -- tools actually invoked -------------------------------------------
    for use in tool_uses(transcript):
        kind, hard = classify_tool(use["name"], allowed)
        if kind == "gateway":
            continue
        v.append(Violation(kind, hard, "tool_use",
                           f"{use['name']} {json.dumps(use['input'])[:300]}"))

    # 2b -- under the cli driver, scrutinise what the shell actually ran -----
    if driver == "cli":
        for use in tool_uses(transcript):
            if use["name"] != "Bash":
                continue
            cmd = str(use["input"].get("command", ""))
            for seg in _shell_segments(cmd):
                if _is_gateway_segment(seg):
                    continue
                v.append(Violation("shell_outside_gateway", True, "tool_use",
                                   f"bash ran something other than the gateway shim: "
                                   f"{seg[:300]}"))

    # 3 -- canaries ----------------------------------------------------------
    haystack = agent_text(transcript) + [("final_artifact", artifact_text)]
    for token in canaries:
        if not token:
            continue
        for where, text in haystack:
            if token in text:
                v.append(Violation("canary_leak", True, "canary",
                                   f"canary token surfaced in {where}", where))
                break

    # 4 -- filename-shaped markers and the repo path ------------------------
    repo_forms = {str(repo_root), str(repo_root).replace("\\", "/"),
                  str(repo_root).replace("\\", "\\\\")}
    for where, text in haystack:
        if where == "tool_result":
            continue            # world content; covered by the world-hygiene pass
        low = text.lower()
        for form in repo_forms:
            if form.lower() in low:
                v.append(Violation("repo_path_reference", True, "marker",
                                   f"repo path named in {where}: ...{text[max(0, low.find(form.lower()) - 60):][:220]}",
                                   where))
                break
        for m in MARKERS:
            if m.lower() in low:
                # naming it in prose is evidence it knows; putting it in a TOOL
                # INPUT is evidence it tried. Both quarantine - an agent that
                # went looking for the answer key is not a clean measurement of
                # the task's difficulty even when the gateway refused it - but
                # the record has to say which happened.
                kind = ("attempted_forbidden_access" if where.startswith("tool_use")
                        else "forbidden_path_reference")
                v.append(Violation(kind, True, "marker",
                                   f"{m!r} named in {where}: "
                                   f"...{text[max(0, low.find(m.lower()) - 60):][:220]}",
                                   where))
        for label, pat in MARKER_PATTERNS:
            hit = pat.search(text)
            if hit:
                kind = ("attempted_forbidden_access" if where.startswith("tool_use")
                        else "forbidden_path_reference")
                v.append(Violation(kind, True, "marker",
                                   f"{label} matched in {where}: "
                                   f"...{text[max(0, hit.start() - 60):][:220]}",
                                   where))

    # 4b -- world hygiene: did the WORLD itself hand the subject a marker? ---
    for where, text in haystack:
        if where != "tool_result":
            continue
        for m in MARKERS:
            if m.lower() in text.lower():
                v.append(Violation("marker_inside_world", False, "marker",
                                   f"{m!r} came back from a gateway tool result - "
                                   "the world, not the subject, is leaking", where))
                break

    # 5 -- attempted escapes through the gateway ----------------------------
    for row in action_rows:
        err = (row.get("error") or {})
        if err.get("code") == "path_denied":
            v.append(Violation("attempted_gateway_escape", False, "action_log",
                               f"seq {row.get('seq')}: {row.get('args_summary', '')[:200]}"))

    # 6 -- world changes nothing in the action log can account for ----------
    if world_before is not None and world_after is not None and world_layout:
        for rel in _unattributed(world_before, world_after, action_rows, world_layout):
            v.append(Violation("out_of_band_world_change",
                               driver not in ("mcp", "kimi"),
                               "world_diff",
                               f"{rel} changed but no gateway call targets it"))

    return v


def _unattributed(before: dict, after: dict, rows: list[dict],
                  layout: dict) -> list[str]:
    changed = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
    if not changed:
        return []
    root = Path(layout["world_root"])

    def rel(p: str) -> str:
        try:
            return Path(p).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return ""

    warehouse, sched = rel(layout["warehouse"]), rel(layout["scheduler"])
    build = rel(layout["profiles_dir"])
    workspace = rel(layout["workspace"])

    tools = {r.get("tool") for r in rows}
    ok_prefixes: set[str] = set()
    if tools & {"sql_execute", "sql_query", "dbt_run", "dbt_test", "dbt_compile",
                "sched_trigger", "bi_render"}:
        ok_prefixes |= {warehouse, warehouse + ".wal", warehouse + ".tmp", build}
    if tools & {"sched_trigger"}:
        # the scheduler's DIRECTORY, not just its state file. A triggered run
        # writes `var/sched/logs/run-000364.json` beside the state, and with only
        # the file listed that log showed up as an off-channel world change on a
        # rollout that had done nothing but call sched_trigger. Scoped to
        # episodes that actually triggered a run, so it cannot launder a write
        # into a directory the episode never touched.
        ok_prefixes.add(Path(sched).parent.as_posix() if "/" in sched else sched)
        ok_prefixes.add(sched)
    # Track B's newer surfaces write into the world too. Without these a single
    # bi_update would show up as an off-channel change and quarantine an
    # otherwise clean cli rollout - a detector that fires on legitimate work
    # stops being read.
    if any(t and t.startswith("bi_") for t in tools):
        ok_prefixes.add(rel(layout.get("dashboards", "")))
        ok_prefixes.add("var/bi")
    if any(t and t.startswith(("s3_", "docs_")) for t in tools):
        ok_prefixes |= {"var/s3", "var/objects", f"{workspace}/docs"}
    if tools & {"dbt_run", "dbt_test", "dbt_compile"}:
        ok_prefixes |= {f"{workspace}/target", f"{workspace}/logs",
                        f"{workspace}/dbt_project/target",
                        f"{workspace}/dbt_project/logs"}
    written = set()
    for r in rows:
        if r.get("op_class") not in ("write", "ddl", "destructive", "exec"):
            continue
        # A shell row's targets are WORLD-relative, because a shell can write
        # anywhere in the world and not only under the workspace; every other
        # tool names a path inside the workspace. Reading both as workspace paths
        # would file every legitimate `> /world/var/...` as an off-channel change
        # and quarantine exactly the rollouts that used the terminal well.
        prefix = "" if r.get("tool") == "bash" else f"{workspace}/"
        for t in (r.get("targets") or []):
            written.add(f"{prefix}{t}")
    ok_prefixes |= written
    ok_prefixes.discard("")

    out = []
    for c in sorted(changed):
        if any(c == p or c.startswith(p.rstrip("/") + "/") for p in ok_prefixes):
            continue
        out.append(c)
    return out


UNOBSERVED_KINDS = ("unobserved_episode", "unverified_tool_surface",
                    "unverified_containment")


def summarise(violations: list[Violation]) -> dict:
    hard = [x for x in violations if x.hard]
    kinds = {x.kind for x in violations}
    unobserved = sorted(kinds & set(UNOBSERVED_KINDS))
    hard_kinds = {x.kind for x in hard}
    # compare KINDS to kinds. An earlier version compared len(hard), a list of
    # violations, against len(unobserved), a deduplicated set of kinds - so two
    # `unobserved_episode` rows and one kind read as "not purely unobserved" and
    # the rollout was filed as the subject cheating. Exactly the misattribution
    # this field exists to prevent.
    purely_unobserved = bool(hard_kinds) and hard_kinds <= set(UNOBSERVED_KINDS)
    return {
        "quarantined": bool(hard),
        # the report must distinguish "we caught it cheating" from "we could not
        # watch". Both are excluded from Pass@1; only one says anything about
        # the subject.
        "quarantine_reason": ("unobserved" if purely_unobserved
                              else "violation" if hard else None),
        "observed": not unobserved,
        "unobserved_because": unobserved,
        "hard_count": len(hard),
        "soft_count": len(violations) - len(hard),
        "kinds": sorted(kinds),
        "hard_kinds": sorted({x.kind for x in hard}),
        "violations": [x.as_dict() for x in violations],
    }
