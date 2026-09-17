#!/usr/bin/env python3
"""Executed proof of the native driver's harness side. No model, no quota.

    gateway/.venv/Scripts/python.exe tasks/harness/test_native_driver.py

WHAT IS REALLY EXECUTED HERE. A stub health service is started in-process on a
loopback port; a SCRIPTED subject - a real bash script in a real subprocess,
handed exactly the environment `agent_under_test.native_env` builds - curls that
service and writes one 9.2.2 row through `alkera_gateway.native.audit.record`.
The row, the transcript, the world inventory and the destinations log are then
read off disk by the real detectors. Nothing about the episode is asserted from
a literal: the row is the row the script wrote, the destination is the port the
stub bound, and the world diff is a before/after `episode.tree_manifest`.

WHAT IS NOT EXECUTED, and is designed only: the nine real fake services, the
Postgres and MinIO backings, the compose stack that writes
`audit/destinations.jsonl`, and the container topology. Those belong to
SERVICES-A/B and COMPOSE-DISCOVERY. This file proves the harness contract they
program against, and says so rather than implying more.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_under_test as aut          # noqa: E402
import blindness                        # noqa: E402
import classify as classifier           # noqa: E402
import episode                          # noqa: E402
import rollout                          # noqa: E402
import surface_audit                    # noqa: E402
from alkera_gateway.native.shims import sched_cli as schedcli  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

OK: list[str] = []
BAD: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    (OK if cond else BAD).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


# ----------------------------------------------------------------- the stub
# One stub answers for all nine services and for object storage, because what
# is under test is the GATE, not nine copies of the same HTTP call. Its answers
# are mutable so that both directions of every content rule can be driven
# against the same running server.
STUB = {
    "health": {"ok": True, "service": "stub", "items": 3},
    "health_raw": None,          # set to bytes to answer something unparseable
    # The status the health endpoint answers with. A fake service whose world
    # has no such plane answers 503 WITH a health document, so the gate has to
    # be driven at both statuses to prove it reads the body either way.
    "status": 200,
    "buckets": {"alkera": ["raw/orders.parquet"], "empty": []},
}

LIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<Name>{bucket}</Name><Prefix></Prefix><KeyCount>{n}</KeyCount>
<MaxKeys>1</MaxKeys><IsTruncated>false</IsTruncated>{contents}
</ListBucketResult>"""

ITEM_XML = ("<Contents><Key>{key}</Key>"
            "<LastModified>2026-09-16T00:00:00.000Z</LastModified>"
            "<ETag>&quot;0&quot;</ETag><Size>12</Size>"
            "<StorageClass>STANDARD</StorageClass></Contents>")


class _Stub(BaseHTTPRequestHandler):
    """`GET /health`, `HEAD /<bucket>` and `GET /<bucket>?list-type=2`.

    Anything else is a 404, so a probe that asked the wrong question fails
    rather than passing on a generous stub.
    """

    def _send(self, status, body=b"", ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _bucket(self):
        name = self.path.split("?", 1)[0].strip("/")
        return name, STUB["buckets"].get(name)

    def do_GET(self):                                   # noqa: N802 - stdlib API
        if self.path == "/health":
            raw = STUB["health_raw"]
            self._send(int(STUB["status"]),
                       raw if raw is not None
                       else json.dumps(STUB["health"]).encode())
            return
        name, keys = self._bucket()
        if keys is not None and "list-type=2" in self.path:
            xml = LIST_XML.format(
                bucket=name, n=len(keys),
                contents="".join(ITEM_XML.format(key=k) for k in keys))
            self._send(200, xml.encode(), "application/xml")
            return
        self._send(404)

    def do_HEAD(self):                                  # noqa: N802 - stdlib API
        _, keys = self._bucket()
        self._send(200 if keys is not None else 404)

    def log_message(self, *a):                          # noqa: D102 - silence
        return


def start_stub() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# --------------------------------------------------------------- task stubs
class SpecStub:
    """The two fields `surface_audit.audit` and `classify` read off a TaskSpec.

    A real task package is not needed to prove the native gate, and standing one
    up would put the world builder between the reader and the thing being shown.
    """

    task_id = "NATIVE-FIXTURE"
    wall_clock_seconds = 600
    max_tool_calls = 50
    pass_requires_checks = ["D1"]
    pass_min_total = 1.0

    def __init__(self, probes, required=()):
        self.surface_probes = list(probes)
        self.required_surfaces = list(required)
        self.dead_by_design = []

    def scores_pass(self, score):
        return bool(score) and score.get("total", 0) >= self.pass_min_total


PROBES = [{"label": "issues", "tool": "issues_list"},
          {"label": "bi", "tool": "bi_render"},
          {"label": "workspace", "tool": "fs_read"}]


# A solved episode, so that "the gate did not stop this rollout" is visible as
# the rollout being scored rather than as some other harness error.
SCORE = {"total": 10.0, "max": 10.0,
         "checks": [{"id": "D1", "points": 10.0, "max": 10.0, "passed": True,
                     "detail": ""}]}


def classify_of(audit_result, spec):
    return classifier.classify(
        spec=spec, score=SCORE, score_stable=True, grader_crashed=False,
        harness_failure=None, agent_exit_reason="completed",
        blindness={"quarantined": False, "soft_count": 0, "kinds": []},
        action_rows=[], surfaces={}, artifact_present=True,
        surface_audit=audit_result)


# ------------------------------------------------------------- the episode
SUBJECT_SCRIPT = """\
set -eu
# A native subject does not call a tool; it uses the shell. This one asks the
# service its environment points at whether it is alive, saves the answer, and
# lets the service-side writer put the 9.2.2 row in the action log.
code=$(curl -sS -o "$ALKERA_TEST_OUT/health.json" -w '%{http_code}' "$JIRA_URL/health")
echo "GET $JIRA_URL/health -> $code"
"$ALKERA_TEST_PY" "$ALKERA_TEST_ROW" "$code"
"""

ROW_WRITER = '''\
"""Stands in for the jira service's own call to native.audit.record.

The real row is written inside the service (SERVICES-A); what this proves is the
contract between the harness and that call - that `ALKERA_NATIVE_AUDIT`, set by
agent_under_test.native_env, lands the row in THIS episode's actions.jsonl.
"""
import sys

from alkera_gateway.native.audit import record

row = record(tool="jira.health", args={"path": "/health"}, op_class="read",
             targets=[], result={"http_status": int(sys.argv[1])},
             latency_ms=1.0, actor="native-test")
print(row["tool"], row["flags"])
'''


def build_episode(tmp: Path, port: int) -> dict:
    """A minimal world plus the episode directories a rollout would create."""
    world_root = tmp / "world"
    workspace = world_root / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "onboarding.md").write_text(
        "# onboarding\nthe warehouse is postgres; ask #data-platform\n",
        encoding="utf-8")
    (workspace / ".envrc").write_text("export JIRA_URL=${JIRA_URL:?}\n",
                                      encoding="utf-8")
    (world_root / "var").mkdir()
    (world_root / "var" / "state.json").write_text("{}\n", encoding="utf-8")
    profiles = world_root / "build"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(
        "world_dbt:\n  target: dev\n  outputs:\n    dev: {type: postgres}\n",
        encoding="utf-8")

    audit_dir = tmp / "audit"
    audit_dir.mkdir()
    out_dir = tmp / "subject_out"          # OUTSIDE the world, deliberately
    out_dir.mkdir()

    base = f"http://127.0.0.1:{port}"
    endpoints_file = tmp / "native_endpoints.json"
    endpoints_file.write_text(json.dumps(
        {name: base for name in aut.NATIVE_SERVICES} | {"s3": base}, indent=2),
        encoding="utf-8")
    return {
        "world": {"world_root": str(world_root), "workspace": str(workspace),
                  "warehouse": str(world_root / "var" / "warehouse.duckdb"),
                  "scheduler": str(world_root / "var" / "state.json"),
                  "profiles_dir": str(profiles)},
        "audit_dir": audit_dir, "out_dir": out_dir,
        "endpoints_file": endpoints_file,
    }


