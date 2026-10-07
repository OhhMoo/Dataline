"""Redshift SQL -> PostgreSQL, for the Postgres-persona Redshift plug.

The persona (gateway/docker/redshift_start.py) is PostgreSQL behind an audited
wire proxy (`alkera_gateway.native.pgwire_proxy`). Every statement a client
sends passes through `translate` first: Redshift's dialect becomes Postgres',
and what Postgres cannot imitate is REFUSED BY NAME (`Unsupported`, which the
proxy turns into an ErrorResponse, SQLSTATE 0A000, "Redshift feature not
available in this environment: <feature>").

THE RULE (R004 RESEARCH-warehouse §3, TOOL_INVENTORY §5): loud unsupported is
fine, silent wrong is disqualifying. sqlglot's redshift -> postgres generator
is not semantics-preserving everywhere - measured on sqlglot 30.18.0:

  DATEDIFF(day, ts_a, ts_b)   epoch seconds / 86400, truncated: 23:00 -> 01:00
                              is 0 days, where Redshift counts 1 boundary
  DATEDIFF(month, a, b)       AGE()-based: Jan 31 -> Feb 1 is 0 months, where
                              Redshift counts 1 boundary
  LISTAGG ... WITHIN GROUP    STRING_AGG(...) WITHIN GROUP: an error on Postgres
  CONCAT(a, b)                Postgres CONCAT, which reads NULL as '' - Redshift
                              CONCAT is `||` and returns NULL
  ADD_MONTHS(d, n)            parsed as DATEADD: Feb 29 + 1 is Mar 29, where
                              Redshift's month-end rule gives Mar 31

So the translation is checked, not trusted. Each statement is parsed (redshift
dialect) and rendered for both dialects; where the renderings differ, the
SMALLEST differing nodes are the constructs sqlglot translated, and every one
of them must be in VETTED (checked against PostgreSQL 17 here) or HANDLED
(rewritten by this module with Redshift's semantics). Anything else is refused,
named. A statement both dialects render the same - with no rewrite, no
canonicalised Redshift spelling and no backslash in it - goes to Postgres as
the client wrote it, byte for byte; any other goes as the rendering.

DEFAULT DENY. Nothing reaches Postgres that this module did not read: text it
cannot tokenize or parse is refused as a syntax error (Unparsable, 42601), not
passed through for Postgres to read its own way; a statement sqlglot leaves as
an unparsed Command goes through only if its verb is in COMMAND_VERBS (EXPLAIN
[VERBOSE], PREPARE and DECLARE have the statement inside them translated); a
"statement" sqlglot reads as a bare expression goes through only for
BARE_VERBS; CREATE FUNCTION / PROCEDURE / TRIGGER / RULE ... (DENIED_CREATE)
and DO are refused, since their bodies would run untranslated; the settings
that would change how the server reads a literal (standard_conforming_strings,
backslash_quote) or the client encoding are refused wherever they are named.

NOT IMITATED, refused by name: UNLOAD, COPY (from S3, STDIN or anywhere: this
cluster has no object store wired to it), SUPER and PartiQL navigation,
JSON_PARSE, external schemas/tables (Spectrum), Redshift ML, datashares,
libraries, ALTER TABLE ... APPEND / ALTER DISTKEY|SORTKEY|DISTSTYLE|ENCODE, the
session settings that change identifier or output semantics, and DATEDIFF on a
unit whose boundary rule is not pinned here (week, sub-second, decade...).

ACCEPTED WITH NO EFFECT (Redshift's physical-layout DDL, which Postgres has no
use for): DISTSTYLE, DISTKEY, [COMPOUND|INTERLEAVED] SORTKEY, ENCODE (column and
table), BACKUP, late binding (`WITH NO SCHEMA BINDING`), and the WLM / result
cache session settings (`SET query_group ...` becomes a placeholder setting,
`alkera.query_group`, which Postgres accepts and nothing reads).

WHAT IT SAYS IT IS: `version()` and the `server_version` the proxy reports are
Redshift's (VERSION, SERVER_VERSION), so psql warns about an 8.0 server as it
does against a real cluster; `current_database()` is `dev`.

SYSTEM VIEWS: svv_tables, svv_columns, pg_table_def and the (empty) Spectrum
views exist (SYSTEM_VIEWS); the other svv_* / stl_* / stv_* do not, and a query
of one fails loudly ("relation ... does not exist"). redshift_connector's
get_tables / get_columns / get_procedures still fail: their SQL leans on
Redshift-only behaviour (a select-list alias referenced in WHERE,
pg_proc_info), which Postgres refuses - loudly. get_schemas, get_primary_keys
and get_catalogs work.

REDSHIFT'S READING, KEPT: a literal's backslash escapes are PostgreSQL 8.0's
('a\\_b' is 'a_b', '\\n' a newline; octal / hex escapes are refused); a
delimited identifier is folded to lower case ("Total" is total); NUMERIC /
DECIMAL with no precision is (18,0).

KNOWN DIVERGENCES (recorded here; no type information reaches this module):
  AVG over an integer column: Redshift returns BIGINT (the average truncated),
      Postgres a NUMERIC with the fraction. The VALUE differs whenever the
      average is not whole - cast it yourself (AVG(x)::BIGINT) for Redshift's.
  GETDATE()/SYSDATE -> CURRENT_TIMESTAMP: a TIMESTAMPTZ in the server's UTC,
      not Redshift's TIMESTAMP; the instant is the same. Both read the box's
      wall clock, as Redshift reads AWS's - not the world's virtual clock.
  TEXT is unbounded (Redshift: VARCHAR(256)); VARCHAR(MAX) is VARCHAR(65535).
  NUMERIC division keeps Postgres' scale rules; values agree, digits may not.
  Aggregate window functions with ORDER BY and no frame clause run with
      Postgres' default frame; Redshift refuses them.
  ALTER USER / SCHEMA / DEFAULT PRIVILEGES and other statements sqlglot leaves
      unparsed outside COMMAND_VERBS are refused, though Redshift has them.
"""
from __future__ import annotations

