"""Exporting beyond GT: every task package, profiles as image variants.

* `harness.task_ids` lists every task package (GT001-GT009, BI001, BI002),
  never `tasks/harness`, `tasks/contracts`, `tasks/stressors`.
* No task package file, BI graders and oracles included, can enter the agent
  image (the context forbid rule and the content fingerprints).
* The variant key is (role, clients, profile with its plugs). The UNPROFILED
  variant renders the template verbatim: the GT Dockerfile and its ENV do not
  move. The da/laptop variant's ENV is the profile's environment
  (`world.native_workspace.environment`), its role lock and kernelspec are
  installed, and it passes every lint the template passes.

    gateway/.venv/bin/python -m pytest -q harbor/tests/test_export_variant.py
"""
from __future__ import annotations

import dataclasses
import re
import subprocess

import pytest

import test_env_dockerfile as tdf
import test_export_support  # noqa: F401 - puts harbor/ on sys.path
from alkera_harbor import adapter, checks, dockerenv, harness, variant
from alkera_harbor.constants import REPO, TEMPLATE_DIR
from alkera_harbor.naming import ArmRef

TEMPLATE = TEMPLATE_DIR / "environment" / "Dockerfile"
BI001 = "BI001-revenue-disagrees-across-bi"
BI002 = "BI002-powerbi-share-and-stale-refresh"


def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def template_env() -> dict[str, str]:
    return dockerenv.parse_env(template_text())


def variant_for(task: str, arm: str = "default") -> variant.Variant:
    return variant.variant_of(harness.load_spec(task, arm))


@pytest.fixture(scope="module")
def bi() -> variant.Variant:
    return variant_for(BI001)


@pytest.fixture(scope="module")
def bi_dockerfile(bi) -> str:
    return variant.render_dockerfile(template_text(), bi, template_env())


# ------------------------------------------------------------- task ids
def test_task_ids_include_bi001_bi002():
    ids = harness.task_ids()
    assert BI001 in ids and BI002 in ids
    assert [t for t in ids if t.startswith("GT")] == sorted(
        p.name for p in (REPO / "tasks").glob("GT*") if (p / "task.toml").is_file())
    assert not {"harness", "contracts", "stressors", "README.md"} & set(ids)
    assert all(harness.is_task_package(REPO / "tasks" / t) for t in ids)


def test_a_package_is_any_dir_with_a_task_toml(tmp_path):
    """No naming rule to fail open: a package called anything is a package,
    for the listing and for the grading fingerprints alike."""
    for name in ("GT001-x", "odd_name", "BI9-short"):
        (tmp_path / name / "tests").mkdir(parents=True)
        (tmp_path / name / "task.toml").write_text("[task]\n")
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "episode.py").write_text("")
    assert harness.task_ids(tmp_path) == ["BI9-short", "GT001-x", "odd_name"]


def test_every_bi_arm_is_selectable():
    refs = adapter.AlkeraHarborAdapter(output_dir=REPO / "harbor" / "dist" / "x",
                                       task_ids=["BI001", "BI002"]).selected()
    assert [r.selector for r in refs] == [
        f"{BI001}:decision", f"{BI001}:default", f"{BI002}:decision", f"{BI002}:default"]


# ------------------------------------------------------------- the leak gate
def test_bi_grading_files_forbidden_in_environment():
    for rel in (f"tasks/{BI001}/tests/grade.py", f"tasks/{BI002}/solution/solve.sh",
                f"tasks/{BI001}/fixtures/seed.yaml", "tasks/GT001-aov-halving/task.toml",
                "tasks/a_package_named_otherwise/tests/grade.py",
                "tasks/harness/episode.py"):
        assert adapter._forbid_in_environment(rel), rel
    assert adapter._forbid_in_environment("gateway/requirements-da.lock") is None
    assert adapter._forbid_in_environment("world/world/build.py") is None
    fingerprints = set(checks.grading_fingerprints().values())
    for task in (BI001, BI002):
        assert f"tasks/{task}/tests/grade.py" in fingerprints
        assert f"tasks/{task}/solution/solve.sh" in fingerprints


# ------------------------------------------------------------- unprofiled
def test_unprofiled_dockerfile_byte_identical():
    text = template_text()
    assert variant.render_dockerfile(text, variant.UNPROFILED, template_env()) == text
    for task in ("GT001-aov-halving", "GT008-warehouse-cutover"):
        assert variant_for(task) == variant.UNPROFILED
    exporter = adapter.AlkeraHarborAdapter(output_dir=REPO / "harbor" / "dist" / "x")
    assert exporter.dockerfile().encode("utf-8") == TEMPLATE.read_bytes()
    assert exporter.static_env() == dockerenv.static_agent_env(text)
    assert variant.UNPROFILED.context_files() == []


