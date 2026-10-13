"""End-to-end check of the Airflow plug in the CONTAINER topology.

    sg docker -c "gateway/.venv/bin/python tasks/harness/airflow_e2e_check.py \\
        --image alkera-episode:head-airflow"

Builds the `commerce_analytics` world (seed 11, the BI001/BI002 seed) for
`de/laptop --plug orchestrator=airflow`, brings up
`container_episode.NativeContainerEpisode` with that profile (native-services
mounts `native/services/airflow.py` at airflow.internal:8100), and from INSIDE the
episode container, as an engineer on the box would:

  (a) discovery: AIRFLOW_API_URL names :8100, `./ops/sched` and SCHED_URL are
      gone, there is no `airflow` CLI and there is `airflowctl`; onboarding and
      services.toml say the same;
  (b) curl: token, version, DAGs, a DAG's source, its runs and task instances,
      a log - each answer equal to what the world's own files hold (read on the
      host);
  (c) requests: the fences (no token, backfill, pause, an unmodelled route), a
      dry-run clear, and two triggers of one DAG plus one of another - QUEUED:
      on the host the ledger then holds exactly those runs in `queue`, `vnow` and
      `runs` unchanged, the warehouse and the engine logs untouched;
  (d) airflowctl: token, login, `version --remote`, `dags list`, `dags get`,
      `dagrun list` - its generated pydantic models validate every answer;
  (e) the grader's `world.scheduler.advance`, on the host: the queue drains, and
      the API (from the episode again) shows the triggered runs run, with logs;
  (f) the audit: one valid 9.2.2 row per request, in the tailer's vocabulary,
      the token's `sub` as the actor; the world diff of the agent's phase is
      explained by its trigger rows (blindness); after teardown
      destinations.jsonl counts airflow.internal:8100 and nothing outside the
      profile's allowlist.

Evidence: tasks/harness/evidence/airflow_e2e.json. Exit 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

PROFILE_NAME = "de/laptop"
PLUGS = {"orchestrator": ("airflow",)}
DEFAULT_IMAGE = "alkera-episode:head-airflow"
EVIDENCE = HERE / "evidence" / "airflow_e2e.json"
API = "http://airflow.internal:8100/api/v2"
VOCABULARY = {"auth_token", "version", "monitor_health", "dag_list", "dag_get",
              "dag_source", "dag_tasks", "dag_update", "dagrun_list", "dagrun_get",
              "dag_trigger", "task_list", "task_get", "task_logs", "task_clear",
              "import_errors", "backfill", "unknown_route", "health", "request"}

ENV = r"""cd /workspace
for v in AIRFLOW_API_URL AIRFLOW_USER SCHED_URL; do echo "$v=${!v-<unset>}"; done
test -e ./ops/sched && echo "ops_sched=present" || echo "ops_sched=absent"
command -v airflow >/dev/null && echo "airflow_cli=present" || echo "airflow_cli=absent"
command -v airflowctl >/dev/null && echo "airflowctl=present" || echo "airflowctl=absent"
grep -n '^| the scheduler' docs/onboarding.md || true
python3 - <<'PY'
import json, os, tomllib
path = os.path.expanduser("~/.config/alkera/services.toml")
with open(path, "rb") as fh:
    toml = tomllib.load(fh)
print("TOML " + json.dumps({k: toml.get(k) for k in ("airflow", "scheduler")}))
PY
"""

CURL = r"""cd /workspace
set -euo pipefail
base="${AIRFLOW_API_URL%/api/v2}"
token=$(curl -sf -X POST "$base/auth/token" -H 'Content-Type: application/json' \
  -d "{\"username\": \"$AIRFLOW_USER\", \"password\": \"$AIRFLOW_PASSWORD\"}" \
  | jq -r .access_token)
h="Authorization: Bearer $token"
version=$(curl -sf "$AIRFLOW_API_URL/version")
dags=$(curl -sf -H "$h" "$AIRFLOW_API_URL/dags" | jq -c '[.dags[].dag_id]')
source_sha=$(curl -sf -H "$h" "$AIRFLOW_API_URL/dagSources/commerce_nightly" \
  | jq -j .content | sha256sum | cut -c1-64)