import re
from collections import Counter

import sqlglot
from sqlglot import exp
from sqlglot.dialects.redshift import Redshift
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import Token, TokenType

from .native.pgwire_proxy import Refusal

POSTGRES = "postgres"

#: What the persona says it is: the `server_version` ParameterStatus every
#: Redshift cluster reports (the proxy rewrites Postgres' own, which
#: redshift_connector cannot even parse), and `SELECT version()`.
SERVER_VERSION = "8.0.2"
VERSION = ("PostgreSQL 8.0.2 on i686-pc-linux-gnu, compiled by GCC gcc (GCC) 3.4.2 "
           "20041017 (Red Hat 3.4.2-6.fc3), Redshift 1.0.87657")

#: The Redshift system views a client reads without knowing it: the drivers'
#: metadata calls (redshift_connector get_tables / get_columns check
#: svv_external_schemas, then read svv_tables / svv_columns) and the catalog
#: idioms an engineer types (`select * from pg_table_def where tablename = ...`).
#: Created in pg_catalog of every database at boot (redshift_start.py
#: `provision`), so they resolve unqualified exactly as Redshift's do. Spectrum
#: is not imitated, so its three views exist and are EMPTY - the truthful answer
#: for a cluster with no external schemas. Columns follow the AWS reference.
SYSTEM_VIEWS: tuple[str, ...] = (
    """CREATE OR REPLACE VIEW pg_catalog.svv_tables AS
       SELECT table_catalog::varchar(128) AS table_catalog,
              table_schema::varchar(128) AS table_schema,
              table_name::varchar(128) AS table_name,
              table_type::varchar(128) AS table_type,
              NULL::varchar(128) AS remarks
       FROM information_schema.tables""",
    """CREATE OR REPLACE VIEW pg_catalog.svv_columns AS
       SELECT table_catalog::varchar(128) AS table_catalog,
              table_schema::varchar(128) AS table_schema,
              table_name::varchar(128) AS table_name,
              column_name::varchar(128) AS column_name,
              ordinal_position::int AS ordinal_position,
              column_default::varchar(4000) AS column_default,
              is_nullable::varchar(3) AS is_nullable,
              data_type::varchar(128) AS data_type,
              character_maximum_length::int AS character_maximum_length,
              numeric_precision::int AS numeric_precision,
              numeric_precision_radix::int AS numeric_precision_radix,
              numeric_scale::int AS numeric_scale,
              datetime_precision::int AS datetime_precision,
              interval_type::varchar(128) AS interval_type,
              interval_precision::varchar(128) AS interval_precision,
              character_set_catalog::varchar(128) AS character_set_catalog,
              character_set_schema::varchar(128) AS character_set_schema,
              character_set_name::varchar(128) AS character_set_name,
              collation_catalog::varchar(128) AS collation_catalog,
              collation_schema::varchar(128) AS collation_schema,
              collation_name::varchar(128) AS collation_name,
              domain_name::varchar(128) AS domain_name,
              NULL::varchar(128) AS remarks
       FROM information_schema.columns""",
    """CREATE OR REPLACE VIEW pg_catalog.pg_table_def AS
       SELECT n.nspname::name AS schemaname, c.relname::name AS tablename,
              a.attname::name AS "column",
              pg_catalog.format_type(a.atttypid, a.atttypmod)::text AS type,
              'none'::char(32) AS encoding, false AS distkey, 0::int AS sortkey,
              a.attnotnull AS notnull
       FROM pg_catalog.pg_class c
       JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
       JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
       WHERE c.relkind IN ('r', 'v', 'm', 'p') AND a.attnum > 0 AND NOT a.attisdropped
         AND pg_catalog.pg_table_is_visible(c.oid)""",
    """CREATE OR REPLACE VIEW pg_catalog.svv_external_schemas AS
       SELECT NULL::oid AS esoid, NULL::smallint AS eskind, NULL::name AS schemaname,
              NULL::int AS esowner, NULL::text AS databasename, NULL::text AS esoptions
       WHERE false""",
    """CREATE OR REPLACE VIEW pg_catalog.svv_external_tables AS
       SELECT NULL::varchar(128) AS redshift_database_name, NULL::varchar(128) AS schemaname,
              NULL::varchar(128) AS tablename, NULL::varchar(128) AS tabletype,
              NULL::varchar(128) AS location, NULL::varchar(128) AS input_format,
              NULL::varchar(128) AS output_format, NULL::varchar(128) AS serialization_lib,
              NULL::varchar(128) AS serde_parameters, NULL::int AS compressed,
              NULL::varchar(128) AS parameters
       WHERE false""",
    """CREATE OR REPLACE VIEW pg_catalog.svv_external_columns AS
       SELECT NULL::varchar(128) AS redshift_database_name, NULL::varchar(128) AS schemaname,
              NULL::varchar(128) AS tablename, NULL::varchar(128) AS columnname,
              NULL::varchar(128) AS external_type, NULL::int AS columnnum,
              NULL::int AS part_key, NULL::varchar(128) AS is_nullable
       WHERE false""",
)

#: The message the client sees, the plan's wording (Task 5).
MESSAGE = "Redshift feature not available in this environment: {feature}"


class Unsupported(Refusal):
    """A Redshift feature this persona does not imitate; `feature` names it."""

    sqlstate = "0A000"

    def __init__(self, feature: str):
        self.feature = feature
        super().__init__(MESSAGE.format(feature=feature))


class Unparsable(Unsupported):
    """Text this persona cannot read as Redshift SQL: refused as a syntax error
    (42601), never passed through for Postgres to read its own way."""

    sqlstate = "42601"

    def __init__(self, detail: str):
        self.feature = "unparsable statement"
        Refusal.__init__(self, f"syntax error (read as Redshift SQL): {detail}")


