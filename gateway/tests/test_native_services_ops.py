"""The ops-plane fake services: vendor shapes, and rows that match the MCP rows.

TWO CLAIMS ARE UNDER TEST HERE, AND ONLY THE SECOND ONE IS HARD.

1. Each service answers in its vendor's shape over a real ASGI transport, and
   every value it serves traces to the world (a Metabase card's rows are the
   rows `sql_query` returns for the same SQL; a Prometheus sample is the value
   `metrics_get` reports).

2. EQUIVALENCE. The same action, done through the MCP gateway and through the
   native service, produces the same `op_class`, `targets` and `read_targets`.
   This is measured, not asserted: `equivalence` clones the seeded world twice,
   drives one clone through `Gateway.call` and the other through HTTP, and
   diffs the two rows. A clone per case, because reading a metric writes the
   observation record and the two worlds would otherwise drift apart.

WHAT IS NOT EXECUTED HERE. `promtool` and `logcli` are not on PATH in this
environment, so the "a real client library works against it" half of the
NATIVE.md rule is *designed* for those two, not measured. The HTTP client
that is used is `httpx` over `ASGITransport`, which is the same client the real
Metabase/Airbyte SDKs use underneath.
"""

import asyncio
import json

import httpx
import pytest
from conftest import clone_world, config
from test_observability import DEFINITIONS, seed_observability

from alkera_gateway import Gateway
from alkera_gateway.native import audit as native_audit
from alkera_gateway.native import context as native_context
from alkera_gateway.native.services import (
    airbyte,
    alertmanager,
    deploy,
    deploy_cli,
    loki,
    metabase,
    prometheus,
)
from alkera_gateway.observability import parse_vts
from alkera_gateway.records import Observation, digest

SERVICES = (metabase, prometheus, alertmanager, loki, airbyte, deploy)


class Native:
    """One world, served by every ops-plane app in this process."""

    def __init__(self, root, audit_root, audit_file):
        self.root = root
        self.audit_root = audit_root
        self.context = native_context.Context(config(root, audit_root))
        self.audit_file = audit_file
        for service in SERVICES:
            native_context.set_context(service.app, self.context)

    def restarted(self) -> "Native":
        """The same world, served by freshly built apps.

        Every service module is a singleton `app`, so pointing them at a new
        `Context` over the same tree is exactly what an operator restarting the
        stack mid-episode gets: no in-process state survives, because after this
        deliverable there is none to survive. What comes back out of a read
        afterwards came out of the world.
        """
        return Native(self.root, self.audit_root, self.audit_file)

    def request(self, app, method, path, **kwargs) -> httpx.Response:
        async def call():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://service.internal"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(call())

    def get(self, app, path, **kwargs):
        return self.request(app, "GET", path, **kwargs)

    def post(self, app, path, **kwargs):
        return self.request(app, "POST", path, **kwargs)

    def ok(self, response: httpx.Response):
        assert response.status_code < 400, response.text
        return response.json()

    def rows(self, tool: str | None = None) -> list[dict]:
        if not self.audit_file.exists():
            return []
        rows = [json.loads(line) for line in self.audit_file.read_text("utf-8").splitlines()]
        return [row for row in rows if tool is None or row["tool"] == tool]


@pytest.fixture
def seeded(world):
    """The observability world, plus the dashboards and connectors conftest seeds."""
    return seed_observability(world)


@pytest.fixture
def native(seeded, tmp_path, monkeypatch):
    audit_file = tmp_path / "native-actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit_file))
    # Nothing to reset between tests any more: a silence lives in the world tree
    # and a deployment status in the observation record, and each test gets its
    # own clone of both.
    return Native(clone_world(seeded, tmp_path / "native-world"), tmp_path / "native-audit",
                  audit_file)


@pytest.fixture
def equivalence(seeded, tmp_path, monkeypatch):
    """-> a factory that hands out a fresh (gateway, native) pair on one world.

    Each pair gets its own clone so that a case's writes - including the
    observation record every metric read appends to - cannot leak into the next
    case and make two identical actions look different.
    """
    made = []

    def pair():
        index = len(made)
        audit_file = tmp_path / f"native-{index}.jsonl"
        monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit_file))
        gateway = Gateway(
            config(clone_world(seeded, tmp_path / f"mcp-world-{index}"), tmp_path / f"mcp-{index}")
        )
        native = Native(
            clone_world(seeded, tmp_path / f"native-world-{index}"),
            tmp_path / f"native-audit-{index}",
            audit_file,
        )
        made.append(gateway)
        return gateway, native

    yield pair
    for gateway in made:
        gateway.close()


