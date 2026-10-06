"""Start loopback fakesnow behind local TLS and publish its public trust bundle.

NATIVE MODE (container topology, a profile that plugs `warehouse: snowflake`).
The per-rollout overlay `tasks/harness/container_episode.py` writes turns on two
more things, each by one environment variable, and neither changes the MCP route:

  ALKERA_SNOWFLAKE_SEED   a directory of the world's own DuckDB warehouse files,
                          bound read-only. Each `<stem>.duckdb` is copied, table
                          by table, into fakesnow's backing file
                          `/snowflake-data/<STEM>.db` BEFORE the first login, so
                          the agent's Snowflake holds the world's warehouse:
                          database `<STEM>` (upper-cased, as Snowflake reads an
                          unquoted name), the same schemas and tables, the same
                          values. Rebuilt from scratch on every boot, single
                          threaded, sorted: two boots of one world are the same
                          bytes to every query.
  ALKERA_NATIVE_AUDIT     the rollout's actions.jsonl. Every statement the proxy
                          forwards writes ONE 9.2.2 row (`snowflake.statement`,
                          op_class/targets from `alkera_gateway.sql.inspect_sql`
                          with dialect snowflake) before the response returns -
                          so the row is ordered before anything the agent does
                          next. Accepted connections are counted per port into
                          `ALKERA_SNOWFLAKE_DESTINATIONS` (serve.DestinationLog's
                          format), its own file because destinations.jsonl is
                          rewritten whole by native-services; the harness folds it
                          in at teardown (container_episode.fold_destinations).

Both live HERE, in gateway/docker, and not in alkera_gateway.snowflake_proxy, on
purpose: this file is what Dockerfile.snowflake copies from the working tree,
while alkera_gateway comes from the episode image's revision.
"""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import certifi
import uvicorn

HOSTNAME = "acme.snowflake.local"
# `snowflake.internal` is the native episode's name for this service
# (compose.native.yml); the connector verifies the certificate against it.
SUBJECT_ALT_NAMES = (f"DNS:{HOSTNAME},DNS:*.snowflake.local,DNS:snowflake.internal,"
                     "DNS:localhost,IP:127.0.0.1")
NATIVE_HOST = "snowflake.internal"
PORT = 8443
SEED_ENV = "ALKERA_SNOWFLAKE_SEED"
AUDIT_ENV = "ALKERA_NATIVE_AUDIT"
DESTINATIONS_ENV = "ALKERA_SNOWFLAKE_DESTINATIONS"
TOOL = "snowflake.statement"
ACTOR = "snowflake"

TLS = Path("/tls")  # private: key + certificate, this service only
TRUST = Path("/etc/alkera/snowflake-tls")  # shared: certificate + bundle, no key
DATA = Path("/snowflake-data")  # writable state, outside the fingerprinted tree
UPSTREAM_PORT = 64616


def certificate() -> Path:
    """Regenerate the certificate and trust bundle together on each boot."""
    TLS.mkdir(parents=True, exist_ok=True)
    key, crt = TLS / "server.key", TLS / "server.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "30",
            "-subj",
            f"/CN={HOSTNAME}",
            "-addext",
            f"subjectAltName={SUBJECT_ALT_NAMES}",
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return crt


def publish_trust(crt: Path) -> Path:
    """The private key never leaves this container; the certificate always does."""
    TRUST.mkdir(parents=True, exist_ok=True)
    (TRUST / "server.crt").write_bytes(crt.read_bytes())
    bundle = TRUST / "bundle.pem"
    bundle.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + crt.read_bytes())
    return bundle


def seed(source: Path, data: Path = DATA) -> list[dict]:
    """The world's DuckDB warehouse files -> fakesnow's backing files.

    One `<STEM>.db` per `<stem>.duckdb`, every base table and view of every
    user schema copied as a TABLE (a view's definition names the world's own
    catalog, so its rows are what is portable), schema and table names
    upper-cased the way Snowflake stores an unquoted identifier. Returns what
    was copied, which the caller prints as the seed record.
    """
    import duckdb

    data.mkdir(parents=True, exist_ok=True)
    copied = []
    for file in sorted(source.glob("*.duckdb")):
        database = file.stem.upper()
        target = data / f"{database}.db"
        for stale in (target, target.with_name(target.name + ".wal")):
            stale.unlink(missing_ok=True)
        conn = duckdb.connect(str(target))
        try:
            conn.execute("SET threads = 1")
            # Fully qualified below: WAREHOUSE.db is catalog WAREHOUSE, which
            # may hold a schema WAREHOUSE, and a two-part name is then ambiguous.
            own = conn.execute("SELECT current_database()").fetchone()[0]
            conn.execute(f"ATTACH '{file.as_posix()}' AS src (READ_ONLY)")
            tables = conn.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_catalog = 'src' AND table_schema NOT IN "
                "('information_schema', 'pg_catalog') ORDER BY 1, 2").fetchall()
            # Every Snowflake database has PUBLIC; a session's scratch work
            # lands there when it names no schema of its own.
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{own}"."PUBLIC"')
            for schema, table in tables:
                name = f'"{own}"."{schema.upper()}"."{table.upper()}"'
                conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{own}"."{schema.upper()}"')
                conn.execute(f'CREATE TABLE {name} AS SELECT * FROM src."{schema}"."{table}"')
                rows = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                copied.append({"database": database, "schema": schema.upper(),
                               "table": table.upper(), "rows": rows})
            conn.execute("DETACH src")
            conn.execute("CHECKPOINT")
        finally:
            conn.close()
    return copied


