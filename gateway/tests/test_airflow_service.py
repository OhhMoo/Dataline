"""The Airflow 3 REST API v2 fake over the world ledger (native/services/airflow.py).

    gateway/.venv/bin/python -m pytest gateway/tests/test_airflow_service.py -q

Claims, each checked against Airflow 3.3.2's own OpenAPI document
(testdata/airflow_v2_schemas.json, the subset this service answers with) rather
than against the service's own output:

- AUTH. `POST /auth/token` (JSON or form) and `/auth/token/cli` answer 201 with
  a JWT; every /api/v2 route but version and health answers 401
  `{"detail": "Not authenticated"}` without it; the token's `sub` is the row's
  actor.
- NO CLOCK. A trigger and an applied clear QUEUE runs (state.json `queue`):
  `vnow`, `runs` and the warehouse are what they were; the grader's
  `world.scheduler.advance` executes them, and the API then shows them run,
  with logs.
- LOUD FENCES. Backfills, pausing and clearing across runs are 400s that name
  what is refused; a real Airflow route this does not model is a 404 naming it;
  anything else is Airflow's plain 404.
- ONE ROW PER REQUEST, written before the response, refusals included, in the
  tailer's vocabulary (`airflow.dag_trigger`, `scheduler:<dag_id>`, ...).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import duckdb
import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "world"))

from alkera_gateway import GatewayConfig  # noqa: E402
from alkera_gateway.backends import BACKENDS  # noqa: E402
from alkera_gateway.native import context as native_context  # noqa: E402
from alkera_gateway.native import serve  # noqa: E402
from alkera_gateway.native.services import airflow  # noqa: E402
from world.themes import load_theme  # noqa: E402

from world import scheduler  # noqa: E402

SCHEMAS = json.loads((Path(__file__).parent / "testdata" / "airflow_v2_schemas.json")
                     .read_text("utf-8"))
THEME = REPO / "world" / "world" / "themes" / "commerce_analytics"
USER = "alkera-local-dev"
API = "/api/v2"


def build_world(root: Path, days: int = 3) -> Path:
    """commerce_analytics' dags/ and `days` nights of its ledger history; each job
    runs one SQL statement, so the grader's advance really executes it."""
    manifest = load_theme("commerce_analytics").manifest
    seed = yaml.safe_load((THEME / "seed.example.yaml").read_text("utf-8"))
    seed["days"] = days
    shutil.copytree(THEME / "workspace" / "dags", root / "workspace" / "dags")
    (root / "var" / "sched").mkdir(parents=True)
    scheduler.save(root, scheduler.initial_state(seed, manifest["jobs"]))
    warehouse = root / "var" / "warehouse" / "warehouse.duckdb"
    warehouse.parent.mkdir(parents=True)
    with duckdb.connect(str(warehouse)) as con:
        con.execute("CREATE TABLE ran (job VARCHAR)")
    jobs = {name: {**{k: v for k, v in spec.items() if k != "exec"},
                   "exec": {"kind": "sql",
                            "statements": [f"INSERT INTO ran VALUES ('{name}')"]}}
            for name, spec in manifest["jobs"].items()}
    scheduler.save_live(root, {"vtime_start": seed["vtime_start"], "days_landed": days,
                               "arrival": None, "jobs": jobs})
    return root


