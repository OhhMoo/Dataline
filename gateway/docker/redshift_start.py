"""Start the Postgres-persona Redshift: PostgreSQL on a private unix socket,
seeded from the world, behind the audited wire proxy on redshift.internal:5439.

    python /opt/stack/redshift_start.py            # all of it, one container
    python /opt/stack/redshift_start.py postgres   # only the server, foreground
    python /opt/stack/redshift_start.py proxy      # provision + seed + proxy, foreground

NATIVE MODE (container topology, a profile that plugs `warehouse: redshift`;
`tasks/harness/container_episode.py backend_overlay` writes the whole service).
The bare form does, in order:

  1. `postgres`: a fresh cluster in ALKERA_REDSHIFT_DATA/pgdata (rebuilt on
     every boot). ONE DOOR: no TCP listener at all (listen_addresses = ''),
     only a unix socket in a directory that is the server's own, 0700, socket
     0700 - so in a container shared with the agent (the Harbor image) no
     process but root and `postgres` can reach the server, and everything else
     must come through the proxy, which writes a row per statement. UTC, ISO
     dates; MD5 passwords for `alkera` (as a Redshift cluster's are), trust for
     the `postgres` superuser on that private socket.
  2. `provision`: role `alkera` (password: the box's REDSHIFT_PASSWORD, the
     obviously fake shared credential) and database `dev`, owned by it -
     dropped and created again if it exists, so a proxy that restarts serves
     the world's warehouse, not what an earlier session wrote into it - and the
     Redshift system views (redshift_sql.SYSTEM_VIEWS) in its pg_catalog.
  3. `seed`, when ALKERA_REDSHIFT_SEED names a directory: every base table and
     view of every user schema of each `<stem>.duckdb` there is copied, as a
     TABLE (a view's definition is DuckDB's, its rows are what is portable), by
     DuckDB's postgres extension (baked into the episode image; no network) into
     `dev.<schema>.<table>` for the world's `warehouse.duckdb` and into database
     `<stem>` for any other file. Owned by `alkera`, ANALYZEd, single threaded,
     sorted. The seed record is printed as one `{"seed": [...]}` line. Seeding
     goes straight to the server, never through the proxy: it writes no row.
  4. the proxy (`alkera_gateway.native.pgwire_proxy.serve`) on 0.0.0.0:5439,
     translating with `alkera_gateway.redshift_sql.translate`, connecting to the
     server over its socket. It listens only once the seed is in, so the
     healthcheck (the port in LISTEN, read from /proc/net/tcp, never dialled)
     means "seeded and answering".

  ALKERA_NATIVE_AUDIT            the rollout's actions.jsonl: one 9.2.2
                                 `redshift.statement` row per statement, written
                                 before the statement is forwarded
  ALKERA_REDSHIFT_DESTINATIONS   accepted connections, counted per port
                                 (serve.DestinationLog's format), folded into
                                 destinations.jsonl at teardown
  ALKERA_REDSHIFT_STATEMENT_LOG  the proxy's private record, one line per
                                 statement it handled (the 1:1 check)

THE SERVER AND THE PROXY LIVE AND DIE TOGETHER. A proxy in front of a server
that restarted would be in front of a fresh, unseeded cluster (`postgres`
re-initialises it): so the proxy watches the postmaster it seeded (its pid, in
postmaster.pid) and exits the moment it changes or dies, and the bare form
exits when its server child does. A supervisor that restarts the proxy gets a
re-provisioned, re-seeded `dev`.

The split forms are for a supervisor that runs the server and the proxy as two
processes (the Harbor image, Task 10): `postgres` execs the server in the
foreground after (re)initialising the cluster; `proxy` waits for the server's
socket, then provisions, seeds and serves. The proxy must run as root or as
`postgres` (the socket's directory is theirs alone). Paths and ports are
environment variables; nothing here assumes compose.

This file is what Dockerfile.redshift copies from the working tree;
alkera_gateway comes from the episode image's revision, like snowflake_start.py.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PORT = 5439
NATIVE_HOST = "redshift.internal"
#: The server's port number, which names its socket (.s.PGSQL.<port>); it
#: opens no TCP port.
UPSTREAM_PORT = int(os.environ.get("ALKERA_REDSHIFT_UPSTREAM_PORT", "5432"))
DATA = Path(os.environ.get("ALKERA_REDSHIFT_DATA", "/redshift-data"))
PGDATA = DATA / "pgdata"
SOCKET_DIR = DATA / "run"
SOCKET = SOCKET_DIR / f".s.PGSQL.{UPSTREAM_PORT}"
PG_BIN = Path(os.environ.get("ALKERA_REDSHIFT_PG_BIN", "/usr/lib/postgresql/17/bin"))
SEED_ENV = "ALKERA_REDSHIFT_SEED"
AUDIT_ENV = "ALKERA_NATIVE_AUDIT"
DESTINATIONS_ENV = "ALKERA_REDSHIFT_DESTINATIONS"
STATEMENT_LOG_ENV = "ALKERA_REDSHIFT_STATEMENT_LOG"
TOOL = "redshift.statement"
ACTOR = "redshift"
DATABASE = "dev"
USER = "alkera"
#: The obviously fake credential every native surface shares
#: (alkera_gateway.backends.FAKE_CREDENTIAL; the box's REDSHIFT_PASSWORD).
PASSWORD = "alkera-local-dev"
#: The world's main warehouse file becomes Redshift's default database.
MAIN_STEM = "warehouse"
SERVER_USER = "postgres"

POSTGRESQL_CONF = f"""\
listen_addresses = ''
port = {UPSTREAM_PORT}
unix_socket_directories = '{SOCKET_DIR}'
unix_socket_permissions = 0700
max_connections = 50
shared_buffers = 64MB
timezone = 'UTC'
log_timezone = 'UTC'
datestyle = 'iso, mdy'
password_encryption = 'md5'
fsync = off
synchronous_commit = off
full_page_writes = off
logging_collector = off
log_min_messages = warning
log_min_error_statement = panic
"""
#: MD5 for the agent's role, as a Redshift cluster's password authentication
#: is. Measured: SCRAM is what Postgres 17 would pick, and redshift_connector
#: 2.1.17 cannot complete it (it reads the SASL mechanism list's terminator as
#: a mechanism named '' and scramp refuses it), where psql, psycopg2 and the
#: connector all do MD5. Trust for the superuser on the private socket only
#: (provisioning and seeding, as root inside this container).
PG_HBA = """\
local   all   postgres               trust
local   all   all                    md5
"""


def _ids(name: str) -> tuple[int, int]:
    import pwd

    entry = pwd.getpwnam(name)
    return entry.pw_uid, entry.pw_gid


def _as_server(argv, **kw):
    """Run `argv` as the `postgres` user (initdb and the server refuse root)."""
    if os.geteuid() == 0:
        kw.setdefault("user", SERVER_USER)
        kw.setdefault("group", SERVER_USER)
        kw.setdefault("extra_groups", [])
    return subprocess.run(argv, check=True, **kw)


def init_cluster() -> None:
    """A fresh cluster on every boot: the volume's mount point stays, the
    cluster in it goes. The data directory and the socket's are the server's,
    0700."""
    for stale in (PGDATA, SOCKET_DIR):
        shutil.rmtree(stale, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)
    for path in (PGDATA, SOCKET_DIR):
        path.mkdir(parents=True)
        if os.geteuid() == 0:
            shutil.chown(path, SERVER_USER, SERVER_USER)
        path.chmod(0o700)
    _as_server([str(PG_BIN / "initdb"), "-D", str(PGDATA), "-U", SERVER_USER,
                "--auth-local=trust", "--auth-host=reject", "-E", "UTF8",
                "--locale=C.UTF-8", "--no-instructions"], stdout=subprocess.DEVNULL)
    with open(PGDATA / "postgresql.conf", "a", encoding="utf-8") as stream:
        stream.write("\n# alkera redshift persona (gateway/docker/redshift_start.py)\n"
                     + POSTGRESQL_CONF)
    (PGDATA / "pg_hba.conf").write_text(PG_HBA, encoding="utf-8")


def server_argv() -> list[str]:
    return [str(PG_BIN / "postgres"), "-D", str(PGDATA)]


def start_server() -> subprocess.Popen:
    kw = {}
    if os.geteuid() == 0:
        kw = {"user": SERVER_USER, "group": SERVER_USER, "extra_groups": []}
    return subprocess.Popen(server_argv(), **kw)


def socket_answers() -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        try:
            probe.connect(str(SOCKET))
            return True
        except OSError:
            return False


def wait_for_server(timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_answers():
            return
        time.sleep(0.2)
    raise RuntimeError(f"postgres did not answer on {SOCKET} within {timeout:.0f}s")


def postmaster_pid() -> int | None:
    try:
        return int((PGDATA / "postmaster.pid").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _connect(database: str, user: str = SERVER_USER):
    import psycopg2

    conn = psycopg2.connect(host=str(SOCKET_DIR), port=UPSTREAM_PORT, dbname=database,
                            user=user, password=PASSWORD if user == USER else None)
    conn.autocommit = True
    return conn


def provision(databases=(DATABASE,)) -> None:
    """Role `alkera`; each database dropped if it exists and created again,
    owned by it; the Redshift system views (`redshift_sql.SYSTEM_VIEWS`) in its
    pg_catalog, readable by everyone."""
    from alkera_gateway.redshift_sql import SYSTEM_VIEWS

    conn = _connect("postgres")
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (USER,))
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {USER} LOGIN PASSWORD %s", (PASSWORD,))
        for name in databases:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{name}" OWNER {USER}')
    finally:
        conn.close()
    for name in databases:
        conn = _connect(name)
        try:
            cur = conn.cursor()
            cur.execute("SET allow_system_table_mods = on")
            for view in SYSTEM_VIEWS:
                cur.execute(view)
                relation = view.split("VIEW", 1)[1].split()[0]
                cur.execute(f"GRANT SELECT ON {relation} TO PUBLIC")
        finally:
            conn.close()


def database_for(stem: str) -> str:
    return DATABASE if stem == MAIN_STEM else stem.lower()


def seed(source: Path) -> list[dict]:
    """The world's DuckDB warehouse files -> the cluster, table by table.

    Through DuckDB's postgres extension, as `alkera` over the private socket,
    so every relation is the agent's own to read and write - and a write lands
    in this copy, never in the world's file (bound read-only besides)."""
    import duckdb

    files = sorted(Path(source).glob("*.duckdb"))
    provision([database_for(f.stem) for f in files] or [DATABASE])
    copied: list[dict] = []
    for file in files:
        database = database_for(file.stem)
        conn = duckdb.connect()
        try:
            conn.execute("SET threads = 1")
            conn.execute("LOAD postgres")
            conn.execute(f"ATTACH '{file.as_posix()}' AS src (READ_ONLY)")
            conn.execute(f"ATTACH 'host={SOCKET_DIR} port={UPSTREAM_PORT} dbname={database} "
                         f"user={USER} password={PASSWORD}' AS pg (TYPE postgres)")
            tables = conn.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_catalog = 'src' AND table_schema NOT IN "
                "('information_schema', 'pg_catalog') ORDER BY 1, 2").fetchall()
            for schema in sorted({s for s, _ in tables}):
                conn.execute(f'CREATE SCHEMA IF NOT EXISTS pg."{schema}"')
            for schema, table in tables:
                conn.execute(f'CREATE TABLE pg."{schema}"."{table}" AS '
                             f'SELECT * FROM src."{schema}"."{table}"')
                rows = conn.execute(f'SELECT count(*) FROM src."{schema}"."{table}"'
                                    ).fetchone()[0]
                copied.append({"database": database, "schema": schema, "table": table,
                               "rows": rows})
        finally:
            conn.close()
        analyze = _connect(database)
        try:
            analyze.cursor().execute("ANALYZE")      # planner statistics, as superuser
        finally:
            analyze.close()
    return copied


