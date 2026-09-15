"""History comes from the record, and the record is the only place it exists.

`OPS-SURFACE` shipped a metrics surface whose every reading was re-derivable from
the data, and then named the defect: reading the dashboard was strictly dominated
by reading the warehouse, so an agent that consulted the dashboard could be
misled and one that ignored it could not.

The fix is not a better alarm. It is that a real observability system **records
what happened** while ours **computed what is** - and those are the same thing
only in a world that never moves. Once `advance` has landed a partition, the
state that produced yesterday's freshness reading is gone, and the sample written
down at the time is the only evidence it ever held that value.

The load-bearing test in this file is
`test_a_past_sample_cannot_be_reconstructed_from_the_world_that_replaced_it`.
Everything else supports it. If that test can be made to pass by deleting
`samples.jsonl`, the store is decoration.
"""

import copy
import json
from pathlib import Path

import duckdb
import pytest
import yaml
from conftest import config
from test_observability import DEFINITIONS, assert_ok, seed_observability

from alkera_gateway import Gateway
from alkera_gateway.audit import read_rows
from alkera_gateway.observability import ObservabilitySpec
from alkera_gateway.records import digest

# The world the recorded samples describe, and the world that replaced it.
T0 = "2026-03-05T09:00:00Z"
T1 = "2026-03-05T18:00:00Z"

# The reading that this whole file exists to protect: orders_freshness as it
# genuinely stood at T0, before the day's partition landed.
FRESHNESS_AT_T0 = 118800.0

# A one-line addition to the R003 fixture: a series alarm whose window is short
# enough that the recorded readings and a fresh per-bucket derivation disagree.
RECORDED_VOLUME_ALARM = {
    "id": "orders_volume_recorded",
    "metric": "orders_rows",
    "evaluate_on": "series",
    "datapoints_to_alarm": 2,
    "comparison": "lt",
    "threshold": 10,
    "severity": "warning",
    "title": "Order volume below ten on two consecutive readings",
}


def definitions(retention_days=14):
    spec = copy.deepcopy(DEFINITIONS)
    spec["retention"] = {"days": retention_days}
    spec["alarms"] = spec["alarms"] + [copy.deepcopy(RECORDED_VOLUME_ALARM)]
    return spec


# What the world wrote down while it was running, per OBS_STORE_CONTRACT.md.
# Four nights of the daily load plus the reading taken at T0. The night of
# 2026-03-04 records `null`: the loader failed, so no reading could be taken -
# which is a fact worth writing down and is emphatically not a zero.
RECORDED = [
    {
        "vts": "2026-03-02T01:00:45Z",
        "metric": "orders_freshness",
        "value": 90045.0,
        "source": "advance",
        "run_id": "run-000001",
        "detail": {"watermark": "2026-03-01T00:00:00Z"},
    },
    {
        "vts": "2026-03-02T01:00:45Z",
        "metric": "orders_rows",
        "value": 8.0,
        "source": "advance",
        "run_id": "run-000001",
        "detail": {"rows": 8},
    },
    {
        "vts": "2026-03-03T01:00:45Z",
        "metric": "orders_freshness",
        "value": 90045.0,
        "source": "advance",
        "run_id": "run-000002",
        "detail": {"watermark": "2026-03-02T00:00:00Z"},
    },
    {
        "vts": "2026-03-03T01:00:45Z",
        "metric": "orders_rows",
        "value": 16.0,
        "source": "advance",
        "run_id": "run-000002",
        "detail": {"rows": 16},
    },
    {
        "vts": "2026-03-04T01:02:00Z",
        "metric": "orders_freshness",
        "value": None,
        "source": "advance",
        "run_id": "run-000003",
        "detail": {"error": "connection reset by peer"},
    },
    {
        "vts": "2026-03-04T01:02:00Z",
        "metric": "orders_rows",
        "value": None,
        "source": "advance",
        "run_id": "run-000003",
        "detail": {"error": "connection reset by peer"},
    },
    {
        "vts": "2026-03-05T01:05:00Z",
        "metric": "orders_freshness",
        "value": 90300.0,
        "source": "advance",
        "run_id": "run-000004",
        "detail": {"watermark": "2026-03-04T00:00:00Z"},
    },
    {
        "vts": "2026-03-05T01:05:00Z",
        "metric": "orders_rows",
        "value": 18.0,
        "source": "advance",
        "run_id": "run-000004",
        "detail": {"rows": 18},
    },
    {
        "vts": T0,
        "metric": "orders_freshness",
        "value": FRESHNESS_AT_T0,
        "source": "advance",
        "run_id": "run-000004",
        "detail": {"watermark": "2026-03-04T00:00:00Z"},
    },
    {
        "vts": T0,
        "metric": "orders_rows",
        "value": 20.0,
        "source": "advance",
        "run_id": "run-000004",
        "detail": {"rows": 20},
    },
    {
        "vts": T0,
        "metric": "orders_null_rate",
        "value": 0.1,
        "source": "advance",
        "run_id": "run-000004",
        "detail": {"rows": 20, "null_rows": 2},
    },
]

