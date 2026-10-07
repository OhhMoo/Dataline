"""Redshift SQL -> PostgreSQL (alkera_gateway.redshift_sql): translations and fences.

    gateway/.venv/bin/python -m pytest gateway/tests/test_redshift_sql.py -q

Most of this is pure translation. The corpus test runs the fixed corpus
(redshift_corpus.CORPUS) through a LIVE persona when one is named:

    ALKERA_REDSHIFT_TEST_PROXY=127.0.0.1:5439       the proxy, as host:port
    ALKERA_REDSHIFT_TEST_WAREHOUSE=<warehouse.duckdb>   the world it was seeded from

and is skipped otherwise - this host has no PostgreSQL. The same corpus runs
inside the episode, through redshift_connector, in every redshift_e2e_check.py
(evidence: tasks/harness/evidence/redshift_e2e.json, `corpus`).
"""
from __future__ import annotations

import os

import pytest
import redshift_corpus
import sqlglot

from alkera_gateway import redshift_sql
from alkera_gateway.redshift_sql import Unsupported, translate


def refused(sql: str) -> str:
    with pytest.raises(Unsupported) as raised:
        translate(sql)
    assert str(raised.value) == \
        f"Redshift feature not available in this environment: {raised.value.feature}"
    assert raised.value.sqlstate == "0A000"
    return raised.value.feature


# ------------------------------------------------------------- accepted, no effect
def test_distkey_sortkey_diststyle_encode_accepted_and_stripped():
    assert translate("CREATE TABLE t (a int ENCODE az64) DISTKEY(a) SORTKEY(a)") == \
        "CREATE TABLE t (a INT)"
    assert translate("CREATE TABLE t (a int ENCODE az64, b varchar(10) ENCODE lzo) "
                     "DISTSTYLE KEY DISTKEY(a) INTERLEAVED SORTKEY(a, b) BACKUP NO") == \
        "CREATE TABLE t (a INT, b VARCHAR(10))"
    assert translate("create table t (a int distkey sortkey) diststyle auto sortkey auto") == \
        "CREATE TABLE t (a INT)"
    # CTAS: the attributes go, the query (whose column may be called distkey) stays
    assert translate("CREATE TEMP TABLE t DISTSTYLE ALL ENCODE AUTO AS SELECT 1 AS distkey") \
        == "CREATE TEMPORARY TABLE t AS SELECT 1 AS distkey"
    assert translate("CREATE VIEW v AS SELECT 1 AS a WITH NO SCHEMA BINDING") == \
        "CREATE VIEW v AS SELECT 1 AS a"


def test_set_query_group_is_a_noop():
    # a placeholder setting Postgres accepts and nothing reads
    assert translate("SET query_group TO 'etl'") == "SET alkera.query_group TO 'etl'"
    assert translate("set query_group = 'etl'") == "set alkera.query_group = 'etl'"
    assert translate("RESET query_group") == "RESET alkera.query_group"
    assert translate("SET wlm_query_slot_count TO 2") == "SET alkera.wlm_query_slot_count TO 2"
    # Postgres' own settings are Postgres'
    assert translate("SET search_path TO warehouse") == "SET search_path TO warehouse"
    # a setting that changes what a query returns is never ignored
    assert refused("SET enable_case_sensitive_identifier TO true") == \
        "SET enable_case_sensitive_identifier"


# ------------------------------------------------------------- refused by name
def test_unload_refused_by_name():
    assert refused("UNLOAD ('select * from warehouse.fct_orders') TO 's3://bucket/fct_' "
                   "IAM_ROLE 'arn:aws:iam::1:role/x' PARQUET") == "UNLOAD"


def test_copy_from_s3_refused_by_name():
    assert refused("COPY warehouse.fct_orders FROM 's3://bucket/orders/' "
                   "IAM_ROLE 'arn:aws:iam::1:role/x' FORMAT AS CSV") == "COPY FROM S3"
    assert refused("COPY t FROM STDIN") == "COPY FROM STDIN"
    assert refused("copy t from 'dynamodb://orders' readratio 50") == "COPY FROM DYNAMODB"


