"""The second review's probes of the Databricks lakehouse, kept as tests.

The reviewer's fixture: one table `warehouse.t` (INT, INT, STRING, DECIMAL(18,2), DATE,
TIMESTAMP, DOUBLE, with NULLs) and `warehouse.w` (tab, newline and backslash text), a
session in schema `warehouse` on the world's clock.

REACH: nothing a statement says gets to the engine's own catalogs, settings, files or
functions - in particular a CTE name exempts only the references IN ITS OWN SCOPE from
qualification, so `FROM pragma_database_list WHERE 1 IN (WITH pragma_database_list AS
...)` reads the session's schema (and fails: no such table), never DuckDB's view.

SEMANTICS: every probe answered is answered as Databricks answers it - the value as the
JSON_ARRAY format carries it, and the type the manifest names - and every probe not
answered is refused naming what was refused.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb
import pytest

from alkera_gateway import databricks_sql as dbx
from alkera_gateway.databricks_sql import Context, Lakehouse, SqlError, Unsupported

CONTEXT = Context(schema="warehouse", now="2026-10-07T12:00:00Z")


def probe_world(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    world = root / "warehouse.duckdb"
    with duckdb.connect(str(world)) as con:
        con.execute("CREATE SCHEMA warehouse")
        con.execute("CREATE TABLE warehouse.warehouse.t (id INT, x INT, s VARCHAR, "
                    "d DECIMAL(18,2), dt DATE, ts TIMESTAMP, f DOUBLE)")
        con.execute("""INSERT INTO warehouse.warehouse.t VALUES
          (1, 3, 'b', 120.50, DATE '2024-01-31', TIMESTAMP '2024-01-31 10:00:00', 2.675),
          (2, NULL, NULL, 80.00, DATE '2024-02-29', TIMESTAMP '2024-02-29 23:59:59', 1.005),
          (3, 1, 'a', 45.25, DATE '2024-03-01', TIMESTAMP '2024-03-01 00:00:00', NULL),
          (4, 2, 'c', 300.10, NULL, NULL, 0.125)""")
        con.execute("CREATE TABLE warehouse.warehouse.w AS SELECT chr(9) || ' a ' || chr(10) "
                    "AS s, 'a' || chr(92) || '_b' AS v, 'a' || chr(92) || '_b' AS p")
    return world


@pytest.fixture
def world(tmp_path) -> Path:
    return probe_world(tmp_path / "world")


@pytest.fixture
def lake(world, tmp_path) -> Lakehouse:
    path = tmp_path / "state" / "lakehouse.duckdb"
    Lakehouse.seed(world, path)
    lakehouse = Lakehouse(path)
    yield lakehouse
    lakehouse.close()


@pytest.fixture(scope="module")
def shared(tmp_path_factory) -> Lakehouse:
    """One lakehouse for the probes that only read."""
    root = tmp_path_factory.mktemp("probes")
    path = root / "state" / "lakehouse.duckdb"
    Lakehouse.seed(probe_world(root / "world"), path)
    lakehouse = Lakehouse(path)
    yield lakehouse
    lakehouse.close()


def answer(lake: Lakehouse, sql: str) -> tuple[list[tuple], list[str]]:
    result, _ = lake.execute(sql, CONTEXT)
    rows = [tuple(dbx.render(v, c) for v, c in zip(row, result.columns, strict=True))
            for row in result.rows]
    return rows, [c.type_text for c in result.columns]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------------ reach
#: A CTE name is visible in its own scope only: each of these names a DuckDB view or
#: table function OUTSIDE the WITH that shadows it, so it is the session's schema's
#: (missing) table - before the fix they read the engine's catalog.
CTE_SCOPE = [
    "SELECT * FROM pragma_database_list WHERE 1 IN (WITH pragma_database_list AS "
    "(SELECT 1 AS a) SELECT a FROM pragma_database_list)",
    "SELECT database_name, path FROM duckdb_databases, (WITH duckdb_databases AS "
    "(SELECT 1 AS a) SELECT a FROM duckdb_databases) s",
    "SELECT name, value FROM duckdb_settings WHERE EXISTS (WITH duckdb_settings AS "
    "(SELECT 1) SELECT 1 FROM duckdb_settings) AND name = 'enable_external_access'",
    "SELECT name, value FROM duckdb_settings, (WITH duckdb_settings AS (SELECT 1 AS a) "
    "SELECT a FROM duckdb_settings) s WHERE name IN ('home_directory', 'temp_directory')",
    "SELECT view_name FROM duckdb_views, (WITH duckdb_views AS (SELECT 1 AS a) "
    "SELECT a FROM duckdb_views) s WHERE internal LIMIT 3",
    "SELECT sql FROM duckdb_views, (WITH duckdb_views AS (SELECT 1 AS a) "
    "SELECT a FROM duckdb_views) s WHERE NOT internal LIMIT 1",
    "SELECT * FROM duckdb_secrets, (WITH duckdb_secrets AS (SELECT 1 AS a) "
    "SELECT a FROM duckdb_secrets) s",
    "CREATE TABLE leak AS SELECT database_name, path FROM duckdb_databases, "
    "(WITH duckdb_databases AS (SELECT 1 AS a) SELECT a FROM duckdb_databases) s",
    "SELECT 1 FROM t WHERE id = (SELECT 1 FROM duckdb_tables, (WITH duckdb_tables AS "
    "(SELECT 1) SELECT 1 FROM duckdb_tables) q LIMIT 1)",
    "WITH x AS (SELECT * FROM pragma_database_list) SELECT * FROM x",
    # a CTE's own body does not see the CTE (not RECURSIVE)
    "WITH duckdb_settings AS (SELECT * FROM duckdb_settings) SELECT * FROM duckdb_settings",
    "SELECT * FROM duckdb_databases", "SELECT * FROM duckdb_settings",
    "SELECT * FROM pragma_database_list",
    "CREATE VIEW v2 AS SELECT * FROM duckdb_databases",
]


@pytest.mark.parametrize("sql", CTE_SCOPE)
def test_a_cte_shadows_names_only_in_its_own_scope(lake, world, sql):
    before = digest(world)
    with pytest.raises(SqlError) as failed:
        lake.execute(sql, CONTEXT)
    assert failed.value.error_class == "TABLE_OR_VIEW_NOT_FOUND", sql
    assert digest(world) == before


def test_a_cte_still_answers_in_its_own_scope(shared):
    assert answer(shared, "WITH duckdb_databases AS (SELECT 1 AS a) "
                          "SELECT * FROM duckdb_databases") == ([("1",)], ["int"])
    assert answer(shared, "SELECT a FROM (WITH c AS (SELECT 2 AS a) SELECT a FROM c)") == \
        ([("2",)], ["int"])
    # visible in the subqueries of its scope, too
    assert answer(shared, "WITH c AS (SELECT id FROM t) SELECT count(*) FROM c "
                          "WHERE id IN (SELECT id FROM c)") == ([("4",)], ["bigint"])


#: Statements that must not reach the engine: refused by name, or failed the way
#: Databricks fails them (no such table, read-only information_schema, parse error).
REFUSED = [
    "select * from read_csv_auto('/etc/passwd')", "select * from '/etc/passwd'",
    "select * from 'x.parquet'", "SELECT * FROM `/etc/passwd`",
    "SELECT * FROM csv.`/etc/passwd`", "SELECT * FROM parquet.`/tmp/x.parquet`",
    "FROM duckdb_secrets()", "SELECT * FROM duckdb_secrets()",
    "SELECT * FROM memory.dbx_information_schema.tables", "SELECT * FROM memory.x",
    "SELECT * FROM temp.main.x", "SELECT * FROM system.main.duckdb_settings",
    "SELECT * FROM dbx_main.warehouse.t", "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM range(3)", "SELECT * FROM query('select 1')",
    "SELECT * FROM query_table('t')", "COPY t TO '/tmp/x.csv'",
    "COPY (SELECT 1) TO '/tmp/x.csv'", "COPY INTO t FROM '/tmp/x'",
    "ATTACH '/tmp/x.db' AS x", "DETACH dbx_main", "PRAGMA database_list",
    "PRAGMA enable_profiling", "CALL pragma_database_list()", "CALL duckdb_settings()",
    "EXPORT DATABASE '/tmp/x'", "IMPORT DATABASE '/tmp/x'", "LOAD httpfs",
    "INSTALL httpfs", "CHECKPOINT", "SUMMARIZE t", "DESCRIBE SELECT 1",
    "SELECT getvariable('x')", "SET VARIABLE x = 1",
    "CREATE SECRET s (TYPE S3, KEY_ID 'a', SECRET 'b')",
    "CREATE MACRO dbx_divisor(b) AS b", "CREATE OR REPLACE TEMP MACRO dbx_divisor(b) AS b",
    "CREATE FUNCTION f(x INT) RETURNS INT RETURN x + 1",
    "CREATE TEMPORARY FUNCTION f AS 'x.y'", "CREATE TEMP VIEW v AS SELECT 1 AS a",
    "CREATE SEQUENCE seq", "CREATE INDEX i ON t (id)", "CREATE TYPE mood AS ENUM ('a')",
    "CREATE TABLE z AS SELECT * FROM read_parquet('/tmp/x')",
    "INSERT INTO information_schema.tables VALUES (1)",
    "INSERT INTO system.information_schema.tables SELECT * FROM "
    "system.information_schema.tables",
    "UPDATE main.information_schema.tables SET table_name = 'x'",
    "DELETE FROM information_schema.columns", "DROP VIEW system.information_schema.tables",
    "DROP TABLE information_schema.tables",
    "MERGE INTO information_schema.tables a USING t b ON a.table_name = b.s "
    "WHEN MATCHED THEN DELETE",
    "CREATE TABLE information_schema.evil (a INT)",
    "TRUNCATE TABLE information_schema.tables",
    "ALTER TABLE information_schema.tables RENAME TO x", "ALTER TABLE t RENAME TO t2",
    "ALTER TABLE t ADD COLUMN q INT", "COMMENT ON TABLE information_schema.tables IS 'x'",
    "CREATE SCHEMA memory.evil", "CREATE SCHEMA temp.evil",
    "DROP SCHEMA memory.dbx_information_schema CASCADE",
    "SELECT * FROM IDENTIFIER('duckdb_databases')", "SELECT * FROM IDENTIFIER('t')",
    "CREATE FUNCTION f(x) AS x + 1", "CREATE TEMPORARY FUNCTION trunc(x) AS 1",
    "CREATE FUNCTION trunc AS 'x'", "CREATE OR REPLACE FUNCTION g() RETURNS INT RETURN 1",
    "DROP SCHEMA information_schema", "CREATE SCHEMA main",
    "DROP SCHEMA memory.dbx_information_schema", "DROP SCHEMA dbx_main.warehouse CASCADE",
    "DROP FUNCTION dbx_divisor", "DROP MACRO dbx_divisor", "DROP SEQUENCE seq",
    "TRUNCATE TABLE memory.dbx_information_schema.tables",
]


@pytest.mark.parametrize("sql", REFUSED)
def test_no_probe_reaches_the_engine(lake, world, sql):
    before = digest(world)
    with pytest.raises((Unsupported, SqlError)):
        lake.execute(sql, CONTEXT)
    assert digest(world) == before


def test_after_every_probe_the_engine_is_as_locked_as_before(lake, world):
    before = digest(world)
    for sql in REFUSED + CTE_SCOPE:
        try:
            lake.execute(sql, CONTEXT)
        except (Unsupported, SqlError):
            pass
    # the settings a SET would change are echoed, never applied
    assert answer(lake, "SET enable_external_access = true")[0] == \
        [("enable_external_access", "true")]
    lake.execute("RESET ALL", CONTEXT)
    con = lake.con
    assert con.execute("SELECT current_setting('enable_external_access')").fetchone()[0] \
        is False
    assert con.execute("SELECT current_setting('lock_configuration')").fetchone()[0] is True
    assert con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name IN "
                       "('leak', 'z', 'evil')").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM duckdb_sequences()").fetchone()[0] == 0
    assert digest(world) == before
    # and the catalog still answers
    assert answer(lake, "SELECT count(*) FROM information_schema.tables")[0] == [("2",)]


def test_create_schema_keeps_the_name_it_was_given(lake):
    """`CREATE SCHEMA x` makes schema x of catalog main - never a rename onto the
    engine's catalog name (the first review's fix renamed `main` to it)."""
    lake.execute("CREATE SCHEMA hive_metastore", CONTEXT)
    lake.execute("CREATE SCHEMA main.reporting", CONTEXT)
    rows, _ = answer(lake, "SELECT schema_name FROM information_schema.schemata")
    assert rows == [("default",), ("hive_metastore",), ("information_schema",),
                    ("reporting",), ("warehouse",)]
    lake.execute("CREATE TABLE reporting.r AS SELECT 1 AS a", CONTEXT)
    assert answer(lake, "SELECT a FROM main.reporting.r") == ([("1",)], ["int"])
    lake.execute("DROP SCHEMA reporting CASCADE", CONTEXT)
    with pytest.raises(Unsupported, match="schema name `main`"):
        lake.execute("CREATE SCHEMA main", CONTEXT)


def test_truncate_writes_and_is_refused_on_the_catalog(lake):
    lake.execute("CREATE TABLE default.tr AS SELECT 1 AS a", CONTEXT)
    lake.execute("TRUNCATE TABLE default.tr", CONTEXT)
    assert answer(lake, "SELECT count(*) FROM default.tr")[0] == [("0",)]
    with pytest.raises(SqlError) as failed:
        lake.execute("TRUNCATE TABLE information_schema.tables", CONTEXT)
    assert failed.value.error_class == "INSUFFICIENT_PERMISSIONS"


# -------------------------------------------------------------------- semantics
#: (statement, rows as JSON_ARRAY renders them, column types) - what Databricks returns.
ANSWERED = [
    ("SELECT round(2.5), round(-2.5), round(0.125, 2)",
     [("3", "-3", "0.13")], ["decimal(2,0)", "decimal(2,0)", "decimal(3,2)"]),
    ("SELECT round(f, 2) FROM t WHERE id IN (1, 2) ORDER BY id",
     [("2.68",), ("1.01",)], ["double"]),
    ("SELECT round(cast(2.675 as double), 2), round(cast(1.005 as double), 2)",
     [("2.68", "1.01")], ["double", "double"]),
    ("SELECT 7/2, 7 div 2, -7 div 2, -7 % 2, 7 % -2",
     [("3.5", "3", "-3", "-1", "1")], ["double", "bigint", "bigint", "int", "int"]),
    ("SELECT 7.5 div 2", [("3",)], ["bigint"]),
    ("SELECT 1.0/3, 1/3", [("0.333333", "0.3333333333333333")], ["decimal(7,6)", "double"]),
    ("SELECT sum(d)/count(*) FROM t", [("136.462500000000",)], ["decimal(38,12)"]),
    ("SELECT avg(d), avg(x), sum(d) FROM t", [("136.462500", "2.0", "545.85")],
     ["decimal(22,6)", "double", "decimal(28,2)"]),
    ("SELECT try_cast('2024-02-30' as date)", [(None,)], ["date"]),
    ("SELECT 'a' || NULL, concat('a', NULL), concat_ws('-', 'a', NULL, 'b')",
     [(None, None, "a-b")], ["string", "string", "string"]),
    ("SELECT concat(s, 'x') FROM t ORDER BY id", [("bx",), (None,), ("ax",), ("cx",)],
     ["string"]),
    ("SELECT date_trunc('week', DATE'2024-01-03'), last_day(DATE'2024-02-10')",
     [("2024-01-01T00:00:00Z", "2024-02-29")], ["timestamp", "date"]),
    ("SELECT cast(date_trunc('MONTH', ts) as string) FROM t WHERE id = 1",
     [("2024-01-01 00:00:00",)], ["string"]),
    ("SELECT concat('m=', date_trunc('MONTH', ts)) FROM t WHERE id = 1",
     [("m=2024-01-01 00:00:00",)], ["string"]),
    ("SELECT cast(timestampadd(DAY, 1, ts) as string) FROM t WHERE id = 1",
     [("2024-02-01 10:00:00",)], ["string"]),
    ("SELECT cast(current_timestamp() as string)", [("2026-10-07 12:00:00",)], ["string"]),
    ("SELECT nvl(1, 2.5), coalesce(x, 1.5) FROM t ORDER BY id",
     [("1.0", "3.0"), ("1.0", "1.5"), ("1.0", "1.0"), ("1.0", "2.0")],
     ["decimal(11,1)", "decimal(11,1)"]),
    ("SELECT 'a_c' LIKE 'a_c', 'abc' LIKE 'a_c', 'ABC' ILIKE 'a%'",
     [("true", "true", "true")], ["boolean", "boolean", "boolean"]),
    ("SELECT 'abc' rlike 'b', 'abc' rlike '^b'", [("true", "false")], ["boolean", "boolean"]),
    ("SELECT size(array(1,2,3)), size(NULL)", [("3", None)], ["int", "int"]),
    ("SELECT array_contains(array(1, 2, NULL), 3), array_contains(array(1,2), 2)",
     [(None, "true")], ["boolean", "boolean"]),
    ("SELECT collect_list(x) FROM t", [("[3,1,2]",)], ["array<int>"]),
    ("SELECT count(distinct x, s) FROM t", [("3",)], ["bigint"]),
    ("SELECT percentile(x, 0.5), median(x) FROM t", [("2.0", "2.0")], ["double", "double"]),
    ("SELECT id, sum(x) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) "
     "FROM t ORDER BY id", [("1", "3"), ("2", "3"), ("3", "1"), ("4", "3")],
     ["int", "bigint"]),
    ("SELECT id FROM t QUALIFY row_number() OVER (ORDER BY id DESC) = 1", [("4",)], ["int"]),
    ("SELECT '12'::int, 1.9::int, -1.9::int", [("12", "1", "-1")], ["int", "int", "int"]),
    ("SELECT x FROM t ORDER BY x", [(None,), ("1",), ("2",), ("3",)], ["int"]),
    ("SELECT x FROM t ORDER BY x DESC", [("3",), ("2",), ("1",), (None,)], ["int"]),
    ("SELECT id, row_number() OVER (ORDER BY x) FROM t ORDER BY id",
     [("1", "4"), ("2", "1"), ("3", "2"), ("4", "3")], ["int", "int"]),
    ("SELECT collect_list(x) OVER (ORDER BY x) FROM t",
     [("[]",), ("[1]",), ("[1,2]",), ("[1,2,3]",)], ["array<int>"]),
    ("SELECT d div 10 FROM t ORDER BY id", [("12",), ("8",), ("4",), ("30",)], ["bigint"]),
    ("SELECT 7.5 div 2, cast(7.5 as double) div 2", [("3", "3")], ["bigint", "bigint"]),
    ("SELECT collect_list(x) FROM t WHERE id > 99", [("[]",)], ["array<int>"]),
    ("SELECT collect_set(x) FROM t WHERE id > 99", [("[]",)], ["array<int>"]),
    ("SELECT array_contains(array(1,2,3), NULL)", [(None,)], ["boolean"]),
    ("SELECT avg(v) FROM VALUES (0.10), (0.20), (0.20) AS v(v)", [("0.166667",)],
     ["decimal(6,6)"]),
    ("SELECT sum(v) FROM VALUES (0.10), (0.20), (0.20) AS v(v)", [("0.50",)],
     ["decimal(12,2)"]),
    ("SELECT cast(0.9999999999999999999 as int)", [("0",)], ["int"]),
    ("SELECT cast(true as int)", [("1",)], ["int"]),
    ("SELECT cast(1.1 as float), cast(cast(1.1 as float) as double)",
     [("1.1", "1.100000023841858")], ["float", "double"]),
    ("SELECT cast(1e20 as string), cast(cast(1e-7 as double) as string), "
     "cast(cast(1.0 as double) as string)", [("1.0E20", "1.0E-7", "1.0")],
     ["string", "string", "string"]),
    ("SELECT cast(cast(2.675 as double) as decimal(4,2)), "
     "cast(cast(1.005 as double) as decimal(4,2))", [("2.68", "1.01")],
     ["decimal(4,2)", "decimal(4,2)"]),
    ("SELECT log(10), log(2, 8), ln(10)", [("2.302585092994046", "3.0", "2.302585092994046")],
     ["double", "double", "double"]),
    ("SELECT sign(-5), ceil(1.2), floor(-1.5), ceil(cast(1.2 as double))",
     [("-1.0", "2", "-2", "2")], ["double", "decimal(2,0)", "decimal(2,0)", "bigint"]),
    ("SELECT initcap('hello-world aBC')", [("Hello-world Abc",)], ["string"]),
    ("SELECT lpad('abc', 2, 'x'), lpad('abc', 5), lpad('abc', 5, ''), rpad('abc', 5, 'xy')",
     [("ab", "  abc", "abc", "abcxy")], ["string", "string", "string", "string"]),
    ("SELECT replace('abc', 'b')", [("ac",)], ["string"]),
    ("SELECT regexp_replace('abcb', '(b)', '[$1]')", [("a[b]c[b]",)], ["string"]),
    ("SELECT regexp_replace('abcb', 'b', 'X')", [("aXcX",)], ["string"]),
    ("SELECT regexp_extract('xaby', 'a(b)'), regexp_extract('xaby', 'a(b)', 0), "
     "regexp_extract('xyz', 'a(b)', 1)", [("b", "ab", "")], ["string", "string", "string"]),
    ("SELECT add_months(DATE'2024-01-31', 1), add_months(TIMESTAMP'2024-01-31 10:00:00', 1)",
     [("2024-02-29", "2024-02-29")], ["date", "date"]),
    ("SELECT add_months(ts, 1) FROM t WHERE id = 1", [("2024-02-29",)], ["date"]),
    ("SELECT date_add(DATE'2024-01-31', 1), date_sub('2024-03-01', 1), "
     "datediff('2024-03-01', '2024-02-01')", [("2024-02-01", "2024-02-29", "29")],
     ["date", "date", "int"]),
    ("SELECT timestampdiff(MONTH, DATE'2024-01-31', DATE'2024-02-29'), "
     "timestampdiff(YEAR, DATE'2023-02-28', DATE'2024-02-28')", [("0", "1")],
     ["bigint", "bigint"]),
    ("SELECT dayofweek(DATE'2024-01-07'), weekday(DATE'2024-01-07'), "
     "extract(DOW FROM DATE'2024-01-07')", [("1", "6", "1")], ["int", "int", "int"]),
    ("SELECT weekofyear(DATE'2024-12-30'), dayofyear(DATE'2024-12-31'), "
     "quarter(DATE'2024-12-31')", [("1", "366", "4")], ["int", "int", "int"]),
    ("SELECT substring('Spark SQL', 0, 3), substring('Spark SQL', -3), "
     "substr('Spark SQL', 5, 1), left('abc', -1), right('abc', 5)",
     [("Spa", "SQL", "k", "", "abc")], ["string"] * 5),
    ("SELECT greatest(1, NULL, 3), least(NULL, 2), nullif(1, 1), nvl2(NULL, 1, 2), "
     "if(NULL, 1, 2)", [("3", "2", None, "2", "2")], ["int"] * 5),
    ("SELECT 1 IN (2, NULL), NOT (1 IN (2, NULL))", [(None, None)], ["boolean", "boolean"]),
    ("SELECT DATE'2024-01-01' + 1", [("2024-01-02",)], ["date"]),
    ("SELECT stddev(x), variance(x), stddev_pop(x), var_pop(x) FROM t",
     [("1.0", "1.0", "0.816496580927726", "0.6666666666666666")], ["double"] * 4),
    ("SELECT stddev(x) FROM t WHERE id = 1", [(None,)], ["double"]),
    ("SELECT ntile(3) OVER (ORDER BY id), percent_rank() OVER (ORDER BY x), "
     "cume_dist() OVER (ORDER BY x) FROM t ORDER BY id",
     [("1", "1.0", "1.0"), ("1", "0.0", "0.25"), ("2", "0.3333333333333333", "0.5"),
      ("3", "0.6666666666666666", "0.75")], ["int", "double", "double"]),
    ("SELECT pow(2, 3), sqrt(4), exp(0), abs(-2.5)", [("8.0", "2.0", "1.0", "2.5")],
     ["double", "double", "double", "decimal(2,1)"]),
    ("SELECT length('abc  '), trim('  a  '), ltrim('  a'), rtrim('a  '), "
     "trim(BOTH 'x' FROM 'xxaxx')", [("5", "a", "a", "a", "a")],
     ["int", "string", "string", "string", "string"]),
    ("SELECT upper('ß'), lower('İ')", [("SS", "i̇")], ["string", "string"]),
    ("SELECT 'it''s'", [("its",)], ["string"]),
    ("SELECT x <=> NULL, x IS DISTINCT FROM NULL FROM t ORDER BY id",
     [("false", "true"), ("true", "false"), ("false", "true"), ("false", "true")],
     ["boolean", "boolean"]),
    ("SELECT x / 2 FROM t WHERE id = 1", [("1.5",)], ["double"]),
    ("SELECT cast(NULL as int) / 0", [(None,)], ["double"]),
    ("SELECT sum(x) / 0 FROM t WHERE id > 99", [(None,)], ["double"]),
    ("SELECT array_contains(array(s, 'z'), 'q') FROM t ORDER BY id",
     [("false",), (None,), ("false",), ("false",)], ["boolean"]),
    ("SELECT cast(d as int), cast(f as int), cast(-f as bigint) FROM t ORDER BY id",
     [("120", "2", "-2"), ("80", "1", "-1"), ("45", None, None), ("300", "0", "0")],
     ["int", "int", "bigint"]),
    ("SELECT cast('yes' as boolean), cast(' true ' as boolean)", [("true", "true")],
     ["boolean", "boolean"]),
    ("SELECT round(avg(d), 2), round(sum(d) / count(d), 2) FROM t",
     [("136.46", "136.46")], ["decimal(19,2)", "decimal(29,2)"]),
    ("SELECT d * 1.1 FROM t WHERE id = 1", [("132.550",)], ["decimal(21,3)"]),
    ("SELECT sum(f), avg(f) FROM t", [("3.8049999999999997", "1.2683333333333333")],
     ["double", "double"]),
    ("SELECT length(s), length(trim(s)) FROM w", [("5", "5")], ["int", "int"]),
    # `\` escapes the next _ in a LIKE pattern read from a column, as in Spark
    ("SELECT v LIKE p, v LIKE 'a%' FROM w", [("false", "true")], ["boolean", "boolean"]),
    ("SELECT concat_ws(',', array('a', 'b'))", [("a,b",)], ["string"]),
    ("SELECT instr('abc', ''), instr(NULL, 'a')", [("1", None)], ["int", "int"]),
    ("SELECT round(25, -1), round(cast(25 as double), -1), round(1.25, 1), round(-1.25, 1)",
     [("30", "30.0", "1.3", "-1.3")], ["int", "double", "decimal(3,1)", "decimal(3,1)"]),
    ("SELECT cast(ts as date), cast(dt as timestamp) FROM t WHERE id = 1",
     [("2024-01-31", "2024-01-31T00:00:00Z")], ["date", "timestamp"]),
    ("SELECT cast(cast(1e7 as double) as string), cast(cast(100.0 as double) as string)",
     [("1.0E7", "100.0")], ["string", "string"]),
    ("SELECT extract(WEEK FROM DATE'2024-12-30'), extract(QUARTER FROM DATE'2024-12-30'), "
     "extract(DOY FROM DATE'2024-12-30')", [("1", "4", "365")], ["int", "int", "int"]),
    ("SELECT concat_ws(',', collect_list(s)) FROM t", [("b,a,c",)], ["string"]),
    ("SELECT concat_ws(',', 'Spark', array('S', 'Q', NULL, 'L'), NULL)",
     [("Spark,S,Q,L",)], ["string"]),
    ("SELECT cast(cast(dt as timestamp) as string), concat('at ', cast(dt as timestamp)) "
     "FROM t WHERE id = 1", [("2024-01-31 00:00:00", "at 2024-01-31 00:00:00")],
     ["string", "string"]),
    ("SELECT cast(ts as string) FROM t WHERE id = 1", [("2024-01-31 10:00:00",)], ["string"]),
    ("SELECT size(collect_list(x)) FROM t WHERE id > 99", [("0",)], ["int"]),
    ("SELECT avg(d) FROM t WHERE id IN (1, 3)", [("82.875000",)], ["decimal(22,6)"]),
    ("SELECT sum(d) / 3 FROM t WHERE id IN (1, 3)", [("55.250000",)], ["decimal(32,6)"]),
    ("SELECT d / 3 FROM t WHERE id = 1", [("40.166667",)], ["decimal(22,6)"]),
    ("SELECT -5.9 div 1, 3 div 2", [("-5", "1")], ["bigint", "bigint"]),
    ("SELECT x div 2 FROM t WHERE id = 1", [("1",)], ["bigint"]),
]


@pytest.mark.parametrize("sql,rows,types", ANSWERED)
def test_probes_answer_as_databricks_does(shared, sql, rows, types):
    assert answer(shared, sql) == (rows, types), sql


#: (statement, the name its refusal carries)
NOT_ANSWERED = [
    ("SELECT months_between(DATE'2024-03-31', DATE'2024-02-29')", "months_between()"),
    ("SELECT split('a,b', ',')", "split()"),
    ("SELECT percentile_approx(x, 0.5) FROM t", "percentile_approx()"),
    ("SELECT locate('b', 'abc', 3)", "instr/locate with position"),
    ("SELECT try_divide(1, 0)", "try_divide()"),
    ("SELECT unix_timestamp()", "unix_timestamp()"),
    ("SELECT array(1, 2)[0]", "array/map subscript"),
    ("SELECT bround(2.5)", "bround()"),
    ("SELECT cast(x as decimal) FROM t", "DECIMAL without a precision"),
    ("SELECT 'ab' LIKE 'a!_' ESCAPE '!'", "Escape"),
    ("SELECT DATE'2024-03-01' - DATE'2024-02-01'", "subtracting DATEs or TIMESTAMPs"),
    ("SELECT TIMESTAMP'2024-03-01 00:00:00' - TIMESTAMP'2024-02-01 00:00:00'",
     "subtracting DATEs or TIMESTAMPs"),
    ("SELECT regexp_extract('abc', 'b')", "regexp_extract group index 1"),
]


@pytest.mark.parametrize("sql,name", NOT_ANSWERED)
def test_probes_not_answered_are_refused_by_name(shared, sql, name):
    with pytest.raises(Unsupported) as refused:
        shared.execute(sql, CONTEXT)
    assert name in refused.value.feature, (sql, refused.value.feature)


@pytest.mark.parametrize("sql,error_class", [
    ("SELECT cast('2024-02-30' as date)", "CAST_INVALID_INPUT"),
    ("SELECT cast('1.9' as int), cast(' 12 ' as int)", "CAST_INVALID_INPUT"),
    ("SELECT cast('1e3' as int)", "CAST_INVALID_INPUT"),
    ("SELECT cast(s as boolean) FROM t WHERE id = 1", "CAST_INVALID_INPUT"),
    ("SELECT 2147483647 + 1", "ARITHMETIC_OVERFLOW"),
    ("SELECT x / 0 FROM t WHERE id = 1", "DIVIDE_BY_ZERO"),
    ("SELECT 5 % 0", "DIVIDE_BY_ZERO"),
    ("SELECT CREATE_ignored", "UNRESOLVED_COLUMN"),
])
def test_probes_databricks_fails_fail_the_same_way(shared, sql, error_class):
    with pytest.raises(SqlError) as failed:
        shared.execute(sql, CONTEXT)
    assert failed.value.error_class == error_class, (sql, failed.value)


def test_unaliased_results_are_named_as_spark_names_them(shared):
    result, _ = shared.execute("SELECT d / 3, count(*), year(dt), x, upper(s) AS u FROM t "
                             "GROUP BY d, dt, x, s", CONTEXT)
    assert [c.name for c in result.columns] == ["(d / 3)", "count(1)", "year(dt)", "x", "u"]