class Airflow:
    """One world served by the Airflow app, with its audit rows."""

    def __init__(self, root: Path, audit: Path, audit_file: Path):
        self.root, self.audit_file = root, audit_file
        self.config = GatewayConfig(
            workspace=root / "workspace", warehouse=root / "var" / "warehouse" /
            "warehouse.duckdb", scheduler=root / "var" / "sched" / "state.json",
            ingest=root / "var" / "ingest" / "state.json",
            dashboards=root / "var" / "bi" / "dashboards", audit_dir=audit,
            profiles_dir=root / "build", episode_id=audit.name,
            initial_world_fp="fixture", world_root=root,
            observability=root / "var" / "obs" / "observability.yml",
            workflow_dir=root / "var" / "workflow")
        native_context.set_context(airflow.app, native_context.Context(self.config))
        self.client = TestClient(airflow.app, base_url="http://airflow.internal:8100")
        self.token: str | None = None
        self.requests = 0

    def login(self) -> str:
        r = self.post("/auth/token", json={"username": USER, "password": USER}, auth=False)
        assert r.status_code == 201, r.text
        self.token = r.json()["access_token"]
        return self.token

    def call(self, method: str, path: str, *, auth: bool = True, **kw):
        headers = kw.pop("headers", {})
        if auth and self.token:
            headers.setdefault("Authorization", f"Bearer {self.token}")
        self.requests += 1
        return self.client.request(method, path, headers=headers, **kw)

    def get(self, path: str, **kw):
        return self.call("GET", path, **kw)

    def post(self, path: str, **kw):
        return self.call("POST", path, **kw)

    def ok(self, method: str, path: str, schema: str | None = None, **kw):
        r = self.call(method, path, **kw)
        assert r.status_code == 200, r.text
        if schema:
            conforms(r.json(), schema)
        return r.json()

    def rows(self) -> list[dict]:
        if not self.audit_file.exists():
            return []
        return [json.loads(line) for line in self.audit_file.read_text("utf-8").splitlines()]

    def state(self) -> dict:
        return json.loads((self.root / scheduler.STATE).read_text("utf-8"))