def mcp_row(gateway: Gateway, tool: str, args: dict) -> dict:
    result = gateway.call(tool, args)
    assert result["ok"], result["error"]
    rows = [
        json.loads(line)
        for line in (gateway.audit.directory / "actions.jsonl").read_text("utf-8").splitlines()
    ]
    return rows[-1]


# --------------------------------------------------------------------------
# The registry contract
# --------------------------------------------------------------------------


def test_every_service_satisfies_the_registry_contract():
    """`services/__init__.py` mounts on NAME/HOST/PORT/app; a port collision
    would make two of them unreachable in the episode network."""
    ports = {}
    for service in SERVICES:
        assert service.NAME and service.HOST.endswith(".internal")
        assert isinstance(service.PORT, int)
        assert service.app is not None
        assert service.PORT not in ports, f"{service.NAME} shares a port with {ports[service.PORT]}"
        ports[service.PORT] = service.NAME


def test_no_service_carries_a_realistic_credential():
    """Repo rule: a credential in the workspace is obviously fake."""
    assert native_context.DEV_TOKEN == "alkera-local-dev"
    assert metabase.SESSION_ID.startswith("alkera-local-dev")


@pytest.mark.parametrize(
    "service, expected",
    [
        (metabase, 1),                              # conftest seeds one dashboard
        (prometheus, len(DEFINITIONS["metrics"])),
        (alertmanager, len(DEFINITIONS["alarms"])),
        (loki, 4),                                  # four seeded runs, each with a line
        (airbyte, 2),                               # acme_orders and globex_returns
        (deploy, len(DEFINITIONS["deploys"])),
    ],
    ids=lambda value: getattr(value, "NAME", str(value)),
)
def test_health_reports_the_fixtures_true_count(native, service, expected):
    """The surface gate reads `items`; a constant here would let a service that
    is wired to nothing pass it, so every count is checked against the world."""
    body = native.ok(native.get(service.app, "/health"))
    assert body == {"ok": True, "service": service.NAME, "items": expected}
    row = native.rows(f"{service.NAME}.health")[0]
    assert row["targets"] == [f"health:{service.NAME}"]
    assert row["read_targets"], "a health probe that read nothing counted nothing"


def test_health_says_so_when_the_world_has_no_such_surface(world, tmp_path, monkeypatch):
    """An unseeded world has no observability definitions. `items: 0` with
    `ok: true` would read as "wired, and empty"; it answers 503 instead."""
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(tmp_path / "bare.jsonl"))
    bare = Native(clone_world(world, tmp_path / "bare-world"), tmp_path / "bare-audit",
                  tmp_path / "bare.jsonl")
    response = bare.get(prometheus.app, "/health")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "service": "prometheus", "items": 0,
                               "error": "no_observability"}


# --------------------------------------------------------------------------
# Metabase
# --------------------------------------------------------------------------


def test_metabase_session_refuses_the_wrong_password(native):
    assert native.post(metabase.app, "/api/session",
                       json={"username": "analyst", "password": "nope"}).status_code == 401
    body = native.ok(native.post(
        metabase.app, "/api/session",
        json={"username": "analyst", "password": native_context.DEV_TOKEN}))
    assert body == {"id": metabase.SESSION_ID}
    assert native.rows("metabase.session_create")[0]["targets"] == ["metabase:session"]


def test_metabase_serves_dashboards_and_cards_in_metabase_shape(native):
    dashboards = native.ok(native.get(metabase.app, "/api/dashboard"))
    assert [item["name"] for item in dashboards] == ["item_totals"]
    card = dashboards[0]["dashcards"][0]["card"]
    assert card["dataset_query"]["type"] == "native"
    assert card["dataset_query"]["native"]["query"].startswith("select count(*)")
    by_name = native.ok(native.get(metabase.app, "/api/dashboard/item_totals"))
    assert by_name["name"] == "item_totals"
    assert native.ok(native.get(metabase.app, f"/api/card/{card['id']}")) == card


