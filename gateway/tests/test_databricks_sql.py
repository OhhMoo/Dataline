"""Databricks SQL over the seeded lakehouse copy (alkera_gateway.databricks_sql).

Claims:

- TRANSLATION IS EXACT OR REFUSED. A fixed corpus of Databricks SQL - backtick
  identifiers, three-part `main.warehouse.<t>` names, the `hive_metastore` alias,
  Spark functions sqlglot rewrites (`nvl`, `date_trunc`, `date_format`, `collect_list`)
  - returns exactly what the equivalent DuckDB query returns on the world's own file.
- DELTA-ONLY SQL IS REFUSED BY NAME. Time travel, history, OPTIMIZE/ZORDER, VACUUM,
  CLONE: there is no Delta log here, and an answer would be invented.
- THE WORLD IS NEVER WRITTEN. The lakehouse is a copy; CREATE/INSERT/DROP land in the
  copy and the world's warehouse file keeps its bytes.
- THE CATALOG LOOKS LIKE UNITY CATALOG. `SHOW SCHEMAS/TABLES/VIEWS`,
  `system.information_schema.*`, `USE`, `current_catalog()` answer in Databricks'
  column names and in catalog `main`, never the engine's internal catalog name.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from conftest import make_world

from alkera_gateway import databricks_sql as dbx
from alkera_gateway.databricks_sql import Context, Lakehouse, SqlError, Unsupported


def lakehouse_world(root: Path) -> Path:
    """`make_world` plus the commerce shapes the probes read: fct_orders, a customer
    dimension, a raw schema and a view. Returns the world's warehouse file."""
    make_world(root)
    warehouse = root / "var" / "warehouse" / "warehouse.duckdb"
    with duckdb.connect(str(warehouse)) as con:
        con.execute("""CREATE TABLE warehouse.warehouse.fct_orders (
            order_id INTEGER, customer_id INTEGER, order_date DATE,
            order_status VARCHAR, order_amount DECIMAL(18, 2))""")
        con.execute("""INSERT INTO warehouse.warehouse.fct_orders VALUES
            (1, 10, DATE '2026-01-15', 'completed', 120.50),
            (2, 11, DATE '2026-02-03', 'completed', 80.00),
            (3, 10, DATE '2026-02-10', 'returned', 45.25),
            (4, 12, DATE '2026-02-21', 'completed', 300.10),
            (5, 11, DATE '2026-03-02', 'pending', NULL),
            (6, 13, DATE '2026-02-28', 'completed', 19.99)""")
        con.execute("""CREATE TABLE warehouse.warehouse.dim_customers (
            customer_id INTEGER, region VARCHAR, segment VARCHAR)""")
        con.execute("""INSERT INTO warehouse.warehouse.dim_customers VALUES
            (10, 'EMEA', 'smb'), (11, 'AMER', 'enterprise'), (12, 'APAC', 'smb'),
            (13, 'EMEA', 'enterprise')""")
        con.execute("CREATE SCHEMA warehouse.raw")
        con.execute("CREATE TABLE warehouse.raw.orders AS SELECT order_id, order_amount "
                    "FROM warehouse.warehouse.fct_orders")
        con.execute("CREATE VIEW warehouse.warehouse.completed_orders AS SELECT * FROM "
                    "warehouse.warehouse.fct_orders WHERE order_status = 'completed'")
    return warehouse


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def world(tmp_path) -> Path:
    return lakehouse_world(tmp_path / "world")


@pytest.fixture
def lake(world, tmp_path) -> Lakehouse:
    path = tmp_path / "state" / "databricks" / "lakehouse.duckdb"
    Lakehouse.seed(world, path)
    lakehouse = Lakehouse(path)
    yield lakehouse
    lakehouse.close()


def run(lake: Lakehouse, sql: str, context: Context | None = None):
    result, _ = lake.execute(sql, context or Context())
    return result


