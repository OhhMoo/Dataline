"""End-to-end check of the Redshift plug (the Postgres persona) in the CONTAINER topology.

    sg docker -c "gateway/.venv/bin/python tasks/harness/redshift_e2e_check.py \\
        --image alkera-episode:<tag built with --warehouse-clients redshift>"

Builds a real `commerce_analytics` world for the `de/aws` profile (warehouse:
redshift), brings up `container_episode.NativeContainerEpisode` with it - the
overlay declares the `redshift` service whole (backend_overlay): PostgreSQL 17
on loopback inside it, seeded from the world's own DuckDB warehouse, behind the
audited wire proxy at `redshift.internal:5439` - and from INSIDE the episode:

  (a) redshift_connector (ssl=False): the persona's server_version; the
      statements it sends at CONNECT (counted from the audit: none); every
      seeded table's row count against the world's DuckDB; the fixed corpus
      (gateway/tests/redshift_corpus.py: GETDATE / DATEADD / DATEDIFF / NVL /
      LISTAGG / `::` / TOP / DECODE / CONCAT ...) against DuckDB truth; a
      scratch CREATE (DISTKEY/SORTKEY/ENCODE) + INSERT + SELECT; UNLOAD and COPY
      FROM S3 refused with SQLSTATE 0A000, by name;
  (b) psql: February 2026 Net Revenue with the box's REDSHIFT_* variables, the
      persona's version(), a refusal as psql prints it;
  (c) dbt-redshift: `dbt debug` and `dbt run` (a table model with dist/sort
      config) against an explicit profile, the model's value checked; and a
      BARE `dbt debug` in the world's own project through the box profile
      (~/.dbt, native_workspace.box_profiles_yaml), whose default target is
      redshift;
  (d) test_remote_write_leaves_world_fingerprint_unchanged: CREATE / INSERT /
      UPDATE / DELETE / DROP through the plug land in the seeded copy (the
      persona's answer moves) while the world's semantic fingerprint and the
      warehouse file's bytes do not, and each write is a write-class row;
  (e) the audit: every statement the proxy handled (its private statement log,
      in the service's own volume) is ONE `redshift.statement` row in the
      rollout's actions.jsonl, in order, each a valid 9.2.2 row; seq is
      contiguous across writers; destinations.jsonl names
      redshift.internal:5439 after teardown.

Evidence: tasks/harness/evidence/redshift_e2e.json. Exit 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

PROFILE_NAME = "de/aws"
DEFAULT_IMAGE = "alkera-episode:head-redshift"
EVIDENCE = HERE / "evidence" / "redshift_e2e.json"
IN_DIR = "/tmp/rs-e2e"
PYTHON = "/usr/local/bin/python"  # absolute: the probe is the check's, not a shim row
TOOL = "redshift.statement"
REFUSAL = "Redshift feature not available in this environment: "
CORPUS_FILE = REPO / "gateway" / "tests" / "redshift_corpus.py"
WRITE_CLASSES = ("write", "ddl", "destructive")


def _corpus():
    spec = importlib.util.spec_from_file_location("redshift_corpus", CORPUS_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_world(work: Path) -> dict:
    """A fresh de/aws world: tool_tour.world_seed's seed (the BI001/BI002 seed,
    the profile record, the stack's BI vendor) built by world.build."""
    import episode
    import tool_tour

    root, grading = work / "world", work / "grading"
    shutil.rmtree(root, ignore_errors=True)
    grading.mkdir(parents=True, exist_ok=True)
    seed = work / "seed.yaml"
    seed.write_text(tool_tour.world_seed(PROFILE_NAME, {}), encoding="utf-8")
    done = subprocess.run([str(episode.PY), "-m", "world.build", "--seed", str(seed),
                           "--root", str(root)], capture_output=True, text=True,
                          env=episode.venv_env())
    if done.returncode != 0:
        raise SystemExit(f"world.build failed:\n{done.stdout}\n{done.stderr}")
    return {"world_root": str(root), "workspace": str(root / "workspace"),
            "warehouse": str(root / "var" / "warehouse" / "warehouse.duckdb"),
            "scheduler": str(root / "var" / "sched" / "state.json"),
            "dashboards": str(root / "var" / "bi" / "dashboards"),
            "profiles_dir": str(root / "build"),
            "workflow_dir": str(root / "var" / "workflow"),
            "grading": str(grading)}


