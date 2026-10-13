"""End-to-end check of the BigQuery plug in the CONTAINER topology.

    sg docker -c "gateway/.venv/bin/python tasks/harness/bigquery_e2e_check.py \\
        --image alkera-episode:w1-bigquery"

Builds the `commerce_analytics` world (seed 11) for the `de/gcp` profile and brings
the stack up the way the tool tour does (`tool_tour.TourTopology`, imported): compose
profile `bigquery` starts goccy's emulator on backend_net; native-services joins
backend_net, seeds the emulator from the world's warehouse at import - before it binds
a port - and serves the audited proxy as `bigquery.internal:9050` on episode_net. From
INSIDE the episode container:

  (a) google-cloud-bigquery (AnonymousCredentials + api_endpoint from the box's own
      variables): every seeded table's COUNT(*) and February 2026 Net Revenue equal
      the world's DuckDB, read on the host; CREATE SCHEMA + CREATE OR REPLACE TABLE +
      INSERT + SELECT on a scratch dataset; the fences (dry run, legacy SQL, ALTER,
      a transaction - its INSERT never reaching the engine -, procedural
      scripting, a syntax error and an untokenizable query answered at once,
      never retried), each refused query a `bigquery.statement` row flagged
      `refused:<feature>`, the untokenizable one a single `unparsed_statement`
      row; a `SELECT; DROP` script a destructive row; `list_rows(max_results=5)`
      five rows;
  (b) dbt-bigquery (gateway/docker/bigquery-clients.in) through the box's own dbt
      profile (~/.dbt, native_workspace.box_profiles_yaml), whose default target is
      the proxy (method oauth-secrets, placeholder token, api_endpoint and project
      from the box's variables): `dbt debug`, `dbt run` (a table from a source, a
      view on it, an incremental model), a re-run (CREATE OR REPLACE), `dbt test`
      (not_null, unique) and `dbt show --inline` in a scratch project - the model's
      February 2026 Net Revenue equal to the world's DuckDB -, dbt's relation
      listing (its cache) seeing every relation it built and the incremental
      model's second run a MERGE, never a silent rebuild (dbt-bigquery is held
      below 1.12, whose routines listing empties that cache here), `dbt seed`
      refused by name (a load job), and a BARE `dbt debug` and
      `dbt show --inline "... {{ ref() }}"` in the world's own project. Every statement dbt logs (its SQLQuery events) is one
      `bigquery.statement` row, by text and in order. The de image's role boundary
      holds with the adapter's dependencies in it: pandas, pyarrow, nbformat and
      jupyter_core's `jupyter` dispatcher are there (toolbelt_check CLIENT_IMPORTS /
      CLIENT_BINARIES, decision D2), and nothing else the gate forbids a de box -
      no kernel, no Jupyter server, no nbclient, no matplotlib;
  (c) the audit: one `bigquery.statement` row per statement the probe sent, in order;
      statements = audit rows, by job id, against the EMULATOR's own job list (read
      straight upstream from native-services, which writes no row and creates no job);
      every row a valid 9.2.2 row and `seq` contiguous;
  (d) isolation: the episode is on episode_net only and cannot resolve the engine;
      native-services is on both networks;
  (e) `test_remote_write_leaves_world_fingerprint_unchanged`: the world's semantic
      fingerprint is the same before and after the remote writes, which left
      write-class rows; and after teardown destinations.jsonl names
      bigquery.internal:9050 with no connection outside the profile's allowlist.

The Task 8 spike's platform question is recorded in the evidence too: which platforms
the pinned emulator image index carries (read here). Its dbt-bigquery half, measured on
loopback when the adapter was not in the image, is now (b), measured in the episode.

Evidence: tasks/harness/evidence/bigquery_e2e.json. Exit 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

PROFILE = "de/gcp"
DEFAULT_IMAGE = "alkera-episode:w1-bigquery"
EVIDENCE = HERE / "evidence" / "bigquery_e2e.json"
IN_DIR = "/tmp/bq-e2e"
PYTHON = "/usr/local/bin/python"  # absolute: the probe is the check's, not a shim row
TOOL = "bigquery.statement"
PROJECT = "alkera-local"
EMULATOR_IMAGE = ("ghcr.io/goccy/bigquery-emulator:0.8.1@sha256:"
                  "f4e428d265a93dc5ce36c294e1c584c7c9b384117d47ab8ddbb63d8d50b7f393")

PROBE = r'''
import json, os, sys, time
from google.api_core import exceptions
from google.api_core.client_options import ClientOptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

spec = json.loads(sys.argv[1])
env = os.environ
project = env["BIGQUERY_PROJECT"]
client = bigquery.Client(project=project, credentials=AnonymousCredentials(),
                         client_options=ClientOptions(api_endpoint=env["BIGQUERY_API_URL"]))
out = {"statements": [], "env": {k: env.get(k) for k in
       ("BIGQUERY_API_URL", "BIGQUERY_PROJECT", "REQUESTS_CA_BUNDLE")}}

def run(sql):
    out["statements"].append(sql)
    job = client.query(sql)
    return job, [[str(v) for v in row.values()] for row in job.result()]

def refused(call):
    started = time.monotonic()
    try:
        call()
    except exceptions.GoogleAPICallError as exc:
        reason = (exc.errors or [{}])[0].get("reason") if exc.errors else None
        return {"code": exc.code, "reason": reason, "message": str(exc)[:400],
                "seconds": round(time.monotonic() - started, 2)}
    return None

out["datasets"] = sorted(d.dataset_id for d in client.list_datasets())
out["counts"] = {name: int(run(f"SELECT COUNT(*) FROM `{project}.{name}`")[1][0][0])
                 for name in spec["tables"]}
job, rows = run(spec["revenue_sql"].format(project=project))
out["net_revenue_feb_2026"] = rows[0][0]
out["select_cost_fields"] = {"total_bytes_processed": job.total_bytes_processed,
                             "total_bytes_billed": job.total_bytes_billed,
                             "statement_type": job.statement_type}
out["write_types"] = []
for sql in (f"CREATE SCHEMA IF NOT EXISTS `{project}`.bq_e2e_scratch",
            f"CREATE OR REPLACE TABLE `{project}.bq_e2e_scratch.notes` (id INT64, note STRING)",
            f"INSERT INTO `{project}.bq_e2e_scratch.notes` (id, note) VALUES (1, 'alpha'), (2, 'beta')"):
    job, _ = run(sql)
    out["write_types"].append(job.statement_type)
out["read_back"] = run(f"SELECT id, note FROM `{project}.bq_e2e_scratch.notes` ORDER BY id")[1]
out["dry_run"] = refused(lambda: client.query(
    "SELECT 1", job_config=bigquery.QueryJobConfig(dry_run=True)))
out["legacy_sql"] = refused(lambda: client.query(
    "SELECT 1", job_config=bigquery.QueryJobConfig(use_legacy_sql=True)).result())
out["alter"] = refused(lambda: client.query(
    f"ALTER TABLE `{project}.bq_e2e_scratch.notes` ADD COLUMN extra STRING").result())
out["transaction"] = refused(lambda: client.query(
    f"BEGIN TRANSACTION; INSERT INTO `{project}.bq_e2e_scratch.notes` (id, note) "
    "VALUES (3, 'gamma'); SELECT ERROR('boom'); COMMIT TRANSACTION").result())
out["bad_sql"] = refused(lambda: client.query("SELEC 1").result())
out["scripting"] = refused(lambda: client.query("IF TRUE THEN SELECT 1; END IF").result())
out["untokenizable"] = refused(lambda: client.query("SELECT 'abc").result())
run(f"CREATE TABLE `{project}.bq_e2e_scratch.drop_me` (a INT64)")
out["drop_script"] = f"SELECT 1; /* tidy */ DROP TABLE `{project}.bq_e2e_scratch.drop_me`"
run(out["drop_script"])
out["list_rows_5"] = [[str(v) for v in row.values()] for row in client.list_rows(
    f"{project}.warehouse.fct_orders", max_results=5)]
out["read_back_after_fences"] = run(
    f"SELECT id, note FROM `{project}.bq_e2e_scratch.notes` ORDER BY id")[1]
client.close()
print("BQ-E2E " + json.dumps(out))
'''

#: The scratch dbt project (b) runs, under the box profile (`world_dbt`, whose default
#: target is the proxy): a table from a source, a view on it, two tests, an incremental
#: model, a seed, and a macro that prints what dbt's relation listing (the input of its
#: relation cache) finds in the project's dataset.
#: Its models build into their own dataset - the world's generate_schema_name rule, a
#: custom schema as given - so nothing lands beside the seeded marts.
DBT_DIR = f"{IN_DIR}/dbt"
DBT_DATASET = "bq_e2e_dbt"
DBT_FILES = {
    "dbt_project.yml": f"""name: bq_e2e