def truth(world: Path, sql: str):
    with duckdb.connect(str(world), read_only=True) as con:
        return con.execute(sql).fetchall()


def naive(rows) -> list[tuple]:
    """Rows with every aware datetime as naive UTC: a Databricks TIMESTAMP (the
    lakehouse's TIMESTAMPTZ in a UTC session) compares with a DuckDB TIMESTAMP."""
    import datetime

    def one(value):
        if isinstance(value, datetime.datetime) and value.tzinfo is not None:
            return value.astimezone(datetime.UTC).replace(tzinfo=None)
        return value
    return [tuple(one(v) for v in row) for row in rows]


# --------------------------------------------------------------------- fences
DELTA_ONLY = [
    ("DESCRIBE HISTORY main.warehouse.fct_orders", "DESCRIBE HISTORY"),
    ("describe history `main`.`warehouse`.`fct_orders`", "DESCRIBE HISTORY"),
    ("SELECT * FROM main.warehouse.fct_orders VERSION AS OF 3", "VERSION AS OF"),
    ("SELECT * FROM main.warehouse.fct_orders TIMESTAMP AS OF '2026-01-01'",
     "TIMESTAMP AS OF"),
    ("OPTIMIZE main.warehouse.fct_orders", "OPTIMIZE"),
    ("OPTIMIZE main.warehouse.fct_orders ZORDER BY (order_date)", "ZORDER"),
    ("VACUUM main.warehouse.fct_orders RETAIN 168 HOURS", "VACUUM"),
    ("CREATE TABLE main.default.c SHALLOW CLONE main.warehouse.fct_orders", "CLONE"),
    ("CREATE OR REPLACE TABLE main.default.c DEEP CLONE main.warehouse.fct_orders", "CLONE"),
    ("RESTORE TABLE main.warehouse.fct_orders TO VERSION AS OF 1", "RESTORE"),
    ("DESCRIBE DETAIL main.warehouse.fct_orders", "DESCRIBE DETAIL"),
    ("SELECT * FROM table_changes('main.warehouse.fct_orders', 1)", "table_changes"),
]


@pytest.mark.parametrize("sql,feature", DELTA_ONLY)
def test_delta_only_sql_refused_by_name(sql, feature):
    with pytest.raises(Unsupported) as refused:
        dbx.translate(sql)
    assert feature in refused.value.feature
    assert feature in str(refused.value)
    assert "not available in this environment" in str(refused.value)


@pytest.mark.parametrize("sql,feature", [
    ("CREATE TABLE main.default.ext (a INT) USING CSV LOCATION 's3://bucket/x'", "USING CSV"),
    ("CREATE TABLE main.default.ext (a INT) LOCATION 's3://bucket/x'", "LOCATION"),
    ("COPY INTO main.default.t FROM 's3://bucket/x' FILEFORMAT = CSV", "COPY INTO"),
    ("SELECT * FROM system.billing.usage", "system.billing"),
])
def test_external_storage_refused_by_name(sql, feature):
    with pytest.raises(Unsupported) as refused:
        dbx.translate(sql)
    assert feature in str(refused.value)


def test_delta_refusal_is_raised_by_the_engine_too(lake, world):
    before = digest(world)
    with pytest.raises(Unsupported, match="DESCRIBE HISTORY"):
        run(lake, "DESCRIBE HISTORY main.warehouse.fct_orders")
    assert digest(world) == before