def startup_filter(params: dict[str, str]) -> dict[str, str]:
    """The persona's startup filter (`redshift_sql.startup_parameters`), and ONE
    login role: the cluster's, `alkera`.

    The proxy reaches the server over its private socket, where the superuser
    `postgres` is trusted (provisioning and seeding need that), and the server
    cannot tell a relayed login from this file's own. A startup naming the
    superuser, relayed, would be a superuser session - `COPY ... TO PROGRAM`
    runs commands as the server's OS user, in a container the agent may share
    with it (the Harbor image). So any user but the cluster's is refused at
    the door, by name, as an error row, before a byte reaches the server."""
    from alkera_gateway.redshift_sql import Unsupported, startup_parameters

    kept = startup_parameters(params)
    user = kept.get("user")
    if user != USER:
        raise Unsupported(f"logging in as {user!r} (this cluster's one user is {USER!r})")
    return kept


def serve_proxy() -> None:
    from alkera_gateway import redshift_sql
    from alkera_gateway.native.pgwire_proxy import serve
    from alkera_gateway.native.serve import DestinationLog

    destinations = None
    if os.environ.get(DESTINATIONS_ENV):
        destinations = DestinationLog(Path(os.environ[DESTINATIONS_ENV]))
        destinations.declare(NATIVE_HOST, PORT)
        destinations.flush()
    log = os.environ.get(STATEMENT_LOG_ENV)
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text("", encoding="utf-8")
    serve(("0.0.0.0", PORT), str(SOCKET),
          translate=redshift_sql.translate, tool=TOOL, actor=ACTOR,
          destinations=destinations, statement_log=Path(log) if log else None,
          parameters={"server_version": redshift_sql.SERVER_VERSION},
          refuse=redshift_sql.Unsupported, startup_filter=startup_filter,
          on_listening=lambda _server: print(json.dumps(
              {"listening": f"0.0.0.0:{PORT}", "upstream": str(SOCKET)}), flush=True))