version: "1.0"
config-version: 2
profile: world_dbt
model-paths: [models]
macro-paths: [macros]
seed-paths: [seeds]
models:
  bq_e2e:
    +schema: {DBT_DATASET}
seeds:
  bq_e2e:
    +schema: {DBT_DATASET}
""",
    "macros/schema.sql": """{% macro generate_schema_name(custom_schema_name, node) -%}
{{ custom_schema_name if custom_schema_name else target.schema }}
{%- endmacro %}
""",
    "models/sources.yml": """version: 2
sources:
  - name: warehouse
    schema: warehouse
    tables:
      - name: fct_orders
models:
  - name: bq_feb_revenue
    columns:
      - name: order_status
        data_tests: [not_null, unique]
""",
    "models/bq_feb_revenue.sql": """{{ config(materialized='table') }}
select order_status, count(*) as orders, sum(order_amount) as net_revenue
from {{ source('warehouse', 'fct_orders') }}
where order_status = 'completed'
  and order_date >= '2026-02-01' and order_date < '2026-03-01'
group by order_status
""",
    "models/bq_feb_revenue_v.sql": """{{ config(materialized='view') }}
select * from {{ ref('bq_feb_revenue') }}
""",
    "models/bq_orders_inc.sql": """{{ config(materialized='incremental', unique_key='order_status') }}