class _Persona(Redshift):
    """Redshift as sqlglot parses it, with two corrections:

    ADD_MONTHS is kept apart from DATEADD (sqlglot folds both into one
    TsOrDsAdd, and their month-end rules differ).

    A string literal's backslash escapes are PostgreSQL 8.0's, which Redshift
    keeps: \\b \\f \\n \\r \\t and \\\\ are what they say, and a backslash before
    any other character is dropped ('a\\_b' is 'a_b'). sqlglot keeps that
    backslash, and knows \\a and \\v, which 8.0 did not. Octal and hex escapes
    (\\101, \\x41) are left as written here and refused (`_escape_fence`)."""

    class Tokenizer(Redshift.Tokenizer):
        ESCAPE_FOLLOW_CHARS = [*"0123456789", "x", "X", "u", "U"]

    class Parser(Redshift.Parser):
        FUNCTIONS = {
            **Redshift.Parser.FUNCTIONS,
            "ADD_MONTHS": lambda args: exp.Anonymous(this="ADD_MONTHS", expressions=args),
        }


_Persona.UNESCAPED_SEQUENCES = {k: v for k, v in _Persona.UNESCAPED_SEQUENCES.items()
                                if k not in ("\\a", "\\v")}

READ = _Persona
RENDER_REDSHIFT = "redshift"

#: Constructs sqlglot translates whose PostgreSQL rendering was checked against
#: Redshift's documented semantics (gateway/tests/test_redshift_sql.py and the
#: corpus the e2e check runs through a real PostgreSQL 17).
VETTED = frozenset({
    "CurrentTimestamp",      # GETDATE(), SYSDATE -> CURRENT_TIMESTAMP (see KNOWN DIVERGENCES)
    "DataType",              # INT vs INTEGER, TEXT vs VARCHAR(MAX): spelling only
    "Identifier",            # quoting of a reserved word, same name
    "Literal",               # a string's backslash escapes (Redshift processes them,
                             # as PostgreSQL 8.0 did) -> a standard string, same value
    "TsOrDsAdd",             # DATEADD -> + INTERVAL; month-end clamps the same way
    "DecodeCase",            # DECODE -> CASE, NULL matching NULL as Redshift does
    "Nvl2",                  # NVL2(a, x, y) -> CASE WHEN NOT a IS NULL THEN x ELSE y END
    "ConvertTimezone",       # CONVERT_TIMEZONE -> AT TIME ZONE twice
    "StrPosition",           # CHARINDEX -> POSITION
    "Length",                # LEN -> LENGTH
    "Median",                # MEDIAN -> PERCENTILE_CONT(0.5) WITHIN GROUP
    "GeneratedAsIdentityColumnConstraint",  # IDENTITY(s, i) -> GENERATED BY DEFAULT
    "Collate",               # COLLATE pg_catalog.default quoting
    "Window",                # ROWS UNBOUNDED PRECEDING spelled out in full
    "WindowSpec",
})
#: Constructs this module rewrites itself (see `_rewrite`).
HANDLED = frozenset({"Concat", "TsOrDsDiff", "GroupConcat", "WithinGroup",
                     "ApproxDistinct", "Anonymous", "Bracket"})

#: Session settings Redshift has and Postgres does not, which change nothing a
#: query returns (WLM routing, the result cache, auto-analyze): accepted, kept
#: under a placeholder name Postgres allows and nothing reads.
NOOP_SETTINGS = frozenset({"query_group", "wlm_query_slot_count",
                           "enable_result_cache_for_session",
                           "analyze_threshold_percent", "auto_mv",
                           "statement_timeout_ms"})
#: Settings that change identifiers or output - or how the SERVER reads a
#: string literal, which would let text this module read one way run another
#: way (standard_conforming_strings, backslash_quote): refused wherever they
#: are named - SET, set_config(), ALTER ROLE/DATABASE ... SET, the startup
#: packet's `options` - never ignored.
FENCED_SETTINGS = frozenset({"enable_case_sensitive_identifier",
                             "enable_case_sensitive_super_attribute",
                             "describe_field_name_in_uppercase",
                             "json_parse_truncate_strings",
                             "json_serialization_enable",
                             "downcase_delimited_identifier",
                             "enable_numeric_rounding",
                             "standard_conforming_strings",
                             "backslash_quote"})
#: The only client encoding: the proxy and this module read text as UTF-8,
#: and a multibyte encoding whose second byte can be a quote or a backslash
#: (SJIS, BIG5, GBK ...) would let the server split strings differently.
UTF8_NAMES = frozenset({"utf8", "utf-8", "unicode"})
NOOP_PREFIX = "alkera."

#: Statement kinds sqlglot parses into a tree this module translates.
STATEMENTS = (exp.Query, exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
              exp.Alter, exp.Set, exp.Transaction, exp.Commit, exp.Rollback, exp.Analyze,
              exp.Grant, exp.Revoke, exp.TruncateTable, exp.Comment, exp.Values)
#: Statements sqlglot leaves as an unparsed `Command` that may go through as
#: written (Redshift and Postgres read them alike, and they hold no SQL of
#: their own to translate). EXPLAIN, PREPARE and DECLARE hold one: theirs is
#: translated (`_wrapped`). Any other Command is refused by name.
COMMAND_VERBS = frozenset({"SHOW", "VACUUM", "ANALYZE", "GRANT", "REVOKE", "CALL", "BEGIN",
                           "START", "COMMIT", "END", "ROLLBACK", "ABORT", "RESET", "FETCH",
                           "EXECUTE", "DEALLOCATE", "CLOSE", "MOVE", "LOCK", "REFRESH"})
#: Statements sqlglot misreads as a bare expression (`START TRANSACTION` as an
#: alias): the Redshift ones go through as written, the rest are refused.
BARE_VERBS = frozenset({"START", "ABORT", "CLOSE", "MOVE", "DEALLOCATE", "BEGIN", "END",
                        "COMMIT", "ROLLBACK"})
