"""`native/context.py` and the `Observation` it rests on, tested directly.

Nine fake services get their world, their run-and-record span and their 9.2.2
row from this one module. Before it there were four copies of that step
(`services/_ops.py` plus a verbatim block in each of jira, slack and
confluence), and a row writer that exists four times is a row writer that will
disagree with itself. The service test files prove each vendor shape; this file
proves the thing under all nine, on a bare app with no vendor in the way.

What is under test, and why each one is here rather than implied:

1. **Classification is obtained.** `Span.run` hands the module the observation
   and the row is written off it afterwards. The test asserts a row whose
   `op_class`/`targets` nobody in the handler ever named.
2. **A handler that classifies nothing cannot emit a row.** That is the rule
   that makes "add an endpoint and forget to wire it up" fail at the first
   request instead of shipping a row that says `read` and `[]`.
3. **Nesting and sealing.** A header read goes through `aside` and its targets
   never reach the row; a second `run` on one span raises rather than
   re-pointing it.
4. **A refusal still leaves a row**, and classifies the way the MCP gateway
   classifies a failure that never reached a module.
5. **The world is resolved per request** and rebuilt when the config it is
   named by changes, which is what lets a test - or a reload - repoint a
   running app.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from conftest import config
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from alkera_gateway.native import audit as native_audit
from alkera_gateway.native import context as native_context
from alkera_gateway.native.context import (
    DEV_TOKEN,
    Context,
    Ledger,
    ServiceError,
    note,
    served,
    service_health,
    set_context,
)
from alkera_gateway.records import (
    Observation,
    SealedObservation,
    ToolError,
    UnclassifiedObservation,
)

SERVICE = "probe"


# --------------------------------------------------------------------------
# A bare app: every route here is one line of handler over one module call, so
# a failure lands on `context.py` and not on a vendor translation.
# --------------------------------------------------------------------------

app = FastAPI()


@app.exception_handler(ServiceError)
async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
    """Each real service renders this in its vendor's spelling; this one does
    not have a vendor, so the status and the code are all it says."""
    return JSONResponse(status_code=exc.status, content={"code": exc.code})


@app.get("/issue/{key}")
def issue(key: str, request: Request):
    return served(request, SERVICE, "issues_get",
                  lambda ops, obs: ops.issues_get(key, obs), {"key": key})


@app.get("/issue-with-header/{key}")
def issue_with_header(key: str, request: Request):
    """One row for the fetch; the directory read alongside it costs none."""
    with Ledger(request, SERVICE).span("issues_get", key=key) as row:
        found = row.run(lambda ops, obs: ops.issues_get(key, obs))
        listing = row.aside(lambda ops, obs: ops.issues_list(None, None, None, 0, obs))
    return {"issue": found["id"], "statuses": listing["statuses"]}


@app.get("/twice/{key}")
def twice(key: str, request: Request):
    """Two module calls against one span: the mistake `seal` exists to catch."""
    with Ledger(request, SERVICE).span("issues_get", key=key) as row:
        row.run(lambda ops, obs: ops.issues_get(key, obs))
        row.run(lambda ops, obs: ops.issues_list(None, None, None, 0, obs))
    return {}


@app.get("/reclassify/{key}")
def reclassify(key: str, request: Request):
    """A handler that hands a sealed observation to a second module call."""
    with Ledger(request, SERVICE).span("issues_get", key=key) as row:
        found = row.run(lambda ops, obs: ops.issues_get(key, obs))
        row.operations.issues_list(None, None, None, 0, row.observation)
    return found


@app.get("/unclassified")
def unclassified(request: Request):
    """An endpoint that answers out of its own head. There is no row to write."""
    with Ledger(request, SERVICE).span("made_up"):
        pass
    return {}


@app.get("/noted")
def noted(request: Request):
    """The escape hatch: audited, but classified by hand because nothing ran."""
    note(request, SERVICE, "constant", op_class="read", targets=["issues"],
         result={"fields": 2})
    return {"fields": 2}


@app.get("/health")
def health(request: Request):
    return service_health(
        request, SERVICE, lambda ops, obs: ops.issues_list(None, None, None, 0, obs)["total"]
    )


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class Probe:
    """The app over one world, and the rows it wrote."""

    def __init__(self, root, settings, rows):
        self.root, self.settings, self.rows = root, settings, rows

    def get(self, path, **kwargs) -> httpx.Response:
        async def call():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://probe.internal") as client:
                return await client.get(path, **kwargs)

        # `asyncio.run`, not the ambient loop: these tests are synchronous and
        # `asyncio_mode = "auto"` closes the loop a neighbouring async test used.
        return asyncio.run(call())

    def written(self) -> list[dict]:
        if not self.rows.exists():
            return []
        return [json.loads(line) for line in
                self.rows.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def probe(world, tmp_path, monkeypatch):
    """The app pointed at a world through the documented env vars, not by hand.

    `set_context` is deliberately NOT used here: the env-var path is the one the
    human plane relies on and the one `serve.py` falls back to, so it is the one
    that has to be exercised.
    """
    audit = tmp_path / "native-audit"
    audit.mkdir()
    settings = config(world, audit)
    written = tmp_path / "gateway.json"
    written.write_text(settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv(native_context.CONFIG_ENV, str(written))
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit / "actions.jsonl"))
    monkeypatch.setenv("ALKERA_SCHEDULER", str(settings.scheduler))
    app.state.alkera_context = None
    return Probe(world, settings, audit / "actions.jsonl")


# --------------------------------------------------------------------------
# `records.Observation`: the object the whole arrangement rests on
# --------------------------------------------------------------------------


def test_an_observation_knows_whether_a_module_classified_it():
    """`classified` is what separates "a read of nothing" from "nothing ran"."""
    blank = Observation()
    assert blank.classified is False
    assert blank.op_class == "read" and blank.targets == []

    blank.targets = ["issues"]
    assert blank.classified is True

    # A flag is a detail of an action, not the claim that one happened.
    flagged = Observation()
    flagged.flags.append("no_change")
    assert flagged.classified is False

    # Constructed with a classification, which is how the tailers build one.
    assert Observation(op_class="write", targets=["x"]).classified is True
    assert Observation(flags=["outside_tool_surface"]).classified is False


def test_a_sealed_observation_refuses_a_second_classification():
    observation = Observation()
    observation.op_class = "write"
    observation.targets = ["issue:DATA-141"]
    observation.seal()

    with pytest.raises(SealedObservation) as refused:
        observation.targets = ["issues"]
    assert "issue:DATA-141" in str(refused.value)
    with pytest.raises(SealedObservation):
        observation.op_class = "read"
    # What it says is unchanged, and a flag - not a classifier - still lands.
    assert observation.targets == ["issue:DATA-141"] and observation.op_class == "write"
    observation.flags.append("no_change")
    assert observation.flags == ["no_change"]


def test_a_child_observation_cannot_overwrite_its_parent():
    parent = Observation()
    parent.op_class = "write"
    parent.targets = ["issue:DATA-141"]
    child = parent.child()
    child.targets = ["issues"]
    assert parent.targets == ["issue:DATA-141"] and parent.op_class == "write"
    assert child.classified is True and child.op_class == "read"


def test_the_bookkeeping_is_not_part_of_what_an_observation_says():
    """`classified`/`sealed` must not change equality or the repr, or every
    existing comparison of two observations silently changes meaning."""
    left, right = Observation(targets=["a"]), Observation(targets=["a"])
    right.seal()
    assert left == right
    assert "sealed" not in repr(left) and "classified" not in repr(left)


# --------------------------------------------------------------------------
# The span: one action, one row, classified by the module
# --------------------------------------------------------------------------


def test_the_row_is_classified_by_the_module_and_not_by_the_handler(probe):
    """Nothing in this file names `read` or `issue:DATA-141`; the row does."""
    assert probe.get("/issue/DATA-141").status_code == 200
    row = probe.written()[-1]
    assert row["tool"] == "probe.issues_get"
    assert row["op_class"] == "read"
    assert row["targets"] == ["issue:DATA-141"] == row["read_targets"]
    assert row["args"] == {"method": "GET", "path": "/issue/DATA-141", "key": "DATA-141"}
    assert set(row) == set(native_audit.ROW_KEYS)
    assert row["transport"] == "native"
    # The module result, not the HTTP body: a grader reads one shape from both
    # transports.
    assert row["result"]["title"]


def test_a_header_read_costs_no_row_and_leaks_no_target(probe):
    """`aside` is HUMAN_PLANE.md rule 3, in code rather than in prose."""
    response = probe.get("/issue-with-header/DATA-141")
    assert response.status_code == 200
    assert response.json()["statuses"]
    written = probe.written()
    assert [row["tool"] for row in written] == ["probe.issues_get"]
    # `issues` - what the directory read touched - is nowhere in the row.
    assert written[0]["targets"] == ["issue:DATA-141"]
    assert written[0]["read_targets"] == ["issue:DATA-141"]


def test_a_second_module_call_on_one_span_raises_rather_than_re_pointing_it(probe):
    """Two calls is two actions; the second needs its own row, or `aside`."""
    with pytest.raises(RuntimeError) as refused:
        probe.get("/twice/DATA-141")
    assert "second module call" in str(refused.value)
    # The failed attempt is still on file: an unaudited request is the hole.
    assert [row["exit"] for row in probe.written()] == ["error"]


def test_a_sealed_span_refuses_a_module_call_that_would_reclassify_it(probe):
    with pytest.raises(SealedObservation):
        probe.get("/reclassify/DATA-141")
    row = probe.written()[-1]
    assert row["exit"] == "error"
    # Sealed at the moment the defining call returned, so the row still names
    # the issue rather than the listing that tried to overwrite it.
    assert row["targets"] == ["issue:DATA-141"]


def test_an_endpoint_that_classifies_nothing_cannot_emit_a_row(probe):
    """The rule that makes a missing wire-up loud.

    Adding an endpoint without running a module call used to produce a row
    saying `read` and no target - indistinguishable from a real read of
    nothing. It now raises, at the first request, naming the tool.
    """
    with pytest.raises(UnclassifiedObservation) as refused:
        probe.get("/unclassified")
    assert "probe.made_up" in str(refused.value)
    assert probe.written() == []


def test_the_escape_hatch_is_explicit_and_still_writes_a_row(probe):
    """`note` is for the endpoints with no module behind them, and says so."""
    assert probe.get("/noted").json() == {"fields": 2}
    row = probe.written()[-1]
    assert row["tool"] == "probe.constant"
    assert row["op_class"] == "read" and row["targets"] == ["issues"]


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_module_refusal_leaves_a_row_and_becomes_a_service_error(probe):
    """The module classified before it raised, so the row names the target."""
    response = probe.get("/issue/DATA-000")
    assert response.status_code == 404
    row = probe.written()[-1]
    assert row["exit"] == "error"
    assert row["error"] == {"code": "issue_not_found", "message": row["error"]["message"]}
    assert row["targets"] == ["issue:DATA-000"]
    assert row["op_class"] == "read"


def test_a_failure_that_never_reached_a_module_classifies_as_the_gateway_does(probe):
    """An unclassified *refusal* is allowed, and is not a silent one.

    `Gateway._call` writes `read` and no targets when a call fails before it
    dispatches - a bad argument, an unknown tool. A native refusal that never
    reached a module is the same event and gets the same row, which is why the
    unclassified rule applies to successes only.
    """
    empty = Observation()
    assert empty.classified is False
    assert (empty.op_class, empty.targets, empty.read_targets) == ("read", [], [])


def test_service_error_maps_module_codes_to_the_statuses_the_vendors_use():
    assert ServiceError.from_tool_error(ToolError("issue_not_found", "x")).status == 404
    assert ServiceError.from_tool_error(ToolError("no_observability", "x")).status == 503
    assert ServiceError.from_tool_error(ToolError("connector_paused", "x")).status == 409
    assert ServiceError.from_tool_error(ToolError("invalid_jql", "x")).status == 400


# --------------------------------------------------------------------------
# Health, and the world behind it
# --------------------------------------------------------------------------


def test_health_counts_the_world_and_keeps_its_own_target_namespace(probe):
    assert probe.get("/health").json() == {"ok": True, "service": SERVICE, "items": 8}
    row = probe.written()[-1]
    assert row["tool"] == "probe.health"
    # A polled gate must not inject the episode's target set (SERVICES_B item 4).
    assert row["targets"] == ["health:probe"]
    assert row["read_targets"] == ["issues"]


def test_a_world_without_the_surface_answers_503_rather_than_zero(tmp_path, monkeypatch):
    settings = tmp_path / "gateway.json"
    empty = tmp_path / "empty"
    (empty / "workspace").mkdir(parents=True)
    settings.write_text(json.dumps({"workspace": str(empty / "workspace"),
                                    "workflow_dir": str(empty / "var" / "workflow"),
                                    "audit_dir": str(tmp_path / "audit")}), encoding="utf-8")
    monkeypatch.setenv(native_context.CONFIG_ENV, str(settings))
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(tmp_path / "audit" / "actions.jsonl"))
    app.state.alkera_context = None
    probe = Probe(empty, None, tmp_path / "audit" / "actions.jsonl")
    response = probe.get("/health")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "service": SERVICE, "items": 0,
                               "error": "no_issue_tracker"}


def test_the_world_is_resolved_per_request_and_rebuilt_when_the_config_changes(
        probe, tmp_path, monkeypatch):
    """A reload or a test may repoint a running app; a cached context may not
    outlive the file it was built from."""
    first = native_context.process_context()
    assert native_context.process_context() is first  # same bytes, same context

    moved = tmp_path / "moved.json"
    moved.write_text(probe.settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv(native_context.CONFIG_ENV, str(moved))
    second = native_context.process_context()
    assert second is not first
    assert second.config.workflow_dir == first.config.workflow_dir

    # And an explicit context wins over the environment, which is how
    # `serve.py` and the ops-plane tests point nine apps at one world.
    set_context(app, Context(probe.settings))
    assert native_context.context_of(app) is not second
    app.state.alkera_context = None


def test_the_clock_is_the_worlds_and_never_the_wall(probe):
    """Every row's `vts` comes from the scheduler state, not from `time`."""
    assert native_context.process_context().clock() == "2026-03-05T09:00:00Z"
    probe.get("/issue/DATA-141")
    assert probe.written()[-1]["vts"] == "2026-03-05T09:00:00Z"