select order_status, count(*) as orders
from {{ source('warehouse', 'fct_orders') }}
group by order_status
""",
    "macros/relations_probe.sql": f"""{{% macro bq_e2e_relations() %}}
  {{% set dataset = api.Relation.create(database=target.database, schema='{DBT_DATASET}') %}}
  {{% set found = adapter.list_relations_without_caching(dataset) %}}
  {{{{ print(found | map(attribute='identifier') | sort | join(',')) }}}}
{{% endmacro %}}
""",
    "seeds/bq_seed.csv": "id,label\n1,alpha\n",
}
#: The world's project, as a bare `dbt` on the box finds it.
WORLD_DBT = "/workspace/dbt_project"
#: dbt's own record of each statement it sent: an SQLQuery event in its JSON file log.
SQL_EVENT = "SQLQuery"


def dbt_statements(log_text: str) -> list[str]:
    """The SQL of every SQLQuery event in a dbt `--log-format-file json` log, in order."""
    out = []
    for line in log_text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (event.get("info") or {}).get("name") == SQL_EVENT:
            out.append(event["data"]["sql"])
    return out


def gate_lists() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(allowed imports, allowed scripts, forbidden imports, forbidden scripts) of a de
    image with the bigquery clients, from the shipped gate itself."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_toolbelt_check", REPO / "gateway" / "docker" / "toolbelt_check.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return (gate.CLIENT_IMPORTS["bigquery"], gate.CLIENT_BINARIES["bigquery"],
            gate.forbidden_imports("de", ("bigquery",)),
            gate.forbidden_binaries("de", ("bigquery",)))


#: The upstream job list, read from INSIDE native-services straight at the engine:
#: no proxy, so no row; a GET, so no job. {job id: query text}.
UPSTREAM_JOBS = r'''
import json, os, httpx
base = os.environ["ALKERA_BIGQUERY_UPSTREAM"] + "/bigquery/v2/projects/alkera-local/jobs"
jobs = {}
for entry in httpx.get(base, timeout=30).json().get("jobs", []):
    job_id = entry["jobReference"]["jobId"]
    full = httpx.get(f"{base}/{job_id}", timeout=30).json()
    jobs[job_id] = ((full.get("configuration") or {}).get("query") or {}).get("query")
print(json.dumps(jobs))
'''


def relations(warehouse: Path) -> dict[str, int]:
    """{dataset.table: rows} for every relation the seed copies, from the world."""
    import duckdb
    from alkera_gateway.native.bigquery_seed import relations as listed

    with duckdb.connect(str(warehouse), read_only=True) as con:
        catalog = con.execute("SELECT current_database()").fetchone()[0]
        return {f"{s}.{t}": con.execute(f'SELECT count(*) FROM "{catalog}"."{s}"."{t}"')
                .fetchone()[0] for s, t in listed(warehouse)}


def semantic_hash(world_root: Path) -> str:
    import episode

    if str(episode.TRACK_A) not in sys.path:
        sys.path.insert(0, str(episode.TRACK_A))
    from world.fingerprint import semantic_hash as fingerprint

    return fingerprint(world_root)


def warehouse_section(world_root: Path) -> str:
    import episode

    if str(episode.TRACK_A) not in sys.path:
        sys.path.insert(0, str(episode.TRACK_A))
    from world.fingerprint import semantic

    return semantic(world_root)["warehouse"]["hash"]


def platforms(image: str) -> dict:
    """The pinned emulator index's platforms (`docker buildx imagetools inspect`)."""
    done = subprocess.run(["docker", "buildx", "imagetools", "inspect", image],
                          capture_output=True, text=True, check=False)
    found = sorted({line.split(":", 1)[1].strip() for line in done.stdout.splitlines()
                    if line.strip().startswith("Platform:")} - {"unknown/unknown"})
    return {"image": image, "platforms": found, "returncode": done.returncode}


def container_networks(container: str) -> list[str]:
    done = subprocess.run(["docker", "inspect", "--format",
                           "{{json .NetworkSettings.Networks}}", container],
                          capture_output=True, text=True, check=False)
    return sorted(json.loads(done.stdout or "{}"))