def test_metabase_card_query_returns_the_rows_the_warehouse_holds(native):
    """The load-bearing BI assertion: the card's rows are the warehouse's rows."""
    card = native.ok(native.get(metabase.app, "/api/card"))[0]
    body = native.ok(native.post(metabase.app, f"/api/card/{card['id']}/query", json={}))
    assert [column["name"] for column in body["data"]["cols"]] == ["items", "amount"]
    assert body["data"]["rows"] == [[2, {"type": "decimal", "value": "30.75"}]]
    assert body["row_count"] == 1


def test_metabase_cache_only_says_not_refreshed_until_a_refresh_happened(native):
    card = native.ok(native.get(metabase.app, "/api/card"))[0]
    path = f"/api/card/{card['id']}/query"
    refused = native.post(metabase.app, path, json={"cache_only": True})
    assert refused.status_code == 404
    assert refused.json()["error_code"] == "not_refreshed"
    refreshed = native.ok(native.post(metabase.app, path, json={"ignore_cache": True}))
    cached = native.ok(native.post(metabase.app, path, json={"cache_only": True}))
    assert cached["data"]["rows"] == refreshed["data"]["rows"]
    assert native.rows("metabase.bi_refresh")[0]["op_class"] == "write"


# --------------------------------------------------------------------------
# Prometheus
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, why",
    [
        ('orders_rows{kind=~"row.*"}', "regex matchers"),
        ("sum(orders_rows) by (kind)", "`by` grouping"),
        ("avg(orders_rows)", "not supported"),
        ("rate(orders_rows)", "range selector"),
        ("orders_rows[5m]", "only supported inside rate"),
        ("orders_rows offset 5m", "`offset`"),
    ],
)
def test_prometheus_refuses_what_it_cannot_parse_by_name(native, query, why):
    """A query outside the subset is a refusal that names the construct, never a
    silently different answer."""
    response = native.get(prometheus.app, "/api/v1/query", params={"query": query})
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error" and body["errorType"] == "bad_data"
    assert why in body["error"]


def test_prometheus_instant_query_reports_the_metric_value(native):
    body = native.ok(native.get(prometheus.app, "/api/v1/query",
                                params={"query": "orders_rows"}))
    assert body["data"]["resultType"] == "vector"
    series = body["data"]["result"][0]
    assert series["metric"] == {"__name__": "orders_rows", "kind": "row_count", "unit": "rows"}
    assert float(series["value"][1]) == 20.0


def test_prometheus_matchers_and_sum_select_and_aggregate(native):
    empty = native.ok(native.get(prometheus.app, "/api/v1/query",
                                 params={"query": 'orders_rows{kind="freshness"}'}))
    assert empty["data"]["result"] == []
    both = native.ok(native.get(prometheus.app, "/api/v1/query",
                                params={"query": 'sum({kind="row_count"})'}))
    # warehouse.orders has 20 rows and warehouse.vendor_feed does not exist, so
    # the sum is over the one series that could be read and the other is named.
    assert float(both["data"]["result"][0]["value"][1]) == 20.0
    assert any("vendor_rows" in warning for warning in both["warnings"])


def test_prometheus_never_reports_zero_for_an_unreadable_metric(native):
    """`vendor_rows` points at a table that does not exist. Prometheus returns
    no series and says why; 0.0 would be the silent-wrong answer."""
    body = native.ok(native.get(prometheus.app, "/api/v1/query",
                                params={"query": "vendor_rows"}))
    assert body["data"]["result"] == []
    assert body["warnings"] and "vendor_rows" in body["warnings"][0]


def test_prometheus_range_query_returns_the_recorded_series(native):
    body = native.ok(native.get(prometheus.app, "/api/v1/query_range",
                                params={"query": "orders_rows", "step": "1d"}))
    assert body["data"]["resultType"] == "matrix"
    values = body["data"]["result"][0]["values"]
    assert [float(value[1]) for value in values] == [8.0, 8.0, 2.0, 2.0]
    assert "step= is not applied" in body["warnings"][0]


def test_prometheus_rate_is_the_first_to_last_slope(native):
    body = native.ok(native.get(prometheus.app, "/api/v1/query",
                                params={"query": "rate(orders_rows[30d])"}))
    value = float(body["data"]["result"][0]["value"][1])
    # 8 rows on 2026-03-01 to 2 rows on 2026-03-04: -6 over three days.
    assert value == pytest.approx(-6.0 / (3 * 86400))


