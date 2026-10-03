"""task.toml: our writer, the contract's shape, and Harbor's own TaskConfig."""
from __future__ import annotations

import tomllib

import pytest

import test_export_support as support
from alkera_harbor import harness, render, tomlw
from alkera_harbor.constants import CANARY_HASH
from alkera_harbor.naming import ArmRef


# ------------------------------------------------------------------ the writer
def test_writer_round_trips_and_escapes():
    doc = {"a": 'q"\\\n\t\x01é', "n": 3, "f": 1800.0, "b": False, "l": ["x", 2],
           "t": {"inline": {"k": "v"}, "empty": {}, "deep": {"x": {"y": 1}}},
           "arr": [{"c": "one"}, {"c": "two"}]}
    text = tomlw.dumps(doc, inline=("t.inline",))
    assert tomllib.loads(text) == doc
    assert "inline = { k = \"v\" }" in text
    assert "[t.empty]" in text            # an empty table is kept
    assert "[t.deep]" not in text         # header-only tables are implied
    assert text.count("[[arr]]") == 2


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), object()])
def test_writer_rejects_unrepresentable(bad):
    with pytest.raises(tomlw.TomlError):
        tomlw.dumps({"x": bad})


# ------------------------------------------------------------------ the document
def _document(task="GT001-aov-halving", arm="decision", reward="pass",
              network="no-network"):
    spec = harness.load_spec(task, arm)
    ref = ArmRef(task, arm)
    env = {"ALKERA_TASK_VARIANT": arm,
           "ALKERA_SHIM_WATCH": "/world/var/warehouse/warehouse.duckdb"}
    doc = render.task_document(ref=ref, spec=spec, role=harness.role_of(spec),
                               reward_mode=reward, agent_network=network, env=env,
                               source_commit="0" * 40, source_dirty=False,
                               oracle="native")
    return spec, doc, render.task_toml(doc)


def test_task_toml_shape_follows_the_contract():
    spec, doc, text = _document()
    assert text.splitlines()[0] == CANARY_HASH
    parsed = tomllib.loads(text)
    assert parsed == doc
    assert set(parsed) == {"schema_version", "artifacts", "task", "metadata", "agent",
                           "verifier", "environment", "solution"}
    assert parsed["schema_version"] == "1.4"
    assert parsed["artifacts"] == ["/world", "/workspace", "/audit"]
    task = parsed["task"]
    assert task["name"] == "alkera/gt001-aov-halving__decision"
    assert task["version"] == spec.raw["task"]["version"]
    assert task["description"] == "Average order value looks wrong (arm: decision)"
    assert task["authors"] == [{"name": "Alkera"}]
    assert parsed["agent"] == {"timeout_sec": 5400.0, "user": "engineer",
                               "network_mode": "no-network"}
    verifier = parsed["verifier"]
    assert verifier["environment_mode"] == "separate"
    assert verifier["network_mode"] == "no-network"
    assert verifier["environment"]["network_mode"] == "no-network"
    assert verifier["collect"] == [{"command": "/opt/alkera/bin/world-down",
                                    "service": "main", "timeout_sec": 120.0,
                                    "user": "root"}]
    env = parsed["environment"]
    assert env["memory_mb"] == 4096 and env["storage_mb"] == 10240
    assert env["network_mode"] == "public" and env["workdir"] == "/workspace"
    assert env["healthcheck"]["command"] == "/opt/alkera/bin/world-up"
    assert env["env"]["ALKERA_TASK_VARIANT"] == "decision"
    assert parsed["solution"] == {"env": {"ALKERA_TASK_VARIANT": "decision"}}
    meta = parsed["metadata"]
    for key in ("category", "subcategory", "tags", "difficulty",
                "expert_time_estimate_hours", "author_name", "alkera_task",
                "alkera_arm", "alkera_role", "reward_mode", "max_tool_calls", "seed",
                "virtual_now", "source_commit", "oracle"):
        assert key in meta, key
    assert meta["expert_time_estimate_hours"] == 14.0      # "8-20" midpoint
    assert meta["difficulty"] == "hard"
    assert meta["alkera_role"] == "decision" and meta["max_tool_calls"] == 600


def test_author_prose_that_names_the_defects_is_never_shipped():
    """F202: [task].summary and every arm note enumerate the planted defects."""
    spec, _doc, text = _document()
    summary = spec.raw["task"]["summary"]
    notes = [v["note"] for v in spec.raw["variants"].values()]
    readme = render.readme(ref=ArmRef("GT001-aov-halving", "decision"), spec=spec,
                           role="decision", reward_mode="pass", oracle="native",
                           anchors="present", agent_network="no-network", n_checks=19)
    for prose in [summary, *notes]:
        for fragment in (prose[:60], prose[-60:]):
            assert fragment not in text and fragment not in readme


