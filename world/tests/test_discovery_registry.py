"""The discovery registry (native_workspace.DISCOVERY) and the files it renders.

    gateway/.venv/bin/python -m pytest world/tests/test_discovery_registry.py -q

THE GOLDENS. testdata/discovery_unprofiled/ and testdata/discovery_da_laptop/
hold the four discovery files native_workspace writes - workspace/.envrc,
workspace/docs/onboarding.md, home/.dbt/profiles.yml and
home/.config/alkera/services.toml - captured from a real GT001 world
(unprofiled: the harness's stage_discovery; GT001 ships no dbt profile, so its
profiles.yml is the unprofiled rendering for the platform's shared `world_dbt`
profile) and BI001's da/laptop world (world.build `native: true`) before the
registry existed, beside inputs.json: the slice of that world the files are
rendered from (the workflow corpus' ids, teams and statuses, the dashboards,
the BI project directories, the jobs). The box's own dbt profile - what
_stage_profiles puts at /root/.dbt in the container - is the world's profile
re-rooted, unchanged when no backend is plugged
(test_box_profile_without_a_plugged_backend_is_the_world_profile). The tests
rebuild a world from inputs.json and render the files again; a byte that
moves is a diff in testdata/. Recapture
from a freshly built world with

    python world/tests/test_discovery_registry.py --capture unprofiled <gt001 world.json>
    python world/tests/test_discovery_registry.py --capture da_laptop <bi001 world.json>

THE REGISTRY. A backend a profile plugs (alkera_gateway.backends.BACKENDS)
has one DISCOVERY row, and a box that plugs it names it in all four files: its
variables in .envrc, its row in onboarding, its [<backend>] section in
services.toml, and - when it has a dbt adapter - a target in the box profile
that becomes the default. A box that does not plug it names it nowhere.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

WORLD_DIR = Path(__file__).resolve().parents[1]
REPO = WORLD_DIR.parent
TESTDATA = Path(__file__).resolve().parent / "testdata"
sys.path.insert(0, str(WORLD_DIR))

from world import native_workspace as nw  # noqa: E402

#: The four files, by their place on the box.
FILES = {".envrc": "workspace/.envrc",
         "onboarding.md": "workspace/docs/onboarding.md",
         "services.toml": "home/.config/alkera/services.toml",
         "profiles.yml": "home/.dbt/profiles.yml"}
#: The analyst's box (alkera_gateway.plugs ROLES["da"]).
DA_SLOTS = ["files", "notebook", "warehouse", "transform", "bi", "human"]
#: A world's own dbt profile, re-rooted onto the container (container_episode
#: .reroot_profiles' output for the commerce_analytics theme).
WORLD_PROFILE = ("world_dbt:\n  outputs:\n    dev:\n"
                 "      path: /world/var/warehouse/warehouse.duckdb\n"
                 "      threads: 1\n      type: duckdb\n  target: dev\n")


def _gateway():
    sys.path.insert(0, str(REPO / "gateway" / "src"))
    from alkera_gateway import backends, warehouses

    return backends, warehouses


# ------------------------------------------------------------------ goldens
def _world_from(inputs: dict, root: Path) -> Path:
    """A world root holding exactly what the discovery files are rendered from."""
    for name, document in inputs["workflow"].items():
        path = root / "var" / "workflow" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
    for stem in inputs["dashboards"]:
        path = root / "var" / "bi" / "dashboards" / f"{stem}.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    for name in inputs["bi_projects"]:
        (root / "workspace" / "bi" / name).mkdir(parents=True, exist_ok=True)
    if inputs["bi_readme"]:
        (root / "workspace" / "bi" / "README.md").write_text("", encoding="utf-8")
    return root


def render(case: str, tmp_path: Path) -> dict[str, str]:
    """The four files for golden `case`, rendered by the code as it is now."""
    folder = TESTDATA / f"discovery_{case}"
    inputs = json.loads((folder / "inputs.json").read_text(encoding="utf-8"))
    root = _world_from(inputs, tmp_path / "world")
    slots, plugs = inputs["slots"], inputs["plugs"]
    # The theme's own .envrc is everything before the platform block.
    head = (folder / ".envrc").read_text(encoding="utf-8").partition(nw.ENVRC_BEGIN)[0]
    return {
        ".envrc": nw.merge_envrc(head, nw.envrc_block(slots, plugs)),
        "onboarding.md": nw.onboarding_markdown(root, {"jobs": inputs["jobs"]}, slots, plugs),
        "services.toml": nw.services_toml(inputs["world_in_container"], slots, plugs),
        "profiles.yml": nw.profiles_yaml(inputs["dbt_profile"]),
    }


def _golden(case: str) -> dict[str, str]:
    return {name: (TESTDATA / f"discovery_{case}" / name).read_text(encoding="utf-8")
            for name in FILES}


def test_unprofiled_discovery_is_byte_identical(tmp_path):
    assert render("unprofiled", tmp_path) == _golden("unprofiled")


def test_da_laptop_discovery_is_byte_identical(tmp_path):
    assert render("da_laptop", tmp_path) == _golden("da_laptop")


def test_unplugged_backend_is_absent_everywhere(tmp_path):
    files = render("da_laptop", tmp_path)
    for name, text in files.items():
        for backend in nw.DISCOVERY:
            assert backend not in text.lower(), (backend, name)


# ----------------------------------------------------------------- registry
def _snowflake_box():
    plugs = nw.profile_plugs("da/snowflake", None)
    return DA_SLOTS, plugs


def test_snowflake_box_has_services_section_and_default_target():
    slots, plugs = _snowflake_box()
    toml = tomllib.loads(nw.services_toml("/world", slots, plugs))
    assert toml["snowflake"]["host"] == "snowflake.internal"
    assert toml["snowflake"]["port"] == 8443
    assert toml["snowflake"]["database"] == "WAREHOUSE"
    assert "warehouse" not in toml                       # no Postgres on this box
    text = nw.box_profiles_yaml(WORLD_PROFILE, ["snowflake"])
    assert "target: snowflake" in text and "type: snowflake" in text
    profile = yaml.safe_load(text)["world_dbt"]
    assert profile["target"] == "snowflake"
    # the re-rooted DuckDB target is kept, unchanged
    assert profile["outputs"]["dev"] == yaml.safe_load(WORLD_PROFILE)["world_dbt"]["outputs"]["dev"]
    assert text.startswith(WORLD_PROFILE.split("  target: dev")[0])
    # secrets are read from the box's variables, never written down
    assert nw.FAKE_TOKEN not in text and "acme" not in text
    snowflake = profile["outputs"]["snowflake"]
    for field, var in nw.DISCOVERY["snowflake"].dbt_env.items():
        assert snowflake[field] == f"{{{{ env_var('{var}') }}}}"
    assert snowflake["port"] == 8443


def test_snowflake_database_exported():
    slots, plugs = _snowflake_box()
    assert nw.environment(slots, plugs)["SNOWFLAKE_DATABASE"] == "WAREHOUSE"
    assert 'export SNOWFLAKE_DATABASE="WAREHOUSE"' in nw.envrc_block(slots, plugs)
    for others in (nw.environment(),                                   # unprofiled
                   nw.environment(DA_SLOTS, nw.profile_plugs("da/laptop", None)),
                   nw.environment(DA_SLOTS, nw.profile_plugs("de/postgres", None))):
        assert "SNOWFLAKE_DATABASE" not in others


def test_box_profile_without_a_plugged_backend_is_the_world_profile():
    assert nw.box_profiles_yaml(WORLD_PROFILE, []) == WORLD_PROFILE
    # a plugged backend with no dbt adapter adds nothing either
    assert nw.box_profiles_yaml(WORLD_PROFILE, ["duckdb", "nope"]) == WORLD_PROFILE


def test_box_profile_refuses_to_overwrite_a_world_output():
    clash = WORLD_PROFILE.replace("    dev:\n", "    snowflake:\n")
    with pytest.raises(ValueError, match="'snowflake'"):
        nw.box_profiles_yaml(clash, ["snowflake"])


def test_box_profile_target_is_the_gateways_dbt_output():
    _, warehouses = _gateway()
    d = nw.DISCOVERY["snowflake"]
    profile = yaml.safe_load(nw.box_profiles_yaml(WORLD_PROFILE, ["snowflake"]))
    assert profile["world_dbt"]["outputs"]["snowflake"] == warehouses.dbt_output(
        d.dbt_type, schema=d.services_toml["schema"], env=d.dbt_env,
        port=int(d.services_toml["port"]))
    assert d.dbt_type in warehouses.DBT_ADAPTERS


def test_discovery_rows_agree_with_the_backend_registry():
    backends, _ = _gateway()
    assert set(nw.DISCOVERY) <= set(backends.BACKENDS)
    for name, d in nw.DISCOVERY.items():
        b = backends.BACKENDS[name]
        assert d.backend == name
        assert dict(d.env) == dict(b.env), name
        assert d.services_toml["host"] == b.host, name
        assert int(d.services_toml["port"]) == b.port, name
        assert set(d.dbt_env.values()) <= set(d.env), name


def test_onboarding_names_the_plugged_backend_from_its_row(tmp_path):
    slots, plugs = _snowflake_box()
    page = nw.onboarding_markdown(tmp_path, {}, slots, plugs)
    d = nw.DISCOVERY["snowflake"]
    assert f"| the warehouse | `snowflake.internal:8443` | {d.clients} | see `.envrc` |" \
        in page
    # beside Postgres it is the second warehouse, as it always was
    two = nw.onboarding_markdown(tmp_path, {}, ["warehouse"],
                                 {"warehouse": ["postgres", "snowflake"]})
    assert f"| the second warehouse | `snowflake.internal:8443` | {d.clients} |" in two


#: A hosted backend standing in for Airflow (Wave 1's row is its own): it
#: answers on 8100 and supersedes the static scheduler group (8080).
AIRFLOW_LIKE = nw.Discovery(
    backend="airflow", label="the scheduler", clients="Airflow REST API v2",
    services_toml={"host": "airflow.internal", "port": "8100",
                   "api": "http://airflow.internal:8100/api/v2",
                   "user": nw.FAKE_USER, "password": nw.FAKE_TOKEN},
    dbt_type=None,
    env={"AIRFLOW_API_URL": "http://airflow.internal:8100/api/v2",
         "AIRFLOW_USER": nw.FAKE_USER, "AIRFLOW_PASSWORD": nw.FAKE_TOKEN},
    replaces=("the scheduler",))
DE_SLOTS = ["files", "warehouse", "transform", "orchestrator", "human"]


def test_a_plugged_backend_replaces_the_static_group(tmp_path, monkeypatch):
    monkeypatch.setitem(nw.DISCOVERY, "airflow", AIRFLOW_LIKE)
    plugs = {"orchestrator": ["airflow"], "warehouse": ["duckdb"]}
    env = nw.environment(DE_SLOTS, plugs)
    envrc = nw.envrc_block(DE_SLOTS, plugs)
    toml_text = nw.services_toml("/world", DE_SLOTS, plugs)
    toml = tomllib.loads(toml_text)
    page = nw.onboarding_markdown(tmp_path, {}, DE_SLOTS, plugs)
    assert env["AIRFLOW_API_URL"] == "http://airflow.internal:8100/api/v2"
    for text in (envrc, toml_text, page):
        assert "airflow.internal:8100" in text
        assert "airflow.internal:8080" not in text
    assert envrc.count("AIRFLOW_API_URL") == 1
    assert "scheduler" not in toml and toml["airflow"]["port"] == 8100
    assert "| the scheduler | `airflow.internal:8100` | Airflow REST API v2 |" in page
    # a box that does not plug it keeps the static group, as before
    ops = nw.environment(DE_SLOTS, {"orchestrator": ["ops_sched"], "warehouse": ["duckdb"]})
    assert ops["AIRFLOW_API_URL"] == "http://airflow.internal:8080/api/v2"


def test_goldens_hold_with_another_backend_registered(tmp_path, monkeypatch):
    """A new registry row is explicit-only: the unprofiled and da/laptop boxes
    do not move."""
    monkeypatch.setitem(nw.DISCOVERY, "airflow", AIRFLOW_LIKE)
    assert render("unprofiled", tmp_path / "u") == _golden("unprofiled")
    assert render("da_laptop", tmp_path / "d") == _golden("da_laptop")


def test_replaces_names_static_groups():
    labels = {label for label, _, _ in nw.BACKEND_NOTES}
    for name, d in nw.DISCOVERY.items():
        assert set(d.replaces) <= labels, name


def test_discovery_never_imports_the_gateway():
    """world/ never imports alkera_gateway (the world factory runs without it):
    rendering the discovery files - .envrc, onboarding, services.toml - for a
    plugged backend loads nothing from the gateway."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            "from pathlib import Path;"
            "from world import native_workspace as nw;"
            "s=['files','warehouse','transform','human'];"
            "p=nw.profile_plugs('da/snowflake', None);"
            "nw.environment(s, p); nw.envrc_block(s, p); nw.services_toml('/world', s, p);"
            "nw.onboarding_markdown(Path(sys.argv[1]), {}, s, p);"
            "assert 'alkera_gateway' not in sys.modules, 'imported'")
    subprocess.run([sys.executable, "-c", code, str(WORLD_DIR)], check=True)


