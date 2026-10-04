"""End to end: export GT001-aov-halving's two arms from a real world build.

Uses stand-in templates (the real ones are packages B and C); a separate test
runs the real harbor/template/ when it exists.
"""
from __future__ import annotations

import json
import re
import tomllib

import pytest

import test_export_support as support
from alkera_harbor import adapter as adapter_mod
from alkera_harbor import checks, digest, dockerenv, worldbuild
from alkera_harbor.adapter import AlkeraHarborAdapter
from alkera_harbor.constants import CANARY_MD, CANARY_TEXT, REPO, TASKS_DIR, TEMPLATE_DIR
from alkera_harbor.reroot import needles_for, scan
from alkera_harbor.validate import validate_dataset, validate_tasks

TASK = "GT001-aov-halving"
ARMS = ("decision", "default")


def _export(out, templates, task_ids, **kw):
    log: list[str] = []
    adapter = AlkeraHarborAdapter(out, task_ids=task_ids, template_dir=templates,
                                  log=log.append, **kw)
    return adapter, adapter.run(), log


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e")
    templates = support.make_standin_templates(root / "template")
    out = root / "dist"
    adapter, report, log = _export(out, templates, [TASK], overwrite=True)
    assert not report.errors, "\n".join(log)
    return adapter, report, out, templates


def _task(out, arm):
    return out / f"gt001-aov-halving__{arm}"


def test_both_arms_generated_and_listed_in_dataset_toml(exported):
    _adapter, report, out, _t = exported
    assert [r.status for r in report.results] == ["ok", "ok"]
    dataset = tomllib.loads((out / "dataset.toml").read_text())
    assert dataset["dataset"]["name"] == "alkera/de-bench"
    assert [t["name"] for t in dataset["tasks"]] == [
        f"alkera/gt001-aov-halving__{arm}" for arm in ARMS]
    for entry in dataset["tasks"]:
        task_dir = out / entry["name"].split("/", 1)[1]
        assert entry["digest"] == digest.task_digest(task_dir)
    manifest = json.loads((out / "manifest.json").read_text())
    assert {t["arm"]: t["role"] for t in manifest["tasks"]} == {
        "decision": "decision", "default": "control"}
    assert "exported_at" in manifest
    assert not (out / ".staging").exists()
    assert (out / "dataset.toml").read_text().startswith("# " + CANARY_TEXT)


@pytest.mark.parametrize("arm", ARMS)
def test_tree_layout(exported, arm):
    _a, _r, out, _t = exported
    task = _task(out, arm)
    for rel in ("task.toml", "instruction.md", "README.md",
                "environment/Dockerfile", "environment/bin/world-up",
                "environment/alkera/world/world/__init__.py",
                "environment/episode/episode.json",
                "environment/episode/world/var/warehouse/warehouse.duckdb",
                "environment/episode/workspace/.git/HEAD",
                "environment/episode/workspace/.envrc",
                "environment/episode/workspace/docs/onboarding.md",
                "environment/episode/workspace/ops/sched",
                "environment/episode/home/.config/alkera/services.toml",
                "tests/Dockerfile", "tests/test.sh", "tests/verify.py",
                "tests/alkera/repo/tasks/harness/taskspec.py",
                f"tests/alkera/repo/tasks/{TASK}/task.toml",
                f"tests/alkera/repo/tasks/{TASK}/tests/grade.py",
                "tests/episode/episode.json", "tests/episode/baselines.json",
                "tests/episode/grading/manifest.yaml",
                "tests/episode/grading/expected.duckdb",
                "tests/episode/before/workspace/.git/HEAD",
                "solution/solve.sh"):
        assert (task / rel).is_file(), rel
    assert not (task / "environment" / "context.txt").exists()
    assert not (task / "tests" / "context.txt").exists()
    assert not (task / "environment/episode/world/workspace").exists()
    assert (task / "environment/episode/workspace/ops/sched").stat().st_mode & 0o111
    assert (task / "solution/solve.sh").stat().st_mode & 0o111
    # the verifier's "before" is the agent's starting tree, byte for byte
    assert support.tree_bytes(task / "environment/episode/workspace") == \
        support.tree_bytes(task / "tests/episode/before/workspace")