def run_subject(tmp: Path, ep: dict, endpoints: dict) -> dict:
    """The scripted subject: a real bash process under the real native env."""
    bash = shutil.which("bash")
    if bash is None:
        raise RuntimeError("no bash on PATH; this test needs a real shell")
    script = tmp / "subject.sh"
    script.write_text(SUBJECT_SCRIPT, encoding="utf-8", newline="\n")
    row_writer = tmp / "row_writer.py"
    row_writer.write_text(ROW_WRITER, encoding="utf-8", newline="\n")

    env = {**os.environ, **aut.native_env(
        endpoints=endpoints, audit_dir=ep["audit_dir"],
        profiles_dir=ep["world"]["profiles_dir"])}
    env.update({"ALKERA_TEST_OUT": ep["out_dir"].as_posix(),
                "ALKERA_TEST_PY": episode.PY.as_posix(),
                "ALKERA_TEST_ROW": row_writer.as_posix(),
                "PYTHONUTF8": "1"})
    proc = subprocess.run([bash, script.as_posix()], env=env,
                          capture_output=True, text=True, timeout=120)
    return {"returncode": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr, "env": env}


def transcript_of(tmp: Path, result: dict) -> list[dict]:
    """A stream-json transcript of what the scripted subject really did.

    Written to disk and read back through `blindness.load_transcript`, so the
    detectors run over a parsed file rather than an in-memory literal.
    """
    path = tmp / "transcript.jsonl"
    events = [
        {"type": "system", "subtype": "init", "tools": ["Bash"],
         "mcp_servers": [], "model": "scripted-native-subject"},
        {"type": "user", "message": {"role": "user", "content": "<the ticket>"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": 'curl -sS "$JIRA_URL/health"'}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": result["stdout"]}]}},
        {"type": "result", "subtype": "success", "result": result["stdout"].strip()},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events),
                    encoding="utf-8", newline="\n")
    return blindness.load_transcript(path)


def write_destinations(audit_dir: Path, rows: list[dict]) -> None:
    (audit_dir / blindness.DESTINATIONS_FILE).write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8",
        newline="\n")


def detect(transcript, world, rows, before, after, *, driver="native",
           destinations=None, allow=None):
    return blindness.detect(
        transcript=transcript, driver=driver, gateway_tools=[],
        canaries=["ALKERA-CANARY-NOTPLANTEDHERE"], repo_root=REPO,
        action_rows=rows, world_before=before, world_after=after,
        world_layout=world, artifact_text="",
        destinations=destinations, network_allow=allow,
        normalise_host=aut.normalise_host)


def hard(violations):
    return {v.kind for v in violations if v.hard}


def soft(violations):
    return {v.kind for v in violations if not v.hard}


