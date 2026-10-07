"""Postgres in the container topology: seeded from the world, audited at teardown.

Every native stack runs a real PostgreSQL server (compose.yml `postgres`, aliased
`postgres.internal:5432` on episode_net by compose.native.yml) and the box carries
the `alkera` role's PG* variables, so `psql`, psycopg and dbt-postgres reach it with
no arguments. Two things this module adds to that server's life, both called from
`container_episode.NativeContainerEpisode`:

SEEDED ONLY WHEN PLUGGED (`seed`, from `prepare()`). A profile whose warehouse slot
plugs `postgres` (de/postgres, or any profile with `--plug warehouse=postgres`) gets
the world's `warehouse.*` base tables copied in, so a psql read answers the world's
facts. Anything else - every unprofiled GT box, BI001/BI002's da/laptop - keeps the
server exactly as compose seeds it: `warehouse.items`, two rows. The copy:

  - reads the world's DuckDB warehouse file on the HOST, read-only;
  - runs as the superuser over the server container's own socket, through `psql -1`
    (one transaction), as role `episode`, which owns the `warehouse` schema and the
    `items` table already in it. Neither role is logged: `log_statement` is set on
    the `alkera` role only (compose.native.yml, `pg_native_role`), so seeding writes
    no server log line and therefore no audit row - the world is not the agent;
  - grants the agent role the privileges compose's init gives it on `items`
    (SELECT, INSERT, UPDATE, DELETE). A write lands in the seeded COPY: the world's
    files never move, and no grader reads through this plug;
  - refuses, by name, a column type it does not map (fences are loud), and checks
    each table's row count back against the DuckDB file.

AUDITED AT TEARDOWN (`fold_statement_log`, from `down()`). The server logs every
statement of the `alkera` role (`log_statement=all`, `log_min_duration_statement=0`,
role-scoped) to `<pgdata>/log/postgresql.log`. Before anything is torn down, the
existing tailer (`alkera_gateway.native.tailers.postgres`) is run over that log inside
`native-services` - the one container that mounts it (`pgdata:/pgdata:ro`) and the
audit directory - and appends one `psql.statement` row per statement through
`native.audit.record`, which allocates every `seq` under `<audit>/writer.lock`, the
lock every other writer holds: the sequence stays contiguous. Rows carry the world's
clock (`ALKERA_SCHEDULER`, as serve.py's own rows do). They land at teardown, never
per call: a probe that reads them is `rows_at="teardown"`.

What it does not do: count a Postgres connection into destinations.jsonl (only
native-services' own sockets are counted), or see a statement the server never ran
(`\\!` in psql, a client-side error before the wire): native/PARITY.md.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: The plug that turns seeding on (alkera_gateway.plugs, slot `warehouse`).
PLUG = "postgres"
#: compose.native.yml: the database the episode connects to, the role that owns the
#: seeded schema (gateway/docker/postgres.sql), the agent's role.
DATABASE = "alkera"
SUPERUSER = "postgres"
OWNER = "episode"
AGENT_ROLE = "alkera"
AGENT_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"
#: The world schema that is copied, from the world's primary warehouse file.
SCHEMA = "warehouse"
WAREHOUSE_FILE = Path("var") / "warehouse" / "warehouse.duckdb"
#: The compose service whose container runs the server.
SERVICE = "postgres"

#: Inside native-services: where the server's log, the audit directory and the
#: world's scheduler state are mounted (compose.native.yml).
SERVER_LOG = "/pgdata/log/postgresql.log"
AUDIT_IN_SERVICES = "/audit"
SCHEDULER_IN_SERVICES = "/world/var/sched/state.json"
SERVICES_PYTHON = "/usr/local/bin/python"
TAILER = "alkera_gateway.native.tailers.postgres"
#: The tailer exits 3 through this script when the server never opened its log.
NO_LOG = 3
TAIL_SCRIPT = (f'test -f {SERVER_LOG} || exit {NO_LOG}; '
               f'exec {SERVICES_PYTHON} -m {TAILER} < {SERVER_LOG}')
ROWS_LINE = re.compile(r"^(\d+) rows$", re.MULTILINE)

#: DuckDB column type -> Postgres column type. A type not here is refused by name.
SCALAR_TYPES = {
    "BOOLEAN": "boolean",
    "TINYINT": "smallint",
    "SMALLINT": "smallint",
    "INTEGER": "integer",
    "BIGINT": "bigint",
    "HUGEINT": "numeric(38,0)",
    "FLOAT": "real",
    "REAL": "real",
    "DOUBLE": "double precision",
    "VARCHAR": "varchar",
    "DATE": "date",
    "TIME": "time",
    "TIMESTAMP": "timestamp",
    "TIMESTAMP WITH TIME ZONE": "timestamptz",
    "UUID": "uuid",
    "JSON": "json",
}
DECIMAL = re.compile(r"^DECIMAL\((\d+),\s*(\d+)\)$")


class SeedError(Exception):
    """The world's warehouse cannot be copied as it is; the message names why."""