@pytest.mark.parametrize("arm", ARMS)
def test_episode_json(exported, arm):
    _a, _r, out, _t = exported
    task = _task(out, arm)
    env_copy = (task / "environment/episode/episode.json").read_bytes()
    assert env_copy == (task / "tests/episode/episode.json").read_bytes()
    episode = json.loads(env_copy)
    assert episode["schema"] == "alkera-harbor-episode/1"
    assert (episode["task"], episode["variant"]) == (TASK, arm)
    assert episode["role"] == ("decision" if arm == "decision" else "control")
    assert episode["agent_home"] == "/home/engineer" and episode["agent_uid"] == 1000
    world = episode["world"]
    assert world["world_root"] == "/world" and world["workspace"] == "/workspace"
    assert world["warehouse"] == "/world/var/warehouse/warehouse.duckdb"
    assert world["grading"] == "/tests/episode/grading"
    assert episode["env"] == {"ALKERA_TASK_VARIANT": arm,
                              "ALKERA_SHIM_WATCH": "/world/var/warehouse/warehouse.duckdb"}
    assert "exported_at" not in json.dumps(episode)
    # what Harbor's packager loses (files only, mode 0644), for B/C to restore
    assert "/workspace/ops/sched" in episode["restore"]["executable_files"]
    assert "/world/build" in episode["restore"]["empty_dirs"]
    assert not any("/.git/" in d for d in episode["restore"]["empty_dirs"])
    toml = tomllib.loads((task / "task.toml").read_text())
    # [environment.env] == every image ENV but PATH (exact values) + per-arm vars
    image_env = dockerenv.parse_env((task / "environment/Dockerfile").read_text())
    toml_env = toml["environment"]["env"]
    assert "PATH" in image_env and "PATH" not in toml_env
    assert toml_env == {**{k: v for k, v in image_env.items() if k != "PATH"},
                        **episode["env"]}
    assert checks.environment_env_problems(task) == []
    assert checks.payload_problems(task) == []
    assert episode["gateway_config"] == "/opt/alkera/gateway.json"
    gateway = json.loads((task / "environment/episode/gateway.json").read_text())
    assert gateway["world_root"] == "/world" and gateway["workspace"] == "/workspace"
    assert gateway["audit_dir"] == "/audit"
    assert gateway["warehouse"] == "/world/var/warehouse/warehouse.duckdb"
    source = (TASKS_DIR / TASK / "tests" / "baselines" / f"{arm}.json").read_bytes()
    assert (task / "tests/episode/baselines.json").read_bytes() == source
    assert episode["anchors"] == "present"


@pytest.mark.parametrize("arm", ARMS)
def test_canary_everywhere(exported, arm):
    _a, _r, out, _t = exported
    task = _task(out, arm)
    assert checks.canary_problems(task) == []
    assert (task / "instruction.md").read_text().split("\n", 1)[0] == CANARY_MD
    lines = (task / "solution/solve.sh").read_text().split("\n")
    assert lines[0].startswith("#!") and CANARY_TEXT in lines[1]


@pytest.mark.parametrize("arm", ARMS)
def test_nothing_of_the_grader_or_oracle_reaches_the_agent_image(exported, arm):
    _a, _r, out, _t = exported
    task = _task(out, arm)
    dockerfile = (task / "environment/Dockerfile").read_text()
    assert not re.search(r"^\s*(COPY|ADD)\b.*\b(tests|solution)\b", dockerfile,
                         re.I | re.M)
    env_files = [p for p in (task / "environment").rglob("*") if p.is_file()]
    env_bytes = {p.read_bytes() for p in env_files}
    graders = list(TASKS_DIR.glob("GT*/tests/grade.py"))
    oracles = [p for p in TASKS_DIR.glob("GT*/solution/*") if p.is_file()]
    assert graders and oracles
    for source in graders + oracles:
        assert source.read_bytes() not in env_bytes, source
    names = {p.name for p in env_files}
    assert not names & {"grade.py", "expected.duckdb", "solve.sh", "solution.sh"}
    assert not any(p.name == "manifest.yaml" and "grading" in p.parts
                   for p in env_files)
    assert not any("solution" in p.relative_to(task).parts[:3] for p in env_files)
    assert checks.environment_problems(task, checks.grading_fingerprints(),
                                       task / "tests/episode/grading") == []