# ------------------------------------------------------------------- main
def main() -> int:
    print("\n  native driver: env contract, prompt, detectors, surface gate")
    tmp = Path(tempfile.mkdtemp(prefix="alkera-native-test-"))
    srv, port = start_stub()
    try:
        ep = build_episode(tmp, port)
        world = ep["world"]
        endpoints = aut.resolve_endpoints(ep["endpoints_file"])

        # --- 1. the environment contract --------------------------------
        print("\n  the environment the subject discovers its stack through")
        env = aut.native_env(endpoints=endpoints, audit_dir=ep["audit_dir"],
                             profiles_dir=world["profiles_dir"])
        check("ALKERA_NATIVE_AUDIT is THIS episode's actions.jsonl",
              env["ALKERA_NATIVE_AUDIT"]
              == str(ep["audit_dir"] / "actions.jsonl"),
              env["ALKERA_NATIVE_AUDIT"])
        check("every service base url is in the environment, none of them named "
              "in a tool list",
              all(env[var] for _, (var, _) in aut.NATIVE_SERVICES.items()))
        check("the warehouse is addressed by PG* and the password is obviously fake",
              env["PGHOST"] == endpoints["postgres"]["host"]
              and env["PGPORT"] == str(endpoints["postgres"]["port"])
              and env["PGPASSWORD"] == "alkera-local-dev",
              f'{env["PGHOST"]}:{env["PGPORT"]} {env["PGPASSWORD"]}')
        check("AWS_ENDPOINT_URL and DBT_PROFILES_DIR point at this episode",
              env["AWS_ENDPOINT_URL"] == endpoints["s3"]
              and env["DBT_PROFILES_DIR"] == world["profiles_dir"])
        check("an unknown --native-endpoints key is REFUSED, never ignored",
              _refuses_unknown_endpoint(tmp))
        check("surface_audit and agent_under_test agree on the service list",
              set(aut.NATIVE_SERVICES) == set(surface_audit.NATIVE_SERVICE_NAMES))

        # --- 2. the prompt ----------------------------------------------
        print("\n  the prompt, which must not be a map of the world")
        prompt = aut.build_prompt(
            "## TICKET\nrevenue is halved since Tuesday.\n",
            {"sql_query": "run sql", "issues_list": "list issues"},
            "/workspace/final_report.json", 50, 600, "native",
            workspace=world["workspace"], profiles_dir=world["profiles_dir"])
        check("no gateway tool name appears anywhere in the native prompt",
              not any(t in prompt for t in ("sql_query", "issues_list",
                                            "mcp__alkera__", "gw.sh")))
        check("the discovery files that EXIST are named",
              "docs/onboarding.md" in prompt and ".envrc" in prompt
              and "profiles.yml" in prompt)
        check("the working tree is named as its real path",
              Path(world["workspace"]).as_posix() in prompt)
        check("a workspace with no set-up files promises none",
              "docs/onboarding.md" not in aut.build_prompt(
                  "t", {}, "/workspace/a.json", 1, 60, "native",
                  workspace=tmp / "nothing-here"))
        check("the native tool surface is an EMPTY mcp config plus the shell",
              aut.tool_surface_argv("native")
              == ["--mcp-config", '{"mcpServers": {}}', "--tools", "Bash"],
              str(aut.tool_surface_argv("native")))
        check("...and the mcp driver's tool-surface flags are untouched",
              aut.tool_surface_argv("mcp", Path("/x/mcp.json"))
              == ["--mcp-config", str(Path("/x/mcp.json")), "--tools", ""])

        # --- 3. the episode ---------------------------------------------
        print("\n  a scripted subject, a real shell, a real row")
        before = episode.tree_manifest(Path(world["world_root"]))
        result = run_subject(tmp, ep, endpoints)
        after = episode.tree_manifest(Path(world["world_root"]))
        check("the subject's shell reached the service and exited 0",
              result["returncode"] == 0 and "-> 200" in result["stdout"],
              (result["stdout"] + result["stderr"])[-300:])

        actions = ep["audit_dir"] / "actions.jsonl"
        rows = [json.loads(ln) for ln in
                actions.read_text(encoding="utf-8").splitlines() if ln.strip()]
        check("exactly one 9.2.2 row landed in the episode's actions.jsonl",
              len(rows) == 1, str(len(rows)))
        row = rows[0] if rows else {}
        check("...written where ALKERA_NATIVE_AUDIT pointed, not flagged "
              "unrecorded (native.audit reports a row it could not place)",
              "unrecorded" not in (row.get("flags") or [])
              and row.get("transport") == "native",
              json.dumps(row.get("flags")))
        check("...and it carries the 9.2.2 keys the detectors read",
              {"tool", "op_class", "targets", "exit", "args_hash", "wts"}
              <= set(row), sorted(row))

        transcript = transcript_of(tmp, result)
        allow = aut.allowed_destinations(endpoints)
        write_destinations(ep["audit_dir"], [{"host": "127.0.0.1", "port": port,
                                              "count": 1}])
        dest = blindness.load_destinations(ep["audit_dir"])
        v = detect(transcript, world, rows, before, after,
                   destinations=dest, allow=allow)
        s = blindness.summarise(v)
        check("the episode is CLEAN: no hard violation of any kind",
              not s["quarantined"], str(s["hard_kinds"]))
        check("...and the world did not change, so nothing is out of band",
              before == after)

        # --- 4. the network-destination rule ----------------------------
        print("\n  network_destination: the rule that replaces the allowlist")
        v = detect(transcript, world, rows, before, after,
                   destinations=dest + [{"host": "api.example.com", "port": 443,
                                         "count": 2}], allow=allow)
        check("a destination outside this episode's own stack is HARD",
              "network_destination" in hard(v), str(hard(v)))
        check("...and it quarantines as a VIOLATION, not as unobserved",
              blindness.summarise(v)["quarantine_reason"] == "violation")

        (ep["audit_dir"] / blindness.DESTINATIONS_FILE).unlink()
        check("an ABSENT destinations log reads as None, never as an empty log",
              blindness.load_destinations(ep["audit_dir"]) is None)
        v = detect(transcript, world, rows, before, after,
                   destinations=blindness.load_destinations(ep["audit_dir"]),
                   allow=allow)
        s = blindness.summarise(v)
        check("...and an absent log is UNOBSERVED and hard, never clean",
              "unobserved_network" in hard(v) and not s["observed"]
              and s["quarantine_reason"] == "unobserved", str(s["hard_kinds"]))

        write_destinations(ep["audit_dir"], [{"host": "127.0.0.1", "port": port,
                                              "count": 1},
                                             {"host": "nope"}])
        v = detect(transcript, world, rows, before, after,
                   destinations=blindness.load_destinations(ep["audit_dir"]),
                   allow=allow)
        check("a malformed destinations row is unobserved, not skipped",
              "unobserved_network" in hard(v), str(hard(v)))
        write_destinations(ep["audit_dir"], [{"host": "jira.internal",
                                              "port": 8080, "count": 0}])
        v = detect(transcript, world, rows, before, after,
                   destinations=blindness.load_destinations(ep["audit_dir"]),
                   allow=allow)
        check("a destination with count 0 was never dialled and is not a violation",
              not hard(v), str(hard(v)))
        write_destinations(ep["audit_dir"], [{"host": "localhost", "port": port,
                                              "count": 3}])
        v = detect(transcript, world, rows, before, after,
                   destinations=blindness.load_destinations(ep["audit_dir"]),
                   allow=allow)
        check("localhost and 127.0.0.1 are the same host, however it was logged",
              not hard(v), str(hard(v)))

        # --- 5. the two rules that no longer apply ----------------------
        print("\n  unknown_tool and shell_outside_gateway in native mode")
        shelly = transcript + [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Bash",
             "input": {"command": "psql -c 'select 1' | head -5"}}]}}]
        v = detect(shelly, world, rows, before, after, destinations=dest,
                   allow=allow)
        check("a shell command that is not a gateway shim is NOT a violation",
              not hard(v), str(hard(v)))
        v_cli = detect(shelly, world, rows, before, after, driver="cli")
        check("...while under the cli driver the same command still is",
              "shell_outside_gateway" in hard(v_cli), str(hard(v_cli)))

        odd = transcript + [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t3", "name": "SomethingNobodyGranted",
             "input": {}}]}}]
        v = detect(odd, world, rows, before, after, destinations=dest, allow=allow)
        check("an unrecognised tool name is recorded SOFT as unlisted_tool",
              "unlisted_tool" in soft(v) and not hard(v), str(hard(v) | soft(v)))
        v_mcp = detect(odd, world, rows, before, after, driver="mcp")
        check("...while under the mcp driver it still fails closed as unknown_tool",
              "unknown_tool" in hard(v_mcp), str(hard(v_mcp)))

        leak = [{"type": "system", "subtype": "init",
                 "tools": ["Bash", "Read", "WebFetch"], "mcp_servers": []}] \
            + transcript[1:]
        v = detect(leak, world, rows, before, after, destinations=dest, allow=allow)
        check("capability_leak is STILL hard: a granted Read quarantines",
              "capability_leak" in hard(v), str(hard(v)))

        # --- 6. out_of_band_world_change, split by plane ----------------
        # The working tree is a real filesystem with no server in front of it,
        # so the TRANSCRIPT audits it and the harness closes the action log
        # afterwards. Everything else under the world root is served, and keeps
        # the strict rule.
        print("\n  out_of_band_world_change: served planes vs the working tree")
        (Path(world["workspace"]) / "models.sql").write_text("select 1\n",
                                                             encoding="utf-8")
        after2 = episode.tree_manifest(Path(world["world_root"]))

        v = detect(transcript, world, rows, before, after2, destinations=dest,
                   allow=allow)
        check("before the harness closes the log, the edit is recorded SOFT - "
              "the transcript shows the shell that made it",
              "out_of_band_world_change" in soft(v) and not hard(v), str(hard(v)))

        staged = rollout._stage_workspace_edits(ep["audit_dir"], world, before,
                                                after2)
        rows2 = [json.loads(ln) for ln in
                 actions.read_text(encoding="utf-8").splitlines() if ln.strip()]
        edit_rows = [r for r in rows2 if r.get("tool") == "workspace.edit"]
        check("the harness appends one workspace.edit row per changed file, "
              "stamped actor=harness",
              staged["rows"] == 1 and len(edit_rows) == 1
              and edit_rows[0]["targets"] == ["workspace:models.sql"]
              and "actor:harness" in (edit_rows[0].get("flags") or []),
              json.dumps(staged))
        check("...it carries the before/after digests, so the row says WHAT "
              "changed and not merely that something did",
              edit_rows[0]["args"]["after_sha256"]
              == after2["workspace/models.sql"][1]
              and edit_rows[0]["args"]["before_sha256"] == "")
        v = detect(transcript, world, rows2, before, after2, destinations=dest,
                   allow=allow)
        check("...and with that row present the one-line edit classifies CLEAN",
              not blindness.summarise(v)["quarantined"], str(hard(v)))

        v = detect([], world, rows2, before, after2, destinations=dest,
                   allow=allow)
        check("the SAME edit with an empty transcript still quarantines: "
              "nothing the subject was given could have made it",
              "out_of_band_world_change" in hard(v), str(hard(v)))
        no_shell = [e for e in transcript
                    if not (e.get("type") == "assistant" and any(
                        b.get("type") == "tool_use" for b in
                        (e.get("message") or {}).get("content") or []))]
        v = detect(no_shell, world, rows2, before, after2, destinations=dest,
                   allow=allow)
        check("...and so does a full transcript with no shell command in it",
              "out_of_band_world_change" in hard(v), str(hard(v)))

        (Path(world["world_root"]) / "var" / "state.json").write_text(
            '{"run": 1}\n', encoding="utf-8")
        after3 = episode.tree_manifest(Path(world["world_root"]))
        v = detect(transcript, world, rows2, before, after3, destinations=dest,
                   allow=allow)
        check("a change under var/ - a SERVED plane - with no row is still HARD",
              "out_of_band_world_change" in hard(v), str(hard(v)))
        v = detect(transcript, world, rows2 + [
            {"seq": 9, "tool": "psql.statement", "op_class": "write",
             "exit": "ok", "targets": []}], before, after3, destinations=dest,
            allow=allow)
        check("...and a served-plane row that does NOT name it does not excuse it",
              "out_of_band_world_change" in hard(v), str(hard(v)))

        # --- 6a. a NATIVE warehouse write, attributed -------------------
        # The finding gateway/NATIVE.md recorded as weakest: under
        # MCP the warehouse file is explained by a row called `sql_execute`,
        # under native by a `duckdb.statement` whose targets are TABLES, and a
        # table name can never match a path. Both directions, on one world.
        print("\n  the duckdb shim's row explains the warehouse file")
        wh_world, wh_before, wh_after = _warehouse_world(tmp)
        delete_row = {
            "seq": 1, "tool": "duckdb.statement", "op_class": "destructive",
            "exit": "ok", "targets": ["main.scratch"],
            "args": {"sql": "DELETE FROM main.scratch",
                     "database": wh_world["warehouse"], "source": "-c"}}
        jira_row = {"seq": 1, "tool": "jira.issues_list", "op_class": "read",
                    "exit": "ok", "targets": ["issues"],
                    "read_targets": ["issues"]}
        check("a duckdb.statement DELETE naming the file it opened explains "
              "the warehouse that changed",
              blindness._unattributed(wh_before, wh_after, [delete_row],
                                      wh_world) == [],
              str(blindness._unattributed(wh_before, wh_after, [delete_row],
                                          wh_world)))
        v = detect(transcript, wh_world, [delete_row], wh_before, wh_after,
                   destinations=dest, allow=allow)
        check("...so the episode is CLEAN, where before it quarantined HARD",
              not blindness.summarise(v)["quarantined"], str(hard(v)))
        v = detect(transcript, wh_world, [jira_row], wh_before, wh_after,
                   destinations=dest, allow=allow)
        check("the SAME changed warehouse with only a jira row is HARD - a "
              "read of the issue tracker explains no byte of the warehouse",
              "out_of_band_world_change" in hard(v), str(hard(v)))
        elsewhere = {**delete_row,
                     "args": {**delete_row["args"],
                              "database": str(tmp / "somewhere-else.duckdb")}}
        check("...and a duckdb row that opened ANOTHER file explains nothing "
              "about this one",
              blindness._unattributed(wh_before, wh_after, [elsewhere],
                                      wh_world) != [])
        reader = {**delete_row, "op_class": "read", "targets": ["main.scratch"]}
        check("...nor does a duckdb READ, however many tables it named",
              blindness._unattributed(wh_before, wh_after, [reader],
                                      wh_world) != [])
        second = _second_warehouse(wh_world, wh_before, wh_after)
        check("a second DuckDB file the world BUILT but did not publish in its "
              "manifest is found the way fingerprint.py finds it",
              second == [], str(second))

        print("\n  a native row's MCP name is the suffix of its tool")
        check("row_tools reads `sched.sched_trigger` as `sched_trigger` and "
              "`deploy.deploy_run` as `deploy_run`",
              {"sched_trigger", "deploy_run", "duckdb.statement"}
              <= blindness.row_tools([{"tool": "sched.sched_trigger"},
                                      {"tool": "deploy.deploy_run"},
                                      {"tool": "duckdb.statement"}]),
              str(sorted(blindness.row_tools([{"tool": "sched.sched_trigger"}]))))
        # `wh_world` and not the episode layout above, whose scheduler sits at
        # `var/state.json`: the rule unlocks the scheduler's DIRECTORY, so a
        # world whose state file is directly under `var/` unlocks all of `var/`
        # and every check below would pass without proving anything.
        trigger_row = {"tool": "sched.sched_trigger", "op_class": "exec",
                       "targets": ["scheduler:load_orders_delta"]}
        sched_before = {"var/sched/state.json": [1, "a"]}
        sched_after = {"var/sched/state.json": [2, "b"],
                       "var/sched/logs/run-000009.json": [3, "c"]}
        check("...so a native sched.sched_trigger row accounts for the "
              "scheduler plane exactly as the MCP sched_trigger row does",
              blindness._unattributed(sched_before, sched_after,
                                      [trigger_row], wh_world) == []
              and blindness._unattributed(sched_before, sched_after,
                                          [jira_row], wh_world)
              == ["var/sched/logs/run-000009.json", "var/sched/state.json"])
        check("...and it accounts for the observability records the run "
              "PUBLISHES, which moved off-channel before the branch existed",
              blindness._unattributed(
                  {"var/obs/samples.jsonl": [1, "a"]},
                  {"var/obs/samples.jsonl": [2, "b"]},
                  [trigger_row], wh_world) == [])
        check("...and the engine's raw run log under executor.RUN_DIR "
              "(`build/sched/logs/...`), which is outside the fingerprint but "
              "inside the world the diff walks",
              blindness._unattributed(
                  {}, {"build/sched/logs/run-000034.log": [1, "a"]},
                  [trigger_row], wh_world) == []
              and blindness._unattributed(
                  {}, {"build/sched/logs/run-000034.log": [1, "a"]},
                  [jira_row], wh_world) == ["build/sched/logs/run-000034.log"])
        check("...while the live-world control plane is NOT accounted for: no "
              "transport gives the agent a way to move the world's clock, so a "
              "live.json that changed is still reported",
              blindness._unattributed(
                  {"var/world/live.json": [1, "a"]},
                  {"var/world/live.json": [2, "b"]},
                  [trigger_row], wh_world) == ["var/world/live.json"])
        check("`./ops/sched` offers no `advance`, and `sched_advance` is not a "
              "name any rule knows - an MCP agent cannot move the clock and a "
              "native one must not either",
              "advance" not in schedcli.SUBCOMMANDS
              and not any("advance" in t for t in blindness.SCHEDULER_TOOLS
                          | blindness.WAREHOUSE_TOOLS),
              str(sorted(schedcli.SUBCOMMANDS)))

        # --- 6c. the surface gate's own rows are not the subject's ------
        print("\n  health: rows are harness bookkeeping, not the episode's work")
        gate_row = {"seq": 7, "tool": "jira.health", "op_class": "read",
                    "exit": "ok", "targets": ["health:jira"],
                    "read_targets": ["issue:DATA-8110", "issue:DATA-8111"]}
        check("a row whose targets are all `health:` is harness-authored",
              blindness.is_harness_row(gate_row))
        check("...and so is a workspace.edit row",
              blindness.is_harness_row(edit_rows[0]))
        check("...while the subject's own jira call is not",
              not blindness.is_harness_row(row))
        mixed = rows2 + [gate_row]
        check("subject_rows strips exactly those, and nothing else",
              len(blindness.subject_rows(mixed)) == len(mixed) - 2
              and all(not blindness.is_harness_row(r)
                      for r in blindness.subject_rows(mixed)),
              f"{len(mixed)} -> {len(blindness.subject_rows(mixed))}")
        check("...so the gate's read_targets cannot enter the episode's target "
              "set, however real they are",
              not any("issue:DATA-8110" in (r.get("read_targets") or [])
                      for r in blindness.subject_rows(mixed)))
        check("a `health:` target never attributes a world change either",
              blindness._unattributed(
                  {"var/state.json": [1, "a"]}, {"var/state.json": [2, "b"]},
                  [{**gate_row, "op_class": "write",
                    "targets": ["health:jira", "var/state.json"]}], world)
              == ["var/state.json"],
              str(blindness._unattributed(
                  {"var/state.json": [1, "a"]}, {"var/state.json": [2, "b"]},
                  [{**gate_row, "op_class": "write",
                    "targets": ["health:jira", "var/state.json"]}], world)))

        # --- 6b. the gate is content-bearing ----------------------------
        print("\n  the health check asks for a COUNT, not a heartbeat")
        for body, want_live, want_code in (
                ({"ok": True, "service": "jira", "items": 4}, True, None),
                ({"ok": True, "service": "jira", "items": 0}, False, "no_content"),
                ({"ok": True, "service": "jira"}, False, "no_item_count"),
                ({"ok": True, "service": "jira", "items": "many"}, False,
                 "no_item_count"),
                ({"ok": False, "service": "jira", "items": 4}, False,
                 "health_not_ok"),
                ({"status": "ok"}, False, "no_health_schema"),
                ("OK", False, "no_health_schema")):
            live, code, _ = surface_audit.health_verdict(body, "stub")
            check(f"health {json.dumps(body)[:44]} -> "
                  f"{'live' if want_live else want_code}",
                  (live, code) == (want_live, want_code), f"got {(live, code)}")
        STUB["status"] = 503
        STUB["health"] = {"ok": False, "service": "airbyte",
                          "error": "no_ingest_state"}
        absent = surface_audit._http_health(endpoints["airbyte"], 5.0)
        check("a 503 carrying the module's own absence code is read as DEAD, "
              "not filed as an answer the gate could not read",
              absent[:2] == (False, "health_not_ok")
              and surface_audit._verdict(
                  {"live": False, "error_code": absent[1],
                   "declared": False}) == "dead", str(absent))
        STUB["health"] = {"ok": True, "service": "airbyte", "items": 4}
        check("...while a failure status with a body claiming ok is believed "
              "in neither direction",
              surface_audit._http_health(endpoints["airbyte"], 5.0)[:2]
              == (False, "no_health_schema"))
        STUB["status"] = 200
        STUB["health"] = {"ok": True, "service": "stub", "items": 3}
        check("items == 0 is DEAD, so an up-but-empty service cannot score",
              surface_audit._verdict({"live": False, "error_code": "no_content",
                                      "declared": True}) == "dead")
        check("...while a body the gate cannot read is SUSPECT, never live and "
              "never dead",
              surface_audit._verdict({"live": False, "declared": True,
                                      "error_code": "no_item_count"}) == "suspect"
              and surface_audit._verdict({"live": False, "declared": True,
                                          "error_code": "no_health_schema"})
              == "suspect")

        spec2 = SpecStub(PROBES, required=["issues", "bi"])
        STUB["health"] = {"ok": True, "service": "stub", "items": 0}
        empty = surface_audit.probe_native(probes=PROBES, endpoints=endpoints,
                                           world=world)
        ae = surface_audit.audit(spec=spec2, surfaces=empty, toolbelt=[])
        check("live services over an EMPTY world fail the gate - GT004, caught "
              "this time",
              ae["gate"] == "fail" and ae["dead_required"] == ["bi", "issues"],
              ae["gate_reason"])
        STUB["health"] = {"ok": True, "service": "stub"}
        nocount = surface_audit.probe_native(probes=PROBES, endpoints=endpoints,
                                             world=world)
        an = surface_audit.audit(spec=spec2, surfaces=nocount, toolbelt=[])
        check("a service that reports no `items` is suspect: the gate does not "
              "fail, the surface is neither live nor dead, and the run is not "
              "clean either",
              an["gate"] == "pass" and not an["dead_required"]
              and set(an["suspect"]) >= {"bi", "issues"} and not an["clean"],
              json.dumps(an["not_clean_because"])[:160])
        STUB["health"] = {"ok": True, "service": "stub", "items": 3}

        print("\n  object storage: the bucket must exist AND hold something")
        live_s3 = surface_audit._s3_bucket_content(endpoints, 5.0)
        check("a bucket with an object is live", live_s3[0], str(live_s3))
        empty_s3 = surface_audit._s3_bucket_content(
            {**endpoints, "s3_bucket": "empty"}, 5.0)
        check("...an existing but EMPTY bucket is dead (no_content)",
              empty_s3[:2] == (False, "no_content"), str(empty_s3))
        missing_s3 = surface_audit._s3_bucket_content(
            {**endpoints, "s3_bucket": "never-made"}, 5.0)
        check("...and a bucket that is not there is unreachable",
              missing_s3[:2] == (False, "service_unreachable"), str(missing_s3))

        print("\n  the warehouse: a table with a row in it, not a handshake")
        for tables, nonempty, want_live, want_code in (
                ([("ops", "dq_checks")], ["ops.dq_checks"], True, None),
                ([("ops", "dq_checks")], [], False, "no_content"),
                ([], [], False, "no_content")):
            live, code, _ = surface_audit.warehouse_verdict(
                tables, nonempty, ["ops"], "pg")
            check(f"warehouse {len(tables)} table(s), {len(nonempty)} non-empty "
                  f"-> {'live' if want_live else want_code}",
                  (live, code) == (want_live, want_code), f"got {(live, code)}")
        sql_down = surface_audit._sql_content(
            {"host": "127.0.0.1", "port": 1, "user": "u", "database": "d",
             "password": "p"}, ["ops"], 1.0)
        check("a warehouse that does not answer is dead, not skipped",
              sql_down[:2] == (False, "service_unreachable"), str(sql_down[:2]))
        print("\n  ...and in native mode that warehouse is a DUCKDB FILE")
        wh_layout, _, _ = _warehouse_world(tmp)
        # A file of its own, because `_warehouse_world` leaves its table EMPTY -
        # it exists to produce a DELETE that really changed the bytes, and an
        # empty table is what the dead direction of this check is about.
        duck = {"warehouse": _populated_warehouse(tmp),
                "workspace": wh_layout["workspace"],
                "scheduler": wh_layout["scheduler"],
                "profiles_dir": wh_layout["profiles_dir"]}
        live_wh = surface_audit._sql_surface(duck, "sql", endpoints, [], 5.0)
        check("the world's own warehouse file opens READ-ONLY through the "
              "aliased ATTACH and reports its content",
              live_wh[0] and "main.scratch" in live_wh[2], str(live_wh))
        check("...and it took no write lock: a second reader still opens it",
              surface_audit._sql_surface(duck, "sql", endpoints, [], 5.0)[0])
        empty_wh = _empty_warehouse(tmp)
        check("a warehouse whose tables are all EMPTY is dead (no_content) "
              "WHEN the task declared the schemas it expects content in",
              surface_audit._sql_surface({"warehouse": empty_wh}, "sql",
                                         endpoints, ["main"], 5.0)[:2]
              == (False, "no_content"))
        check("...and LIVE when it declared none, because the MCP probe for "
              "this surface is `SELECT 1` and GT008's nova is empty at t0 by "
              "design - one task.toml must not describe two tasks",
              surface_audit._sql_surface({"warehouse": empty_wh}, "sql",
                                         endpoints, [], 5.0)[0])
        check("...with the emptiness reported either way, so it is never hidden",
              "empty" in surface_audit._sql_surface(
                  {"warehouse": empty_wh}, "sql", endpoints, [], 5.0)[2],
              surface_audit._sql_surface({"warehouse": empty_wh}, "sql",
                                         endpoints, [], 5.0)[2][:160])
        check("a declared warehouse file that is not there is unreachable, "
              "never quietly live",
              surface_audit._sql_surface({"warehouse": str(tmp / "gone.duckdb")},
                                         "sql", endpoints, [], 5.0)[:2]
              == (False, "service_unreachable"))
        check("a schema the task did not declare is not sampled",
              surface_audit._sql_surface(duck, "sql", endpoints,
                                         ["nothing_here"], 5.0)[:2]
              == (False, "no_content"))
        named = {**duck, "warehouses": [{"name": "legacy",
                                         "path": str(tmp / "gone.duckdb")}]}
        check("`sql_query(warehouse='legacy')` asks about the LEGACY file, so "
              "GT008's two sql probes cannot answer for each other",
              surface_audit.native_surface_for("sql_query",
                                               {"warehouse": "legacy"})
              == "sql:legacy"
              and surface_audit._warehouse_path(named, "sql:legacy")
              == str(tmp / "gone.duckdb")
              and surface_audit._warehouse_path(named, "sql") == duck["warehouse"])
        check("...and a world that declares postgres keeps the psycopg path",
              surface_audit._sql_surface(
                  {**duck, "warehouse_backend": "postgres"}, "sql",
                  {"postgres": {"host": "127.0.0.1", "port": 1, "user": "u",
                                "database": "d", "password": "p"}}, [], 1.0)[1]
              in ("service_unreachable", "probe_driver_missing"))

        print("\n  dbt is its own surface: the toolchain AND the profile")
        good_profiles = _profiles(tmp / "dbt_good", duck["warehouse"])
        bad_profiles = _profiles(tmp / "dbt_bad", str(tmp / "elsewhere.duckdb"))
        check("a profiles.yml naming this world's warehouse matches",
              surface_audit.dbt_profile_targets_warehouse(
                  str(good_profiles), duck["warehouse"])[0])
        check("...and one naming another file does NOT, so a dbt that builds "
              "a warehouse nobody grades reads as dead",
              not surface_audit.dbt_profile_targets_warehouse(
                  str(bad_profiles), duck["warehouse"])[0])
        check("a relative path in the profile is resolved against the profiles "
              "dir, the way dbt resolves it",
              surface_audit.dbt_profile_targets_warehouse(
                  str(_profiles(tmp / "dbt_rel", "../populated.duckdb")),
                  duck["warehouse"])[0])
        check("with dbt on PATH and the right profile the surface is LIVE",
              surface_audit._dbt_toolchain(str(good_profiles),
                                           duck["warehouse"])[0]
              if surface_audit.dbt_executable() else True,
              str(surface_audit.dbt_executable()))
        check("...and with the wrong profile it is dead with a code that says "
              "which half failed",
              surface_audit._dbt_toolchain(str(bad_profiles),
                                           duck["warehouse"])[1]
              in ("no_dbt_profile", "no_dbt_toolchain"))
        check("both dbt failure codes are DEAD, never suspect",
              all(surface_audit._verdict(
                  {"live": False, "error_code": code, "declared": True}) == "dead"
                  for code in ("no_dbt_profile", "no_dbt_toolchain")))

        print("\n  the scheduler: a state file with a job in it")
        for state, want_live, want_code in (
                ({"jobs": {"load": {}}, "runs": []}, True, None),
                ({"jobs": {}, "runs": []}, False, "no_content"),
                ({"jobs": []}, False, "no_content"),
                ({"runs": []}, False, "no_scheduler_state"),
                ("not a document", False, "no_scheduler_state"),
                (None, False, "no_scheduler")):
            live, code, _ = surface_audit.scheduler_verdict(state, "state.json")
            check(f"scheduler {json.dumps(state)[:40]} -> "
                  f"{'live' if want_live else want_code}",
                  (live, code) == (want_live, want_code), f"got {(live, code)}")
        check("a real state file on disk is read, not asserted",
              surface_audit._scheduler_state(wh_layout["scheduler"])[:2]
              == (False, "no_content")
              and surface_audit._scheduler_state(
                  str(tmp / "no-such-state.json"))[:2] == (False, "no_scheduler"))
        check("sched and sched_log are one surface and one check",
              surface_audit.native_surface_for("sched_state")
              == surface_audit.native_surface_for("sched_logs") == "sched")

        print("\n  docs, the working tree and the repository")
        check("docs_* maps to workspace/docs and NOT to confluence - that is "
              "what Operations.docs_list reads",
              surface_audit.native_surface_for("docs_list") == "docs"
              and surface_audit.native_surface_for("kb_list") == "confluence")
        docs_dir = Path(duck["workspace"]) / "docs"
        check("an absent docs directory is dead",
              surface_audit._run_native_check("docs", endpoints, duck, [], 5.0)[1]
              == "no_docs")
        docs_dir.mkdir(parents=True, exist_ok=True)
        check("...an EMPTY one is dead too, on the same content rule",
              surface_audit._run_native_check("docs", endpoints, duck, [],
                                              5.0)[1] == "no_content")
        (docs_dir / "onboarding.md").write_text("# hello\n", encoding="utf-8")
        check("...and one with a page in it is live",
              surface_audit._run_native_check("docs", endpoints, duck, [],
                                              5.0)[0])
        check("fs_* is the working tree; a tree with a file in it is live",
              surface_audit._run_native_check("workspace", endpoints, duck, [],
                                              5.0)[0])
        check("git_* is the strictly stronger question, and a tree that is no "
              "repository is dead",
              surface_audit._run_native_check("git", endpoints, duck, [],
                                              5.0)[1] == "no_git_repository")
        check("...while a repository with a commit in it is live",
              _git_world_is_live(tmp, endpoints))

        print("\n  every surface the tasks REQUIRE now maps to a check")
        for task in ("GT004-doubling-window", "GT006-quality-gate-drops-rows",
                     "GT008-warehouse-cutover"):
            unmapped = _unmapped_required(task)
            check(f"{task}: every required surface has a native check",
                  unmapped == [], str(unmapped))

        check("[harness].warehouse_schemas is read off the task, never invented",
              surface_audit.warehouse_schemas(
                  type("S", (), {"raw": {"harness": {
                      "warehouse_schemas": ["ops", "mart"]}}})()) == ["ops", "mart"]
              and surface_audit.warehouse_schemas(SpecStub([])) == [])

        # --- 7. the surface gate ----------------------------------------
        print("\n  the surface gate, against a live stub and a dead one")
        spec = SpecStub(PROBES, required=["issues", "bi"])
        live = surface_audit.probe_native(probes=PROBES, endpoints=endpoints,
                                          world=world)
        a = surface_audit.audit(spec=spec, surfaces=live, toolbelt=[])
        check("the health checks that ran are the ones the spec names",
              {live["surfaces"]["issues"]["check"],
               live["surfaces"]["bi"]["check"],
               live["surfaces"]["workspace"]["check"]}
              == {"http_health", "path"},
              json.dumps({k: v["check"] for k, v in live["surfaces"].items()}))
        check("with the stub up, every required surface is live and the gate PASSES",
              a["gate"] == "pass" and surface_audit.gate_error(a) is None,
              a["gate_reason"])
        check("...and the sweep covered every service the task did not name",
              sorted(k for k in live["surfaces"] if k.startswith("~"))
              == sorted(f"~{n}" for n in surface_audit.NATIVE_SERVICE_NAMES
                        if n not in ("jira", "metabase")),
              str(sorted(k for k in live["surfaces"] if k.startswith("~"))))
        check("...and the run is clean", a["clean"], str(a["not_clean_because"]))
        check("...so the rollout is scored normally, not turned into a "
              "harness error",
              classify_of(a, spec)["classification"] == "pass"
              and classify_of(a, spec)["scored"] is True,
              classify_of(a, spec)["classification"])

        srv.shutdown()
        srv.server_close()
        dead = surface_audit.probe_native(probes=PROBES, endpoints=endpoints,
                                          world=world)
        b = surface_audit.audit(spec=spec, surfaces=dead, toolbelt=[])
        check("with the stub DOWN the required surfaces are dead and the gate FAILS",
              b["gate"] == "fail" and b["dead_required"] == ["bi", "issues"],
              b["gate_reason"])
        check("...the rollout is refused before the subject is handed the terminal",
              surface_audit.gate_error(b) is not None)
        check("...and it classifies as harness_error, never as a score",
              classify_of(b, spec)["classification"] == "harness_error"
              and classify_of(b, spec)["scored"] is False)
        check("...while the working tree, which did not go away, is still live",
              dead["surfaces"]["workspace"]["live"])
        check("a probe whose tool maps to no native surface fails CLOSED",
              _unmapped_is_dead(endpoints, world))
    finally:
        try:
            srv.shutdown()
        except Exception:                               # noqa: BLE001
            pass
        srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    # --- 8. the mcp driver is unchanged ---------------------------------
    print("\n  the existing harness, re-run in full")
    selftest = subprocess.run([str(episode.PY), str(HERE / "selftest.py")],
                              capture_output=True, text=True)
    tail = [ln for ln in selftest.stdout.splitlines() if "passed," in ln]
    check(f"selftest.py: {tail[-1].strip() if tail else 'no summary line'}",
          selftest.returncode == 0 and bool(tail) and "0 failed" in tail[-1])
    refused = subprocess.run([str(episode.PY),
                              str(HERE / "test_blindness_refused.py")],
                             capture_output=True, text=True)
    n_pass = sum(1 for ln in refused.stdout.splitlines()
                 if ln.startswith("PASS "))
    check(f"test_blindness_refused.py: {n_pass}/4 checks",
          refused.returncode == 0 and n_pass == 4)

    print(f"\n  {len(OK)} passed, {len(BAD)} failed")
    if BAD:
        print("  FAILED: " + "; ".join(BAD))
    return 1 if BAD else 0


