"""Make the instance behind ``acme.snowflake.local`` honest.

Applied at boot by ``snowflake_start.py``. Nothing here is imported by the host
episode path; it patches ``fakesnow`` inside the snowflake service container only.

The governing rule, inherited from R004 RESEARCH-warehouse §3: **loud unsupported is
fine; silent wrong is disqualifying.** Every change below either turns a silent wrong
into a loud refusal, turns an unattributable hang into an attributable error, or makes
a statement that should work actually work.

Five items, each keyed to the research that sized it:

1. §2.2  Qualified ``CREATE TEMPORARY TABLE`` - the one defect that breaks dbt
         incremental on the *second* run. fakesnow passes ``db.schema.name`` through
         to DuckDB, which forbids any qualification on a TEMPORARY table.
2. §2.3  The 500 -> retry storm. The connector retries HTTP 500 until
         ``network_timeout`` expires, so an unsupported statement costs ~30 s of the
         agent's wall clock and reports nothing it can attribute.
3. §3.2  Strict mode. sqlglot's ``Command`` fallback hands text we never parsed
         straight to DuckDB. Where DuckDB accepts it, the client is told the
         statement succeeded.
4. §3.2  Fences for the named silent-wrongs: ``CREATE DYNAMIC TABLE``,
         ``ALTER TABLE ... CLUSTER BY``, division and modulo by zero - in reads
         and, since the Snowflake plug's tests ran writes, in UPDATE / DELETE /
         INSERT / MERGE too.
5. §3.2  Session identity - the "where am I" reflex.

Environment (all optional; defaults are the ones the world image should ship):

    ALKERA_SNOWFLAKE_STRICT      "1" (default) fails on an unparsed Command; "0" warns
    ALKERA_SNOWFLAKE_FENCES      "1" (default) fences the silent-wrongs; "0" disables
    ALKERA_SNOWFLAKE_ACCOUNT     CURRENT_ACCOUNT()        default "ACME"
    ALKERA_SNOWFLAKE_REGION      CURRENT_REGION()         default "AWS_US_WEST_2"
    ALKERA_SNOWFLAKE_ROLE        CURRENT_ROLE()           default "SYSADMIN"
    ALKERA_SNOWFLAKE_VERSION     CURRENT_VERSION()        default "9.20.1"
    ALKERA_SNOWFLAKE_WAREHOUSES  comma-separated warehouse names, first is the default

Known divergences are recorded in DIVERGENCES below rather than in a separate document,
so they travel with the code that causes them.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import uuid
from typing import Any

import duckdb
import snowflake.connector.errors
import sqlglot
from sqlglot import exp

logger = logging.getLogger("alkera.snowflake_fidelity")

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


ACCOUNT = os.environ.get("ALKERA_SNOWFLAKE_ACCOUNT", "ACME")
REGION = os.environ.get("ALKERA_SNOWFLAKE_REGION", "AWS_US_WEST_2")
ROLE = os.environ.get("ALKERA_SNOWFLAKE_ROLE", "SYSADMIN")
VERSION = os.environ.get("ALKERA_SNOWFLAKE_VERSION", "9.20.1")
WAREHOUSES = [
    w.strip().upper()
    for w in os.environ.get("ALKERA_SNOWFLAKE_WAREHOUSES", "COMPUTE_WH,WH_ETL,WH_BI").split(",")
    if w.strip()
]

# A deterministic namespace so query ids replay identically. uuid4 does not.
QUERY_ID_NS = uuid.UUID("6f0f5c9e-1f4a-5e77-9a23-2f1d6f7b0c11")

# Raised through DuckDB's error() and caught on the way back out. DuckDB has no
# Snowflake-shaped division-by-zero, so the sentinel carries the intent.
DIV_ZERO_SENTINEL = "ALKERA_SF_DIV0"

DIVERGENCES = [
    # (id, what we do, what real Snowflake does, what an agent could learn that is false)
    (
        "temp_table_scope",
        "A qualified CREATE TEMPORARY TABLE becomes an unqualified DuckDB temp table; "
        "later references to the qualified name are rewritten back to it, per session.",
        "Snowflake keeps the temp table in the named schema, where it shadows a permanent "
        "table of the same name for this session only.",
        "Two temp tables with the same identifier in *different* schemas collide here and "
        "would not on Snowflake. An agent that relies on schema to disambiguate temp "
        "relations would learn it is safe when it is not.",
    ),
    (
        "warehouse_is_a_name",
        "USE WAREHOUSE sets CURRENT_WAREHOUSE() and is accepted only for a configured name.",
        "Snowflake resumes a suspended warehouse, bills credits, and queues on a busy one.",
        "An agent learns warehouse selection is free and instantaneous. Sizing, suspend/resume "
        "and credits are absent - deliberately, per R004 §5.3 (zero authored archetypes).",
    ),
    (
        "explain_is_refused",
        "EXPLAIN is refused with a Snowflake-shaped unsupported-feature error.",
        "Snowflake returns a query plan.",
        "Nothing false - this is a loud absence. Synthesising a plan would be the wrong fake.",
    ),
    (
        "number_arithmetic_is_float",
        "NUMBER(38,10)/3 still returns a float. Not fixed here (R004 §5.2 item 5).",
        "Snowflake returns fixed-point NUMBER.",
        "Trailing digits of financial totals differ from the real system. Still open.",
    ),
    (
        "query_id_is_synthetic",
        "Query ids are uuid5 over (session, statement ordinal), so they replay identically.",
        "Snowflake ids are globally unique and unordered.",
        "An agent could infer statement ordering from an id. Real ids carry no such signal.",
    ),
    (
        "failed_statement_aborts_the_transaction",
        "Not fixed, and not fixable from here. One failing statement inside BEGIN/COMMIT "
        "puts DuckDB's transaction in an aborted state; every later statement fails with "
        "'Current transaction is aborted (please ROLLBACK)' until a rollback.",
        "Snowflake leaves the transaction open and usable after a statement error; the "
        "session can retry the statement and still commit the rest.",
        "THE MOST DANGEROUS ONE LEFT. An agent learns that a transaction is lost as soon "
        "as any statement in it fails, so it writes defensive retry-the-whole-thing logic "
        "it would not need. Loud, so not silently wrong - but the lesson is false.",
    ),
    (
        "ddl_inside_a_transaction_is_refused",
        "A permanent CREATE / DROP / ALTER / TRUNCATE inside BEGIN..COMMIT is refused with "
        "a Snowflake-shaped unsupported-feature error. Temporary relations are exempt.",
        "Snowflake commits the open transaction before every DDL statement, and the DDL "
        "itself cannot be rolled back.",
        "Nothing false - a loud absence. Unfenced, DuckDB made the DDL transactional: "
        "measured, BEGIN; INSERT; DROP TABLE; ROLLBACK undid the DROP *and* the INSERT, "
        "where Snowflake keeps both. An agent would have learned ROLLBACK protects schema "
        "changes. The gateway's sql_execute refuses DDL inside a multi-statement batch "
        "for the same reason, before any of it reaches here.",
    ),
    (
        "metadata_is_not_persisted",
        "Not fixed. fakesnow attaches its _fs_global catalog as ':memory:' regardless of "
        "db_path (instance.py:32), so table comments, VARCHAR length metadata, users and "
        "stage metadata live only in RAM.",
        "Snowflake keeps all of it in the account.",
        "Measured: after a service restart with the same db_path, rows survive but "
        "VARCHAR(10) reports character_maximum_length NULL and a table comment reads NULL. "
        "Nothing false is taught inside one episode, but a snapshot of db_path does not "
        "restore the service, which is a Track C restore/replay gap, not a fidelity one.",
    ),
]


# ---------------------------------------------------------------------------
# Snowflake-shaped errors
# ---------------------------------------------------------------------------


def _programming_error(msg: str, errno: int, sqlstate: str):
    return snowflake.connector.errors.ProgrammingError(msg=msg, errno=errno, sqlstate=sqlstate)


def unsupported(feature: str):
    """The shape Snowflake uses to refuse a statement it will not run.

    errno/sqlstate are chosen to be Snowflake-shaped and non-retryable; they are not
    verified against a live account (we have none). The load-bearing properties are
    that the client gets HTTP 200 + success:false, a 6-digit code, and a message that
    names the feature - so an agent can attribute the refusal instead of waiting out
    a retry storm.
    """
    return _programming_error(
        f"SQL compilation error:\nUnsupported feature '{feature}'.", 2140, "0A000"
    )


def object_does_not_exist(kind: str, name: str):
    return _programming_error(
        f"SQL compilation error:\nObject does not exist, or operation cannot be performed: "
        f"{kind} '{name}'.",
        2003,
        "02000",
    )


def division_by_zero():
    return _programming_error("Division by zero", 100051, "22012")


# ---------------------------------------------------------------------------
# item 1 - qualified CREATE TEMPORARY TABLE  (R004 §2.2)
# ---------------------------------------------------------------------------


def _temp_names(conn) -> dict[str, str]:
    """(catalog, schema, name) recorded per connection -> the unqualified temp name."""
    names = getattr(conn, "_alkera_temp_names", None)
    if names is None:
        names = {}
        conn._alkera_temp_names = names
    return names


def _key(table: exp.Table, conn) -> str:
    catalog = (table.catalog or (conn.database or "")).upper()
    db = (table.db or (conn.schema or "")).upper()
    return f"{catalog}.{db}.{table.name.upper()}"


def _strip_qualification(table: exp.Table) -> None:
    table.set("catalog", None)
    table.set("db", None)


def temp_table_qualification(expression: exp.Expression, conn) -> exp.Expression:
    """Strip catalog/schema from a TEMPORARY target, and from later references to it.

    DuckDB: ``TEMPORARY table names can *only* use the "temp" catalog``. fakesnow passes
    the qualified name straight through, so dbt's second incremental run dies 100% of the
    time (R004 §2.2). Stripping the CREATE alone is not enough - dbt then reads and drops
    the relation by its fully-qualified name - so the stripped names are remembered for
    the life of the session and rewritten on the way in.
    """
    names = _temp_names(conn)

    # dbt-snowflake's incremental materialization builds its tmp relation as a
    # `create or replace temporary VIEW`, not a table - measured from dbt.log on
    # GT004 run #2. R004 §2.2 isolated the mechanism on CREATE TEMPORARY TABLE, so a
    # fix aimed only at TABLE compiles, passes a hand-written probe, and still leaves
    # dbt incremental broken. Both kinds are handled here.
    if isinstance(expression, exp.Create) and (expression.args.get("kind") or "").upper() in {
        "TABLE",
        "VIEW",
    }:
        props = expression.args.get("properties")
        is_temp = bool(props) and any(
            isinstance(p, (exp.TemporaryProperty, exp.VolatileProperty))
            for p in props.expressions
        )
        if is_temp:
            target = expression.this
            table = target.this if isinstance(target, exp.Schema) else target
            if isinstance(table, exp.Table):
                names[_key(table, conn)] = table.name.upper()
                _strip_qualification(table)
            # deliberately fall through: fakesnow's MERGE transform re-parses itself
            # into `CREATE OR REPLACE TEMPORARY TABLE merge_candidates AS SELECT ...
            # FROM <target> FULL OUTER JOIN <source>` (transforms/merge.py:90), so the
            # statement that *creates* a temp relation is also the statement that
            # *reads* one. Returning here leaves the source qualified and the merge
            # cannot find it.

    if not names:
        return expression

    if isinstance(expression, exp.Drop):
        table = expression.this
        if isinstance(table, exp.Table) and names.pop(_key(table, conn), None):
            _strip_qualification(table)
        return expression

    for table in expression.find_all(exp.Table):
        if (table.catalog or table.db) and _key(table, conn) in names:
            _strip_qualification(table)
    return expression


def qualify_merge_insert_values(expression: exp.Expression) -> exp.Expression:
    """Qualify bare columns in a MERGE's WHEN NOT MATCHED ... VALUES with the source.

    The third blocking defect on the dbt path, again found only by running dbt.
    dbt-snowflake emits

        when not matched then insert ("ORDER_ID", ...) values ("ORDER_ID", ...)

    with the VALUES columns unqualified - legal on Snowflake, where a merge's insert
    values can only come from the source. fakesnow copies those names verbatim into

        CREATE OR REPLACE TEMPORARY TABLE merge_candidates AS
        SELECT "ORDER_ID", ... FROM <target> FULL OUTER JOIN <source> ...

    (transforms/merge.py:90) where every one of them is ambiguous across the join:
    ``Binder Error: Ambiguous reference to column name "ADJUSTMENT_NOTE"``. Naming the
    source explicitly is what Snowflake resolves them to anyway, so this makes
    fakesnow agree with Snowflake rather than papering over a difference.

    This blocks every incremental model with a unique_key - 13 of 41 archetypes carry
    incremental models (R004 §1.3).
    """
    if not isinstance(expression, exp.Merge):
        return expression
    source = expression.args.get("using")
    alias = source.alias_or_name if source is not None else None
    if not alias:
        return expression

    whens = expression.args.get("whens")
    if isinstance(whens, exp.Expression):
        whens = whens.expressions
    for when in whens or []:
        if when.args.get("matched"):
            continue
        then = when.args.get("then")
        values = then.expression if isinstance(then, exp.Insert) else None
        for column in getattr(values, "expressions", []) or []:
            if isinstance(column, exp.Column) and not column.args.get("table"):
                column.set("table", exp.to_identifier(alias, quoted=False))
    return expression


# ---------------------------------------------------------------------------
# item 4 - fences for the silent-wrongs  (R004 §3.2)
# ---------------------------------------------------------------------------


def _has_cluster_by(expression: exp.Expression) -> bool:
    if isinstance(expression, exp.Alter):
        for action in expression.args.get("actions") or []:
            if isinstance(action, exp.Cluster) or type(action).__name__ in {
                "ClusterProperty",
                "ClusteredByProperty",
            }:
                return True
    props = expression.args.get("properties")
    if props is not None:
        for prop in props.expressions:
            if type(prop).__name__ in {"ClusterProperty", "ClusteredByProperty"}:
                return True
    return False


def _is_dynamic_table(expression: exp.Expression) -> bool:
    props = expression.args.get("properties")
    return bool(props) and any(
        type(p).__name__ == "DynamicProperty" for p in props.expressions
    )


def fence(expression: exp.Expression) -> None:
    """Refuse the statements fakesnow reports success for while doing something else.

    Each of these returns 'successfully created' / 'executed successfully' on stock
    fakesnow. The success message is the defect: it teaches an agent that a statement
    it wrote worked.
    """
    if isinstance(expression, exp.Create) and _is_dynamic_table(expression):
        # Stock fakesnow makes an ordinary frozen table and SHOW TABLES calls it a TABLE.
        # Staleness is this product's largest theme; a substrate that plants a stale-data
        # defect is indistinguishable from the stale-data defects we plant as tasks.
        raise unsupported("CREATE DYNAMIC TABLE")
    if _has_cluster_by(expression):
        # alter_table_strip_cluster_by's own docstring calls this a deliberate no-op.
        # Correct for fakesnow's audience, disqualifying for ours: there is no
        # SYSTEM$CLUSTERING_INFORMATION here to contradict it.
        raise unsupported("CLUSTER BY")


def _wrapped_div(node: exp.Expression) -> exp.Expression:
    """a / b  ->  CASE WHEN b = 0 THEN error('...') ELSE a / b END.

    DuckDB returns ``inf`` for 1/0 and NULL for 5 % 0; Snowflake raises 100051. An inf
    or a NULL propagates silently into an aggregate and then into a dashboard, which is
    precisely the failure mode this substrate exists to measure.
    """
    denominator = node.expression
    if isinstance(denominator, exp.Literal) and not denominator.is_string:
        try:
            if float(denominator.this) != 0:
                return node  # a non-zero literal denominator can never divide by zero
        except ValueError:
            pass
    guard = exp.EQ(this=denominator.copy(), expression=exp.Literal.number(0))
    raiser = exp.Anonymous(
        this="error", expressions=[exp.Literal.string(DIV_ZERO_SENTINEL)]
    )
    case = exp.Case(ifs=[exp.If(this=guard, true=raiser)], default=node.copy())
    case.set("_alkera_div_guard", True)
    return case


# Every statement context that evaluates an expression against rows. A SELECT alone
# was not enough: `UPDATE t SET f = f / 0` on a FLOAT column stored inf and reported
# one row updated, `DELETE ... WHERE x % 0 = 1` compared NULL and deleted nothing,
# `INSERT ... VALUES (1 / 0)` stored inf - measured, the stock fence covered none of
# them. Column DEFAULTs and CHECKs in DDL stay out: there is no row to evaluate yet.
_EVALUATING = (exp.Select, exp.Update, exp.Delete, exp.Insert, exp.Merge, exp.Values)


def guard_division(expression: exp.Expression) -> exp.Expression:
    if (
        isinstance(expression, (exp.Div, exp.Mod))
        and expression.find_ancestor(*_EVALUATING) is not None
    ):
        if expression.find_ancestor(exp.Case) is not None and _in_guard(expression):
            return expression
        return _wrapped_div(expression)
    return expression


def _in_guard(node: exp.Expression) -> bool:
    ancestor = node.find_ancestor(exp.Case)
    while ancestor is not None:
        if ancestor.args.get("_alkera_div_guard"):
            return True
        ancestor = ancestor.find_ancestor(exp.Case)
    return False


# ---------------------------------------------------------------------------
# item 5 - session identity  (R004 §3.2 item 6)
# ---------------------------------------------------------------------------

_SHOW_WAREHOUSE_TEMPLATE: str | None = None


def _show_warehouses_sql(current: str | None) -> str:
    global _SHOW_WAREHOUSE_TEMPLATE
    if _SHOW_WAREHOUSE_TEMPLATE is None:
        from fakesnow.transforms.show import SQL_SHOW_WAREHOUSES

        _SHOW_WAREHOUSE_TEMPLATE = SQL_SHOW_WAREHOUSES.strip().rstrip(";")
    rows = []
    for index, name in enumerate(WAREHOUSES):
        row = _SHOW_WAREHOUSE_TEMPLATE.replace("'FAKESNOW_WAREHOUSE'", f"'{name}'")
        if index == 0:
            row = row.replace("'N' as is_default", "'Y' as is_default")
        if current and name == current.upper():
            row = row.replace("'N' as is_current", "'Y' as is_current")
        rows.append(f"SELECT * FROM ({row})")
    return "\nUNION ALL\n".join(rows)


def session_identity(expression: exp.Expression, conn) -> exp.Expression:
    """Answer the 'where am I' questions with values, not DuckDB's opinion.

    Stock fakesnow: CURRENT_ROLE()/CURRENT_USER() are 'duckdb', CURRENT_VERSION() is
    '0.0.0', and CURRENT_ACCOUNT/REGION/WAREHOUSE/SESSION/CLIENT and LAST_QUERY_ID do
    not exist at all - an agent burns turns on our bug rather than on the task.
    """
    warehouse = getattr(conn, "_alkera_warehouse", None)

    if isinstance(expression, exp.Show) and expression.name.upper() == "WAREHOUSES":
        return sqlglot.parse_one(_show_warehouses_sql(warehouse), read="duckdb")

    literal = None
    if isinstance(expression, exp.CurrentAccount):
        literal = ACCOUNT
    elif isinstance(expression, exp.CurrentRegion):
        literal = REGION
    elif isinstance(expression, exp.CurrentRole):
        literal = getattr(conn, "_alkera_role", None) or ROLE
    elif isinstance(expression, exp.CurrentUser):
        literal = getattr(conn, "_alkera_user", None) or "ALKERA"
    elif isinstance(expression, exp.CurrentVersion):
        literal = VERSION
    elif isinstance(expression, exp.CurrentClient):
        literal = getattr(conn, "_alkera_client", None) or "PythonConnector"
    elif isinstance(expression, exp.CurrentSession):
        literal = str(getattr(conn, "_alkera_session_id", 0))
    elif isinstance(expression, exp.CurrentWarehouse):
        if warehouse is None:
            return exp.Null()
        literal = warehouse
    elif isinstance(expression, exp.Anonymous) and expression.name.lower() == "last_query_id":
        last = getattr(conn, "_alkera_last_query_id", None)
        if last is None:
            return exp.Null()
        literal = last

    if literal is None:
        return expression
    return exp.Literal.string(literal)


# ---------------------------------------------------------------------------
# statement router - runs before fakesnow's own execute()
# ---------------------------------------------------------------------------


_ROUTABLE_KEYWORDS = ("USE", "CREATE", "ALTER", "DROP", "TRUNCATE")


def _strip_leading_comments(sql: str) -> str:
    """Drop leading -- and /* */ comments; dbt prefixes statements with both."""
    text = sql.lstrip()
    while True:
        if text.startswith("--"):
            text = text.partition("\n")[2].lstrip()
        elif text.startswith("/*"):
            text = text.partition("*/")[2].lstrip()
        else:
            return text


def _routable(command: str) -> bool:
    """Only USE / CREATE / ALTER / DROP / TRUNCATE can be routed or fenced.

    Without this guard route() parses every statement a second time, which on a
    multi-thousand-row INSERT ... VALUES is the dominant cost of this whole module.
    """
    return _strip_leading_comments(command[:4096])[:8].upper().startswith(_ROUTABLE_KEYWORDS)


def route(command: str, conn) -> str | None:
    """Return a replacement statement, raise a Snowflake-shaped error, or None.

    ``USE WAREHOUSE`` is the single most idiomatic statement in a Snowflake session and
    the first thing an engineer types. On stock fakesnow it is an HTTP 500, which the
    connector retries for the whole network_timeout - measured at 36 s (R004 §2.3).
    """
    if not _routable(command):
        # Only USE / CREATE / ALTER / DROP / TRUNCATE can be routed or fenced. Without
        # this guard every statement is parsed twice, which on a multi-thousand-row
        # INSERT ... VALUES is the dominant cost of this whole module.
        return None
    try:
        expression = sqlglot.parse_one(command, read="snowflake")
    except Exception:  # noqa: BLE001 - let fakesnow produce the parse error
        return None

    use_kind = (
        (expression.args.get("kind") or exp.Var()).name.upper()
        if isinstance(expression, exp.Use)
        else None
    )
    if use_kind == "WAREHOUSE":
        target = expression.this
        name = (target.name if isinstance(target, exp.Table) else str(target)).upper()
        if name not in WAREHOUSES:
            raise object_does_not_exist("Warehouse", name)
        conn._alkera_warehouse = name
        return "SELECT 'Statement executed successfully.' as status"

    if _flag("ALKERA_SNOWFLAKE_FENCES"):
        fence(expression)
        if getattr(conn, "_alkera_in_tx", False) and _commits_implicitly(expression, conn):
            # DIVERGENCES `ddl_inside_a_transaction_is_refused`.
            raise unsupported("DDL inside an explicit transaction")
    return None


def _commits_implicitly(expression: exp.Expression, conn) -> bool:
    """A DDL statement Snowflake would commit the open transaction for.

    Temporary relations are left alone: dbt's own incremental path and fakesnow's
    MERGE transform create and drop them, and they are session-scoped either way.
    """
    if isinstance(expression, exp.Create):
        return not _is_temporary(expression)
    if isinstance(expression, exp.Drop):
        table = expression.this
        if expression.args.get("temporary"):
            return False
        return not (isinstance(table, exp.Table) and _key(table, conn) in _temp_names(conn))
    return isinstance(expression, (exp.Alter, exp.TruncateTable))


# ---------------------------------------------------------------------------
# patching
# ---------------------------------------------------------------------------


def _patch_cursor() -> None:
    from fakesnow.cursor import FakeSnowflakeCursor

    if getattr(FakeSnowflakeCursor, "_alkera_patched", False):
        return

    original_execute = FakeSnowflakeCursor.execute
    original_transform = FakeSnowflakeCursor._transform
    original_explode = FakeSnowflakeCursor._transform_explode
    original_low_execute = FakeSnowflakeCursor._execute

    def execute(self, command, params=None, *args, **kwargs):
        replacement = route(command, self._conn)
        return original_execute(self, replacement or command, params, *args, **kwargs)

    def _transform_explode(self, expression):
        # must run before fakesnow re-parses the MERGE into its own statements
        return original_explode(self, qualify_merge_insert_values(expression))

    def _transform(self, expression, params):
        # Whether the *input* was understood. fakesnow's own transforms legitimately
        # emit Commands (CREATE DATABASE -> ATTACH, USE SCHEMA -> SET), so testing the
        # output alone gives false positives on statements that work perfectly.
        unparsed = _unparsed_command(expression)
        expression = temp_table_qualification(expression, self._conn)
        expression = expression.transform(lambda e: session_identity(e, self._conn), copy=False)
        if _flag("ALKERA_SNOWFLAKE_FENCES"):
            expression = expression.transform(guard_division, copy=False)
        transformed = original_transform(self, expression, params)
        _drop_temp_relation_metadata(transformed)
        _coalesce_merge_counts(transformed)
        if unparsed is not None and transformed.find(exp.Command) is not None:
            _strict_check(unparsed)
        return transformed

    def _execute(self, transformed, params=None):
        conn = self._conn
        if isinstance(transformed, exp.Transaction) and getattr(conn, "_alkera_in_tx", False):
            # Snowflake: "If a transaction is already in progress, BEGIN has no
            # effect." DuckDB raises, and - the part that matters - a raised statement
            # ABORTS the whole DuckDB transaction, so every statement after it fails
            # with "Current transaction is aborted". Catching the error after the fact
            # is therefore not enough; the nested BEGIN must never reach DuckDB.
            # fakesnow already swallows the mirror case, COMMIT/ROLLBACK with no
            # transaction open (cursor.py:482), but not this one.
            _restate_transaction_result(self, transformed)
            _stamp_query_id(self, force=True)
            return
        try:
            original_low_execute(self, transformed, params)
        except duckdb.InvalidInputException as exc:
            if DIV_ZERO_SENTINEL in str(exc):
                raise division_by_zero() from None
            raise
        if isinstance(transformed, exp.Transaction):
            conn._alkera_in_tx = True
        elif isinstance(transformed, (exp.Commit, exp.Rollback)):
            conn._alkera_in_tx = False
        _restate_transaction_result(self, transformed)
        _stamp_query_id(self)

    FakeSnowflakeCursor.execute = execute
    FakeSnowflakeCursor._transform_explode = _transform_explode
    FakeSnowflakeCursor._transform = _transform
    FakeSnowflakeCursor._execute = _execute
    FakeSnowflakeCursor._alkera_patched = True


def _unparsed_command(expression: exp.Expression) -> exp.Command | None:
    """The Command sqlglot produced because it could not parse the client's statement."""
    if isinstance(expression, exp.Command):
        return expression
    return expression.find(exp.Command)


