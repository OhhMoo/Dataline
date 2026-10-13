"""End-to-end check of Databricks tier 2 (Spark Connect) in the CONTAINER topology.

    sg docker -c "gateway/.venv/bin/python tasks/harness/spark_e2e_check.py \\
        --image alkera-episode:t14-ds-databricks-spark"

Builds the `commerce_analytics` world (seed 11) for the `ds/databricks` profile and
brings the stack up the way the tool tour does (`tool_tour.TourTopology`): compose
profile `spark` builds and starts the engine (gateway/docker/spark/Dockerfile.spark) on
backend_net, seeded from the world's warehouse files at /seed; native-services joins
backend_net, waits at import for the engine to accept, and serves the audited proxy as
`spark_connect.internal:15002` on episode_net. From INSIDE the episode container, with
Spark's own thin client and the box's SPARK_REMOTE alone:

  (a) SQL and the DataFrame API: every seeded warehouse table's count, February 2026 Net
      Revenue (spark.sql), and a filter / groupBy / agg (Net Revenue by status) equal the
      world's DuckDB, read on the host; `python -c` works (the box's SPARK_HOME);
  (b) writes on the copy: CREATE SCHEMA, df.write.saveAsTable, INSERT and a DELETE on the
      copy's fct_orders read back through Spark, and DESCRIBE HISTORY answers - this is
      Delta; each a write-class `databricks.spark` row
      (`test_remote_write_leaves_world_fingerprint_unchanged`: the world's semantic
      fingerprint does not move); tier 1's SQL warehouse still answers the world's
      count for fct_orders - two data planes, by design;
  (c) the fences, each named in the client's exception: AddArtifacts, a Python UDF, a
      path read, TRANSFORM, reflect(), parquet.`<path>`, a fenced Config key;
  (d) determinism: the ordered rows of fct_orders hashed in 3 sessions, one hash, equal
      to the same rows read from the world's DuckDB;
  (e) the audit: one `databricks.spark` row per RPC the client sent - counted by a gRPC
      interceptor in the client (the client's own witness) - in the order sent for
      ExecutePlan / AnalyzePlan; every row a valid 9.2.2 row and `seq` contiguous;
  (f) isolation: the episode cannot resolve the engine, only the proxy; native-services
      is on both networks, the engine on backend_net only, and nothing in the engine but
      Spark Connect's port listens off loopback;
  (g) after teardown destinations.jsonl counts spark_connect.internal:15002 and holds
      nothing outside the profile's allowlist; the world's warehouse never changed.

Evidence: tasks/harness/evidence/spark_e2e.json. Exit 1 on any FAIL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

PROFILE = "ds/databricks"
DEFAULT_IMAGE = "alkera-episode:t14-ds-databricks-spark"
EVIDENCE = HERE / "evidence" / "spark_e2e.json"
PYTHON = "/usr/local/bin/python"  # absolute: the probe is the check's, not a shim row
IN_DIR = "/tmp/spark-e2e"
TOOL = "databricks.spark"
HOST, PORT = "spark_connect.internal", 15002
ENGINE = "spark"
FEB_SQL = ("SELECT SUM(order_amount) FROM main.warehouse.fct_orders WHERE order_status = "
           "'completed' AND order_date >= DATE'2026-02-01' AND order_date < DATE'2026-03-01'")
HASH_SQL = "SELECT * FROM main.warehouse.fct_orders ORDER BY order_id"

PROBE = r'''
import hashlib, json, os, sys, tempfile, time
import grpc
from pyspark.sql import SparkSession, functions as F
from pyspark.sql.connect.client import DefaultChannelBuilder
from pyspark.sql.connect.session import SparkSession as RemoteSession

spec = json.loads(sys.argv[1])
out = {"env": {k: os.environ.get(k) for k in ("SPARK_REMOTE", "SPARK_HOME")}}

# (a') SPARK_REMOTE alone
plain = SparkSession.builder.getOrCreate()
out["plain_type"] = type(plain).__module__
out["feb"] = str(plain.sql(spec["feb_sql"]).collect()[0][0])
plain.stop()

# (d) determinism, before anything writes: the same rows hashed in 3 sessions
hashes = []
for _ in range(3):
    s = SparkSession.builder.getOrCreate()
    rows = s.sql(spec["hash_sql"]).collect()
    hashes.append(hashlib.sha256(json.dumps([[str(v) for v in r] for r in rows])
                                 .encode()).hexdigest())
    s.stop()
out["hashes"] = hashes

# the client's own witness: every RPC this session sends, by method
sent = []
class Count(grpc.UnaryUnaryClientInterceptor, grpc.UnaryStreamClientInterceptor,
            grpc.StreamUnaryClientInterceptor, grpc.StreamStreamClientInterceptor):
    def _note(self, details):
        sent.append(details.method.rsplit("/", 1)[-1])
    def intercept_unary_unary(self, cont, details, request):
        self._note(details); return cont(details, request)
    def intercept_unary_stream(self, cont, details, request):
        self._note(details); return cont(details, request)
    def intercept_stream_unary(self, cont, details, it):
        self._note(details); return cont(details, it)
    def intercept_stream_stream(self, cont, details, it):
        self._note(details); return cont(details, it)

class Counted(DefaultChannelBuilder):
    def toChannel(self):
        return grpc.intercept_channel(super().toChannel(), Count())

spark = RemoteSession.builder.channelBuilder(Counted(os.environ["SPARK_REMOTE"])).getOrCreate()
out["session_id"] = spark.session_id
sqls = []
def sql(text):
    sqls.append(text)
    return spark.sql(text)

# (a) SQL and the DataFrame API
out["counts"] = {t: sql(f"SELECT count(*) FROM main.warehouse.{t}").collect()[0][0]
                 for t in spec["tables"]}
by_status = (spark.table("main.warehouse.fct_orders").filter(F.col("order_amount") > 0)
             .groupBy("order_status").agg(F.sum("order_amount").alias("amount"))
             .orderBy("order_status").collect())
out["by_status"] = [[r["order_status"], str(r["amount"])] for r in by_status]
# toPandas over Arrow: the ds lock's pandas 3, which pyspark-client warns it does not
# fully support
frame = spark.table("main.warehouse.dim_date").orderBy("date_day").limit(3).toPandas()
out["to_pandas"] = {"rows": len(frame), "columns": list(frame.columns),
                    "first": str(frame.iloc[0]["date_day"])}

# (b) writes on the copy, Delta's own
sql("CREATE SCHEMA IF NOT EXISTS main.t14_scratch").collect()
(spark.table("main.warehouse.fct_orders").filter(F.col("order_status") == "refunded")
 .write.mode("overwrite").saveAsTable("main.t14_scratch.refunded"))
out["saved"] = sql("SELECT count(*) FROM main.t14_scratch.refunded").collect()[0][0]
sql("INSERT INTO main.t14_scratch.refunded SELECT * FROM main.warehouse.fct_orders "
    "WHERE order_status = 'refunded' LIMIT 1").collect()
out["after_insert"] = sql("SELECT count(*) FROM main.t14_scratch.refunded").collect()[0][0]
sql("DELETE FROM main.warehouse.fct_orders WHERE order_status = 'refunded'").collect()
out["copy_after_delete"] = sql(
    "SELECT count(*) FROM main.warehouse.fct_orders WHERE order_status = 'refunded'"
).collect()[0][0]
history = sql("DESCRIBE HISTORY main.warehouse.fct_orders").collect()
out["history"] = [r["operation"] for r in history]

# (c) the fences: each must fail, naming the feature
def refused(name, call):
    try:
        call()
        out.setdefault("refusals", {})[name] = None
    except Exception as exc:  # noqa: BLE001 - the message is the finding
        out.setdefault("refusals", {})[name] = f"{type(exc).__name__}: {exc}"[:600]
jar = os.path.join(tempfile.mkdtemp(), "x.jar")
open(jar, "wb").write(b"PK\x03\x04")
refused("AddArtifacts", lambda: spark.addArtifacts(jar))
plus_one = F.udf(lambda x: x + 1, "int")
refused("python_udf", lambda: spark.table("main.warehouse.fct_orders")
        .select(plus_one("order_id")).collect())
refused("path_read", lambda: spark.read.parquet("/seed").collect())
refused("transform", lambda: spark.sql(
    "SELECT TRANSFORM(order_id) USING 'cat' AS (x) FROM main.warehouse.fct_orders").collect())
refused("reflect", lambda: spark.sql(
    "SELECT reflect('java.lang.System', 'getenv', 'PATH')").collect())
refused("parquet_path", lambda: spark.sql(   # the engine's own seed staging file
    "SELECT * FROM parquet.`/spark-data/seed/warehouse.fct_orders.parquet`").collect())
refused("config", lambda: spark.conf.set("spark.sql.catalog.evil", "x.Y"))
try:
    dbutils  # noqa: F821 - not provided, and an honest NameError says so
    out["dbutils"] = "defined"
except NameError as exc:
    out["dbutils"] = f"NameError: {exc}"
spark.stop()
out["sent"] = list(sent)                  # ReleaseSession, from stop(), included
out["sqls"] = sqls

print("SPARK-E2E " + json.dumps(out))
'''
#: `python -c`, which pyspark-client 4.2.0 runs only with SPARK_HOME set (the box sets it).
DASH_C = ("from pyspark.sql import SparkSession; s = SparkSession.builder.getOrCreate(); "
          "print('DASH-C', s.sql('SELECT count(*) FROM main.warehouse.dim_date')"
          ".collect()[0][0])")
#: Tier 1's SQL warehouse, through the SDK, after tier 2's DELETE: still the world's.
TIER1 = r'''
import json
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
r = w.statement_execution.execute_statement(
    warehouse_id="alkera", statement="SELECT count(*) FROM main.warehouse.fct_orders "
    "WHERE order_status = 'refunded'")
print("TIER1 " + json.dumps({"state": r.status.state.value,
                             "rows": r.result.data_array if r.result else None}))
'''
#: Every LISTEN socket in a container, from /proc/net/tcp{,6}: [address, port, loopback],
#: the address decoded (each 32-bit word is host-endian) and a v4-mapped one unwrapped.
LISTENING = r'''
import ipaddress, json, pathlib, struct
out = []
for name in ("tcp", "tcp6"):
    for line in (pathlib.Path("/proc/net") / name).read_text().splitlines()[1:]:
        f = line.split()
        if f[3] != "0A":
            continue
        raw, port = f[1].rsplit(":", 1)
        packed = b"".join(struct.pack("<I", int(raw[i:i + 8], 16)) for i in range(0, len(raw), 8))
        addr = ipaddress.ip_address(packed)
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        out.append([str(addr), int(port, 16), addr.is_loopback])
print(json.dumps(sorted(out)))
'''


def truths(warehouse: Path) -> dict:
    import duckdb

    with duckdb.connect(str(warehouse), read_only=True) as con:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_catalog = "
            "current_database() AND table_schema = 'warehouse' ORDER BY 1").fetchall()]
        counts = {t: con.execute(f'SELECT count(*) FROM warehouse.warehouse."{t}"')
                  .fetchone()[0] for t in tables}
        feb = con.execute(FEB_SQL.replace("main.warehouse", "warehouse.warehouse")
                          .replace("DATE'", "DATE '")).fetchone()[0]
        by_status = con.execute(
            "SELECT order_status, sum(order_amount) FROM warehouse.warehouse.fct_orders "
            "WHERE order_amount > 0 GROUP BY 1 ORDER BY 1").fetchall()
        rows = con.execute(HASH_SQL.replace("main.warehouse", "warehouse.warehouse")
                           ).fetchall()
        refunded = con.execute("SELECT count(*) FROM warehouse.warehouse.fct_orders "
                               "WHERE order_status = 'refunded'").fetchone()[0]
        dim_date = con.execute("SELECT count(*) FROM warehouse.warehouse.dim_date"
                               ).fetchone()[0]
    return {"tables": tables, "counts": counts, "feb": str(feb),
            "by_status": [[s, str(a)] for s, a in by_status],
            "hash": hashlib.sha256(json.dumps([[str(v) for v in r] for r in rows])
                                   .encode()).hexdigest(),
            "refunded": refunded, "dim_date": dim_date}


def container_networks(container: str) -> list[str]:
    done = subprocess.run(["docker", "inspect", "--format",
                           "{{json .NetworkSettings.Networks}}", container],
                          capture_output=True, text=True, check=False)
    return sorted(json.loads(done.stdout or "{}"))


def compose_container(stack, service: str) -> str:
    done = subprocess.run(["docker", "ps", "-q", "--filter",
                           f"label=com.docker.compose.project={stack.project}",
                           "--filter", f"label=com.docker.compose.service={service}"],
                          capture_output=True, text=True, check=False)
    return done.stdout.strip().splitlines()[0] if done.stdout.strip() else ""


def main(argv=None) -> int:
    import agent_under_test as aut
    import bi_e2e_check as bi
    import blindness
    import tool_tour as tour
    from bigquery_e2e_check import semantic_hash, warehouse_section

    from alkera_gateway.native.audit import ROW_KEYS

    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--work", type=Path,
                        default=Path.home() / "alkera-rollouts" / "t14-spark" / "e2e")
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
    truth = truths(Path(world["warehouse"]))
    warehouse_before = warehouse_section(world_root)
    allowed: set | None = None
    record: dict = {"profile": PROFILE, "image": args.image, "revision": bi._git_rev(),
                    "truth": {k: v for k, v in truth.items() if k != "tables"}}
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
        check("compose profile `spark` builds and starts the engine beside native-services",
              ENGINE in (up.get("services") or []), up.get("services"))
        services = stack.services_container
        engine = compose_container(stack, ENGINE)
        logs = subprocess.run(["docker", "logs", engine], capture_output=True, text=True,
                              check=False) if engine else None
        lines = [json.loads(ln) for ln in (logs.stdout if logs else "").splitlines()
                 if ln.startswith("{")]
        seeded = next((ln for ln in lines if ln.get("seed") == "spark"), None)
        listening = next((ln for ln in lines if ln.get("spark_connect") == "listening"), None)
        record["seed"], record["engine"] = seeded, listening
        check("the engine seeded every warehouse table with the world's count, then listened",
              bool(seeded and listening) and {k.split(".", 1)[1]: v for k, v
                                              in seeded["tables"].items()
                                              if k.startswith("warehouse.")}
              == truth["counts"], {"seed": seeded, "listening": listening})
        rows_after_up = bi.read_rows(audit_file)
        check("seeding wrote no audit row", not any(
            str(r.get("tool", "")).startswith("databricks.") for r in rows_after_up),
            [r.get("tool") for r in rows_after_up])

        # (f) isolation
        episode_nets = container_networks(stack.container_id)
        services_nets = container_networks(services)
        engine_nets = container_networks(engine) if engine else []
        record["networks"] = {"episode": episode_nets, "native-services": services_nets,
                              "engine": engine_nets}
        check("isolation: native-services is on backend_net and episode_net",
              any(n.endswith("backend_net") for n in services_nets)
              and any(n.endswith("episode_net") for n in services_nets), services_nets)
        check("isolation: the engine is on backend_net only",
              engine_nets and all(n.endswith("backend_net") for n in engine_nets), engine_nets)
        check("isolation: the episode is on episode_net only",
              episode_nets and all(n.endswith("episode_net") for n in episode_nets),
              episode_nets)
        resolve_engine = stack.exec_(["getent", "hosts", ENGINE], check=False)
        resolve_front = stack.exec_(["getent", "hosts", HOST], check=False)
        record["resolve"] = {ENGINE: resolve_engine.stdout.strip(),
                             HOST: resolve_front.stdout.strip()}
        check("isolation: the episode cannot resolve the engine, only the proxy",
              resolve_engine.returncode != 0 and resolve_front.returncode == 0,
              record["resolve"])
        sockets = subprocess.run(["docker", "exec", engine, "python", "-c", LISTENING],
                                 capture_output=True, text=True, check=False) if engine \
            else None
        listen = json.loads(sockets.stdout) if sockets and sockets.stdout.strip() else []
        record["engine_listening"] = listen
        off_loopback = sorted({port for _, port, loopback in listen if not loopback})
        check("isolation: in the engine only Spark Connect's port listens off loopback",
              off_loopback == [PORT], listen)

        # (a)-(d) the thin client, from inside the episode
        stack.exec_(["mkdir", "-p", IN_DIR])
        probe = work / "probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        stack.copy_in(probe, f"{IN_DIR}/probe.py")
        spec = {"tables": truth["tables"], "feb_sql": FEB_SQL, "hash_sql": HASH_SQL}
        before_rows = len(bi.read_rows(audit_file))
        fingerprint_before = semantic_hash(world_root)
        done = stack.exec_([PYTHON, f"{IN_DIR}/probe.py", json.dumps(spec)], check=False,
                           timeout=900)
        fingerprint_after = semantic_hash(world_root)
        line = next((ln for ln in done.stdout.splitlines() if ln.startswith("SPARK-E2E ")),
                    None)
        check("thin-client probe ran inside the episode", line is not None,
              (done.stdout + done.stderr)[-3000:])
        got = json.loads(line[len("SPARK-E2E "):]) if line else {}
        record["probe"] = {k: v for k, v in got.items() if k not in ("sent",)}
        check("discovery: SPARK_REMOTE (no token: plain gRPC) and SPARK_HOME",
              got.get("env") == {"SPARK_REMOTE": f"sc://{HOST}:{PORT}",
                                 "SPARK_HOME": "/usr/local/lib/python3.11/site-packages/"
                                               "pyspark"}, got.get("env"))
        check("SparkSession.builder.getOrCreate() is a Spark Connect session",
              got.get("plain_type") == "pyspark.sql.connect.session", got.get("plain_type"))
        check("SQL: February 2026 Net Revenue equals the world's DuckDB",
              tour.same_value(got.get("feb"), truth["feb"]),
              {"got": got.get("feb"), "truth": truth["feb"]})
        check("SQL: every warehouse table's count equals the world's DuckDB",
              got.get("counts") == truth["counts"],
              {"got": got.get("counts"), "truth": truth["counts"]})
        check("DataFrame API: filter / groupBy / agg equals the world's DuckDB",
              got.get("by_status") == truth["by_status"],
              {"got": got.get("by_status"), "truth": truth["by_status"]})
        check("toPandas: an Arrow result becomes a pandas frame (pandas 3)",
              (got.get("to_pandas") or {}).get("rows") == 3
              and "date_day" in (got.get("to_pandas") or {}).get("columns", []),
              got.get("to_pandas"))
        check("writes: saveAsTable, INSERT and DELETE land in the copy and read back",
              got.get("saved") == truth["refunded"]
              and got.get("after_insert") == truth["refunded"] + 1
              and got.get("copy_after_delete") == 0,
              {k: got.get(k) for k in ("saved", "after_insert", "copy_after_delete")})
        check("Delta: DESCRIBE HISTORY of the copy names the DELETE and the seed's create",
              (got.get("history") or [])[:1] == ["DELETE"]
              and any(op.startswith("CREATE") for op in got.get("history") or []),
              got.get("history"))
        check("test_remote_write_leaves_world_fingerprint_unchanged",
              fingerprint_before == fingerprint_after,
              {"before": fingerprint_before, "after": fingerprint_after})
        refusals = got.get("refusals") or {}
        expected = {"AddArtifacts": "Spark feature not available in this environment: "
                                    "AddArtifacts",
                    "python_udf": "Spark feature not available in this environment: "
                                  "Python UDFs",
                    "path_read": "Spark feature not available in this environment: file paths",
                    "transform": "Spark feature not available in this environment: TRANSFORM",
                    "reflect": "Spark feature not available in this environment: reflect",
                    "parquet_path": "Spark feature not available in this environment: "
                                    "reading files by path",
                    "config": "Spark feature not available in this environment: setting "
                              "spark.sql.catalog.evil"}
        for name, needle in expected.items():
            check(f"fence: {name} refused by name", needle in (refusals.get(name) or ""),
                  refusals.get(name))
        check("no dbutils: an honest NameError",
              (got.get("dbutils") or "").startswith("NameError"), got.get("dbutils"))
        hashes = got.get("hashes") or []
        record["hashes"] = hashes
        check("determinism: 3 sessions, one hash, equal to the world's rows hashed",
              len(hashes) == 3 and set(hashes) == {truth["hash"]},
              {"got": hashes, "truth": truth["hash"]})
        dash_c = stack.exec_([PYTHON, "-c", DASH_C], check=False, timeout=300)
        record["dash_c"] = (dash_c.stdout + dash_c.stderr)[-400:]
        check("`python -c` with the thin client works on the box (SPARK_HOME)",
              f"DASH-C {truth['dim_date']}" in dash_c.stdout, record["dash_c"])
        tier1 = stack.exec_([PYTHON, "-c", TIER1], check=False, timeout=300)
        t1 = next((json.loads(ln[6:]) for ln in tier1.stdout.splitlines()
                   if ln.startswith("TIER1 ")), {})
        record["tier1_after_tier2_delete"] = t1
        check("two data planes: tier 1 still answers the world's count after tier 2's DELETE",
              t1.get("rows") == [[str(truth["refunded"])]], t1 or tier1.stderr[-600:])

        # (e) the audit
        rows = bi.read_rows(audit_file)
        mine = [r for r in rows[before_rows:] if r["tool"] == TOOL
                and r["args"].get("session_id") == got.get("session_id")]
        sent = got.get("sent") or []
        plans = ("ExecutePlan", "AnalyzePlan")
        record["counts"] = {"rows_total": len(rows), "session_rows": len(mine),
                            "session_rpcs_sent": len(sent),
                            "by_rpc_sent": {m: sent.count(m) for m in sorted(set(sent))},
                            "by_rpc_rows": {m: [r["args"]["rpc"] for r in mine].count(m)
                                            for m in sorted({r["args"]["rpc"] for r in mine})}}
        check("audit: plans are 1:1 with rows - every ExecutePlan / AnalyzePlan the client "
              "sent is one row, in the order sent",
              bool(sent) and [m for m in sent if m in plans]
              == [r["args"]["rpc"] for r in mine if r["args"]["rpc"] in plans],
              record["counts"])
        check("audit: every RPC the client sent is one row (housekeeping included)",
              sorted(sent) == sorted(r["args"]["rpc"] for r in mine), record["counts"])
        texts = [r["args"]["sql"] for r in mine if r["args"].get("sql")]
        check("audit: each spark.sql text the client ran is in a row's args.sql",
              all(text in texts for text in got.get("sqls") or []),
              {"sent": got.get("sqls"), "rows": texts[:8]})
        by_sql = {r["args"]["sql"]: r for r in mine if r["args"].get("sql")}
        record["write_rows"] = [{k: r[k] for k in ("seq", "op_class", "targets")} | {
            "plan": r["args"].get("plan")} for r in mine
            if r["op_class"] in ("write", "ddl", "destructive")]
        save = [r for r in mine if (r["args"].get("plan") or "").startswith(
            "command:write_operation")]
        check("audit: saveAsTable is a write row naming its table",
              bool(save) and save[0]["op_class"] == "write"
              and save[0]["targets"] == ["main.t14_scratch.refunded"],
              [{k: r[k] for k in ("op_class", "targets")} for r in save])
        delete = by_sql.get("DELETE FROM main.warehouse.fct_orders WHERE order_status = "
                            "'refunded'")
        check("audit: the DELETE is a write-class row on the copy's fct_orders",
              delete is not None and delete["op_class"] in ("write", "destructive"),
              delete and {k: delete[k] for k in ("op_class", "targets")})
        refused_rows = [r for r in rows[before_rows:] if r["exit"] == "error"
                        and any(f.startswith("refused:") for f in r["flags"])]
        record["refused_rows"] = [{k: r[k] for k in ("seq", "op_class", "flags")} | {
            "rpc": r["args"].get("rpc")} for r in refused_rows]
        check("audit: the proxy's refusals are error rows flagged refused:<feature>",
              {f for r in refused_rows for f in r["flags"] if f.startswith("refused:")}
              >= {"refused:AddArtifacts", "refused:Python UDFs", "refused:file paths and URLs",
                  "refused:setting spark.sql.catalog.evil"},
              record["refused_rows"])
        bad = [r["seq"] for r in rows if set(r) != set(ROW_KEYS)]
        check("audit: rows carry exactly the 9.2.2 keys", not bad, bad[:5])
        seqs = [r["seq"] for r in rows]
        check("audit: seq is contiguous across writers", seqs == list(range(1, len(seqs) + 1)),
              seqs[-5:])
        record["tools"] = sorted({r["tool"] for r in rows})
        record["audit_sample"] = mine[:2] + [r for r in mine if r["op_class"] != "read"][-2:]
        allowed = aut.allowed_destinations(stack.episode_endpoints())
        record["allowed_destinations"] = sorted(f"{h}:{p}" for h, p in allowed)
        check(f"the profile allows {HOST}:{PORT}", (HOST, PORT) in allowed,
              record["allowed_destinations"])
    finally:
        if not args.keep and topo.stack is not None:
            topo.down()
    if not args.keep:
        dest = bi.read_rows(Path(topo.audit_dir) / "destinations.jsonl")
        record["destinations"] = dest
        ours = [d for d in dest if d.get("host") == HOST and d.get("port") == PORT]
        check(f"destinations.jsonl counts {HOST}:{PORT} after teardown",
              bool(ours) and ours[0]["count"] > 0, ours)
        violations = ([str(v) for v in blindness.network_violations(
            dest, allowed, aut.normalise_host)] if allowed is not None
            else ["no allowlist: the stack never came up"])
        check("no destination outside the profile's allowlist", not violations, violations)
        warehouse_after = warehouse_section(world_root)
        record["warehouse_hash"] = {"before_up": warehouse_before,
                                    "after_down": warehouse_after}
        check("the world's warehouse never changed (after every write and teardown)",
              warehouse_after == warehouse_before, record["warehouse_hash"])
    record["checks"] = checks
    record["result"] = "PASS" if all(c["result"] == "PASS" for c in checks) else "FAIL"
    record["summary"] = (f"{sum(c['result'] == 'PASS' for c in checks)}/{len(checks)} PASS; "
                         f"up in {record.get('up_seconds')} s; {record.get('counts')}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n",
                        encoding="utf-8")
    print(f"{record['result']}: {record['summary']} -> {args.out}")
    return 0 if record["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