def test_prometheus_series_and_label_values_list_the_definitions(native):
    names = native.ok(native.get(prometheus.app, "/api/v1/label/__name__/values"))
    assert "orders_freshness" in names["data"]
    kinds = native.ok(native.get(prometheus.app, "/api/v1/label/kind/values"))
    assert "job_duration" in kinds["data"]
    found = native.ok(native.get(prometheus.app, "/api/v1/series",
                                 params={"match[]": '{kind="job_duration"}'}))
    assert found["data"] == [{"__name__": "daily_load_duration", "kind": "job_duration",
                              "unit": "seconds", "job": "daily_load"}]


def test_prometheus_rules_keep_insufficient_data_distinguishable(native):
    """The whole reason rules are served here: an alarm that could not be
    evaluated must not read as an alarm that is fine."""
    rules = native.ok(native.get(prometheus.app, "/api/v1/rules"))["data"]["groups"][0]["rules"]
    by_name = {rule["name"]: rule for rule in rules}
    assert by_name["orders_stale"]["state"] == "firing"
    unknown = [rule for rule in rules if rule["health"] == "unknown"]
    assert unknown, "this world has an alarm that cannot be evaluated"
    assert all(rule["lastError"] for rule in unknown)


# --------------------------------------------------------------------------
# Alertmanager
# --------------------------------------------------------------------------


def test_alertmanager_holds_firing_alarms_only(native):
    alerts = native.ok(native.get(alertmanager.app, "/api/v2/alerts"))
    names = {alert["labels"]["alertname"] for alert in alerts}
    assert "orders_stale" in names
    listed = native.context.operations.observability.list_alarms(None, Observation())
    states = {alarm["alarm"]: alarm["state"] for alarm in listed["alarms"]}
    assert names == {name for name, state in states.items() if state == "firing"}
    assert all(alert["status"]["state"] == "active" for alert in alerts)


def test_alertmanager_groups_by_severity(native):
    groups = native.ok(native.get(alertmanager.app, "/api/v2/alerts/groups"))
    assert [group["labels"]["severity"] for group in groups] == sorted(
        {group["labels"]["severity"] for group in groups}
    )
    assert all(group["alerts"] for group in groups)


def test_alertmanager_silence_suppresses_and_is_audited_against_the_alarm(native):
    silenced = native.ok(native.post(alertmanager.app, "/api/v2/silences", json={
        "matchers": [{"name": "alertname", "value": "orders_stale"}],
        "comment": "investigating", "createdBy": "agent",
    }))
    # One row for the write, named after the operation `operations.py` now
    # serves, and pointed at the alarm rather than at a matcher expression.
    row = native.rows("alertmanager.alarms_ack")[0]
    assert row["op_class"] == "write" and row["targets"] == ["alarm:orders_stale"]
    alerts = native.ok(native.get(alertmanager.app, "/api/v2/alerts"))
    stale = next(a for a in alerts if a["labels"]["alertname"] == "orders_stale")
    assert stale["status"]["state"] == "suppressed"
    assert stale["status"]["silencedBy"] == [silenced["silenceID"]]
    active_only = native.ok(native.get(alertmanager.app, "/api/v2/alerts",
                                       params={"silenced": "false"}))
    assert "orders_stale" not in {a["labels"]["alertname"] for a in active_only}


def world_files(root) -> dict[str, str]:
    """Every file in the world tree, by digest - the shape a fingerprint reads.

    `world/world/fingerprint.py` hashes `var/obs` as a whole directory, so a
    file appearing there is a change the grader's world diff sees.
    """
    return {str(path.relative_to(root)).replace("\\", "/"): digest(path.read_bytes())
            for path in sorted(root.rglob("*")) if path.is_file()}


