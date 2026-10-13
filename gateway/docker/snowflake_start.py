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

ONE DOOR IN A SHARED CONTAINER. fakesnow itself answers the wire protocol with
no credential check and no audit; the proxy in front of it is the door. In its
own container that upstream listens on 127.0.0.1:64616, out of the episode's
reach. Where the agent shares the container (the Harbor image) a loopback port
is the agent's to dial, so `ALKERA_SNOWFLAKE_UPSTREAM_SOCKET` puts the upstream
on a unix socket instead - in a directory only this service's user can enter -
and the proxy reaches it through that socket. Unset, nothing changes.
"""

import json
import os
import shutil
import threading
import time
from pathlib import Path

import uvicorn

HOSTNAME = "acme.snowflake.local"
# `snowflake.internal` is the native episode's name for this service
# (compose.native.yml); the connector verifies the certificate against it.
ALT_NAMES = ("*.snowflake.local", "snowflake.internal")
# What the certificate covers (tls.subject_alt_names(HOSTNAME, ALT_NAMES)),
# spelled out; gateway/tests/test_backends.py holds the two equal.
SUBJECT_ALT_NAMES = (f"DNS:{HOSTNAME},DNS:*.snowflake.local,DNS:snowflake.internal,"
                     "DNS:localhost,IP:127.0.0.1")
NATIVE_HOST = "snowflake.internal"
PORT = 8443
SEED_ENV = "ALKERA_SNOWFLAKE_SEED"
AUDIT_ENV = "ALKERA_NATIVE_AUDIT"
DESTINATIONS_ENV = "ALKERA_SNOWFLAKE_DESTINATIONS"
UPSTREAM_SOCKET_ENV = "ALKERA_SNOWFLAKE_UPSTREAM_SOCKET"
TOOL = "snowflake.statement"
ACTOR = "snowflake"

TLS = Path("/tls")  # private: key + certificate, this service only
TRUST = Path("/etc/alkera/snowflake-tls")  # shared: certificate + bundle, no key
DATA = Path("/snowflake-data")  # writable state, outside the fingerprinted tree
UPSTREAM_PORT = 64616


def certificate() -> tuple[Path, Path]:
    """Regenerate the certificate and trust bundle together on each boot:
    `alkera_gateway.native.tls.mint`, the helper every TLS backend shares
    (backends.BACKENDS rows with `tls=True`), with this service's names."""
    from alkera_gateway.native.tls import mint

    return mint(HOSTNAME, TRUST, private_dir=TLS, alt_names=ALT_NAMES)


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
    socket_path = os.environ.get(UPSTREAM_SOCKET_ENV)
    if socket_path:
        private_socket_dir(Path(socket_path))
        config = uvicorn.Config(app, uds=socket_path, log_level="info")
    else:
        config = uvicorn.Config(app, host="127.0.0.1", port=UPSTREAM_PORT, log_level="info")
    server = uvicorn.Server(config)
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


def private_socket_dir(socket_path: Path) -> None:
    """The upstream socket's directory: this process's user's alone (0700), so
    the socket (uvicorn makes it 0666) is reachable by nobody else. Refuses a
    directory another user owns or can enter."""
    directory = socket_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    stat = directory.stat()
    if stat.st_uid != os.geteuid():
        raise SystemExit(f"{directory} is not this user's: the fakesnow upstream socket "
                         f"would be another user's to reach")
    directory.chmod(0o700)
    socket_path.unlink(missing_ok=True)


def upstream_route():
    """(the proxy's upstream URL, the httpx transport to reach it with): the
    private socket when ALKERA_SNOWFLAKE_UPSTREAM_SOCKET names one, else the
    loopback port (FAKESNOW_UPSTREAM overrides) and httpx's own transport."""
    socket_path = os.environ.get(UPSTREAM_SOCKET_ENV)
    if not socket_path:
        return (os.environ.get("FAKESNOW_UPSTREAM", f"http://127.0.0.1:{UPSTREAM_PORT}"),
                None)
    import httpx

    class SharedSocketTransport(httpx.AsyncHTTPTransport):
        """One pool over the socket for every request. The proxy opens a client
        per request, and that client's exit would close this pool's connections -
        those of the requests still in flight on it too - so it does not."""

        async def __aexit__(self, *exc) -> None:
            return None

    return "http://fakesnow", SharedSocketTransport(uds=socket_path)


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
    upstream_url, transport = upstream_route()
    uvicorn.run(
        snowflake_proxy.proxy_app(upstream_url, transport=transport),
        host="0.0.0.0", port=PORT, ssl_certfile="/tls/server.crt",
        ssl_keyfile="/tls/server.key",
        http=AuditedH11Protocol if destinations else "auto")


def main() -> None:
    certificate()
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