def _strict_check(command: exp.Command) -> None:
    """sqlglot's Command fallback is the free detector for every silent no-op.

    Every silent divergence found in R004 §3.2 is preceded by
    "... contains unsupported syntax. Falling back to parsing as a 'Command'", written
    to stderr where the client never sees it. When such a Command survives the whole
    transform chain we are about to hand DuckDB text we never understood; where DuckDB
    accepts it, the client is told the statement succeeded.
    """
    text = f"{command.name} {command.args.get('expression') or ''}".strip()
    if _flag("ALKERA_SNOWFLAKE_STRICT"):
        raise unsupported(command.name.upper() or "statement")
    logger.warning("unparsed statement handed to duckdb: %s", text[:200])


def _is_temporary(expression: exp.Expression) -> bool:
    props = expression.args.get("properties")
    return bool(props) and any(
        isinstance(p, (exp.TemporaryProperty, exp.VolatileProperty)) for p in props.expressions
    )


def _drop_temp_relation_metadata(transformed: exp.Expression) -> None:
    """Don't record a temp relation's column metadata in the persistent info schema.

    The fourth blocking defect on the dbt path. DuckDB refuses to let one transaction
    write to two attached databases, and fakesnow keeps its column/comment metadata in
    a separate ``_fs_global`` catalog (info_schema.py:301). dbt's incremental merge runs
    inside an explicit BEGIN/COMMIT and fakesnow's own MERGE transform opens it with
    ``CREATE OR REPLACE TEMPORARY TABLE merge_candidates AS SELECT ...``; recording that
    table's VARCHAR lengths writes to _fs_global, and the merge's own writes to the real
    catalog then fail with

        TransactionContext Error: Attempting to write to database "ANALYTICS" in a
        transaction that has already modified database "_fs_global"

    Measured: DuckDB exempts the ``temp`` catalog from that rule, so suppressing only
    the metadata write is enough. It is also the right thing on its own terms - fakesnow
    files a temp relation's metadata under ``<current database>.<schema>.<name>``, a
    relation that does not exist there.

    KNOWN LIMITATION, not fixed: the same collision still fires for a *permanent*
    table created inside a user-opened transaction, e.g.
    ``BEGIN; CREATE TABLE t (s VARCHAR(10)); INSERT INTO u ...; COMMIT;``
    Fixing that needs the _fs_global writes deferred to commit time, which is a change
    inside fakesnow rather than around it.
    """
    if isinstance(transformed, exp.Create) and _is_temporary(transformed):
        transformed.set("text_lengths", None)
        transformed.set("table_comment", None)


