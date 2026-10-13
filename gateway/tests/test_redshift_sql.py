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
    # GETDATE() is the statement's start in whole seconds, SYSDATE the
    # transaction's; both TIMESTAMP. CURRENT_TIMESTAMP stays Postgres'.
    assert translate("SELECT GETDATE(), SYSDATE, CURRENT_TIMESTAMP") == \
        "SELECT CAST(DATE_TRUNC('SECOND', STATEMENT_TIMESTAMP()) AS TIMESTAMP), " \
        "CAST(TRANSACTION_TIMESTAMP() AS TIMESTAMP), CURRENT_TIMESTAMP"
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
    # an unknown escape drops its backslash: LIKE 'pg\_%' is LIKE 'pg_%' there
    assert translate(r"SELECT 'a\_b'") == "SELECT 'a_b'"
    assert translate(r"SELECT 'a\vb'") == "SELECT 'avb'"       # no \v in 8.0
    assert translate(r"SELECT x FROM t WHERE y LIKE 'pg\_%'") == \
        "SELECT x FROM t WHERE y LIKE 'pg_%'"
    # octal and hex escapes are refused, not guessed
    assert refused(r"SELECT 'x\101'") == "an octal or hex escape in a string literal"
    assert refused(r"SELECT 'x\x41'") == "an octal or hex escape in a string literal"


def test_untokenizable_text_is_refused_not_passed_through():
    # Redshift reads 'a\' as an unterminated string; Postgres (standard
    # strings) would end it at the quote and run the COPY after it
    with pytest.raises(Unsupported) as raised:
        translate("SELECT 'a\\'; COPY t FROM STDIN")
    assert raised.value.sqlstate == "42601"
    assert str(raised.value).startswith("syntax error (read as Redshift SQL): ")
    # and a text sqlglot cannot parse is a syntax error, not the server's to read
    with pytest.raises(redshift_sql.Unparsable):
        translate("SELECT a AS b FROM t WHERE")
    # a nested comment is read as Postgres reads it: the COPY after it is seen
    assert refused("/* a /* nested */ comment */ COPY t FROM STDIN") == "COPY FROM STDIN"


def test_settings_that_change_how_text_is_read_are_refused_wherever_named():
    for sql in ("SET standard_conforming_strings = off",
                "SET SESSION backslash_quote TO on",
                "SELECT set_config('standard_conforming_strings', 'off', false)",
                "ALTER ROLE alkera SET standard_conforming_strings = off",
                "ALTER DATABASE dev SET backslash_quote = on"):
        assert refused(sql).startswith("SET "), sql
    for sql in ("SET client_encoding TO 'SJIS'", "SET NAMES 'BIG5'",
                "SELECT set_config('client_encoding', 'SJIS', false)",
                "ALTER ROLE alkera SET client_encoding = 'SJIS'"):
        assert refused(sql) == "a client_encoding other than UTF8", sql
    assert translate("SET client_encoding TO 'UTF8'") == "SET client_encoding TO 'UTF8'"
    assert translate("SELECT current_setting('client_encoding')") == \
        "SELECT current_setting('client_encoding')"


def test_startup_parameters_are_filtered_like_set():
    keep = {"user": "alkera", "database": "dev"}
    assert redshift_sql.startup_parameters(keep) == keep
    out = redshift_sql.startup_parameters(
        {**keep, "options": "-c query_group=etl --search_path=warehouse -cdatestyle=iso"})
    assert out["options"] == "-c search_path=warehouse -c datestyle=iso"
    assert "options" not in redshift_sql.startup_parameters({**keep, "options": "-c auto_mv=1"})
    for options, feature in (("-c standard_conforming_strings=off",
                              "SET standard_conforming_strings"),
                             ("--backslash-quote=on", "SET backslash_quote"),
                             ("-c client_encoding=SJIS", "a client_encoding other than UTF8"),
                             ("-d 5", "startup option '-d'"),
                             # Postgres drops these backslashes when it splits options
                             ("-c standard\\_conforming_strings=off",
                              "a backslash in the startup options"),
                             ("-c backslash_quote\\=on", "a backslash in the startup options")):
        with pytest.raises(Unsupported) as raised:
            redshift_sql.startup_parameters({**keep, "options": options})
        assert raised.value.feature == feature
    with pytest.raises(Unsupported):
        redshift_sql.startup_parameters({**keep, "client_encoding": "SJIS"})
    # keys are Postgres' case-insensitive names
    with pytest.raises(Unsupported):
        redshift_sql.startup_parameters({**keep, "CLIENT_ENCODING": "SJIS"})
    assert redshift_sql.startup_parameters({**keep, "OPTIONS": "-c search_path=x"}) == \
        {**keep, "options": "-c search_path=x"}