def test_public_agent_network_and_training_mode():
    _spec, doc, _text = _document(arm="default", reward="training", network="public")
    assert doc["agent"]["network_mode"] == "public"
    assert doc["metadata"]["reward_mode"] == "training"
    assert doc["metadata"]["alkera_role"] == "control"


def test_env_value_with_dollar_is_refused():
    spec = harness.load_spec("GT001-aov-halving", "default")
    with pytest.raises(render.RenderError, match="templated"):
        render.task_document(ref=ArmRef("GT001-aov-halving", "default"), spec=spec,
                             role="control", reward_mode="pass",
                             agent_network="no-network", env={"X": "$HOME"},
                             source_commit="x", source_dirty=False, oracle="native")


# ------------------------------------------------------------------ Harbor itself
STRICT = r'''
import json, sys, tomllib
from harbor.models.task.config import TaskConfig

def unknown(raw, dumped, path=""):
    out = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            p = f"{path}.{k}" if path else k
            if not isinstance(dumped, dict) or k not in dumped:
                out.append(p)
            elif p != "metadata" and k != "env":
                out += unknown(v, dumped[k], p)
    elif isinstance(raw, list) and isinstance(dumped, list):
        for i, (a, b) in enumerate(zip(raw, dumped)):
            out += unknown(a, b, f"{path}[{i}]")
    return out

out = {}
for name, text in json.loads(sys.stdin.read()).items():
    cfg = TaskConfig.model_validate_toml(text)
    out[name] = {
        "unknown": unknown(tomllib.loads(text), cfg.model_dump(mode="json", exclude_unset=True)),
        "agent_user": cfg.agent.user, "agent_net": cfg.agent.network_mode.value,
        "verifier_mode": cfg.verifier.environment_mode.value,
        "verifier_env_net": cfg.verifier.environment.network_mode.value,
        "collect": [(c.command, c.user) for c in cfg.verifier.collect],
        "health": cfg.environment.healthcheck.command,
        "artifacts": cfg.artifacts, "name": cfg.task.name,
    }
print(json.dumps(out))
'''


def test_harbor_taskconfig_accepts_every_key_we_write():
    texts = {}
    for task, arm in (("GT001-aov-halving", "decision"), ("GT008-warehouse-cutover", "default"),
                      ("GT004-doubling-window", "no-ticket")):
        texts[f"{task}:{arm}"] = _document(task, arm)[2]
    result = support.run_harbor(STRICT, texts)
    for name, verdict in result.items():
        assert verdict["unknown"] == [], (name, verdict["unknown"])
        assert verdict["agent_user"] == "engineer"
        assert verdict["agent_net"] == "no-network"
        assert verdict["verifier_mode"] == "separate"
        assert verdict["verifier_env_net"] == "no-network"
        assert verdict["collect"] == [["/opt/alkera/bin/world-down", "root"]]
        assert verdict["health"] == "/opt/alkera/bin/world-up"
        assert verdict["artifacts"] == ["/world", "/workspace", "/audit"]


def test_the_strict_check_does_catch_an_unknown_key():
    _spec, _doc, text = _document()
    bad = text.replace("[agent]\n", "[agent]\nnot_a_harbor_field = 1\n", 1)
    bad = bad.replace("schema_version = \"1.4\"\n", "schema_version = \"1.4\"\nbogus = 2\n")
    verdict = support.run_harbor(STRICT, {"bad": bad})["bad"]
    assert sorted(verdict["unknown"]) == ["agent.not_a_harbor_field", "bogus"]


@pytest.mark.parametrize("reward", ["pass", "training"])
def test_readme_tells_the_truth_about_unmeasured_and_damage(reward):
    """Harbor counts a trial without a reward as 0 (metrics/base.py
    aggregate_reward_dicts, utils/pass_at_k.py): the README must not promise
    that it drops out of the denominators. A destroyed warehouse is damage."""
    spec = harness.load_spec("GT001-aov-halving", "decision")
    text = render.readme(ref=ArmRef("GT001-aov-halving", "decision"), spec=spec,
                         role="decision", reward_mode=reward, oracle="native",
                         anchors="present", agent_network="no-network", n_checks=19)
    flat = " ".join(text.split())
    assert "denominator" not in flat
    assert "count that trial as 0" in flat
    assert "failures the agent cannot cause" in flat
    assert "destroyed or corrupted is damage too" in flat
    assert "an `unmeasured` trial writes no reward" in flat or \
        "an `unmeasured` trial writes none" in flat