def test_a_silence_is_world_state_that_outlives_the_service(native):
    """The ops plane's weakest finding, measured closed.

    A silence used to live in a dict in the service process: audited, invisible
    to the world, gone on restart. Three things are checked here and none of
    them is `ok` - the world tree changed, the change is the file the
    fingerprint hashes, and a *restarted* stack still reports the alarm
    suppressed because it read the world rather than remembering anything.
    """
    before = world_files(native.root)
    silenced = native.ok(native.post(alertmanager.app, "/api/v2/silences", json={
        "matchers": [{"name": "alertname", "value": "orders_stale"}],
        "endsAt": "2026-03-06T09:00:00Z", "comment": "backfill running",
        "createdBy": "agent",
    }))
    after = world_files(native.root)
    assert set(after) - set(before) == {"var/obs/silences.json"}
    assert {name for name in before if after.get(name) != before[name]} == set()

    restarted = native.restarted()
    listed = restarted.ok(restarted.get(alertmanager.app, "/api/v2/silences"))
    assert [entry["id"] for entry in listed] == [silenced["silenceID"]]
    assert listed[0]["comment"] == "backfill running"
    assert listed[0]["status"]["state"] == "active"
    assert listed[0]["matchers"] == [
        {"name": "alertname", "value": "orders_stale", "isRegex": False, "isEqual": True}]
    alerts = restarted.ok(restarted.get(alertmanager.app, "/api/v2/alerts"))
    stale = next(a for a in alerts if a["labels"]["alertname"] == "orders_stale")
    assert stale["status"]["state"] == "suppressed"

    # And the row that names the change: a world diff can attribute the new file
    # to an audited action rather than calling it unexplained.
    row = restarted.rows("alertmanager.alarms_ack")[0]
    assert row["targets"] == ["alarm:orders_stale"] and row["op_class"] == "write"

    expired = restarted.request(alertmanager.app, "DELETE",
                                f"/api/v2/silence/{silenced['silenceID']}")
    assert expired.status_code == 200
    again = native.restarted().ok(
        native.get(alertmanager.app, "/api/v2/silences"))
    assert again[0]["status"]["state"] == "expired"


def test_alertmanager_refuses_a_silence_that_selects_no_declared_alarm(native):
    """A matcher that matches nothing is a 404, not a silence covering nothing."""
    response = native.post(alertmanager.app, "/api/v2/silences", json={
        "matchers": [{"name": "alertname", "value": "no_such_alarm"}]})
    assert response.status_code == 404
    assert native.rows("alertmanager.alarms_ack")[0]["exit"] == "error"
    assert not (native.root / "var" / "obs" / "silences.json").exists()


def test_alertmanager_refuses_a_regex_matcher(native):
    response = native.post(alertmanager.app, "/api/v2/silences", json={
        "matchers": [{"name": "alertname", "value": "orders.*", "isRegex": True}]})
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Loki
# --------------------------------------------------------------------------


def test_loki_selects_one_run_and_stamps_its_lines(native):
    body = native.ok(native.get(loki.app, "/loki/api/v1/query_range",
                                params={"query": '{run_id="run-000003"}'}))
    stream = body["data"]["result"][0]
    assert stream["stream"]["job"] == "daily_load"
    assert stream["stream"]["source"] == "scheduler_state"
    at, text = stream["values"][0]
    assert "connection reset by peer" in text
    # The stamp is the run's start plus the line number, in nanoseconds - the
    # run started at 01:00 on 2026-03-04 and this is its first line.
    assert int(at) == int(parse_vts("2026-03-04T01:00:00Z").timestamp()) * 1_000_000_000 + 1


def test_loki_line_filter_finds_the_failure_across_runs(native):
    body = native.ok(native.get(loki.app, "/loki/api/v1/query_range",
                                params={"query": '{job="daily_load"} |= "exited 1"'}))
    lines = [value[1] for stream in body["data"]["result"] for value in stream["values"]]
    assert lines == ["dt=2026-03-03 loader exited 1: connection reset by peer"]


def test_loki_unfiltered_selector_reads_the_runs_rather_than_searching(native):
    """`logs_search` needs a non-empty query in the served MCP schema, so an
    unfiltered stream is `logs_get` per run - the same calls an MCP agent makes."""
    body = native.ok(native.get(loki.app, "/loki/api/v1/query_range",
                                params={"query": '{job="daily_load"}', "limit": "10"}))
    lines = [value[1] for stream in body["data"]["result"] for value in stream["values"]]
    assert len(lines) == 4
    assert not native.rows("loki.logs_search")
    assert len(native.rows("loki.logs_get")) == 4