def main(argv=None) -> int:
    import agent_under_test as aut
    import bi_e2e_check as bi
    import blindness
    import tool_tour as tour
    from alkera_gateway.native.audit import ROW_KEYS

    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--work", type=Path,
                        default=Path.home() / "alkera-rollouts" / "w1-bigquery" / "e2e")
    parser.add_argument("--out", type=Path, default=EVIDENCE)
    parser.add_argument("--keep", action="store_true", help="leave the stack up")
    args = parser.parse_args(argv)

    checks: list[dict] = []

    def check(name, ok, detail=None):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if not ok and detail
                                                       else ""), flush=True)

    work = args.work
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    seed = tour.world_seed(PROFILE, {})
    with tour.as_profile(PROFILE, {}, seed):
        world = bi.build_world(work)
    world_root = Path(world["world_root"])
    warehouse = Path(world["warehouse"])
    expected = relations(warehouse)
    revenue_sql = ("SELECT SUM(order_amount) FROM `{project}.warehouse.fct_orders` "
                   "WHERE order_status = 'completed' AND order_date >= '2026-02-01' "
                   "AND order_date < '2026-03-01'")
    import duckdb

    with duckdb.connect(str(warehouse), read_only=True) as con:
        revenue = con.execute(
            "SELECT SUM(order_amount) FROM warehouse.warehouse.fct_orders WHERE "
            "order_status = 'completed' AND order_date >= DATE '2026-02-01' "
            "AND order_date < DATE '2026-03-01'").fetchone()[0]
    warehouse_before = warehouse_section(world_root)
    allowed: set | None = None
    record: dict = {"profile": PROFILE, "image": args.image, "revision": bi._git_rev(),
                    "expected": {"tables": expected, "net_revenue_feb_2026": str(revenue)},
                    "spike": {"emulator_image": platforms(EMULATOR_IMAGE)}}
    platforms_found = record["spike"]["emulator_image"]["platforms"]
    check("spike: the pinned emulator image is multi-arch (amd64 + arm64 in its index)",
          {"linux/amd64", "linux/arm64"} <= set(platforms_found), platforms_found)

    topo = tour.TourTopology(world, work, args.image, PROFILE, {}, seed)
    audit_file = Path(topo.audit_dir) / "actions.jsonl"
    started = time.monotonic()
    try:
        up = topo.up()
        stack = topo.stack
        record["up_seconds"] = round(time.monotonic() - started, 1)
        record["services"] = up.get("services")
        record["overlay"] = stack.overlay.read_text(encoding="utf-8")
        record["compose_profiles"] = stack.compose_env().get("COMPOSE_PROFILES")
        check("compose profile `bigquery` starts the engine beside native-services",
              "bigquery" in (up.get("services") or []), up.get("services"))
        services = stack.services_container
        logs = subprocess.run(["docker", "logs", services], capture_output=True, text=True, check=False)
        seeds = [json.loads(line) for line in logs.stdout.splitlines()
                 if line.startswith('{"') and '"seed": "bigquery"' in line]
        record["seed"] = seeds[-1] if seeds else None
        check("native-services seeded the emulator before binding (one seed line)",
              len(seeds) == 1, logs.stdout[-1500:] + logs.stderr[-1500:])
        check("seed: every relation's row count equals the world's DuckDB",
              bool(seeds) and seeds[-1]["tables"] == expected,
              {"seed": seeds[-1]["tables"] if seeds else None, "world": expected})
        check("seed: within the healthcheck window (< 45 s)",
              bool(seeds) and seeds[-1]["seconds"] < 45, seeds[-1] if seeds else None)
        rows_after_up = bi.read_rows(audit_file)
        check("seed: writes no audit row", not any(
            str(r.get("tool", "")).startswith("bigquery.") for r in rows_after_up),
            [r.get("tool") for r in rows_after_up])

        # (d) isolation
        episode_nets = container_networks(stack.container_id)
        services_nets = container_networks(services)
        record["networks"] = {"episode": episode_nets, "native-services": services_nets}
        check("isolation: native-services is on backend_net and episode_net",
              any(n.endswith("backend_net") for n in services_nets)
              and any(n.endswith("episode_net") for n in services_nets), services_nets)
        check("isolation: the episode is on episode_net only",
              episode_nets and all(n.endswith("episode_net") for n in episode_nets),
              episode_nets)
        engine = stack.exec_(["getent", "hosts", "bigquery"], check=False)
        front = stack.exec_(["getent", "hosts", "bigquery.internal"], check=False)
        record["resolve"] = {"bigquery": engine.stdout.strip(),
                             "bigquery.internal": front.stdout.strip()}
        check("isolation: the episode cannot resolve the engine, only the proxy",
              engine.returncode != 0 and front.returncode == 0, record["resolve"])

        # (a) the Python client, from inside the episode
        stack.exec_(["mkdir", "-p", f"{IN_DIR}/models"])
        probe = work / "probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        stack.copy_in(probe, f"{IN_DIR}/probe.py")
        spec = {"tables": sorted(expected), "revenue_sql": revenue_sql}
        before_rows = len(bi.read_rows(audit_file))
        fingerprint_before = semantic_hash(world_root)
        done = stack.exec_([PYTHON, f"{IN_DIR}/probe.py", json.dumps(spec)], check=False,
                           timeout=600)
        fingerprint_after = semantic_hash(world_root)
        line = next((ln for ln in done.stdout.splitlines() if ln.startswith("BQ-E2E ")), None)
        check("client probe ran inside the episode", line is not None,
              (done.stdout + done.stderr)[-2000:])
        got = json.loads(line[7:]) if line else {}
        record["probe"] = got
        check("discovery: the episode carries BIGQUERY_API_URL and BIGQUERY_PROJECT",
              got.get("env", {}).get("BIGQUERY_API_URL") == "http://bigquery.internal:9050"
              and got.get("env", {}).get("BIGQUERY_PROJECT") == PROJECT,
              got.get("env"))
        check("client: the datasets are the world's schemas (anonymous ones hidden)",
              got.get("datasets") == sorted({k.split(".")[0] for k in expected}),
              got.get("datasets"))
        check("client: every table's COUNT(*) equals the world's DuckDB",
              got.get("counts") == expected, {"got": got.get("counts")})
        check("client: February 2026 Net Revenue equals the world's DuckDB",
              got.get("net_revenue_feb_2026") is not None
              and float(got["net_revenue_feb_2026"]) == float(revenue),
              {"got": got.get("net_revenue_feb_2026"), "expected": str(revenue)})
        check("client: no cost figure reaches the client",
              got.get("select_cost_fields", {}).get("total_bytes_processed") is None
              and got.get("select_cost_fields", {}).get("total_bytes_billed") is None,
              got.get("select_cost_fields"))
        check("client: statement types are the statements' own",
              got.get("write_types") == ["CREATE_SCHEMA", "CREATE_TABLE", "INSERT"],
              got.get("write_types"))
        check("client: scratch INSERT reads back", got.get("read_back") == [
            ["1", "alpha"], ["2", "beta"]], got.get("read_back"))
        for name, code, text in (("dry_run", 501, "cost estimation is not modelled"),
                                 ("legacy_sql", 501, "legacy SQL"),
                                 ("alter", 501, "ALTER"),
                                 ("transaction", 501, "transactions"),
                                 ("scripting", 501, "procedural scripting"),
                                 ("untokenizable", 400, "Syntax error"),
                                 ("bad_sql", 400, "Syntax error")):
            seen = got.get(name) or {}
            check(f"fence: {name} refused by name, at once ({code})",
                  seen.get("code") == code and text in seen.get("message", "")
                  and seen.get("seconds", 99) < 10, seen)
        check("fence: the refused transaction's INSERT never reached the engine",
              got.get("read_back_after_fences") == [["1", "alpha"], ["2", "beta"]],
              got.get("read_back_after_fences"))
        check("client: list_rows(max_results=5) returns 5 rows (tabledata.list paged)",
              len(got.get("list_rows_5") or []) == 5, len(got.get("list_rows_5") or []))
        rows = bi.read_rows(audit_file)
        probe_rows = rows[before_rows:]
        sent = got.get("statements", [])
        statement_sql = [r["args"]["sql"] for r in probe_rows if r["tool"] == TOOL]
        check("audit: one bigquery.statement row per probe statement, in order",
              [s for s in statement_sql if s in sent] == sent,
              {"sent": len(sent), "statement_rows": len(statement_sql)})
        writes = [r for r in probe_rows if r["tool"] == TOOL and r["exit"] == "ok"
                  and r["op_class"] in ("write", "ddl", "destructive")]
        record["write_rows"] = [{k: r[k] for k in ("seq", "op_class", "targets", "flags")}
                                | {"sql": r["args"]["sql"]} for r in writes]
        check("test_remote_write_leaves_world_fingerprint_unchanged: the remote "
              "writes left write-class rows and the world's fingerprint did not move",
              len(writes) >= 3 and fingerprint_before == fingerprint_after,
              {"before": fingerprint_before, "after": fingerprint_after,
               "write_rows": len(writes)})
        refusals = [r for r in probe_rows if any(
            f.startswith("refused:") for f in r["flags"])]
        record["refused_rows"] = [{k: r[k] for k in ("seq", "tool", "op_class", "exit",
                                                     "flags")} for r in refusals]
        check("audit: each refused query is a bigquery.statement row, exit error, "
              "flagged refused:<feature>",
              {r["args"].get("feature") for r in refusals}
              == {"dry_run", "legacy_sql", "alter", "transaction", "scripting"}
              and all(r["tool"] == TOOL and r["exit"] == "error" and r["args"].get("sql")
                      for r in refusals),
              record["refused_rows"])
        unparsed = [r for r in probe_rows if r["args"].get("sql") == "SELECT 'abc"]
        check("audit: an untokenizable query is exactly one statement row, "
              "unparsed_statement, invalidQuery",
              len(unparsed) == 1 and "unparsed_statement" in unparsed[0]["flags"]
              and (unparsed[0]["error"] or {}).get("code") == "invalidQuery",
              [{k: r[k] for k in ("seq", "flags", "error")} for r in unparsed])
        script = [r for r in probe_rows if r["args"].get("sql") == got.get("drop_script")]
        check("audit: a script is as serious as its worst statement "
              "(SELECT then DROP is a destructive row)",
              len(script) == 1 and script[0]["op_class"] == "destructive",
              [{k: r[k] for k in ("seq", "op_class", "targets")} for r in script])

        # (b) dbt-bigquery through the box profile, and the role boundary with it
        adapter = stack.exec_([PYTHON, "-c", "import dbt.adapters.bigquery"], check=False)
        check("dbt-bigquery is installed (the bigquery client lock's adapter)",
              adapter.returncode == 0, adapter.stdout + adapter.stderr[-300:])
        allowed_mods, allowed_bins, forbidden_mods, forbidden_bins = gate_lists()

        def importable(name):
            return stack.exec_([PYTHON, "-c", f"import {name}"], check=False).returncode == 0

        def on_disk(name):
            found = (f"command -v {name} || test -e /usr/local/bin/{name} "
                     f"|| test -e /opt/toolbelt/bin/{name}")
            return stack.exec_(["sh", "-c", found], check=False).returncode == 0

        boundary = {"client_imports": {m: importable(m) for m in allowed_mods},
                    "client_scripts": {b: on_disk(b) for b in allowed_bins},
                    "forbidden_imports": {m: importable(m) for m in forbidden_mods},
                    "forbidden_scripts": {b: on_disk(b) for b in forbidden_bins}}
        server = stack.exec_(["jupyter", "server", "--version"], check=False)
        boundary["jupyter_server_runs"] = server.returncode == 0
        record["role_boundary"] = boundary
        check("role boundary: the adapter's pandas, pyarrow, nbformat and `jupyter` "
              "dispatcher are in the de image (CLIENT_IMPORTS / CLIENT_BINARIES)",
              all(boundary["client_imports"].values())
              and all(boundary["client_scripts"].values()), boundary)
        check("role boundary: nothing else the gate forbids a de box - no kernel, no "
              "Jupyter server, no nbclient, no matplotlib, and `jupyter server` has "
              "nothing to run",
              not any(boundary["forbidden_imports"].values())
              and not any(boundary["forbidden_scripts"].values())
              and not boundary["jupyter_server_runs"],
              {**boundary, "jupyter_server": (server.stdout + server.stderr)[-300:]})
        import yaml

        box = stack.exec_(["cat", "/root/.dbt/profiles.yml"], check=False)
        record["box_profile"] = box.stdout
        try:
            box_profile = (yaml.safe_load(box.stdout) or {}).get("world_dbt") or {}
        except yaml.YAMLError:
            box_profile = {}
        outputs = box_profile.get("outputs") or {}
        target = outputs.get("bigquery") or {}
        check("box profile (~/.dbt) defaults to the bigquery target through the proxy, "
              "DuckDB dev kept",
              box_profile.get("target") == "bigquery"
              and outputs.get("dev", {}).get("type") == "duckdb"
              and target.get("type") == "bigquery"
              and target.get("method") == "oauth-secrets"
              and target.get("api_endpoint") == "{{ env_var('BIGQUERY_API_URL') }}"
              and target.get("project") == "{{ env_var('BIGQUERY_PROJECT') }}"
              and "keyfile" not in target and "port" not in target,
              box.stdout[-800:] or box.stderr[-400:])

        for rel, text in DBT_FILES.items():
            local = work / "dbt" / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(text, encoding="utf-8")
            stack.exec_(["mkdir", "-p", str(Path(f"{DBT_DIR}/{rel}").parent)])
            stack.copy_in(local, f"{DBT_DIR}/{rel}")
        show_sql = ("select net_revenue, orders from {{ ref('bq_feb_revenue_v') }} "
                    "where order_status = 'completed'")
        world_sql = ("select sum(order_amount) as net_revenue from {{ ref('fct_orders') }} "
                     "where order_status = 'completed' and order_date >= '2026-02-01' "
                     "and order_date < '2026-03-01'")
        steps = (("debug", DBT_DIR, "dbt debug"),
                 ("run", DBT_DIR, "dbt run"),
                 ("re-run", DBT_DIR, "dbt run --exclude bq_orders_inc"),
                 ("relations", DBT_DIR, "dbt --quiet run-operation bq_e2e_relations"),
                 ("incremental", DBT_DIR, "dbt run -s bq_orders_inc"),
                 ("test", DBT_DIR, "dbt test"),
                 ("show", DBT_DIR,
                  f"dbt --quiet show --inline \"{show_sql}\" --output json"),
                 ("seed", DBT_DIR, "dbt seed"),
                 ("bare debug", WORLD_DBT, "dbt debug"),
                 ("bare show", WORLD_DBT,
                  f"dbt --quiet show --inline \"{world_sql}\" --output json"))
        dbt: dict = {}
        dbt_sent: list[str] = []
        dbt_rows: list[dict] = []
        for name, where, command in steps:
            slug = name.replace(" ", "-")
            logs = f"{DBT_DIR}/logs/{slug}"
            # dbt's JSON file log is the statement list; `debug` takes no --target-path
            extra = f" --log-path {logs} --log-format-file json" + (
                "" if command == "dbt debug" else f" --target-path {DBT_DIR}/target/{slug}")
            before = len(bi.read_rows(audit_file))
            res = stack.exec_(["sh", "-c", f"cd {where} && {command}{extra}"],
                              check=False, timeout=600)
            step_rows = bi.read_rows(audit_file)[before:]
            log = stack.exec_(["cat", f"{logs}/dbt.log"], check=False).stdout
            sent = dbt_statements(log)
            rows_sql = [r["args"]["sql"] for r in step_rows if r["tool"] == TOOL]
            dbt[name] = {"rc": res.returncode, "statements": len(sent), "sent": sent,
                         "statement_rows": len(rows_sql),
                         "tools": sorted({r["tool"] for r in step_rows}),
                         "refused": sorted({f for r in step_rows for f in r["flags"]
                                            if f.startswith("refused:")}),
                         "stdout": res.stdout[-1500:], "tail": res.stderr[-1200:]}
            dbt_sent += sent
            dbt_rows += [r for r in step_rows if r["tool"] == TOOL]
            if name == "seed":
                check("dbt seed is refused by name (a load job), loudly",
                      res.returncode != 0 and "refused:load_job" in dbt[name]["refused"]
                      and "load jobs" in res.stdout + res.stderr,
                      {k: dbt[name][k] for k in ("rc", "refused", "stdout")})
            elif name == "incremental":
                # dbt saw the table it built (its relation cache), so the second run
                # is a MERGE into it - never a silent rebuild; what the engine does
                # with the MERGE is recorded, and a failure must name it
                merged = [q for q in sent if "merge into" in q.lower()]
                rebuilt = [q for q in sent if "create or replace table" in q.lower()
                           and "bq_orders_inc`" in q and "__dbt_tmp" not in q]
                out = res.stdout + res.stderr
                check("dbt incremental: the second run MERGEs into the table dbt sees, "
                      "never a silent rebuild; a failure is loud",
                      bool(merged) and not rebuilt
                      and (res.returncode == 0 or "MERGE" in out.upper()),
                      {"rc": res.returncode, "merge": len(merged), "rebuilt": len(rebuilt),
                       "tail": out[-800:]})
            else:
                check(f"dbt {name} through the box profile's bigquery target",
                      res.returncode == 0, (res.stdout + res.stderr)[-1200:])
        record["dbt"] = dbt
        listed = dbt["relations"]["stdout"].strip().splitlines()
        check("dbt's relation listing (its cache) sees every relation it built",
              listed[-1:] == ["bq_feb_revenue,bq_feb_revenue_v,bq_orders_inc"], listed[-3:])
        for name in ("debug", "bare debug"):
            out = dbt[name]["stdout"] + dbt[name]["tail"]
            check(f"dbt {name}: the bigquery adapter, and the connection test passes",
                  "Registered adapter: bigquery" in out and "All checks passed" in out,
                  out[-800:])

        def shown(name):
            try:
                return json.loads(dbt[name]["stdout"])["show"][0]
            except (ValueError, KeyError, IndexError, TypeError):
                return {}

        got_model, got_world = shown("show"), shown("bare show")
        record["dbt_values"] = {"model": got_model, "world_project": got_world}
        check("dbt: the model's February 2026 Net Revenue equals the world's DuckDB",
              got_model.get("net_revenue") is not None
              and float(got_model["net_revenue"]) == float(revenue),
              {"got": got_model, "expected": str(revenue)})
        check("dbt: `show --inline` with ref() in the world's own project equals the "
              "world's DuckDB",
              got_world.get("net_revenue") is not None
              and float(got_world["net_revenue"]) == float(revenue),
              {"got": got_world, "expected": str(revenue)})
        record["dbt_statements"] = {"logged": len(dbt_sent), "rows": len(dbt_rows)}
        check("audit: every statement dbt sent (its SQLQuery log) is one "
              "bigquery.statement row, by text and in order",
              bool(dbt_sent) and [r["args"]["sql"] for r in dbt_rows] == dbt_sent,
              {"logged": len(dbt_sent), "rows": len(dbt_rows),
               "first_mismatch": next(
                   ({"logged": a[:300], "row": b[:300]} for a, b in zip(
                       dbt_sent, [r["args"]["sql"] for r in dbt_rows], strict=False)
                    if a != b), None)})
        check("audit: dbt's writes are write-class rows naming their target",
              any(r["op_class"] in ("write", "ddl", "destructive") and r["exit"] == "ok"
                  and any(DBT_DATASET in t for t in r["targets"]) for r in dbt_rows),
              [{k: r[k] for k in ("seq", "op_class", "targets")} for r in dbt_rows
               if r["op_class"] != "read"][:6])

        # (c) statements = audit rows, by job id, against the engine's own job log
        listed = subprocess.run(["docker", "exec", services, PYTHON, "-c", UPSTREAM_JOBS],
                                capture_output=True, text=True, timeout=300, check=False)
        jobs = json.loads(listed.stdout.strip().splitlines()[-1]) if listed.stdout.strip() \
            else {}
        rows = bi.read_rows(audit_file)
        statements = [r for r in rows if r["tool"] == TOOL]
        by_job = {r["result"]["job_id"]: r["args"]["sql"] for r in statements
                  if r["result"].get("job_id")}
        jobless = [r["args"]["sql"] for r in statements if not r["result"].get("job_id")]
        record["counts"] = {"statement_rows": len(statements), "engine_jobs": len(jobs),
                            "statement_rows_with_job": len(by_job),
                            "statement_rows_without_job": len(jobless),
                            "rows_total": len(rows)}
        record["statements_without_job"] = jobless
        check("audit: statements = audit rows - every engine job is exactly one "
              "bigquery.statement row with its job id and text, and the reverse",
              bool(jobs) and by_job == jobs,
              {"only_in_engine": sorted(set(jobs) - set(by_job)),
               "only_in_rows": sorted(set(by_job) - set(jobs)),
               "text_differs": sorted(k for k in set(jobs) & set(by_job)
                                      if jobs[k] != by_job[k])})
        bad = [r["seq"] for r in rows if set(r) != set(ROW_KEYS)]
        check("audit: rows carry exactly the 9.2.2 keys", not bad, bad[:5])
        seqs = [r["seq"] for r in rows]
        check("audit: seq is contiguous across writers", seqs == list(range(1, len(seqs) + 1)),
              seqs[-5:])
        record["tools"] = sorted({r["tool"] for r in rows})
        record["audit_sample"] = [r for r in statements if r["op_class"] != "read"][-3:]
        allowed = aut.allowed_destinations(stack.episode_endpoints())
        record["allowed_destinations"] = sorted(f"{h}:{p}" for h, p in allowed)
        check("the profile allows bigquery.internal:9050", ("bigquery.internal", 9050)
              in allowed, record["allowed_destinations"])
    finally:
        if not args.keep and topo.stack is not None:
            topo.down()
    if not args.keep:
        dest = bi.read_rows(Path(topo.audit_dir) / "destinations.jsonl")
        record["destinations"] = dest
        bq_dest = [d for d in dest if d.get("host") == "bigquery.internal"
                   and d.get("port") == 9050]
        check("destinations.jsonl counts bigquery.internal:9050 after teardown",
              bool(bq_dest) and bq_dest[0]["count"] > 0, bq_dest)
        violations = ([str(v) for v in blindness.network_violations(
            dest, allowed, aut.normalise_host)] if allowed is not None
            else ["no allowlist: the stack never came up"])
        check("no destination outside the profile's allowlist", not violations, violations)
        warehouse_after = warehouse_section(world_root)
        record["warehouse_hash"] = {"before_up": warehouse_before, "after_down": warehouse_after}
        check("the world's warehouse never changed (after every write, dbt and teardown)",
              warehouse_after == warehouse_before, record["warehouse_hash"])
    record["checks"] = checks
    record["result"] = "PASS" if all(c["result"] == "PASS" for c in checks) else "FAIL"
    record["summary"] = (f"{sum(c['result'] == 'PASS' for c in checks)}/{len(checks)} PASS; "
                         f"seed {record.get('seed', {}) and record['seed'].get('rows')} rows "
                         f"in {record.get('seed', {}) and record['seed'].get('seconds')} s; "
                         f"{record.get('counts')}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n",
                        encoding="utf-8")
    print(f"{record['result']}: {record['summary']} -> {args.out}")
    return 0 if record["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
