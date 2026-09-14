"""The stable warehouse seam; drivers own connections, never audit rows."""

import os
from threading import Timer
from typing import Protocol

import duckdb
from sqlglot import exp

from .config import GatewayConfig
from .records import Observation, ToolError, json_value
from .sql import inspect_sql


class WarehouseConnector(Protocol):
    dialect: str

    def execute(
        self,
        sql: str,
        observation: Observation,
        *,
        read_only: bool = False,
        limit: int | None = None,
    ) -> dict: ...


def count_result(count):
    return {"columns": [{"name": "Count", "type": "BIGINT"}], "rows": [[count]], "truncated": False}


class DuckDBWarehouse:
    """The default backend, and the only one a world gets without declaring one.

    Never open the warehouse file as the primary catalog: DuckDB names the
    catalog after the file stem, so `/var/warehouse/warehouse.duckdb` holding a
    schema named `warehouse` makes every two-part reference ambiguous. Attach it
    under `wh` to an in-memory connection instead (agent.md 9.2.1).
    """

    dialect = "duckdb"

    def __init__(self, config: GatewayConfig):
        self.config = config

    def connect(self, read_only: bool):
        conn = duckdb.connect(
            ":memory:",
            config={
                "threads": 1,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
            },
        )
        path = str(self.config.warehouse).replace("'", "''")
        mode = " (READ_ONLY)" if read_only else ""
        conn.execute(f"ATTACH '{path}' AS wh{mode}")
        conn.execute("USE wh.warehouse")
        conn.execute("SET enable_external_access = false")
        conn.execute("SET lock_configuration = true")
        return conn

    def result(self, cursor, row_limit: int):
        columns = [
            {"name": name, "type": str(dtype)} for name, dtype, *_ in cursor.description or []
        ]
        rows = cursor.fetchmany(row_limit + 1) if columns else []
        return {
            "columns": columns,
            "rows": json_value(rows[:row_limit]),
            "truncated": len(rows) > row_limit,
        }

    def execute(self, sql: str, observation: Observation, *, read_only=False, limit=None):
        statements = inspect_sql(sql, observation, read_only, self.dialect)
        row_limit = min(limit or self.config.max_rows, self.config.max_rows)
        with self.connect(read_only) as conn:
            timer = Timer(self.config.sql_timeout_s, conn.interrupt)
            timer.daemon = True
            timer.start()
            try:
                if len(statements) == 1:
                    results = [self.result(conn.execute(sql), row_limit)]
                else:
                    conn.begin()
                    try:
                        results = [
                            self.result(
                                conn.execute(statement.sql(dialect=self.dialect)), row_limit
                            )
                            for statement in statements
                        ]
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
            finally:
                timer.cancel()
                timer.join()
        counts = [
            result["rows"][0][0]
            for result in results
            if result["columns"] == [{"name": "Count", "type": "BIGINT"}] and result["rows"]
        ]
        if observation.op_class != "read" and counts:
            observation.rows_affected = sum(counts)
        if len(results) == 1:
            return results[0]
        return {"statements": results}


PG_TYPES = {
    16: "BOOLEAN",
    17: "BLOB",
    20: "BIGINT",
    21: "SMALLINT",
    23: "INTEGER",
    25: "VARCHAR",
    700: "FLOAT",
    701: "DOUBLE",
    1042: "VARCHAR",
    1043: "VARCHAR",
    1082: "DATE",
    1083: "TIME",
    1114: "TIMESTAMP",
    1184: "TIMESTAMP WITH TIME ZONE",
    114: "JSON",
    3802: "JSON",
    2950: "UUID",
}


def pg_type(column):
    if column.type_code == 1700:
        if column.precision is not None and column.scale is not None:
            return f"DECIMAL({column.precision},{column.scale})"
        return "DECIMAL"
    return PG_TYPES.get(column.type_code, f"POSTGRES_OID_{column.type_code}")


class PostgresWarehouse:
    dialect = "postgres"

    def __init__(self, config):
        self.config = config

    def execute(self, sql, observation, *, read_only=False, limit=None):
        import psycopg

        statements = inspect_sql(sql, observation, read_only, self.dialect)
        dsn = os.environ.get(self.config.warehouse_dsn_env)
        if not dsn:
            raise ToolError("warehouse_unconfigured", "Postgres connection secret is missing")
        row_limit = min(limit or self.config.max_rows, self.config.max_rows)
        results, counts = [], []
        try:
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                conn.read_only = read_only
                conn.execute("SET LOCAL search_path TO warehouse, public")
                conn.execute(f"SET LOCAL statement_timeout = {self.config.sql_timeout_s * 1000}")
                for statement in statements:
                    cursor = conn.execute(statement.sql(dialect=self.dialect))
                    if cursor.description:
                        rows = cursor.fetchmany(row_limit + 1)
                        results.append(
                            {
                                "columns": [
                                    {"name": c.name, "type": pg_type(c)} for c in cursor.description
                                ],
                                "rows": json_value(rows[:row_limit]),
                                "truncated": len(rows) > row_limit,
                            }
                        )
                    else:
                        count = max(cursor.rowcount, 0)
                        empty_column = {"name": "Success", "type": "BOOLEAN"} if isinstance(
                            statement, (exp.Drop, exp.Alter)
                        ) else {"name": "Count", "type": "BIGINT"}
                        results.append(
                            count_result(count)
                            if cursor.rowcount >= 0
                            else {
                                "columns": [empty_column],
                                "rows": [],
                                "truncated": False,
                            }
                        )
                        if cursor.rowcount >= 0:
                            counts.append(count)
        except psycopg.Error as exc:
            message = exc.diag.message_primary or "Postgres connection failed"
            raise ToolError("warehouse_error", message) from None
        if counts and observation.op_class != "read":
            observation.rows_affected = sum(counts)
        return results[0] if len(results) == 1 else {"statements": results}


def create_warehouse(config: GatewayConfig) -> WarehouseConnector:
    if config.warehouse_backend == "duckdb":
        return DuckDBWarehouse(config)
    if config.warehouse_backend == "postgres":
        return PostgresWarehouse(config)
    from .snowflake import SnowflakeWarehouse

    return SnowflakeWarehouse(config)
