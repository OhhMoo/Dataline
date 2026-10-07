"""The Airflow plug in the harness: what a box that plugs it gets, what an
unprofiled box keeps, and how a rollout's audit explains what it changed.

    gateway/.venv/bin/python -m pytest tasks/harness/test_airflow_wiring.py -q

- An unprofiled box's AIRFLOW_* (the static table, 8080) are untouched, and so
  are the unprofiled and da/laptop overlays (tasks/harness/testdata goldens).
- `de/laptop --plug orchestrator=airflow` gets the row's three variables in
  place, at 8100, an `airflow.internal` alias on native-services, the endpoint
  in its allowlist, no SCHED_URL and no `./ops/sched`.
- A ledger change after an `airflow.dag_trigger` / `airflow.task_clear` row is
  the agent's (blindness.SCHEDULER_TOOLS); after a read, it is not explained.
No docker, no model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_under_test as aut  # noqa: E402
import blindness  # noqa: E402
import container_episode as ce  # noqa: E402
import native_stack  # noqa: E402
import test_backends as golden  # noqa: E402
from alkera_gateway import backends  # noqa: E402

PLUGS = {"orchestrator": ["airflow"]}
URL = "http://airflow.internal:8100/api/v2"
STATIC = "http://airflow.internal:8080/api/v2"
IMAGE_PATH = golden.IMAGE_PATH


def _stack(tmp_path, name, **kw):
    world = golden._world(tmp_path / name, "2026-03-02T00:00:00Z", "")
    return ce.NativeContainerEpisode(project="t", world=world,
                                     audit_dir=tmp_path / name / "audit",
                                     rollout_dir=tmp_path / name / "rollout", **kw)


@pytest.fixture
def no_docker(monkeypatch):
    monkeypatch.setattr(ce, "image_path", lambda image: IMAGE_PATH)


# ------------------------------------------------------- unprofiled: unchanged
def test_unprofiled_airflow_env_unchanged(tmp_path, no_docker):
    static = ce.static_environment("episode")
    assert static["AIRFLOW_API_URL"] == STATIC
    sys.path.insert(0, str(HERE.parent.parent / "world"))
    from world import native_workspace as nw

    assert nw.environment()["AIRFLOW_API_URL"] == STATIC
    assert "airflow.internal:8100" not in nw.envrc_block()
    # the unprofiled and da/laptop overlays are the pre-registry bytes
    for name in ("unprofiled", "da_laptop"):
        assert golden.render(name, tmp_path / name) == golden._golden(name)
    unprofiled = _stack(tmp_path, "u").write_overlay().read_text("utf-8")
    assert "8100" not in unprofiled
    # and an ops_sched box (de/laptop's default) names no Airflow at all
    ops = _stack(tmp_path, "o", profile="de/laptop").write_overlay().read_text("utf-8")
    ops_env = golden._overlay_yaml(ops)["services"]["episode"]["environment"]
    assert "AIRFLOW_API_URL" not in ops_env and "8100" not in ops
    assert ("airflow.internal", 8100) not in aut.allowed_destinations(
        _stack(tmp_path, "o2", profile="de/laptop").episode_endpoints())


# --------------------------------------------------- the box that plugs it
def test_a_box_that_plugs_airflow(tmp_path, no_docker):
    stack = _stack(tmp_path, "a", profile="de/laptop", plugs=PLUGS)
    text = stack.write_overlay().read_text("utf-8")
    overlay = golden._overlay_yaml(text)
    env = overlay["services"]["episode"]["environment"]
    assert env["AIRFLOW_API_URL"] == URL and text.count("AIRFLOW_API_URL:") == 1
    assert env["AIRFLOW_USER"] == env["AIRFLOW_PASSWORD"] == backends.FAKE_CREDENTIAL
    assert STATIC not in text and "SCHED_URL" not in env
    assert "REQUESTS_CA_BUNDLE" not in env                     # not a TLS backend
    aliases = overlay["services"]["native-services"]["networks"]["episode_net"]["aliases"]
    assert "airflow.internal" in aliases and "sched.internal" not in aliases
    assert "airflow" not in overlay["services"]                 # hosted: no container
    endpoints = stack.episode_endpoints()
    assert endpoints["airflow"] == "http://airflow.internal:8100"
    allowed = aut.allowed_destinations(endpoints)
    assert ("airflow.internal", 8100) in allowed and ("airflow.internal", 8080) not in allowed
    assert ("sched.internal", 8120) not in allowed
    # the profile's image carries the airflow client set (airflowctl)
    assert ce.profile_image(ce.DEFAULT_IMAGE, stack.environment_profile()) == \
        "alkera-episode:head-airflow"
    # serve.py is started with the profile, which mounts the hosted module
    assert "--plug orchestrator=airflow" in stack.compose_env()["ALKERA_NATIVE_PROFILE_ARGS"]
    assert "airflow" not in stack.compose_env().get("COMPOSE_PROFILES", "").split(",")


def test_a_box_that_plugs_airflow_gets_no_ops_sched(tmp_path):
    world = golden._world(tmp_path / "w", "2026-03-02T00:00:00Z", "")
    root = Path(world["world_root"])
    (root / "var" / "world").mkdir(parents=True)
    (root / "var" / "world" / "profile.json").write_text(json.dumps(
        {"name": "de/laptop", "slots": ["files", "warehouse", "transform", "orchestrator"],
         "plugs": PLUGS}), "utf-8")
    result = native_stack.install_workspace_cli(world, "container")
    assert result["cli"] is None and "airflow" in result["skipped"]
    assert not (root / "workspace" / native_stack.SCHED_CLI_PATH).exists()


# ------------------------------------------------- the audit explains it
def _layout(root: Path) -> dict:
    return {"world_root": str(root), "workspace": str(root / "workspace"),
            "warehouse": str(root / "var" / "warehouse" / "warehouse.duckdb"),
            "scheduler": str(root / "var" / "sched" / "state.json"),
            "profiles_dir": str(root / "build")}


def _row(tool: str, op_class: str, targets: list[str]) -> dict:
    return {"tool": tool, "op_class": op_class, "targets": targets, "exit": "ok"}


@pytest.mark.parametrize("row", [
    _row("airflow.dag_trigger", "exec", ["scheduler:commerce_nightly", "dbt_project"]),
    _row("airflow.task_clear", "destructive",
         ["scheduler:commerce_nightly", "scheduler:scheduled__2026-03-01T02:30:00+00:00"]),
])
def test_trigger_row_attributed_by_blindness(tmp_path, row):
    """The queue lands in var/sched/state.json, and the grader's advance later
    writes the run logs and observability beside it: all explained by the row."""
    assert row["tool"] in blindness.SCHEDULER_TOOLS
    root = tmp_path / "world"
    root.mkdir()
    before = {"var/sched/state.json": "a"}
    after = {"var/sched/state.json": "b", "build/sched/logs/run-000010.log": "c"}
    assert blindness._unattributed(before, after, [row], _layout(root)) == []
    # the same change with only reads in the log is off-channel
    reads = [_row("airflow.dag_list", "read", ["scheduler"]),
             _row("airflow.dagrun_get", "read", ["scheduler:run"])]
    assert blindness._unattributed(before, after, reads, _layout(root)) == [
        "build/sched/logs/run-000010.log", "var/sched/state.json"]
    # and the live-world control plane is never the agent's, trigger or not
    live = {**after, "var/world/live.json": "d"}
    assert blindness._unattributed(before, live, [row], _layout(root)) == [
        "var/world/live.json"]
