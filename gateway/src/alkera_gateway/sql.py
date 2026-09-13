from threading import Timer

import duckdb
import sqlglot
from sqlglot import exp

from .config import GatewayConfig
from .records import Observation, ToolError, json_value

OP_PRIORITY = {"read": 0, "write": 1, "ddl": 2, "destructive": 3}


def inspect_statement(tree, read_only: bool) -> Observation:
    observation = Observation()
    tables = sorted({table.sql(dialect="duckdb") for table in tree.find_all(exp.Table)})
    observation.targets = tables
    if isinstance(tree, (exp.Query, exp.Describe, exp.Show)):
        observation.read_targets = tables
        return observation
    if read_only:
        raise ToolError("read_only", "Use sql_execute for statements that change state")
    observation.op_class = "write"
    if isinstance(tree, (exp.Drop, exp.Delete, exp.TruncateTable, exp.Alter)):
        observation.op_class = "destructive"
        observation.flags.append(type(tree).__name__.lower())
        if isinstance(tree, exp.Delete) and tree.args.get("where") is None:
            observation.flags.append("unbounded_delete")
    elif isinstance(tree, exp.Create):
        observation.op_class = "ddl"
        if tree.args.get("replace"):
            observation.op_class = "destructive"
            observation.flags.append("replace")
        if tree.args.get("kind") not in {"TABLE", "VIEW", "SCHEMA", "INDEX"}:
            raise ToolError(
                "unsupported_sql", "Only tables, views, schemas and indexes may be created"
            )
    elif not isinstance(tree, (exp.Insert, exp.Update, exp.Merge)):
        raise ToolError("unsupported_sql", "This statement is outside the warehouse tool surface")
    if isinstance(tree, exp.Update) and tree.args.get("where") is None:
        observation.op_class = "destructive"
        observation.flags.append("unbounded_update")
    if isinstance(tree, exp.Insert) and tree.args.get("overwrite"):
        observation.op_class = "destructive"
        observation.flags.append("insert_overwrite")
    return observation


def inspect_sql(sql: str, observation: Observation, read_only: bool):
    try:
        statements = [item for item in sqlglot.parse(sql, dialect="duckdb") if item is not None]
    except sqlglot.errors.ParseError as exc:
        raise ToolError("invalid_sql", str(exc)) from exc
    if not statements:
        raise ToolError("invalid_sql", "Submit at least one SQL statement")
    if read_only and len(statements) != 1:
        raise ToolError("invalid_sql", "sql_query accepts exactly one statement")
    targets = set()
    read_targets = set()
    op_class = "read"
    for statement in statements:
        current = inspect_statement(statement, read_only)
        targets.update(current.targets)
        read_targets.update(current.read_targets)
        observation.flags.extend(current.flags)
        if OP_PRIORITY[current.op_class] > OP_PRIORITY[op_class]:
            op_class = current.op_class
    observation.op_class = op_class
    observation.targets = sorted(targets)
    observation.read_targets = sorted(read_targets)
    return statements


class Warehouse:
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
        statements = inspect_sql(sql, observation, read_only)
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
                            self.result(conn.execute(statement.sql(dialect="duckdb")), row_limit)
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