#: CREATE kinds whose body or effect is not SQL this module translates (a
#: function's or procedure's body runs unseen), or that Redshift does not have.
DENIED_CREATE = frozenset({"FUNCTION", "PROCEDURE", "TRIGGER", "RULE", "EXTENSION",
                           "LANGUAGE", "SERVER", "FOREIGN", "EVENT", "AGGREGATE", "OPERATOR",
                           "CAST", "TRANSFORM", "ACCESS", "CONVERSION", "PUBLICATION",
                           "SUBSCRIPTION", "STATISTICS", "TEXT", "COLLATION", "DOMAIN",
                           "TYPE"})

#: DATEDIFF units whose boundary rule this module implements.
_DIFF_UNITS = {"YEAR", "QUARTER", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND"}
_SECONDS = {"HOUR": 3600, "MINUTE": 60, "SECOND": 1}


# --------------------------------------------------------------------------- tokens
def _tokens(text: str):
    return _Persona().tokenize(text)


_COMMAND_TOKENS = frozenset(_Persona.Tokenizer.COMMANDS)


def _read(text: str):
    """The tokens, or `Unparsable`: a text this module cannot tokenize (an
    unterminated string, identifier or comment as Redshift reads them) is never
    passed on for Postgres to read its own way.

    sqlglot reads a statement that starts with a command keyword (EXPLAIN,
    PREPARE, DECLARE, CALL, VACUUM, SHOW, RESET, EXECUTE, ...) as that keyword
    and ONE string holding the rest; the rest is tokenized here too, so every
    fence below sees its words, at their own offsets in `text`."""
    try:
        tokens = _tokens(text)
    except (TokenError, ValueError) as exc:
        raise Unparsable(f"unterminated string, identifier or comment "
                         f"({str(exc).splitlines()[0][:120]})") from exc
    if len(tokens) > 1 and tokens[0].token_type in _COMMAND_TOKENS:
        at = tokens[0].end + 1
        rest = _read(text[at:])
        tokens = tokens[:1] + [Token(t.token_type, t.text, t.line, t.col, t.start + at,
                                     t.end + at, t.comments) for t in rest]
    return tokens


def _statements(sql: str) -> list[str]:
    """`sql` split on its top-level semicolons (strings and comments respected),
    empty statements dropped."""
    tokens = _read(sql)
    out, start = [], 0
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            out.append(sql[start:token.start])
            start = token.end + 1
    out.append(sql[start:])
    return [s for s in out if s.strip() and _read(s)]


def _words(tokens, n: int = 6) -> list[str]:
    return [t.text.upper() for t in tokens[:n]]


# --------------------------------------------------------------------------- fences
_S3 = re.compile(r"\bFROM\s+'(s3|emr|dynamodb)://", re.IGNORECASE)


def _verb_fences(tokens, text: str) -> None:
    """Refusals decided by a statement's leading words, before (and whether or
    not) sqlglot can parse it."""
    words = _words(tokens, 8)
    if not words:
        return
    head = words[0]
    if head == "UNLOAD":
        raise Unsupported("UNLOAD")
    if head == "COPY":
        source = _S3.search(text)
        if source:
            raise Unsupported(f"COPY FROM {source.group(1).upper()}")
        if re.search(r"\bFROM\s+STDIN\b", text, re.IGNORECASE):
            raise Unsupported("COPY FROM STDIN")
        if re.search(r"\bTO\b", text, re.IGNORECASE) and not re.search(
                r"\bFROM\b", text, re.IGNORECASE):
            raise Unsupported("COPY TO")
        raise Unsupported("COPY")
    if head in ("CREATE", "ALTER", "DROP") and "EXTERNAL" in words[1:4]:
        kind = words[words.index("EXTERNAL") + 1] if words.index("EXTERNAL") + 1 < len(words) \
            else ""
        raise Unsupported(f"EXTERNAL {kind}".strip())
    if head == "CREATE" and words[1:2] == ["MODEL"]:
        raise Unsupported("CREATE MODEL (Redshift ML)")
    if head == "CREATE" and words[1:2] == ["LIBRARY"]:
        raise Unsupported("CREATE LIBRARY")
    if head == "CREATE":
        kind = next((w for w in words[1:] if w not in (
            "OR", "REPLACE", "TEMP", "TEMPORARY", "LOCAL", "TRUSTED", "PROCEDURAL",
            "CONSTRAINT", "DEFAULT", "UNLOGGED")), "")
        if kind in DENIED_CREATE:
            raise Unsupported(f"CREATE {kind}")
    if head == "DO":
        raise Unsupported("DO (an anonymous code block)")
    if head == "ALTER" and words[1:2] == ["SYSTEM"]:
        raise Unsupported("ALTER SYSTEM")
    if "DATASHARE" in words:
        raise Unsupported("DATASHARE")
    if head == "ALTER" and words[1:2] == ["TABLE"]:
        upper = text.upper()
        if re.search(r"\bAPPEND\s+FROM\b", upper):
            raise Unsupported("ALTER TABLE APPEND")
        found = re.search(r"\bALTER\s+(?:COMPOUND\s+|INTERLEAVED\s+)?"
                          r"(DISTKEY|DISTSTYLE|SORTKEY|ENCODE)\b", upper)
        if found:
            raise Unsupported(f"ALTER TABLE ... ALTER {found.group(1)}")


_STRINGS = frozenset({TokenType.STRING, TokenType.NATIONAL_STRING, TokenType.BYTE_STRING})


def _fenced_names(tokens) -> None:
    """A fenced setting named ANYWHERE - an identifier or a string - is refused:
    `SET x`, `set_config('x', ...)`, `ALTER ROLE r SET x = ...` alike. The
    client encoding may be changed only by a plain SET (`_setting` checks the
    value): through set_config() or ALTER ... SET it is refused."""
    words = {t.text.lower() for t in tokens}
    for name in sorted(words & FENCED_SETTINGS):
        raise Unsupported(f"SET {name}")
    if "client_encoding" in words and tokens[0].text.upper() != "SET" and (
            "set_config" in words or "set" in words):
        raise Unsupported("a client_encoding other than UTF8")


def _escape_fence(tokens, text: str) -> None:
    """Octal and hex escapes in a string literal ('\\101', '\\x41') are refused:
    their bytes are not read here the way Redshift reads them."""
    for t in tokens:
        if t.token_type not in _STRINGS:
            continue
        raw = text[t.start:t.end + 1]
        i = 0
        while i < len(raw) - 1:
            if raw[i] == "\\":
                if raw[i + 1].isdigit() or raw[i + 1] in "xXuU":
                    raise Unsupported("an octal or hex escape in a string literal")
                i += 2
                continue
            i += 1


def _setting(tokens, text: str) -> str | None:
    """`SET [SESSION|LOCAL] name ...` / `RESET name`: a Redshift-only setting is
    renamed to its placeholder (accepted, no effect) or refused; any other
    setting is the client's text, untouched, for Postgres to apply or refuse.
    None for a statement that is not a SET / RESET."""
    words = _words(tokens, 4)
    if not words or words[0] not in ("SET", "RESET"):
        return None
    index = 1
    if words[0] == "SET" and len(words) > 1 and words[1] in ("SESSION", "LOCAL"):
        index = 2
    if index >= len(tokens):
        return text
    name_token = tokens[index]
    name = name_token.text.lower()
    if name in FENCED_SETTINGS:
        raise Unsupported(f"SET {name}")
    if words[0] == "SET" and name in ("client_encoding", "names"):
        value = [t.text.lower() for t in tokens[index + 1:]
                 if t.text.upper() not in ("TO", "=")]
        if not value or value[0] not in UTF8_NAMES | {"default"}:
            raise Unsupported("a client_encoding other than UTF8")
    if name not in NOOP_SETTINGS:
        if "\\" in text:
            raise Unsupported("a backslash escape in a SET statement")
        return text
    return text[:name_token.start] + NOOP_PREFIX + name + text[name_token.end + 1:]


def startup_parameters(params: dict[str, str]) -> dict[str, str]:
    """The startup packet's parameters as the persona allows them (the proxy's
    `startup_filter`): a UTF-8 client_encoding only, and in `options` only
    `-c name=value` / `--name=value` settings, a fenced one refused, a
    Redshift-only one dropped. Anything else in `options` (a backend switch)
    is refused."""
    out = dict(params)
    encoding = out.get("client_encoding")
    if encoding is not None and encoding.strip().lower() not in UTF8_NAMES | {"auto"}:
        raise Unsupported("a client_encoding other than UTF8")
    options = out.get("options")
    if options is None:
        return out
    items = re.split(r"(?<!\\)\s+", options.strip()) if options.strip() else []
    kept: list[str] = []
    i = 0
    while i < len(items):
        item = items[i]
        if item == "-c" and i + 1 < len(items):
            setting, i = items[i + 1], i + 2
        elif item.startswith("-c") and len(item) > 2:
            setting, i = item[2:], i + 1
        elif item.startswith("--") and "=" in item:
            setting, i = item[2:], i + 1
        else:
            raise Unsupported(f"startup option {item!r}")
        name, _, value = setting.partition("=")
        name = name.strip().lower().replace("-", "_")
        if name in FENCED_SETTINGS:
            raise Unsupported(f"SET {name}")
        if name == "client_encoding" and value.strip().lower() not in UTF8_NAMES:
            raise Unsupported("a client_encoding other than UTF8")
        if name not in NOOP_SETTINGS:
            kept.append(f"-c {setting}")
    if kept:
        out["options"] = " ".join(kept)
    else:
        out.pop("options")
    return out


# --------------------------------------------------------------------------- DDL
_TABLE_LEVEL = {"DISTSTYLE", "DISTKEY", "SORTKEY", "COMPOUND", "INTERLEAVED", "ENCODE",
                "BACKUP"}


def _strip_physical(tokens, text: str) -> str:
    """CREATE TABLE / CREATE VIEW with Redshift's physical-layout attributes
    removed (accepted with no effect), everything else byte for byte."""
    words = _words(tokens, 4)
    if not words or words[0] != "CREATE":
        return text
    rest = [w for w in words[1:] if w not in ("OR", "REPLACE", "LOCAL", "TEMP",
                                              "TEMPORARY")]
    kind = rest[0] if rest else ""
    cut: list[tuple[int, int]] = []
    if kind == "TABLE":
        cut = _table_attributes(tokens)
    elif kind in ("VIEW", "MATERIALIZED"):
        cut = _no_schema_binding(tokens)
    if not cut:
        return text
    out, at = [], 0
    for start, end in cut:
        out.append(text[at:start])
        at = end + 1
    out.append(text[at:])
    return "".join(out)


def _table_attributes(tokens) -> list[tuple[int, int]]:
    cut: list[tuple[int, int]] = []
    depth, i, n = 0, 0, len(tokens)
    while i < n:
        token = tokens[i]
        kind, word = token.token_type, token.text.upper()
        if kind == TokenType.L_PAREN:
            depth += 1
        elif kind == TokenType.R_PAREN:
            depth -= 1
        elif depth == 0 and kind == TokenType.ALIAS:     # CTAS: the query is not ours
            break
        elif depth == 0 and word in _TABLE_LEVEL:
            j = _attribute_end(tokens, i)
            if j is not None:
                cut.append((token.start, tokens[j].end))
                i = j + 1
                continue
        elif depth == 1 and word in ("ENCODE", "DISTKEY", "SORTKEY") and i > 0 \
                and tokens[i - 1].token_type not in (TokenType.L_PAREN, TokenType.COMMA):
            # a column attribute: `a int ENCODE az64 DISTKEY SORTKEY`
            j = i + 1 if word == "ENCODE" and i + 1 < n else i
            cut.append((tokens[i - 1].end + 1, tokens[j].end))
            i = j + 1
            continue
        i += 1
    return cut


def _attribute_end(tokens, i: int) -> int | None:
    """The index of the last token of the table attribute starting at `i`."""
    n = len(tokens)
    word = tokens[i].text.upper()

    def nxt(k):
        return tokens[k].text.upper() if k < n else ""

    if word in ("COMPOUND", "INTERLEAVED"):
        return _attribute_end(tokens, i + 1) if nxt(i + 1) == "SORTKEY" else None
    if word in ("DISTSTYLE", "BACKUP"):
        return i + 1 if i + 1 < n else None
    if word == "ENCODE":
        return i + 1 if nxt(i + 1) == "AUTO" else None
    if word in ("DISTKEY", "SORTKEY"):
        if nxt(i + 1) == "AUTO":
            return i + 1
        if i + 1 < n and tokens[i + 1].token_type == TokenType.L_PAREN:
            depth, k = 0, i + 1
            while k < n:
                if tokens[k].token_type == TokenType.L_PAREN:
                    depth += 1
                elif tokens[k].token_type == TokenType.R_PAREN:
                    depth -= 1
                    if depth == 0:
                        return k
                k += 1
    return None


def _no_schema_binding(tokens) -> list[tuple[int, int]]:
    words = [t.text.upper() for t in tokens]
    for i in range(len(words) - 3):
        if words[i:i + 4] == ["WITH", "NO", "SCHEMA", "BINDING"]:
            return [(tokens[i].start, tokens[i + 3].end)]
    return []


# --------------------------------------------------------------------------- the tree
def _node_fences(tree: exp.Expression) -> None:
    for node in tree.walk():
        if isinstance(node, exp.DataType) and node.this in (exp.DataType.Type.SUPER,):
            raise Unsupported("SUPER")
        if isinstance(node, exp.DataType) and str(node.this.value).upper() in (
                "HLLSKETCH", "VARBYTE", "GEOMETRY", "GEOGRAPHY"):
            raise Unsupported(str(node.this.value).upper())
        if isinstance(node, exp.ParseJSON):
            raise Unsupported("SUPER (JSON_PARSE)")
        # `alias.attr.attr[0]`: navigation into a SUPER value. A subscript on a
        # plain column or a function result is Postgres array indexing, which
        # the catalog queries the drivers send use (current_schemas(true))[1].
        if isinstance(node, exp.Bracket) and isinstance(node.this, exp.Column) \
                and node.this.args.get("db") is not None:
            raise Unsupported("PartiQL (SUPER navigation)")
        if isinstance(node, exp.Unnest) and isinstance(node.parent, (exp.Join, exp.From)):
            raise Unsupported("PartiQL (FROM-clause unnesting)")
        if isinstance(node, exp.Copy):
            raise Unsupported("COPY")


def _name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).upper()
    if isinstance(node, exp.Func):
        return node.sql_name()
    if isinstance(node, exp.Select) and node.args.get("qualify") is not None:
        return "QUALIFY"
    return node.key.upper()