def test_unprofiled_environment_rule_reproduces_the_template_block():
    """The rule a variant's ENV block is computed by gives, for the unprofiled
    box, exactly the template's block: one rule, both boxes."""
    env = template_env()
    block = {k: v for k, v in env.items()
             if k not in ("PYTHONUTF8", "TZ", "PYTHONDONTWRITEBYTECODE",
                          "PYTHONUNBUFFERED", "PYTHONPATH")
             and not variant._kept(k)}
    assert variant.environment(variant.UNPROFILED, env) == block
    assert list(variant.environment(variant.UNPROFILED, env))[-1] == "REQUESTS_CA_BUNDLE"


# ------------------------------------------------------------- da/laptop
def test_bi_variant_key(bi):
    assert bi.profiled and bi.role == "da" and bi.clients == ()
    assert bi.profile == "da/laptop"
    assert bi.plugs == (("bi", ("powerbi", "sigma", "tableau")),)
    assert bi.key == "da/laptop+bi=powerbi,sigma,tableau [role da, clients none]"
    assert bi.context_files() == ["gateway/requirements-da.lock"]
    assert set(bi.services) == {"powerbi", "tableau", "sigma", "jira", "slack",
                                "confluence"}


def test_bi_variant_env_equals_profile_environment(bi, bi_dockerfile):
    from alkera_gateway.plugs import resolve
    from world import native_workspace as nw

    env = dockerenv.parse_env(bi_dockerfile)
    assert env["POWERBI_API_URL"] == "http://powerbi.internal:8095"
    assert env["TABLEAU_SERVER_URL"] == "http://tableau.internal:8096"
    assert env["SIGMA_BASE_URL"] == "http://sigma.internal:8097"
    assert env["TABLEAU_SITE"] == ""
    assert not [k for k in env if k.startswith(("PG", "SNOWFLAKE_", "AWS_", "AIRFLOW_"))]
    for gone in ("SCHED_URL", "METABASE_URL", "PROMETHEUS_URL", "LOKI_URL", "DEPLOY_URL",
                 "AIRBYTE_URL", "ALERTMANAGER_URL"):
        assert gone not in env, gone
    plugs = {"bi": ["powerbi", "tableau", "sigma"]}
    profile = resolve("da/laptop", plugs)
    expected = nw.environment(profile.slots, nw.profile_plugs("da/laptop", plugs),
                              home="/home/engineer")
    discovery = {k: v for k, v in env.items()
                 if k in expected or k in template_env() and not variant._kept(k)
                 and k not in ("PYTHONUTF8", "TZ", "PYTHONDONTWRITEBYTECODE",
                               "PYTHONUNBUFFERED", "PYTHONPATH")}
    discovery.pop("REQUESTS_CA_BUNDLE")
    assert discovery == expected
    # what the image itself sets is untouched: the audit variables and PATH
    base = template_env()
    for name in [n for n in base if variant._kept(n)] + ["PYTHONPATH", "REQUESTS_CA_BUNDLE"]:
        assert env[name] == base[name], name


def test_bi_variant_is_the_world_envrc(bi):
    """The box's ENV is what BI001's own committed .envrc exports, but for
    DBT_PROFILES_DIR (the agent's HOME is /home/engineer: harbor/README.md)."""
    from world import native_workspace as nw

    plugs = bi.plug_map()
    envrc = nw.environment(bi.slot_map(), nw.profile_plugs(bi.profile, plugs))
    box = variant.environment(bi, template_env())
    assert {k: v for k, v in envrc.items() if k != "DBT_PROFILES_DIR"} == \
        {k: v for k, v in box.items() if k not in ("DBT_PROFILES_DIR", "REQUESTS_CA_BUNDLE")}
    assert envrc["DBT_PROFILES_DIR"] == "/root/.dbt"
    assert box["DBT_PROFILES_DIR"] == "/home/engineer/.dbt"