def test_verifier_context_strips_solution_and_prompts_but_keeps_the_brief(exported):
    _a, _r, out, _t = exported
    shipped = _task(out, "decision") / "tests/alkera/repo/tasks" / TASK
    assert not (shipped / "solution").exists()
    assert sorted(p.name for p in (shipped / "prompts").iterdir()) == ["vague.md"]
    assert (shipped / "prompts/vague.md").read_bytes() == \
        (TASKS_DIR / TASK / "prompts/vague.md").read_bytes()


@pytest.mark.parametrize("arm", ARMS)
def test_no_host_path_anywhere(exported, arm):
    _a, _r, out, _t = exported
    from pathlib import Path
    needles = needles_for(repo=REPO, home=Path.home())
    assert scan(_task(out, arm), needles) == []


@pytest.mark.parametrize("arm", ARMS)
def test_readme_and_description_carry_no_author_prose(exported, arm):
    _a, _r, out, _t = exported
    task = _task(out, arm)
    raw = tomllib.loads((TASKS_DIR / TASK / "task.toml").read_text())
    readme = (task / "README.md").read_text()
    toml_text = (task / "task.toml").read_text()
    for prose in [raw["task"]["summary"], *(v["note"] for v in raw["variants"].values())]:
        assert prose[:50] not in readme and prose[:50] not in toml_text
    assert f"(arm: {arm})" in tomllib.loads(toml_text)["task"]["description"]
    assert "Pass criterion" in readme


def test_export_is_deterministic(exported, tmp_path):
    _a, report, _out, templates = exported
    _a2, again, log = _export(tmp_path / "again", templates, [TASK])
    assert not again.errors, "\n".join(log)
    assert {r.name: r.digest for r in again.results} == \
        {r.name: r.digest for r in report.results}


def test_existing_tasks_are_skipped_without_overwrite(exported, tmp_path):
    _a, _r, out, templates = exported
    before = (out / "dataset.toml").read_text()
    _ad, report, _log = _export(out, templates, [f"{TASK}:default"])
    assert [r.status for r in report.results] == ["skipped"]
    assert (out / "dataset.toml").read_text() == before     # still lists both


def test_harbor_loads_every_generated_task_and_agrees_on_the_digest(exported):
    adapter, report, out, _t = exported
    dirs = [out / r.name.split("/", 1)[1] for r in report.results]
    verdicts = validate_tasks(
        dirs, harbor_python=support.harbor_python(),
        instruction_sha256={r.name.split("/", 1)[1]: r.instruction_sha256
                            for r in report.results})
    for r in report.results:
        verdict = verdicts[r.name.split("/", 1)[1]]
        assert verdict["ok"], verdict
        assert verdict["problems"] == [], verdict["problems"]
        assert verdict["digest"] == r.digest
        assert verdict["name"] == r.name
    seen = validate_dataset(out / "dataset.toml", harbor_python=support.harbor_python())
    assert seen["name"] == "alkera/de-bench"
    assert seen["tasks"] == [{"name": r.name, "digest": r.digest} for r in report.results]


def test_a_host_path_that_survives_fails_the_export(tmp_path, monkeypatch):
    """GT007 writes absolute host paths into var/dbt/profiles.yml. With the
    re-rooting disabled they survive, and the leak gate must fail the arm."""
    templates = support.make_standin_templates(tmp_path / "template")
    monkeypatch.setattr(worldbuild.Rerooter, "tree", lambda self, root: [])
    _ad, report, log = _export(tmp_path / "dist", templates,
                               ["GT007-ran-long-or-started-late:default"])
    (result,) = report.results
    assert result.status == "error", log
    assert "release gates failed" in result.reason
    assert "export temp dir" in result.reason
    assert not (tmp_path / "dist" / "gt007-ran-long-or-started-late__default").exists()