def conforms(document, name: str) -> None:
    """`document` validates against Airflow 3.3.2's schema `name`."""
    schema = {"$schema": SCHEMAS["$schema"], "$defs": SCHEMAS["$defs"],
              "$ref": f"#/$defs/{name}"}
    jsonschema.validate(document, schema,
                        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
    required = SCHEMAS["$defs"][name].get("required", [])
    assert set(required) <= set(document), (name, set(required) - set(document))


@pytest.fixture
def af(tmp_path, monkeypatch):
    root = build_world(tmp_path / "world")
    audit_file = tmp_path / "native-actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit_file))
    monkeypatch.setenv("ALKERA_SCHEDULER", str(root / "var" / "sched" / "state.json"))
    a = Airflow(root, tmp_path / "audit", audit_file)
    a.login()
    return a


def actor(row: dict) -> str:
    """The row's writer-side actor: 9.2.2 has no key for it, so native.audit carries
    it as the flag `actor:<name>` (`""` when the request authenticated nobody)."""
    return next((f.split(":", 1)[1] for f in row["flags"] if f.startswith("actor:")), "")


def latest_run(a: Airflow, dag_id: str = "commerce_nightly") -> dict:
    return a.ok("GET", f"{API}/dags/{dag_id}/dagRuns?order_by=-run_after&limit=1",
                "DAGRunCollectionResponse")["dag_runs"][0]


# ----------------------------------------------------------------- contract
def test_the_module_satisfies_the_registry_contract():
    row = BACKENDS["airflow"]
    assert (airflow.NAME, airflow.HOST, airflow.PORT) == (row.name, row.host, row.port)
    assert row.hosted and not row.tls and row.scheme == "http"
    assert row.env["AIRFLOW_API_URL"] == f"http://{row.host}:{row.port}/api/v2"
    assert serve.serves_health(airflow.app)


def test_served_only_when_a_profile_plugs_it():
    plugged = serve.selected_services("de/laptop", ["orchestrator=airflow"])
    assert "airflow" in plugged
    assert "airflow" not in serve.selected_services("de/laptop", [])
    assert serve.selected_services(None, []) is None           # the unprofiled nine
    assert not serve.sched_enabled("de/laptop", ["orchestrator=airflow"])
    assert serve.hosted_ports()["airflow"] == 8100


# ---------------------------------------------------------------------- auth
def test_token_is_a_jwt_whose_sub_is_the_actor(af):
    import jwt

    claims = jwt.decode(af.token, options={"verify_signature": False})
    assert claims["sub"] == USER and claims["aud"] == "apache-airflow"
    af.ok("GET", f"{API}/dags", "DAGCollectionResponse")
    row = af.rows()[-1]
    assert row["tool"] == "airflow.dag_list" and actor(row) == USER
    # form-encoded works on /auth/token, JSON on /auth/token/cli (airflowctl)
    form = af.post("/auth/token", data={"username": "ops", "password": USER}, auth=False)
    assert form.status_code == 201
    conforms(form.json(), "LoginResponse")
    cli = af.post("/auth/token/cli", json={"username": "ops", "password": USER}, auth=False)
    assert cli.status_code == 201
    af.token = cli.json()["access_token"]
    af.ok("GET", f"{API}/dags")
    assert actor(af.rows()[-1]) == "ops"


def test_refusals_without_or_with_a_bad_token(af):
    token, af.token = af.token, None
    r = af.get(f"{API}/dags")
    assert r.status_code == 401 and r.json() == {"detail": "Not authenticated"}
    assert r.headers["www-authenticate"] == "Bearer"
    conforms(r.json(), "HTTPExceptionResponse")
    r = af.get(f"{API}/dags", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json() == {"detail": "Invalid JWT token"}
    bad = af.post("/auth/token", json={"username": USER, "password": "admin"})
    assert bad.status_code == 401 and bad.json() == {"detail": "Invalid credentials"}
    missing = af.post("/auth/token", json={"username": USER})
    assert missing.status_code == 422
    conforms(missing.json(), "HTTPValidationError")
    # version and health need no token, as in Airflow
    assert af.get(f"{API}/version").json() == {"version": "3.3.2", "git_version": None}
    conforms(af.get(f"{API}/monitor/health").json(), "HealthInfoResponse")
    rows = af.rows()
    refused = [r for r in rows if r["exit"] == "error"]
    assert [r["tool"] for r in refused] == ["airflow.dag_list", "airflow.dag_list",
                                            "airflow.auth_token", "airflow.auth_token"]
    assert all(actor(r) == "" for r in refused)
    af.token = token


# --------------------------------------------------------------- the reads
def test_every_response_is_airflow_33_shaped(af):
    dags = af.ok("GET", f"{API}/dags", "DAGCollectionResponse")
    assert [d["dag_id"] for d in dags["dags"]] == ["bi_refresh", "commerce_nightly"]
    assert dags["total_entries"] == 2
    conforms(af.ok("GET", f"{API}/dags/commerce_nightly"), "DAGResponse")
    conforms(af.ok("GET", f"{API}/dags/commerce_nightly/details"), "DAGDetailsResponse")
    tasks = af.ok("GET", f"{API}/dags/commerce_nightly/tasks", "TaskCollectionResponse")
    assert [t["task_id"] for t in tasks["tasks"]] == ["daily_load", "dbt_build"]
    conforms(af.ok("GET", f"{API}/dags/commerce_nightly/tasks/dbt_build"), "TaskResponse")
    source = af.ok("GET", f"{API}/dagSources/commerce_nightly", "DAGSourceResponse")
    assert source["content"] == (af.root / "workspace" / "dags" /
                                 "commerce_nightly.py").read_text("utf-8")
    plain = af.get(f"{API}/dagSources/commerce_nightly", headers={"Accept": "text/plain"})
    assert plain.text == source["content"]
    runs = af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns", "DAGRunCollectionResponse")
    assert runs["total_entries"] == 3
    run_id = runs["dag_runs"][-1]["dag_run_id"]
    assert run_id.startswith("scheduled__")
    conforms(af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns/{run_id}"), "DAGRunResponse")
    tis = af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns/{run_id}/taskInstances",
                "TaskInstanceCollectionResponse")
    assert [(t["task_id"], t["state"]) for t in tis["task_instances"]] == [
        ("daily_load", "success"), ("dbt_build", "success")]
    conforms(af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns/{run_id}/taskInstances/"
                          "dbt_build"), "TaskInstanceResponse")
    log = af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns/{run_id}/taskInstances/"
                       "dbt_build/logs/1", "TaskInstancesLogResponse")
    assert log["content"][-1]["event"] == "Task instance in success state"
    nd = af.get(f"{API}/dags/commerce_nightly/dagRuns/{run_id}/taskInstances/dbt_build/"
                "logs/1", headers={"Accept": "application/x-ndjson"})
    assert [json.loads(line) for line in nd.text.splitlines()] == log["content"]
    tools = [r["tool"] for r in af.rows()]
    for tool in ("dag_list", "dag_get", "dag_tasks", "dag_source", "dagrun_list",
                 "dagrun_get", "task_list", "task_get", "task_logs"):
        assert f"airflow.{tool}" in tools, tool
    targets = {r["tool"]: r["targets"] for r in af.rows()}
    assert targets["airflow.dag_get"] == ["scheduler:commerce_nightly"]
    assert targets["airflow.dagrun_get"] == [f"scheduler:{run_id}"]


