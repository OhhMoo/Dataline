"""The vetted Databricks function surface (alkera_gateway.databricks_functions).

FUNCTION_CORPUS is one Databricks SQL expression per line, the value Databricks returns
for it EXACTLY as the Statement Execution API's JSON_ARRAY format carries it (a string:
'40.166667', '1.0E20', '2024-01-01 00:00:00', never a float compared with a tolerance),
and the result's type as the manifest names it ('decimal(22,6)', 'bigint') - the
examples of the Databricks SQL language reference where it gives one ("docs"), and the
documented semantics otherwise (truncating integral CAST, ANSI CAST of strings, Sunday =
1, java.time's whole units in timestampdiff, Spark's substring positions, Spark's
DECIMAL result precision and scale, HALF_UP, ANSI DIVIDE_BY_ZERO). Every entry on the
vetted lists has at least one case here (`test_every_vetted_entry_has_a_corpus_case`),
and everything else is refused naming the function (`REFUSED`).
"""
from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp
from test_databricks_sql import lakehouse_world

from alkera_gateway import databricks_functions as dbf
from alkera_gateway import databricks_sql as dbx
from alkera_gateway.databricks_sql import Context, Lakehouse, SqlError, Unsupported, render

#: The world's clock for current_date/current_timestamp.
NOW = "2026-03-02T09:30:00Z"
FCT = "main.warehouse.fct_orders"
#: fct_orders in lakehouse_world: order_id 1..6, customer_id 10,11,10,12,11,13,
#: order_amount 120.50, 80.00, 45.25, 300.10, NULL, 19.99, statuses completed x4,
#: returned, pending.