def _warehouse_world(tmp: Path) -> tuple[dict, dict, dict]:
    """A world whose DuckDB warehouse really changed. (layout, before, after).

    A real file written twice rather than two literals, so the "changed" the
    detector sees is a changed sha256 of real DuckDB bytes.
    """
    root = tmp / "wh_world"
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    (root / "workspace" / "model.sql").write_text("select 1\n", encoding="utf-8")
    (root / "var" / "warehouse").mkdir(parents=True, exist_ok=True)
    (root / "build").mkdir(parents=True, exist_ok=True)
    (root / "var" / "sched").mkdir(parents=True, exist_ok=True)
    (root / "var" / "sched" / "state.json").write_text(
        '{"jobs": {}, "runs": []}\n', encoding="utf-8")
    warehouse = root / "var" / "warehouse" / "warehouse.duckdb"
    layout = {"world_root": str(root), "workspace": str(root / "workspace"),
              "warehouse": str(warehouse),
              "scheduler": str(root / "var" / "sched" / "state.json"),
              "profiles_dir": str(root / "build")}
    import duckdb

    connection = duckdb.connect(str(warehouse))
    connection.execute("CREATE OR REPLACE TABLE main.scratch AS SELECT 1 AS n")
    connection.close()
    before = episode.tree_manifest(root)
    connection = duckdb.connect(str(warehouse))
    connection.execute("DELETE FROM main.scratch WHERE n = 1")
    connection.close()
    return (layout, before, episode.tree_manifest(root))


