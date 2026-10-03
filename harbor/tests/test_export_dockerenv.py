"""The image's ENV, mirrored into task.toml [environment.env]."""
from __future__ import annotations

import json

import pytest

import test_export_support  # noqa: F401  (puts harbor/ on sys.path)
from alkera_harbor import checks, dockerenv, tomlw
from alkera_harbor.constants import TEMPLATE_DIR


def test_parse_forms_and_last_assignment_wins():
    text = ("FROM x\n"
            "# ENV COMMENTED=1\n"
            "RUN echo a \\\n    ENV NOT_AN_INSTRUCTION=1 \\\n    && true\n"
            "ENV A=1\n"
            "ENV B=2 C=three\n"
            "env D four words\n"
            "ENV A=override\n"
            'ENV Q="quoted value"\n')
    assert dockerenv.parse_env(text) == {"A": "override", "B": "2", "C": "three",
                                         "D": "four words", "Q": "quoted value"}


@pytest.mark.parametrize("bad", ["ENV A=$HOME\n", "ENV A=${X:-y}\n",
                                 "ENV A=1 \\\n    B=2\n", "ENV =x\n", "ENV A\n"])
def test_non_literal_or_unparseable_env_is_refused(bad):
    with pytest.raises(dockerenv.DockerEnvError):
        dockerenv.parse_env("FROM x\n" + bad)


def test_static_env_mirrors_everything_but_path():
    # the image must set PATH itself (shims first); task.toml never repeats it
    assert dockerenv.static_agent_env("FROM x\nENV A=1\nENV PATH=/bin\n") == {"A": "1"}
    with pytest.raises(dockerenv.DockerEnvError, match="PATH"):
        dockerenv.static_agent_env("FROM x\nENV A=1\n")
    with pytest.raises(dockerenv.DockerEnvError, match="bypass the audited shims"):
        dockerenv.static_agent_env("FROM x\nENV ALKERA_SHIM_BIN=/opt/s\n"
                                   "ENV PATH=/usr/bin:/opt/s\n")
    assert dockerenv.static_agent_env("FROM x\nENV ALKERA_SHIM_BIN=/opt/s\n"
                                      "ENV PATH=/opt/s:/usr/bin\n") == \
        {"ALKERA_SHIM_BIN": "/opt/s"}
    with pytest.raises(dockerenv.DockerEnvError, match="per-arm"):
        dockerenv.static_agent_env("FROM x\nENV PATH=/bin\nENV ALKERA_TASK_VARIANT=x\n")


@pytest.mark.skipif(not (TEMPLATE_DIR / "environment" / "Dockerfile").is_file(),
                    reason="harbor/template/environment (package B) not present yet")
def test_the_real_template_env_has_what_world_up_checks():
    text = (TEMPLATE_DIR / "environment" / "Dockerfile").read_text()
    assert dockerenv.parse_env(text)["PATH"].split(":")[0] == "/opt/alkera/shims"
    env = dockerenv.static_agent_env(text)
    assert "PATH" not in env
    for key in ("ALKERA_SHIM_BIN", "ALKERA_SHIM_WORKSPACE", "ALKERA_WORKSPACE",
                "ALKERA_NATIVE_AUDIT", "ALKERA_SHIM_AUDIT_UID", "SCHED_URL",
                "JIRA_URL", "DBT_PROFILES_DIR"):
        assert env.get(key), key
    assert env["DBT_PROFILES_DIR"] == "/home/engineer/.dbt"
    assert "HOME" not in env        # each user gets its own from passwd


# ------------------------------------------------------------------ the release gate
DOCKERFILE = ("FROM x\nENV ALKERA_SHIM_BIN=/opt/s\nENV SCHED_URL=http://s:1\n"
              "ENV PATH=/opt/s:/usr/bin\n")
PER_ARM = {"ALKERA_TASK_VARIANT": "decision", "ALKERA_SHIM_WATCH": "/world/w.duckdb"}


def _task(tmp_path, toml_env: dict):
    task = tmp_path / "t"
    (task / "environment" / "episode").mkdir(parents=True)
    (task / "environment" / "Dockerfile").write_text(DOCKERFILE)
    (task / "environment" / "episode" / "episode.json").write_text(
        json.dumps({"env": PER_ARM}))
    (task / "task.toml").write_text(tomlw.dumps({"environment": {"env": toml_env}}))
    return task


def test_gate_accepts_exactly_image_env_minus_path_plus_per_arm(tmp_path):
    good = {**dockerenv.static_agent_env(DOCKERFILE), **PER_ARM}
    assert "PATH" not in good
    assert checks.environment_env_problems(_task(tmp_path, good)) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda e: {**e, "PATH": "/opt/s:/usr/bin"}, "sets PATH; it must not"),
    (lambda e: {k: v for k, v in e.items() if k != "SCHED_URL"},
     "lacks SCHED_URL from the image's ENV"),
    (lambda e: {k: v for k, v in e.items() if k != "ALKERA_SHIM_WATCH"},
     "lacks ALKERA_SHIM_WATCH from episode.json env"),
    (lambda e: {**e, "SCHED_URL": "http://elsewhere:1"}, "expected 'http://s:1'"),
    (lambda e: {**e, "ALKERA_TASK_VARIANT": "default"}, "expected 'decision'"),
    (lambda e: {**e, "EXTRA": "1"}, "neither an image ENV"),
])
def test_gate_refuses_any_drift(tmp_path, mutate, needle):
    env = mutate({**dockerenv.static_agent_env(DOCKERFILE), **PER_ARM})
    problems = checks.environment_env_problems(_task(tmp_path, env))
    assert any(needle in p for p in problems), problems
