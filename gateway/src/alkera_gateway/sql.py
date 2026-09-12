from threading import Timer

import duckdb
import sqlglot
from sqlglot import exp

from .config import GatewayConfig
from .records import Observation, ToolError, json_value


def inspect_sql(sql: str, observation: Observation, read_only: bool):
    try:
        statements = [item for item in sqlglot.parse(sql, dialect="duckdb") if item is not None]
    except sqlglot.errors.ParseError as exc:
        raise ToolError("invalid_sql", str(exc)) from exc
    if len(statements) != 1:
        raise ToolError("invalid_sql", "Submit exactly one SQL statement per call")
    tree = statements[0]
    tables = sorted({table.sql(dialect="duckdb") for table in tree.find_all(exp.Table)})
    observation.targets = tables
    if isinstance(tree, (exp.Query, exp.Describe, exp.Show)):
        observation.read_targets = tables
        return
    observation.op_class = "write"
    if read_only:
        raise ToolError("read_only", "Use sql_execute for statements that change state")
    if isinstance(tree, (exp.Drop, exp.Delete, exp.TruncateTable)):
        observation.op_class = "destructive"
        observation.flags.append(type(tree).__name__.lower())
        if isinstance(tree, exp.Delete) and tree.args.get("where") is None:
            observation.flags.append("unbounded_delete")
    elif isinstance(tree, (exp.Create, exp.Alter)):
        observation.op_class = "ddl"
        if tree.args.get("replace"):
            observation.op_class = "destructive"
            observation.flags.append("replace")
        if isinstance(tree, exp.Create) and tree.args.get("kind") not in {
            "TABLE", "VIEW", "SCHEMA", "INDEX"
        }:
            raise ToolError("unsupported_sql", "Only tables, views, schemas and indexes may be created")
    elif not isinstance(tree, (exp.Insert, exp.Update, exp.Merge)):
        raise ToolError("unsupported_sql", "This statement is outside the warehouse tool surface")
    if isinstance(tree, exp.Update) and tree.args.get("where") is None:
        observation.op_class = "destructive"
        observation.flags.append("unbounded_update")


class Warehouse:
    def __init__(self, config: GatewayConfig):
        self.config = config

    def execute(self, sql: str, observation: Observation, *, read_only=False, limit=None):
        inspect_sql(sql, observation, read_only)
        settings = {
            "threads": 1,
            "enable_external_access": False,
            "allowed_directories": [str(self.config.workspace)],
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "lock_configuration": True,
        }
        row_limit = min(limit or self.config.max_rows, self.config.max_rows)
        with duckdb.connect(str(self.config.warehouse), read_only=read_only, config=settings) as conn:
            timer = Timer(self.config.sql_timeout_s, conn.interrupt)
            timer.daemon = True
            timer.start()
            try:
                cursor = conn.execute(sql)
                columns = [{"name": name, "type": str(dtype)} for name, dtype, *_ in cursor.description]
                rows = cursor.fetchmany(row_limit + 1)
            finally:
                timer.cancel()
                timer.join()
        if observation.op_class != "read" and columns == [{"name": "Count", "type": "BIGINT"}]:
            observation.rows_affected = rows[0][0] if rows else 0
        return {"columns": columns, "rows": json_value(rows[:row_limit]), "truncated": len(rows) > row_limit}

