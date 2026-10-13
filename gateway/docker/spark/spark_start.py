"""Start Databricks tier 2's engine: the world's warehouse as Delta tables, then
Spark Connect on :15002.

    python /opt/spark-start/spark_start.py            # seed, then exec the server
    python /opt/spark-start/spark_start.py --print    # the seed plan and argv only

The `spark_connect` plug (alkera_gateway.backends.BACKENDS["spark_connect"]) is
two processes, the BigQuery pattern: THIS container is the ENGINE, on
backend_net only - the episode cannot reach it - and the front door is the
audited gRPC proxy native-services hosts (native/services/spark_connect.py),
which forwards to `$ALKERA_SPARK_CONNECT_UPSTREAM` (`spark:15002`). The engine
answers whoever reaches it; every row the agent's work is judged by is the
proxy's.

What this does, in order:

  1. EXPORT. `/seed/warehouse.duckdb` (the world's primary warehouse file,
     mounted read-only; ALKERA_SPARK_SEED) is attached READ_ONLY and every base
     table and view of every user schema is written to Parquet with DuckDB's own
     COPY - set-based, never row by row (world/world/warehouse_seed.py), and
     never through the proxy, so seeding writes no audit row. DuckDB's default
     schema `main` becomes `default`, as in tier 1 (databricks_sql.Lakehouse
     .seed). A view becomes a TABLE of its rows: its SQL is DuckDB's. A column
     type with no exact Spark equivalent stops the boot, naming it
     (SPARK_TYPES); TIMESTAMP becomes Spark's TIMESTAMP (the session time zone is
     UTC), not TIMESTAMP_NTZ, which is what Databricks shows for the column.
  2. SEED. One statement per line: CREATE SCHEMA, then CREATE TABLE <s>.<t>
     USING DELTA AS SELECT ... FROM parquet.`<file>`, run by the server's own JVM
     BEFORE it binds its port (org.alkera.spark.SeededConnectServer: the catalog
     is in-memory, so a seeding process before it would leave nothing the server
     could see). Every table is counted back against the world's count; a
     difference stops the process before anything listens. Engine-side fences
     (org.alkera.spark.Guard) are armed only after the seed, which reads files by
     path - the one thing the agent may not.
  3. SERVE. Spark Connect, Spark's own server, on 0.0.0.0:15002, `local[1]`,
     one shuffle partition, session time zone UTC: one thread, so one plan's
     answer is one deterministic computation (run it three times, get one hash).
     The driver's own RPC and block manager bind 127.0.0.1; the UI is off. The
     JVM is exec'd, so it is this container's main process and SIGTERM reaches it.

The JVM's command line is the one Spark's own launcher builds
(org.apache.spark.launcher.Main, what bin/spark-class runs), so the module
options a Java 17+ runtime needs are Spark's, not a copy here.

NO NETWORK. Every jar is in the image (gateway/docker/spark/fetch.py, at build
time, by checksum); nothing here resolves a package (`spark.jars.packages` is
never set) and the engine runs on an internal network with no egress.

This file is what Dockerfile.spark copies from the working tree. Standard
library plus DuckDB (gateway/docker/spark/requirements.txt).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PORT = int(os.environ.get("ALKERA_SPARK_CONNECT_PORT", "15002"))
SEED = Path(os.environ.get("ALKERA_SPARK_SEED", "/seed"))
DATA = Path(os.environ.get("ALKERA_SPARK_DATA", "/spark-data"))
SPARK_HOME = Path(os.environ.get("SPARK_HOME", "/opt/spark"))
JAVA = Path(os.environ.get("JAVA_HOME", "/usr/lib/jvm/java-21")) / "bin" / "java"
#: The JVM heap. local[1] runs every task in the driver.
DRIVER_MEMORY = os.environ.get("ALKERA_SPARK_DRIVER_MEMORY", "1g")
#: The world's main warehouse file; its schemas become the `main` catalog's.
MAIN_STEM = "warehouse"
#: DuckDB's default schema, and the name Databricks gives it.
ENGINE_DEFAULT_SCHEMA, DEFAULT_SCHEMA = "main", "default"
SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")
SERVER_CLASS = "org.alkera.spark.SeededConnectServer"
#: Every DuckDB column type the seed copies, and the Spark type it arrives as. A
#: type not here stops the boot naming it: a lossy copy would be a world the agent
#: is told is the warehouse and is not.
SPARK_TYPES = {
    "BOOLEAN": "BOOLEAN", "TINYINT": "TINYINT", "SMALLINT": "SMALLINT",
    "INTEGER": "INT", "BIGINT": "BIGINT", "FLOAT": "FLOAT", "DOUBLE": "DOUBLE",
    "VARCHAR": "STRING", "DATE": "DATE", "BLOB": "BINARY",
    "TIMESTAMP": "TIMESTAMP", "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
}
DECIMAL = re.compile(r"^DECIMAL\((\d+),(\d+)\)$")
#: The session's configuration, as `--conf` pairs. Delta is the default table
#: format (CREATE TABLE without USING is Delta, as on Databricks), `main` is the
#: catalog (MainCatalog over the session catalog), and the engine fences are an
#: extension next to Delta's.
CONF = {
    "spark.master": "local[1]",
    "spark.sql.shuffle.partitions": "1",
    "spark.sql.session.timeZone": "UTC",
    "spark.ui.enabled": "false",
    "spark.driver.bindAddress": "127.0.0.1",
    "spark.driver.host": "127.0.0.1",
    "spark.connect.grpc.binding.port": str(PORT),
    "spark.sql.extensions": "io.delta.sql.DeltaSparkSessionExtension,org.alkera.spark.Guard",
    "spark.sql.catalog.spark_catalog": "org.apache.spark.sql.delta.catalog.DeltaCatalog",
    "spark.sql.catalog.main": "org.alkera.spark.MainCatalog",
    "spark.sql.defaultCatalog": "main",
    "spark.sql.sources.default": "delta",
    "spark.sql.catalogImplementation": "in-memory",
    "spark.sql.warehouse.dir": str(DATA / "warehouse"),
    "spark.local.dir": str(DATA / "tmp"),
    "spark.databricks.delta.snapshotPartitions": "1",
    "spark.sql.legacy.createHiveTableByDefault": "false",
}


class SeedError(RuntimeError):
    """The world's warehouse cannot be copied faithfully; the boot stops."""


def quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def duck_quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def spark_type(duck_type: str) -> str:
    """The Spark type a DuckDB column arrives as, or SeedError naming it."""
    found = DECIMAL.match(duck_type)
    if found and int(found.group(1)) <= 38:
        return f"DECIMAL({found.group(1)},{found.group(2)})"
    if duck_type in SPARK_TYPES:
        return SPARK_TYPES[duck_type]
    raise SeedError(f"DuckDB type {duck_type} has no exact Spark equivalent")


def export(world_file: Path, out: Path) -> list[dict]:
    """Every user relation of `world_file` as one Parquet file under `out`.

    -> [{"schema", "table", "rows", "parquet", "columns": [(name, spark type)]}],
    sorted by (schema, table). The world file is attached READ_ONLY under its own
    stem, the name its views were defined against, and never written."""
    import duckdb

    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:", config={"threads": 1})
    tables = []
    try:
        con.execute("SET TimeZone = 'UTC'")
        src = duck_quote(world_file.stem)
        con.execute(f"ATTACH '{world_file.as_posix()}' AS {src} (READ_ONLY)")
        names = con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables WHERE "
            "table_catalog = ? AND table_schema NOT IN ('information_schema', "
            "'pg_catalog') ORDER BY 1, 2", [world_file.stem]).fetchall()
        for schema, table in names:
            columns = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns WHERE "
                "table_catalog = ? AND table_schema = ? AND table_name = ? ORDER BY "
                "ordinal_position", [world_file.stem, schema, table]).fetchall()
            typed = [(name, spark_type(kind)) for name, kind in columns]
            source = f"{src}.{duck_quote(schema)}.{duck_quote(table)}"
            target = DEFAULT_SCHEMA if schema == ENGINE_DEFAULT_SCHEMA else schema
            parquet = out / f"{target}.{table}.parquet"
            con.execute(f"COPY (SELECT * FROM {source}) TO '{parquet.as_posix()}' "
                        f"(FORMAT parquet)")
            rows = con.execute(f"SELECT count(*) FROM {source}").fetchone()[0]
            tables.append({"schema": target, "table": table, "rows": int(rows),
                           "parquet": parquet.as_posix(), "columns": typed})
    finally:
        con.close()
    return tables


