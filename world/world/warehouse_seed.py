"""Seeding and fingerprinting a SECOND warehouse, never through the connector.

Two rules, and the first one is not about speed.

**A seeded warehouse must not appear in the episode's action log.** Every call
through `alkera_gateway` writes an audit row and consumes a `seq`. If world
construction went through `sql_execute`, the grader could not tell the world
from the agent, and `process_signals` would open every episode with a few
hundred destructive writes that nobody made. That alone settles it, whatever
the throughput is. It is also why `world/` deliberately does not import
`alkera_gateway` anywhere (`world/world/gateway_scheduler.py:8` says so in
prose), and why this module keeps that property.

**And seeding must be set-based.** Measured on this host, 203k rows into DuckDB:

    duckdb executemany, row at a time      187 rows/s     (~18 min)
    multi-row INSERT ... VALUES, 10k/batch  75,938 rows/s  (2.7 s)
    read_csv / COPY                      2,904,622 rows/s  (0.07 s)

Postgres `COPY ... FROM STDIN` measured 933,310 rows/s on the same data. The
row-at-a-time figure is the ~100 rows/s regime, and it is not a property of any
one backend - it is the cost of one statement per row. The Snowflake connector
is worse again for a structural reason worth naming: `SnowflakeWarehouse.execute`
opens a fresh `snowflake.connector.connect(...)` per call
(`gateway/src/alkera_gateway/snowflake.py:36`), so a row-at-a-time seed pays a
TLS handshake and a login-request per row.

So: CSV and COPY, or the driver's own bulk reader. Never a loop.

The second half of this module is `logical_hash`, which answers the question
`world/world/fingerprint.py` cannot: what IS the fingerprint of a warehouse that
does not live in the world tree? It produces the same canonical shape for a
DuckDB file and for a Postgres database, so a Postgres warehouse can carry a
real `initial_world_fp` instead of the gateway pretending a hash of the local
directory covered it (`gateway/src/alkera_gateway/config.py:75-76`).
"""

import csv
import hashlib
from pathlib import Path

from .hashing import canon_value, json_canon, json_hash

SCHEMAS = ("raw", "staging", "warehouse")


# --------------------------------------------------------------------- seeding


def write_rows_csv(path: Path, columns, rows) -> Path:
    """A deterministic CSV: LF endings, no locale, no BOM, stable column order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)
    return path


def seed_duckdb(db_path: Path, tables: dict) -> None:
    """Build a DuckDB warehouse from CSV files.

    `tables` maps a qualified name to `(csv_path, column_types)`, e.g.
    `{"warehouse.fct_revenue": (Path("landing/fct.csv"),
      {"day": "DATE", "amount": "DECIMAL(12,2)"})}`.

    One `CREATE TABLE AS SELECT ... FROM read_csv` per table. The cast list is
    explicit on purpose: `read_csv`'s sniffer is deterministic for a given file
    but not for a given *schema*, and a world whose column types depend on which
    rows the sniffer happened to look at is not a reproducible world.
    """
    import duckdb

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("PRAGMA threads=1")
        for schema in SCHEMAS:
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        for qualified, (csv_path, column_types) in sorted(tables.items()):
            columns = ", ".join(
                f'"{name}"::{dtype} AS "{name}"' for name, dtype in column_types.items()
            )
            source = Path(csv_path).as_posix().replace("'", "''")
            conn.execute(
                f"CREATE OR REPLACE TABLE {qualified} AS "
                f"SELECT {columns} FROM read_csv('{source}', header=true, "
                "all_varchar=true, sample_size=-1)"
            )


def seed_postgres(dsn: str, tables: dict) -> None:
    """Build a Postgres warehouse from the same CSV files, via COPY.

    `tables` maps a qualified name to `(csv_path, column_types)`, where the
    types are written straight into the DDL. COPY, not INSERT: measured at
    933,310 rows/s against the digest-pinned server `gateway/docker/compose.yml`
    runs, versus 187 rows/s for a row-at-a-time loop.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        # Not `CREATE SCHEMA IF NOT EXISTS`: Postgres checks the CREATE-on-
        # database privilege before it looks at IF NOT EXISTS, and the episode
        # stack's `episode` role OWNS raw/staging/warehouse (docker/postgres.sql)
        # without holding CREATE on the database. Measured on the compose
        # stack: "permission denied for database episode". So a schema that
        # exists is left alone, and only a missing one is created.
        present = {
            row[0] for row in conn.execute(
                "select nspname from pg_namespace where nspname = any(%s)", [list(SCHEMAS)]
            ).fetchall()
        }
        for schema in SCHEMAS:
            if schema not in present:
                conn.execute(f"CREATE SCHEMA {schema}")
        for qualified, (csv_path, column_types) in sorted(tables.items()):
            columns = ", ".join(f'"{name}" {dtype}' for name, dtype in column_types.items())
            names = ", ".join(f'"{name}"' for name in column_types)
            conn.execute(f"DROP TABLE IF EXISTS {qualified}")
            conn.execute(f"CREATE TABLE {qualified} ({columns})")
            with open(csv_path, newline="", encoding="utf-8") as handle:
                reader = csv.reader(handle)
                next(reader)  # header
                # The default COPY TEXT format, not `WITH (FORMAT csv)`:
                # `write_row` serialises through psycopg's own text protocol
                # (tab-separated, backslash-escaped) and the two framings do not
                # mix - asking for csv here makes the server read the whole
                # tab-joined line as the first column.
                with conn.cursor().copy(f"COPY {qualified} ({names}) FROM STDIN") as copy:
                    for row in reader:
                        copy.write_row(row)


