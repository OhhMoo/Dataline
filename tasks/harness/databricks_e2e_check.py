"""End-to-end check of the Databricks plug (tier 1) in the CONTAINER topology.

    sg docker -c "gateway/.venv/bin/python tasks/harness/databricks_e2e_check.py \\
        --image alkera-episode:w1-databricks"

Builds the `commerce_analytics` world (seed 11) for the `de/databricks` profile, brings
up `container_episode.NativeContainerEpisode` with it - native-services then hosts the
fake workspace at `https://databricks.internal` (port 443, a certificate minted at boot,
trusted by the episode through REQUESTS_CA_BUNDLE and SSL_CERT_FILE), seeded from the
world's DuckDB warehouse into its own state - and from INSIDE the episode container:

  (a) databricks-sdk, configured from the box's variables alone: a Statement Execution
      query whose value must equal the world's DuckDB, SCIM Me, Unity Catalog's three
      levels, the SQL warehouse, Jobs;
  (b) REST with curl: the same statement, and an unknown route in Databricks' 404 shape;
  (c) databricks-sql-connector with use_sea=True (the spike's transport): the value
      again, and `cursor.tables()`; its default Thrift transport refused loudly;
  (d) dbt-databricks: a BARE `dbt debug` in the world's own project through the box
      profile (~/.dbt, default target databricks), and `dbt run` of a scratch model
      whose value is read back through the SDK;
  (e) a Delta-only statement (DESCRIBE HISTORY) fails naming the feature;
  (f) remote writes - CREATE, INSERT, and a DELETE on the copy's fct_orders - land in
      the copy: they read back through the workspace, and the world's semantic
      fingerprint is unchanged (`test_remote_write_leaves_world_fingerprint_unchanged`);
      each is a write-class `databricks.statement` row;
  (g) Jobs run-now on `bi_refresh` is the world scheduler's trigger: one manual run in
      the ledger, the clock moved by that run's own duration only, `runs/get` agrees;
  (h) the audit: every probe statement is exactly one `databricks.statement` row, in
      order; every row a valid 9.2.2 row; seq contiguous; destinations.jsonl names
      databricks.internal:443 after teardown.

Evidence: tasks/harness/evidence/databricks_e2e.json. Exit 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "world"))

PROFILE_NAME = "de/databricks"
DEFAULT_IMAGE = "alkera-episode:head-databricks"
EVIDENCE = HERE / "evidence" / "databricks_e2e.json"
PYTHON = "/usr/local/bin/python"  # absolute: the probe is the check's, not a shim row
IN_DIR = "/tmp/dbx-e2e"
TOOL = "databricks.statement"
JOB = "bi_refresh"

SDK_PROBE = r'''
import json, os, sys
from databricks.sdk import WorkspaceClient
spec = json.loads(sys.argv[1])
w = WorkspaceClient()            # DATABRICKS_HOST + DATABRICKS_TOKEN, from the box
out = {"statements": [], "env": {k: os.environ.get(k) for k in (
    "DATABRICKS_HOST", "DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HTTP_PATH",
    "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")}}
def run(sql):
    out["statements"].append(sql)
    r = w.statement_execution.execute_statement(warehouse_id="alkera", statement=sql)
    if r.status.state.value != "SUCCEEDED":
        return {"state": r.status.state.value, "error": r.status.error.message}
    return {"state": "SUCCEEDED", "rows": r.result.data_array or []}
out["feb"] = run(spec["feb_sql"])
out["counts"] = {t: run(f"select count(*) from main.warehouse.{t}")["rows"][0][0]
                 for t in spec["tables"]}
out["me"] = w.current_user.me().user_name
out["catalogs"] = [c.name for c in w.catalogs.list()]
out["schemas"] = sorted(s.name for s in w.schemas.list(catalog_name="main"))
out["tables"] = sorted(t.name for t in w.tables.list(catalog_name="main",
                                                     schema_name="warehouse"))
fct = w.tables.get("main.warehouse.fct_orders")
out["fct_columns"] = [(c.name, c.type_name.value) for c in fct.columns]
out["warehouses"] = [(x.id, x.state.value) for x in w.warehouses.list()]
out["jobs"] = [(j.job_id, j.settings.name) for j in w.jobs.list()]
out["delta"] = run("DESCRIBE HISTORY main.warehouse.fct_orders")
out["writes"] = [run(sql) for sql in spec["writes"]]
out["scratch"] = run("select id, note from main.default.e2e_scratch order by id")
out["fct_after_delete"] = run("select count(*) from main.warehouse.fct_orders "
                              "where order_status = 'returned'")
print("DBX-SDK " + json.dumps(out, default=str))
'''

CONNECTOR_PROBE = r'''
import json, os, sys
from databricks import sql
spec = json.loads(sys.argv[1])
env, out = os.environ, {"statements": []}
with sql.connect(server_hostname=env["DATABRICKS_SERVER_HOSTNAME"],
                 http_path=env["DATABRICKS_HTTP_PATH"], access_token=env["DATABRICKS_TOKEN"],
                 use_sea=True) as conn:
    with conn.cursor() as cur:
        out["statements"].append(spec["feb_sql"])
        cur.execute(spec["feb_sql"])
        out["feb"] = str(cur.fetchone()[0])
        cur.tables(catalog_name="main", schema_name="warehouse")
        out["tables"] = sorted(r[2] for r in cur.fetchall())
try:
    sql.connect(server_hostname=env["DATABRICKS_SERVER_HOSTNAME"],
                http_path=env["DATABRICKS_HTTP_PATH"], access_token=env["DATABRICKS_TOKEN"],
                _retry_stop_after_attempts_count=1)
    out["thrift"] = "connected"
except Exception as exc:
    out["thrift"] = str(exc)
print("DBX-CONN " + json.dumps(out, default=str))
'''

JOBS_PROBE = r'''
import json, sys
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
job_id = dict((name, i) for i, name in [(j.job_id, j.settings.name) for j in w.jobs.list()])[
    sys.argv[1]]
run = w.jobs.run_now(job_id=job_id)
got = w.jobs.get_run(run_id=run.run_id)
print("DBX-JOBS " + json.dumps({"job_id": job_id, "run_id": run.run_id,
      "state": [got.state.life_cycle_state.value, got.state.result_state.value],
      "trigger": got.trigger.value, "run_name": got.run_name}))
'''

DBT_PROJECT = """name: dbx_e2e
version: "1.0"
profile: dbx_e2e
config-version: 2
model-paths: [models]
"""

DBT_PROFILES = """dbx_e2e:
  target: databricks
  outputs:
    databricks:
      type: databricks
      host: "{{ env_var('DATABRICKS_SERVER_HOSTNAME') }}"
      http_path: "{{ env_var('DATABRICKS_HTTP_PATH') }}"
      token: "{{ env_var('DATABRICKS_TOKEN') }}"
      catalog: main
      schema: dbt_e2e
      threads: 1
      connection_parameters:
        use_sea: true