def expected_from_duckdb(path: Path) -> dict:
    """Per-table row counts ("schema.table") and February 2026 Net Revenue."""
    import duckdb

    from tool_tour_probes import NET_REVENUE_FEB_2026_SQL

    conn = duckdb.connect(str(path), read_only=True)
    try:
        tables = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_catalog = current_database() AND table_schema NOT IN "
            "('information_schema', 'pg_catalog') ORDER BY 1, 2").fetchall()
        counts = {f"{s}.{t}": conn.execute(
            f'SELECT count(*) FROM warehouse."{s}"."{t}"').fetchone()[0] for s, t in tables}
        net = conn.execute(NET_REVENUE_FEB_2026_SQL).fetchone()[0]
        return {"counts": counts, "net_revenue_feb_2026": str(net)}
    finally:
        conn.close()


def fingerprint(world: dict) -> dict:
    sys.path.insert(0, str(REPO / "world"))
    from world.fingerprint import semantic_hash
    from world.hashing import file_hash

    return {"semantic_hash": semantic_hash(Path(world["world_root"])),
            "warehouse_bytes": file_hash(Path(world["warehouse"]))}


CONNECTOR = r'''
import ast, io, json, logging, os, sys
import redshift_connector

spec = json.loads(sys.argv[1])
env = os.environ
log = io.StringIO()
handler = logging.StreamHandler(log)
logging.getLogger("redshift_connector").addHandler(handler)
logging.getLogger("redshift_connector").setLevel(logging.DEBUG)

def connect():
    return redshift_connector.connect(
        host=env["REDSHIFT_HOST"], port=int(env["REDSHIFT_PORT"]),
        database=env["REDSHIFT_DATABASE"], user=env["REDSHIFT_USER"],
        password=env["REDSHIFT_PASSWORD"], ssl=False, timeout=30)

out = {"statements": [], "env": {k: env.get(k) for k in
       ("REDSHIFT_HOST", "REDSHIFT_PORT", "REDSHIFT_DATABASE", "REDSHIFT_USER")}}
if spec["mode"] == "connect":
    conn = connect()
    logging.getLogger("redshift_connector").setLevel(logging.WARNING)
    out["server_version"] = str(conn._server_version)
    startup = [line for line in log.getvalue().splitlines() if "driver_version" in line]
    out["startup_parameters"] = sorted(ast.literal_eval(startup[0]).keys()) if startup else None
    conn.close()
    print("RS-E2E " + json.dumps(out))
    raise SystemExit(0)
logging.getLogger("redshift_connector").setLevel(logging.WARNING)
conn = connect()
conn.autocommit = True
cur = conn.cursor()

def run(sql, fetch=True):
    out["statements"].append(sql)
    cur.execute(sql)
    return [[str(v) for v in row] for row in cur.fetchall()] if fetch else None

def refused(sql):
    out["statements"].append(sql)
    try:
        cur.execute(sql)
    except Exception as exc:
        info = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {"M": str(exc)}
        return {"code": info.get("C"), "message": info.get("M")}
    return None

if spec["mode"] == "read":
    out["counts"] = {name: int(run(f"SELECT count(*) FROM {name}")[0][0])
                     for name in spec["tables"]}
    out["corpus"] = {}
    for name, sql in spec["corpus"]:
        out["corpus"][name] = run(sql)[0][0]
    run("CREATE TABLE public.rs_e2e_scratch (id INT ENCODE az64, note VARCHAR(MAX) ENCODE lzo) "
        "DISTSTYLE KEY DISTKEY(id) COMPOUND SORTKEY(id)", fetch=False)
    run("INSERT INTO public.rs_e2e_scratch VALUES (1, 'alpha'), (2, 'beta')", fetch=False)
    out["read_back"] = run("SELECT id, note FROM public.rs_e2e_scratch ORDER BY id")
    out["refusals"] = {
        "UNLOAD": refused("UNLOAD ('select * from warehouse.fct_orders') "
                          "TO 's3://acme-exports/fct_orders_' IAM_ROLE "
                          "'arn:aws:iam::123456789012:role/redshift-unload'"),
        "COPY FROM S3": refused("COPY warehouse.fct_orders FROM 's3://acme-landing/orders/' "
                                "IAM_ROLE 'arn:aws:iam::123456789012:role/redshift-copy' CSV"),
    }
    out["after_refusal"] = run("SELECT 1")
elif spec["mode"] == "write":
    run("CREATE TABLE warehouse.rs_write_probe AS SELECT * FROM warehouse.fct_orders "
        "ORDER BY order_id LIMIT 5", fetch=False)
    run("INSERT INTO warehouse.fct_orders (order_id, order_date, customer_id, order_status, "
        "order_amount, shipping_amount) VALUES (999999999, '2026-02-15', 1001, 'completed', "
        "1000000.00, 0)", fetch=False)
    run("UPDATE warehouse.dim_customer SET segment = 'Rewritten' WHERE customer_id = 1001",
        fetch=False)
    run("DELETE FROM warehouse.fct_order_lines WHERE order_id = 100001", fetch=False)
    run("DROP TABLE warehouse.rs_write_probe", fetch=False)
    out["net_after_write"] = run(spec["net_sql"])[0][0]
elif spec["mode"] == "dbt_value":
    out["value"] = run("SELECT n FROM dbt_e2e.rs_check")[0][0]
cur.close()
conn.close()
print("RS-E2E " + json.dumps(out))
'''