FUNCTION_CORPUS: list[tuple[str, str | None, str]] = [
    # CAST: integral targets truncate toward zero (DuckDB would round)
    ("cast(10.50 as int)", "10", "int"),
    ("int(10.5)", "10", "int"),
    ("cast(-10.9 as bigint)", "-10", "bigint"),
    ("bigint('7')", "7", "bigint"),
    ("smallint(3.99)", "3", "smallint"),
    ("tinyint(-3.99)", "-3", "tinyint"),
    ("try_cast(10.7 as int)", "10", "int"),
    ("try_cast('x' as int)", None, "int"),
    ("cast(10.555 as decimal(5,2))", "10.56", "decimal(5,2)"),               # HALF_UP
    ("cast(1.5 as string)", "1.5", "string"),
    ("cast(date'2026-02-01' as string)", "2026-02-01", "string"),
    ("cast('2026-02-01' as date)", "2026-02-01", "date"),
    ("cast('true' as boolean)", "true", "boolean"),
    ("cast(3 as double)", "3.0", "double"),
    ("cast(1.25 as float)", "1.25", "float"),
    ("cast('2026-02-01 10:11:12' as timestamp)", "2026-02-01T10:11:12Z", "timestamp"),
    # days of the week: Sunday is 1 for dayofweek, Monday 0 for weekday
    ("dayofweek(date'2009-07-30')", "5", "int"),                               # docs
    ("dayofweek(date'2026-03-01')", "1", "int"),                               # a Sunday
    ("extract(DAYOFWEEK FROM date'2026-03-01')", "1", "int"),
    ("extract(DOW FROM date'2026-03-07')", "7", "int"),                        # a Saturday
    ("extract(DAYOFWEEK_ISO FROM date'2026-03-01')", "7", "int"),
    ("weekday(date'2009-07-30')", "3", "int"),                                 # docs
    ("weekday(date'2026-03-01')", "6", "int"),
    # date arithmetic: date_add/date_sub return a DATE
    ("date_add(date'2016-07-30', 1)", "2016-07-31", "date"),                # docs
    ("date_sub(date'2016-07-30', 1)", "2016-07-29", "date"),                # docs
    ("date_add(timestamp'2026-02-27 23:30:00', 1)", "2026-02-28", "date"),
    ("add_months(date'2016-08-31', 1)", "2016-09-30", "date"),              # docs
    ("add_months(date'2024-01-31', 1)", "2024-02-29", "date"),
    ("last_day(date'2009-01-12')", "2009-01-31", "date"),                   # docs
    ("timestampadd(MONTH, -1, timestamp'2022-03-31 00:00:00')",
     "2022-02-28T00:00:00Z", "timestamp"),  # docs
    ("dateadd(MONTH, 1, date'2026-01-31')", "2026-02-28T00:00:00Z", "timestamp"),
    ("timestampadd(QUARTER, 1, date'2026-01-31')", "2026-04-30T00:00:00Z", "timestamp"),
    ("timestampadd(HOUR, 2, timestamp'2026-02-01 10:00:00')", "2026-02-01T12:00:00Z", "timestamp"),
    ("timestampadd(MICROSECOND, 5, timestamp'2022-02-28 00:00:00')",
     "2022-02-28T00:00:00.000005Z", "timestamp"),                                     # docs
    # timestampdiff: whole units, the end date stepping back when its time is earlier
    ("timestampdiff(MONTH, date'2024-01-31', date'2024-02-29')", "0", "bigint"),
    ("timestampdiff(MONTH, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", "1", "bigint"),                           # docs
    ("timestampdiff(MONTH, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 07:00:00')", "0", "bigint"),
    ("timestampdiff(WEEK, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", "4", "bigint"),                           # docs
    ("timestampdiff(DAY, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", "28", "bigint"),                          # docs
    ("timestampdiff(HOUR, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", "672", "bigint"),                         # docs
    ("timestampdiff(MINUTE, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", "40320", "bigint"),                       # docs
    ("timestampdiff(SECOND, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", "2419200", "bigint"),                     # docs
    ("timestampdiff(MILLISECOND, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", "2419200000", "bigint"),                  # docs
    ("timestampdiff(MICROSECOND, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", "2419200000000", "bigint"),               # docs
    ("timestampdiff(YEAR, timestamp'2021-02-28 08:00:00', "
     "timestamp'2021-03-28 08:00:00')", "0", "bigint"),                           # docs
    ("timestampdiff(YEAR, date'2020-02-29', date'2021-02-28')", "0", "bigint"),
    ("timestampdiff(QUARTER, date'2026-01-15', date'2026-07-14')", "1", "bigint"),
    ("timestampdiff(DAY, timestamp'2024-01-01 23:00:00', timestamp'2024-01-02 01:00:00')",
     "0", "bigint"),
    ("timestampdiff(MONTH, date'2024-03-31', date'2024-02-29')", "-1", "bigint"),
    ("datediff(MONTH, date'2024-01-31', date'2024-02-29')", "0", "bigint"),
    ("datediff(date'2009-07-31', date'2009-07-30')", "1", "int"),              # docs
    ("datediff(timestamp'2024-01-02 01:00:00', timestamp'2024-01-01 23:00:00')", "1", "int"),
    # truncation: date_trunc returns a TIMESTAMP, trunc a DATE
    ("date_trunc('YEAR', '2015-03-05T09:32:05.359')", "2015-01-01T00:00:00Z", "timestamp"),  # docs
    ("date_trunc('MM', '2015-03-05T09:32:05.359')", "2015-03-01T00:00:00Z", "timestamp"),    # docs
    ("date_trunc('DD', '2015-03-05T09:32:05.359')", "2015-03-05T00:00:00Z", "timestamp"),    # docs
    ("date_trunc('HOUR', '2015-03-05T09:32:05.359')", "2015-03-05T09:00:00Z", "timestamp"),  # docs
    ("date_trunc('QUARTER', date'2026-05-17')", "2026-04-01T00:00:00Z", "timestamp"),
    ("date_trunc('WEEK', date'2026-03-04')", "2026-03-02T00:00:00Z", "timestamp"),
    ("trunc(date'2019-08-04', 'week')", "2019-07-29", "date"),               # docs
    ("trunc(date'2019-08-04', 'quarter')", "2019-07-01", "date"),             # docs
    ("trunc(date'2009-02-12', 'MM')", "2009-02-01", "date"),                  # docs
    ("trunc(date'2015-10-27', 'YEAR')", "2015-01-01", "date"),                # docs
    # fields
    ("year(date'2016-07-30')", "2016", "int"),                                  # docs
    ("quarter(date'2016-08-31')", "3", "int"),                                  # docs
    ("month(date'2016-07-30')", "7", "int"),                                    # docs
    ("day(date'2009-07-30')", "30", "int"),                                     # docs
    ("dayofmonth(date'2009-07-30')", "30", "int"),                              # docs
    ("dayofyear(date'2016-04-09')", "100", "int"),                              # docs
    ("weekofyear(date'2008-02-20')", "8", "int"),                               # docs
    ("hour(timestamp'2009-07-30 12:58:59')", "12", "int"),                      # docs
    ("minute(timestamp'2009-07-30 12:58:59')", "58", "int"),                    # docs
    ("second(timestamp'2009-07-30 12:58:59')", "59", "int"),                    # docs
    ("extract(YEAR FROM timestamp'2019-08-12 01:00:00.123456')", "2019", "int"),   # docs
    ("extract(week FROM timestamp'2019-08-12 01:00:00.123456')", "33", "int"),     # docs
    ("extract(doy FROM date'2019-08-12')", "224", "int"),                          # docs
    ("extract(SECONDS FROM timestamp'2019-08-12 01:00:00.123456')",
     "0.123456", "decimal(8,6)"),                                                # docs
    ("extract(minute FROM timestamp'2019-08-12 01:00:00.123456')", "0", "int"),    # docs
    ("extract(hour FROM timestamp'2019-08-12 01:00:00.123456')", "1", "int"),      # docs
    ("extract(QUARTER FROM date'2026-05-17')", "2", "int"),
    ("extract(MONTH FROM date'2026-05-17')", "5", "int"),
    ("extract(DAY FROM date'2026-05-17')", "17", "int"),
    # parsing and formatting
    ("to_date('2009-07-30 04:17:52')", "2009-07-30", "date"),                 # docs
    ("to_date('2016-12-31', 'yyyy-MM-dd')", "2016-12-31", "date"),           # docs
    ("to_timestamp('2016-12-31 00:12:00')", "2016-12-31T00:12:00Z", "timestamp"),    # docs
    ("to_timestamp('2016-12-31', 'yyyy-MM-dd')", "2016-12-31T00:00:00Z", "timestamp"),      # docs
    ("date_format(date'2016-04-08', 'y')", "2016", "string"),                     # docs
    ("date_format(timestamp'2016-04-08 10:11:12', 'yyyy-MM-dd HH:mm:ss')",
     "2016-04-08 10:11:12", "string"),
    # the world's clock
    ("current_date()", "2026-03-02", "date"),
    ("current_timestamp()", "2026-03-02T09:30:00Z", "timestamp"),
    ("now()", "2026-03-02T09:30:00Z", "timestamp"),
    # strings
    ("substring('Spark SQL', 5)", "k SQL", "string"),                             # docs
    ("substring('Spark SQL', -3)", "SQL", "string"),                              # docs
    ("substring('Spark SQL', 5, 1)", "k", "string"),                              # docs
    ("substring('Spark SQL', 0, 3)", "Spa", "string"),
    ("substr('Spark SQL', -3, 2)", "SQ", "string"),
    ("substring('abc', -5, 3)", "a", "string"),
    ("left('Spark SQL', 3)", "Spa", "string"),                                    # docs
    ("left('Spark', -1)", "", "string"),
    ("right('Spark SQL', 3)", "SQL", "string"),                                   # docs
    ("right('Spark', 0)", "", "string"),
    ("right('ab', 5)", "ab", "string"),
    ("lower('SparkSql')", "sparksql", "string"),                                  # docs
    ("upper('SparkSql')", "SPARKSQL", "string"),                                  # docs
    ("length('Spark SQL ')", "10", "int"),                                       # docs
    ("trim('    SparkSQL   ')", "SparkSQL", "string"),                            # docs
    ("trim(BOTH 'SL' FROM 'SSparkSQLS')", "parkSQ", "string"),                    # docs
    ("ltrim('    SparkSQL   ')", "SparkSQL   ", "string"),                        # docs
    ("rtrim('    SparkSQL   ')", "    SparkSQL", "string"),                       # docs
    ("concat('Spark', 'SQL')", "SparkSQL", "string"),                             # docs
    ("concat('a', NULL)", None, "string"),
    ("concat_ws(' ', 'Spark', 'SQL')", "Spark SQL", "string"),                    # docs
    ("concat_ws('-', 'a', NULL, 'b')", "a-b", "string"),
    ("replace('ABCabc', 'abc', 'DEF')", "ABCDEF", "string"),                      # docs
    ("regexp_replace('100-200', '[0-9]+', 'num')", "num-num", "string"),
    ("regexp_extract('100-200', '([0-9]+)-([0-9]+)', 1)", "100", "string"),
    ("regexp_extract('100-200', '([0-9]+)-([0-9]+)')", "100", "string"),
    ("regexp_extract('100-200', '([0-9]+)-([0-9]+)', 2)", "200", "string"),
    ("'Spark' rlike 'S.*k'", "true", "boolean"),
    ("'Spark' like 'S%'", "true", "boolean"),
    ("'Spark' like 'S_ark'", "true", "boolean"),
    ("'Spark' ilike 's%'", "true", "boolean"),
    ("lpad('hi', 5, '??')", "???hi", "string"),                                   # docs
    ("lpad('hi', 1, '??')", "h", "string"),                                       # docs
    ("rpad('hi', 5, '??')", "hi???", "string"),                                   # docs
    ("initcap('sPark sql')", "Spark Sql", "string"),                              # docs
    ("instr('SparkSQL', 'SQL')", "6", "int"),                                    # docs
    ("locate('bar', 'foobarbar')", "4", "int"),                                  # docs
    ("'Spark' || 'SQL'", "SparkSQL", "string"),
    # conditionals
    ("if(1 < 2, 'a', 'b')", "a", "string"),
    ("iff(1 > 2, 'a', 'b')", "b", "string"),
    ("case when 1 > 2 then 'a' else 'b' end", "b", "string"),
    ("coalesce(NULL, 1, NULL)", "1", "int"),                                     # docs
    ("nvl(NULL, 2)", "2", "int"),
    ("ifnull(NULL, 3)", "3", "int"),
    ("nullif(2, 2)", None, "int"),                                             # docs
    ("nvl2(NULL, 2, 1)", "1", "int"),                                            # docs
    ("greatest(10, 9, 2, 4, 3)", "10", "int"),                                   # docs
    ("least(10, 9, 2, 4, 3)", "2", "int"),                                       # docs
    ("greatest(1, NULL, 3)", "3", "int"),
    ("1 <=> NULL", "false", "boolean"),
    ("NULL <=> NULL", "true", "boolean"),
    ("2 between 1 and 3", "true", "boolean"),
    ("2 in (1, 3)", "false", "boolean"),
    ("NULL is null", "true", "boolean"),
    ("not true", "false", "boolean"),
    # math
    ("abs(-1)", "1", "int"),                                                     # docs
    ("round(2.5, 0)", "3", "decimal(2,0)"),                                               # docs
    ("round(25, -1)", "30", "int"),                                              # docs
    ("round(-2.5)", "-3", "decimal(2,0)"),
    ("round(cast(2.675 as double), 2)", "2.68", "double"),
    ("floor(-0.1)", "-1", "decimal(1,0)"),                                                # docs
    ("ceil(-0.1)", "0", "decimal(1,0)"),                                                  # docs
    ("ceiling(5)", "5", "bigint"),
    ("sqrt(4)", "2.0", "double"),                                                   # docs
    ("power(2, 3)", "8.0", "double"),                                               # docs
    ("pow(2, 3)", "8.0", "double"),
    ("ln(1)", "0.0", "double"),                                                     # docs
    ("log(10, 100)", "2.0", "double"),                                              # docs
    ("log10(10)", "1.0", "double"),                                                 # docs
    ("log2(2)", "1.0", "double"),                                                   # docs
    ("exp(0)", "1.0", "double"),                                                    # docs
    ("signum(40)", "1.0", "double"),                                                # docs
    ("sign(-3)", "-1.0", "double"),
    ("7 / 2", "3.5", "double"),
    ("7 div 2", "3", "bigint"),
    ("-7 div 2", "-3", "bigint"),
    ("-7 % 2", "-1", "int"),
    ("2 * 3 - 1 + -4", "1", "int"),
    # arrays
    ("array(1, 2)", "[1,2]", "array<int>"),
    ("size(array(1, 2))", "2", "int"),
    ("array_contains(array(1, 2), 2)", "true", "boolean"),
    # the session
    ("current_catalog()", "main", "string"),
    ("current_schema()", "default", "string"),
    ("current_database()", "default", "string"),
    ("current_user()", "alkera-local-dev", "string"),
    ("session_user()", "alkera-local-dev", "string"),
    ("user()", "alkera-local-dev", "string"),
    ("length(current_version()) > 10", "true", "boolean"),
    ("'a\\tb'", 'a\tb', 'string'),                       # a Spark escape sqlglot unescapes: TAB
    # Spark's result types and rounding: DECIMAL arithmetic in Spark's precision and
    # scale (division exact, HALF_UP), div a BIGINT, a DOUBLE as Java writes it
    ("1.0 / 3", "0.333333", "decimal(7,6)"),
    ("1 / 3", "0.3333333333333333", "double"),
    ("cast(120.50 as decimal(18,2)) / 3", "40.166667", "decimal(22,6)"),
    ("cast(2 as decimal(10,0)) / cast(3 as decimal(10,0))", "0.66666666667",
     "decimal(21,11)"),
    ("2.5 * 1.1", "2.75", "decimal(5,2)"),
    ("1.5 + 2", "3.5", "decimal(3,1)"),
    ("7.5 % 2", "1.5", "decimal(2,1)"),
    ("cast(NULL as int) / 0", None, "double"),
    ("-5.9 div 1", "-5", "bigint"),                                    # docs
    ("3 div 2", "1", "bigint"),                                        # docs
    ("120.50 div 10", "12", "bigint"),
    ("0.3 div 0.1", "3", "bigint"),                                    # exact, not 2.999..
    ("cast(7.5 as double) div 2", "3", "bigint"),
    ("round(0.125, 2)", "0.13", "decimal(3,2)"),
    ("round(1.25, 1)", "1.3", "decimal(3,1)"),
    ("round(-1.25, 1)", "-1.3", "decimal(3,1)"),
    ("round(cast(1.005 as double), 2)", "1.01", "double"),             # on 1.005, not 1.00499..
    ("round(cast(25 as double), -1)", "30.0", "double"),
    ("floor(-1.5)", "-2", "decimal(2,0)"),
    ("floor(cast(-1.5 as double))", "-2", "bigint"),
    ("ceil(cast(1.2 as double))", "2", "bigint"),
    ("cast(0.9999999999999999999 as int)", "0", "int"),
    ("cast(' 12 ' as int)", "12", "int"),
    ("try_cast('1.9' as int)", None, "int"),
    ("try_cast('1e3' as int)", None, "int"),
    ("cast(true as int)", "1", "int"),
    ("cast(1.1 as float)", "1.1", "float"),
    ("cast(cast(1.1 as float) as double)", "1.100000023841858", "double"),
    ("cast(1e20 as double)", "1.0E20", "double"),
    ("cast(1e20 as string)", "1.0E20", "string"),
    ("cast(cast(1e-7 as double) as string)", "1.0E-7", "string"),
    ("cast(cast(1e7 as double) as string)", "1.0E7", "string"),
    ("cast(cast(100.0 as double) as string)", "100.0", "string"),
    ("cast(cast(1.005 as double) as decimal(4,2))", "1.01", "decimal(4,2)"),
    ("cast('yes' as boolean)", "true", "boolean"),
    ("cast(' F ' as boolean)", "false", "boolean"),
    ("try_cast('maybe' as boolean)", None, "boolean"),
    ("concat('x', cast(1e20 as double))", "x1.0E20", "string"),
    ("'a' || NULL", None, "string"),
    # a TIMESTAMP written as a string has no zone suffix
    ("cast(date_trunc('MONTH', timestamp'2024-01-31 10:00:00') as string)",
     "2024-01-01 00:00:00", "string"),
    ("concat('at ', cast(date'2024-01-31' as timestamp))", "at 2024-01-31 00:00:00", "string"),
    ("cast(timestampadd(DAY, 1, timestamp'2024-01-31 10:00:00') as string)",
     "2024-02-01 10:00:00", "string"),
    ("cast(current_timestamp() as string)", "2026-03-02 09:30:00", "string"),
    ("add_months(timestamp'2024-01-31 10:00:00', 1)", "2024-02-29", "date"),
    # strings and arrays
    ("regexp_replace('abcb', '(b)', '[$1]')", "a[b]c[b]", "string"),
    ("regexp_replace('abc', '(a)(b)', '$2$1')", "bac", "string"),
    ("concat_ws(',', array('a', 'b'))", "a,b", "string"),
    ("concat_ws(',', 'Spark', array('S', 'Q', NULL, 'L'), NULL)", "Spark,S,Q,L", "string"),
    ("concat_ws(NULL, 'a', 'b')", None, "string"),
    ("lpad('abc', 2, 'x')", "ab", "string"),
    ("lpad('abc', 5)", "  abc", "string"),
    ("lpad('abc', 5, '')", "abc", "string"),
    ("rpad('abc', 5, 'xy')", "abcxy", "string"),
    ("replace('abc', 'b')", "ac", "string"),
    ("array_contains(array(1, 2, NULL), 3)", None, "boolean"),
    ("array_contains(array(1, 2, 3), NULL)", None, "boolean"),
    ("size(NULL)", None, "int"),
    ("1 IS DISTINCT FROM NULL", "true", "boolean"),
]