def test_loki_labels_and_values_come_from_the_run_list(native):
    assert native.ok(native.get(loki.app, "/loki/api/v1/labels"))["data"] == list(loki.LABELS)
    jobs = native.ok(native.get(loki.app, "/loki/api/v1/label/job/values"))
    assert jobs["data"] == ["daily_load"]
    sources = native.ok(native.get(loki.app, "/loki/api/v1/label/source/values"))
    assert sources["data"] == ["scheduler_state"]


@pytest.mark.parametrize("query", ['{job=~"daily.*"}', '{job="x"} |~ "e.*"',
                                   '{status="failed"}', "{}"])
def test_loki_refuses_logql_it_does_not_serve(native, query):
    assert native.get(loki.app, "/loki/api/v1/query_range",
                      params={"query": query}).status_code == 400


# --------------------------------------------------------------------------
# Airbyte
# --------------------------------------------------------------------------


def test_airbyte_serves_both_prefixes_identically(native):
    cloud = native.ok(native.get(airbyte.app, "/v1/connections"))
    hosted = native.ok(native.get(airbyte.app, "/api/public/v1/connections"))
    assert cloud == hosted
    assert [item["connectionId"] for item in cloud["data"]] == ["acme_orders", "globex_returns"]
    assert cloud["data"][1]["status"] == "inactive"  # the paused connector


def test_airbyte_jobs_carry_the_run_ids_the_world_uses(native):
    jobs = native.ok(native.get(airbyte.app, "/v1/jobs", params={"connectionId": "acme_orders"}))
    assert [job["jobId"] for job in jobs["data"]] == [3, 1]
    assert "rowsSynced" not in jobs["data"][0], "a run summary does not know its row counts"
    job = native.ok(native.get(airbyte.app, "/v1/jobs/3"))
    assert job["status"] == "succeeded" and job["rowsSynced"] == 52


def test_airbyte_trigger_creates_a_run_in_the_world(native):
    created = native.ok(native.post(airbyte.app, "/v1/jobs",
                                    json={"connectionId": "acme_orders", "jobType": "sync"}))
    assert created["jobId"] == 4
    state = json.loads(
        (native.context.config.ingest).read_text("utf-8")
    )
    assert state["runs"][-1]["id"] == "sync-000004"
    assert native.rows("airbyte.ingest_trigger")[0]["op_class"] == "exec"


def test_airbyte_refuses_a_paused_connector(native):
    response = native.post(airbyte.app, "/v1/jobs", json={"connectionId": "globex_returns"})
    assert response.status_code == 409
    assert response.json()["title"] == "connector_paused"


def test_airbyte_alerts_are_marked_as_this_services_own_route(native):
    alerts = native.ok(native.get(airbyte.app, "/v1/alerts", params={"acknowledged": "false"}))
    assert alerts["data"][0]["kind"] == "sync_failed"


# --------------------------------------------------------------------------
# Deploy
# --------------------------------------------------------------------------


def test_deploy_environments_carry_the_canary(native):
    body = native.ok(native.get(deploy.app, "/repos/alkera/warehouse/environments"))
    names = [item["name"] for item in body["environments"]]
    assert names == ["fix_null_amounts", "prune_old_orders", "build_models"]
    canary = body["environments"][0]["alkera_deploy"]["canary"]
    assert canary["metrics"] == ["orders_rows", "orders_null_rate"]


def test_deploy_creating_a_deployment_runs_it_and_reports_the_canary(native):
    created = native.ok(native.post(deploy.app, "/repos/alkera/warehouse/deployments", json={
        "ref": "fix_null_amounts", "environment": "fix_null_amounts",
        "payload": {"deploy": "fix_null_amounts"},
    }))
    assert created["alkera_canary"]["status"] == "clean"
    assert created["alkera_status"]["state"] == "success"
    row = native.rows("deploy.deploy_run")[0]
    assert row["op_class"] == "write"
    assert "deploy:fix_null_amounts" in row["targets"]
    # The world moved: the null amounts are gone.
    listed = native.ok(native.get(deploy.app, "/repos/alkera/warehouse/deployments"))
    assert [item["id"] for item in listed] == [created["id"]]
    statuses = native.ok(
        native.get(deploy.app, f"/repos/alkera/warehouse/deployments/{created['id']}/statuses")
    )
    assert statuses[0]["state"] == "success"