def test_template_problems_fail_the_arm(tmp_path):
    templates = support.make_standin_templates(tmp_path / "template")
    dockerfile = templates / "environment" / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + "COPY tests/ /tests/\n")
    _ad, report, _log = _export(tmp_path / "dist", templates, [f"{TASK}:default"])
    assert report.results[0].status == "error"
    assert "bakes tests/solution" in report.results[0].reason


def test_missing_template_fails_the_run_before_any_world_is_built(tmp_path):
    _ad, report, _log = _export(tmp_path / "dist", tmp_path / "nowhere",
                                [f"{TASK}:default"])
    assert report.results == []
    assert any("does not exist" in p for p in report.dataset_problems)


@pytest.mark.skipif(not ((TEMPLATE_DIR / "environment" / "context.txt").is_file()
                         and (TEMPLATE_DIR / "tests" / "context.txt").is_file()),
                    reason="harbor/template/{environment,tests} (packages B and C) "
                           "not present yet")
def test_real_templates_export_gt001_default(tmp_path):
    _ad, report, log = _export(tmp_path / "dist", TEMPLATE_DIR, [f"{TASK}:default"])
    assert not report.errors, "\n".join(log)


def test_module_exports(exported):
    assert adapter_mod.STAGING_DIR == ".staging"


def test_a_template_edited_mid_run_does_not_split_the_dataset(tmp_path):
    """Packages B and C edit their templates while exports run: every task of
    one run is built from one snapshot of the templates."""
    templates = support.make_standin_templates(tmp_path / "template")
    dockerfile = templates / "environment" / "Dockerfile"
    original = dockerfile.read_text()
    log: list[str] = []

    def edit_after_first_arm(line: str) -> None:
        log.append(line)
        if line.startswith("[1/2]"):
            dockerfile.write_text(original + "RUN echo edited-mid-run\n")

    adapter = AlkeraHarborAdapter(tmp_path / "dist", task_ids=[TASK],
                                  template_dir=templates, log=edit_after_first_arm)
    report = adapter.run()
    assert not report.errors and report.dataset_problems == [], "\n".join(log)
    for arm in ARMS:
        assert (tmp_path / "dist" / f"gt001-aov-halving__{arm}" /
                "environment" / "Dockerfile").read_text() == original


def test_static_parts_that_differ_across_one_run_fail_it(exported, monkeypatch):
    adapter, report, _out, _t = exported
    values = iter(["a", "b"])
    monkeypatch.setattr(checks, "static_fingerprint", lambda *_a: next(values))
    copy = adapter_mod.ExportReport(output_dir=report.output_dir,
                                    results=list(report.results))
    adapter._check_static_parts(copy)
    assert copy.dataset_problems and "changed while the export ran" in \
        copy.dataset_problems[0]


def test_static_fingerprint_ignores_the_per_task_payload(exported):
    _a, _r, out, _t = exported
    fps = {checks.static_fingerprint(_task(out, arm), TASK) for arm in ARMS}
    assert len(fps) == 1


def test_gt008_gateway_config_attaches_the_legacy_warehouse(tmp_path):
    """GT008 publishes its second warehouse only in the world manifest; the
    services must get it from gateway.json (serve.py's fixed layout cannot)."""
    templates = support.make_standin_templates(tmp_path / "template")
    _ad, report, log = _export(tmp_path / "dist", templates,
                               ["GT008-warehouse-cutover:default"])
    assert not report.errors, "\n".join(log)
    task = tmp_path / "dist" / "gt008-warehouse-cutover__default"
    text = (task / "environment/episode/gateway.json").read_text()
    gateway = json.loads(text)
    paths = {spec.get("path") for spec in gateway.get("warehouses") or []}
    assert "/world/var/warehouse/legacy.duckdb" in paths, gateway
    assert "/tmp/" not in text and str(REPO) not in text
    from pathlib import Path
    assert scan(task / "environment" / "episode", needles_for(repo=REPO,
                                                              home=Path.home())) == []
    episode = json.loads((task / "tests/episode/episode.json").read_text())
    assert episode["gateway_config"] == "/opt/alkera/gateway.json"
    assert "legacy.duckdb" in episode["env"]["ALKERA_SHIM_WATCH"]