def seed_statements(tables: list[dict]) -> tuple[list[str], list[str]]:
    """(SQL statements, `<table>\\t<rows>` checks) for SeededConnectServer.

    Each column is selected by name and cast to the type SPARK_TYPES gives it,
    so what Parquet carried (a DuckDB TIMESTAMP reads as TIMESTAMP_NTZ) is not
    what the table says."""
    schemas = sorted({DEFAULT_SCHEMA, *(t["schema"] for t in tables)})
    statements = [f"CREATE SCHEMA IF NOT EXISTS main.{quote(s)}" for s in schemas]
    checks = []
    for t in tables:
        name = f"main.{quote(t['schema'])}.{quote(t['table'])}"
        select = ", ".join(f"CAST({quote(c)} AS {kind}) AS {quote(c)}"
                           for c, kind in t["columns"])
        statements.append(f"CREATE TABLE {name} USING DELTA AS SELECT {select} "
                          f"FROM parquet.{quote(t['parquet'])}")
        checks.append(f"{name}\t{t['rows']}")
    return statements, checks


def launcher_argv(seed_sql: Path, expect: Path) -> list[str]:
    """`spark-submit --class SeededConnectServer spark-internal ...` as argv."""
    argv = ["org.apache.spark.deploy.SparkSubmit", "--class", SERVER_CLASS,
            "--name", "Spark Connect server", "--driver-memory", DRIVER_MEMORY]
    for key, value in CONF.items():
        argv += ["--conf", f"{key}={value}"]
    return argv + ["spark-internal", str(seed_sql), str(expect)]


def java_argv(seed_sql: Path, expect: Path) -> list[str]:
    """The JVM command Spark's launcher builds for `launcher_argv` (what
    bin/spark-class does, without the shell): NUL-separated on its stdout."""
    classpath = f"{SPARK_HOME / 'jars'}/*"
    # SPARK_SCALA_VERSION: what bin/load-spark-env.sh exports; without it the
    # launcher looks for a source build's directories and fails.
    done = subprocess.run(
        [str(JAVA), "-Xmx128m", "-cp", classpath, "org.apache.spark.launcher.Main",
         *launcher_argv(seed_sql, expect)],
        capture_output=True,
        env={**os.environ, "SPARK_HOME": str(SPARK_HOME), "SPARK_SCALA_VERSION": "2.13"})
    # One NUL-terminated word per argument; the launcher starts its output with a
    # newline, which is not part of the java path.
    argv = [w for w in (part.decode("utf-8").strip("\n") for part in done.stdout.split(b"\0"))
            if w]
    if done.returncode or not argv or not argv[0].endswith("java"):
        raise SystemExit(f"spark launcher failed ({done.returncode}): "
                         f"{done.stderr.decode('utf-8', 'replace')[-2000:]}")
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--print", action="store_true",
                        help="export and print the plan and the JVM argv; do not serve")
    args = parser.parse_args(argv)

    world_file = SEED / f"{MAIN_STEM}.duckdb"
    if not world_file.is_file():
        raise SystemExit(f"spark: no world warehouse at {world_file}")
    ignored = sorted(p.name for p in SEED.glob("*.duckdb") if p != world_file)
    for sub in ("seed", "warehouse", "tmp"):
        shutil.rmtree(DATA / sub, ignore_errors=True)
    try:
        tables = export(world_file, DATA / "seed")
    except SeedError as exc:
        raise SystemExit(f"spark: seed refused: {exc}") from exc
    statements, checks = seed_statements(tables)
    seed_sql, expect = DATA / "seed.sql", DATA / "expect.tsv"
    seed_sql.write_text("\n".join(statements) + "\n", encoding="utf-8")
    expect.write_text("\n".join(checks) + "\n", encoding="utf-8")
    print(json.dumps({"seed": "spark", "from": world_file.name, "not_seeded": ignored,
                      "tables": {f"{t['schema']}.{t['table']}": t["rows"] for t in tables}},
                     sort_keys=True), flush=True)
    command = java_argv(seed_sql, expect)
    if args.print:
        print(json.dumps({"statements": statements, "argv": command}, indent=2))
        return 0
    os.execv(command[0], command)
    return 1  # pragma: no cover - execv does not return


if __name__ == "__main__":
    sys.exit(main())