def test_deploy_status_is_audited_against_the_deploy_it_judges(native):
    created = native.ok(native.post(deploy.app, "/repos/alkera/warehouse/deployments",
                                    json={"payload": {"deploy": "build_models"}}))
    posted = native.post(
        deploy.app, f"/repos/alkera/warehouse/deployments/{created['id']}/statuses",
        json={"state": "inactive", "description": "rolled back by hand"},
    )
    assert posted.status_code == 201
    row = native.rows("deploy.deploy_status")[0]
    assert row["targets"] == ["deploy:build_models"] and row["op_class"] == "write"


def test_a_deployment_status_outlives_the_service(native):
    """The other half of the same finding: a verdict is recorded, not remembered.

    A deployment status goes into the observation record beside the deploy
    history it annotates - not into the world tree, which is where a silence
    goes, because it changes nothing another reader of the world sees. So the
    check here is persistence and nothing about the fingerprint.
    """
    created = native.ok(native.post(deploy.app, "/repos/alkera/warehouse/deployments",
                                    json={"payload": {"deploy": "build_models"}}))
    native.ok(native.post(
        deploy.app, f"/repos/alkera/warehouse/deployments/{created['id']}/statuses",
        json={"state": "inactive", "description": "rolled back by hand"}))

    restarted = native.restarted()
    statuses = restarted.ok(restarted.get(
        deploy.app, f"/repos/alkera/warehouse/deployments/{created['id']}/statuses"))
    # The run's own canary verdict first, then the operator's.
    assert [item["state"] for item in statuses] == ["success", "inactive"]
    assert statuses[1]["description"] == "rolled back by hand"
    assert statuses[1]["environment"] == "build_models"
    # The world tree is untouched: this is observation state, and it lives with
    # the deploy history `deploy_run` already writes there.
    assert not (native.root / "var" / "obs" / "silences.json").exists()
    assert restarted.rows("deploy.deploy_statuses")[-1]["targets"] == ["deploy:build_models"]


def test_deploy_cli_runs_lists_and_refuses_to_pretend_it_rolled_back(native, monkeypatch, capsys):
    """The CLI is an HTTP client, so the test gives it an in-process transport
    and nothing else changes: same URL shape, same JSON, same exit codes."""

    def request(method, url, json=None, timeout=None):
        path = url.split("/repos/alkera/warehouse", 1)[1]
        return native.request(deploy.app, method, f"/repos/alkera/warehouse{path}", json=json)

    monkeypatch.setattr(deploy_cli.httpx, "request", request)
    assert deploy_cli.main(["list"]) == 0
    assert "fix_null_amounts" in capsys.readouterr().out
    assert deploy_cli.main(["run", "fix_null_amounts"]) == 0
    assert "canary clean" in capsys.readouterr().out
    assert deploy_cli.main(["rollback", "fix_null_amounts"]) == 1
    printed = capsys.readouterr().out
    assert "NOTHING WAS REVERTED" in printed
    assert native.rows("deploy.deploy_status")[0]["targets"] == ["deploy:fix_null_amounts"]


# --------------------------------------------------------------------------
# Equivalence: the same action, both transports, the same row
# --------------------------------------------------------------------------