READ_CALLS = [
    ("metrics_list", {}),
    ("alarms_list", {}),
    ("alarms_history", {}),
    ("deploy_list", {}),
    ("deploy_history", {}),
    ("logs_list", {}),
    ("sched_state", {}),
    ("sql_query", {"sql": "select * from warehouse.orders order by order_id"}),
    ("sql_query", {"sql": "select max(order_date) as watermark from warehouse.orders"}),
]


def samples_path(root):
    return root / "var" / "obs" / "samples.jsonl"


def write_samples(root, rows=RECORDED):
    """A deterministic stand-in for the world's writer. Byte-for-byte stable for
    a given list, which is what the writer side of the contract promises."""
    path = samples_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def advance_the_world(root, vnow=T1):
    """What `advance` does to this world, done directly.

    The day's partition lands - eight orders dated 2026-03-05, arriving at 18:00
    on a day whose clock read 09:00 when the last sample was taken - and the
    backfill completes the two amounts that the failed night left blank. Both are
    ordinary things for a day to do, and between them they destroy every route
    back to the reading the record holds for T0."""
    db = root / "var" / "warehouse" / "warehouse.duckdb"
    with duckdb.connect(str(db)) as conn:
        for index in range(8):
            conn.execute(
                "INSERT INTO warehouse.warehouse.orders VALUES "
                f"({200 + index}, DATE '2026-03-05', 'C9{index:02d}', 25.00)"
            )
        conn.execute(
            "UPDATE warehouse.warehouse.orders SET amount = 12.00 "
            "WHERE order_date = DATE '2026-03-04' AND amount IS NULL"
        )
    state_path = root / "var" / "sched" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["vnow"] = vnow
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def surface_dump(gateway, spec=None):
    """Every read the observability surface offers, serialised. Used to ask a
    question no single assertion can: is this number reachable *anywhere*."""
    spec = spec or definitions()
    calls = list(READ_CALLS)
    calls += [("metrics_get", {"metric": m["id"]}) for m in spec["metrics"]]
    calls += [("metrics_series", {"metric": m["id"], "limit": 200}) for m in spec["metrics"]]
    calls += [("alarms_get", {"alarm": a["id"]}) for a in spec["alarms"]]
    out = []
    for tool, args in calls:
        out.append(json.dumps(gateway.call(tool, args), sort_keys=True, default=str))
    return "\n".join(out)


@pytest.fixture
def recorded_world(world):
    root = seed_observability(world, definitions=definitions())
    write_samples(root)
    return root


@pytest.fixture
def advanced(recorded_world, tmp_path):
    """A world that has moved on from the samples it recorded. The gateway is
    built *after* the advance, so nothing in this episode ever measured the old
    world - exactly the position an agent arriving at the episode is in."""
    advance_the_world(recorded_world)
    instance = Gateway(config(recorded_world, tmp_path / "audit"))
    yield instance
    instance.close()


# --------------------------------------------------------------------------
# The invariant
# --------------------------------------------------------------------------


