import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest
import yaml

from alkera_gateway import Gateway, GatewayConfig


def make_world(root: Path):
    workspace = root / "workspace"
    project = workspace / "dbt_project"
    (project / "models").mkdir(parents=True)
    (workspace / "docs").mkdir()
    (root / "var" / "warehouse").mkdir(parents=True)
    (root / "var" / "sched").mkdir(parents=True)
    dashboards = root / "var" / "bi" / "dashboards"
    dashboards.mkdir(parents=True)
    build = root / "build"
    build.mkdir()

    db_path = root / "var" / "warehouse" / "warehouse.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE SCHEMA warehouse")
        conn.execute("CREATE TABLE warehouse.warehouse.items(id INTEGER, amount DECIMAL(10,2))")
        conn.execute("INSERT INTO warehouse.warehouse.items VALUES (1, 10.50), (2, 20.25)")

    (workspace / "README.md").write_text("# Fixture\n", encoding="utf-8")
    (workspace / "docs" / "runbook.md").write_text(
        "The daily load is idempotent.\n", encoding="utf-8"
    )
    (project / "dbt_project.yml").write_text(
        "name: fixture\nversion: '1.0'\nconfig-version: 2\nprofile: fixture\n",
        encoding="utf-8",
    )
    (project / "models" / "sample.sql").write_text("select 1 as id\n", encoding="utf-8")
    (project / "models" / "schema.yml").write_text(
        "version: 2\nmodels:\n  - name: sample\n"
        "    columns:\n      - name: id\n        data_tests: [not_null]\n",
        encoding="utf-8",
    )
    profiles = {
        "fixture": {
            "target": "dev",
            "outputs": {
                "dev": {"type": "duckdb", "path": str(db_path), "schema": "main", "threads": 1}
            },
        }
    }
    (build / "profiles.yml").write_text(yaml.safe_dump(profiles), encoding="utf-8")
    dashboard = {
        "dashboard": "item_totals",
        "panels": [
            {
                "name": "summary",
                "sql": "select count(*) as items, sum(amount) as amount from warehouse.items",
            }
        ],
    }
    (dashboards / "item_totals.yml").write_text(
        yaml.safe_dump(dashboard, sort_keys=True), encoding="utf-8"
    )
    state = {
        "vnow": "2026-03-05T09:00:00Z",
        "jobs": {"daily_load": {"command": "load_daily", "duration_s": 5}},
        "runs": [],
    }
    (root / "var" / "sched" / "state.json").write_text(
        json.dumps(state, sort_keys=True), encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    env = {
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@alkera.local",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@alkera.local",
        "GIT_AUTHOR_DATE": "2026-03-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-03-01T00:00:00Z",
    }
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=workspace, env=env, check=True)
    return root


def world_hash(root: Path):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (
            path.is_file()
            and ".git" not in relative.parts
            and "audit" not in relative.parts
            and "build" not in relative.parts
        ):
            digest.update(relative.as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def config(root: Path, audit: Path, fingerprint: str | None = None, **overrides):
    """Fixture config. `overrides` reaches fields no existing test needs (s3_endpoint,
    s3_buckets, max_payload_bytes) without changing any existing call site."""
    return GatewayConfig(
        workspace=root / "workspace",
        warehouse=root / "var" / "warehouse" / "warehouse.duckdb",
        scheduler=root / "var" / "sched" / "state.json",
        dashboards=root / "var" / "bi" / "dashboards",
        audit_dir=audit,
        profiles_dir=root / "build",
        episode_id=audit.name,
        initial_world_fp=fingerprint or world_hash(root),
        **overrides,
    )


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path / "world")


@pytest.fixture
def gateway(world, tmp_path):
    instance = Gateway(config(world, tmp_path / "audit"))
    yield instance
    instance.close()


def clone_world(source: Path, destination: Path):
    shutil.copytree(source, destination)
    profiles = yaml.safe_load((destination / "build" / "profiles.yml").read_text(encoding="utf-8"))
    profiles["fixture"]["outputs"]["dev"]["path"] = str(
        destination / "var" / "warehouse" / "warehouse.duckdb"
    )
    (destination / "build" / "profiles.yml").write_text(yaml.safe_dump(profiles), encoding="utf-8")
    return destination
