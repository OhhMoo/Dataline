"""Connection plumbing only; Snowflake semantics belong to the upstream mock."""

import json
import os

from sqlglot import exp

from .records import ToolError, json_value
from .sql import inspect_sql
from .warehouse import count_result

# The statements a multi-statement `sql_execute` may contain. Snowflake commits
# every DDL statement implicitly - including the open transaction before it - so a
# batch with a CREATE/DROP/ALTER/TRUNCATE in it is not atomic on Snowflake, while
# fakesnow (DuckDB underneath) would roll that DDL back. Running such a batch here
# would teach an agent a rollback guarantee the real system does not give, so it is
# refused before anything runs. Each DDL statement on its own call is fine.
BATCHABLE = (exp.Query, exp.Insert, exp.Update, exp.Delete, exp.Merge)
DML = (exp.Insert, exp.Update, exp.Delete, exp.Merge)

# Snowflake's own type names (snowflake.connector.constants.FIELD_ID_TO_NAME),
# not DuckDB's: an engine-specific type is allowed to differ across backends, a
# misleading one is not. FIXED is spelled NUMBER(p,s) so a float result of
# NUMBER arithmetic (DIVERGENCES `number_arithmetic_is_float`) reads FLOAT.
TYPE_NAMES = {
    1: "FLOAT",
    2: "VARCHAR",
    3: "DATE",
    4: "TIMESTAMP",
    5: "VARIANT",
    6: "TIMESTAMP_LTZ",
    7: "TIMESTAMP_TZ",
    8: "TIMESTAMP_NTZ",
    9: "OBJECT",
    10: "ARRAY",
    11: "BINARY",
    12: "TIME",
    13: "BOOLEAN",
}


def snowflake_type(column):
    if column.type_code == 0:
        if column.precision is not None and column.scale is not None:
            return f"NUMBER({column.precision},{column.scale})"
        return "NUMBER"
    return TYPE_NAMES.get(column.type_code, f"SNOWFLAKE_{column.type_code}")


class SnowflakeWarehouse:
    dialect = "snowflake"

    def __init__(self, config):
        self.config = config

    def result(self, cursor, statement, row_limit):
        if isinstance(statement, DML) and cursor.rowcount is not None and cursor.rowcount >= 0:
            # The contract's DML shape (DuckDB's, Postgres's), in place of
            # Snowflake's "number of rows inserted" / "... updated" columns.
            return count_result(cursor.rowcount), cursor.rowcount
        columns = [{"name": c.name, "type": snowflake_type(c)} for c in cursor.description or []]
        rows = cursor.fetchmany(row_limit + 1) if columns else []
        return {
            "columns": columns,
            "rows": json_value(rows[:row_limit]),
            "truncated": len(rows) > row_limit,
        }, None

    def execute(self, sql, observation, *, read_only=False, limit=None):
        try:
            import snowflake.connector
        except ImportError:
            raise ToolError(
                "warehouse_unconfigured", "Install the gateway snowflake extra"
            ) from None
        statements = inspect_sql(sql, observation, read_only, self.dialect)
        if len(statements) > 1:
            refused = [s for s in statements if not isinstance(s, BATCHABLE)]
            if refused:
                raise ToolError(
                    "unsupported_sql",
                    "Snowflake commits DDL implicitly, so a batch containing "
                    f"{type(refused[0]).__name__.upper()} cannot be atomic; send each DDL "
                    "statement as its own sql_execute call. A multi-statement batch may "
                    "hold only SELECT, INSERT, UPDATE, DELETE and MERGE.",
                )
        options = os.environ.get(self.config.snowflake_options_env)
        if not options:
            raise ToolError("warehouse_unconfigured", "Snowflake connection secret is missing")
        row_limit = min(limit or self.config.max_rows, self.config.max_rows)
        timeout = self.config.sql_timeout_s
        connect = {**json.loads(options), "login_timeout": 5, "network_timeout": timeout}
        results, counts = [], []
        try:
            with snowflake.connector.connect(**connect) as conn:
                with conn.cursor() as cursor:
                    if len(statements) == 1:
                        cursor.execute(sql, timeout=timeout)
                        result, count = self.result(cursor, statements[0], row_limit)
                        results.append(result)
                        counts.append(count)
                    else:
                        results, counts = self.batch(cursor, statements, row_limit)
        except snowflake.connector.Error as exc:
            # Surface the connector's MESSAGE, not just its number. The hardened
            # fakesnow now returns Snowflake-shaped, NAMED refusals - "Unsupported
            # feature 'CREATE DYNAMIC TABLE'", "Division by zero" - and throwing
            # them away left the agent a bare integer, which discards the entire
            # point of making the fake loud instead of silently wrong. Matches the
            # Postgres driver, which already surfaces diag.message_primary
            # (warehouse.py:186-188).
            message = getattr(exc, "msg", None) or str(exc) or "Snowflake connection failed"
            raise ToolError("warehouse_error", message) from None
        counts = [count for count in counts if count is not None]
        if counts and observation.op_class != "read":
            observation.rows_affected = sum(counts)
        return results[0] if len(results) == 1 else {"statements": results}

    def batch(self, cursor, statements, row_limit):
        """All or nothing: BEGIN, each statement, COMMIT; ROLLBACK and raise on failure.

        The first failure ends the batch. That also keeps the batch clear of
        DIVERGENCES `failed_statement_aborts_the_transaction` (fakesnow aborts the
        transaction on any error, Snowflake does not): nothing is ever run after
        a failed statement, so both engines end in the same state - rolled back.
        """
        import snowflake.connector

        timeout = self.config.sql_timeout_s
        results, counts = [], []
        cursor.execute("BEGIN", timeout=timeout)
        try:
            for index, statement in enumerate(statements, 1):
                try:
                    cursor.execute(statement.sql(dialect=self.dialect), timeout=timeout)
                except snowflake.connector.Error as exc:
                    message = getattr(exc, "msg", None) or str(exc)
                    raise ToolError(
                        "warehouse_error",
                        f"statement {index} of {len(statements)} failed; the batch was "
                        f"rolled back: {message}",
                    ) from None
                result, count = self.result(cursor, statement, row_limit)
                results.append(result)
                counts.append(count)
            cursor.execute("COMMIT", timeout=timeout)
        except BaseException:
            try:
                cursor.execute("ROLLBACK", timeout=timeout)
            except snowflake.connector.Error:
                # The original failure is the one to report. A session that
                # cannot roll back is closed below, and an uncommitted
                # transaction dies with its session.
                pass
            raise
        return results, counts