def test_a_past_sample_cannot_be_reconstructed_from_the_world_that_replaced_it(
    advanced, recorded_world, capsys
):
    """The one test that proves the assignment.

    The record says orders_freshness was 118800.0 at 2026-03-05T09:00:00Z. The
    world then advanced. Assert three things, in order:

    1. the live reading is a different number, so `now` really did move;
    2. the best reconstruction available from present state - the same
       point-in-time query the lagged-collector machinery runs - gives a
       different answer, so the old value is not recoverable by computing;
    3. with the store deleted, the number 118800.0 appears nowhere in any
       response the whole surface can produce, and with the store present it
       does. The store is therefore the only evidence, which is the definition
       of a record rather than a decoration.
    """
    evidence = []

    recorded = assert_ok(advanced.call("metrics_series", {"metric": "orders_freshness"}))
    at_t0 = [point for point in recorded["points"] if point["at"] == T0]
    assert at_t0 == [
        {
            "at": T0,
            "value": FRESHNESS_AT_T0,
            "source": "advance",
            "run_id": "run-000004",
            "detail": {"watermark": "2026-03-04T00:00:00Z"},
        }
    ]
    assert recorded["series_source"] == "recorded"
    evidence.append(f"recorded at {T0}:                       {FRESHNESS_AT_T0}")

    # 1. `now` is computed, and it moved.
    live = assert_ok(advanced.call("metrics_get", {"metric": "orders_freshness"}))["current"]
    assert live["value"] == 64800.0 != FRESHNESS_AT_T0
    assert live["detail"]["watermark"] == "2026-03-05T00:00:00Z"
    evidence.append(f"measured now ({T1}):       {live['value']}")

    # 2. The best reconstruction present state allows is a different number. The
    #    partition that landed at 18:00 is dated 2026-03-05, so it is *inside*
    #    any window ending at T0: restricting rows to those dated at or before
    #    T0 no longer excludes it, and no other restriction is available.
    watermark = assert_ok(
        advanced.call(
            "sql_query",
            {
                "sql": "select max(order_date) as watermark from warehouse.orders "
                "where order_date <= TIMESTAMP '2026-03-05 09:00:00'"
            },
        )
    )["rows"][0][0]
    assert watermark == "2026-03-05"
    reconstructed = 9 * 3600.0  # T0 minus 2026-03-05T00:00:00Z
    assert reconstructed == 32400.0 != FRESHNESS_AT_T0
    evidence.append(f"reconstructed for {T0} from the world as it now stands: {reconstructed}")

    # 3. Present with the record; absent without it.
    with_store = surface_dump(advanced)
    assert "118800.0" in with_store
    samples_path(recorded_world).unlink()
    without_store = surface_dump(advanced)
    assert "118800.0" not in without_store
    # And it is genuinely the whole surface that goes quiet, not one tool.
    assert "no_observability" not in without_store
    assert assert_ok(advanced.call("metrics_get", {"metric": "orders_freshness"}))["current"][
        "value"
    ] == 64800.0
    spec = definitions()
    calls = len(READ_CALLS) + 2 * len(spec["metrics"]) + len(spec["alarms"])
    evidence.append(
        f"'118800.0' anywhere in the {calls} read responses the surface can produce:"
    )
    evidence.append("    with    var/obs/samples.jsonl:  present")
    evidence.append("    without var/obs/samples.jsonl:  absent")

    with capsys.disabled():
        print("\n  " + "\n  ".join(evidence))


def test_an_alarm_that_fired_yesterday_still_shows_as_having_fired(advanced):
    """The advance cleared orders_stale. The record still knows it fired."""
    alarm = assert_ok(advanced.call("alarms_get", {"alarm": "orders_stale"}))
    assert alarm["state"] == "ok"  # 64800s < the 86400s threshold, measured now
    assert alarm["recorded"]["fired_in_window"] is True
    assert alarm["recorded"]["last_fired_at"] == "2026-03-05T01:05:00Z"

    states = [(item["at"], item["state"], item["origin"]) for item in alarm["history"]]
    assert ("2026-03-02T01:00:45Z", "firing", "recorded_samples") in states
    assert ("2026-03-04T01:02:00Z", "insufficient_data", "recorded_samples") in states
    assert ("2026-03-05T01:05:00Z", "firing", "recorded_samples") in states
    # ...and the episode's own observation of the cleared state is kept apart
    # from the world's record of the firing.
    assert states[-1][1:] == ("ok", "gateway_observed")

    # `since` is the start of the *current* run, not the first time the alarm was
    # ever ok - a flapping alarm has more than one ok in its history.
    assert alarm["since"] == alarm["evaluated_at"]

    listing = assert_ok(advanced.call("alarms_list", {}))
    firing = {item["alarm"] for item in listing["alarms"] if item["state"] == "firing"}
    assert "orders_stale" not in firing
    assert listing["fired_in_window"] >= 1