def test_arms_share_their_tasks_variant_and_the_plugs_are_in_the_key(bi, bi_dockerfile):
    """Both arms of a task share one Dockerfile. BI002 plugs Power BI alone
    (`bi = ["powerbi"]`), so it is ANOTHER variant of da/laptop - why the key
    carries the plugs - and its box names no Tableau or Sigma; the two differ
    only in the header comment and the ENV block, so they share every
    build-cache layer up to it."""
    other = variant_for(BI001, "decision")
    assert other == bi
    assert variant.render_dockerfile(template_text(), other, template_env()) == bi_dockerfile
    bi002 = variant_for(BI002)
    assert variant_for(BI002, "decision") == bi002
    assert bi002 != bi and bi002.key == "da/laptop+bi=powerbi [role da, clients none]"
    text = variant.render_dockerfile(template_text(), bi002, template_env())
    env = dockerenv.parse_env(text)
    assert env["POWERBI_API_URL"] and not [k for k in env
                                           if k.startswith(("TABLEAU_", "SIGMA_"))]
    layers = [s for s in tdf.instructions(text) if s[0] != "ENV"]
    assert layers == [s for s in tdf.instructions(bi_dockerfile) if s[0] != "ENV"]


def test_bi_variant_installs_its_role(bi_dockerfile):
    steps = tdf.instructions(bi_dockerfile)
    copies = [rest for k, rest in steps if k == "COPY"]
    assert "alkera/gateway/requirements-da.lock /opt/gateway/requirements-da.lock" in copies
    runs = [rest for k, rest in steps if k == "RUN"]
    extension = runs.index(next(r for r in runs if "INSTALL postgres" in r))
    assert "requirements-da.lock" in runs[extension + 1]
    assert "install_audited_kernelspecs('/usr/local/share/jupyter'" in runs[extension + 2]
    assert bi_dockerfile.splitlines()[0] == tdf.CANARY
    assert "# VARIANT da/laptop+bi=powerbi,sigma,tableau" in bi_dockerfile


def _apt_pins(run: str) -> list[str]:
    return run.split("apt-get install -y --no-install-recommends", 1)[1] \
        .split(";", 1)[0].split()


def test_bi_variant_installs_chromium_shell(bi, bi_dockerfile):
    """A da box carries Playwright's headless Chromium (plan decision D5), as the
    measured episode image installs it for ALKERA_TOOLBELT=da: its shared libraries
    from the frozen snapshot, the browser the role lock's Playwright pins, checked
    by the same sha256 per arch, and linked where Playwright looks by default for
    the agent (engineer) and root - so the box's ENV, and task.toml's mirror of it,
    stay the profile's. After the kernelspec, so every layer before it is the
    role's as before. A de variant gets none of it, and the template none."""
    text = bi_dockerfile
    runs = [rest for k, rest in tdf.instructions(text) if k == "RUN"]
    kernel = runs.index(next(r for r in runs if "install_audited_kernelspecs" in r))
    browser = runs[kernel + 1]
    assert "PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright python -m playwright install " \
           "--only-shell chromium" in browser
    pins = _apt_pins(browser)
    assert pins and all("=" in p for p in pins), pins
    episode = next(rest for k, rest in tdf.instructions(tdf.EPISODE_DOCKERFILE.read_text(
        encoding="utf-8")) if k == "RUN" and "playwright install --only-shell" in rest)
    assert pins == _apt_pins(episode)                  # one set of pins, two images
    digests = set(re.findall(r"\b[0-9a-f]{64}\b", browser))
    assert len(digests) == 4 and digests == set(re.findall(r"\b[0-9a-f]{64}\b", episode))
    assert "/opt/ms-playwright/chromium_headless_shell-1243/" in browser
    assert "/opt/ms-playwright/chromium_headless_shell-1243/" in episode
    for home in ("/home/engineer", "/root"):
        assert f"{home}/.cache/ms-playwright" in browser
    assert "chromium.launch()" in browser and "about:blank" in browser
    assert "PLAYWRIGHT_BROWSERS_PATH" not in dockerenv.parse_env(text)
    assert "playwright" not in template_text()
    de = variant.render_dockerfile(template_text(), dataclasses.replace(bi, role="de"),
                                   template_env())
    assert "playwright" not in de and "ms-playwright" not in de


def test_a_variant_reuses_every_network_layer_of_the_unprofiled_image(bi_dockerfile):
    """Everything up to and including the DuckDB extension layer (apt, the
    toolbelt, the gateway locks and wheel, the extension: every step that
    fetches from the network) is the template's, instruction for instruction,
    so a variant builds from the unprofiled image's cache and fetches only its
    role's lock."""
    steps, template = tdf.instructions(bi_dockerfile), tdf.instructions()
    cut = next(i for i, (k, rest) in enumerate(template)
               if k == "RUN" and "INSTALL postgres" in rest) + 1
    assert steps[:cut] == template[:cut]
    assert steps[cut][0] == "COPY" and "requirements-da.lock" in steps[cut][1]