# ------------------------------------------------------------ the corpus
#: (Databricks SQL, the DuckDB query on the world's own file that it must equal)
CORPUS = [
    ("select sum(order_amount) from main.warehouse.fct_orders "
     "where order_status='completed'",
     "SELECT sum(order_amount) FROM warehouse.warehouse.fct_orders "
     "WHERE order_status = 'completed'"),
    ("SELECT `order_status`, count(*) AS `n` FROM `main`.`warehouse`.`fct_orders` "
     "GROUP BY `order_status` ORDER BY `order_status`",
     "SELECT order_status, count(*) FROM warehouse.warehouse.fct_orders "
     "GROUP BY 1 ORDER BY 1"),
    ("select count(*) from hive_metastore.warehouse.fct_orders",
     "SELECT count(*) FROM warehouse.warehouse.fct_orders"),
    ("select date_trunc('MONTH', order_date) as m, sum(order_amount) "
     "from main.warehouse.fct_orders where order_status = 'completed' "
     "group by 1 order by 1",
     "SELECT date_trunc('month', order_date), sum(order_amount) "
     "FROM warehouse.warehouse.fct_orders WHERE order_status = 'completed' "
     "GROUP BY 1 ORDER BY 1"),
    ("select o.order_id, c.region from main.warehouse.fct_orders o "
     "join `main`.warehouse.`dim_customers` c on o.customer_id = c.customer_id "
     "order by o.order_id",
     "SELECT o.order_id, c.region FROM warehouse.warehouse.fct_orders o "
     "JOIN warehouse.warehouse.dim_customers c USING (customer_id) ORDER BY 1"),
    ("select order_id, nvl(order_amount, 0) from main.warehouse.fct_orders order by 1",
     "SELECT order_id, coalesce(order_amount, 0) FROM warehouse.warehouse.fct_orders "
     "ORDER BY 1"),
    ("select date_format(order_date, 'yyyy-MM') as ym, count(*) "
     "from main.warehouse.fct_orders group by 1 order by 1",
     "SELECT strftime(order_date, '%Y-%m'), count(*) "
     "FROM warehouse.warehouse.fct_orders GROUP BY 1 ORDER BY 1"),
    ("select size(collect_list(order_status)) from main.warehouse.fct_orders",
     "SELECT count(order_status) FROM warehouse.warehouse.fct_orders"),
    ("select sum(order_amount) from main.warehouse.completed_orders "
     "where order_date >= '2026-02-01' and order_date < '2026-03-01'",
     "SELECT sum(order_amount) FROM warehouse.warehouse.fct_orders "
     "WHERE order_status = 'completed' AND order_date >= DATE '2026-02-01' "
     "AND order_date < DATE '2026-03-01'"),
    ("select count(*) from main.raw.orders where order_amount > 50",
     "SELECT count(*) FROM warehouse.raw.orders WHERE order_amount > 50"),
]


@pytest.mark.parametrize("databricks,duck", CORPUS)
def test_backticks_and_three_part_names_match_duckdb_truth(lake, world, databricks, duck):
    got = run(lake, databricks).rows
    assert naive(got) == naive(truth(world, duck))


def test_two_part_names_resolve_in_the_current_catalog(lake, world):
    expected = truth(world, "SELECT count(*) FROM warehouse.warehouse.dim_customers")
    assert run(lake, "select count(*) from warehouse.dim_customers").rows == expected
    in_schema = Context(schema="warehouse")
    assert run(lake, "select count(*) from dim_customers", in_schema).rows == expected


def test_result_columns_carry_databricks_types(lake):
    result = run(lake, "select order_id, order_amount, order_status, order_date, "
                       "sum(order_amount) over () as total, count(*) over () as n "
                       "from main.warehouse.fct_orders limit 1")
    assert [c.name for c in result.columns] == ["order_id", "order_amount", "order_status",
                                                "order_date", "total", "n"]
    assert [c.type_name for c in result.columns] == ["INT", "DECIMAL", "STRING", "DATE",
                                                     "DECIMAL", "LONG"]
    amount = result.columns[1]
    assert (amount.type_text, amount.type_precision, amount.type_scale) == \
        ("decimal(18,2)", 18, 2)
    assert isinstance(result.rows[0][1], Decimal)