# ------------------------------------------------------------------ capture
def _project(world_root: Path) -> dict:
    """The workflow corpus, reduced to what onboarding_markdown reads."""
    out = {}
    workflow = world_root / "var" / "workflow"
    issues = workflow / "issues.json"
    if issues.is_file():
        doc = json.loads(issues.read_text(encoding="utf-8"))
        out["issues"] = {"project": doc.get("project"), "statuses": doc["statuses"],
                         "users": [{"team": u.get("team")} for u in doc["users"]],
                         "issues": [{"status": i["status"]} for i in doc["issues"]]}
    kb = workflow / "kb.json"
    if kb.is_file():
        doc = json.loads(kb.read_text(encoding="utf-8"))
        out["kb"] = {"space": doc["space"],
                     "pages": [{"id": p["id"], "title": p["title"]} for p in doc["pages"]]}
    chat = workflow / "chat.json"
    if chat.is_file():
        doc = json.loads(chat.read_text(encoding="utf-8"))
        out["chat"] = {"workspace": doc["workspace"],
                       "channels": [{"id": c["id"]} for c in doc["channels"]]}
    return out


def capture(case: str, world_json: Path) -> None:
    """Write testdata/discovery_<case>/ from a world built by its task's builder."""
    sys.path.insert(0, str(REPO / "tasks" / "harness"))
    import native_stack

    world = json.loads(world_json.read_text(encoding="utf-8").strip().splitlines()[-1])
    root = Path(world["world_root"])
    if case == "unprofiled":
        native_stack.stage_discovery(world)             # what rollout.py does
        slots = plugs = None
    else:
        record = json.loads((root / "var" / "world" / "profile.json").read_text())
        slots = record["slots"]
        plugs = nw.profile_plugs(record["name"], record.get("plugs"))
    state = root / "var" / "sched" / "state.json"
    jobs = json.loads(state.read_text(encoding="utf-8")).get("jobs", {}) \
        if state.is_file() else {}
    bi = root / "workspace" / "bi"
    inputs = {"case": case, "slots": slots, "plugs": plugs, "world_in_container": "/world",
              "jobs": jobs if slots is None else {}, "dbt_profile": "world_dbt",
              "workflow": _project(root),
              "dashboards": sorted(p.stem for p in (root / "var" / "bi" / "dashboards")
                                   .glob("*.yml")),
              "bi_projects": sorted(p.name for p in bi.iterdir() if p.is_dir())
              if bi.is_dir() else [],
              "bi_readme": (bi / "README.md").is_file()}
    profiles = root / FILES["profiles.yml"]
    files = {".envrc": (root / FILES[".envrc"]).read_text(encoding="utf-8"),
             "onboarding.md": (root / FILES["onboarding.md"]).read_text(encoding="utf-8"),
             "services.toml": (root / FILES["services.toml"]).read_text(encoding="utf-8"),
             # GT001's world ships none: the unprofiled rendering stands in
             "profiles.yml": profiles.read_text(encoding="utf-8") if profiles.is_file()
             else nw.profiles_yaml(inputs["dbt_profile"])}
    folder = TESTDATA / f"discovery_{case}"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in {**files, "inputs.json": json.dumps(inputs, indent=1, sort_keys=True)
                       + "\n"}.items():
        with open(folder / name, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
    print(f"wrote {folder}")


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--capture" or \
            sys.argv[2] not in ("unprofiled", "da_laptop"):
        raise SystemExit("usage: python world/tests/test_discovery_registry.py "
                         "--capture unprofiled|da_laptop <world.json>")
    capture(sys.argv[2], Path(sys.argv[3]))
