"""Postgres `log_statement=all` -> one 9.2.2 row per statement.

HOW THE SERVER IS MADE TO TALK
------------------------------
    postgres -c log_statement=all -c log_min_duration_statement=0
                 -c log_line_prefix='%m [%p] '

`log_statement=all` writes every statement the server received, before it runs,
under its backend pid. `log_min_duration_statement=0` writes a `duration:` entry
after it, which is the only place a real `latency_ms` can come from - the client
is not being asked. Neither setting is needed for the parse to work; without the
second, rows carry `latency_ms: 0.0` and say so with the flag `no_duration`.

WHY THE PARSER IGNORES `log_line_prefix`
----------------------------------------
The prefix is operator-configurable, so keying on it would make this file a
guess about somebody's postgresql.conf. Postgres indents every continuation line
of a multi-line statement with a tab and starts every new entry at column zero,
so "a new entry begins at a line that does not start with whitespace" is true
for every prefix. The severity is then the first `LOG:  `/`ERROR:  `/... in the
line. That is the whole grammar this module relies on.

CLASSIFICATION IS THE GATEWAY'S, NOT A SECOND OPINION
-----------------------------------------------------
`op_class`, `targets`, `read_targets` and `flags` come from
`alkera_gateway.sql.inspect_sql`, the same call `Operations.sql_query` and
`sql_execute` make, at `dialect="postgres"`. Using the brief's verb table here
instead would put `ALTER` in `ddl` natively and `destructive` over MCP - the two
logs would disagree on exactly the rows a grader cares about. The verb table
below is therefore the *fallback*, for statements `sql.py` deliberately refuses
(`SET`, `BEGIN`, `COPY`, `GRANT`, `VACUUM`): those get `op_class` from the verb,
targets from a bare sqlglot parse, and the flag `outside_tool_surface`.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

from ...records import Observation, ToolError
from ...sql import inspect_sql
from ..audit import record

ACTOR = "postgres-tailer"
TOOL = "psql.statement"
DIALECT = "postgres"

SEVERITY = re.compile(
    r"(?P<severity>LOG|ERROR|FATAL|PANIC|WARNING|NOTICE|DETAIL|STATEMENT|HINT|CONTEXT):  "
)
PID = re.compile(r"\[(?P<pid>\d+)\]")
# `statement: <sql>` (simple protocol) and `execute <portal>: <sql>` (extended,
# what psycopg and every driver that binds parameters sends).
STATEMENT = re.compile(r"^(?:statement|execute [^:]*): (?P<sql>.*)$", re.DOTALL)
# `duration: 0.773 ms` on its own follows a statement `log_statement` already
# logged. When `log_statement` did NOT cover the statement but
# `log_min_duration_statement` did, the server writes both in ONE entry -
# `duration: 0.773 ms  statement: SELECT ...` - and a parser that stops at the
# duration drops that statement silently. Measured on postgres 16.15 by logging
# per-role: the covered role produced two entries, the uncovered one produced
# this combined entry.
DURATION = re.compile(r"^duration: (?P<ms>[0-9.]+) ms(?:  (?P<tail>.*))?$", re.DOTALL)
PARAMETERS = re.compile(r"^parameters: (?P<parameters>.*)$", re.DOTALL)

# Verbs `sql.py` does not model. Fallback only - see the module docstring.
VERB_OP_CLASS = {
    "SELECT": "read",
    "WITH": "read",
    "TABLE": "read",
    "VALUES": "read",
    "SHOW": "read",
    "EXPLAIN": "read",
    "INSERT": "write",
    "UPDATE": "write",
    "MERGE": "write",
    "COPY": "write",
    "CREATE": "ddl",
    "ALTER": "ddl",
    "COMMENT": "ddl",
    "GRANT": "ddl",
    "REVOKE": "ddl",
    "DROP": "destructive",
    "TRUNCATE": "destructive",
    "DELETE": "destructive",
}
FALLBACK_OP_CLASS = "exec"
# Schemas whose tables are the server describing itself. psql's `\d` is eight
# catalog SELECTs the agent never typed, so rows over these are flagged rather
# than dropped: a grader can exclude them, and an agent that really did query
# `information_schema` is still on the record.
CATALOG_SCHEMAS = ("pg_catalog.", "information_schema.")


@dataclass
class Entry:
    """One server log entry: its pid, its severity and its unwrapped body."""

    pid: str
    severity: str
    body: str


@dataclass
class Statement:
    """A statement the server received, with whatever the log later said about it."""

    pid: str
    sql: str
    parameters: str | None = None
    duration_ms: float = 0.0
    error: str | None = None
    flags: list[str] = field(default_factory=list)


def iter_entries(lines: Iterable[str]) -> Iterator[Entry]:
    """Log lines -> entries, joining tab-indented continuation lines."""
    pid = ""
    severity = ""
    body: list[str] = []
    for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")
        if line[:1] in ("\t", " ") and severity:
            body.append(line.lstrip("\t"))
            continue
        match = SEVERITY.search(line)
        if match is None:
            continue
        if severity:
            yield Entry(pid, severity, "\n".join(body))
        found = PID.search(line[: match.start()])
        pid = found.group("pid") if found else ""
        severity = match.group("severity")
        body = [line[match.end():]]
    if severity:
        yield Entry(pid, severity, "\n".join(body))


def iter_statements(entries: Iterable[Entry]) -> Iterator[Statement]:
    """Entries -> statements, closing each one when its backend moves on.

    A statement is emitted when the same backend logs its next entry that is not
    about it, or at end of stream. That ordering is what lets `duration:`,
    `DETAIL:  parameters:` and a failing `ERROR:` - all of which the server
    writes *after* the statement - land on the row they belong to.

    A failing statement is emitted AT its `ERROR:`/`FATAL:`/`PANIC:`. The server
    writes no `duration:` after an error, so waiting for the backend's next entry
    held the row back until that backend spoke again - for a `psql -c` that exits
    on the error, until the end of the log - and it landed after statements other
    backends ran later (measured: a refused DROP's row came last of 33).
    """
    pending: dict[str, Statement] = {}
    for entry in entries:
        current = pending.get(entry.pid)
        if entry.severity in ("ERROR", "FATAL", "PANIC"):
            if current is not None:
                current.error = entry.body
                current.flags.append("no_duration")
                yield pending.pop(entry.pid)
            continue
        if entry.severity == "DETAIL":
            parameters = PARAMETERS.match(entry.body)
            if current is not None and parameters:
                current.parameters = parameters.group("parameters")
            continue
        if entry.severity != "LOG":
            continue
        duration = DURATION.match(entry.body)
        if duration is not None:
            if current is not None:
                current.duration_ms = float(duration.group("ms"))
                yield pending.pop(entry.pid)
            combined = STATEMENT.match((duration.group("tail") or "").strip())
            if combined is not None:
                yield Statement(
                    pid=entry.pid,
                    sql=combined.group("sql").strip(),
                    duration_ms=float(duration.group("ms")),
                )
            continue
        statement = STATEMENT.match(entry.body)
        if statement is None:
            continue
        if current is not None:
            current.flags.append("no_duration")
            yield pending.pop(entry.pid)
        pending[entry.pid] = Statement(pid=entry.pid, sql=statement.group("sql").strip())
    for statement in pending.values():
        statement.flags.append("no_duration")
        yield statement


def copy_direction(sql: str) -> str | None:
    """`TO` or `FROM` for a COPY, ignoring a parenthesised source query.

    `COPY t TO STDOUT` reads and `COPY t FROM STDIN` writes, so the verb alone
    is not enough, and `COPY (SELECT * FROM t) TO STDOUT` puts a FROM in front
    of the one that decides. Parenthesised text is therefore removed first.
    """
    body = sql[sql.upper().index("COPY") + len("COPY"):]
    depth = 0
    outside = []
    for character in body:
        if character == "(":
            depth += 1
        elif character == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            outside.append(character)
    found = re.search(r"\b(TO|FROM)\b", "".join(outside), re.IGNORECASE)
    return found.group(1).upper() if found else None


def fallback_observation(sql: str, dialect: str = DIALECT) -> Observation:
    """Targets and op_class for a statement outside the gateway's tool surface.

    `dialect` defaults to this tailer's own, so every call that existed when
    this parameter did not behaves exactly as it was measured. The DuckDB CLI
    shim (`native/shims/duckdb_cli.py`) passes `"duckdb"`: the verb table and
    the COPY rule are dialect-independent, and only the bare sqlglot parse
    below is not.
    """
    observation = Observation(flags=["outside_tool_surface"])
    verb = re.match(r"\s*(?P<verb>[A-Za-z]+)", sql)
    name = verb.group("verb").upper() if verb else ""
    observation.op_class = VERB_OP_CLASS.get(name, FALLBACK_OP_CLASS)
    if name == "COPY" and copy_direction(sql) == "TO":
        observation.op_class = "read"
    try:
        trees = [tree for tree in sqlglot.parse(sql, dialect=dialect) if tree is not None]
    except sqlglot.errors.ParseError:
        observation.flags.append("unparsed_statement")
        return observation
    targets = sorted({table.sql() for tree in trees for table in tree.find_all(exp.Table)})
    observation.targets = targets
    if observation.op_class == "read":
        observation.read_targets = targets
    return observation


def classify(sql: str) -> Observation:
    """The gateway's own classification, with the verb fallback behind it."""
    observation = Observation()
    try:
        inspect_sql(sql, observation, read_only=False, dialect=DIALECT)
    except ToolError:
        observation = fallback_observation(sql)
    if observation.targets and all(
        target.lower().startswith(CATALOG_SCHEMAS) for target in observation.targets
    ):
        observation.flags.append("catalog_only")
    return observation


def emit(statement: Statement) -> dict:
    """One statement -> one 9.2.2 row."""
    observation = classify(statement.sql)
    args = {"sql": statement.sql, "backend_pid": statement.pid}
    if statement.parameters is not None:
        args["parameters"] = statement.parameters
    return record(
        tool=TOOL,
        args=args,
        op_class=observation.op_class,
        targets=observation.targets,
        read_targets=observation.read_targets,
        exit="error" if statement.error else "ok",
        error={"code": "sql_error", "message": statement.error} if statement.error else None,
        latency_ms=statement.duration_ms,
        flags=observation.flags + statement.flags,
        actor=ACTOR,
    )


def run(lines: Iterable[str]) -> list[dict]:
    """Drain a stream of log lines, returning every row written."""
    return [emit(statement) for statement in iter_statements(iter_entries(lines))]


def follow(path: Path, poll_s: float = 0.2) -> Iterator[str]:
    """Yield lines as the server appends them; blocks, like `tail -f`."""
    with open(path, encoding="utf-8", errors="replace", newline="") as stream:
        stream.seek(0, 2)
        buffer = ""
        while True:
            chunk = stream.readline()
            if not chunk:
                time.sleep(poll_s)
                continue
            buffer += chunk
            if buffer.endswith("\n"):
                yield buffer
                buffer = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "log",
        nargs="?",
        help="server log file to follow; omitted means read stdin to EOF "
        "(`docker logs -f postgres 2>&1 | python -m ...`)",
    )
    arguments = parser.parse_args(argv)
    if arguments.log:
        for statement in iter_statements(iter_entries(follow(Path(arguments.log)))):
            emit(statement)
        return 0
    rows = run(sys.stdin)
    print(f"{len(rows)} rows", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