# --------------------------------------------------------------- the copy
def test_seed_copies_every_table_and_view_into_catalog_main(world, tmp_path):
    record = Lakehouse.seed(world, tmp_path / "lake" / "lakehouse.duckdb")
    copied = {(r["schema"], r["table"]): r["rows"] for r in record}
    assert copied[("warehouse", "fct_orders")] == 6
    assert copied[("warehouse", "completed_orders")] == 4      # a view, materialised
    assert copied[("raw", "orders")] == 6
    assert copied[("warehouse", "items")] == 2


def test_remote_write_leaves_world_fingerprint_unchanged(lake, world):
    before = digest(world)
    run(lake, "CREATE TABLE main.default.scratch (id INT, note STRING) USING DELTA "
              "PARTITIONED BY (id) TBLPROPERTIES ('delta.appendOnly' = 'false')")
    inserted = run(lake, "INSERT INTO main.default.scratch VALUES (1, 'alpha'), (2, 'beta')")
    assert [c.name for c in inserted.columns] == ["num_affected_rows", "num_inserted_rows"]
    assert inserted.rows == [(2, 2)]
    run(lake, "DELETE FROM main.warehouse.fct_orders WHERE order_status = 'returned'")
    run(lake, "CREATE OR REPLACE TABLE main.warehouse.fct_orders_copy USING DELTA AS "
              "SELECT * FROM main.warehouse.fct_orders")
    run(lake, "DROP TABLE main.warehouse.dim_customers")
    assert run(lake, "select note from main.default.scratch order by id").rows == \
        [("alpha",), ("beta",)]
    assert run(lake, "select count(*) from main.warehouse.fct_orders").rows == [(5,)]
    assert digest(world) == before
    assert truth(world, "SELECT count(*) FROM warehouse.warehouse.fct_orders") == [(6,)]


def test_a_reseed_does_not_replace_an_existing_copy(world, tmp_path):
    path = tmp_path / "lake" / "lakehouse.duckdb"
    Lakehouse.seed(world, path)
    lake = Lakehouse(path)
    run(lake, "CREATE TABLE main.default.kept AS SELECT 1 AS id")
    lake.close()
    assert Lakehouse.seed(world, path) is None                # already seeded: kept
    lake = Lakehouse(path)
    assert run(lake, "select id from main.default.kept").rows == [(1,)]
    lake.close()


# ------------------------------------------------------- catalog commands
def test_show_catalogs_schemas_tables_views(lake):
    assert run(lake, "SHOW CATALOGS").rows == [("main",)]
    schemas = run(lake, "SHOW SCHEMAS IN `main`")
    assert [c.name for c in schemas.columns] == ["databaseName"]
    assert {r[0] for r in schemas.rows} >= {"default", "information_schema", "raw",
                                             "warehouse"}
    assert run(lake, "SHOW SCHEMAS IN main LIKE 'ware*'").rows == [("warehouse",)]
    tables = run(lake, "SHOW TABLES IN main.warehouse")
    assert [c.name for c in tables.columns] == ["database", "tableName", "isTemporary"]
    assert ("warehouse", "fct_orders", False) in tables.rows
    assert run(lake, "show tables in warehouse like 'dim*|items'").rows == [
        ("warehouse", "dim_customers", False), ("warehouse", "items", False)]
    run(lake, "CREATE VIEW main.default.v AS SELECT 1 AS id")
    views = run(lake, "SHOW VIEWS IN main.default")
    assert [c.name for c in views.columns] == ["namespace", "viewName", "isTemporary",
                                               "isMaterialized"]
    assert views.rows == [("default", "v", False, False)]