runs=$(curl -sf -H "$h" "$AIRFLOW_API_URL/dags/commerce_nightly/dagRuns?limit=1" \
  | jq .total_entries)
latest=$(curl -sf -H "$h" \
  "$AIRFLOW_API_URL/dags/commerce_nightly/dagRuns?order_by=-run_after&limit=1" \
  | jq -r '.dag_runs[0].dag_run_id')
tis=$(curl -sf -H "$h" \
  "$AIRFLOW_API_URL/dags/commerce_nightly/dagRuns/$latest/taskInstances" \
  | jq -c '[.task_instances[] | {task_id, state, try_number}]')
every="$AIRFLOW_API_URL/dags/commerce_nightly/dagRuns/~/taskInstances"
builds=$(curl -sf -H "$h" "$every?task_id=dbt_build&state=success&limit=1" \
  | jq .total_entries)
ti="$AIRFLOW_API_URL/dags/commerce_nightly/dagRuns/$latest/taskInstances"
log=$(curl -sf -H "$h" "$ti/dbt_build/logs/1" | jq -c '[.content[].event]')
echo "AF-CURL $(jq -cn --argjson version "$version" --argjson dags "$dags" \
  --arg source_sha "$source_sha" --argjson runs "$runs" --arg latest "$latest" \
  --argjson tis "$tis" --argjson builds "$builds" --argjson log "$log" \
  '{requests: 9, version: $version, dags: $dags, source_sha: $source_sha, runs: $runs,
    latest: $latest, tis: $tis, dbt_build_successes: $builds, log: $log}')"
"""

DRIVE = r"""cd /workspace
python3 - <<'PY'
import json, os
import requests

api = os.environ["AIRFLOW_API_URL"]
base = api[:-len("/api/v2")]
out = {"requests": 0}
s = requests.Session()


def call(method, path, **kw):
    out["requests"] += 1
    r = s.request(method, (base if path.startswith("/auth") else api) + path,
                  timeout=60, **kw)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


out["no_token"] = call("GET", "/dags")
status, body = call("POST", "/auth/token", json={"username": os.environ["AIRFLOW_USER"],
                                                  "password": os.environ["AIRFLOW_PASSWORD"]})
out["token_status"] = status
s.headers["Authorization"] = "Bearer " + body["access_token"]
out["backfill"] = call("POST", "/backfills", json={
    "dag_id": "commerce_nightly", "from_date": "2026-01-01T00:00:00Z",
    "to_date": "2026-01-03T00:00:00Z"})
out["pause"] = call("PATCH", "/dags/commerce_nightly", json={"is_paused": True})
out["unmodelled"] = call("GET", "/pools")
out["typo"] = call("GET", "/dagz")
_, latest = call("GET", "/dags/commerce_nightly/dagRuns?order_by=-run_after&limit=1")
run_id = latest["dag_runs"][0]["dag_run_id"]
out["clear_dry_run"] = call("POST", "/dags/commerce_nightly/clearTaskInstances",
                            json={"dag_run_id": run_id, "only_failed": False})
out["trigger_1"] = call("POST", "/dags/commerce_nightly/dagRuns",
                        json={"logical_date": None, "note": "e2e"})
out["trigger_2"] = call("POST", "/dags/commerce_nightly/dagRuns",
                        json={"logical_date": None})
out["trigger_bi"] = call("POST", "/dags/bi_refresh/dagRuns", json={"logical_date": None})
out["queued"] = call("GET", "/dags/~/dagRuns?state=queued")
print("AF-PY " + json.dumps(out))
PY
"""

CTL = r"""cd /workspace
set -uo pipefail
export AIRFLOW_HOME=/tmp/airflowctl-e2e
base="${AIRFLOW_API_URL%/api/v2}"
token=$(airflowctl auth token --api-url "$base" --username "$AIRFLOW_USER" \
  --password "$AIRFLOW_PASSWORD" | tail -n 1)
