"""Task 14 step 1, the gate: can the Spark Connect thin client read the world from the
engine, offline, the same way three times?

    sg docker -c "gateway/.venv/bin/python gateway/docker/spark/spike.py \\
        --world-warehouse <world>/var/warehouse/warehouse.duckdb"

For each of RUNS cold boots of `alkera-spark:<tag>` (Dockerfile.spark) with
`--network none` - so a jar, a package or anything else fetched at run time is
a failed boot, not a slow one - with the world's warehouse file bound read-only
at /seed:

  * cold start: `docker run` to the engine's "listening" line (seeded, port bound);
  * from a client container in the engine's own network namespace (still no
    network), the pinned thin client (gateway/requirements-spark.lock, pyspark-client)
    with SparkSession.builder.remote("sc://localhost:15002"):
      - `spark.sql("select count(*) from main.warehouse.fct_orders")`, against the
        world's DuckDB count;
      - every row of fct_orders, ordered by order_id, hashed; against the same rows
        read from the world's file by DuckDB, hashed the same way;
      - one DataFrame-API aggregate (filter / groupBy / agg) against DuckDB;
  * the hash must be the same in every run.

Writes gateway/docker/evidence/spark_spike.json (or --out). Exit 1 on any FAIL.
The client image is built here from the client lock alone; it is a measuring
instrument, not something the stack runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ENGINE = "alkera-spark:t14"
CLIENT = "alkera-spark:t14-spike-client"
RUNS = 3
PORT = 15002
BASE = "python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534"
CLIENT_DOCKERFILE = f"""FROM {BASE}
COPY requirements-spark.lock /tmp/requirements-spark.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements-spark.lock
"""
TABLE = "main.warehouse.fct_orders"
DF_SQL = ("SELECT order_status, count(*) AS n, sum(order_amount) AS amount FROM "
          "warehouse.warehouse.fct_orders WHERE order_amount > 50 GROUP BY order_status "
          "ORDER BY order_status")
PROBE = r'''
import hashlib, json, sys, time
from pyspark.sql import SparkSession, functions as F
t0 = time.monotonic()
spark = SparkSession.builder.remote("sc://localhost:%d").getOrCreate()
count = spark.sql("select count(*) from %s").collect()[0][0]
first_ms = round((time.monotonic() - t0) * 1000)
rows = spark.sql("select * from %s order by order_id").collect()
digest = hashlib.sha256(json.dumps([[str(v) for v in r] for r in rows]).encode()).hexdigest()
agg = (spark.table("%s").filter(F.col("order_amount") > 50).groupBy("order_status")
       .agg(F.count("*").alias("n"), F.sum("order_amount").alias("amount"))
       .orderBy("order_status").collect())
print("SPIKE " + json.dumps({"count": count, "rows": len(rows), "hash": digest,
                             "agg": [[str(v) for v in r] for r in agg],
                             "first_query_ms": first_ms, "spark": spark.version}))
''' % (PORT, TABLE, TABLE, TABLE)


#: How an agent may start the client, each measured once: pyspark-client 4.2.0's
#: `check_dependencies` takes a `__main__` with neither __spec__ nor __file__ for a
#: doctest run and imports pyspark.testing, which exits 255 when no SPARK_HOME is
#: found - so `python -c` fails before it connects unless SPARK_HOME is set (to
#: anything; the box sets the client's own package directory).
INVOCATIONS = r'''
Q='from pyspark.sql import SparkSession
s = SparkSession.builder.getOrCreate()
print("RESULT", s.sql("select count(*) from main.warehouse.fct_orders").collect()[0][0])'
export SPARK_REMOTE=sc://localhost:%d
SH=$(python -I -c "import importlib.util,os;print(os.path.dirname(importlib.util.find_spec('pyspark').origin))")
row() { printf '%%s rc=%%s %%s\n' "$1" "$2" "$(grep -oE 'RESULT [0-9]+|Could not find valid SPARK_HOME' "$3" | head -1)"; }
python -c "$Q" > /tmp/o 2>&1; row "python -c" $? /tmp/o
printf '%%s\n' "$Q" | python - > /tmp/o 2>&1; row "python - (heredoc)" $? /tmp/o
printf '%%s\n' "$Q" > /tmp/q.py; python /tmp/q.py > /tmp/o 2>&1; row "python file.py" $? /tmp/o
SPARK_HOME=$SH python -c "$Q" > /tmp/o 2>&1; row "SPARK_HOME=<pyspark dir> python -c" $? /tmp/o
''' % PORT


def invocations(name: str) -> list[str]:
    done = sh("docker", "run", "--rm", "-i", "--network", f"container:{name}", CLIENT,
              "bash", "-s", input=INVOCATIONS, check=False)
    return [ln for ln in done.stdout.splitlines() if " rc=" in ln]


def sh(*argv, check=True, timeout=1800, **kw):
    return subprocess.run(list(argv), check=check, capture_output=True, text=True,
                          timeout=timeout, **kw)


def truth(warehouse: Path) -> dict:
    import duckdb

    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        count = con.execute("SELECT count(*) FROM warehouse.warehouse.fct_orders").fetchone()[0]
        rows = con.execute("SELECT * FROM warehouse.warehouse.fct_orders ORDER BY order_id"
                           ).fetchall()
        agg = con.execute(DF_SQL).fetchall()
    finally:
        con.close()
    digest = hashlib.sha256(json.dumps([[str(v) for v in r] for r in rows]).encode()
                            ).hexdigest()
    return {"count": count, "rows": len(rows), "hash": digest,
            "agg": [[str(v) for v in r] for r in agg]}


def boot(warehouse: Path, name: str) -> dict:
    sh("docker", "rm", "-f", name, check=False)
    started = time.monotonic()
    sh("docker", "run", "-d", "--name", name, "--network", "none",
       "-v", f"{warehouse}:/seed/warehouse.duckdb:ro", ENGINE)
    line = None
    while time.monotonic() - started < 600:
        logs = sh("docker", "logs", name, check=False)
        line = next((ln for ln in logs.stdout.splitlines() if '"spark_connect"' in ln), None)
        if line:
            break
        state = sh("docker", "inspect", "-f", "{{.State.Running}}", name, check=False)
        if state.stdout.strip() != "true":
            return {"listening": False, "logs": (logs.stdout + logs.stderr)[-3000:]}
        time.sleep(0.25)
    if not line:
        return {"listening": False, "logs": "no listening line in 600 s"}
    return {"listening": True, "cold_start_s": round(time.monotonic() - started, 2),
            "engine": json.loads(line)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--world-warehouse", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=HERE.parent / "evidence" / "spark_spike.json")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    warehouse = args.world_warehouse.resolve()

    build = subprocess.run(["docker", "build", "-q", "-t", CLIENT, "-f", "-",
                            str(REPO / "gateway")], input=CLIENT_DOCKERFILE,
                           capture_output=True, text=True, timeout=3600)
    if build.returncode:
        print(build.stderr[-3000:], file=sys.stderr)
        return 1
    image = json.loads(sh("docker", "image", "inspect", ENGINE).stdout)[0]
    want = truth(warehouse)
    checks, runs = [], []

    def check(name, ok, detail=None):
        checks.append({"check": name, "result": "PASS" if ok else "FAIL", "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail}"), flush=True)

    for i in range(RUNS):
        name = f"alkera-spark-spike-{i}"
        try:
            run = boot(warehouse, name)
            if run["listening"]:
                # On stdin, not `python -c`: see INVOCATIONS.
                done = sh("docker", "run", "--rm", "-i", "--network", f"container:{name}",
                          CLIENT, "python", "-", input=PROBE, check=False)
                line = next((ln for ln in done.stdout.splitlines()
                             if ln.startswith("SPIKE ")), None)
                run["client"] = json.loads(line[6:]) if line else {
                    "error": (done.stdout + done.stderr)[-3000:]}
                run["engine_network"] = sh("docker", "inspect", "-f",
                                           "{{.HostConfig.NetworkMode}}", name).stdout.strip()
                if i == 0:
                    run["invocations"] = invocations(name)
            runs.append(run)
        finally:
            if not args.keep:
                sh("docker", "rm", "-f", name, check=False)
    clients = [r.get("client") or {} for r in runs]
    check("the engine seeds and listens with --network none, every run",
          all(r["listening"] and r.get("engine_network") == "none" for r in runs),
          [r.get("logs") for r in runs if not r["listening"]])
    check("the thin client's count of main.warehouse.fct_orders equals DuckDB truth",
          all(c.get("count") == want["count"] for c in clients),
          {"truth": want["count"], "got": [c.get("count") for c in clients]})
    check("every row of fct_orders, hashed, equals DuckDB's rows hashed",
          all(c.get("hash") == want["hash"] for c in clients),
          {"truth": want["hash"], "got": [c.get("hash") for c in clients]})
    check("a DataFrame-API filter/groupBy/agg equals DuckDB truth",
          all(c.get("agg") == want["agg"] for c in clients),
          {"truth": want["agg"], "got": [c.get("agg") for c in clients]})
    seen = (runs[0].get("invocations") or []) if runs else []
    check("SPARK_REMOTE alone: builder.getOrCreate() works from a file, a heredoc, and "
          "`python -c` once SPARK_HOME is set",
          any(ln.startswith("python file.py rc=0 RESULT") for ln in seen)
          and any(ln.startswith("python - (heredoc) rc=0 RESULT") for ln in seen)
          and any(ln.startswith("SPARK_HOME=<pyspark dir> python -c rc=0 RESULT")
                  for ln in seen), seen)
    check("the same result hash over 3 runs",
          len({c.get("hash") for c in clients}) == 1 and len(clients) == RUNS,
          [c.get("hash") for c in clients])
    passed = all(c["result"] == "PASS" for c in checks)
    record = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "task": "T14 step 1 (gate)",
        "engine_image": ENGINE, "engine_image_id": image["Id"],
        "engine_image_size_bytes": image["Size"],
        "engine_image_size_mb": round(image["Size"] / 1e6, 1),
        "world_warehouse": str(warehouse),
        "world_warehouse_sha256": hashlib.sha256(warehouse.read_bytes()).hexdigest(),
        "truth": want,
        "runs": [{k: v for k, v in r.items()} for r in runs],
        "cold_start_s": [r.get("cold_start_s") for r in runs],
        "offline": "every boot ran with --network none: all jars are in the image",
        "checks": checks,
        "passed": passed,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "out": str(args.out),
                      "engine_image_size_mb": record["engine_image_size_mb"],
                      "cold_start_s": record["cold_start_s"]}))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