def test_regexp_replace_replaces_every_match_as_redshift_does():
    assert translate("SELECT REGEXP_REPLACE(s, 'a', 'b') FROM t") == \
        "SELECT REGEXP_REPLACE(s, 'a', 'b', 'g') FROM t"
    assert translate("SELECT REGEXP_REPLACE(s, 'a') FROM t") == \
        "SELECT REGEXP_REPLACE(s, 'a', '', 'g') FROM t"
    assert translate("SELECT REGEXP_REPLACE(s, 'a', 'b', 2) FROM t") == \
        "SELECT REGEXP_REPLACE(s, 'a', 'b', 2, 0) FROM t"
    assert translate("SELECT REGEXP_REPLACE(s, 'a', 'b', 1, 'i') FROM t") == \
        "SELECT REGEXP_REPLACE(s, 'a', 'b', 1, 0, 'i') FROM t"
    assert refused("SELECT REGEXP_REPLACE(s, 'a', 'b', 1, 'p') FROM t") == \
        "REGEXP_REPLACE (parameter 'p')"


def test_adjacent_string_literals_on_one_line_are_a_syntax_error():
    with pytest.raises(redshift_sql.Unparsable):
        translate("SELECT 'a' 'b'")
    assert translate("SELECT 'a'\n'b'") == "SELECT 'a' || 'b'"   # the standard's continuation


def test_unparsed_commands_default_deny():
    # through as written: no SQL of their own to translate
    for sql in ("SHOW search_path", "VACUUM warehouse.fct_orders", "CALL p(1)", "LOCK t",
                "FETCH NEXT FROM c", "EXECUTE p(1)", "RESET ALL", "START TRANSACTION",
                "ABORT", "CLOSE c", "DEALLOCATE p", "REFRESH MATERIALIZED VIEW mv"):
        assert translate(sql) == sql, sql
    # the statement inside is translated
    assert translate("EXPLAIN SELECT TOP 1 a FROM t") == "EXPLAIN SELECT a FROM t LIMIT 1"
    assert translate("PREPARE p (int) AS SELECT NVL(a, 0) FROM t WHERE b = $1") == \
        "PREPARE p (int) AS SELECT COALESCE(a, 0) FROM t WHERE b = $1"
    assert translate("DECLARE c CURSOR FOR SELECT TOP 2 a FROM t") == \
        "DECLARE c CURSOR FOR SELECT a FROM t LIMIT 2"
    assert refused("PREPARE p AS UNLOAD ('select 1') TO 's3://b/k' IAM_ROLE 'r'") == "UNLOAD"
    # refused by name: bodies that would run untranslated, and what Redshift lacks
    for sql, feature in (
            ("CREATE FUNCTION f() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql",
             "CREATE FUNCTION"),
            ("CREATE OR REPLACE PROCEDURE p() AS $$ BEGIN COPY t FROM STDIN; END $$ "
             "LANGUAGE plpgsql", "CREATE PROCEDURE"),
            ("CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW EXECUTE FUNCTION f()",
             "CREATE TRIGGER"),
            ("CREATE RULE r AS ON INSERT TO t DO ALSO DELETE FROM s", "CREATE RULE"),
            ("DO $$ BEGIN PERFORM 1; END $$", "DO (an anonymous code block)"),
            ("EXPLAIN ANALYZE DELETE FROM t", "EXPLAIN ANALYZE / EXPLAIN (options)"),
            ("LOAD 'x'", "LOAD X"), ("CHECKPOINT", "CHECKPOINT"),
            ("LISTEN x", "LISTEN"), ("NOTIFY x", "NOTIFY"), ("DISCARD ALL", "DISCARD"),
            ("ALTER SYSTEM SET work_mem = '1GB'", "ALTER SYSTEM")):
        assert refused(sql) == feature, sql


def test_bare_numeric_is_redshifts_18_0():
    assert translate("SELECT 2.5::numeric, CAST(x AS DECIMAL), y::numeric(10,2) FROM t") == \
        "SELECT CAST(2.5 AS DECIMAL(18, 0)), CAST(x AS DECIMAL(18, 0)), " \
        "CAST(y AS DECIMAL(10, 2)) FROM t"
    assert translate("CREATE TABLE t (a numeric, b decimal(12,2))") == \
        "CREATE TABLE t (a DECIMAL(18, 0), b DECIMAL(12, 2))"


def test_delimited_identifiers_fold_to_lower_case():
    assert translate('SELECT "MixedCase" AS "Total" FROM "Warehouse".t') == \
        'SELECT "mixedcase" AS "total" FROM "warehouse".t'


def test_qualify_is_refused_by_its_name():
    assert refused("SELECT a FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY a) = 1") == \
        "QUALIFY (no verified PostgreSQL equivalent)"


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
    assert len(got) == len(redshift_corpus.CORPUS) == 14