echo "AF-CTL-TOKEN ${#token}"
airflowctl auth login --api-url "$base" --api-token "$token" --skip-keyring >/dev/null 2>&1
echo "AF-CTL-LOGIN $?"
export AIRFLOW_CLI_TOKEN="$token"
echo "AF-CTL-VERSION $(airflowctl version --remote 2>&1 | tr '\n' ' ')"
echo "AF-CTL-DAGS $(airflowctl dags list -o json 2>&1 | jq -c '[.[].dag_id]' 2>&1)"
got=$(airflowctl dags get commerce_nightly -o json 2>&1)
echo "AF-CTL-GET $(echo "$got" | jq -c '.' 2>&1)"
runs=$(airflowctl dagrun list --dag-id commerce_nightly --state queued --limit 100 \
  -o json 2>&1)
echo "AF-CTL-RUNS $(echo "$runs" | jq -c '[.[0].dag_runs[] | {dag_run_id, state}]' 2>&1)"
# `dags trigger` with no --logical-date sends the WALL clock: refused by name
wall=$(airflowctl dags trigger commerce_nightly -o json 2>&1 | tr '\n' ' ')
echo "AF-CTL-WALLCLOCK $(jq -cn --arg out "$wall" '$out')"
# without --limit airflowctl 0.1.5 sends `limit=` (empty): Airflow's FastAPI says 422
nolimit=$(airflowctl dagrun list --dag-id commerce_nightly -o json 2>&1 | tr '\n' ' ')
echo "AF-CTL-NOLIMIT $(jq -cn --arg out "$nolimit" '$out')"
"""

AFTER = r"""cd /workspace
python3 - <<'PY'
import json, os
import requests

api = os.environ["AIRFLOW_API_URL"]
base = api[:-len("/api/v2")]
s = requests.Session()
r = s.post(base + "/auth/token", json={"username": os.environ["AIRFLOW_USER"],
                                       "password": os.environ["AIRFLOW_PASSWORD"]})
s.headers["Authorization"] = "Bearer " + r.json()["access_token"]
ids = json.loads(r'''__IDS__''')
out = {"runs": {}, "requests": 1}
for dag_id, run_id in ids:
    run = s.get(f"{api}/dags/{dag_id}/dagRuns/{run_id}", timeout=60).json()
    tis = s.get(f"{api}/dags/{dag_id}/dagRuns/{run_id}/taskInstances", timeout=60).json()
    logs = {}
    for ti in tis["task_instances"]:
        if ti["try_number"]:
            got = s.get(f"{api}/dags/{dag_id}/dagRuns/{run_id}/taskInstances/"
                        f"{ti['task_id']}/logs/{ti['try_number']}", timeout=60).json()
            logs[ti["task_id"]] = [e["event"] for e in got["content"]]
            out["requests"] += 1
    out["requests"] += 2
    out["runs"][run_id] = {"state": run["state"], "start_date": run["start_date"],
                           "end_date": run["end_date"],
                           "tis": {t["task_id"]: t["state"] for t in tis["task_instances"]},
                           "logs": logs}