def test_alarm_state_comes_from_the_recorded_series_not_a_fresh_derivation(
    advanced, recorded_world
):
    """`orders_volume_recorded` compares the last two readings of orders_rows
    against a floor of ten.

    The record holds the scalar readings the world took - 18 then 20 - so the
    alarm is ok. A fresh derivation would instead group the table by day and
    compare 8 and 8, and fire. Both are defensible numbers and they differ in
    scope, which is exactly why history must not be recomputed: a series
    re-derived today answers a different question than the one that was asked
    and answered at the time."""
    from_record = assert_ok(advanced.call("alarms_get", {"alarm": "orders_volume_recorded"}))
    assert from_record["series_source"] == "recorded"
    assert [point["value"] for point in from_record["datapoints"]] == [18.0, 20.0]
    assert from_record["state"] == "ok"

    samples_path(recorded_world).unlink()
    recomputed = assert_ok(advanced.call("alarms_get", {"alarm": "orders_volume_recorded"}))
    assert recomputed["series_source"] == "derived_from_data"
    assert [point["value"] for point in recomputed["datapoints"]] == [2.0, 8.0]
    assert recomputed["state"] == "firing"


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def test_old_samples_age_out_and_the_surface_says_so_rather_than_going_quiet(world, tmp_path):
    """A record with no limit is an oracle. This one keeps two days, and what
    falls outside is reported, not silently missing - "it aged out" and "it was
    never recorded" must not look the same."""
    root = seed_observability(world, definitions=definitions(retention_days=2))
    write_samples(root)
    advance_the_world(root)
    gateway = Gateway(config(root, tmp_path / "audit"))
    try:
        series = assert_ok(gateway.call("metrics_series", {"metric": "orders_freshness"}))
        stamps = [point["at"] for point in series["points"]]
        # The window opens at T1 minus two days.
        assert series["recorded"]["window_start"] == "2026-03-03T18:00:00Z"
        assert stamps == ["2026-03-04T01:02:00Z", "2026-03-05T01:05:00Z", T0]
        assert "2026-03-02T01:00:45Z" not in stamps
        assert series["recorded"]["aged_out"] == 2
        assert series["recorded"]["retained"] == 3
        assert "older than the 2-day window" in series["recorded"]["aged_out_note"]

        # And the alarm history shortens with it: the firing on 2026-03-02 is
        # outside the window, so the surface does not claim to know about it.
        alarm = assert_ok(gateway.call("alarms_get", {"alarm": "orders_stale"}))
        origins = [item["at"] for item in alarm["history"] if item["origin"] == "recorded_samples"]
        assert origins == ["2026-03-04T01:02:00Z", "2026-03-05T01:05:00Z"]
        assert alarm["recorded"]["aged_out"] == 2
    finally:
        gateway.close()


def test_a_window_that_excludes_everything_is_an_empty_record_not_a_missing_one(world, tmp_path):
    root = seed_observability(world, definitions=definitions(retention_days=1))
    write_samples(root, RECORDED[:4])  # all stamped 2026-03-02 and -03
    advance_the_world(root)
    gateway = Gateway(config(root, tmp_path / "audit"))
    try:
        series = assert_ok(gateway.call("metrics_series", {"metric": "orders_freshness"}))
        assert series["recorded"]["present"] is True
        assert series["recorded"]["retained"] == 0
        assert series["recorded"]["aged_out"] == 2
        # Nothing recorded is left in the window, so the surface falls back to
        # what it can honestly offer and says which source it used.
        assert series["series_source"] == "none"
    finally:
        gateway.close()


# --------------------------------------------------------------------------
# The record is evidence, so it is never faked and never edited
# --------------------------------------------------------------------------