_STOPPING = threading.Event()


def _die(reason: dict) -> None:
    if _STOPPING.is_set():
        return
    print(json.dumps(reason), file=sys.stderr, flush=True)
    os._exit(1)


def _watch_child(server: subprocess.Popen) -> None:
    """The bare form: the server child dying takes the container with it."""
    _die({"postgres_exited": server.wait()})


def _watch_postmaster(pid: int) -> None:
    """The split form: the postmaster this proxy seeded must stay the one
    serving; another one (a supervisor restarted it, on a fresh cluster) or
    none at all ends the proxy too."""
    while True:
        time.sleep(1)
        now = postmaster_pid()
        alive = now == pid
        if alive:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
            except PermissionError:
                pass
        if not alive:
            _die({"postmaster_changed": {"seeded": pid, "now": now}})


def provision_seed_serve() -> None:
    started = time.monotonic()
    if os.environ.get(SEED_ENV):
        record = seed(Path(os.environ[SEED_ENV]))
        print(json.dumps({"seed": record,
                          "seconds": round(time.monotonic() - started, 2)}), flush=True)
    else:
        provision()
    serve_proxy()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else "all"
    if mode not in ("all", "postgres", "proxy"):
        raise SystemExit(f"usage: redshift_start.py [all|postgres|proxy], not {mode!r}")
    if mode == "postgres":
        init_cluster()
        if os.geteuid() == 0:
            uid, gid = _ids(SERVER_USER)
            os.setgid(gid)
            os.setgroups([])
            os.setuid(uid)
        os.execv(server_argv()[0], server_argv())
    if mode == "proxy":
        wait_for_server()
        pid = postmaster_pid()
        threading.Thread(target=_watch_postmaster, args=(pid,), daemon=True,
                         name="postmaster").start()
        provision_seed_serve()
        return 0
    init_cluster()
    server = start_server()

    def stop(signum, _frame):
        _STOPPING.set()
        server.send_signal(signal.SIGINT)        # fast shutdown
        try:
            server.wait(timeout=5)
        finally:
            os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    wait_for_server()
    threading.Thread(target=_watch_child, args=(server,), daemon=True,
                     name="postgres").start()
    provision_seed_serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