#: Parts of a HANDLED node that render differently only because the node does.
STRUCTURAL = frozenset({"Distinct", "Order", "Ordered"})


def _ancestors(node: exp.Expression):
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def _render(node: exp.Expression, dialect) -> str:
    return node.sql(dialect=dialect)


def _causes(tree: exp.Expression) -> list[exp.Expression]:
    """The smallest nodes whose redshift and postgres renderings differ."""
    found: list[exp.Expression] = []

    def visit(node: exp.Expression) -> bool:
        child = False
        for sub in node.iter_expressions():
            if visit(sub):
                child = True
        try:
            differs = _render(node, RENDER_REDSHIFT) != _render(node, POSTGRES)
        except Exception:  # noqa: BLE001 - a node that cannot render alone
            differs = child
        if differs and not child:
            found.append(node)
        return differs

    visit(tree)
    return found


def _handled(node: exp.Expression) -> bool:
    kind = type(node).__name__
    return kind in HANDLED and (kind != "Anonymous" or _name(node) == "ADD_MONTHS")


def _check(tree: exp.Expression) -> None:
    for node in _causes(tree):
        kind = type(node).__name__
        if kind in VETTED or _handled(node):
            continue
        if kind in STRUCTURAL and any(_handled(a) for a in _ancestors(node)):
            # the DISTINCT / ORDER BY parts of a LISTAGG this module rewrites
            continue
        raise Unsupported(f"{_name(node)} (no verified PostgreSQL equivalent)")


