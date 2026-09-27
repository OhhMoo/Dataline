"""The `duckdb` CLI shim: the statements it was given become the row's targets.

    python -m alkera_gateway.native.shims.run duckdb -c "SELECT ..." warehouse.duckdb

WHY THIS BINARY IS NOT WRAPPED LIKE THE OTHERS
----------------------------------------------
`shims/run.py` audits a binary by diffing sha256 over the workspace: it can say
which files changed and nothing about what was done to them. That is the honest
answer for `dbt` and `python`, whose effects have no other description. It is
the wrong answer for `duckdb`, because the statement IS the description, and
`NATIVE.md`'s 2026-09-16 amendment makes that the difference between
the native and the MCP log:

> the `duckdb` shim parses `-c`/`.read`/stdin statements through
> `sql.inspect_sql` so rows carry table targets exactly as the MCP path does

So this module reads the command line the way the CLI reads it, hands each
command to `alkera_gateway.sql.inspect_sql` at `dialect="duckdb"` - the same
call `Operations.sql_query` and `sql_execute` make - and writes one 9.2.2 row
per command with the tables in `targets`. A row from `duckdb -c "DELETE FROM
warehouse.items"` and a row from `sql_execute` with the same SQL therefore carry
the same `op_class`, the same `targets` and the same `flags`, which is what
makes the two transports' target sets comparable at all.

ONE ROW PER COMMAND, NOT PER SEMICOLON
--------------------------------------
`duckdb -c "A; B"` is one command carrying two statements, and `inspect_sql`
merges them: the strongest `op_class` wins and the targets are the union. That
is exactly what `sql_execute(sql="A; B")` records over MCP, so the merge is
parity rather than a shortcut. The postgres tailer writes one row per statement
because the SERVER logs one entry per statement; here the command line is the
only record there is.

WHAT IS PARSED, AND WHAT IS NOT
-------------------------------
Parsed: `-c/--command/-cmd SQL` (repeatable), `-f/-file/-init FILE`, a `.read
FILE` dot-command anywhere in a command's text, a trailing positional SQL
argument after the database file, and stdin when it is not a terminal and the
invocation carries nothing the CLI runs and then exits on (`-c`, `-s`, `-f`, a
positional SQL argument): a pipe or heredoc is read to EOF, audited as one
`source: "stdin"` command, and forwarded byte for byte to the real binary.
`-cmd` and `-init` do not stop the CLI reading stdin, so their commands come
first and stdin's after. Not parsed: an interactive session's own typing (there is
no pipe to read), and `.shell`/`.system` escapes, which run a second binary this
shim never sees - the same hole `\\!` leaves in the postgres tailer, recorded in
native/PARITY.md. An invocation that yields no command at all still writes one
row, with the workspace diff `run.py` would have produced and the flag
`no_statements`, because an unaudited `duckdb` call is the one outcome this
module may not have.

THE ENGINE
----------
The real `duckdb` binary is run when there is one. When there is not - this
repository ships no `duckdb` CLI and neither the episode image nor the
development host has one - the same statements are executed through the
`duckdb` PYTHON module, which is the same libduckdb against the same file, and
every row says which of the two ran (`flags: ["duckdb_library"]`, and
`result.engine`). What is lost with the library is the CLI's own output
formatting and its dot-commands other than `.read`; what is not lost is the
statement, the file it ran against, the error and the exit code, which is all
the action log is about.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ...records import Observation, ToolError
from ...sql import inspect_sql
from ..audit import record
from ..tailers.postgres import fallback_observation

ACTOR = "shim"
BINARY = "duckdb"
TOOL = "duckdb.statement"
DIALECT = "duckdb"
#: Options that take the next argument as SQL to run.
COMMAND_FLAGS = ("-c", "--command", "-cmd", "-s")
#: Options that take the next argument as a file of SQL to run.
FILE_FLAGS = ("-f", "-file", "--file", "-init")
#: Of those, the ones that make the CLI exit afterwards ("run COMMAND and
#: exit", "read/process named file and exit") and so never read stdin. `-cmd`
#: and `-init` run first and the CLI then goes on to read stdin, exactly as
#: sqlite3's shell, which duckdb's is built from, does.
EXITING_FLAGS = ("-c", "--command", "-s", "-f", "-file", "--file")
#: Options that take a value this shim does not read, so the value is never
#: mistaken for the database file or for a trailing SQL argument.
VALUE_FLAGS = ("-separator", "-nullvalue", "-newline", "-cmd-timeout", "-p",
               "-log_query_path", "-ui_port")
#: How much of a `-f`/`.read` file is read into the row's `args`. A migration
#: script is legitimately large and a row is not a place to keep one.
MAX_SQL_BYTES = 64_000


@dataclass
class Command:
    """One unit of SQL the CLI was told to run, and where it came from."""

    sql: str
    source: str            # "-c" | "-f" | ".read" | "stdin" | "argv"
    origin: str | None = None      # the file a "-f"/".read" command came from


@dataclass
class Invocation:
    """What `parse_argv` made of a `duckdb` command line."""

    database: str | None = None
    read_only: bool = False
    commands: list[Command] = field(default_factory=list)
    interactive: bool = True       # no command of its own; stdin decides
    reads_stdin: bool = True       # no -c/-s/-f/positional SQL: the CLI reads stdin


def read_sql_file(name: str, cwd: Path) -> str:
    """The text of a `-f`/`.read` file, or "" when it cannot be read.

    An unreadable file is not an error here: the binary will report it, and a
    shim that refused to write a row because it could not read a file would
    turn a failed command into an unaudited one.
    """
    try:
        return (cwd / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def expand_dot_read(command: Command, cwd: Path) -> list[Command]:
    """`.read FILE` lines inside a command, replaced by the file's own SQL.

    The dot-command and the SQL around it are both kept: `duckdb -c ".read
    setup.sql" -c "SELECT 1"` must audit what setup.sql did, and a command that
    is nothing but dot-commands must still leave the row saying so.
    """
    out: list[Command] = []
    remainder: list[str] = []
    for line in command.sql.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(".read "):
            name = stripped[len(".read "):].strip().strip('"\'')
            text = read_sql_file(name, cwd)
            if text.strip():
                out.append(Command(text, ".read", origin=name))
            continue
        remainder.append(line)
    body = "\n".join(remainder)
    if body.strip():
        out.insert(0, Command(body, command.source, command.origin))
    return out or ([] if command.sql.strip() else [command])


def looks_like_sql(value: str) -> bool:
    """Is this positional argument the trailing SQL rather than a file name?

    The CLI's own rule is positional: the first non-option argument is the
    database and the second is SQL. This is only a guard for the first one, so
    `duckdb "SELECT 1"` - a legal invocation against an in-memory database -
    is not recorded as a database file called `SELECT 1`.
    """
    head = value.strip().split(None, 1)[0].upper() if value.strip() else ""
    return head in {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "CREATE",
                    "DROP", "ALTER", "COPY", "ATTACH", "PRAGMA", "CALL",
                    "EXPLAIN", "BEGIN", "COMMIT", "SET", "INSTALL", "LOAD",
                    "DESCRIBE", "SUMMARIZE", "TRUNCATE", "VACUUM", "CHECKPOINT"}


def parse_argv(argv: list[str], cwd: Path) -> Invocation:
    """A `duckdb` command line, as the CLI reads it."""
    invocation = Invocation()
    positionals: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in EXITING_FLAGS and index + 1 < len(argv):
            invocation.reads_stdin = False
        if token in COMMAND_FLAGS and index + 1 < len(argv):
            invocation.commands.append(Command(argv[index + 1], "-c"))
            index += 2
            continue
        if token in FILE_FLAGS and index + 1 < len(argv):
            name = argv[index + 1]
            invocation.commands.append(
                Command(read_sql_file(name, cwd), "-f", origin=name))
            index += 2
            continue
        if token in VALUE_FLAGS and index + 1 < len(argv):
            index += 2
            continue
        if token in ("-readonly", "--readonly"):
            invocation.read_only = True
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        positionals.append(token)
        index += 1
    if positionals:
        if looks_like_sql(positionals[0]):
            invocation.commands.append(Command(positionals[0], "argv"))
            invocation.reads_stdin = False
        else:
            invocation.database = positionals[0]
            for extra in positionals[1:]:
                invocation.commands.append(Command(extra, "argv"))
                invocation.reads_stdin = False
    invocation.interactive = not invocation.commands
    expanded: list[Command] = []
    for command in invocation.commands:
        expanded.extend(expand_dot_read(command, cwd))
    invocation.commands = expanded
    return invocation


def read_piped_stdin() -> bytes | None:
    """Stdin's bytes when it is a pipe, a file or a heredoc; None for a tty.

    This is what the CLI itself does when it was given nothing to run and exit
    on: a terminal gets the interactive prompt (left alone here, stdin is
    inherited), anything else is read to EOF as a script. The bytes are handed
    on to the real binary verbatim, because once read here they are gone from
    the descriptor it inherits.
    """
    stream = sys.stdin
    if stream is None:
        return None
    try:
        if stream.isatty():
            return None
        return stream.buffer.read()
    except (OSError, ValueError, AttributeError):
        # A closed or unusable stdin is one the CLI could not read either.
        return None


def add_stdin(invocation: Invocation, data: bytes, cwd: Path) -> None:
    """Append stdin's script to the invocation as one `stdin` command."""
    text = data.decode("utf-8", errors="replace")
    if text.strip():
        invocation.commands.extend(expand_dot_read(Command(text, "stdin"), cwd))
        invocation.interactive = False