def test_super_and_partiql_refused_by_name():
    assert refused("CREATE TABLE events (payload SUPER)") == "SUPER"
    assert refused("SELECT JSON_PARSE('{\"a\": 1}')") == "SUPER (JSON_PARSE)"
    assert refused("SELECT c.payload.items[0] FROM events c") == "PartiQL (SUPER navigation)"
    assert refused("SELECT i FROM events AS c, c.payload.items AS i") == \
        "PartiQL (FROM-clause unnesting)"
    # Postgres array indexing, which redshift_connector's own catalog queries
    # use - 1-based on both sides (sqlglot would shift it, reading PartiQL's 0)
    catalog = ("SELECT nspname FROM pg_catalog.pg_namespace "
               "WHERE nspname = (pg_catalog.current_schemas(true))[1]")
    assert "current_schemas(TRUE))[1]" in translate(catalog)


def test_external_schema_refused_by_name():
    assert refused("CREATE EXTERNAL SCHEMA spectrum FROM DATA CATALOG DATABASE 'lake' "
                   "IAM_ROLE 'arn:aws:iam::1:role/x'") == "EXTERNAL SCHEMA"
    assert refused("CREATE EXTERNAL TABLE spectrum.t (a int) LOCATION 's3://b/t/'") == \
        "EXTERNAL TABLE"
    assert refused("CREATE MODEL m FROM t TARGET y FUNCTION f IAM_ROLE default") == \
        "CREATE MODEL (Redshift ML)"
    assert refused("ALTER TABLE t ALTER DISTKEY a") == "ALTER TABLE ... ALTER DISTKEY"
    assert refused("ALTER TABLE t APPEND FROM s") == "ALTER TABLE APPEND"


def test_unverified_translation_is_refused_not_guessed():
    # sqlglot renders REGEXP_SUBSTR as REGEXP_EXTRACT and STRTOL as FROM_BASE:
    # neither is vetted here, so neither reaches Postgres in a guessed spelling
    assert "REGEXP" in refused("SELECT REGEXP_SUBSTR(order_status, 'c.*') FROM t")
    assert "FROM_BASE" in refused("SELECT STRTOL('ff', 16)")
    # DATEDIFF on a unit whose boundary rule is not pinned
    assert refused("SELECT DATEDIFF(week, a, b) FROM t") == "DATEDIFF(week)"


# ------------------------------------------------------------- translated
def test_nothing_to_translate_is_the_clients_own_bytes():
    for sql in ("select sum(order_amount) from warehouse.fct_orders where order_status='completed'",
                "SELECT 1", "", "/* a comment */",
                "SELECT n.nspname FROM pg_catalog.pg_namespace n ORDER BY 1"):
        assert translate(sql) is sql


def test_redshift_spellings_become_postgres():
    assert translate("SELECT NVL(a, 0) FROM t") == "SELECT COALESCE(a, 0) FROM t"
    assert translate("SELECT TOP 5 a FROM t ORDER BY a") == "SELECT a FROM t ORDER BY a LIMIT 5"
    assert translate("SELECT LEN(x), CHARINDEX('b', x) FROM t") == \
        "SELECT LENGTH(x), POSITION('b' IN x) FROM t"
    assert translate("SELECT GETDATE(), SYSDATE") == \
        "SELECT CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"
    assert translate("SELECT a::varchar(max) FROM t") == "SELECT CAST(a AS VARCHAR(65535)) FROM t"
    assert translate("SELECT APPROXIMATE COUNT(DISTINCT x) FROM t") == \
        "SELECT COUNT(DISTINCT x) FROM t"
    assert translate("SELECT version()") == f"SELECT '{redshift_sql.VERSION}' AS version"


def test_listagg_becomes_string_agg_with_its_order():
    assert translate("SELECT LISTAGG(s, ',') WITHIN GROUP (ORDER BY s) FROM t") == \
        "SELECT STRING_AGG(s, ',' ORDER BY s) FROM t"
    assert translate("SELECT LISTAGG(DISTINCT s, '|') WITHIN GROUP (ORDER BY s) FROM t") == \
        "SELECT STRING_AGG(DISTINCT s, '|' ORDER BY s) FROM t"
    # Redshift's default delimiter is none at all; Postgres needs it spelled out
    assert translate("SELECT LISTAGG(s) FROM t") == "SELECT STRING_AGG(s, '') FROM t"