def test_bi_variant_passes_the_template_lints(bi_dockerfile):
    """Every static rule test_env_dockerfile.py holds the template to."""
    steps = tdf.instructions(bi_dockerfile)
    assert not [k for k, _ in steps if k == "ARG"]
    assert "${" not in bi_dockerfile
    assert not re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\}", bi_dockerfile)
    assert not re.search(r"GT0\d\d|BI0\d\d|__default|__decision", bi_dockerfile)
    froms = [rest for k, rest in steps if k == "FROM"]
    assert froms == [rest for k, rest in tdf.instructions() if k == "FROM"]
    assert checks.dockerfile_problems.__name__        # the release gate below
    e2b_pair = re.compile(r"(\w+)=([^\s]*(?:\s+(?!\w+=)[^\s]*)*)")
    for keyword, rest in steps:
        assert keyword != "ADD"
        if keyword in ("COPY", "RUN"):
            assert not rest.startswith("--")
        if keyword == "RUN":
            assert "<<" not in rest
            for command in re.split(r"&&|;", rest):
                if "pip install" in command:
                    local = "--no-deps" in command and ".whl" in command
                    assert "--require-hashes" in command or local, command
                    assert " --retries " not in command and " --timeout " not in command
        if keyword == "ENV":
            assert e2b_pair.findall(rest) == [tuple(rest.split("=", 1))], rest
    # the per-task block and what follows are the template's
    first = next(i for i, (k, rest) in enumerate(steps)
                 if k == "COPY" and rest.startswith("episode/"))
    template_steps = tdf.instructions()
    t_first = next(i for i, (k, rest) in enumerate(template_steps)
                   if k == "COPY" and rest.startswith("episode/"))
    assert steps[first:] == template_steps[t_first:]
    assert dockerenv.static_agent_env(bi_dockerfile)       # PATH rules hold


VERIFIER_TEMPLATE = TEMPLATE_DIR / "tests" / "Dockerfile"


def test_unprofiled_verifier_dockerfile_byte_identical():
    text = VERIFIER_TEMPLATE.read_text(encoding="utf-8")
    assert variant.render_tests_dockerfile(text, variant.UNPROFILED, template_text()) == text
    exporter = adapter.AlkeraHarborAdapter(output_dir=REPO / "harbor" / "dist" / "x")
    assert exporter.tests_dockerfile().encode("utf-8") == VERIFIER_TEMPLATE.read_bytes()


def test_profiled_verifier_has_the_agent_images_git(bi):
    """The BI graders read the agent's repository with the git CLI (`git
    archive HEAD`, branches), which the verifier image lacked: in Harbor they
    graded every repository as unreadable (T2/S2 0) or crashed (decision
    arms). A profiled verifier adds git right after the pip layer, from the
    agent image's own frozen snapshot and pin."""
    text = VERIFIER_TEMPLATE.read_text(encoding="utf-8")
    rendered = variant.render_tests_dockerfile(text, bi, template_text())
    steps = tdf.instructions(rendered)
    base = tdf.instructions(text)
    pip = next(i for i, (k, rest) in enumerate(base)
               if k == "RUN" and "requirements.lock" in rest) + 1
    assert steps[:pip] == base[:pip]                     # the cached layers stay shared
    keyword, apt = steps[pip]
    assert keyword == "RUN" and "apt-get install" in apt
    env_apt = next(rest for k, rest in tdf.instructions() if k == "RUN" and "apt-get" in rest)
    git_pin = next(w for w in env_apt.split() if w.startswith("git="))
    packages = apt.split("--no-install-recommends", 1)[1].split(";", 1)[0].split()
    assert packages == [git_pin]
    assert "snapshot.debian.org/archive/debian/20260910T000000Z/" in apt
    assert steps[pip + 1:] == base[pip:]
    assert rendered.splitlines()[0] == tdf.CANARY
    assert "--platform" not in rendered and "${" not in rendered
    assert checks.dockerfile_problems.__module__ == "alkera_harbor.checks"
    other = variant_for(BI002)
    assert variant.render_tests_dockerfile(text, other, template_text()).split("\n")[7:] \
        == rendered.split("\n")[7:]                      # only the header names the variant


def test_variant_context_files_are_tracked_locks(bi):
    tracked = set(subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True,
                                 text=True, check=True).stdout.split("\n"))
    for rel in bi.context_files() + variant.Variant(
            role="ds", clients=("snowflake",), profile="ds/x").context_files():
        assert rel in tracked, rel
        assert (REPO / rel).read_text(encoding="utf-8").count("--hash=sha256:") > 10