def classify(sql: str) -> Observation:
    """The gateway's own classification of one command, at the duckdb dialect.

    Identical in shape to `tailers.postgres.classify`, and deliberately so: a
    statement that `sql.py` refuses (`SET`, `BEGIN`, `INSTALL`, `PRAGMA`) gets
    its `op_class` from the verb table and the flag `outside_tool_surface`
    rather than no row at all.
    """
    observation = Observation()
    try:
        inspect_sql(sql, observation, read_only=False, dialect=DIALECT)
    except ToolError:
        observation = fallback_observation(sql, dialect=DIALECT)
    return observation


def real_binary() -> str | None:
    """The `duckdb` binary this shim stands in front of, if the box has one."""
    from .run import real_binary as resolve

    return resolve(BINARY)


def execute_with_library(invocation: Invocation) -> tuple[int, str | None]:
    """Run the parsed commands through the `duckdb` module. (returncode, error).

    Output goes to stdout as the CLI's default box formatting does not: rows
    are printed one tuple per line. The agent sees the values it asked for; it
    does not see them in the CLI's table. Stated here rather than discovered by
    whoever reads a transcript.
    """
    import duckdb

    target = invocation.database or ":memory:"
    try:
        connection = duckdb.connect(target, read_only=invocation.read_only)
    except Exception as exc:                            # noqa: BLE001 - reported
        print(f"duckdb: {exc}", file=sys.stderr)
        return 1, f"{type(exc).__name__}: {exc}"
    try:
        for command in invocation.commands:
            # Statement by statement, so `SELECT 1; SELECT 2` prints both
            # results as the CLI does; `execute` on the whole text would
            # return only the last one.
            for statement in duckdb.extract_statements(command.sql):
                cursor = connection.execute(statement)
                if cursor.description:
                    for row in cursor.fetchall():
                        print("\t".join("" if value is None else str(value)
                                        for value in row))
    except Exception as exc:                            # noqa: BLE001 - reported
        print(f"duckdb: {exc}", file=sys.stderr)
        return 1, f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()
    return 0, None


