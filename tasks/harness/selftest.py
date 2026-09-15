#!/usr/bin/env python3
"""Self-test for the detectors and the classifier.

A detector nobody has seen fire is not a detector. This drives each blindness
detector and each failure class with input that must trigger it, and each with
input that must NOT (a detector that fires on everything is also not a detector).

    python selftest.py

Fast: pure functions over synthetic transcripts and action logs. It does NOT
prove the plumbing - that is what the live adversarial rollout does
(`rollout.py --driver cli --adversarial-suffix ...`). Both are needed. This one
proves the logic; that one proves the wiring.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import blindness                      # noqa: E402
import classify as classifier         # noqa: E402
import kimi_subject                   # noqa: E402
import report                         # noqa: E402
import surface_audit                  # noqa: E402
import taskspec                       # noqa: E402

REPO = Path(__file__).resolve().parents[2]
GATEWAY_TOOLS = ["sql_query", "sql_execute", "fs_read", "fs_write", "fs_list",
                 "docs_search", "git_log", "dbt_run", "sched_state", "bi_render"]
CANARY = "ALKERA-CANARY-DEADBEEFDEADBEEF"

OK, BAD = [], []


def check(label, cond, detail=""):
    (OK if cond else BAD).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


# ------------------------------------------------------------ transcript fakes
def tr(*, tools=None, uses=(), texts=(), results=(), init=True):
    out = []
    if init:
        out.append({"type": "system", "subtype": "init",
                    "tools": list(tools if tools is not None
                                  else [f"mcp__alkera__{t}" for t in GATEWAY_TOOLS]),
                    "mcp_servers": [{"name": "alkera", "status": "connected"}],
                    "model": "test"})
    out.append({"type": "user", "message": {"role": "user", "content": "THE PROMPT: "
                "do not read the grader, the manifest or the seed spec."}})
    for name, args in uses:
        out.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": args}]}})
    for t in texts:
        out.append({"type": "assistant", "message": {"content": [
            {"type": "text", "text": t}]}})
    for r in results:
        out.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": r}]}})
    out.append({"type": "result", "subtype": "success", "result": "done"})
    return out


LAYOUT = {"world_root": "/w", "workspace": "/w/workspace",
          "warehouse": "/w/var/warehouse/warehouse.duckdb",
          "scheduler": "/w/var/sched/state.json", "profiles_dir": "/w/build"}


def detect(transcript, *, driver="mcp", rows=(), before=None, after=None,
           artifact=""):
    return blindness.detect(
        transcript=transcript, driver=driver, gateway_tools=GATEWAY_TOOLS,
        canaries=[CANARY], repo_root=REPO, action_rows=list(rows),
        world_before=before or {}, world_after=after or {},
        world_layout=LAYOUT, artifact_text=artifact)


def kinds(vs, hard_only=True):
    return {v.kind for v in vs if v.hard or not hard_only}


# --------------------------------------------------------------------- tests
def test_blindness():
    print("\n  blindness detectors")

    clean = tr(uses=[("mcp__alkera__sql_query", {"sql": "SELECT 1"}),
                     ("mcp__alkera__fs_write", {"path": "final_report.json",
                                                "content": "{}"})],
               texts=["I will not go looking outside the toolbelt."])
    v = detect(clean)
    check("clean rollout raises NO hard violation", not kinds(v), str(kinds(v)))
    check("clean rollout is not quarantined",
          not blindness.summarise(v)["quarantined"])

    v = detect(tr(uses=[("WebSearch", {"query": "dbt append duplicate AOV"})],
                  tools=[f"mcp__alkera__{t}" for t in GATEWAY_TOOLS] + ["WebSearch"]))
    check("web search is caught", "web_access" in kinds(v))

    v = detect(tr(uses=[("Read", {"file_path": "/somewhere/else.txt"})],
                  tools=[f"mcp__alkera__{t}" for t in GATEWAY_TOOLS] + ["Read"]))
    check("host filesystem read is caught", "host_filesystem" in kinds(v))

    v = detect(tr(uses=[("Bash", {"command": "ls -la .."})],
                  tools=[f"mcp__alkera__{t}" for t in GATEWAY_TOOLS] + ["Bash"]))
    check("shell use under the mcp driver is caught", "host_exec" in kinds(v))

    v = detect(tr(uses=[("Agent", {"prompt": "go read the repo for me"})],
                  tools=[f"mcp__alkera__{t}" for t in GATEWAY_TOOLS] + ["Agent"]))
    check("delegation to a sub-agent is caught", "delegation" in kinds(v))

    v = detect(tr(uses=[("SomeToolWeHaveNeverSeen", {})],
                  tools=[f"mcp__alkera__{t}" for t in GATEWAY_TOOLS]
                        + ["SomeToolWeHaveNeverSeen"]))
    check("an unknown tool fails CLOSED", "unknown_tool" in kinds(v))

    v = detect(tr(tools=[f"mcp__alkera__{t}" for t in GATEWAY_TOOLS] + ["Bash", "Read"]))
    check("capability leak is caught even when the tool is never used",
          "capability_leak" in kinds(v))

    v = detect(tr(tools=[f"mcp__alkera__{t}" for t in GATEWAY_TOOLS] + ["TodoWrite"],
                  uses=[("TodoWrite", {"todos": []})]))
    check("bookkeeping tools are NOT treated as sight", not kinds(v), str(kinds(v)))

    v = detect(tr(texts=[f"I found a file mentioning {CANARY}"]))
    check("canary in agent prose is caught", "canary_leak" in kinds(v))

    v = detect(tr(), artifact=json.dumps({"primary_cause": f"per {CANARY}"}))
    check("canary in the final artifact is caught", "canary_leak" in kinds(v))

    v = detect(tr(texts=["let me check expected.duckdb for the right answer"]))
    check("a forbidden filename in prose is caught",
          "forbidden_path_reference" in kinds(v))

    v = detect(tr(texts=[f"reading {REPO}\\tasks"]))
    check("naming the repo path is caught", "repo_path_reference" in kinds(v))

    v = detect(tr(texts=["the grader, the manifest and the seed spec are off limits, "
                         "so I will not look for them"]))
    check("PROSE about the grader/manifest/seed spec does NOT false-positive",
          not kinds(v), str(kinds(v)))

    # the ticket ASKS for a regression test, and the natural home for one is
    # dbt_project/tests/. A marker pattern on `/tests` would have quarantined
    # exactly the rollouts that did the right thing.
    v = detect(tr(uses=[("mcp__alkera__fs_write", {
        "path": "dbt_project/tests/assert_fct_orders_unique.sql",
        "content": "select order_id, line_nbr from {{ ref('fct_orders') }} "
                   "group by 1,2 having count(*) > 1"})]))
    check("writing the regression test the ticket asks for does NOT quarantine",
          not kinds(v), str(kinds(v)))

    v = detect(tr(uses=[("mcp__alkera__fs_read", {"path": "../grading/manifest.yaml"})]))
    check("...but reaching for the grading zone still does",
          "attempted_forbidden_access" in kinds(v))

    # the gateway REFUSED this one, so nothing leaked - but the agent aimed at
    # the grading zone, and an episode that went looking for the answer key is
    # not a clean measurement whether or not it got there.
    v = detect(tr(uses=[("mcp__alkera__fs_read", {"path": "../../grading/x"})]),
               rows=[{"seq": 1, "tool": "fs_read", "exit": "error",
                      "args_summary": '{"path":"../../grading/x"}',
                      "error": {"code": "path_denied", "message": "denied"}}])
    s = blindness.summarise(v)
    check("aiming a gateway tool at the grading zone quarantines",
          "attempted_forbidden_access" in kinds(v) and s["quarantined"])
    check("the gateway's own refusal is recorded alongside it",
          "attempted_gateway_escape" in {x["kind"] for x in s["violations"]})

    v = detect(tr(tools=["Bash"]), before={"workspace/a.sql": [1, "aa"]},
               after={"workspace/a.sql": [1, "bb"]},
               rows=[{"seq": 1, "tool": "sql_query", "op_class": "read",
                      "exit": "ok", "targets": []}], driver="cli")
    check("a world change no gateway call explains is caught (cli driver)",
          "out_of_band_world_change" in kinds(v))

    v = detect(tr(tools=["Bash"]), before={"workspace/a.sql": [1, "aa"]},
               after={"workspace/a.sql": [1, "bb"]},
               rows=[{"seq": 1, "tool": "fs_write", "op_class": "write",
                      "exit": "ok", "targets": ["a.sql"]}], driver="cli")
    check("an fs_write DOES explain its own file", not kinds(v), str(kinds(v)))

    v = detect(tr(init=False))
    s = blindness.summarise(v)
    check("a missing init event QUARANTINES (surface unverifiable)",
          "unverified_tool_surface" in kinds(v) and s["quarantined"])
    check("...and is reported as unobserved, not as the subject cheating",
          s["quarantine_reason"] == "unobserved" and not s["observed"])

    print("\n  cli driver")
    good = tr(tools=["Bash"], uses=[("Bash", {"command": 'gw sql_query \'{"sql":"SELECT 1"}\''})])
    v = detect(good, driver="cli")
    check("cli driver: the gateway shim is allowed", not kinds(v), str(kinds(v)))
    v = detect(tr(tools=["Bash"], uses=[("Bash", {"command": "cat /etc/passwd"})]),
               driver="cli")
    check("cli driver: any other shell command is caught",
          "shell_outside_gateway" in kinds(v))

    # --- reviewer finding 2: the guard was anchored at the start of the
    # command, so anything after the first separator was unexamined.
    v = detect(tr(tools=["Bash"], uses=[
        ("Bash", {"command": "gw noop; cd ../g*; cat *.yaml"})]), driver="cli")
    check("cli driver: a COMPOUND command hiding the escape after a ';' is caught",
          "shell_outside_gateway" in kinds(v))
    v = detect(tr(tools=["Bash"], uses=[
        ("Bash", {"command": 'gw sql_query \'{"sql":"SELECT 1"}\' '
                             '&& echo $(cat ../grading/manifest.yaml)'})]),
               driver="cli")
    check("cli driver: an escape inside $( ) is caught",
          "shell_outside_gateway" in kinds(v))
    v = detect(tr(tools=["Bash"], uses=[
        ("Bash", {"command": "gw fs_list '{}' | head -5"})]), driver="cli")
    check("cli driver: even a benign-looking pipe target is caught (fail closed)",
          "shell_outside_gateway" in kinds(v))
    v = detect(tr(tools=["Bash"], uses=[
        ("Bash", {"command": 'cd dbt_project && bash gw.sh fs_list \'{"path":"."}\''})]),
               driver="cli")
    check("cli driver: a LOCAL cd around the shim is still allowed",
          not kinds(v), str(kinds(v)))
    v = detect(tr(tools=["Bash"], uses=[
        ("Bash", {"command": "cd .. && bash gw.sh fs_list '{}'"})]), driver="cli")
    check("cli driver: but `cd ..` is not - moving toward the zone is the signal",
          "shell_outside_gateway" in kinds(v))


def test_missing_evidence():
    """Reviewer finding 1, and the more serious of the two: the detectors
    defaulted to CLEAN when they had nothing to look at."""
    print("\n  missing evidence must be `unknown`, and `unknown` must quarantine")

    v = detect([])
    s = blindness.summarise(v)
    check("an empty transcript quarantines instead of passing",
          s["quarantined"] and "unobserved_episode" in kinds(v))
    check("...and is labelled unobserved, not a violation by the subject",
          s["quarantine_reason"] == "unobserved")

    v = blindness.detect(
        transcript=tr(), driver="mcp", gateway_tools=GATEWAY_TOOLS,
        canaries=[CANARY], repo_root=REPO, action_rows=[],
        world_before=None, world_after=None, world_layout=None)
    check("an un-inventoried world quarantines (off-channel change unprovable)",
          blindness.summarise(v)["quarantined"]
          and "unobserved_episode" in kinds(v))

    t = tr()
    t.append({"type": "unparsed", "raw": "{truncated json"})
    v = detect(t)
    check("an unreadable transcript line quarantines the rollout",
          "unobserved_episode" in kinds(v))

    v = detect(tr())
    s = blindness.summarise(v)
    check("a fully observed clean episode is NOT quarantined",
          not s["quarantined"] and s["observed"], str(kinds(v)))

    # several unobserved rows, but still only unobserved KINDS. An earlier
    # version compared a list of violations to a deduplicated set of kinds and
    # filed this as the subject cheating.
    v = blindness.detect(
        transcript=[], driver="mcp", gateway_tools=GATEWAY_TOOLS,
        canaries=[CANARY], repo_root=REPO, action_rows=[],
        world_before=None, world_after=None, world_layout=None)
    s = blindness.summarise(v)
    check("MULTIPLE unobserved rows still read as unobserved, not as a violation",
          s["quarantine_reason"] == "unobserved" and s["hard_count"] >= 2,
          f"{s['hard_count']} hard rows, reason {s['quarantine_reason']}")


# ------------------------------------------------- container topology + shell
def probe(when, **overrides):
    """A containment probe result of the shape container_episode writes."""
    base = {
        "when": when,
        "contained": True,
        "egress_blocked": True,
        "forbidden_dirs": [{"path": "/grading", "exists": False}],
        "forbidden_reads": [{"path": "/grading/manifest.yaml",
                             "result": "FileNotFoundError: no such file"}],
        "host_paths_present": [],
        "marker_sweep": {"files_scanned": 150, "truncated": False, "hits": []},
        "network": {"default_route": False},
    }
    base.update(overrides)
    return base


def test_container_topology():
    """The guarantee moves from the tool surface to the container - and the
    container has to be MEASURED, per rollout, or the move is a downgrade."""
    print("\n  container topology: containment is evidence, never a flag")

    both = [probe("before_episode"), probe("after_episode")]
    check("a probed, sealed container earns `filesystem_isolated`",
          blindness.guarantee(driver="mcp", topology="container",
                              containment=both) == "filesystem_isolated")
    check("the host topology keeps `tool_surface_revoked` and is unaffected",
          blindness.guarantee(driver="mcp", topology="host",
                              containment=None) == "tool_surface_revoked")
    check("no evidence at all does NOT earn the stronger claim",
          blindness.guarantee(driver="mcp", topology="container",
                              containment=None)
          == "tool_surface_revoked+unverified_container")
    check("evidence from only one end of the episode does not earn it either",
          blindness.guarantee(driver="mcp", topology="container",
                              containment=[probe("before_episode")])
          == "tool_surface_revoked+unverified_container")

    def det(containment):
        return blindness.detect(
            transcript=tr(), driver="mcp", gateway_tools=GATEWAY_TOOLS + ["bash"],
            canaries=[CANARY], repo_root=REPO, action_rows=[],
            world_before={}, world_after={}, world_layout=LAYOUT,
            topology="container", containment=containment)

    v = det(None)
    s = blindness.summarise(v)
    check("an unprobed container quarantines rather than passing",
          s["quarantined"] and "unverified_containment" in kinds(v))
    check("...as UNOBSERVED - a statement about us, not about the subject",
          s["quarantine_reason"] == "unobserved")

    v = det([probe("before_episode"),
             probe("after_episode", contained=False,
                   forbidden_dirs=[{"path": "/grading", "exists": True}])])
    check("a grading path that EXISTS inside the container quarantines",
          "unverified_containment" in kinds(v))

    v = det([probe("before_episode"),
             probe("after_episode", contained=False, egress_blocked=False)])
    check("egress from inside the container quarantines", "unverified_containment" in kinds(v))

    v = det([probe("before_episode"),
             probe("after_episode", contained=False, forbidden_reads=[
                 {"path": "/grading/manifest.yaml", "result": "READ"}])])
    check("a grading file that was actually READ quarantines",
          "unverified_containment" in kinds(v))

    v = det([probe("before_episode"),
             probe("after_episode", contained=False, marker_sweep={
                 "files_scanned": 9, "truncated": False,
                 "hits": [{"kind": "marker_filename", "path": "/opt/x/grade.py"}]})])
    check("a marker-shaped FILE readable inside the container quarantines",
          "unverified_containment" in kinds(v))

    v = det([probe("before_episode"), probe("after_episode")])
    check("a container probed at both ends and sealed is NOT quarantined",
          not blindness.summarise(v)["quarantined"], str(kinds(v)))

    # the shell itself
    t = tr(tools=[f"mcp__alkera__{x}" for x in GATEWAY_TOOLS + ["bash"]],
           uses=[("mcp__alkera__bash", {"command": "git log --oneline -3"})])
    v = blindness.detect(
        transcript=t, driver="mcp", gateway_tools=GATEWAY_TOOLS + ["bash"],
        canaries=[CANARY], repo_root=REPO, action_rows=[],
        world_before={}, world_after={}, world_layout=LAYOUT,
        topology="container",
        containment=[probe("before_episode"), probe("after_episode")])
    check("the gateway's own bash tool is not a capability leak",
          not blindness.summarise(v)["quarantined"], str(kinds(v)))

    # the shell writes to the world, and the row must account for it
    rows = [{"seq": 1, "tool": "bash", "op_class": "exec",
             "targets": ["var/sched/state.json", "workspace/models/fct.sql"],
             "flags": ["shell"]}]
    v = blindness.detect(
        transcript=t, driver="mcp", gateway_tools=GATEWAY_TOOLS + ["bash"],
        canaries=[CANARY], repo_root=REPO, action_rows=rows,
        world_before={"var/sched/state.json": [1, "a"],
                      "workspace/models/fct.sql": [1, "a"]},
        world_after={"var/sched/state.json": [2, "b"],
                     "workspace/models/fct.sql": [2, "b"]},
        world_layout=LAYOUT, topology="container",
        containment=[probe("before_episode"), probe("after_episode")])
    check("a shell write the bash row NAMES is attributed, not flagged off-channel",
          "out_of_band_world_change" not in kinds(v, hard_only=False), str(kinds(v, False)))

    v = blindness.detect(
        transcript=t, driver="mcp", gateway_tools=GATEWAY_TOOLS + ["bash"],
        canaries=[CANARY], repo_root=REPO, action_rows=rows,
        world_before={"var/other/secret.json": [1, "a"]},
        world_after={"var/other/secret.json": [2, "b"]},
        world_layout=LAYOUT, topology="container",
        containment=[probe("before_episode"), probe("after_episode")])
    check("a world change NO row names is still flagged off-channel",
          "out_of_band_world_change" in kinds(v, hard_only=False), str(kinds(v, False)))


# ------------------------------------------------------------ kimi driver
def wire(*, tools=None, calls=(), texts=(), thinks=(), results=(),
         snapshot=True, extra=()):
    """A synthetic kimi session wire log (the shapes kimi 0.42.0 writes)."""
    lines = [
        {"type": "metadata", "protocol_version": "1.5", "created_at": 0},
        {"type": "turn.prompt", "input": "THE PROMPT"},
        {"type": "context.append_message", "message": {"role": "user",
         "content": [{"type": "text", "text": "THE PROMPT: do not read the "
                     "grader, the manifest or the seed spec."}]}},
    ]
    if snapshot:
        names = tools if tools is not None else [
            f"mcp__alkera__{t}" for t in GATEWAY_TOOLS]
        lines.append({"type": "llm.tools_snapshot",
                      "tools": [{"name": n} for n in names]})
    lines.append({"type": "llm.request", "model": "k3"})
    for t in thinks:
        lines.append({"type": "context.append_loop_event", "event": {
            "type": "content.part", "part": {"type": "think", "think": t}}})
    for name, args in calls:
        lines.append({"type": "context.append_loop_event", "event": {
            "type": "tool.call", "name": name, "args": args}})
    for r in results:
        lines.append({"type": "context.append_loop_event", "event": {
            "type": "tool.result", "result": {"output": r}}})
    for t in texts:
        lines.append({"type": "context.append_loop_event", "event": {
            "type": "content.part", "part": {"type": "text", "text": t}}})
    lines.extend(extra)
    lines.append({"type": "turn.ended", "reason": "completed"})
    return lines


def kimi_detect(wire_events, **kw):
    import tempfile
    d = Path(tempfile.mkdtemp())
    w, o = d / "wire.jsonl", d / "transcript.jsonl"
    w.write_text("\n".join(json.dumps(e) for e in wire_events) + "\n",
                 encoding="utf-8")
    kimi_subject.translate_wire(w, o)
    return detect(blindness.load_transcript(o), driver="kimi", **kw)


def test_kimi_driver():
    print("\n  kimi driver (wire translation + detection over it)")

    v = kimi_detect(wire(calls=[("mcp__alkera__sql_query", {"sql": "SELECT 1"})],
                         texts=["I will not go looking outside the toolbelt."]))
    s = blindness.summarise(v)
    check("kimi: a clean episode raises NO hard violation and is observed",
          not s["quarantined"] and s["observed"], str(kinds(v)))

    v = kimi_detect(wire(calls=[("Bash", {"command": "ls .."})],
                         tools=[f"mcp__alkera__{t}" for t in GATEWAY_TOOLS]
                               + ["Bash"]))
    check("kimi: a shell tool CALL is caught", "host_exec" in kinds(v))
    check("kimi: ...and the GRANTED shell is caught as a capability leak",
          "capability_leak" in kinds(v))

    v = kimi_detect(wire(snapshot=False))
    check("kimi: no tools_snapshot means no init event, which quarantines",
          "unverified_tool_surface" in kinds(v))

    v = kimi_detect(wire(extra=[{"type": "some.future.event", "x": 1}]))
    check("kimi: an unrecognised wire event fails CLOSED (unparsed line)",
          "unobserved_episode" in kinds(v))

    v = kimi_detect(wire(thinks=[f"hmm, the answer is probably {CANARY}"]))
    check("kimi: a canary in THINKING is caught", "canary_leak" in kinds(v))

    v = kimi_detect(wire(calls=[("mcp__alkera__fs_read",
                                 {"path": "../grading/manifest.yaml"})]))
    check("kimi: reaching for the grading zone is caught",
          "attempted_forbidden_access" in kinds(v))

    v = kimi_detect(wire(), before={"workspace/a.sql": [1, "aa"]},
                    after={"workspace/a.sql": [1, "bb"]},
                    rows=[{"seq": 1, "tool": "sql_query", "op_class": "read",
                           "exit": "ok", "targets": []}])
    soft = {x.kind for x in v if not x.hard}
    check("kimi: an unexplained world change is recorded as a SOFT signal, "
          "exactly as under the mcp driver (no shell to blame it on either "
          "way)", "out_of_band_world_change" in soft)


# ------------------------------------------------------------------ classifier
class FakeSpec:
    task_id = "T"
    wall_clock_seconds = 5400
    max_tool_calls = 600
    pass_requires_checks = ["D1", "D2"]
    pass_min_total = 8.0

    def scores_pass(self, score):
        by = {c["id"]: c for c in score["checks"]}
        return (all(by.get(c, {}).get("passed") for c in self.pass_requires_checks)
                and score["total"] >= self.pass_min_total)


def score(total, **passed):
    return {"total": total, "max": 10,
            "checks": [{"id": k, "points": 5 if v else 0, "max": 5, "passed": v,
                        "detail": ""} for k, v in passed.items()]}


def cls(**kw):
    base = dict(spec=FakeSpec(), score=score(10, D1=True, D2=True),
                score_stable=True, grader_crashed=False, harness_failure=None,
                agent_exit_reason="completed",
                blindness={"quarantined": False, "hard_count": 0, "soft_count": 0,
                           "kinds": [], "hard_kinds": []},
                action_rows=[{"tool": "sql_query", "exit": "ok"}] * 20,
                surfaces={}, artifact_present=True)
    base.update(kw)
    return classifier.classify(**base)


def test_classifier():
    print("\n  failure classification")
    r = cls()
    check("a solved episode classifies as pass", r["classification"] == "pass"
          and r["passed"] and r["scored"])

    r = cls(score=score(6, D1=True, D2=False))
    check("a wrong answer inside budget classifies as reasoning",
          r["classification"] == "reasoning" and r["scored"], r["why"][:70])

    r = cls(score=score(6, D1=True, D2=False), agent_exit_reason="timeout")
    check("a killed-on-wall-clock episode classifies as timeout",
          r["classification"] == "timeout" and r["scored"])

    r = cls(score=score(6, D1=True, D2=False), agent_exit_reason="budget")
    check("a killed-on-tool-budget episode classifies as budget",
          r["classification"] == "budget")

    r = cls(score=score(6, D1=True, D2=False),
            action_rows=[{"tool": "dbt_run", "exit": "error",
                          "error": {"code": "command_failed"}}] * 12,
            surfaces={"dbt": {"tool": "dbt_run", "live": False}})
    check("hammering a surface the probe found dead classifies as tool_gap",
          r["classification"] == "tool_gap" and r["needs_review"], r["why"][:90])

    r = cls(score=score(6, D1=True, D2=False), agent_exit_reason="timeout",
            action_rows=[{"tool": "dbt_run", "exit": "error",
                          "error": {"code": "command_failed"}}] * 12,
            surfaces={"dbt": {"tool": "dbt_run", "live": False}})
    check("a timeout spent on a dead surface is a tool_gap, not a timeout",
          r["classification"] == "tool_gap")

    r = cls(blindness={"quarantined": True, "hard_count": 1, "soft_count": 0,
                       "kinds": ["web_access"], "hard_kinds": ["web_access"]})
    check("a hard blindness violation quarantines and is NOT scored",
          r["classification"] == "blindness_violation" and not r["scored"]
          and not r["passed"])

    r = cls(score=score(10, D1=True, D2=True),
            blindness={"quarantined": True, "hard_count": 1, "soft_count": 0,
                       "kinds": ["canary_leak"], "hard_kinds": ["canary_leak"]})
    check("quarantine beats a PASSING score - a cheat never enters Pass@1",
          r["classification"] == "blindness_violation" and not r["passed"])

    r = cls(harness_failure="the gateway never started")
    check("a harness failure classifies as harness_error and is not scored",
          r["classification"] == "harness_error" and not r["scored"])

    r = cls(agent_exit_reason="agent_error")
    check("a subject process that died is harness_error, not reasoning",
          r["classification"] == "harness_error")

    r = cls(grader_crashed=True, score=None)
    check("a grader that crashed classifies as grader_defect",
          r["classification"] == "grader_defect" and not r["scored"])

    r = cls(score_stable=False, score=score(10, D1=True, D2=True))
    check("a grader that scores the same world differently twice is a defect",
          r["classification"] == "grader_defect" and not r["scored"])

    r = cls(blindness={"quarantined": True, "quarantine_reason": "unobserved",
                       "observed": False, "hard_count": 1, "soft_count": 0,
                       "kinds": ["unobserved_episode"],
                       "unobserved_because": ["unobserved_episode"],
                       "hard_kinds": ["unobserved_episode"]})
    check("an unobservable episode is `unobserved`, not `blindness_violation`",
          r["classification"] == "unobserved" and not r["scored"]
          and not r["passed"] and r["needs_review"])

    # when we KNOW why we could not watch, say so. Reporting our own crash as an
    # unexplained blind spot throws away the diagnosis we already have.
    r = cls(harness_failure="world builder failed: duckdb file locked",
            blindness={"quarantined": True, "quarantine_reason": "unobserved",
                       "observed": False, "hard_count": 2, "soft_count": 0,
                       "kinds": ["unobserved_episode"],
                       "unobserved_because": ["unobserved_episode"],
                       "hard_kinds": ["unobserved_episode"]})
    check("a KNOWN harness failure outranks `unobserved` and names the cause",
          r["classification"] == "harness_error" and "duckdb file locked" in r["why"])

    # ...but a real violation still outranks the harness failure: if the subject
    # reached outside, that is the finding, whatever else also went wrong.
    r = cls(harness_failure="grader path missing",
            blindness={"quarantined": True, "quarantine_reason": "violation",
                       "observed": True, "hard_count": 1, "soft_count": 0,
                       "kinds": ["canary_leak"], "unobserved_because": [],
                       "hard_kinds": ["canary_leak"]})
    check("a real violation still outranks a harness failure",
          r["classification"] == "blindness_violation")

    r = cls(score=score(6, D1=True, D2=False), artifact_present=False)
    check("a missing final artifact is flagged",
          any("no_final_artifact" in f for f in r["flags"]))


# ------------------------------------------------------- the surface gate
def surf(tool, *, live=True, code=None, declared=True, covers=()):
    return {"tool": tool, "covers": list(covers), "declared": declared,
            "live": live, "error_code": code,
            "error": None if live else json.dumps({"code": code}),
            "result_summary": "{}"}


class SurfaceSpec(FakeSpec):
    def __init__(self, probes=(), required=(), by_design=()):
        self.surface_probes = list(probes)
        self.required_surfaces = list(required)
        self.dead_by_design = [{"label": l, "reason": "declared"} for l in by_design]


def sa(surfaces, *, probes=(), required=(), by_design=(), toolbelt=None):
    spec = SurfaceSpec(probes, required, by_design)
    return surface_audit.audit(
        spec=spec, surfaces={"probed": bool(surfaces), "surfaces": surfaces},
        toolbelt=toolbelt if toolbelt is not None else GATEWAY_TOOLS)


def test_surface_audit():
    print("\n  pre-episode surface audit")
    probes = [{"label": "sql", "tool": "sql_query"},
              {"label": "chat", "tool": "chat_channels"}]

    a = sa({"sql": surf("sql_query"), "chat": surf("chat_channels")},
           probes=probes, required=["sql", "chat"])
    check("every required surface live: the gate passes", a["gate"] == "pass",
          a["gate_reason"])
    check("...and nothing stops the rollout",
          surface_audit.gate_error(a) is None)
    check("...and the run is clean", a["clean"], str(a["not_clean_because"]))

    a = sa({"sql": surf("sql_query"),
            "chat": surf("chat_channels", live=False, code="no_chat")},
           probes=probes, required=["sql", "chat"])
    check("a REQUIRED surface that is dead fails the gate", a["gate"] == "fail")
    check("...and names it", a["dead_required"] == ["chat"], str(a["dead_required"]))
    check("...and produces the HarnessError that stops the rollout",
          "chat" in (surface_audit.gate_error(a) or ""))
    check("...and the run is not clean", not a["clean"])

    a = sa({"sql": surf("sql_query"),
            "chat": surf("chat_channels", live=False, code="no_chat")},
           probes=probes, required=["sql"], by_design=["chat"])
    check("a surface declared DEAD BY DESIGN does not fail the gate",
          a["gate"] == "pass" and a["dead_by_design"] == ["chat"])
    check("...and a deliberately minimal world stays clean", a["clean"],
          str(a["not_clean_because"]))
    check("...and it is not counted as undeclared", a["dead_undeclared"] == [])

    a = sa({"sql": surf("sql_query"),
            "chat": surf("chat_channels", live=False, code="no_chat")},
           probes=probes, required=["sql"])
    check("a dead surface that is NEITHER required nor declared is named "
          "`dead_undeclared`", a["dead_undeclared"] == ["chat"])
    check("...and makes the run un-clean without stopping it",
          not a["clean"] and a["gate"] == "pass"
          and surface_audit.gate_error(a) is None)

    a = sa({"sql": surf("sql_query"), "chat": surf("chat_channels")},
           probes=probes, required=["sql"], by_design=["chat"])
    check("a dead_by_design declaration for a LIVE surface is reported stale",
          a["dead_by_design_but_live"] == ["chat"])

    # the ceiling: a tool the task never declared
    a = sa({"sql": surf("sql_query"),
            "~kb_list": surf("kb_list", live=False, code="no_knowledge_base",
                             declared=False)},
           probes=[{"label": "sql", "tool": "sql_query"}], required=["sql"])
    check("an UNPROBED tool that is dead cannot hide: the sweep finds it",
          a["dead"] == ["~kb_list"] and a["dead_undeclared"] == ["~kb_list"])
    check("...and it is fatal when the task requires it",
          sa({"~kb_list": surf("kb_list", live=False, code="no_knowledge_base",
                               declared=False)},
             probes=[{"label": "~kb_list", "tool": "kb_list"}],
             required=["kb_list"])["gate"] == "fail")

    a = sa({"~sql_execute": surf("sql_execute", live=False,
                                 code="invalid_arguments", declared=False)},
           probes=[], required=[])
    check("a SWEPT tool that needs arguments is `unreachable`, never `dead` - "
          "a probe we could not form says nothing about the world",
          a["unreachable_by_sweep"] == ["~sql_execute"] and a["dead"] == [])
    a = sa({"dbt": surf("dbt_compile", live=False, code="invalid_arguments")},
           probes=[{"label": "dbt", "tool": "dbt_compile"}], required=[])
    check("...but a DECLARED probe that will not validate is dead: the task "
          "asked, and nothing answered", a["dead"] == ["dbt"])

    a = sa({"logs": surf("logs_get", live=False, code="no_output")},
           probes=[{"label": "logs", "tool": "logs_get"}], required=[])
    check("an absence-shaped code this harness does not know is SUSPECT, "
          "not automatically fatal",
          a["suspect"] == ["logs"] and a["dead"] == [] and not a["clean"])

    sweep = surface_audit.sweep_probes(
        ["sql_query", "dbt_compile", "dbt_run", "dbt_test", "kb_list"],
        [{"label": "dbt", "tool": "dbt_compile", "covers": ["dbt_run"]}])
    names = sorted(p["tool"] for p in sweep)
    check("the sweep skips what the task already declares, and what it covers",
          "dbt_compile" not in names and "dbt_run" not in names, str(names))
    check("...and never runs dbt_test, which would mutate the probe copy",
          "dbt_test" not in names, str(names))
    check("...and does sweep everything else", names == ["kb_list", "sql_query"],
          str(names))
    check("...marking swept probes as NOT declared by the task",
          all(p["declared"] is False for p in sweep))

    check("a task with no probes at all cannot claim a verified world",
          sa({}, probes=[], required=[])["gate"] == "unprobed")
    check("a task that declares no required_surfaces cannot fail the gate, "
          "and the audit says so rather than reading as 'all fine'",
          sa({"sql": surf("sql_query")},
             probes=[{"label": "sql", "tool": "sql_query"}])["gate"]
          == "not_declared")


def test_taskspec_declarations():
    print("\n  task declarations the gate depends on")
    probes = [{"label": "sql", "tool": "sql_query"},
              {"label": "dbt", "tool": "dbt_compile", "covers": ["dbt_run"]}]

    def raises(fn):
        try:
            fn()
        except taskspec.TaskSpecError as exc:
            return str(exc)
        return ""

    check("requiring a surface nobody probes is refused - an unverifiable gate "
          "would silently pass",
          "no surface_probe declares" in raises(
              lambda: taskspec._check_surface_declarations(
                  "T", probes, ["chat"], [])))
    check("...while requiring a probe LABEL is accepted",
          not raises(lambda: taskspec._check_surface_declarations(
              "T", probes, ["sql"], [])))
    check("...and so is requiring a tool a probe `covers`",
          not raises(lambda: taskspec._check_surface_declarations(
              "T", probes, ["dbt_run"], [])))
    check("declaring a surface both required and dead_by_design is refused",
          "both required and dead_by_design" in raises(
              lambda: taskspec._check_surface_declarations(
                  "T", probes, ["sql"], [{"label": "sql", "reason": "x"}])))
    check("dead_by_design without a reason is refused",
          "no `reason`" in raises(
              lambda: taskspec._dead_by_design(
                  "T", {"dead_by_design": [{"label": "chat"}]})))
    check("...and with one is accepted",
          taskspec._dead_by_design(
              "T", {"dead_by_design": [{"label": "chat", "reason": "no chat "
                                        "in this world by design"}]})
          == [{"label": "chat", "reason": "no chat in this world by design"}])


def test_gate_classification():
    print("\n  a dead required surface as a run outcome")
    audit = {"gate": "fail", "dead_required": ["chat"], "dead_undeclared": [],
             "dead_by_design": []}
    r = cls(surface_audit=audit)
    check("a rollout that started with a required surface dead is a "
          "harness_error, not a score", r["classification"] == "harness_error")
    check("...and is NOT scored, so it never enters Pass@1",
          r["scored"] is False and r["passed"] is False)
    check("...even though the grader gave it full marks - the world it was "
          "measured in was not the task's",
          r["classification"] == "harness_error")
    check("...and it carries needs_review", r["needs_review"])
    check("...and the dead surface is flagged by name",
          any("dead_required_surfaces=['chat']" in f for f in r["flags"]),
          str(r["flags"]))

    r = cls(surface_audit=audit,
            blindness={"quarantined": True, "hard_count": 1, "soft_count": 0,
                       "kinds": ["canary_leak"], "hard_kinds": ["canary_leak"]})
    check("a subject caught reaching outside the toolbelt still outranks the "
          "gate - a cheat is a cheat whatever the world looked like",
          r["classification"] == "blindness_violation")

    r = cls(surface_audit=audit, harness_failure="the gateway never started")
    check("a harness failure we can NAME outranks the gate, for the same reason "
          "it outranks `unobserved`",
          r["classification"] == "harness_error"
          and "gateway never started" in r["why"])

    r = cls(surface_audit={"gate": "pass", "dead_required": [],
                           "dead_undeclared": [], "dead_by_design": []})
    check("a passing gate changes nothing about an ordinary rollout",
          r["classification"] == "pass" and r["scored"] and r["passed"])

    r = cls(surface_audit={"gate": "pass", "dead_required": [],
                           "dead_undeclared": ["kb"], "dead_by_design": []})
    check("an undeclared dead surface is FLAGGED but does not quarantine - we "
          "do not know it mattered, and the report is where that is settled",
          r["classification"] == "pass" and r["scored"]
          and any("dead_undeclared_surfaces" in f for f in r["flags"]))


# -------------------------------------------------------------- the report
def rec(rid, cls_, *, passed=None, scored=None, model="opus", variant="vague",
        gate="pass", dead_required=(), dead_undeclared=(), live=("sql", "fs"),
        redteam=False):
    passed = (cls_ == "pass") if passed is None else passed
    scored = (cls_ not in ("blindness_violation", "unobserved", "harness_error",
                           "grader_defect")) if scored is None else scored
    return {
        "rollout_id": rid, "classification": cls_, "passed": passed,
        "scored": scored, "model": model, "driver": "mcp", "topology": "host",
        "prompt_variant": variant, "prompt_sha256": "sha-" + variant,
        "blindness_guarantee": "tool_surface_revoked",
        "redteam_detector_test": redteam,
        "blindness": {"quarantined": not scored, "hard_kinds": [],
                      "violations": []},
        "score": {"total": 10 if passed else 4, "max": 10},
        "checks": {"D1": {"points": 5, "max": 5, "passed": passed, "detail": ""}},
        "tool_calls": 20, "wall_clock_s": 100, "why": "", "needs_review": False,
        "unprobed_tools": [],
        "surface_audit": {
            "probed": True, "gate": gate, "clean": True,
            "live": list(live), "dead": sorted(set(dead_required) | set(dead_undeclared)),
            "suspect": [], "unreachable_by_sweep": [],
            "required_labels": list(live), "dead_required": list(dead_required),
            "dead_by_design": [], "dead_by_design_but_live": [],
            "dead_undeclared": list(dead_undeclared),
            "declared_probes": 2, "swept_probes": 40,
            "unprobed_tools": [], "unswept_tools": [],
        },
    }


def test_report_conditions():
    print("\n  the report: conditions, and the refusal to publish without them")
    clean = [rec("r0", "pass"), rec("r1", "reasoning")]
    agg = report.aggregate(clean)
    md = report.render("T", "run", agg, clean)
    check("a clean run PUBLISHES Pass@1", agg["measurement_valid"]
          and "**Pass@1 = 0.5**" in md, str(agg["invalidated_because"]))
    check("...and states the live-surface set beside every rate it prints",
          all("surfaces: " in l for l in md.splitlines()
              if "Pass@1 = " in l or "Pass^k = " in l))
    check("...and names the prompt variant that produced it",
          "prompt variant: vague" in md)
    check("...with the conditions ABOVE the number, not under it",
          md.index("## Run conditions") < md.index("Pass@1 = "))

    dead = [rec("r0", "harness_error", gate="fail", dead_required=["chat"]),
            rec("r1", "pass", gate="fail", dead_required=["chat"])]
    agg = report.aggregate(dead)
    md = report.render("T", "run", agg, dead)
    check("a run with a DEAD REQUIRED surface cannot publish a Pass@1",
          not agg["measurement_valid"] and "Pass@1 = WITHHELD" in md)
    check("...and the reason is a fact about the world, not about the model",
          any("required surfaces were DEAD" in x
              for x in agg["invalidated_because"]),
          str(agg["invalidated_because"]))
    check("...and the rate is still computed, marked provisional, never dropped",
          agg["pass_at_1"] == 1.0 and "provisional, do not publish" in md)

    undecl = [rec("r0", "pass", dead_undeclared=["kb"])]
    agg = report.aggregate(undecl)
    check("a run with a dead surface the task never addressed cannot publish "
          "either", not agg["measurement_valid"]
          and any("neither requires nor declares" in x
                  for x in agg["invalidated_because"]))

    mixed = [rec("r0", "pass", variant="vague"),
             rec("r1", "pass", variant="guided")]
    agg = report.aggregate(mixed)
    check("a run that mixes prompt variants cannot publish one rate for both",
          not agg["measurement_valid"]
          and any("mixes prompts" in x for x in agg["invalidated_because"]))

    unlocked = [rec("r0", "pass", variant="UNLOCKED (brief sha256 ab matches "
                                          "none of ['vague'])")]
    check("a brief matching no locked variant cannot be attributed to a ticket",
          not report.aggregate(unlocked)["measurement_valid"])

    drifted = [rec("r0", "pass", live=("sql", "fs")),
               rec("r1", "pass", live=("sql",))]
    check("a live-surface set that differs across a byte-identical K is itself "
          "an invalidation",
          any("DIFFERS between rollouts" in x
              for x in report.aggregate(drifted)["invalidated_because"]))

    legacy = [rec("r0", "pass")]
    legacy[0].pop("surface_audit")
    agg = report.aggregate(legacy)
    check("a rollout with no surface audit at all cannot be published - the "
          "world it ran in is undescribed",
          not agg["measurement_valid"]
          and agg["run_conditions"]["rollouts_without_surface_audit"] == 1)


def test_pass_hat_k():
    print("\n  Pass^k")
    allpass = [rec(f"r{i}", "pass") for i in range(5)]
    agg = report.aggregate(allpass)
    check("five of five passed: Pass^k = 1.0",
          agg["pass_hat_k"] == 1.0 and agg["pass_hat_k_k"] == 5)
    check("...and Pass@1 agrees at 1.0", agg["pass_at_1"] == 1.0)

    one_bad = [rec(f"r{i}", "pass") for i in range(4)] + [rec("r4", "reasoning")]
    agg = report.aggregate(one_bad)
    check("four of five is Pass@1 0.8 and Pass^k 0.0 - the metric that "
          "separates 'usually' from 'always'",
          agg["pass_at_1"] == 0.8 and agg["pass_hat_k"] == 0.0)

    agg = report.aggregate([rec(f"r{i}", "pass") for i in range(3)]
                           + [rec("r3", "unobserved")])
    check("a quarantined rollout leaves k, it does not count as a failure - "
          "that would blame the model for the harness",
          agg["pass_hat_k"] == 1.0 and agg["pass_hat_k_k"] == 3)
    check("...and the report says the claim is weaker than the run intended",
          agg["pass_hat_k_complete"] is False
          and agg["pass_hat_k_attempted"] == 4)
    check("...and says so in the rendered report",
          "k < K" in report.render("T", "run", agg,
                                   [rec(f"r{i}", "pass") for i in range(3)]
                                   + [rec("r3", "unobserved")]))

    agg = report.aggregate([rec("r0", "harness_error")])
    check("a run with nothing scored has no Pass^k, rather than a 0 that reads "
          "as a model result", agg["pass_hat_k"] is None)

    # the GT001 shape that made this metric worth adding
    two = ([rec(f"c{i}", "pass", model="claude") for i in range(5)]
           + [rec("k0", "pass", model="kimi"), rec("k1", "pass", model="kimi"),
              rec("k2", "pass", model="kimi"), rec("k3", "reasoning", model="kimi")])
    agg = report.aggregate(two)
    check("Pass^k is computed per model: one number over two models says "
          "nothing about either",
          agg["pass_hat_k_by_model"]["claude"]["pass_hat_k"] == 1.0
          and agg["pass_hat_k_by_model"]["kimi"]["pass_hat_k"] == 0.0)
    check("...and the rendered report breaks it out when a run has two models",
          "| claude | 1.0 |" in report.render("T", "run", agg, two))


def main() -> int:
    print("\n  harness self-test")
    test_blindness()
    test_missing_evidence()
    test_container_topology()
    test_kimi_driver()
    test_classifier()
    test_surface_audit()
    test_taskspec_declarations()
    test_gate_classification()
    test_report_conditions()
    test_pass_hat_k()
    print(f"\n  {len(OK)} passed, {len(BAD)} failed")
    if BAD:
        print("  FAILED: " + ", ".join(BAD))
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