def observation(sql: str):
    """op_class / targets for one statement: the gateway's own classifier, the
    Postgres tailer's verb fallback behind it (an unparsed statement is still
    one row, flagged, never dropped)."""
    from alkera_gateway.native.tailers.postgres import fallback_observation
    from alkera_gateway.records import Observation, ToolError
    from alkera_gateway.sql import inspect_sql

    found = Observation()
    try:
        inspect_sql(sql, found, read_only=False, dialect="snowflake")
    except ToolError:
        found = fallback_observation(sql, dialect="snowflake")
    return found


def audited(original):
    """`snowflake_proxy.log_statement`, plus one 9.2.2 row per statement."""
    from alkera_gateway.native.audit import record

    def log_statement(sql, rows, error, latency_ms):
        original(sql, rows, error, latency_ms)
        seen = observation(sql)
        record(tool=TOOL, args={"sql": sql}, op_class=seen.op_class,
               targets=list(seen.targets), read_targets=list(seen.read_targets),
               exit="error" if error else "ok",
               error={"code": "sql_error", "message": error} if error else None,
               latency_ms=latency_ms,
               # `rows` is the result's row count; for DML that is the one
               # "number of rows inserted" row, not the rows it touched.
               result={"rows": rows},
               flags=list(seen.flags), actor=ACTOR)

    return log_statement


def upstream() -> None:
    """Bind the shared instance to persistent storage before accepting logins."""
    import fakesnow.server as fakesnow_server
    import snowflake_fidelity
    from fakesnow.instance import FakeSnow

    DATA.mkdir(parents=True, exist_ok=True)
    fakesnow_server.shared_fs = FakeSnow(db_path=str(DATA))
    # Patch fakesnow before the first login: qualified temp relations, the
    # 500 -> retry-storm catch-all, strict mode, the silent-wrong fences and session
    # identity. See gateway/docker/snowflake_fidelity.py; ALKERA_SNOWFLAKE_* tunes it.
    app = snowflake_fidelity.apply(fakesnow_server)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=UPSTREAM_PORT, log_level="info")
    )
    thread = threading.Thread(target=server.run, name="fakesnow", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while thread.is_alive() and not server.started and time.monotonic() < deadline:
        time.sleep(0.1)
    if not server.started:
        raise RuntimeError("fakesnow upstream failed to start")
    # Attach every seeded database now, so a fully qualified name resolves
    # from a session that logged in without one.
    for file in sorted(DATA.glob("*.db")):
        fakesnow_server.shared_fs.connect(database=file.stem).close()


def serve_native() -> None:
    """snowflake_proxy.main, with accepted connections counted per port."""
    import uvicorn

    from alkera_gateway import snowflake_proxy
    from alkera_gateway.native.serve import AuditedH11Protocol, DestinationLog

    destinations = os.environ.get(DESTINATIONS_ENV)
    if destinations:
        log = DestinationLog(Path(destinations))
        log.declare(NATIVE_HOST, PORT)
        log.flush()
        AuditedH11Protocol.destinations = log
    uvicorn.run(
        snowflake_proxy.proxy_app(os.environ.get("FAKESNOW_UPSTREAM",
                                                 f"http://127.0.0.1:{UPSTREAM_PORT}")),
        host="0.0.0.0", port=PORT, ssl_certfile="/tls/server.crt",
        ssl_keyfile="/tls/server.key",
        http=AuditedH11Protocol if destinations else "auto")


def main() -> None:
    publish_trust(certificate())
    if os.environ.get(SEED_ENV):
        # The volume's mount point stays; everything in it goes.
        for stale in (DATA.iterdir() if DATA.exists() else ()):
            shutil.rmtree(stale) if stale.is_dir() else stale.unlink()
        print(json.dumps({"seed": seed(Path(os.environ[SEED_ENV]))}), flush=True)
    upstream()
    if os.environ.get(AUDIT_ENV):
        from alkera_gateway import snowflake_proxy

        snowflake_proxy.log_statement = audited(snowflake_proxy.log_statement)
        serve_native()
        return
    from alkera_gateway.snowflake_proxy import main as serve

    serve()


if __name__ == "__main__":
    main()
