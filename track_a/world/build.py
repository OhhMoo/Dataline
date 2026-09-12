"""seed.yaml -> world builder.

Produces the contract layout (split_of_work.md §2.1) under <root>:
  workspace/    git repo: dbt_project/, data/landing/, docs/, README.md
  var/warehouse/warehouse.duckdb   (raw loaded by builder; staging/warehouse by dbt)
  var/sched/state.json             (scheduler history + v-clock)
  var/bi/dashboards/*.yml
  build/provenance.json

Determinism rules enforced here: per-day seeded RNG (gen.py), integer-cent
amounts, duckdb PRAGMA threads=1 + ORDER BY on load, dbt threads: 1, git
author/committer dates pinned to vtime_start, no wall-clock reads anywhere.
"""
import argparse
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import yaml

from . import __version__, scheduler
from .gen import write_landing
from .hashing import file_hash, json_canon

DBT_PROJECT_YML = """\
name: world_dbt
version: "1.0.0"
config-version: 2
profile: world_dbt
model-paths: ["models"]
models:
  world_dbt:
    staging: {+materialized: view, +schema: staging}
    warehouse: {+materialized: table, +schema: warehouse}
"""

SCHEMA_MACRO = """\
{% macro generate_schema_name(custom_schema_name, node) -%}
{{ custom_schema_name if custom_schema_name else target.schema }}
{%- endmacro %}
"""

SOURCES_YML = """\
version: 2
sources:
  - name: raw
    schema: raw
    tables: [{name: daily_revenue}]
"""

STG_SQL = """\
select
  cast(order_id as integer) as order_id,
  cast(order_date as date) as order_date,
  customer_id,
  cast(amount as decimal(12,2)) as amount
from {{ source('raw', 'daily_revenue') }}
"""

FCT_SQL = """\
select order_date, count(*) as orders, sum(amount) as revenue
from {{ ref('stg_revenue') }}
group by order_date
order by order_date
"""

DASHBOARD = {
    "dashboard": "revenue_daily",
    "panels": [
        {"name": "daily_revenue",
         "sql": "select order_date, revenue from warehouse.fct_revenue order by order_date"},
        {"name": "total_revenue",
         "sql": "select sum(revenue) as total_revenue from warehouse.fct_revenue"},
    ],
}

README = """\
# revenue workspace

- `dbt_project/` — dbt Core project (staging view + warehouse table over `raw`)
- `data/landing/daily_revenue/dt=*/revenue.csv` — daily order landings
- `docs/` — data dictionary and runbooks

The warehouse is reachable only through the sql_* tools, never via fs_*.
"""

DOC_DATA_DICTIONARY = """\
# Data dictionary

- `raw.daily_revenue` — as-landed order rows from the landing zone.
  `amount` is a string here; cast happens in staging.
- `staging.stg_revenue` — typed order rows (`amount` decimal(12,2)).
- `warehouse.fct_revenue` — one row per `order_date`: `orders`, `revenue`.

Dashboard `revenue_daily` reads `warehouse.fct_revenue`.
"""

DOC_RUNBOOK = """\
# Runbook: daily_load

Scheduler job `daily_load` lands one day partition into `raw.daily_revenue`
(runs 01:00 UTC), followed by `dbt_build` at 01:05 UTC. Both should be
green in `sched_state`; investigate the first non-success run, not the last.
"""


def load_seed(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)


def _git(workspace: Path, seed: dict) -> None:
    m = re.match(r"(.*)<(.*)>", seed["git_author"])
    env = {**os.environ,
           "GIT_AUTHOR_DATE": seed["vtime_start"],
           "GIT_COMMITTER_DATE": seed["vtime_start"]}

    def run(*args):
        subprocess.run(["git", *args], cwd=workspace, env=env, check=True,
                       capture_output=True)

    run("init", "-q")
    run("config", "user.name", m.group(1).strip())
    run("config", "user.email", m.group(2).strip())
    run("add", "-A")
    run("commit", "-q", "-m", f"world seed {seed['seed']}")