def plugged(profile) -> bool:
    """True when `profile` (a resolved `alkera_gateway.plugs.Profile`, or None for
    an unprofiled task) plugs `postgres` into its warehouse slot."""
    return profile is not None and PLUG in (profile.slots.get("warehouse") or ())


def pg_type(duckdb_type: str, *, column: str = "?") -> str:
    """The Postgres spelling of one DuckDB column type, or SeedError naming it."""
    name = str(duckdb_type).strip().upper()
    if name in SCALAR_TYPES:
        return SCALAR_TYPES[name]
    found = DECIMAL.match(name)
    if found:
        return f"numeric({found.group(1)},{found.group(2)})"
    raise SeedError(f"column {column}: DuckDB type {duckdb_type!r} has no Postgres "
                    f"mapping here; the seed refuses it rather than guess")


def _ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[tuple[str, str], ...]   # (name, postgres type)
    rows: int
    csv: str                               # header-less CSV, DuckDB's own writer


def read_tables(warehouse: Path, schema: str = SCHEMA) -> tuple[list[Table], list[str]]:
    """(the base tables of `schema` in the DuckDB file, the views skipped), read-only.

    The data is DuckDB's own CSV (NULL as an empty field, an empty string quoted),
    which Postgres' `COPY ... (FORMAT csv)` reads back the same way."""
    import duckdb

    warehouse = Path(warehouse)
    if not warehouse.is_file():
        raise SeedError(f"the world has no warehouse file at {warehouse}")
    con = duckdb.connect(str(warehouse), read_only=True)
    tables: list[Table] = []
    try:
        catalog = con.execute("SELECT current_database()").fetchone()[0]
        listing = con.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_catalog = current_database() AND table_schema = ? "
            "ORDER BY table_name", [schema]).fetchall()
        views = [name for name, kind in listing if kind != "BASE TABLE"]
        with tempfile.TemporaryDirectory(prefix="alkera-pg-seed-") as scratch:
            for name, kind in listing:
                if kind != "BASE TABLE":
                    continue
                columns = tuple(
                    (column, pg_type(dtype, column=f"{schema}.{name}.{column}"))
                    for column, dtype in con.execute(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_catalog = current_database() AND table_schema = ? "
                        "AND table_name = ? ORDER BY ordinal_position",
                        [schema, name]).fetchall())
                qualified = f"{_ident(catalog)}.{_ident(schema)}.{_ident(name)}"
                order = ", ".join(str(i + 1) for i in range(len(columns)))
                target = Path(scratch) / f"{name}.csv"
                con.execute(f"COPY (SELECT * FROM {qualified} ORDER BY {order}) TO "
                            f"'{target.as_posix()}' (FORMAT csv, HEADER false)")
                text = target.read_text(encoding="utf-8")
                if any(line == "\\." for line in text.splitlines()):
                    raise SeedError(f"{schema}.{name}: a row reads `\\.`, which ends "
                                    f"psql's inline COPY data")
                rows = con.execute(f"SELECT count(*) FROM {qualified}").fetchone()[0]
                tables.append(Table(name, columns, int(rows), text))
    finally:
        con.close()
    return tables, views


def seed_script(tables: list[Table], schema: str = SCHEMA) -> str:
    """The psql script that copies `tables` into `schema`: run with `psql -1` as the
    superuser, as role OWNER, data inline after each COPY."""
    lines = [f"SET ROLE {OWNER};"]
    for table in tables:
        qualified = f"{_ident(schema)}.{_ident(table.name)}"
        columns = ", ".join(f"{_ident(c)} {t}" for c, t in table.columns)
        names = ", ".join(_ident(c) for c, _ in table.columns)
        lines += [f"DROP TABLE IF EXISTS {qualified};",
                  f"CREATE TABLE {qualified} ({columns});",
                  f"COPY {qualified} ({names}) FROM STDIN WITH (FORMAT csv);"]
        lines.append(table.csv.rstrip("\n") + "\n\\." if table.csv.strip() else "\\.")
        lines.append(f"GRANT {AGENT_PRIVILEGES} ON {qualified} TO {AGENT_ROLE};")
    return "\n".join(lines) + "\n"