def test_the_drivers_metadata_commands(lake):
    """What databricks-sql-connector sends over SEA for cursor.tables()/columns():
    JDBC patterns, the table type at index 5 (the connector's own filter reads it)."""
    tables = run(lake, "SHOW TABLES IN CATALOG main SCHEMA LIKE 'warehouse'")
    assert [c.name for c in tables.columns][:6] == [
        "catalogName", "namespace", "tableName", "isTemporary", "information", "tableType"]
    assert ("main", "warehouse", "fct_orders") == tables.rows[2][:3]
    assert {r[5] for r in tables.rows} == {"TABLE"}
    assert run(lake, "SHOW TABLES IN CATALOG main SCHEMA LIKE 'ware%' LIKE 'dim%'").rows[
        0][2] == "dim_customers"
    assert run(lake, "SHOW TABLES IN CATALOG samples").rows == []
    columns = run(lake, "SHOW COLUMNS IN CATALOG main SCHEMA LIKE 'warehouse' "
                        "TABLE LIKE 'fct_orders'")
    assert [r[0] for r in columns.rows] == ["order_id", "customer_id", "order_date",
                                            "order_status", "order_amount"]
    assert columns.rows[-1][4] == "decimal(18,2)"


def test_a_missing_session_schema_fails_unqualified_names_until_created(lake):
    fresh = Context(schema="dbt_new")
    with pytest.raises(SqlError, match="SCHEMA_NOT_FOUND"):
        lake.execute("create table t as select 1 as id", fresh)
    lake.execute("create schema if not exists main.dbt_new", fresh)
    lake.execute("create table t as select 1 as id", fresh)
    assert run(lake, "select id from main.dbt_new.t").rows == [(1,)]
    assert "dbt_new" in lake.schemas()


def test_information_schema_is_unity_catalogs(lake):
    rows = run(lake, "SELECT table_catalog, table_schema, table_name, table_type, "
                     "data_source_format FROM system.information_schema.tables "
                     "WHERE table_schema = 'warehouse' ORDER BY table_name").rows
    assert ("main", "warehouse", "fct_orders", "MANAGED", "DELTA") in rows
    assert ("main", "warehouse", "completed_orders", "MANAGED", "DELTA") in rows
    cols = run(lake, "select column_name, data_type, full_data_type from "
                     "main.information_schema.columns where table_name = 'fct_orders' "
                     "order by ordinal_position").rows
    assert cols[0] == ("order_id", "INT", "int")
    assert cols[-1] == ("order_amount", "DECIMAL", "decimal(18,2)")


def test_describe_table(lake):
    described = run(lake, "DESCRIBE TABLE main.warehouse.fct_orders")
    assert [c.name for c in described.columns] == ["col_name", "data_type", "comment"]
    assert described.rows[0] == ("order_id", "int", None)
    assert ("order_amount", "decimal(18,2)", None) in described.rows


def test_use_and_current_catalog(lake):
    context = Context()
    result, context = lake.execute("USE CATALOG hive_metastore", context)
    assert context.catalog == "hive_metastore"
    result, context = lake.execute("USE SCHEMA warehouse", context)
    assert (context.catalog, context.schema) == ("hive_metastore", "warehouse")
    result, _ = lake.execute("select current_catalog(), current_schema(), current_user()",
                             context)
    assert result.rows == [("hive_metastore", "warehouse", dbx.USER)]
    with pytest.raises(SqlError, match="SCHEMA_NOT_FOUND"):
        lake.execute("USE SCHEMA nope", context)
    with pytest.raises(SqlError, match="NO_SUCH_CATALOG_EXCEPTION"):
        lake.execute("USE CATALOG samples", context)


def test_set_is_accepted_and_echoed(lake):
    result = run(lake, "SET spark.sql.ansi.enabled = true")
    assert [c.name for c in result.columns] == ["key", "value"]
    assert result.rows == [("spark.sql.ansi.enabled", "true")]


def test_errors_name_catalog_main_never_the_engine(lake):
    with pytest.raises(SqlError) as missing:
        run(lake, "select * from main.warehouse.nope")
    assert missing.value.error_class == "TABLE_OR_VIEW_NOT_FOUND"
    assert missing.value.sqlstate == "42P01"
    assert dbx.ENGINE_CATALOG not in str(missing.value)
    with pytest.raises(SqlError) as syntax:
        run(lake, "selec 1")
    assert syntax.value.error_class == "PARSE_SYNTAX_ERROR"