def test_concat_keeps_redshift_null_semantics():
    # Postgres CONCAT reads NULL as ''; Redshift CONCAT is `||` and yields NULL
    assert translate("SELECT CONCAT(a, 'b') FROM t") == "SELECT a || 'b' FROM t"


def test_datediff_counts_boundaries():
    day = translate("SELECT DATEDIFF(day, a, b) FROM t")
    assert day == "SELECT CAST((CAST(b AS DATE) - CAST(a AS DATE)) AS BIGINT) FROM t"
    month = translate("SELECT DATEDIFF(month, a, b) FROM t")
    assert "AGE(" not in month and "EXTRACT(MONTH FROM CAST(b AS TIMESTAMP))" in month
    assert "* 12" in month
    assert "DATE_TRUNC('HOUR'" in translate("SELECT DATEDIFF(hour, a, b) FROM t")


def test_add_months_keeps_the_month_end_rule():
    sql = translate("SELECT ADD_MONTHS(d, 1) FROM t")
    assert sql.startswith("SELECT CASE WHEN CAST(CAST(d AS TIMESTAMP) AS DATE) = ")
    # and DATEADD keeps its own (clamping) rule
    assert translate("SELECT DATEADD(month, 1, d) FROM t") == \
        "SELECT CAST(d AS TIMESTAMP) + INTERVAL '1 MONTH' FROM t"


def test_backslash_escapes_are_redshifts():
    # Redshift processes backslash escapes in a literal (as PostgreSQL 8.0 did);
    # the Postgres side gets a standard string with the same value
    assert translate(r"SELECT 'a\nb'") == "SELECT 'a\nb'"
    assert translate(r"SELECT 'c:\\dir'") == r"SELECT 'c:\dir'"


def test_several_statements_translate_one_by_one():
    assert translate("SELECT 1; SELECT TOP 1 a FROM t;") == "SELECT 1;\nSELECT a FROM t LIMIT 1"
    with pytest.raises(Unsupported):
        translate("SELECT 1; UNLOAD ('select 1') TO 's3://b/k' IAM_ROLE 'r'")


def test_every_corpus_query_translates_to_postgres_sql():
    for name, sql, _ in redshift_corpus.CORPUS:
        out = translate(sql)
        sqlglot.parse_one(out, read="postgres")        # valid Postgres SQL
        for word in ("GETDATE", "DATEADD", "DATEDIFF", "NVL(", "LISTAGG", " TOP ",
                     "DECODE", "MEDIAN"):
            assert word not in out.upper(), (name, word, out)


def test_vetted_and_handled_names_are_sqlglot_classes():
    from sqlglot import exp

    for name in redshift_sql.VETTED | redshift_sql.HANDLED | redshift_sql.STRUCTURAL:
        assert isinstance(getattr(exp, name, None), type), name


# ------------------------------------------------------------- against a live persona
LIVE = os.environ.get("ALKERA_REDSHIFT_TEST_PROXY")
WAREHOUSE = os.environ.get("ALKERA_REDSHIFT_TEST_WAREHOUSE")


@pytest.mark.skipif(not (LIVE and WAREHOUSE),
                    reason="no live persona named (ALKERA_REDSHIFT_TEST_PROXY / _WAREHOUSE); "
                           "the corpus runs in tasks/harness/redshift_e2e_check.py")
def test_translation_matches_duckdb_truth():
    import psycopg2

    host, _, port = LIVE.partition(":")
    truth = redshift_corpus.truths(WAREHOUSE)
    conn = psycopg2.connect(host=host, port=int(port), dbname="dev", user="alkera",
                            password="alkera-local-dev", sslmode="disable")
    conn.autocommit = True
    try:
        cur = conn.cursor()
        got = {}
        for name, sql, _ in redshift_corpus.CORPUS:
            cur.execute(sql)
            got[name] = cur.fetchone()[0]
    finally:
        conn.close()
    wrong = {n: (got[n], truth[n]) for n in got if not redshift_corpus.same(got[n], truth[n])}
    assert not wrong, wrong
    assert len(got) == 12