def _second_warehouse(layout: dict, _before: dict, after: dict) -> list[str]:
    """Does an UNPUBLISHED `var/warehouse/*.duckdb` get found and explained?

    `world/world/fingerprint.py` hashes every `.duckdb` beside the primary
    whether or not the manifest lists it, so `declared_warehouses` reads the
    directory as well as the manifest. Without that a world that built two
    files and published one would have the second explained by nothing.
    """
    import duckdb

    legacy = Path(layout["world_root"]) / "var" / "warehouse" / "legacy.duckdb"
    connection = duckdb.connect(str(legacy))
    connection.execute("CREATE TABLE main.old AS SELECT 1 AS n")
    connection.close()
    grown = episode.tree_manifest(Path(layout["world_root"]))
    row = {"seq": 2, "tool": "duckdb.statement", "op_class": "ddl", "exit": "ok",
           "targets": ["main.old"],
           "args": {"sql": "CREATE TABLE main.old AS SELECT 1",
                    "database": str(legacy)}}
    return blindness._unattributed(after, grown, [row], layout)


def _populated_warehouse(tmp: Path) -> str:
    """A real DuckDB file holding a real table holding a real row."""
    import duckdb

    path = tmp / "populated.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE OR REPLACE TABLE main.scratch AS SELECT 1 AS n")
    connection.close()
    return str(path)