@pytest.mark.parametrize("sql", [
    "ATTACH '/world/var/warehouse/warehouse.duckdb' AS w",
    "INSTALL httpfs",
    "COPY main.warehouse.fct_orders TO '/tmp/out.csv'",
    "SELECT * FROM read_csv('/world/var/warehouse/x.csv')",
    "DROP VIEW memory.dbx_information_schema.tables",
    "DROP VIEW information_schema.tables",
    "DROP VIEW system.information_schema.columns",
    "INSERT INTO main.information_schema.tables SELECT * FROM main.information_schema.tables",
    f"SELECT * FROM {dbx.ENGINE_CATALOG}.warehouse.fct_orders",
    "SELECT * FROM samples.nyctaxi.trips",
])
def test_no_statement_reaches_past_the_copy(lake, world, sql):
    """The engine's own catalogs, files and extensions are not Databricks SQL: refused
    (by name, or as Databricks fails them), and the world file is untouched."""
    before = digest(world)
    with pytest.raises((Unsupported, SqlError)):
        run(lake, sql)
    assert digest(world) == before
    assert run(lake, "select count(*) from system.information_schema.tables "
                     "where table_schema = 'warehouse'").rows[0][0] > 0


@pytest.mark.parametrize("sql,name", [
    ("INSTALL httpfs", "INSTALL statements"),
    ("DETACH dbx_main", "DETACH statements"),
    ("CHECKPOINT", "CHECKPOINT statements"),
    ("PRAGMA database_list", "PRAGMA statements"),
    ("SELECT * FROM memory.x", "TABLE_OR_VIEW_NOT_FOUND"),
    ("SELECT * FROM pragma_database_list", "TABLE_OR_VIEW_NOT_FOUND"),
    ("SELECT * FROM duckdb_databases()", "duckdb_databases()"),
    ("SELECT * FROM query_table('dbx_main.warehouse.fct_orders')", "query_table()"),
])
def test_refusals_name_what_was_refused(lake, sql, name):
    with pytest.raises((Unsupported, SqlError)) as refused:
        run(lake, sql)
    said = refused.value.feature if isinstance(refused.value, Unsupported) else \
        refused.value.error_class
    assert name in said, (sql, said)


def test_merge_update_and_insert_on_the_copy(lake):
    run(lake, "CREATE TABLE main.default.m (id INT, v STRING)")
    run(lake, "INSERT INTO main.default.m VALUES (1, 'old'), (2, 'keep')")
    merged = run(lake, "MERGE INTO main.default.m AS t USING (SELECT 1 AS id, 'new' AS v "
                       "UNION ALL SELECT 3, 'added') AS s ON t.id = s.id "
                       "WHEN MATCHED THEN UPDATE SET v = s.v "
                       "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v)")
    assert merged.rows == [(2,)] and merged.rows_affected == 2
    assert run(lake, "select id, v from main.default.m order by id").rows == [
        (1, "new"), (2, "keep"), (3, "added")]


def test_one_statement_per_request(lake):
    with pytest.raises(SqlError, match="one statement"):
        run(lake, "select 1; select 2")
    assert run(lake, "select 1;").rows == [(1,)]


def test_translate_is_a_string_in_duckdb_with_the_engine_catalog():
    out = dbx.translate("SELECT `a` FROM `main`.`warehouse`.`t` WHERE b = 'main'")
    assert out == f'SELECT "a" FROM "{dbx.ENGINE_CATALOG}"."warehouse"."t" WHERE b = \'main\''
    # an unaliased result expression keeps Spark's name, whatever its DuckDB form
    assert dbx.translate("select 1 from hive_metastore.s.t") == \
        f'SELECT 1 AS "1" FROM {dbx.ENGINE_CATALOG}.s.t'