CASES = [
    ("bi_list", {}, "metabase.bi_list",
     lambda n: n.get(metabase.app, "/api/dashboard")),
    ("bi_get", {"dashboard": "item_totals"}, "metabase.bi_get",
     lambda n: n.get(metabase.app, "/api/dashboard/item_totals")),
    ("bi_render", {"dashboard": "item_totals"}, "metabase.bi_render",
     lambda n: n.post(metabase.app, "/api/card/1/query", json={})),
    ("bi_refresh", {"dashboard": "item_totals"}, "metabase.bi_refresh",
     lambda n: n.post(metabase.app, "/api/card/1/query", json={"ignore_cache": True})),
    ("metrics_list", {}, "prometheus.metrics_list",
     lambda n: n.get(prometheus.app, "/api/v1/label/__name__/values")),
    ("metrics_get", {"metric": "orders_rows"}, "prometheus.metrics_get",
     lambda n: n.get(prometheus.app, "/api/v1/query", params={"query": "orders_rows"})),
    ("metrics_series", {"metric": "orders_rows", "limit": prometheus.MAX_SERIES},
     "prometheus.metrics_series",
     lambda n: n.get(prometheus.app, "/api/v1/query_range", params={"query": "orders_rows"})),
    ("alarms_list", {"state": "firing"}, "alertmanager.alarms_list",
     lambda n: n.get(alertmanager.app, "/api/v2/alerts")),
    ("logs_list", {"limit": loki.MAX_RUNS}, "loki.logs_list",
     lambda n: n.get(loki.app, "/loki/api/v1/labels")),
    ("logs_get", {"run_id": "run-000003"}, "loki.logs_get",
     lambda n: n.get(loki.app, "/loki/api/v1/query_range",
                     params={"query": '{run_id="run-000003"}'})),
    ("logs_search", {"query": "exited 1", "job": "daily_load", "limit": 100},
     "loki.logs_search",
     lambda n: n.get(loki.app, "/loki/api/v1/query_range",
                     params={"query": '{job="daily_load"} |= "exited 1"', "limit": "100"})),
    ("ingest_list", {}, "airbyte.ingest_list",
     lambda n: n.get(airbyte.app, "/v1/connections")),
    ("ingest_get", {"connector_id": "acme_orders"}, "airbyte.ingest_get",
     lambda n: n.get(airbyte.app, "/v1/connections/acme_orders")),
    ("ingest_runs", {"connector_id": "acme_orders", "limit": 20}, "airbyte.ingest_runs",
     lambda n: n.get(airbyte.app, "/v1/jobs", params={"connectionId": "acme_orders"})),
    ("ingest_trigger", {"connector_id": "acme_orders"}, "airbyte.ingest_trigger",
     lambda n: n.post(airbyte.app, "/v1/jobs", json={"connectionId": "acme_orders"})),
    ("ingest_alerts", {"connector_id": None, "acknowledged": None}, "airbyte.ingest_alerts",
     lambda n: n.get(airbyte.app, "/v1/alerts")),
    ("deploy_list", {}, "deploy.deploy_list",
     lambda n: n.get(deploy.app, "/repos/alkera/warehouse/environments")),
    ("deploy_history", {"deploy": None, "limit": 50}, "deploy.deploy_history",
     lambda n: n.get(deploy.app, "/repos/alkera/warehouse/deployments")),
    ("deploy_run", {"deploy": "fix_null_amounts"}, "deploy.deploy_run",
     lambda n: n.post(deploy.app, "/repos/alkera/warehouse/deployments",
                      json={"payload": {"deploy": "fix_null_amounts"}})),
]


@pytest.mark.parametrize("tool, args, native_tool, call", CASES, ids=[case[0] for case in CASES])
def test_the_same_action_classifies_the_same_way_on_both_transports(
    equivalence, tool, args, native_tool, call
):
    gateway, native = equivalence()
    expected = mcp_row(gateway, tool, args)
    response = call(native)
    assert response.status_code < 400, response.text
    rows = native.rows(native_tool)
    assert len(rows) == 1, f"expected one {native_tool} row, got {len(rows)}"
    actual = rows[0]
    assert actual["op_class"] == expected["op_class"]
    assert actual["targets"] == expected["targets"]
    assert actual["read_targets"] == expected["read_targets"]


def test_every_ops_plane_tool_is_covered_by_the_equivalence_table():
    """The table is the claim; a tool missing from it is an unproven mapping."""
    covered = {case[0] for case in CASES}
    expected = {
        "bi_list", "bi_get", "bi_render", "bi_refresh",
        "metrics_list", "metrics_get", "metrics_series", "alarms_list",
        "logs_list", "logs_get", "logs_search",
        "ingest_list", "ingest_get", "ingest_runs", "ingest_trigger", "ingest_alerts",
        "deploy_list", "deploy_run", "deploy_history",
    }
    assert covered == expected


def test_a_native_row_validates_as_a_9_2_2_row(native):
    """Shape, not just classification: the row the services write has the keys
    the action log is read for."""
    native.ok(native.get(prometheus.app, "/api/v1/query", params={"query": "orders_rows"}))
    row = native.rows("prometheus.metrics_get")[0]
    for key in ("tool", "args", "args_hash", "op_class", "targets", "read_targets",
                "exit", "error", "flags", "latency_ms", "result", "transport", "wts"):
        assert key in row, key
    assert row["transport"] == "native"
    assert row["op_class"] in native_audit.OP_CLASSES
    assert row["args"]["method"] == "GET" and row["args"]["path"] == "/api/v1/query"