#: Aggregates and windows over fct_orders: (query, value, type).
QUERY_CORPUS: list[tuple[str, str | None, str]] = [
    (f"select count(*) from {FCT}", "6", "bigint"),
    (f"select count(order_amount) from {FCT}", "5", "bigint"),
    (f"select count(distinct order_status) from {FCT}", "3", "bigint"),
    (f"select sum(order_amount) from {FCT}", "565.84", "decimal(28,2)"),
    (f"select avg(order_amount) from {FCT}", "113.168000", "decimal(22,6)"),
    (f"select min(order_amount) from {FCT}", "19.99", "decimal(18,2)"),
    (f"select max(order_amount) from {FCT}", "300.10", "decimal(18,2)"),
    (f"select count_if(order_status = 'completed') from {FCT}", "4", "bigint"),
    (f"select any_value(order_id) from {FCT} where order_id = 1", "1", "int"),
    (f"select stddev(order_id) from {FCT}", "1.8708286933869707", "double"),
    (f"select stddev_samp(order_id) from {FCT}", "1.8708286933869707", "double"),
    (f"select stddev_pop(order_id) from {FCT}", "1.707825127659933", "double"),
    (f"select variance(order_id) from {FCT}", "3.5", "double"),
    (f"select var_samp(order_id) from {FCT}", "3.5", "double"),
    (f"select var_pop(order_id) from {FCT}", "2.9166666666666665", "double"),
    (f"select median(order_id) from {FCT}", "3.5", "double"),
    ("select median(col) from values (0), (10) as tab(col)", "5.0", "double"),                # docs
    ("select percentile(col, 0.3) from values (0), (10) as tab(col)", "3.0", "double"),       # docs
    (f"select percentile(order_id, 0.25) from {FCT}", "2.25", "double"),
    (f"select size(collect_list(order_status)) from {FCT}", "6", "int"),
    (f"select size(collect_set(order_status)) from {FCT}", "3", "int"),
    (f"select bool_and(order_id > 0) from {FCT}", "true", "boolean"),
    (f"select bool_or(order_id > 5) from {FCT}", "true", "boolean"),
    (f"select max_by(order_id, order_amount) from {FCT}", "4", "int"),
    (f"select min_by(order_id, order_amount) from {FCT}", "6", "int"),
    (f"select sum(order_amount) filter (where order_status = 'completed') from {FCT}",
     "520.59", "decimal(28,2)"),
    (f"select max(r) from (select row_number() over (order by order_id) as r from {FCT})",
     "6", "int"),
    (f"select max(r) from (select rank() over (order by customer_id) as r from {FCT})", "6", "int"),
    (f"select max(r) from (select dense_rank() over (order by customer_id) as r from {FCT})",
     "4", "int"),
    (f"select max(r) from (select ntile(2) over (order by order_id) as r from {FCT})", "2", "int"),
    (f"select max(r) from (select percent_rank() over (order by order_id) as r from {FCT})",
     "1.0", "double"),
    (f"select min(r) from (select cume_dist() over (order by customer_id) as r from {FCT})",
     "0.3333333333333333", "double"),
    (f"select l from (select order_id, lag(order_id) over (order by order_id) as l "
     f"from {FCT}) where order_id = 3", "2", "int"),
    (f"select l from (select order_id, lead(order_id, 1, 0) over (order by order_id) as l "
     f"from {FCT}) where order_id = 6", "0", "int"),
    (f"select f from (select order_id, first_value(order_id) over (order by order_id) "
     f"as f from {FCT}) where order_id = 3", "1", "int"),
    (f"select f from (select order_id, last_value(order_id) over (order by order_id) "
     f"as f from {FCT}) where order_id = 3", "3", "int"),
    (f"select count(*) from {FCT} where exists (select 1 from {FCT} where order_id = 1)",
     "6", "bigint"),
    (f"select count(*) from {FCT} where order_id = any (select order_id from {FCT} "
     f"where order_id < 3)", "2", "bigint"),
    # DECIMAL aggregates in Spark's types: avg(DECIMAL(18,2)) is DECIMAL(22,6), the
    # DECIMAL(28,2) sum over a BIGINT count DECIMAL(38,12)
    (f"select avg(order_amount) from {FCT} where order_id in (1, 3)", "82.875000",
     "decimal(22,6)"),
    (f"select sum(order_amount) / count(*) from {FCT}", "94.306666666667", "decimal(38,12)"),
    (f"select sum(order_amount) / 3 from {FCT} where order_id in (1, 3)", "55.250000",
     "decimal(32,6)"),
    (f"select order_amount / 3 from {FCT} where order_id = 1", "40.166667", "decimal(22,6)"),
    (f"select order_amount * 1.1 from {FCT} where order_id = 1", "132.550", "decimal(21,3)"),
    (f"select order_amount div 10 from {FCT} where order_id = 1", "12", "bigint"),
    (f"select round(avg(order_amount), 2) from {FCT}", "113.17", "decimal(19,2)"),
    (f"select sum(order_amount) / 0 from {FCT} where order_id > 99", None, "decimal(32,6)"),
    (f"select avg(order_id) from {FCT}", "3.5", "double"),
    (f"select sum(order_id) from {FCT}", "21", "bigint"),
    (f"select median(order_amount) from {FCT}", "80.0", "double"),
    (f"select a from (select order_id, avg(order_amount) over (partition by customer_id) "
     f"as a from {FCT}) where order_id = 1", "82.875000", "decimal(22,6)"),
    (f"select s from (select order_id, sum(order_amount) over (order by order_id) as s "
     f"from {FCT}) where order_id = 2", "200.50", "decimal(28,2)"),
    # collect_list/collect_set over no rows (or only NULLs) is [], not NULL
    (f"select collect_list(order_id) from {FCT} where order_id > 99", "[]", "array<int>"),
    (f"select collect_set(order_id) from {FCT} where order_id > 99", "[]", "array<int>"),
    (f"select size(collect_list(order_id)) from {FCT} where order_id > 99", "0", "int"),
    (f"select c from (select order_id, collect_list(order_amount) over (order by order_id "
     f"rows between current row and current row) as c from {FCT}) where order_id = 5",
     "[]", "array<decimal(18,2)>"),
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
    ("select date'2026-03-01' - date'2026-02-01'", "subtracting DATEs or TIMESTAMPs"),
    ("select regexp_replace('a', '(a)', '${x}')", "named or malformed group reference"),
    ("select regexp_replace('a', '(a)', '$10')", "group number above 9"),
    (f"select regexp_replace('a', 'a', order_status) from {FCT}", "not a string literal"),
    ("create sequence s", "CREATE SEQUENCE statements"),
    ("create temporary view v as select 1", "TEMPORARY views and tables"),
    ("create schema main", "schema name `main`"),
    ("drop schema information_schema", "schema name `information_schema`"),
    ("set variable x = 1", "SET VARIABLE"),
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


def value(lake: Lakehouse, sql: str) -> tuple[str | None, str]:
    """The one cell `sql` answers, as the JSON_ARRAY format carries it (the service's
    own rendering), and its column's type as the manifest names it."""
    result, _ = lake.execute(sql, Context(now=NOW))
    assert len(result.rows) == 1 and len(result.rows[0]) == 1, result.rows
    column = result.columns[0]
    return render(result.rows[0][0], column), column.type_text


@pytest.mark.parametrize("expr,expected,type_text", FUNCTION_CORPUS)
def test_function_corpus_matches_databricks(lake, expr, expected, type_text):
    # exact: the value as Databricks renders it (scale, '1.0E20', no '+00') AND its type
    assert value(lake, f"select {expr}") == (expected, type_text), expr


@pytest.mark.parametrize("sql,expected,type_text", QUERY_CORPUS)
def test_query_corpus_matches_databricks(lake, sql, expected, type_text):
    assert value(lake, sql) == (expected, type_text), sql


@pytest.mark.parametrize("sql,name", REFUSED)
def test_anything_unvetted_is_refused_by_name(lake, sql, name):
    with pytest.raises(Unsupported) as refused:
        lake.execute(sql, Context(now=NOW))
    assert name in refused.value.feature, (sql, refused.value.feature)


@pytest.mark.parametrize("sql,error_class,sqlstate", [
    ("select 1 / 0", "DIVIDE_BY_ZERO", "22012"),
    ("select 7 div 0", "DIVIDE_BY_ZERO", "22012"),
    ("select 7 % 0", "DIVIDE_BY_ZERO", "22012"),
    ("select 1.5 / 0", "DIVIDE_BY_ZERO", "22012"),
    ("select 1.5 div 0", "DIVIDE_BY_ZERO", "22012"),
    (f"select sum(order_amount) / count(*) - 1 / (order_id - order_id) from {FCT} "
     "group by order_id", "DIVIDE_BY_ZERO", "22012"),
    # ANSI CAST: a string that is not an integer is malformed, not truncated
    ("select cast('1.9' as int)", "CAST_INVALID_INPUT", "22018"),
    ("select cast('1e3' as int)", "CAST_INVALID_INPUT", "22018"),
    ("select cast('maybe' as boolean)", "CAST_INVALID_INPUT", "22018"),
    ("select 2147483647 + 1", "ARITHMETIC_OVERFLOW", "22003"),
])
def test_errors_raise_as_an_ansi_warehouse_does(lake, sql, error_class, sqlstate):
    with pytest.raises(SqlError) as failed:
        lake.execute(sql, Context(now=NOW))
    assert (failed.value.error_class, failed.value.sqlstate) == (error_class, sqlstate)


@pytest.mark.parametrize("sql", [
    "select a / b from t", "select avg(a) from t", "select a div b from t",
    "select round(a, 2) from t", "select floor(a) from t",
    "select concat_ws(',', a) from t",
])
def test_typed_rewrites_refuse_what_they_cannot_type(sql):
    """Without the lakehouse's column types, the rewrites that need Spark's result type
    refuse by name rather than answer in DuckDB's."""
    with pytest.raises(Unsupported, match="unknown type"):
        dbx.translate(sql, Context())


def test_a_missing_table_is_not_found_rather_than_untyped(lake):
    with pytest.raises(SqlError) as failed:
        lake.execute("select avg(x) from main.default.nope", Context(now=NOW))
    assert failed.value.error_class == "TABLE_OR_VIEW_NOT_FOUND"


def test_current_date_needs_the_world_clock(lake):
    with pytest.raises(Unsupported, match="world clock"):
        lake.execute("select current_date()", Context())


def test_a_date_column_declared_decimal_is_decimal_10_0(lake):
    lake.execute("create or replace table main.default.dec_t (a decimal)", Context())
    lake.execute("insert into main.default.dec_t values (10.5)", Context())
    assert value(lake, "select a from main.default.dec_t") == ("11", "decimal(10,0)")


def _covered() -> tuple[set[type], set[str]]:
    classes, names = set(), set()
    sources = [f"select {e}" for e, *_ in FUNCTION_CORPUS] + [q for q, *_ in QUERY_CORPUS]
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