def emit(command: Command, invocation: Invocation, *, engine: str,
         returncode: int, error: str | None, latency_ms: float,
         argv: list[str], cwd: str) -> dict:
    """One command -> one 9.2.2 row."""
    observation = classify(command.sql)
    flags = list(observation.flags)
    if engine == "library":
        flags.append("duckdb_library")
    failed = returncode != 0
    args = {"sql": command.sql[:MAX_SQL_BYTES], "source": command.source,
            "database": invocation.database, "argv": argv, "cwd": cwd}
    if command.origin:
        args["file"] = command.origin
    return record(
        tool=TOOL,
        args=args,
        op_class=observation.op_class,
        targets=observation.targets,
        read_targets=observation.read_targets,
        exit="error" if failed else "ok",
        error={"code": "sql_error", "message": error} if failed and error else
              ({"code": "command_failed",
                "message": f"duckdb exited {returncode}"} if failed else None),
        latency_ms=latency_ms,
        result={"engine": engine, "returncode": returncode},
        flags=flags,
        actor=ACTOR,
    )


def emit_unparsed(invocation: Invocation, *, engine: str, returncode: int,
                  latency_ms: float, argv: list[str], cwd: str,
                  targets: list[str], unscanned: bool) -> dict:
    """The row for an invocation that named no SQL at all.

    An interactive `duckdb` or one driven by dot-commands this shim does not
    read still moved the world, so it gets `run.py`'s answer - the workspace
    files that changed - under a flag that says the statement-level resolution
    was not available. Never no row.
    """
    flags = ["no_statements"] + (["duckdb_library"] if engine == "library" else [])
    if unscanned:
        flags.append("workspace_unscanned")
    failed = returncode != 0
    return record(
        tool=f"shim.{BINARY}",
        args={"argv": argv, "cwd": cwd, "database": invocation.database},
        op_class="exec",
        targets=targets,
        exit="error" if failed else "ok",
        error={"code": "command_failed",
               "message": f"duckdb exited {returncode}"} if failed else None,
        latency_ms=latency_ms,
        result={"engine": engine, "returncode": returncode, "changed": targets},
        flags=flags,
        actor=ACTOR,
    )