def _coalesce_merge_counts(transformed: exp.Expression) -> None:
    """A MERGE that matches nothing must report 0, not NULL.

    The fifth blocking defect, and the commonest case of all: a nightly dbt run where
    no new rows arrived. fakesnow reports a merge's result with
    ``COUNT_IF(merge_op in (...))`` over its merge_candidates table
    (transforms/merge.py:_counts). Measured on DuckDB 1.5.5:

        select count_if(x in (0)) from <empty>   ->  NULL     (DuckDB)
        SELECT COUNT_IF(...) over an empty set   ->  0        (Snowflake)

    so fakesnow then does ``int(sum(None, ...))`` (cursor.py:628) and raises TypeError,
    which in server mode is an HTTP 500 and a 30 s retry storm on the client. Wrapping
    the projections in COALESCE makes the count Snowflake's, and keeps the crash from
    happening at all.
    """
    if not transformed.args.get("merge_counts"):
        return
    for index, projection in enumerate(transformed.expressions):
        alias = projection.args.get("alias") if isinstance(projection, exp.Alias) else None
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        filled = exp.Coalesce(this=inner.copy(), expressions=[exp.Literal.number(0)])
        transformed.expressions[index] = (
            exp.Alias(this=filled, alias=alias) if alias is not None else filled
        )


