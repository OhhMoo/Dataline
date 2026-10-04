"""P7's scheduler check under a profile that serves no scheduler plane.

    cd gateway && uv run pytest ../tasks/harness/test_profile_containment.py -q

container_episode._scheduler_not_served: on a box whose profile plugs no
orchestrator (every da/* profile), `sched.internal` is not served by design,
so P7's "the scheduler plane answers with jobs" cannot hold and must not
quarantine a contained episode. Pinned: it is the ONLY check that changes, it
changes only when the plane is absent from the episode's endpoint map, and
`contained` is recomputed from what remains - a real failure still fails.
No Docker.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_under_test as aut  # noqa: E402
import blindness  # noqa: E402
import container_episode as ce  # noqa: E402
import episode  # noqa: E402

CHECKS = {"egress_refused": True, "every_declared_warehouse_is_mounted": True,
          "every_service_is_reachable_from_the_episode": True,
          "ledger_is_present_empty_and_read_only": True,
          "no_canary_and_no_marker_is_readable": True,
          "no_grading_or_rollout_path_exists": True,
          "no_mount_root_names_the_repository": True,
          "the_episode_can_still_work": True,
          ce.SCHEDULER_CHECK: False,
          "world_holds_only_the_mounted_warehouses": True}


def _evidence(**overrides) -> dict:
    checks = {**CHECKS, **overrides}
    return {"checks": checks, "contained": all(checks.values()), "egress_blocked": True}


def _endpoints(profile: str | None, plugs: dict | None = None) -> dict:
    return aut.profile_endpoints(aut.resolve_endpoints(None),
                                 episode.environment_profile(profile or "", plugs or {}))


def test_da_profile_serves_no_scheduler_and_the_check_is_not_applicable():
    endpoints = _endpoints("da/laptop", {"bi": ["powerbi", "tableau", "sigma"]})
    assert "sched" not in endpoints
    evidence = ce._scheduler_not_served(_evidence(), endpoints, "da/laptop")
    assert ce.SCHEDULER_CHECK not in evidence["checks"]
    assert ce.SCHEDULER_CHECK in evidence["checks_not_applicable"]
    assert evidence["contained"] is True
    faults = blindness.containment_faults(
        [{**evidence, "when": "before_episode"}, {**evidence, "when": "after_episode"}]) \
        if hasattr(blindness, "containment_faults") else None
    if faults is not None:
        assert faults == []


def test_another_failure_still_fails_under_the_profile():
    endpoints = _endpoints("da/laptop")
    evidence = ce._scheduler_not_served(_evidence(egress_refused=False), endpoints,
                                        "da/laptop")
    assert evidence["contained"] is False
    assert evidence["checks"]["egress_refused"] is False


def test_unprofiled_stack_is_unchanged():
    endpoints = _endpoints(None)
    assert "sched" in endpoints
    before = _evidence()
    after = ce._scheduler_not_served(copy.deepcopy(before), endpoints, "")
    assert after == before and after["contained"] is False


def test_a_scheduler_that_answers_is_left_alone():
    endpoints = _endpoints("da/laptop")
    before = _evidence(**{ce.SCHEDULER_CHECK: True})
    assert ce._scheduler_not_served(copy.deepcopy(before), endpoints, "da/laptop") == before