# --------------------------------------------------------------------------- rewrites
def _ts(node: exp.Expression) -> exp.Expression:
    return exp.Cast(this=node.copy(), to=exp.DataType.build("TIMESTAMP"))


def _extract(part: str, node: exp.Expression) -> exp.Expression:
    return exp.Extract(this=exp.var(part), expression=_ts(node))


def _datediff(node: exp.TsOrDsDiff) -> exp.Expression:
    unit = str(node.args.get("unit").name if node.args.get("unit") else "DAY").upper()
    end, start = node.this, node.expression
    if unit not in _DIFF_UNITS:
        raise Unsupported(f"DATEDIFF({unit.lower()})")
    if unit == "DAY":
        body = exp.Sub(this=exp.Cast(this=end.copy(), to=exp.DataType.build("DATE")),
                       expression=exp.Cast(this=start.copy(), to=exp.DataType.build("DATE")))
    elif unit in _SECONDS:
        delta = exp.Sub(this=exp.TimestampTrunc(this=_ts(end), unit=exp.var(unit)),
                        expression=exp.TimestampTrunc(this=_ts(start), unit=exp.var(unit)))
        body = exp.Div(this=exp.Extract(this=exp.var("EPOCH"), expression=delta),
                       expression=exp.Literal.number(_SECONDS[unit]))
    else:
        years = exp.Sub(this=_extract("YEAR", end), expression=_extract("YEAR", start))
        if unit == "YEAR":
            body = years
        else:
            part, per = ("MONTH", 12) if unit == "MONTH" else ("QUARTER", 4)
            body = exp.Add(
                this=exp.Mul(this=exp.Paren(this=years), expression=exp.Literal.number(per)),
                expression=exp.Paren(this=exp.Sub(this=_extract(part, end),
                                                  expression=_extract(part, start))))
    return exp.Cast(this=exp.Paren(this=body), to=exp.DataType.build("BIGINT"))