# ---------------------------------------------------------------- fingerprinting


def _table_hash(columns, rows) -> str:
    digest = hashlib.sha256()
    digest.update(json_canon({"columns": columns}))
    for row in rows:
        digest.update(json_canon([canon_value(value) for value in row]) + b"\n")
    return digest.hexdigest()


def duckdb_logical(db_path: Path) -> dict:
    """Canonical logical contents of a DuckDB warehouse file.

    Deliberately the same shape as `world/world/fingerprint.py:_warehouse_hash`:
    base tables only, columns in ordinal order, rows ordered by every column.
    """
    import duckdb

    db_path = Path(db_path)
    catalog = db_path.stem
    conn = duckdb.connect(str(db_path), read_only=True)
    tables = {}
    try:
        placeholders = ", ".join(f"'{schema}'" for schema in SCHEMAS)
        listing = conn.execute(
            "select table_schema, table_name from information_schema.tables "
            f"where table_schema in ({placeholders}) and table_type = 'BASE TABLE' order by 1, 2"
        ).fetchall()
        for schema, table in listing:
            columns = conn.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_schema = ? and table_name = ? order by ordinal_position",
                [schema, table],
            ).fetchall()
            order = ", ".join(f'"{name}"' for name, _ in columns)
            rows = conn.execute(
                f'select * from "{catalog}"."{schema}"."{table}" order by {order}'
            ).fetchall()
            tables[f"{schema}.{table}"] = _table_hash(
                [[name, _normalize_type(dtype)] for name, dtype in columns], rows
            )
    finally:
        conn.close()
    return {"tables": tables, "hash": json_hash(tables)}


def postgres_logical(dsn: str) -> dict:
    """The same canonical shape, read out of a Postgres server.

    This is what makes a remote warehouse fingerprintable at all: the world
    tree cannot contain it, so the hash has to be taken over its logical
    contents. Type names are normalised through `_normalize_type` so that the
    same table seeded into either engine hashes the same - which is the whole
    claim a dual-run cutover rests on.
    """
    import psycopg

    tables = {}
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        placeholders = ", ".join(f"'{schema}'" for schema in SCHEMAS)
        listing = conn.execute(
            "select table_schema, table_name from information_schema.tables "
            f"where table_schema in ({placeholders}) and table_type = 'BASE TABLE' order by 1, 2"
        ).fetchall()
        for schema, table in listing:
            columns = conn.execute(
                "select column_name, data_type, numeric_precision, numeric_scale "
                "from information_schema.columns "
                "where table_schema = %s and table_name = %s order by ordinal_position",
                [schema, table],
            ).fetchall()
            order = ", ".join(f'"{name}"' for name, *_ in columns)
            rows = conn.execute(
                f'select * from "{schema}"."{table}" order by {order}'
            ).fetchall()
            tables[f"{schema}.{table}"] = _table_hash(
                [
                    [name, _normalize_type(dtype, precision, scale)]
                    for name, dtype, precision, scale in columns
                ],
                rows,
            )
    return {"tables": tables, "hash": json_hash(tables)}


TYPE_ALIASES = {
    "INTEGER": "INTEGER",
    "INT": "INTEGER",
    "INT4": "INTEGER",
    "BIGINT": "BIGINT",
    "INT8": "BIGINT",
    "SMALLINT": "SMALLINT",
    "CHARACTER VARYING": "VARCHAR",
    "VARCHAR": "VARCHAR",
    "TEXT": "VARCHAR",
    "BOOLEAN": "BOOLEAN",
    "DATE": "DATE",
    "DOUBLE PRECISION": "DOUBLE",
    "DOUBLE": "DOUBLE",
    "TIMESTAMP WITHOUT TIME ZONE": "TIMESTAMP",
    "TIMESTAMP": "TIMESTAMP",
}


def _normalize_type(dtype: str, precision=None, scale=None) -> str:
    """One spelling per logical type, so two engines agree or say why they do not.

    Not a transpiler and not trying to be: it folds the handful of spellings the
    seeded schemas actually use. An unrecognised type is returned verbatim, so
    a divergence shows up as a hash mismatch naming the column rather than as a
    silent equality.
    """
    upper = dtype.upper()
    if upper in ("NUMERIC", "DECIMAL"):
        if precision is not None and scale is not None:
            return f"DECIMAL({precision},{scale})"
        return "DECIMAL"
    if upper.startswith("DECIMAL(") or upper.startswith("NUMERIC("):
        return "DECIMAL" + upper[upper.index("(") :]
    return TYPE_ALIASES.get(upper, upper)


def logical_hash(target) -> str:
    """`initial_world_fp` material for one warehouse, whatever backend holds it."""
    if isinstance(target, (str, Path)) and str(target).endswith(".duckdb"):
        return duckdb_logical(Path(target))["hash"]
    return postgres_logical(str(target))["hash"]
