"""Connection plumbing only; Snowflake semantics belong to the upstream mock."""

import json
import os

from .records import ToolError, json_value
from .sql import inspect_sql


class SnowflakeWarehouse:
    dialect = "snowflake"

    def __init__(self, config):
        self.config = config

    def execute(self, sql, observation, *, read_only=False, limit=None):
        try:
            import snowflake.connector
        except ImportError:
            raise ToolError(
                "warehouse_unconfigured", "Install the gateway snowflake extra"
            ) from None
        statements = inspect_sql(sql, observation, read_only, self.dialect)
        if len(statements) != 1:
            raise ToolError(
                "unsupported_sql", "Snowflake multi-statement atomicity is not certified"
            )
        options = os.environ.get(self.config.snowflake_options_env)
        if not options:
            raise ToolError("warehouse_unconfigured", "Snowflake connection secret is missing")
        limit = min(limit or self.config.max_rows, self.config.max_rows)
        try:
            with snowflake.connector.connect(
                **json.loads(options), login_timeout=5, network_timeout=self.config.sql_timeout_s
            ) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql, timeout=self.config.sql_timeout_s)
                    rows = cursor.fetchmany(limit + 1)
                    return {
                        "columns": [
                            {"name": c.name, "type": f"SNOWFLAKE_{c.type_code}"}
                            for c in cursor.description or []
                        ],
                        "rows": json_value(rows[:limit]),
                        "truncated": len(rows) > limit,
                    }
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