def _listagg(node: exp.Expression) -> exp.Expression:
    """LISTAGG([DISTINCT] x [, sep]) [WITHIN GROUP (ORDER BY ...)] ->
    STRING_AGG([DISTINCT] x, sep [ORDER BY ...]); Redshift's default separator
    is the empty string, which Postgres requires spelled out."""
    order = None
    agg = node
    if isinstance(node, exp.WithinGroup):
        agg, order = node.this, node.expression
    value = agg.this
    separator = agg.args.get("separator")
    distinct = isinstance(value, exp.Distinct)
    if distinct:
        parts = list(value.expressions)
        value = parts[0]
        if separator is None and len(parts) > 1:
            separator = parts[1]
    if separator is None:
        separator = exp.Literal.string("")
    inner: exp.Expression = exp.Distinct(expressions=[value.copy()]) if distinct \
        else value.copy()
    if isinstance(order, exp.Order):
        inner = exp.Order(this=inner, expressions=[o.copy() for o in order.expressions])
    return exp.GroupConcat(this=inner, separator=separator.copy())


def _add_months(node: exp.Anonymous) -> exp.Expression:
    """ADD_MONTHS(d, n): the last day of a month maps to the last day of the
    result month (Redshift), any other day as DATEADD(month, n, d)."""
    if len(node.expressions) != 2:
        raise Unsupported("ADD_MONTHS (arguments)")
    date, months = (e.sql(dialect=POSTGRES) for e in node.expressions)
    d = f"CAST({date} AS TIMESTAMP)"
    step = f"INTERVAL '1 MONTH' * ({months})"
    month_end = "INTERVAL '1 MONTH' - INTERVAL '1 DAY'"
    template = (
        f"CASE WHEN CAST({d} AS DATE) = CAST(DATE_TRUNC('MONTH', {d}) + {month_end} AS DATE) "
        f"THEN DATE_TRUNC('MONTH', {d} + {step}) + {month_end} + ({d} - DATE_TRUNC('DAY', {d})) "
        f"ELSE {d} + {step} END")
    return sqlglot.parse_one(template, read=POSTGRES)


def _rewrite(tree: exp.Expression) -> bool:
    """Apply the HANDLED rewrites (and VARCHAR(MAX)), deepest first. True when
    anything changed."""
    nodes = [n for n in tree.walk() if isinstance(n, (exp.Concat, exp.TsOrDsDiff,
                                                      exp.GroupConcat, exp.ApproxDistinct,
                                                      exp.Anonymous, exp.DataType,
                                                      exp.CurrentVersion, exp.Bracket,
                                                      exp.Identifier))]
    nodes.sort(key=lambda n: n.depth, reverse=True)
    changed = False
    for node in nodes:
        if node.parent is None and node is not tree:
            continue
        new = None
        if isinstance(node, exp.Concat):
            parts = list(node.expressions)
            new = parts[0].copy()
            for part in parts[1:]:
                new = exp.DPipe(this=new, expression=part.copy(), safe=False)
        elif isinstance(node, exp.TsOrDsDiff):
            new = _datediff(node)
        elif isinstance(node, exp.GroupConcat):
            target = node.parent if isinstance(node.parent, exp.WithinGroup) else node
            target.replace(_listagg(target))
            changed = True
            continue
        elif isinstance(node, exp.ApproxDistinct):
            new = exp.Count(this=exp.Distinct(expressions=[node.this.copy()]))
        elif isinstance(node, exp.Anonymous) and _name(node) == "ADD_MONTHS":
            new = _add_months(node)
        elif isinstance(node, exp.Bracket):
            # sqlglot reads a Redshift subscript as 0-based (SUPER's PartiQL)
            # and adds one for Postgres; the arrays left after the PartiQL fence
            # are pg_catalog's, 1-based on Redshift as on Postgres. Kept as written.
            if node.args.get("offset") != 1:
                node.set("offset", 1)
                changed = True
            continue
        elif isinstance(node, exp.CurrentVersion):
            new = exp.Literal.string(VERSION)
            if isinstance(node.parent, exp.Select) and node.arg_key == "expressions":
                new = exp.alias_(new, "version")     # the column name Postgres gives it
        elif isinstance(node, exp.DataType):
            params = node.expressions
            if params and isinstance(params[0], exp.DataTypeParam) and \
                    isinstance(params[0].this, exp.Var) and params[0].this.name.upper() == "MAX":
                params[0].set("this", exp.Literal.number(65535))
                changed = True
            elif node.this == exp.DataType.Type.DECIMAL and not params:
                # Redshift's NUMERIC / DECIMAL with no precision is (18,0): a
                # cast to it rounds to an integer; Postgres' keeps every digit.
                node.set("expressions", [exp.DataTypeParam(this=exp.Literal.number(18)),
                                         exp.DataTypeParam(this=exp.Literal.number(0))])
                changed = True
            continue
        elif isinstance(node, exp.Identifier):
            # A delimited identifier is case-insensitive on Redshift and folded
            # to lower case (enable_case_sensitive_identifier, which would
            # change that, is fenced): "Total" is total, as a name and as the
            # column a client is handed back.
            if node.quoted and node.this != node.this.lower():
                node.set("this", node.this.lower())
                changed = True
            continue
        if new is not None:
            if node is tree:
                tree = new
            node.replace(new)
            changed = True
    return changed