def test_filters_pagination_and_the_tilde(af):
    page = af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns?limit=2&offset=1")
    assert len(page["dag_runs"]) == 2 and page["total_entries"] == 3
    newest = af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns?order_by=-run_after")
    assert newest["dag_runs"][0]["run_after"] > newest["dag_runs"][-1]["run_after"]
    every = af.ok("GET", f"{API}/dags/~/dagRuns", "DAGRunCollectionResponse")
    assert every["total_entries"] == 6
    builds = af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns/~/taskInstances"
                          "?task_id=dbt_build&state=success",
                   "TaskInstanceCollectionResponse")
    assert builds["total_entries"] == 3
    assert af.ok("GET", f"{API}/dags?dag_id_pattern=nightly")["total_entries"] == 1
    assert af.ok("GET", f"{API}/dags?paused=true")["total_entries"] == 0
    bad = af.get(f"{API}/dags?limit=x")
    assert bad.status_code == 422
    conforms(bad.json(), "HTTPValidationError")
    order = af.get(f"{API}/dags/commerce_nightly/dagRuns?order_by=nope")
    assert order.status_code == 400 and "nope" in order.json()["detail"]
    clamp = af.ok("GET", f"{API}/dags/~/dagRuns?limit=500")
    assert len(clamp["dag_runs"]) == 6
    missing = af.get(f"{API}/dags/nope")
    assert missing.status_code == 404 and missing.json() == {
        "detail": "Dag with id nope was not found"}


# ------------------------------------------------------- the writes: no clock
def test_trigger_queues_and_never_advances(af):
    before = af.state()
    run = af.ok("POST", f"{API}/dags/commerce_nightly/dagRuns",
                "DAGRunResponse", json={"logical_date": None, "conf": {"why": "rerun"}})
    after = af.state()
    assert run["state"] == "queued" and run["triggered_by"] == "rest_api"
    assert run["triggering_user_name"] == USER and run["conf"] == {"why": "rerun"}
    assert after["vnow"] == before["vnow"] and after["runs"] == before["runs"]
    assert [e["job"] for e in after["queue"]] == ["daily_load", "dbt_build"]
    with duckdb.connect(str(af.root / "var" / "warehouse" / "warehouse.duckdb"),
                        read_only=True) as con:
        assert con.execute("SELECT count(*) FROM ran").fetchone()[0] == 0
    row = af.rows()[-1]
    assert row["tool"] == "airflow.dag_trigger" and row["op_class"] == "exec"
    assert row["targets"] == ["scheduler:commerce_nightly", "dbt_project"]
    assert actor(row) == USER and row["exit"] == "ok" and row["rows_affected"] == 2
    assert row["result"]["dag_run_id"] == run["dag_run_id"]
    assert row["vts"] == before["vnow"]
    shown = af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns/{run['dag_run_id']}")
    assert shown["state"] == "queued"


def test_double_trigger_gives_two_runs(af):
    one = af.ok("POST", f"{API}/dags/commerce_nightly/dagRuns", json={"logical_date": None})
    two = af.ok("POST", f"{API}/dags/commerce_nightly/dagRuns", json={"logical_date": None})
    assert one["dag_run_id"] != two["dag_run_id"]
    queued = af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns?state=queued")
    assert sorted(r["dag_run_id"] for r in queued["dag_runs"]) == sorted(
        [one["dag_run_id"], two["dag_run_id"]])
    assert len(af.state()["queue"]) == 4 and af.state()["vnow"] == one["run_after"]
    # a chosen id or logical date that exists is Airflow's 409
    clash = af.post(f"{API}/dags/commerce_nightly/dagRuns",
                    json={"logical_date": None, "dag_run_id": one["dag_run_id"]})
    assert clash.status_code == 409
    assert len(af.state()["queue"]) == 4