DBT_PROJECT = """name: rs_e2e
version: "1.0"
profile: rs_e2e
config-version: 2
model-paths: [models]
"""

DBT_PROFILES = """rs_e2e:
  target: redshift
  outputs:
    redshift:
      type: redshift
      host: "{{ env_var('REDSHIFT_HOST') }}"
      port: 5439
      user: "{{ env_var('REDSHIFT_USER') }}"
      password: "{{ env_var('REDSHIFT_PASSWORD') }}"
      dbname: "{{ env_var('REDSHIFT_DATABASE') }}"
      schema: dbt_e2e
      sslmode: disable
      threads: 1
      connect_timeout: 20
"""

DBT_MODEL = ("{{ config(materialized='table', dist='order_status', sort=['order_status']) }}\n"
             "select count(*) as n, getdate() as built_at from warehouse.fct_orders\n"
             "where order_status = 'completed'\n")

PSQL = ('PGPASSWORD="$REDSHIFT_PASSWORD" psql -h "$REDSHIFT_HOST" -p "$REDSHIFT_PORT" '
        '-U "$REDSHIFT_USER" -d "$REDSHIFT_DATABASE" -v ON_ERROR_STOP=1 -tAc')


def main(argv=None) -> int:
    import container_episode as ce
    import episode
    import native_stack

    from alkera_gateway.native.audit import ROW_KEYS

    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--work", type=Path, default=Path.home() / "alkera-rs-e2e")
    parser.add_argument("--out", type=Path, default=EVIDENCE)
    parser.add_argument("--keep", action="store_true", help="leave the stack up")
    args = parser.parse_args(argv)

    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    for name in ("audit", "rollout", "dbt"):
        shutil.rmtree(work / name, ignore_errors=True)
    world = build_world(work)
    profile = episode.environment_profile(PROFILE_NAME, None)
    episode.check_world_profile(Path(world["world_root"]), profile)
    warehouse = Path(world["warehouse"])
    expected = expected_from_duckdb(warehouse)
    corpus = _corpus()
    truths = corpus.truths(warehouse)
    checks: list[dict] = []

    def check(name, ok, detail=None):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if not ok and detail else ""),
              flush=True)

    record: dict = {"profile": PROFILE_NAME, "image": args.image, "expected": expected}
    audit_file = work / "audit" / "actions.jsonl"

    def rows():
        return [json.loads(ln) for ln in audit_file.read_text(encoding="utf-8").splitlines()] \
            if audit_file.exists() else []

    def connector(spec: dict) -> tuple[dict, str]:
        done = stack.exec_([PYTHON, f"{IN_DIR}/connector.py", json.dumps(spec)], check=False,
                           timeout=600)
        line = next((ln for ln in done.stdout.splitlines() if ln.startswith("RS-E2E ")), None)
        return (json.loads(line[7:]) if line else {}), (done.stdout + done.stderr)[-1500:]

    stack = ce.NativeContainerEpisode(
        project=f"alkera-rs-e2e-{int(time.time())}", world=world,
        audit_dir=work / "audit", rollout_dir=work / "rollout", image=args.image,
        profile=PROFILE_NAME, plugs=None)
    redshift = None
    try:
        record["discovery"] = native_stack.stage_discovery(world)
        started = time.monotonic()
        record["container_id"] = stack.up()[:12]
        record["stack_up_seconds"] = round(time.monotonic() - started, 1)
        record["prepared"] = stack.prepare()
        record["overlay"] = stack.overlay.read_text(encoding="utf-8")
        ids = stack._compose("ps", "-q", "redshift").stdout.split()
        redshift = ids[0] if ids else None
        check("redshift service running under the profile", bool(redshift))
        logs = subprocess.run(["docker", "logs", redshift], capture_output=True,
                              text=True).stdout
        seeds = [json.loads(ln) for ln in logs.splitlines() if ln.startswith('{"seed"')]
        record["seed_log"] = seeds
        record["postgres_version"] = subprocess.run(
            ["docker", "exec", redshift, "/usr/lib/postgresql/17/bin/postgres", "--version"],
            capture_output=True, text=True).stdout.strip()
        seeded = {f"{r['schema']}.{r['table']}": r["rows"] for r in (seeds[0]["seed"]
                                                                       if seeds else [])}
        check("seeded: every world table is in dev, with the world's row count",
              seeded == expected["counts"], {"seeded": seeded, "expected": expected["counts"]})

        stack.exec_(["mkdir", "-p", f"{IN_DIR}/models"])
        probe = work / "connector.py"
        probe.write_text(CONNECTOR, encoding="utf-8")
        stack.copy_in(probe, f"{IN_DIR}/connector.py")

        # (a) redshift_connector: connect only, then reads, corpus, scratch, refusals
        before = len(rows())
        got, tail = connector({"mode": "connect"})
        connect_rows = len(rows()) - before
        record["connector_connect"] = {**got, "statements_at_connect": connect_rows}
        check("connector: connects with ssl=False through the proxy", bool(got), tail)
        check("connector: sends no statement at connect (startup reply only)",
              connect_rows == 0, connect_rows)
        check("connector: the persona reports Redshift's server_version 8.0.2",
              got.get("server_version") == "8.0.2", got.get("server_version"))
        before = len(rows())
        spec = {"mode": "read", "tables": sorted(expected["counts"]),
                "corpus": [[name, sql] for name, sql, _ in corpus.CORPUS]}
        got, tail = connector(spec)
        record["connector"] = got
        check("connector probe ran inside the episode", bool(got), tail)
        check("connector: every table's row count equals the world's DuckDB",
              got.get("counts") == expected["counts"],
              {"got": got.get("counts"), "expected": expected["counts"]})
        corpus_results = {name: {"redshift_sql": sql, "value": (got.get("corpus") or {}).get(name),
                                 "truth": str(truths[name]),
                                 "equal": corpus.same((got.get("corpus") or {}).get(name),
                                                      truths[name])}
                          for name, sql, _ in corpus.CORPUS}
        record["corpus"] = corpus_results
        check(f"corpus: all {len(corpus.CORPUS)} Redshift queries equal the DuckDB truth",
              all(r["equal"] for r in corpus_results.values()),
              {n: r for n, r in corpus_results.items() if not r["equal"]})
        check("scratch CREATE (DISTKEY/SORTKEY/ENCODE) + INSERT reads back",
              got.get("read_back") == [["1", "alpha"], ["2", "beta"]], got.get("read_back"))
        for feature, error in (got.get("refusals") or {}).items():
            check(f"refused by name with 0A000: {feature}",
                  bool(error) and error.get("code") == "0A000"
                  and error.get("message") == REFUSAL + feature, error)
        check("the connection is usable after a refusal", got.get("after_refusal") == [["1"]],
              got.get("after_refusal"))
        probe_rows = [r for r in rows()[before:] if r["tool"] == TOOL]
        sent = got.get("statements", [])
        check("audit: one redshift.statement row per connector statement, in order",
              [r["args"]["sql"] for r in probe_rows] == sent,
              {"sent": len(sent), "rows": len(probe_rows)})
        refused_rows = [r for r in probe_rows if r["exit"] == "error"]
        check("audit: each refusal is an error row with 0A000",
              len(refused_rows) == 2 and all(r["error"]["code"] == "0A000" for r in refused_rows),
              [r["error"] for r in refused_rows])
        record["audit_sample"] = probe_rows[:2] + refused_rows[:1]

        # (b) psql
        before = len(rows())
        net = stack.exec_(["bash", "-c", f"{PSQL} \"SELECT SUM(order_amount) FROM "
                           "warehouse.fct_orders WHERE order_status = 'completed' AND "
                           "order_date >= '2026-02-01' AND order_date < '2026-03-01'\""],
                          check=False, timeout=120)
        version = stack.exec_(["bash", "-c", f"{PSQL} 'SELECT version()'"], check=False)
        unload = stack.exec_(["bash", "-c", f"{PSQL} \"UNLOAD ('select 1') TO "
                              "'s3://acme-exports/x' IAM_ROLE 'arn:aws:iam::1:role/r'\""],
                             check=False)
        record["psql"] = {"net_revenue": net.stdout.strip() or net.stderr[-400:],
                          "version": version.stdout.strip(),
                          "unload": {"rc": unload.returncode, "stderr": unload.stderr.strip()}}
        check("psql: February 2026 Net Revenue equals the world's",
              net.returncode == 0 and corpus.same(net.stdout.strip(),
                                                  expected["net_revenue_feb_2026"]),
              record["psql"]["net_revenue"])
        check("psql: version() is the persona's Redshift",
              "Redshift" in version.stdout and "PostgreSQL 8.0.2" in version.stdout,
              version.stdout[-300:] or version.stderr[-300:])
        check("psql: a refusal is an ERROR naming the feature",
              unload.returncode != 0 and REFUSAL + "UNLOAD" in unload.stderr,
              unload.stderr[-400:])
        psql_rows = [r for r in rows()[before:] if r["tool"] == TOOL]
        check("audit: psql's three statements are three rows", len(psql_rows) == 3,
              [r["args"]["sql"][:60] for r in psql_rows])

        # (c) dbt-redshift, explicit profile, then the box profile
        files = {"dbt_project.yml": DBT_PROJECT, "profiles.yml": DBT_PROFILES,
                 "models/rs_check.sql": DBT_MODEL}
        for rel, text in files.items():
            local = work / "dbt" / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(text, encoding="utf-8")
            stack.copy_in(local, f"{IN_DIR}/{rel}")
        dbt_before = len(rows())
        dbt = {}
        for verb in (["debug"], ["run", "-s", "rs_check"]):
            res = stack.exec_(["sh", "-c", f"cd {IN_DIR} && dbt {' '.join(verb)} "
                               f"--project-dir {IN_DIR} --profiles-dir {IN_DIR}"],
                              check=False, timeout=600)
            dbt[verb[0]] = {"rc": res.returncode, "tail": (res.stdout + res.stderr)[-1200:]}
            check(f"dbt {' '.join(verb)} against the redshift target", res.returncode == 0,
                  dbt[verb[0]]["tail"])
        got, tail = connector({"mode": "dbt_value"})
        completed = int(truths["nvl_counts_completed"])
        check("dbt model's value equals the world's DuckDB",
              str(got.get("value")) == str(completed),
              {"got": got.get("value"), "expected": completed, "tail": tail[-300:]})
        import yaml

        box = stack.exec_(["cat", "/root/.dbt/profiles.yml"], check=False)
        record["box_profile"] = box.stdout
        try:
            box_profile = (yaml.safe_load(box.stdout) or {}).get("world_dbt") or {}
        except yaml.YAMLError:
            box_profile = {}
        outputs = box_profile.get("outputs") or {}
        check("box profile (~/.dbt) defaults to the redshift target, DuckDB dev kept",
              box_profile.get("target") == "redshift"
              and outputs.get("dev", {}).get("type") == "duckdb"
              and outputs.get("redshift", {}).get("type") == "redshift"
              and outputs.get("redshift", {}).get("sslmode") == "disable",
              box.stdout[-800:] or box.stderr[-400:])
        bare = stack.exec_(["sh", "-c", "cd /workspace/dbt_project && dbt debug"],
                           check=False, timeout=600)
        bare_out = bare.stdout + bare.stderr
        dbt["bare_debug"] = {"rc": bare.returncode, "tail": bare_out[-1200:]}
        check("bare `dbt debug` connects to redshift through the box profile",
              bare.returncode == 0 and "Registered adapter: redshift" in bare_out,
              dbt["bare_debug"]["tail"])
        record["dbt"] = dbt
        dbt_rows = [r for r in rows()[dbt_before:] if r["tool"] == TOOL]
        check("audit: dbt's statements are redshift.statement rows", len(dbt_rows) > 0,
              len(dbt_rows))
        record["dbt_rows"] = len(dbt_rows)

        # (d) a write through the plug leaves the world untouched
        print("test_remote_write_leaves_world_fingerprint_unchanged", flush=True)
        from tool_tour_probes import NET_REVENUE_FEB_2026_SQL

        # the world's truth SQL, on the persona's names (database dev, schema warehouse)
        net_sql = NET_REVENUE_FEB_2026_SQL.replace("warehouse.warehouse.", "warehouse.")
        fp_before = fingerprint(world)
        before = len(rows())
        got, tail = connector({"mode": "write", "net_sql": net_sql})
        fp_after = fingerprint(world)
        write_rows = [r for r in rows()[before:] if r["tool"] == TOOL]
        record["remote_write"] = {"fingerprint_before": fp_before, "fingerprint_after": fp_after,
                                  "net_after_write": got.get("net_after_write"),
                                  "rows": [(r["op_class"], r["args"]["sql"][:60])
                                           for r in write_rows]}
        check("remote write: the writes landed in the persona's seeded copy",
              got.get("net_after_write") is not None and not corpus.same(
                  got.get("net_after_write"), expected["net_revenue_feb_2026"]),
              {"got": got.get("net_after_write"), "world": expected["net_revenue_feb_2026"],
               "tail": tail[-300:]})
        check("test_remote_write_leaves_world_fingerprint_unchanged",
              fp_before == fp_after, {"before": fp_before, "after": fp_after})
        check("remote write: the world's DuckDB still answers the original truth",
              expected_from_duckdb(warehouse)["net_revenue_feb_2026"]
              == expected["net_revenue_feb_2026"])
        check("remote write: each write statement is a write-class row",
              [r["op_class"] in WRITE_CLASSES for r in write_rows[:5]] == [True] * 5,
              record["remote_write"]["rows"])

        # (e) the audit, whole
        log = subprocess.run(["docker", "exec", redshift, "cat",
                              "/redshift-data/statements.jsonl"], capture_output=True,
                             text=True).stdout
        logged = [json.loads(ln)["sql"] for ln in log.splitlines() if ln.strip()]
        all_rows = rows()
        audited = [r["args"]["sql"] for r in all_rows if r["tool"] == TOOL]
        check("audit: every statement the proxy handled is exactly one row "
              "(statement log 1:1)", logged == audited,
              {"statement_log": len(logged), "audit_rows": len(audited)})
        bad = [r["seq"] for r in all_rows if r["tool"] == TOOL and set(r) != set(ROW_KEYS)]
        check("audit: rows carry exactly the 9.2.2 keys", not bad, bad[:5])
        seqs = [r["seq"] for r in all_rows]
        check("audit: seq is contiguous across writers", seqs == list(range(1, len(seqs) + 1)),
              seqs[-5:])
        record["counts"] = {"statement_log": len(logged), "audit_rows": len(audited),
                            "dbt_rows": len(dbt_rows), "connector_rows": len(probe_rows),
                            "psql_rows": len(psql_rows), "write_rows": len(write_rows),
                            "translated_rows": sum(1 for r in all_rows if r["tool"] == TOOL
                                                   and "translated" in r["flags"])}
    finally:
        if not args.keep:
            stack.down()
    if not args.keep:
        dest = [json.loads(ln) for ln in (work / "audit" / "destinations.jsonl")
                .read_text(encoding="utf-8").splitlines() if ln.strip()]
        rs_dest = [d for d in dest if d.get("host") == "redshift.internal"
                   and d.get("port") == 5439]
        check("destinations.jsonl counts redshift.internal:5439 after teardown",
              bool(rs_dest) and rs_dest[0]["count"] > 0, rs_dest)
        record["destinations"] = dest
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