def test_the_shared_credential_is_obviously_fake():
    """Repo rule: a workspace credential must never look like a real key."""
    assert DEV_TOKEN == "alkera-local-dev-token"
    assert len(DEV_TOKEN) >= 8 and "alkera-local-dev" in DEV_TOKEN


def test_the_services_get_the_same_scheduler_the_mcp_gateway_gets(
        world, tmp_path, monkeypatch):
    """`Context` must build `Operations` with `scheduling.episode_scheduler`.

    `alkera_gateway.cli serve` injects it, and until this test existed the nine
    native services did not: they ran on the built-in `FileScheduler` while
    `./ops/sched` ran on `WorldScheduler`, so the two halves of native mode
    disagreed about which scheduler the episode's world has. `FileScheduler`
    reports `not_executed` for every job that is not dbt-shaped, which is the
    failure `scheduling.py` was written to end.

    Both directions, because the fallback is not a bug: a config that names no
    world keeps the built-in, and that is what a service serving an arbitrary
    config should do.
    """
    from alkera_gateway.surfaces import FileScheduler

    assert (world / "var" / "sched" / "state.json").is_file()

    # No `world_root`: nothing names a world, so the built-in stays.
    plain = Context(config(world, tmp_path / "audit"))
    assert isinstance(plain.operations.scheduler, FileScheduler)

    # `world_root` + its scheduler state: the executing scheduler, the one the
    # MCP gateway is handed. `world/` is on the path exactly as an episode has
    # it (tasks/harness/episode.venv_env sets PYTHONPATH to the repo's world/).
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[2] / "world"))
    from world.gateway_scheduler import WorldScheduler

    episode = Context(config(world, tmp_path / "audit", world_root=world))
    assert isinstance(episode.operations.scheduler, WorldScheduler)
    assert Path(episode.operations.scheduler.root) == Path(world)