def test_clear_marks_and_next_advance_executes(af):
    data = af.state()
    index = max(i for i, r in enumerate(data["runs"]) if r["job"] == "dbt_build")
    data["runs"][index].update(status="failed", error="test not_null failed")
    scheduler.save(af.root, data)
    run = latest_run(af)
    assert run["state"] == "failed"
    path = f"{API}/dags/commerce_nightly/clearTaskInstances"
    dry = af.ok("POST", path, "TaskInstanceCollectionResponse",
                json={"dag_run_id": run["dag_run_id"]})          # dry_run defaults true
    assert [t["task_id"] for t in dry["task_instances"]] == ["dbt_build"]
    assert "queue" not in af.state() and af.rows()[-1]["op_class"] == "read"
    done = af.ok("POST", path, "TaskInstanceCollectionResponse",
                 json={"dag_run_id": run["dag_run_id"], "dry_run": False})
    assert [(t["task_id"], t["state"], t["try_number"]) for t in done["task_instances"]] == [
        ("dbt_build", "queued", 2)]
    row = af.rows()[-1]
    assert row["tool"] == "airflow.task_clear" and row["op_class"] == "destructive"
    assert row["targets"] == ["scheduler:commerce_nightly", f"scheduler:{run['dag_run_id']}"]
    assert af.state()["vnow"] == data["vnow"] and af.state()["runs"] == data["runs"]
    scheduler.advance(af.root, 1)                                   # the grader's
    ti = af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns/{run['dag_run_id']}/"
                      "taskInstances/dbt_build", "TaskInstanceResponse")
    assert ti["state"] == "success" and ti["try_number"] == 2
    assert latest_run(af)["state"] == "success"
    old = af.ok("GET", f"{API}/dags/commerce_nightly/dagRuns/{run['dag_run_id']}/"
                       "taskInstances/dbt_build/logs/1")
    assert any("not_null failed" in e["event"] for e in old["content"])


def test_triggered_runs_execute_at_the_graders_advance(af):
    run = af.ok("POST", f"{API}/dags/bi_refresh/dagRuns", json={"logical_date": None})
    scheduler.advance(af.root, 1)
    shown = af.ok("GET", f"{API}/dags/bi_refresh/dagRuns/{run['dag_run_id']}",
                  "DAGRunResponse")
    assert shown["state"] == "success" and shown["start_date"] == run["run_after"]
    executed = af.state()["runs"][-1]
    assert executed["origin"]["dag_run_id"] == run["dag_run_id"]
    assert executed["trigger"] == "manual"


# --------------------------------------------------------------- the fences
def test_backfill_refused_by_name(af):
    before = (af.root / scheduler.STATE).read_bytes()
    for method, path in (("POST", f"{API}/backfills"), ("GET", f"{API}/backfills"),
                         ("POST", f"{API}/backfills/dry_run"),
                         ("PUT", f"{API}/backfills/1/pause")):
        r = af.call(method, path, json={"dag_id": "commerce_nightly",
                                        "from_date": "2026-01-01T00:00:00Z",
                                        "to_date": "2026-02-01T00:00:00Z"})
        assert r.status_code == 400, (method, path)
        assert "Backfills are not available" in r.json()["detail"]
        assert "the clock is the world's" in r.json()["detail"]
        conforms(r.json(), "HTTPExceptionResponse")
    assert (af.root / scheduler.STATE).read_bytes() == before
    rows = [r for r in af.rows() if r["tool"] == "airflow.backfill"]
    assert len(rows) == 4 and all(r["exit"] == "error" for r in rows)


def test_pause_refused_by_name_and_unpause_is_the_state_it_is_in(af):
    r = af.call("PATCH", f"{API}/dags/commerce_nightly", json={"is_paused": True})
    assert r.status_code == 400 and "Pausing a DAG is not modelled" in r.json()["detail"]
    ok = af.ok("PATCH", f"{API}/dags/commerce_nightly", "DAGResponse",
               json={"is_paused": False})
    assert ok["is_paused"] is False
    rows = [r for r in af.rows() if r["tool"] == "airflow.dag_update"]
    assert [r["exit"] for r in rows] == ["error", "ok"]


