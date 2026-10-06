"""Containment P7 under an environment profile, and the services' own state.

    cd gateway && uv run pytest ../tasks/harness/test_profile_containment.py -q

What P7 asks is decided by `native_episode_probe.expectations(profile, plugs)`,
computed by serve.py's own rules: every service the box serves (plugged BI
vendors included) must answer, every one it excludes must not even resolve,
and the scheduler plane is asked for jobs only when `ops_sched` is plugged.
Pinned here:

- a da/* box (no orchestrator) does not ask the scheduler check; it records
  it under `checks_not_applicable` WITH the reason, and `sched.internal` joins
  the must-not-resolve list - replaced by a stronger check, never switched off
  (until 2026-10-04 the harness popped the failed check after the fact and
  asserted nothing in its place: `container_episode._scheduler_not_served`);
- a box WITH `ops_sched`, and the unprofiled box, still ask it, and a
  scheduler that does not answer still fails containment;
- `spec()` refuses a scheduler-less spec that does not say why or does not
  assert the absence;
- question H: a mount of the services' state dir, by target or by host path,
  fails; so does the host path existing; `service_state_isolation` reads
  `docker inspect` and passes only when native-services alone mounts it,
  read-write.

The probe body is executed for real (`python -` on this host, the way it runs
in the container), with the sweep and the world off; no Docker.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_under_test as aut  # noqa: E402
import container_episode as ce  # noqa: E402
import episode  # noqa: E402

sys.path.insert(0, str(ce.DOCKER_DIR))
import native_episode_probe as nep  # noqa: E402

BI001 = ("da/laptop", {"bi": ["powerbi", "tableau", "sigma"]})


# ------------------------------------------------------------- expectations
def test_bi001_box_serves_its_six_and_excludes_the_rest():
    got = nep.expectations(*BI001)
    assert set(got["services"]) == {"jira", "slack", "confluence",
                                    "powerbi", "tableau", "sigma"}
    assert got["services"]["sigma"] == "http://sigma.internal:8097"
    assert set(got["excluded"]) == {"metabase", "prometheus", "alertmanager", "loki",
                                    "airbyte", "deploy", "sched", "minio"}
    assert got["scheduler"] is None
    reason = got["not_applicable"][nep.SCHEDULER_CHECK]
    assert "ops_sched" in reason and "must instead NOT resolve" in reason


def test_expectations_agree_with_the_episode_endpoint_map():
    """The probe's rules (serve.py) and the harness's (agent_under_test) name
    the same served set, so P7 asks about the addresses the episode was given."""
    for profile, plugs in (BI001, ("de/laptop", {}), ("da/laptop", {}), (None, {})):
        endpoints = aut.profile_endpoints(aut.resolve_endpoints(None),
                                          episode.environment_profile(profile or "", plugs))
        got = nep.expectations(profile, plugs)
        named = {k for k in endpoints
                 if k in {*aut.NATIVE_SERVICES, *aut.PLUG_SERVICES,
                          *aut.NATIVE_WORKSPACE_SERVICES}}
        assert set(got["services"]) == named, profile
        assert {k: endpoints[k] for k in named} == got["services"], profile


def test_an_orchestrator_box_still_asks_the_scheduler():
    got = nep.expectations("de/laptop", {})
    assert got["scheduler"] == nep.SCHEDULER_SERVICE
    assert got["services"]["sched"] == "http://sched.internal:8120"
    assert got["not_applicable"] == {}
    assert "sched" not in got["excluded"] and "minio" not in got["excluded"]


def test_the_unprofiled_box_asks_what_it_always_asked_and_no_vendor_resolves():
    got = nep.expectations(None, ())
    assert set(got["services"]) == {*aut.NATIVE_SERVICES, "sched"}
    assert got["scheduler"] == "sched" and got["not_applicable"] == {}
    assert set(got["excluded"]) == set(aut.PLUG_SERVICES)


def test_spec_refuses_a_silent_scheduler_omission():
    with pytest.raises(ValueError):
        nep.spec(warehouses=[], services={}, scheduler=None)
    with pytest.raises(ValueError):
        nep.spec(warehouses=[], services={}, scheduler=None,
                 not_applicable={nep.SCHEDULER_CHECK: "why"})
    with pytest.raises(ValueError):
        nep.spec(warehouses=[], services={}, scheduler=None,
                 excluded={"sched": "sched.internal"})
    with pytest.raises(ValueError):
        nep.spec(warehouses=[], services={}, scheduler="sched",
                 not_applicable={nep.SCHEDULER_CHECK: "why"})
    ok = nep.spec(warehouses=[], services={}, scheduler=None,
                  excluded={"sched": "sched.internal"},
                  not_applicable={nep.SCHEDULER_CHECK: "why"})
    assert ok["scheduler"] is None and ok["excluded"] == {"sched": "sched.internal"}


def test_the_harness_constant_is_the_probes():
    assert ce.SCHEDULER_CHECK == nep.SCHEDULER_CHECK
    assert ce.SERVICE_STATE_IN_SERVICES == nep.SERVICE_STATE_TARGET
    assert not hasattr(ce, "_scheduler_not_served")


# --------------------------------------------------------- the probe, run
def _run_probe(spec: dict) -> dict:
    spec = {**spec, "sweep_roots": [], "world_planes": [], "warehouses": []}
    program = f"SPEC = {json.dumps(json.dumps(spec))}\n{nep.PROBE}"
    done = subprocess.run([sys.executable, "-"], input=program, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


#: Names no resolver answers (RFC 6761 `.invalid`).
NOWHERE = {"sched": "sched.alkera-p7-test.invalid",
           "metabase": "metabase.alkera-p7-test.invalid"}


def test_probe_records_not_applicable_and_asserts_the_absence(tmp_path):
    expected = nep.expectations(*BI001)
    out = _run_probe(nep.spec(
        warehouses=[], services={}, scheduler=None, excluded=NOWHERE,
        not_applicable=expected["not_applicable"]))
    assert nep.SCHEDULER_CHECK not in out["checks"]
    assert out["checks_not_applicable"] == expected["not_applicable"]
    assert out["checks"]["no_excluded_service_resolves"] is True
    assert out["excluded_that_resolve"] == []


def test_probe_fails_an_excluded_name_that_resolves():
    out = _run_probe(nep.spec(
        warehouses=[], services={}, scheduler=None,
        excluded={"sched": "localhost"},
        not_applicable={nep.SCHEDULER_CHECK: "why"}))
    assert out["checks"]["no_excluded_service_resolves"] is False
    assert out["contained"] is False


def test_probe_still_fails_a_served_scheduler_that_does_not_answer():
    out = _run_probe(nep.spec(
        warehouses=[], services={"sched": "http://127.0.0.1:9/never"},
        scheduler="sched"))
    assert out["checks"][nep.SCHEDULER_CHECK] is False
    assert out["checks_not_applicable"] == {}
    assert out["contained"] is False


def test_probe_service_state_check(tmp_path):
    """Question H, from the vantage: the host path existing here, or a mount
    whose root is that path, fails; an absent path and no mount pass."""
    present = _run_probe(nep.spec(
        warehouses=[], services={},
        service_state={"targets": ["/service-state"], "roots": [str(tmp_path)]}))
    assert present["checks"]["the_services_state_is_out_of_reach"] is False
    assert present["service_state"]["roots_present"] == [str(tmp_path)]
    absent = _run_probe(nep.spec(
        warehouses=[], services={},
        service_state={"targets": ["/service-state-never"],
                       "roots": [str(tmp_path / "never")]}))
    assert absent["checks"]["the_services_state_is_out_of_reach"] is True
    # a mount, found by its root: `/` is the root of this process's own `/`
    mounted = _run_probe(nep.spec(
        warehouses=[], services={},
        service_state={"targets": ["/service-state-never"], "roots": ["/"]}))
    assert mounted["service_state"]["mounts"]
    assert mounted["checks"]["the_services_state_is_out_of_reach"] is False


# ------------------------------------------------- the stack, docker inspect
def _container(service: str, *mounts) -> dict:
    return {"Config": {"Labels": {"com.docker.compose.service": service}},
            "Mounts": [{"Source": src, "Destination": dst, "RW": rw}
                       for src, dst, rw in mounts]}


def test_service_state_isolation(tmp_path):
    state = (tmp_path / "service-state").as_posix()
    good = [_container("native-services", (state, "/service-state", True),
                       ("/x/audit", "/audit", True)),
            _container("episode", ("/x/audit", "/audit", True)),
            _container("jupyter", ("/x/audit", "/audit", True))]
    assert nep.service_state_isolation(good, state)["isolated"] is True
    leaked = [*good, _container("episode", (state + "/bi", "/opt/forge", True))]
    got = nep.service_state_isolation(leaked, state)
    assert got["isolated"] is False and got["mounted_elsewhere"][0]["service"] == "episode"
    by_target = [*good, _container("jupyter", ("volume-x", "/service-state", True))]
    assert nep.service_state_isolation(by_target, state)["isolated"] is False
    read_only = [_container("native-services", (state, "/service-state", False))]
    assert nep.service_state_isolation(read_only, state)["isolated"] is False
    assert nep.service_state_isolation(good[1:], state)["isolated"] is False
    elsewhere = [_container("native-services", ("/other", "/service-state", True))]
    assert nep.service_state_isolation(elsewhere, state)["isolated"] is False