def test_a_recorded_null_is_never_served_as_a_zero(advanced):
    series = assert_ok(advanced.call("metrics_series", {"metric": "orders_freshness"}))
    blank = [point for point in series["points"] if point["at"] == "2026-03-04T01:02:00Z"]
    assert blank[0]["value"] is None
    assert blank[0]["detail"] == {"error": "connection reset by peer"}
    assert 0.0 not in [point["value"] for point in series["points"]]

    # An alarm whose window contains a recorded null does not guess.
    alarm = assert_ok(advanced.call("alarms_get", {"alarm": "orders_stale"}))
    recorded = [item for item in alarm["history"] if item["at"] == "2026-03-04T01:02:00Z"]
    assert recorded[0]["state"] == "insufficient_data"
    assert recorded[0]["value"] is None


def test_the_gateway_never_writes_to_the_world_s_sample_store(advanced, recorded_world):
    """The writer side of the contract belongs to the world. A reader that can
    append to the evidence is not a reader."""
    before = samples_path(recorded_world).read_bytes()
    surface_dump(advanced)
    assert_ok(advanced.call("deploy_run", {"deploy": "prune_old_orders"}))
    surface_dump(advanced)
    assert samples_path(recorded_world).read_bytes() == before


def test_a_malformed_line_is_counted_and_named_not_silently_dropped(world, tmp_path):
    root = seed_observability(world, definitions=definitions())
    path = write_samples(root)
    path.write_text(
        path.read_text(encoding="utf-8")
        + "{not json\n"
        + json.dumps({"vts": T0, "metric": "orders_freshness"}, sort_keys=True)  # no value key
        + "\n"
        + json.dumps({"vts": "yesterday", "metric": "orders_freshness", "value": 1.0})
        + "\n",
        encoding="utf-8",
    )
    advance_the_world(root)
    gateway = Gateway(config(root, tmp_path / "audit"))
    try:
        series = assert_ok(gateway.call("metrics_series", {"metric": "orders_freshness"}))
        status = series["recorded"]
        assert status["malformed"] == 3
        assert [item["line"] for item in status["malformed_lines"]] == [12, 13, 14]
        # A missing `value` key is malformed, emphatically not a zero.
        assert 0.0 not in [point["value"] for point in series["points"]]
    finally:
        gateway.close()


def test_a_sample_stamped_after_the_world_clock_is_not_treated_as_observed(world, tmp_path):
    root = seed_observability(world, definitions=definitions())
    write_samples(
        root,
        RECORDED
        + [
            {
                "vts": "2026-03-09T01:00:00Z",
                "metric": "orders_freshness",
                "value": 999999.0,
                "source": "advance",
                "run_id": "run-000099",
                "detail": {},
            }
        ],
    )
    advance_the_world(root)
    gateway = Gateway(config(root, tmp_path / "audit"))
    try:
        series = assert_ok(gateway.call("metrics_series", {"metric": "orders_freshness"}))
        assert 999999.0 not in [point["value"] for point in series["points"]]
        assert series["recorded"]["not_yet_observed"] == 1
    finally:
        gateway.close()


def test_a_world_with_no_sample_store_behaves_exactly_as_it_did_before(world, tmp_path):
    """The store is additive. A world that seeds definitions and no samples gets
    the R003 surface unchanged, and says plainly that there is no record."""
    root = seed_observability(world, definitions=definitions())
    gateway = Gateway(config(root, tmp_path / "audit"))
    try:
        series = assert_ok(gateway.call("metrics_series", {"metric": "orders_rows"}))
        assert series["series_source"] == "derived_from_data"
        assert series["recorded"]["present"] is False
        assert series["recorded"]["retained"] == 0
        alarm = assert_ok(gateway.call("alarms_get", {"alarm": "orders_stale"}))
        assert alarm["recorded"] == {"present": False}
        assert alarm["state"] == "firing"
    finally:
        gateway.close()


def test_the_author_side_field_still_never_reaches_the_agent(advanced):
    """Unchanged from R003, re-asserted over the new response shapes: telling the
    agent which alarm was planted crooked would destroy the measurement."""
    dump = surface_dump(advanced)
    for forbidden in ("known_imprecision", "blind_spot", "false_positive", "AUTHOR-SIDE"):
        assert forbidden not in dump


