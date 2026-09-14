"""Dialect-agnostic SQL inspection.

This module classifies a statement and never opens a connection: the driver and
its dialect live behind `warehouse.create_warehouse`. Keeping the two apart is
what lets a world declare a backend, so nothing here may import a database
driver or name a dialect.
"""

import sqlglot
from sqlglot import exp

from .records import Observation, ToolError

OP_PRIORITY = {"read": 0, "write": 1, "ddl": 2, "destructive": 3}


def inspect_statement(tree, read_only: bool) -> Observation:
    observation = Observation()
    tables = sorted({table.sql() for table in tree.find_all(exp.Table)})
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


def inspect_sql(sql: str, observation: Observation, read_only: bool, dialect: str):
    try:
        statements = [item for item in sqlglot.parse(sql, dialect=dialect) if item is not None]
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