def test_clear_across_runs_is_refused(af):
    before = (af.root / scheduler.STATE).read_bytes()
    for body in ({"dry_run": False}, {"dry_run": False, "dag_run_id": "x",
                                      "include_past": True},
                 {"start_date": "2026-01-01T00:00:00Z", "dry_run": False}):
        r = af.post(f"{API}/dags/commerce_nightly/clearTaskInstances", json=body)
        assert r.status_code == 400 and "re-running history is a backfill" in \
            r.json()["detail"], body
    assert (af.root / scheduler.STATE).read_bytes() == before


def test_trigger_fences(af):
    path = f"{API}/dags/commerce_nightly/dagRuns"
    missing = af.post(path, json={})
    assert missing.status_code == 422
    assert missing.json()["detail"][0]["loc"] == ["body", "logical_date"]
    extra = af.post(path, json={"logical_date": None, "nope": 1})
    assert extra.status_code == 422 and extra.json()["detail"][0]["type"] == "extra_forbidden"
    later = af.post(path, json={"logical_date": None, "run_after": "2030-01-01T00:00:00Z"})
    assert later.status_code == 400 and "run_after is not modelled" in later.json()["detail"]
    part = af.post(path, json={"logical_date": None, "partition_key": "p"})
    assert part.status_code == 400 and "partition_key" in part.json()["detail"]
    nodag = af.post(f"{API}/dags/nope/dagRuns", json={"logical_date": None})
    assert nodag.status_code == 404
    assert "queue" not in af.state()
    rows = [r for r in af.rows() if r["tool"] == "airflow.dag_trigger"]
    assert len(rows) == 5 and all(r["exit"] == "error" for r in rows)


def test_unknown_and_unmodelled_routes_404_with_a_row(af):
    pools = af.get(f"{API}/pools")
    assert pools.status_code == 404
    assert "GET /api/v2/pools" in pools.json()["detail"]
    assert "does not model" in pools.json()["detail"]
    clear_run = af.post(f"{API}/dags/commerce_nightly/dagRuns/x/clear", json={})
    assert clear_run.status_code == 404 and "dagRuns/{dag_run_id}/clear" in \
        clear_run.json()["detail"]
    typo = af.get(f"{API}/dagz")
    assert typo.status_code == 404 and typo.json() == {"detail": "Not Found"}
    rows = [r for r in af.rows() if r["tool"] == "airflow.unknown_route"]
    assert len(rows) == 3 and all(r["exit"] == "error" for r in rows)
    assert rows[0]["flags"] == ["unmodelled_route"] and rows[2]["flags"] == []
    assert rows[0]["args"]["path"] == "/api/v2/pools"


def test_every_request_writes_exactly_one_row(af):
    af.requests = 0
    start = len(af.rows())
    af.get(f"{API}/dags")
    af.get(f"{API}/dags/nope")
    af.post(f"{API}/dags/commerce_nightly/dagRuns", json={"logical_date": None})
    af.get(f"{API}/version")
    af.get("/nowhere")
    af.call("DELETE", f"{API}/dags/commerce_nightly")
    af.post(f"{API}/backfills", json={})
    af.get("/health")
    malformed = af.post(f"{API}/dags/commerce_nightly/dagRuns", content=b"{nope",
                        headers={"Content-Type": "application/json"})
    assert malformed.status_code == 422
    conforms(malformed.json(), "HTTPValidationError")
    garbled = af.post("/auth/token", content=b"[1", auth=False,
                      headers={"Content-Type": "application/json"})
    assert garbled.status_code == 422
    assert len(af.rows()) - start == af.requests == 10
    assert [r["tool"] for r in af.rows()[-2:]] == ["airflow.dag_trigger", "airflow.auth_token"]
    seqs = [r["seq"] for r in af.rows()]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))


def test_health_counts_the_dags(af):
    body = af.get("/health").json()
    assert body == {"ok": True, "service": "airflow", "items": 2}