# --------------------------------------------------------------------------
# Definitions: every world is now live
# --------------------------------------------------------------------------


def theme_definitions():
    themes = Path(__file__).resolve().parents[2] / "world" / "world" / "themes"
    return sorted(themes.glob("*/obs/definitions.yaml"))


@pytest.mark.skipif(not theme_definitions(), reason="world/ is not checked out beside gateway/")
@pytest.mark.parametrize("path", theme_definitions(), ids=lambda p: p.parents[1].name)
def test_every_theme_seeds_definitions_this_surface_can_load(path):
    """OPS-SURFACE left the surface dead in every real world: no theme seeded a
    definitions file, so every built world answered `no_observability`. These
    four files are that handoff. They are validated here against the same model
    the gateway loads them with, so a typo in a theme fails the gateway suite
    rather than an episode."""
    spec = ObservabilitySpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert spec.metrics and spec.alarms and spec.deploys
    assert spec.retention.days >= 1
    # Every metric is reachable. The staging.* models are dbt views whose
    # stored bodies name the `warehouse` catalog, which does not exist under the
    # gateway's ATTACH ... AS wh connection, so they are unreadable through every
    # gateway tool, sql_query included. A definition that points at one blanks
    # silently, which is the worst way for a metric to be wrong.
    referenced = []
    for metric in spec.metrics:
        referenced += [metric.source.table, metric.source.sql]
        if metric.series is not None:
            referenced.append(metric.series.sql)
    for deploy in spec.deploys:
        referenced += deploy.statements
    assert not [text for text in referenced if text and "staging." in text]
    # The theme's jobs are real jobs.
    manifest = yaml.safe_load(
        (path.parents[1] / "theme.yaml").read_text(encoding="utf-8")
    )
    jobs = set(manifest["jobs"])
    for metric in spec.metrics:
        if metric.source.job is not None:
            assert metric.source.job in jobs, f"{path.parents[1].name}: unknown job"
    # At least one alarm carries declared author-side imprecision, and at least
    # one job-side and one data-side alarm exist: the theme's pathology has to be
    # noticeable from both directions.
    kinds = {spec_metric.id: spec_metric.kind for spec_metric in spec.metrics}
    sources = {kinds[alarm.metric] for alarm in spec.alarms}
    assert sources & {"job_duration", "job_error_rate"}
    assert sources & {"freshness", "row_count", "null_rate", "sql_scalar"}


def test_definitions_yaml_is_read_when_observability_yml_is_absent(world, tmp_path):
    """OBS_STORE_CONTRACT.md names the definitions file `definitions.yaml` while
    the gateway's own default is `observability.yml`. Both are read, so the two
    documents cannot drift into a world that answers `no_observability`."""
    root = seed_observability(world, definitions=definitions())
    obs = root / "var" / "obs"
    (obs / "observability.yml").rename(obs / "definitions.yaml")
    write_samples(root)
    gateway = Gateway(config(root, tmp_path / "audit"))
    try:
        listing = assert_ok(gateway.call("metrics_list", {}))
        assert {metric["id"] for metric in listing["metrics"]} >= {"orders_freshness"}
    finally:
        gateway.close()


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def test_the_action_log_contract_holds_for_every_history_read(advanced, tmp_path):
    """agent.md 9.2.2: every call writes seq, tool, args_hash, op_class, exit -
    and op_class stays in the log rather than in the response."""
    calls = [
        ("metrics_series", {"metric": "orders_freshness", "limit": 200}),
        ("alarms_history", {"alarm": "orders_stale", "limit": 50}),
        ("alarms_get", {"alarm": "orders_volume_recorded"}),
    ]
    for tool, args in calls:
        assert_ok(advanced.call(tool, args))
    rows = read_rows(advanced.audit.actions)
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
    tail = rows[-len(calls) :]
    for row, (tool, args) in zip(tail, calls, strict=True):
        assert row["tool"] == tool
        assert row["args_hash"] == digest(args)
        assert row["op_class"] == "read"
        assert row["exit"] == "ok"
        assert f"metric:{args.get('metric')}" in row["targets"] or f"alarm:{args.get('alarm')}" in (
            row["targets"]
        )