def main(argv: list[str]) -> int:
    """Run `duckdb argv`, audit every command it was given, return its code."""
    from .run import DEFAULT_MAX_FILES, changed, snapshot, workspace_root

    cwd = Path.cwd()
    invocation = parse_argv(argv, cwd)
    piped = read_piped_stdin() if invocation.reads_stdin else None
    if piped is not None:
        add_stdin(invocation, piped, cwd)
    executable = real_binary()
    root = workspace_root()
    max_files = int(os.environ.get("ALKERA_SHIM_MAX_FILES") or DEFAULT_MAX_FILES)
    before = snapshot(root, max_files) if invocation.interactive else None

    started = time.monotonic()
    if executable is not None:
        engine = "binary"
        # Stdin that was read for the audit is forwarded; otherwise inherited.
        completed = subprocess.run([executable, *argv], input=piped)
        returncode, error = completed.returncode, None
    else:
        engine = "library"
        returncode, error = execute_with_library(invocation)
    latency_ms = (time.monotonic() - started) * 1000

    record_argv = [BINARY, *argv]
    if invocation.commands:
        for command in invocation.commands:
            emit(command, invocation, engine=engine, returncode=returncode,
                 error=error, latency_ms=latency_ms / len(invocation.commands),
                 argv=record_argv, cwd=cwd.as_posix())
    else:
        after = snapshot(root, max_files) if before is not None else None
        unscanned = before is None or after is None
        emit_unparsed(invocation, engine=engine, returncode=returncode,
                      latency_ms=latency_ms, argv=record_argv,
                      cwd=cwd.as_posix(),
                      targets=[] if unscanned else changed(before, after),
                      unscanned=unscanned)
    return returncode