_STATUS_SQL = "SELECT 'Statement executed successfully.' as status"


def _restate_transaction_result(cursor, transformed) -> None:
    """Give BEGIN/COMMIT/ROLLBACK a describable result.

    A second blocking defect, found only by running dbt (R004 never reached it - the
    temp-relation error fired first). In server mode every statement's columns come
    from ``cursor._describe_last_sql()``, which runs ``DESCRIBE <last sql>``. For a
    transaction statement that is ``DESCRIBE BEGIN``, and DuckDB answers
    ``Catalog Error: Table with name BEGIN does not exist!``.

    dbt-snowflake wraps its incremental merge in an explicit ``begin;``/``commit;``,
    so on stock fakesnow *every* incremental model fails here even once the temp
    relation is fixed. COMMIT appears to work on stock fakesnow only by accident: with
    no transaction open DuckDB raises TransactionException, which fakesnow swallows
    and replaces with a status row. Once BEGIN works, COMMIT hits the same wall.
    """
    if not isinstance(transformed, (exp.Transaction, exp.Commit, exp.Rollback)):
        return
    cursor._duck_conn.execute(_STATUS_SQL)
    cursor._arrow_table = cursor._duck_conn.to_arrow_table()
    cursor._rowcount = cursor._arrow_table.num_rows
    cursor._last_sql = _STATUS_SQL
    cursor._last_params = None
    # server.py:146 asserts cur._last_transformed is set; the short-circuit path below
    # never reaches the code in fakesnow that would have set it.
    cursor._last_transformed = transformed