def count_query(tables: list[Table], schema: str = SCHEMA) -> str:
    """One row per table: `<name>|<count>`, ordered by name."""
    return " UNION ALL ".join(
        f"SELECT '{t.name}' AS t, count(*) AS n FROM {_ident(schema)}.{_ident(t.name)}"
        for t in sorted(tables, key=lambda t: t.name)) + " ORDER BY 1"


Runner = Callable[..., subprocess.CompletedProcess]


def _psql(container: str, *args: str) -> list[str]:
    return ["docker", "exec", "-i", container, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
            "-U", SUPERUSER, "-d", DATABASE, *args]


def service_container(stack, service: str = SERVICE) -> str:
    ids = stack._compose("ps", "-q", service).stdout.split()
    if not ids:
        raise SeedError(f"no running `{service}` container in project {stack.project}")
    return ids[0]


def seed(stack, *, runner: Runner = subprocess.run) -> dict | None:
    """Copy the world's `warehouse.*` into the stack's Postgres when its profile plugs
    `postgres`; None (and nothing touched) otherwise. Raises SeedError on a failed or
    short copy: a de/postgres box whose warehouse is not the world is not that box."""
    if not plugged(stack.environment_profile()):
        return None
    started = time.monotonic()
    warehouse = Path(stack.world.get("warehouse") or (stack.world_root / WAREHOUSE_FILE))
    tables, views = read_tables(warehouse)
    if not tables:
        raise SeedError(f"{warehouse} has no base table in schema {SCHEMA!r} to seed")
    container = service_container(stack)
    done = runner(_psql(container, "-1"), input=seed_script(tables), capture_output=True,
                  text=True, encoding="utf-8", errors="replace", timeout=600)
    if done.returncode != 0:
        raise SeedError(f"seeding {SCHEMA}.* into Postgres failed ({done.returncode}): "
                        f"{(done.stderr or done.stdout)[-1500:]}")
    check = runner(_psql(container, "-tA", "-F", "|", "-c", count_query(tables)),
                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=120)
    got = dict(line.split("|", 1) for line in check.stdout.split() if "|" in line)
    counts = {t.name: t.rows for t in tables}
    seeded = {name: int(n) for name, n in got.items()}
    if check.returncode != 0 or seeded != counts:
        raise SeedError(f"seeded row counts {seeded or check.stderr[-500:]} != the "
                        f"world's {counts}")
    return {"schema": SCHEMA, "source": str(warehouse), "tables": counts,
            "views_skipped": views, "role": OWNER, "granted_to": AGENT_ROLE,
            "seconds": round(time.monotonic() - started, 2)}


def tail_argv(container: str, clock: str | None) -> list[str]:
    """`docker exec` the tailer in native-services over the server's log."""
    env: Mapping[str, str] = {
        "ALKERA_NATIVE_AUDIT": f"{AUDIT_IN_SERVICES}/actions.jsonl",
        "ALKERA_SCHEDULER": SCHEDULER_IN_SERVICES,
        **({"ALKERA_VIRTUAL_TIME": clock} if clock else {}),
    }
    flags = [part for name, value in env.items() for part in ("-e", f"{name}={value}")]
    return ["docker", "exec", *flags, container, "sh", "-c", TAIL_SCRIPT]


def fold_statement_log(stack, *, clock: str | None = None,
                       runner: Runner = subprocess.run) -> dict:
    """Run the Postgres tailer over the server's statement log, appending one
    `psql.statement` row per statement to the stack's action log. Called from
    `down()` while the stack is still up, before anything else is torn down."""
    container = getattr(stack, "services_container", "")
    if not container:
        return {"rows": None, "error": "no native-services container to run the tailer in"}
    started = time.monotonic()
    done = runner(tail_argv(container, clock), capture_output=True, text=True,
                  encoding="utf-8", errors="replace", timeout=600)
    found = ROWS_LINE.search(done.stderr or "")
    out = {"rows": int(found.group(1)) if found else None, "returncode": done.returncode,
           "log": SERVER_LOG, "seconds": round(time.monotonic() - started, 2)}
    if done.returncode == NO_LOG:
        out["error"] = f"the server wrote no {SERVER_LOG}"
    elif done.returncode != 0 or found is None:
        out["error"] = (done.stderr or done.stdout or "")[-1500:]
    return out