print("AF-AFTER " + json.dumps(out))
PY
"""


def line(stdout: str, tag: str):
    found = [ln for ln in stdout.splitlines() if ln.startswith(tag + " ")]
    if not found:
        return None
    text = found[-1][len(tag) + 1:]
    try:
        return json.loads(text)
    except ValueError:
        return text


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None) -> int:
    import agent_under_test as aut
    import bi_e2e_check as bi
    import blindness
    import episode
    import tool_tour as tt

    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--work", type=Path, default=Path.home() / "alkera-airflow-e2e")
    parser.add_argument("--out", type=Path, default=EVIDENCE)
    parser.add_argument("--advance-hours", type=float, default=1.0)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO / "world"))
    from world import airflow_view as av
    from world import scheduler

    work = args.work
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    checks: list[dict] = []

    def check(name, ok, detail=None):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (
            f": {json.dumps(detail, default=str)[:600]}" if not ok and detail is not None
            else ""), flush=True)

    record: dict = {"profile": PROFILE_NAME, "plugs": {k: list(v) for k, v in PLUGS.items()},
                    "image": args.image, "revision": bi._git_rev()}
    profile = episode.environment_profile(PROFILE_NAME, PLUGS)
    record["image_gate"] = __import__("container_episode").assert_image_current(args.image)
    seed = tt.world_seed(PROFILE_NAME, PLUGS)
    with tt.as_profile(PROFILE_NAME, PLUGS, seed):
        world = bi.build_world(work)
    episode.check_world_profile(Path(world["world_root"]), profile)
    root = Path(world["world_root"])
    state0 = json.loads((root / scheduler.STATE).read_text("utf-8"))
    host = {
        "vnow": state0["vnow"], "runs": len(state0["runs"]),
        "daily_load_runs": sum(r["job"] == "daily_load" for r in state0["runs"]),
        "dbt_build_successes": sum(r["job"] == "dbt_build" and r["status"] == "success"
                                   for r in state0["runs"]),
        "dags": [d.dag_id for d in av.dags(root)],
        "source_sha": sha(root / "workspace" / "dags" / "commerce_nightly.py"),
        "warehouse_sha": sha(Path(world["warehouse"])),
        "logs": sorted(p.name for p in (root / av.LOGS_DIR).glob("*.log"))
        if (root / av.LOGS_DIR).is_dir() else [],
    }
    record["host"] = host
    topo = tt.TourTopology(world, work, args.image, PROFILE_NAME, PLUGS, seed)
    audit_file = work / "audit" / "actions.jsonl"
    allowed = None
    try:
        record["stack"] = {k: v for k, v in topo.up().items()
                           if k in ("container_id", "image", "image_digest", "services",
                                    "episode_endpoints", "workspace_cli", "subject_env")}
        tree_before = episode.tree_manifest(root)
        clocks = (state0["vnow"],)

        # (a) discovery
        env = topo.run(ENV)
        record["env"] = env.stdout[-3000:]
        lines = dict(ln.split("=", 1) for ln in env.stdout.splitlines()
                     if "=" in ln and not ln.startswith(("|", "TOML", " ")) and ":" not in
                     ln.split("=", 1)[0])
        check("discovery: AIRFLOW_API_URL names airflow.internal:8100",
              lines.get("AIRFLOW_API_URL") == API, lines)
        check("discovery: no SCHED_URL and no ./ops/sched on an airflow box",
              lines.get("SCHED_URL") == "<unset>" and lines.get("ops_sched") == "absent",
              lines)
        check("discovery: no `airflow` CLI, airflowctl present",
              lines.get("airflow_cli") == "absent" and lines.get("airflowctl") == "present",
              lines)
        check("discovery: onboarding names :8100 and says the airflow CLI is absent",
              "airflow.internal:8100" in env.stdout and "no `airflow` CLI" in env.stdout,
              env.stdout[-800:])
        toml = line(env.stdout, "TOML") or {}
        check("discovery: services.toml has [airflow] at 8100 and no [scheduler]",
              (toml.get("airflow") or {}).get("port") == 8100 and toml.get("scheduler") is None,
              toml)

        # (b) curl
        start = len(bi.read_rows(audit_file))
        curl = topo.run(CURL)
        got = line(curl.stdout, "AF-CURL") or {}
        record["curl"] = {"rc": curl.returncode, "result": got, "stderr": curl.stderr[-800:]}
        check("curl: every call answered", curl.returncode == 0 and bool(got),
              curl.stderr[-800:])
        check("curl: /version is Airflow 3.3.2",
              got.get("version") == {"version": "3.3.2", "git_version": None}, got.get("version"))
        check("curl: the DAGs are the workspace's dags/",
              got.get("dags") == host["dags"], [got.get("dags"), host["dags"]])
        check("curl: dagSources equals the DAG file on the host",
              got.get("source_sha") == host["source_sha"])
        check("curl: commerce_nightly's DAG runs = the ledger's daily_load runs",
              got.get("runs") == host["daily_load_runs"], [got.get("runs"),
                                                          host["daily_load_runs"]])
        check("curl: successful dbt_build task instances = the ledger's",
              got.get("dbt_build_successes") == host["dbt_build_successes"],
              [got.get("dbt_build_successes"), host["dbt_build_successes"]])
        check("curl: a task log is served", bool(got.get("log")), got.get("log"))
        curl_rows = bi.read_rows(audit_file)[start:]
        check("audit: one row per curl request",
              len([r for r in curl_rows if r["tool"].startswith("airflow.")])
              == got.get("requests"), [r["tool"] for r in curl_rows])

        # (c) requests: fences, dry run, three triggers
        start = len(bi.read_rows(audit_file))
        drive = topo.run(DRIVE)
        got = line(drive.stdout, "AF-PY") or {}
        record["drive"] = {"rc": drive.returncode, "result": got,
                           "stderr": drive.stderr[-800:]}
        check("requests: the driver ran", drive.returncode == 0 and bool(got),
              drive.stderr[-800:])
        check("fence: no token is 401 Not authenticated",
              got.get("no_token") == [401, {"detail": "Not authenticated"}], got.get("no_token"))
        backfill = got.get("backfill") or [None, {}]
        check("fence: a backfill is 400 and names it",
              backfill[0] == 400 and "Backfills are not available" in str(backfill[1])
              and "the clock is the world's" in str(backfill[1]), backfill)
        pause = got.get("pause") or [None, {}]
        check("fence: pausing is 400 and names it",
              pause[0] == 400 and "Pausing a DAG is not modelled" in str(pause[1]), pause)
        unmodelled = got.get("unmodelled") or [None, {}]
        check("fence: a real route this does not model is 404 naming it",
              unmodelled[0] == 404 and "/api/v2/pools" in str(unmodelled[1]), unmodelled)
        check("fence: an unknown route is Airflow's 404",
              got.get("typo") == [404, {"detail": "Not Found"}], got.get("typo"))
        dry = got.get("clear_dry_run") or [None, {}]
        check("clear: dry run (the default) lists the task instances",
              dry[0] == 200 and len((dry[1] or {}).get("task_instances", [])) == 2, dry)
        triggers = [got.get(k) or [None, {}] for k in ("trigger_1", "trigger_2", "trigger_bi")]
        check("trigger: three triggers answer 200 queued",
              all(t[0] == 200 and t[1].get("state") == "queued" for t in triggers),
              [t[0] for t in triggers])
        ids = [(t[1].get("dag_id"), t[1].get("dag_run_id")) for t in triggers]
        check("trigger: the same DAG twice is two runs", ids[0] != ids[1] and None not in
              (ids[0][1], ids[1][1]), ids)
        queued = (got.get("queued") or [None, {}])[1] or {}
        check("trigger: the API lists exactly the three queued runs",
              sorted(r["dag_run_id"] for r in queued.get("dag_runs", []))
              == sorted(i[1] for i in ids), queued.get("total_entries"))
        drive_rows = bi.read_rows(audit_file)[start:]
        check("audit: one row per requests call",
              len([r for r in drive_rows if r["tool"].startswith("airflow.")])
              == got.get("requests"), [r["tool"] for r in drive_rows])

        # the ledger after the agent's phase: queued, no clock, nothing executed
        state1 = json.loads((root / scheduler.STATE).read_text("utf-8"))
        queue = state1.get("queue") or []
        check("ledger: vnow unchanged and no run added",
              state1["vnow"] == state0["vnow"] and state1["runs"] == state0["runs"],
              {"vnow": state1["vnow"], "runs": len(state1["runs"])})
        check("ledger: the queue holds exactly the requested runs, in order",
              [(e["origin"]["dag_run_id"], e["job"]) for e in queue] == [
                  (ids[0][1], "daily_load"), (ids[0][1], "dbt_build"),
                  (ids[1][1], "daily_load"), (ids[1][1], "dbt_build"),
                  (ids[2][1], "bi_refresh")], [(e["job"]) for e in queue])
        check("ledger: nothing executed (warehouse and engine logs untouched)",
              sha(Path(world["warehouse"])) == host["warehouse_sha"]
              and (sorted(p.name for p in (root / av.LOGS_DIR).glob("*.log"))
                   if (root / av.LOGS_DIR).is_dir() else []) == host["logs"])

        # (d) airflowctl
        start = len(bi.read_rows(audit_file))
        ctl = topo.run(CTL)
        record["airflowctl"] = {"rc": ctl.returncode, "stdout": ctl.stdout[-4000:],
                                "stderr": ctl.stderr[-1500:]}
        check("airflowctl: a token from /auth/token/cli",
              (line(ctl.stdout, "AF-CTL-TOKEN") or 0) > 20, ctl.stdout[-600:])
        check("airflowctl: login --skip-keyring", line(ctl.stdout, "AF-CTL-LOGIN") == 0,
              ctl.stdout[-600:])
        check("airflowctl: version --remote reports 3.3.2",
              "3.3.2" in str(line(ctl.stdout, "AF-CTL-VERSION")),
              line(ctl.stdout, "AF-CTL-VERSION"))
        check("airflowctl: dags list validates and lists both DAGs",
              line(ctl.stdout, "AF-CTL-DAGS") == host["dags"], line(ctl.stdout, "AF-CTL-DAGS"))
        dag_get = line(ctl.stdout, "AF-CTL-GET")
        dag_get = dag_get[0] if isinstance(dag_get, list) and dag_get else dag_get
        check("airflowctl: dags get validates (Airflow 3.3 DAGResponse)",
              isinstance(dag_get, dict) and dag_get.get("timetable_summary") == "30 2 * * *",
              dag_get)
        ctl_runs = line(ctl.stdout, "AF-CTL-RUNS")
        check("airflowctl: dagrun list validates and shows the queued runs",
              isinstance(ctl_runs, list) and sorted(r["dag_run_id"] for r in ctl_runs)
              == sorted([ids[0][1], ids[1][1]]), ctl_runs)
        wall = str(line(ctl.stdout, "AF-CTL-WALLCLOCK"))
        check("airflowctl: `dags trigger` with the wall clock as logical date is refused",
              "after the world's clock" in wall and "Client error" in wall, wall[-600:])
        record["airflowctl"]["no_limit"] = line(ctl.stdout, "AF-CTL-NOLIMIT")
        ctl_rows = [r for r in bi.read_rows(audit_file)[start:]
                    if r["tool"].startswith("airflow.")]
        record["airflowctl"]["tools"] = [r["tool"] for r in ctl_rows]

        # blindness: the agent's phase changed only what its rows explain
        tree_after = episode.tree_manifest(root)
        rows = bi.read_rows(audit_file)
        changed = sorted(k for k in set(tree_before) | set(tree_after)
                         if tree_before.get(k) != tree_after.get(k))
        layout = {**world, "world_root": str(root)}
        unexplained = blindness._unattributed(tree_before, tree_after, rows, layout,
                                              blindness.container_path_map(world))
        record["world_changed"] = changed
        check("blindness: the agent phase changed only the ledger",
              changed == ["var/sched/state.json"], changed)
        check("blindness: the change is attributed to the trigger rows (none unexplained)",
              unexplained == [], unexplained)
        reads_only = [r for r in rows if r["tool"] not in blindness.SCHEDULER_TOOLS]
        check("blindness: without the trigger rows the same change is off-channel",
              blindness._unattributed(tree_before, tree_after, reads_only, layout,
                                      blindness.container_path_map(world))
              == ["var/sched/state.json"])

        # (e) the grader's advance, on the host, where graders run it
        started = time.monotonic()
        advanced = scheduler.advance(root, args.advance_hours)
        record["advance"] = {"hours": args.advance_hours,
                             "seconds": round(time.monotonic() - started, 1),
                             "events": advanced["events"][:12]}
        state2 = json.loads((root / scheduler.STATE).read_text("utf-8"))
        drained = state2["runs"][len(state0["runs"]):len(state0["runs"]) + 5]
        check("advance: the queue drains first, in order, and the key is gone",
              "queue" not in state2 and [r["job"] for r in drained] ==
              ["daily_load", "dbt_build", "daily_load", "dbt_build", "bi_refresh"]
              and drained[0]["vts_start"] == state0["vnow"],
              [(r["job"], r["status"], r["vts_start"]) for r in drained])
        after = topo.run(AFTER.replace("__IDS__", json.dumps(ids)))
        got = line(after.stdout, "AF-AFTER") or {}
        record["after"] = {"rc": after.returncode, "result": got,
                           "stderr": after.stderr[-800:]}
        by_run = {r["origin"]["dag_run_id"]: r for r in drained}
        expect = {}
        for r in drained:
            expect.setdefault(r["origin"]["dag_run_id"], {})[r["job"]] = r["status"]
        shown = {run_id: v["tis"] for run_id, v in (got.get("runs") or {}).items()}
        check("after advance: each triggered run's task states equal the ledger's",
              shown == expect, {"api": shown, "ledger": expect})
        check("after advance: no triggered run is still queued",
              all(v["state"] != "queued" for v in (got.get("runs") or {}).values())
              and len(got.get("runs") or {}) == 3, got.get("runs"))
        build_log = ((got.get("runs") or {}).get(ids[0][1]) or {}).get("logs", {}).get(
            "dbt_build", [])
        # The ledger's record of the run, and the engine's own output when the
        # executor kept one (a quiet dbt success prints nothing, so it keeps none).
        raw = root / av.LOGS_DIR / f"run-{len(state0['runs']) + 2:06d}.log"
        built = drained[1]
        expected = [line_ for line_ in (raw.read_text("utf-8").splitlines()
                                         if raw.is_file() else [])]
        check("after advance: dbt_build's log is its ledger record (+ any engine output)",
              len(build_log) >= 3 and build_log[1:1 + len(expected)] == expected
              and str(built["detail"] or built["action"]) in build_log
              and build_log[-1] == f"Task instance in {built['status']} state",
              {"raw": raw.is_file(), "events": build_log[:6]})
        record["drained"] = [{k: r.get(k) for k in ("job", "status", "vts_start", "vts_end",
                                                    "trigger", "queued_at", "error")}
                             for r in by_run.values()]

        # (f) the audit
        rows = bi.read_rows(audit_file)
        airflow_rows = [r for r in rows if r["tool"].startswith("airflow.")]
        clocks = (state0["vnow"], state2["vnow"])
        bad = {r["seq"]: p for r in airflow_rows if (p := bi.validate_row(r, vnow=clocks))}
        check("audit: every airflow row is a valid 9.2.2 row", not bad, bad)
        names = {r["tool"].split(".", 1)[1] for r in airflow_rows}
        check("audit: tool names are the tailer's vocabulary", names <= VOCABULARY,
              sorted(names - VOCABULARY))
        trig = [r for r in airflow_rows if r["tool"] == "airflow.dag_trigger"]
        # three accepted (requests), one refused (airflowctl's wall-clock date)
        check("audit: the trigger rows are exec, on scheduler:<dag>, with the token's sub",
              [r["exit"] for r in trig] == ["ok", "ok", "ok", "error"]
              and all(r["op_class"] == "exec" and
                                     r["targets"][0].startswith("scheduler:") and
                                     "actor:alkera-local-dev" in r["flags"] for r in trig),
              [(r["op_class"], r["targets"], r["flags"]) for r in trig])
        seqs = [r["seq"] for r in rows]
        check("audit: seq is contiguous across writers", seqs == list(range(1, len(seqs) + 1)),
              seqs[-5:])
        record["rows"] = {"total": len(rows), "airflow": len(airflow_rows),
                          "by_tool": {t: sum(r["tool"] == t for r in airflow_rows)
                                      for t in sorted({r["tool"] for r in airflow_rows})}}
        allowed = aut.allowed_destinations(topo.stack.episode_endpoints())
        record["allowed_destinations"] = sorted(f"{h}:{p}" for h, p in allowed)
    finally:
        topo.down()
    dest = tt.read_destinations(work / "audit") or []
    record["destinations"] = dest
    hit = [d for d in dest if (d.get("host"), d.get("port")) == ("airflow.internal", 8100)]
    check("destinations: airflow.internal:8100 counted after teardown",
          bool(hit) and hit[0]["count"] > 0, hit)
    violations = [str(v) for v in blindness.network_violations(
        dest, allowed, aut.normalise_host)]
    check("destinations: nothing outside the profile's allowlist", not violations, violations)
    check("destinations: sched.internal is not served on an airflow box",
          not [d for d in dest if d.get("host") == "sched.internal"])
    record["checks"] = checks
    passed = sum(c["result"] == "PASS" for c in checks)
    record["result"] = "PASS" if passed == len(checks) else "FAIL"
    record["headline"] = f"{record['result']} {passed}/{len(checks)}"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=1, sort_keys=True, default=str) + "\n",
                        encoding="utf-8")
    print(f"{record['headline']} -> {args.out}")
    return 0 if record["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