def test_warehouse_clients_render_after_the_role():
    from alkera_gateway.plugs import resolve

    profile = resolve("da/snowflake", {})
    v = variant.Variant(role="da", clients=("snowflake",), profile="da/snowflake",
                        slots=variant._canonical(profile.slots))
    text = variant.render_dockerfile(template_text(), v, template_env())
    runs = [rest for k, rest in tdf.instructions(text) if k == "RUN"]
    role = next(i for i, r in enumerate(runs) if "requirements-da.lock" in r)
    assert "requirements-snowflake.lock" in runs[role + 1]
    assert dockerenv.parse_env(text)["SNOWFLAKE_HOST"] == "snowflake.internal"


def test_clients_come_from_the_backend_registry(bi):
    """`alkera_gateway.backends.plugged` decides the client locks, the rule
    container_episode.profile_image and build_episode.image_tag share."""
    from alkera_gateway import backends
    from alkera_gateway.plugs import resolve

    snowflake = resolve("da/snowflake", {}).slots
    assert variant.clients_for(snowflake) == tuple(
        sorted({b.clients for b in backends.plugged(snowflake) if b.clients})) \
        == ("snowflake",)
    assert [b.name for b in variant.plugged_backends(snowflake)] == ["snowflake"]
    assert bi.clients == () and bi.backends == ()
    assert variant.UNPROFILED.backends == ()


def test_box_profile_follows_the_container_topology(bi):
    """The agent's ~/.dbt/profiles.yml: a profile that plugs a registry backend
    with a dbt adapter gets world.native_workspace.box_profiles_yaml (as
    container_episode._stage_profiles stages it); every other box keeps the
    re-rooted world profile byte for byte."""
    from alkera_harbor import worldbuild
    from world.native_workspace import box_profiles_yaml

    world_profile = ("world_dbt:\n  outputs:\n    dev:\n      path: "
                     "/world/var/warehouse/warehouse.duckdb\n      type: duckdb\n"
                     "  target: dev\n")
    assert worldbuild.box_profile(world_profile, bi) == world_profile
    assert worldbuild.box_profile(world_profile, variant.UNPROFILED) == world_profile
    snowflake = variant.Variant(role="da", clients=("snowflake",), profile="da/snowflake",
                                backends=("snowflake",))
    boxed = worldbuild.box_profile(world_profile, snowflake)
    assert boxed == box_profiles_yaml(world_profile, ["snowflake"])
    assert "target: snowflake" in boxed and "type: snowflake" in boxed


def test_unserved_warehouse_is_refused_by_name():
    """A profile whose warehouse the Harbor image does not serve fails loudly,
    naming it, before any world is built (plan Task 10 served the rest)."""
    class Spec:
        profile = "de/laptop"
        plugs = {"warehouse": ["duckdb", "bigquery"]}
    with pytest.raises(variant.VariantError, match="warehouse=bigquery"):
        variant.variant_of(Spec())


def test_unserved_notebook_is_a_recorded_deviation(bi):
    assert variant.unserved_notes(bi) == [
        "notebook=jupyter: jupyter.internal:8888 is not started"]
    assert variant.unserved_notes(variant.UNPROFILED) == []


def test_template_anchors_are_unique():
    """The edits anchor on template lines that occur exactly once; a template
    edit that breaks one fails here, not mid-export."""
    lines = template_text().split("\n")
    assert lines.count(variant.ANCHOR_DUCKDB_EXTENSION) == 1
    assert sum(line.startswith(variant.ANCHOR_ENV_COMMENT) for line in lines) == 1


def test_exported_variant_task_has_its_dockerfile(tmp_path):
    """The adapter writes the variant's Dockerfile and its locks into a
    profiled task's environment/ - checked on a staged copy, no world build."""
    exporter = adapter.AlkeraHarborAdapter(output_dir=tmp_path)
    bi = variant_for(BI001)
    exporter._environment_context(bi, BI001, tmp_path / "alkera")
    assert (tmp_path / "alkera/gateway/requirements-da.lock").read_bytes() == \
        (REPO / "gateway/requirements-da.lock").read_bytes()
    assert exporter.static_env(bi)["POWERBI_API_URL"] == "http://powerbi.internal:8095"
    assert ArmRef(BI001, "default").short_name == "bi001-revenue-disagrees-across-bi__default"