def _empty_warehouse(tmp: Path) -> str:
    """A real DuckDB file holding a real table holding no row."""
    import duckdb

    path = tmp / "empty.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("CREATE TABLE main.nothing (n INTEGER)")
    connection.close()
    return str(path)


def _profiles(directory: Path, warehouse: str) -> Path:
    """A dbt profiles.yml pointing one output at `warehouse`."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profiles.yml").write_text(
        "world_dbt:\n  target: dev\n  outputs:\n    dev:\n"
        "      type: duckdb\n"
        f"      path: {json.dumps(warehouse)}\n      threads: 1\n",
        encoding="utf-8")
    return directory


def _git_world_is_live(tmp: Path, endpoints: dict) -> bool:
    """A real `git init` + commit, because `git_log` needs a HEAD and nothing
    short of running git can say whether there is one."""
    import shutil as shutillib
    import subprocess as sp

    git = shutillib.which("git")
    if git is None:
        return True                       # no git on this box; nothing to prove
    tree = tmp / "repo" / "workspace"
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "model.sql").write_text("select 1\n", encoding="utf-8")
    run = lambda *a: sp.run([git, *a], cwd=tree, capture_output=True,  # noqa: E731
                            text=True, encoding="utf-8")
    run("init", "-q")
    before = surface_audit._run_native_check(
        "git", endpoints, {"workspace": str(tree)}, [], 5.0)
    run("config", "user.name", "native test")
    run("config", "user.email", "native@example.invalid")
    run("add", "-A")
    run("commit", "-q", "-m", "seed")
    after = surface_audit._run_native_check(
        "git", endpoints, {"workspace": str(tree)}, [], 5.0)
    # A freshly `git init`ed tree HAS a .git and answers neither git tool, so
    # the check has to ask for a commit and not for the directory.
    return before[:2] == (False, "no_content") and after[0]


def _unmapped_required(task_id: str) -> list[str]:
    """Required surfaces of a real task whose probe maps to no native check.

    Read off the task package, not restated here: the gate mapping is complete
    only if it is complete for the tasks Phase 4 runs.
    """
    import taskspec

    spec = taskspec.load(task_id)
    by_label = {p.get("label") or p["tool"]: p for p in spec.surface_probes}
    tools = {p["tool"] for p in spec.surface_probes}
    out = []
    for name in spec.required_surfaces:
        probe = by_label.get(name)
        if probe is None:
            probe = next((p for p in spec.surface_probes
                          if name in (p.get("covers") or ()) or name in tools),
                         None)
        if probe is None:
            out.append(f"{name}: no probe declares it")
            continue
        surface = surface_audit.native_surface_for(probe["tool"],
                                                   probe.get("args"))
        if surface is None or surface_audit.native_check_kind(surface) == "unmapped":
            out.append(f"{name} ({probe['tool']}) -> {surface}")
    return out


def _refuses_unknown_endpoint(tmp: Path) -> bool:
    bad = tmp / "bad_endpoints.json"
    bad.write_text(json.dumps({"jjira": "http://127.0.0.1:1"}), encoding="utf-8")
    try:
        aut.resolve_endpoints(bad)
    except episode.HarnessError:
        return True
    return False


def _unmapped_is_dead(endpoints: dict, world: dict) -> bool:
    """A required surface native mode cannot health-check must stop the rollout.

    The alternative is a gate that silently passes on the one surface nobody
    wrote a check for, which is the failure mode the whole module exists to
    refuse.
    """
    probes = [{"label": "mystery", "tool": "quantum_flux"}]
    spec = SpecStub(probes, required=["mystery"])
    res = surface_audit.probe_native(probes=probes, endpoints=endpoints,
                                     world=world)
    a = surface_audit.audit(spec=spec, surfaces=res, toolbelt=[])
    return (res["surfaces"]["mystery"]["error_code"] == "native_surface_unmapped"
            and a["gate"] == "fail")


if __name__ == "__main__":
    raise SystemExit(main())