"""


def truths(world_root: str) -> dict:
    import tool_tour_core as core
    from tool_tour_probes import NET_REVENUE_FEB_2026_SQL

    import duckdb

    con = duckdb.connect(str(Path(world_root) / core.WAREHOUSE_REL), read_only=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = "
            "current_database() AND table_schema = 'warehouse' ORDER BY 1").fetchall()]
        counts = {t: con.execute(f'SELECT count(*) FROM warehouse.warehouse."{t}"')
                  .fetchone()[0] for t in tables}
        returned = con.execute("SELECT count(*) FROM warehouse.warehouse.fct_orders "
                               "WHERE order_status = 'returned'").fetchone()[0]
    finally:
        con.close()
    return {"feb": core.truth(NET_REVENUE_FEB_2026_SQL, world_root), "counts": counts,
            "returned": returned}


def main(argv=None) -> int:
    import bi_e2e_check as bi
    import tool_tour as tour

    from world import fingerprint

    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--work", type=Path,
                        default=Path.home() / "alkera-rollouts" / "w1-databricks" / "e2e")
    parser.add_argument("--out", type=Path, default=EVIDENCE)
    parser.add_argument("--keep", action="store_true", help="leave the stack up")
    args = parser.parse_args(argv)

    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    for name in ("audit", "rollout", "scripts", "dbt"):
        shutil.rmtree(work / name, ignore_errors=True)
    seed = tour.world_seed(PROFILE_NAME, {})
    with tour.as_profile(PROFILE_NAME, {}, seed):
        world = bi.build_world(work)
    root = Path(world["world_root"])
    truth = truths(world["world_root"])
    fp_before = fingerprint.semantic_hash(root)
    sections_before = fingerprint.semantic(root)

    def moved(since: dict) -> list[str]:
        now = fingerprint.semantic(root)
        return sorted(k for k in set(now) | set(since) if now.get(k) != since.get(k))
    checks: list[dict] = []

    def check(name, ok, detail=None):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if not ok and detail else ""),
              flush=True)

    record: dict = {"profile": PROFILE_NAME, "image": args.image, "truth": truth,
                    "fingerprint_before": fp_before}
    topo = tour.TourTopology(world, work, args.image, PROFILE_NAME, {}, seed)
    audit_file = work / "audit" / "actions.jsonl"

    def rows() -> list[dict]:
        return [json.loads(ln) for ln in audit_file.read_text(encoding="utf-8").splitlines()
                if ln.strip()] if audit_file.exists() else []

    def probe(name: str, script: str, *argv: str, marker: str, timeout: int = 600):
        path = topo.put(name, script)
        done = topo.exec_raw([PYTHON, path, *argv], timeout=timeout)
        line = next((ln for ln in done.stdout.splitlines() if ln.startswith(marker + " ")),
                    None)
        return (json.loads(line[len(marker) + 1:]) if line else None), done

    feb_sql = ("select sum(order_amount) from main.warehouse.fct_orders where order_status "
               "= 'completed' and order_date >= date'2026-02-01' and order_date < "
               "date'2026-03-01'")
    writes = ["CREATE TABLE main.default.e2e_scratch (id INT, note STRING) USING DELTA",
              "INSERT INTO main.default.e2e_scratch VALUES (1, 'alpha'), (2, 'beta')",
              "DELETE FROM main.warehouse.fct_orders WHERE order_status = 'returned'"]
    try:
        up = topo.up()
        # Taken with the stack up: bringing it up writes the box's discovery files
        # into the world's workspace (native_stack.stage_discovery), which the
        # fingerprint covers. Everything after this is the workspace's doing.
        fp_up = fingerprint.semantic_hash(root)
        record["fingerprint_up"] = fp_up
        record["fingerprint_sections_moved_by_up"] = moved(sections_before)
        sections_up = fingerprint.semantic(root)
        record["stack"] = {k: up.get(k) for k in ("container_id", "image", "services",
                                                   "episode_endpoints")}
        env = topo.container_env
        check("episode env: the workspace and both trust knobs",
              env.get("DATABRICKS_HOST") == "https://databricks.internal"
              and env.get("REQUESTS_CA_BUNDLE") == "/etc/alkera/databricks-tls/bundle.pem"
              and env.get("SSL_CERT_FILE") == "/etc/alkera/databricks-tls/bundle.pem",
              {k: env.get(k) for k in ("DATABRICKS_HOST", "REQUESTS_CA_BUNDLE",
                                       "SSL_CERT_FILE")})

        # (a) the SDK
        before = len(rows())
        spec = {"feb_sql": feb_sql, "tables": sorted(truth["counts"]), "writes": writes}
        sdk, done = probe("sdk.py", SDK_PROBE, json.dumps(spec), marker="DBX-SDK")
        check("SDK probe ran inside the episode", sdk is not None,
              (done.stdout + done.stderr)[-1500:])
        sdk = sdk or {}
        record["sdk"] = sdk
        feb = (sdk.get("feb") or {}).get("rows") or [[None]]
        check("SDK statement: February 2026 Net Revenue equals the world's DuckDB",
              tour.same_value(feb[0][0], truth["feb"]), {"got": feb, "truth": truth["feb"]})
        check("SDK statements: every warehouse table's count equals the world's DuckDB",
              {t: int(n) for t, n in (sdk.get("counts") or {}).items()} == truth["counts"],
              {"got": sdk.get("counts"), "truth": truth["counts"]})
        check("SCIM Me, Unity Catalog's three levels, the warehouse, Jobs",
              sdk.get("me") == "alkera-local-dev" and sdk.get("catalogs") == ["main"]
              and {"default", "warehouse", "information_schema"} <= set(sdk.get("schemas", []))
              and set(sdk.get("tables", [])) == set(truth["counts"])
              and ["alkera", "RUNNING"] in [list(x) for x in sdk.get("warehouses", [])]
              and JOB in [name for _, name in sdk.get("jobs", [])],
              {k: sdk.get(k) for k in ("me", "catalogs", "schemas", "warehouses", "jobs")})
        delta = sdk.get("delta") or {}
        check("Delta-only SQL refused by name (DESCRIBE HISTORY)",
              delta.get("state") == "FAILED" and "DESCRIBE HISTORY" in (delta.get("error") or ""),
              delta)
        check("remote writes succeed on the copy and read back",
              all(w.get("state") == "SUCCEEDED" for w in sdk.get("writes", []))
              and (sdk.get("scratch") or {}).get("rows") == [["1", "alpha"], ["2", "beta"]]
              and (sdk.get("fct_after_delete") or {}).get("rows") == [["0"]],
              {k: sdk.get(k) for k in ("writes", "scratch", "fct_after_delete")})
        fp_writes = fingerprint.semantic_hash(root)
        check("test_remote_write_leaves_world_fingerprint_unchanged",
              fp_writes == fp_up and truths(world["world_root"])["returned"]
              == truth["returned"],
              {"up": fp_up, "after_writes": fp_writes, "moved": moved(sections_up)})
        probe_rows = [r for r in rows()[before:] if r["tool"] == TOOL]
        sent = sdk.get("statements", [])
        check("audit: one databricks.statement row per SDK statement, in order",
              [r["args"]["sql"] for r in probe_rows] == sent,
              {"sent": len(sent), "rows": len(probe_rows)})
        by_sql = {r["args"]["sql"]: r for r in probe_rows}
        check("audit: the remote writes are write-class rows",
              [by_sql.get(sql, {}).get("op_class") for sql in writes]
              == ["ddl", "write", "destructive"],
              [by_sql.get(sql, {}).get("op_class") for sql in writes])
        record["audit_sample"] = probe_rows[:2] + probe_rows[-2:]

        # (b) REST with curl
        body = topo.put("feb.json", json.dumps({"warehouse_id": "alkera",
                                                "statement": feb_sql}))
        curl = topo.run(
            'curl -sS --fail-with-body -H "Authorization: Bearer $DATABRICKS_TOKEN" '
            '-H "Content-Type: application/json" "$DATABRICKS_HOST/api/2.0/sql/statements" '
            f"-d @{body}")
        try:
            got = json.loads(curl.stdout)["result"]["data_array"][0][0]
        except (ValueError, KeyError, IndexError, TypeError):
            got = None
        check("REST (curl, trusting SSL_CERT_FILE's bundle): the same value",
              tour.same_value(got, truth["feb"]), (curl.stdout + curl.stderr)[-600:])
        unknown = topo.run('curl -sS -H "Authorization: Bearer $DATABRICKS_TOKEN" '
                           '"$DATABRICKS_HOST/api/2.0/clusters/list"')
        check("an unknown route is Databricks' 404 envelope, audited",
              '"error_code":"ENDPOINT_NOT_FOUND"' in unknown.stdout.replace(" ", "")
              and rows()[-1]["tool"] == "databricks.unknown_route",
              unknown.stdout[-300:])

        # (c) the SQL connector
        before = len(rows())
        conn, done = probe("connector.py", CONNECTOR_PROBE, json.dumps({"feb_sql": feb_sql}),
                           marker="DBX-CONN")
        record["connector"] = conn
        conn = conn or {}
        check("databricks-sql-connector (use_sea=True): the same value",
              tour.same_value(conn.get("feb"), truth["feb"]),
              conn or (done.stdout + done.stderr)[-1500:])
        check("databricks-sql-connector: cursor.tables() lists the warehouse schema",
              set(conn.get("tables") or []) == set(truth["counts"]), conn.get("tables"))
        check("the connector's default Thrift transport is refused, naming use_sea=True",
              "use_sea=True" in (conn.get("thrift") or ""), conn.get("thrift"))
        conn_tools = sorted({r["tool"] for r in rows()[before:]})
        record["connector_tools"] = conn_tools
        check("the connector met no unknown route (feature flags, closes, telemetry served)",
              "databricks.unknown_route" not in conn_tools, conn_tools)

        # (d) dbt-databricks
        import yaml

        box = topo.exec_raw(["cat", "/root/.dbt/profiles.yml"])
        record["box_profile"] = box.stdout
        try:
            profile = (yaml.safe_load(box.stdout) or {}).get("world_dbt") or {}
        except yaml.YAMLError:
            profile = {}
        target = (profile.get("outputs") or {}).get("databricks") or {}
        check("box profile (~/.dbt) defaults to the databricks target, DuckDB dev kept",
              profile.get("target") == "databricks"
              and (profile.get("outputs") or {}).get("dev", {}).get("type") == "duckdb"
              and target.get("type") == "databricks"
              and target.get("connection_parameters") == {"use_sea": True},
              box.stdout[-800:])
        dbt = {}
        bare = topo.run("cd /workspace/dbt_project && dbt debug", timeout=900)
        dbt["bare_debug"] = {"rc": bare.returncode, "tail": (bare.stdout + bare.stderr)[-1500:]}
        check("bare `dbt debug` connects to databricks through the box profile",
              bare.returncode == 0 and "databricks" in bare.stdout
              and "All checks passed" in bare.stdout, dbt["bare_debug"]["tail"])
        topo.exec_raw(["mkdir", "-p", f"{IN_DIR}/models"])
        files = {"dbt_project.yml": DBT_PROJECT, "profiles.yml": DBT_PROFILES,
                 "models/dbx_check.sql": "{{ config(materialized='table') }}\n" + feb_sql + "\n"}
        for rel, text in files.items():
            local = work / "dbt" / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(text, encoding="utf-8")
            topo.stack.copy_in(local, f"{IN_DIR}/{rel}")
        dbt_before = len(rows())
        for verb in ("debug", "run -s dbx_check"):
            res = topo.run(f"cd {IN_DIR} && dbt {verb} --project-dir {IN_DIR} "
                           f"--profiles-dir {IN_DIR}", timeout=900)
            dbt[verb.split()[0]] = {"rc": res.returncode,
                                    "tail": (res.stdout + res.stderr)[-1500:]}
            check(f"dbt {verb} against the databricks target", res.returncode == 0,
                  dbt[verb.split()[0]]["tail"])
        record["dbt"] = dbt
        dbt_rows = [r for r in rows()[dbt_before:] if r["tool"] == TOOL]
        check("audit: dbt's statements are databricks.statement rows", len(dbt_rows) > 0,
              len(dbt_rows))
        model = topo.run(
            'curl -sS -H "Authorization: Bearer $DATABRICKS_TOKEN" '
            '"$DATABRICKS_HOST/api/2.0/sql/statements" '
            "-d '{\"warehouse_id\": \"alkera\", \"statement\": "
            "\"select * from main.dbt_e2e.dbx_check\"}'")
        try:
            model_value = json.loads(model.stdout)["result"]["data_array"][0][0]
        except (ValueError, KeyError, IndexError, TypeError):
            model_value = None
        check("the dbt model's value equals the world's DuckDB",
              tour.same_value(model_value, truth["feb"]),
              {"got": model_value, "truth": truth["feb"], "out": model.stdout[-300:]})

        # (g) Jobs run-now over the world scheduler
        state_file = root / "var" / "sched" / "state.json"
        ledger_before = json.loads(state_file.read_text(encoding="utf-8"))
        jobs, done = probe("jobs.py", JOBS_PROBE, JOB, marker="DBX-JOBS")
        ledger_after = json.loads(state_file.read_text(encoding="utf-8"))
        record["jobs"] = {"probe": jobs, "runs_before": len(ledger_before["runs"]),
                          "runs_after": len(ledger_after["runs"]),
                          "vnow": [ledger_before["vnow"], ledger_after["vnow"]]}
        last = ledger_after["runs"][-1] if ledger_after["runs"] else {}
        jobs = jobs or {}
        check("run-now is one manual run in the world's ledger, the clock moved by it alone",
              len(ledger_after["runs"]) == len(ledger_before["runs"]) + 1
              and last.get("job") == JOB and last.get("trigger") == "manual"
              and ledger_after["vnow"] == last.get("vts_end")
              and jobs.get("run_id") == len(ledger_after["runs"])
              and jobs.get("trigger") == "ONE_TIME"
              and jobs.get("state", [None])[0] == "TERMINATED",
              {"probe": jobs or (done.stdout + done.stderr)[-800:], "run": last})
        check("audit: run-now is a databricks.sched_trigger row (blindness attributes it)",
              any(r["tool"] == "databricks.sched_trigger" and r["op_class"] == "exec"
                  for r in rows()), sorted({r["tool"] for r in rows()}))

        # (h) the audit, whole
        everything = rows()
        bad = {r["seq"]: problems for r in everything if r["tool"].startswith("databricks.")
               and (problems := bi.validate_row(r))}
        check("audit: every databricks row is a valid 9.2.2 row", not bad,
              dict(list(bad.items())[:5]))
        seqs = [r["seq"] for r in everything]
        check("audit: seq is contiguous across writers", seqs == list(range(1, len(seqs) + 1)),
              seqs[-5:])
        unknown_rows = [r["args"].get("path") for r in everything
                        if r["tool"] == "databricks.unknown_route"]
        check("audit: the only unknown route is the deliberate one",
              unknown_rows == ["/api/2.0/clusters/list"], unknown_rows)
        record["tools"] = sorted({r["tool"] for r in everything})
        record["counts"] = {"rows": len(everything),
                            "statements": sum(r["tool"] == TOOL for r in everything),
                            "dbt_statements": len(dbt_rows)}
    finally:
        if not args.keep:
            topo.down()
    if not args.keep:
        dest = [json.loads(ln) for ln in (work / "audit" / "destinations.jsonl")
                .read_text(encoding="utf-8").splitlines() if ln.strip()]
        dbx_dest = [d for d in dest if d.get("host") == "databricks.internal"
                    and d.get("port") == 443]
        check("destinations.jsonl counts databricks.internal:443 after teardown",
              bool(dbx_dest) and dbx_dest[0]["count"] > 0, dbx_dest)
        record["destinations"] = dest
        import agent_under_test as aut

        allowed = aut.allowed_destinations(record["stack"]["episode_endpoints"])
        outside = tour.disallowed({(d["host"], d["port"]): d["count"] for d in dest},
                                  allowed)
        check("no destination outside the profile's endpoint map", not outside, outside)
    record["checks"] = checks
    record["result"] = "PASS" if all(c["result"] == "PASS" for c in checks) else "FAIL"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n",
                        encoding="utf-8")
    print(f"{record['result']}: {sum(c['result'] == 'PASS' for c in checks)}/{len(checks)} "
          f"-> {args.out}")
    return 0 if record["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