def _load_raw(workspace: Path, db_path: Path) -> None:
    glob = (workspace / "data" / "landing" / "daily_revenue").as_posix() + "/dt=*/*.csv"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.daily_revenue AS
            SELECT cast(order_id as integer) AS order_id,
                   cast(order_date as date) AS order_date,
                   customer_id, amount, dt
            FROM read_csv('{glob}', hive_partitioning=true, columns={{
                'order_id': 'VARCHAR', 'order_date': 'VARCHAR',
                'customer_id': 'VARCHAR', 'amount': 'VARCHAR'}})
            ORDER BY order_date, order_id""")
    finally:
        con.close()


def _dbt_build(workspace: Path, db_path: Path, build_dir: Path) -> None:
    from dbt.cli.main import dbtRunner

    proj = workspace / "dbt_project"
    profiles = {"world_dbt": {"target": "dev", "outputs": {"dev": {
        "type": "duckdb", "path": str(db_path), "threads": 1}}}}
    # profiles carry an absolute path, so they live outside the fingerprinted
    # workspace (in build/) to keep workspace content root-location-independent.
    _write(build_dir / "profiles.yml", yaml.safe_dump(profiles))
    res = dbtRunner().invoke(["build", "--quiet", "--project-dir", str(proj),
                              "--profiles-dir", str(build_dir)])
    if not res.success:
        raise SystemExit(f"dbt build failed: {res.exception or res.result}")
    shutil.rmtree(proj / "target", ignore_errors=True)
    shutil.rmtree(proj / "logs", ignore_errors=True)


def build(seed_path: Path, root: Path, lock_path: Path | None = None) -> Path:
    seed = load_seed(seed_path)
    root = Path(root)
    workspace, build_dir = root / "workspace", root / "build"
    db_path = root / "var" / "warehouse" / "warehouse.duckdb"
    for d in (workspace, build_dir, db_path.parent,
              root / "var" / "sched", root / "var" / "bi" / "dashboards"):
        d.mkdir(parents=True, exist_ok=True)

    write_landing(workspace, seed)

    proj = workspace / "dbt_project"
    _write(proj / "dbt_project.yml", DBT_PROJECT_YML)
    _write(proj / "macros" / "schema.sql", SCHEMA_MACRO)
    _write(proj / "models" / "sources.yml", SOURCES_YML)
    _write(proj / "models" / "staging" / "stg_revenue.sql", STG_SQL)
    _write(proj / "models" / "warehouse" / "fct_revenue.sql", FCT_SQL)
    _write(workspace / "README.md", README)
    _write(workspace / "docs" / "data_dictionary.md", DOC_DATA_DICTIONARY)
    _write(workspace / "docs" / "runbook_daily_load.md", DOC_RUNBOOK)
    _write(root / "var" / "bi" / "dashboards" / "revenue_daily.yml",
           yaml.safe_dump(DASHBOARD, sort_keys=True))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    _load_raw(workspace, db_path)
    _dbt_build(workspace, db_path, build_dir)
    scheduler.save(root, scheduler.initial_state(seed))
    _git(workspace, seed)

    provenance = {
        "seed_sha256": file_hash(Path(seed_path)),
        "generator_version": __version__,
        "duckdb_version": duckdb.__version__,
        "dbt_version": importlib.metadata.version("dbt-core"),
        "dbt_duckdb_version": importlib.metadata.version("dbt-duckdb"),
        "requirements_lock_sha256": file_hash(lock_path) if lock_path else None,
        "image_digest": None,  # filled by the image build, never by hand
    }
    _write(build_dir / "provenance.json", json_canon(provenance) + b"\n")
    return root


def main(argv=None):
    p = argparse.ArgumentParser(prog="world.build")
    p.add_argument("--seed", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--lock", type=Path, default=None,
                   help="requirements.lock to attest in provenance")
    a = p.parse_args(argv)
    build(a.seed, a.root, a.lock)
    print(f"world built at {a.root}")


if __name__ == "__main__":
    sys.exit(main())