def _stamp_query_id(cursor, force: bool = False) -> None:
    """Deterministic, ordered query ids so a replay produces the same bytes.

    fakesnow uses uuid4 (cursor.py:630), which no fingerprint can survive.
    """
    conn = cursor._conn
    old = cursor._sfqid
    if not old and not force:
        return
    ordinal = getattr(conn, "_alkera_statement_no", 0) + 1
    conn._alkera_statement_no = ordinal
    session_id = getattr(conn, "_alkera_session_id", 0)
    new = str(uuid.uuid5(QUERY_ID_NS, f"{session_id}:{ordinal}"))
    cache = getattr(conn, "results_cache", None)
    if cache is not None and old in cache:
        del cache[old]
        # rebuild rather than move the tuple: _restate_transaction_result may have
        # replaced the cursor's result since fakesnow cached it.
        cache[new] = (
            cursor._arrow_table,
            cursor._rowcount,
            cursor._last_sql,
            cursor._last_params,
            cursor._last_transformed,
        )
    cursor._sfqid = new
    conn._alkera_last_query_id = new


# ---------------------------------------------------------------------------
# server - item 2, the retry storm  (R004 §2.3)
# ---------------------------------------------------------------------------


def _wrap_query_request(original):
    from starlette.responses import JSONResponse

    async def query_request(request):
        response = await original(request)
        if response.status_code < 500:
            return response
        # fakesnow raises ServerError(500) for anything it did not anticipate
        # (server.py:170-176). The Snowflake connector treats 500 as retryable and burns
        # the full network_timeout before failing - measured at 28-36 s per statement,
        # with no error the agent can attribute. A SQL-compilation shape is both
        # non-retryable and attributable.
        try:
            body = json.loads(bytes(response.body))
        except Exception:  # noqa: BLE001
            body = {}
        logger.warning(
            "converted fakesnow 500 to a Snowflake-shaped error: %s",
            str(body.get("message") or "")[:400],
        )
        # Starlette caches the body, so re-reading it here costs nothing and lets the
        # refusal name the statement rather than being anonymous.
        feature = "statement"
        try:
            raw = await request.body()
            if request.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            sql_text = json.loads(raw)["sqlText"]
            feature = " ".join(str(sql_text).split()[:2]).upper()
        except Exception:  # noqa: BLE001
            pass
        error = unsupported(feature)
        return JSONResponse(
            {
                "data": {"errorCode": "002140", "sqlState": "0A000"},
                "code": "002140",
                "message": error.msg,
                "success": False,
            }
        )

    return query_request