# --------------------------------------------------------------------------- entry
def _first_line(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:200]


def _wrapped(tokens, text: str, verb: str) -> str:
    """EXPLAIN [VERBOSE] <statement>, PREPARE name [(types)] AS <statement>,
    DECLARE name ... CURSOR ... FOR <statement>: the statement inside is
    translated like any other, its prefix kept as written."""
    start = None
    if verb == "EXPLAIN":
        i = 1
        if i < len(tokens) and tokens[i].text.upper() == "VERBOSE":
            i += 1
        if i < len(tokens) and (tokens[i].text.upper() in ("ANALYZE", "ANALYSE")
                                or tokens[i].token_type == TokenType.L_PAREN):
            raise Unsupported("EXPLAIN ANALYZE / EXPLAIN (options)")
        start = tokens[i].start if i < len(tokens) else None
    else:
        marker = TokenType.ALIAS if verb == "PREPARE" else None
        depth = 0
        for i, t in enumerate(tokens):
            if t.token_type == TokenType.L_PAREN:
                depth += 1
            elif t.token_type == TokenType.R_PAREN:
                depth -= 1
            elif depth == 0 and i + 1 < len(tokens) and (
                    (marker is not None and t.token_type == marker)
                    or (marker is None and t.text.upper() == "FOR")):
                start = tokens[i + 1].start
                break
    if start is None:
        raise Unparsable(f"{verb} without a statement")
    prefix, inner = text[:start], text[start:]
    if "\\" in prefix:
        raise Unsupported(f"a backslash escape in a {verb} statement")
    return prefix + _statement(inner)


def _command(tree: exp.Command, tokens, text: str) -> str:
    """A statement sqlglot left unparsed: one that holds a statement is
    translated inside; a short list goes as written; the rest are refused."""
    verb = str(tree.this).upper()
    if verb in ("EXPLAIN", "PREPARE", "DECLARE"):
        return _wrapped(tokens, text, verb)
    if verb in COMMAND_VERBS:
        if "\\" in text:
            raise Unsupported(f"a backslash escape in a {verb} statement")
        return text
    words = _words(tokens, 2)
    raise Unsupported(" ".join(words))


def _statement(text: str) -> str:
    tokens = _read(text)
    if not tokens:
        return text
    _escape_fence(tokens, text)
    _fenced_names(tokens)
    _verb_fences(tokens, text)
    setting = _setting(tokens, text)
    if setting is not None:
        return setting
    stripped = _strip_physical(tokens, text)
    try:
        trees = [t for t in sqlglot.parse(stripped, read=READ) if t is not None]
    except (ParseError, TokenError, ValueError) as exc:
        raise Unparsable(_first_line(exc)) from exc
    if len(trees) != 1:
        raise Unparsable("one statement was expected")
    tree = trees[0]
    if isinstance(tree, exp.Command):
        return _command(tree, _read(stripped), stripped)
    if not isinstance(tree, STATEMENTS):
        head = _words(tokens, 1)[0]
        if head in BARE_VERBS and "\\" not in text:
            return text
        raise Unsupported(head)
    _node_fences(tree)
    try:
        differs = _render(tree, RENDER_REDSHIFT) != _render(tree, POSTGRES)
    except Exception as exc:  # noqa: BLE001 - e.g. a property sqlglot cannot emit
        raise Unsupported(f"statement the translator cannot render "
                          f"({type(exc).__name__}: {exc})") from exc
    if differs:
        _check(tree)
    holder = exp.Tuple(expressions=[tree])     # so a rewrite can replace the root
    changed = _rewrite(holder.expressions[0])
    tree = holder.expressions[0]
    try:
        rendered = tree.sql(dialect=POSTGRES)
    except Exception as exc:  # noqa: BLE001
        raise Unsupported(f"statement the translator cannot render "
                          f"({type(exc).__name__}: {exc})") from exc
    # The client's bytes go up only when nothing was translated: no dialect
    # difference, no rewrite, no Redshift spelling sqlglot canonicalised on the
    # way in - and no backslash anywhere, the one character Redshift's reader
    # (escapes in a literal) and Postgres' (standard strings) treat apart.
    if stripped == text and not differs and not changed and "\\" not in text \
            and _spelled_alike(text, rendered):
        return text
    return rendered


_LITERALS = frozenset({TokenType.STRING, TokenType.NUMBER, TokenType.NATIONAL_STRING,
                       TokenType.BIT_STRING, TokenType.HEX_STRING, TokenType.BYTE_STRING,
                       TokenType.PARAMETER})


def _words_in(text: str, dialect) -> Counter:
    tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(text)
    return Counter(t.text.upper() for t in tokens
                   if t.token_type not in _LITERALS and t.text[:1].isalpha())


def _spelled_alike(text: str, rendered: str) -> bool:
    """Does the Postgres rendering keep every word the client wrote? Then the
    client's own bytes go upstream unchanged. A Redshift spelling sqlglot
    canonicalised - NVL, LEN, TOP, GETDATE, SYSDATE, DATEDIFF, LISTAGG - is
    missing from the rendering, and the rendering goes instead. (sqlglot's
    postgres PARSER cannot be the judge: it knows NVL and LEN too.)"""
    try:
        return not (_words_in(text, READ) - _words_in(rendered, POSTGRES))
    except Exception:  # noqa: BLE001
        return False


def translate(sql: str) -> str:
    """`sql` (Redshift) as PostgreSQL, or `Unsupported(feature)`.

    A text with nothing to translate comes back as the same string; a text of
    several statements comes back as their translations joined by `;\\n`."""
    statements = _statements(sql)
    if not statements:
        return sql
    out = [_statement(s) for s in statements]
    if out == statements:
        return sql
    return ";\n".join(s.strip() for s in out)
