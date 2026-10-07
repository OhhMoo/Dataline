"""The vetted Databricks function surface (alkera_gateway.databricks_functions).

FUNCTION_CORPUS is one Databricks SQL expression per line and the value Databricks
returns for it - the examples of the Databricks SQL language reference where it gives
one ("docs"), and the documented semantics otherwise (truncating integral CAST, Sunday =
1, java.time's whole units in timestampdiff, Spark's substring positions, ANSI
DIVIDE_BY_ZERO). Every entry on the vetted lists has at least one case here
(`test_every_vetted_entry_has_a_corpus_case`), and everything else is refused naming
the function (`REFUSED`).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlglot
from sqlglot import exp
from test_databricks_sql import lakehouse_world

from alkera_gateway import databricks_functions as dbf
from alkera_gateway.databricks_sql import Context, Lakehouse, SqlError, Unsupported

D, T = dt.date, dt.datetime
#: The world's clock for current_date/current_timestamp.
NOW = "2026-03-02T09:30:00Z"
FCT = "main.warehouse.fct_orders"
#: fct_orders in lakehouse_world: order_id 1..6, customer_id 10,11,10,12,11,13,
#: order_amount 120.50, 80.00, 45.25, 300.10, NULL, 19.99, statuses completed x4,
#: returned, pending.

FUNCTION_CORPUS: list[tuple[str, object]] = [
    # CAST: integral targets truncate toward zero (DuckDB would round)
    ("cast(10.50 as int)", 10),
    ("int(10.5)", 10),
    ("cast(-10.9 as bigint)", -10),
    ("bigint('7')", 7),
    ("smallint(3.99)", 3),
    ("tinyint(-3.99)", -3),
    ("try_cast(10.7 as int)", 10),
    ("try_cast('x' as int)", None),
    ("cast(10.555 as decimal(5,2))", Decimal("10.56")),               # HALF_UP
    ("cast(1.5 as string)", "1.5"),
    ("cast(date'2026-02-01' as string)", "2026-02-01"),
    ("cast('2026-02-01' as date)", D(2026, 2, 1)),
    ("cast('true' as boolean)", True),
    ("cast(3 as double)", 3.0),
    ("cast(1.25 as float)", 1.25),
    ("cast('2026-02-01 10:11:12' as timestamp)", T(2026, 2, 1, 10, 11, 12)),
    # days of the week: Sunday is 1 for dayofweek, Monday 0 for weekday
    ("dayofweek(date'2009-07-30')", 5),                               # docs
    ("dayofweek(date'2026-03-01')", 1),                               # a Sunday
    ("extract(DAYOFWEEK FROM date'2026-03-01')", 1),
    ("extract(DOW FROM date'2026-03-07')", 7),                        # a Saturday
    ("extract(DAYOFWEEK_ISO FROM date'2026-03-01')", 7),
    ("weekday(date'2009-07-30')", 3),                                 # docs
    ("weekday(date'2026-03-01')", 6),
    # date arithmetic: date_add/date_sub return a DATE
    ("date_add(date'2016-07-30', 1)", D(2016, 7, 31)),                # docs
    ("date_sub(date'2016-07-30', 1)", D(2016, 7, 29)),                # docs
    ("date_add(timestamp'2026-02-27 23:30:00', 1)", D(2026, 2, 28)),
    ("add_months(date'2016-08-31', 1)", D(2016, 9, 30)),              # docs
    ("add_months(date'2024-01-31', 1)", D(2024, 2, 29)),
    ("last_day(date'2009-01-12')", D(2009, 1, 31)),                   # docs
    ("timestampadd(MONTH, -1, timestamp'2022-03-31 00:00:00')", T(2022, 2, 28)),  # docs
    ("dateadd(MONTH, 1, date'2026-01-31')", T(2026, 2, 28)),
    ("timestampadd(QUARTER, 1, date'2026-01-31')", T(2026, 4, 30)),
    ("timestampadd(HOUR, 2, timestamp'2026-02-01 10:00:00')", T(2026, 2, 1, 12)),
    ("timestampadd(MICROSECOND, 5, timestamp'2022-02-28 00:00:00')",
     T(2022, 2, 28, 0, 0, 0, 5)),                                     # docs
    # timestampdiff: whole units, the end date stepping back when its time is earlier
    ("timestampdiff(MONTH, date'2024-01-31', date'2024-02-29')", 0),
    ("timestampdiff(MONTH, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", 1),                           # docs
    ("timestampdiff(MONTH, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 07:00:00')", 0),
    ("timestampdiff(WEEK, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", 4),                           # docs
    ("timestampdiff(DAY, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", 28),                          # docs
    ("timestampdiff(HOUR, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", 672),                         # docs
    ("timestampdiff(MINUTE, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", 40320),                       # docs
    ("timestampdiff(SECOND, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", 2419200),                     # docs
    ("timestampdiff(MILLISECOND, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", 2419200000),                  # docs
    ("timestampdiff(MICROSECOND, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", 2419200000000),               # docs
    ("timestampdiff(YEAR, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", 0),                           # docs
    ("timestampdiff(YEAR, date'2020-02-29', date'2021-02-28')", 0),
    ("timestampdiff(QUARTER, date'2026-01-15', date'2026-07-14')", 1),
    ("timestampdiff(DAY, timestamp'2024-01-01 23:00:00', timestamp'2024-01-02 01:00:00')", 0),
    ("timestampdiff(MONTH, date'2024-03-31', date'2024-02-29')", -1),
    ("datediff(MONTH, date'2024-01-31', date'2024-02-29')", 0),
    ("datediff(date'2009-07-31', date'2009-07-30')", 1),              # docs
    ("datediff(timestamp'2024-01-02 01:00:00', timestamp'2024-01-01 23:00:00')", 1),
    # truncation: date_trunc returns a TIMESTAMP, trunc a DATE
    ("date_trunc('YEAR', '2015-03-05T09:32:05.359')", T(2015, 1, 1)),  # docs
    ("date_trunc('MM', '2015-03-05T09:32:05.359')", T(2015, 3, 1)),    # docs
    ("date_trunc('DD', '2015-03-05T09:32:05.359')", T(2015, 3, 5)),    # docs
    ("date_trunc('HOUR', '2015-03-05T09:32:05.359')", T(2015, 3, 5, 9)),  # docs
    ("date_trunc('QUARTER', date'2026-05-17')", T(2026, 4, 1)),
    ("date_trunc('WEEK', date'2026-03-04')", T(2026, 3, 2)),
    ("trunc(date'2019-08-04', 'week')", D(2019, 7, 29)),               # docs
    ("trunc(date'2019-08-04', 'quarter')", D(2019, 7, 1)),             # docs
    ("trunc(date'2009-02-12', 'MM')", D(2009, 2, 1)),                  # docs
    ("trunc(date'2015-10-27', 'YEAR')", D(2015, 1, 1)),                # docs
    # fields
    ("year(date'2016-07-30')", 2016),                                  # docs
    ("quarter(date'2016-08-31')", 3),                                  # docs
    ("month(date'2016-07-30')", 7),                                    # docs
    ("day(date'2009-07-30')", 30),                                     # docs
    ("dayofmonth(date'2009-07-30')", 30),                              # docs
    ("dayofyear(date'2016-04-09')", 100),                              # docs
    ("weekofyear(date'2008-02-20')", 8),                               # docs
    ("hour(timestamp'2009-07-30 12:58:59')", 12),                      # docs
    ("minute(timestamp'2009-07-30 12:58:59')", 58),                    # docs
    ("second(timestamp'2009-07-30 12:58:59')", 59),                    # docs
    ("extract(YEAR FROM timestamp'2019-08-12 01:00:00.123456')", 2019),   # docs
    ("extract(week FROM timestamp'2019-08-12 01:00:00.123456')", 33),     # docs
    ("extract(doy FROM date'2019-08-12')", 224),                          # docs
    ("extract(SECONDS FROM timestamp'2019-08-12 01:00:00.123456')",
     Decimal("0.123456")),                                                # docs
    ("extract(minute FROM timestamp'2019-08-12 01:00:00.123456')", 0),    # docs
    ("extract(hour FROM timestamp'2019-08-12 01:00:00.123456')", 1),      # docs
    ("extract(QUARTER FROM date'2026-05-17')", 2),
    ("extract(MONTH FROM date'2026-05-17')", 5),
    ("extract(DAY FROM date'2026-05-17')", 17),
    # parsing and formatting
    ("to_date('2009-07-30 04:17:52')", D(2009, 7, 30)),                 # docs
    ("to_date('2016-12-31', 'yyyy-MM-dd')", D(2016, 12, 31)),           # docs
    ("to_timestamp('2016-12-31 00:12:00')", T(2016, 12, 31, 0, 12)),    # docs
    ("to_timestamp('2016-12-31', 'yyyy-MM-dd')", T(2016, 12, 31)),      # docs
    ("date_format(date'2016-04-08', 'y')", "2016"),                     # docs
    ("date_format(timestamp'2016-04-08 10:11:12', 'yyyy-MM-dd HH:mm:ss')",
     "2016-04-08 10:11:12"),
    # the world's clock
    ("current_date()", D(2026, 3, 2)),
    ("current_timestamp()", T(2026, 3, 2, 9, 30)),
    ("now()", T(2026, 3, 2, 9, 30)),
    # strings
    ("substring('Spark SQL', 5)", "k SQL"),                             # docs
    ("substring('Spark SQL', -3)", "SQL"),                              # docs
    ("substring('Spark SQL', 5, 1)", "k"),                              # docs
    ("substring('Spark SQL', 0, 3)", "Spa"),
    ("substr('Spark SQL', -3, 2)", "SQ"),
    ("substring('abc', -5, 3)", "a"),
    ("left('Spark SQL', 3)", "Spa"),                                    # docs
    ("left('Spark', -1)", ""),
    ("right('Spark SQL', 3)", "SQL"),                                   # docs
    ("right('Spark', 0)", ""),
    ("right('ab', 5)", "ab"),
    ("lower('SparkSql')", "sparksql"),                                  # docs
    ("upper('SparkSql')", "SPARKSQL"),                                  # docs
    ("length('Spark SQL ')", 10),                                       # docs
    ("trim('    SparkSQL   ')", "SparkSQL"),                            # docs
    ("trim(BOTH 'SL' FROM 'SSparkSQLS')", "parkSQ"),                    # docs
    ("ltrim('    SparkSQL   ')", "SparkSQL   "),                        # docs
    ("rtrim('    SparkSQL   ')", "    SparkSQL"),                       # docs
    ("concat('Spark', 'SQL')", "SparkSQL"),                             # docs
    ("concat('a', NULL)", None),
    ("concat_ws(' ', 'Spark', 'SQL')", "Spark SQL"),                    # docs
    ("concat_ws('-', 'a', NULL, 'b')", "a-b"),
    ("replace('ABCabc', 'abc', 'DEF')", "ABCDEF"),                      # docs
    ("regexp_replace('100-200', '[0-9]+', 'num')", "num-num"),
    ("regexp_extract('100-200', '([0-9]+)-([0-9]+)', 1)", "100"),
    ("regexp_extract('100-200', '([0-9]+)-([0-9]+)')", "100"),
    ("regexp_extract('100-200', '([0-9]+)-([0-9]+)', 2)", "200"),
    ("'Spark' rlike 'S.*k'", True),
    ("'Spark' like 'S%'", True),
    ("'Spark' like 'S_ark'", True),
    ("'Spark' ilike 's%'", True),
    ("lpad('hi', 5, '??')", "???hi"),                                   # docs
    ("lpad('hi', 1, '??')", "h"),                                       # docs
    ("rpad('hi', 5, '??')", "hi???"),                                   # docs
    ("initcap('sPark sql')", "Spark Sql"),                              # docs
    ("instr('SparkSQL', 'SQL')", 6),                                    # docs
    ("locate('bar', 'foobarbar')", 4),                                  # docs
    ("'Spark' || 'SQL'", "SparkSQL"),
    # conditionals
    ("if(1 < 2, 'a', 'b')", "a"),
    ("iff(1 > 2, 'a', 'b')", "b"),
    ("case when 1 > 2 then 'a' else 'b' end", "b"),
    ("coalesce(NULL, 1, NULL)", 1),                                     # docs
    ("nvl(NULL, 2)", 2),
    ("ifnull(NULL, 3)", 3),
    ("nullif(2, 2)", None),                                             # docs
    ("nvl2(NULL, 2, 1)", 1),                                            # docs
    ("greatest(10, 9, 2, 4, 3)", 10),                                   # docs
    ("least(10, 9, 2, 4, 3)", 2),                                       # docs
    ("greatest(1, NULL, 3)", 3),
    ("1 <=> NULL", False),
    ("NULL <=> NULL", True),
    ("2 between 1 and 3", True),
    ("2 in (1, 3)", False),
    ("NULL is null", True),
    ("not true", False),
    # math
    ("abs(-1)", 1),                                                     # docs
    ("round(2.5, 0)", 3),                                               # docs
    ("round(25, -1)", 30),                                              # docs
    ("round(-2.5)", -3),
    ("round(cast(2.675 as double), 2)", 2.68),
    ("floor(-0.1)", -1),                                                # docs
    ("ceil(-0.1)", 0),                                                  # docs
    ("ceiling(5)", 5),
    ("sqrt(4)", 2.0),                                                   # docs
    ("power(2, 3)", 8.0),                                               # docs
    ("pow(2, 3)", 8.0),
    ("ln(1)", 0.0),                                                     # docs
    ("log(10, 100)", 2.0),                                              # docs
    ("log10(10)", 1.0),                                                 # docs
    ("log2(2)", 1.0),                                                   # docs
    ("exp(0)", 1.0),                                                    # docs
    ("signum(40)", 1.0),                                                # docs
    ("sign(-3)", -1.0),
    ("7 / 2", 3.5),
    ("7 div 2", 3),
    ("-7 div 2", -3),
    ("-7 % 2", -1),
    ("2 * 3 - 1 + -4", 1),
    # arrays
    ("array(1, 2)", [1, 2]),
    ("size(array(1, 2))", 2),
    ("array_contains(array(1, 2), 2)", True),
    # the session
    ("current_catalog()", "main"),
    ("current_schema()", "default"),
    ("current_database()", "default"),
    ("current_user()", "alkera-local-dev"),
    ("session_user()", "alkera-local-dev"),
    ("user()", "alkera-local-dev"),
    ("length(current_version()) > 10", True),
    ("'a\\tb'", "a\tb"),                       # a Spark escape sqlglot unescapes: TAB
]

#: Aggregates and windows over fct_orders: (query, value).
QUERY_CORPUS: list[tuple[str, object]] = [
    (f"select count(*) from {FCT}", 6),
    (f"select count(order_amount) from {FCT}", 5),
    (f"select count(distinct order_status) from {FCT}", 3),
    (f"select sum(order_amount) from {FCT}", Decimal("565.84")),
    (f"select avg(order_amount) from {FCT}", 113.168),
    (f"select min(order_amount) from {FCT}", Decimal("19.99")),
    (f"select max(order_amount) from {FCT}", Decimal("300.10")),
    (f"select count_if(order_status = 'completed') from {FCT}", 4),
    (f"select any_value(order_id) from {FCT} where order_id = 1", 1),
    (f"select stddev(order_id) from {FCT}", 1.8708286933869707),
    (f"select stddev_samp(order_id) from {FCT}", 1.8708286933869707),
    (f"select stddev_pop(order_id) from {FCT}", 1.707825127659933),
    (f"select variance(order_id) from {FCT}", 3.5),
    (f"select var_samp(order_id) from {FCT}", 3.5),
    (f"select var_pop(order_id) from {FCT}", 2.9166666666666665),
    (f"select median(order_id) from {FCT}", 3.5),
    ("select median(col) from values (0), (10) as tab(col)", 5.0),                # docs
    ("select percentile(col, 0.3) from values (0), (10) as tab(col)", 3.0),       # docs
    (f"select percentile(order_id, 0.25) from {FCT}", 2.25),
    (f"select size(collect_list(order_status)) from {FCT}", 6),
    (f"select size(collect_set(order_status)) from {FCT}", 3),
    (f"select bool_and(order_id > 0) from {FCT}", True),
    (f"select bool_or(order_id > 5) from {FCT}", True),
    (f"select max_by(order_id, order_amount) from {FCT}", 4),
    (f"select min_by(order_id, order_amount) from {FCT}", 6),
    (f"select sum(order_amount) filter (where order_status = 'completed') from {FCT}",
     Decimal("520.59")),
    (f"select max(r) from (select row_number() over (order by order_id) as r from {FCT})", 6),
    (f"select max(r) from (select rank() over (order by customer_id) as r from {FCT})", 6),
    (f"select max(r) from (select dense_rank() over (order by customer_id) as r from {FCT})",
     4),
    (f"select max(r) from (select ntile(2) over (order by order_id) as r from {FCT})", 2),
    (f"select max(r) from (select percent_rank() over (order by order_id) as r from {FCT})",
     1.0),
    (f"select min(r) from (select cume_dist() over (order by customer_id) as r from {FCT})",
     2 / 6),
    (f"select l from (select order_id, lag(order_id) over (order by order_id) as l "
     f"from {FCT}) where order_id = 3", 2),
    (f"select l from (select order_id, lead(order_id, 1, 0) over (order by order_id) as l "
     f"from {FCT}) where order_id = 6", 0),
    (f"select f from (select order_id, first_value(order_id) over (order by order_id) "
     f"as f from {FCT}) where order_id = 3", 1),
    (f"select f from (select order_id, last_value(order_id) over (order by order_id) "
     f"as f from {FCT}) where order_id = 3", 3),
    (f"select count(*) from {FCT} where exists (select 1 from {FCT} where order_id = 1)", 6),
    (f"select count(*) from {FCT} where order_id = any (select order_id from {FCT} "
     f"where order_id < 3)", 2),
]

#: Refused by name: (expression or statement, the name the refusal must carry).
REFUSED = [
    ("select hash('a')", "hash()"),
    ("select bround(2.5)", "bround()"),
    ("select approx_count_distinct(order_id) from " + FCT, "approx_count_distinct"),
    ("select date_format(date'2026-02-01', 'MMM')", "datetime format"),
    ("select locate('l', 'hello', 4)", "instr/locate with position"),
    ("select date'2026-02-01' + interval 1 day", "INTERVAL arithmetic"),
    ("select array(1, 2)[0]", "array/map subscript"),
    ("select explode(array(1, 2))", "explode"),
    ("select named_struct('a', 1)", "struct()"),
    ("select map('a', 1)", "map()"),
    ("select x'0a'", "binary literals"),
    ("select 'x\\d'", "backslash escape"),             # Spark reads it as 'xd'
    ("select 'a\\tb' || 'x\\d'", "backslash escape"),
    ("select cast(1.5 as decimal)", "DECIMAL without a precision"),
    ("select 1.5BD", "DECIMAL without a precision"),
    ("select cast(1 as binary)", "CAST to BINARY"),
    ("select extract(MILLISECOND FROM timestamp'2026-02-01 00:00:00')", "EXTRACT(MILLISECOND)"),
    ("select date_trunc('DECADE', date'2026-02-01')", "date_trunc unit DECADE"),
    ("select trunc(date'2026-02-01', 'DD')", "trunc unit DD"),
    ("select 'a_b' like 'a\\_b'", "LIKE with an escape sequence"),
    ("select first(order_id) from " + FCT, "first"),
    ("select * from range(3)", "range"),
    ("select * from duckdb_settings()", "duckdb_settings"),
    ("select * from query('select 1')", "query"),
    ("select current_setting('TimeZone')", "current_setting"),
    ("select 1 & 3", "BitwiseAnd"),
]


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    root = tmp_path_factory.mktemp("functions")
    world = lakehouse_world(root / "world")
    path = root / "state" / "lakehouse.duckdb"
    Lakehouse.seed(world, path)
    lakehouse = Lakehouse(path)
    yield lakehouse
    lakehouse.close()


def value(lake: Lakehouse, sql: str):
    result, _ = lake.execute(sql, Context(now=NOW))
    assert len(result.rows) == 1 and len(result.rows[0]) == 1, result.rows
    got = result.rows[0][0]
    if isinstance(got, dt.datetime) and got.tzinfo is not None:
        got = got.astimezone(dt.UTC).replace(tzinfo=None)
    return got


def same(got, expected) -> bool:
    if expected is None or got is None:
        return got is expected
    if isinstance(expected, bool) or isinstance(got, bool):
        return got is expected
    if isinstance(expected, dt.datetime):
        return isinstance(got, dt.datetime) and got == expected
    if isinstance(expected, dt.date):
        # a DATE, not a TIMESTAMP (datetime is a date subclass)
        return type(got) is dt.date and got == expected
    if isinstance(expected, (int, float, Decimal)) and isinstance(got, (int, float, Decimal)):
        return abs(Decimal(str(got)) - Decimal(str(expected))) <= Decimal("1e-9")
    return got == expected


@pytest.mark.parametrize("expr,expected", FUNCTION_CORPUS)
def test_function_corpus_matches_databricks(lake, expr, expected):
    got = value(lake, f"select {expr}")
    assert same(got, expected), (expr, got, expected)


@pytest.mark.parametrize("sql,expected", QUERY_CORPUS)
def test_query_corpus_matches_databricks(lake, sql, expected):
    got = value(lake, sql)
    assert same(got, expected), (sql, got, expected)


@pytest.mark.parametrize("sql,name", REFUSED)
def test_anything_unvetted_is_refused_by_name(lake, sql, name):
    with pytest.raises(Unsupported) as refused:
        lake.execute(sql, Context(now=NOW))
    assert name in refused.value.feature, (sql, refused.value.feature)


@pytest.mark.parametrize("sql,error_class", [
    ("select 1 / 0", "DIVIDE_BY_ZERO"),
    ("select 7 div 0", "DIVIDE_BY_ZERO"),
    ("select 7 % 0", "DIVIDE_BY_ZERO"),
    (f"select sum(order_amount) / count(*) - 1 / (order_id - order_id) from {FCT} "
     "group by order_id", "DIVIDE_BY_ZERO"),
])
def test_division_by_zero_raises_as_an_ansi_warehouse_does(lake, sql, error_class):
    with pytest.raises(SqlError) as failed:
        lake.execute(sql, Context(now=NOW))
    assert failed.value.error_class == error_class and failed.value.sqlstate == "22012"


def test_current_date_needs_the_world_clock(lake):
    with pytest.raises(Unsupported, match="world clock"):
        lake.execute("select current_date()", Context())


def test_a_date_column_declared_decimal_is_decimal_10_0(lake):
    lake.execute("create or replace table main.default.dec_t (a decimal)", Context())
    lake.execute("insert into main.default.dec_t values (10.5)", Context())
    assert value(lake, "select a from main.default.dec_t") == Decimal("11")


def _covered() -> tuple[set[type], set[str]]:
    classes, names = set(), set()
    sources = [f"select {e}" for e, _ in FUNCTION_CORPUS] + [q for q, _ in QUERY_CORPUS]
    for sql in sources:
        tree = sqlglot.parse_one(sql, read="databricks")
        for node in tree.walk():
            classes.add(type(node))
            if isinstance(node, exp.Anonymous):
                names.add(str(node.this).lower())
    return classes, names


def test_every_vetted_entry_has_a_corpus_case():
    classes, names = _covered()
    missing = sorted(rule.name for cls, rule in {**dbf.FUNCTIONS, **dbf.OPERATORS}.items()
                     if cls not in classes)
    missing += sorted(name for name in dbf.NAMED if name not in names)
    assert not missing, missing


def test_the_vetted_lists_are_what_the_report_says():
    assert dbf.vetted_count() == len(dbf.FUNCTIONS) + len(dbf.NAMED) + len(dbf.OPERATORS)
    assert len(FUNCTION_CORPUS) + len(QUERY_CORPUS) >= dbf.vetted_count()