def _wrap_login_request(original, server_module):
    """Capture who the client says it is, so the identity functions can answer."""
    from starlette.responses import JSONResponse

    async def login_request(request):
        response = await original(request)
        try:
            body = json.loads(bytes(response.body))
            token = body["data"]["token"]
            conn = server_module.sessions[token]
        except Exception:  # noqa: BLE001
            return response
        server_module._alkera_sessions = getattr(server_module, "_alkera_sessions", 0) + 1
        conn._alkera_session_id = 1000000000000 + server_module._alkera_sessions
        conn._alkera_statement_no = 0
        warehouse = request.query_params.get("warehouse")
        conn._alkera_warehouse = (warehouse or WAREHOUSES[0]).upper() if WAREHOUSES else None
        role = request.query_params.get("roleName")
        conn._alkera_role = (role or ROLE).upper()
        try:
            payload: dict[str, Any] = json.loads(await request.body())["data"]
            conn._alkera_user = str(payload.get("LOGIN_NAME") or "ALKERA").upper()
            app = payload.get("CLIENT_APP_ID") or "PythonConnector"
            conn._alkera_client = f"{app} {payload.get('CLIENT_APP_VERSION') or ''}".strip()
        except Exception:  # noqa: BLE001
            pass
        assert isinstance(response, JSONResponse)
        return response

    return login_request


def apply(server_module):
    """Patch fakesnow in place and return a Starlette app with the wrapped routes."""
    from starlette.applications import Starlette
    from starlette.routing import Route

    _patch_cursor()

    routes = []
    for route_ in server_module.routes:
        endpoint = route_.endpoint
        if route_.path == "/queries/v1/query-request":
            endpoint = _wrap_query_request(endpoint)
        elif route_.path == "/session/v1/login-request":
            endpoint = _wrap_login_request(endpoint, server_module)
        routes.append(Route(route_.path, endpoint, methods=list(route_.methods - {"HEAD"})))

    logger.info(
        "alkera snowflake fidelity: strict=%s fences=%s warehouses=%s",
        _flag("ALKERA_SNOWFLAKE_STRICT"),
        _flag("ALKERA_SNOWFLAKE_FENCES"),
        WAREHOUSES,
    )
    return Starlette(debug=False, routes=routes)
