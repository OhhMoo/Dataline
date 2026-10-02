"""BigQuery against goccy/bigquery-emulator: connection plumbing plus two fences.

BigQuery semantics belong to the emulator (`docker/bigquery/`, pinned v0.8.1). This
module only reaches it, normalises results to the connector contract, and refuses
what the emulator would answer wrongly. "Loud unsupported is fine. Silent wrong is
disqualifying." What that rules out, measured against v0.8.1:

COST IS NOT MODELLED. Results carry `columns`, `rows`, `truncated` and nothing else:
never totalBytesBilled, totalBytesProcessed, slot milliseconds or cache-hit state.
The emulator's billing figures run inverted relative to real BigQuery, where on-demand
billing rounds every query up to a 10 MB minimum per table and bills what is
*scanned*: v0.8.1 reports totalBytesBilled equal to a raw byte count of the tiny
table (57 bytes for two rows here) with no minimum, no cache, no slots. Any cost
figure surfaced here would teach an agent the wrong optimisation. A dry run -
BigQuery's cost-estimation call - is refused with "bigquery cost estimation is not
modelled", whether a caller asks for it per call or slips `dry_run` into the
options; v0.8.1 would otherwise execute the "dry" query for real and return its
rows.

ROW COUNTS ARE COUNTED, NOT TRUSTED. The emulator never reports
`numDmlAffectedRows` (every job comes back `statementType: SELECT`). A count the
server does report wins; otherwise the count is taken by a `SELECT COUNT(*)` over
the statement's own predicate immediately before it runs (DELETE / UPDATE without
FROM / INSERT VALUES / INSERT SELECT). Where no such count is exact - MERGE, UPDATE
... FROM - the result carries no count, `rows_affected` stays None and the
observation is flagged `rows_affected_unreported`. A guessed number would be the
silent kind of wrong.

NO TRANSACTIONS. The emulator executes a script statement by statement and keeps
whatever ran before an error (even inside BEGIN TRANSACTION, whose failure it
swallows). Statements are therefore sent one per request, and a failure after the
first says how many earlier statements were applied and not rolled back, instead of
implying the atomic batch `sql_execute` promises on the other backends.

NO RETRIES. The emulator answers many client errors with HTTP 500, which the client
library retries until its deadline; every call here passes `retry=None` so the
emulator's message reaches the agent at once.

Options come from the JSON object in the variable `bigquery_options_env` names:
`project`, `dataset` (the default dataset unqualified names resolve against),
`api_endpoint`, optional `location`. Credentials are anonymous; the emulator checks
none. Note v0.8.1 resolves the default dataset for queries and DML but not for DDL:
`CREATE TABLE t` fails with the emulator's "unexpected table name path" - qualify it.
"""

import concurrent.futures
import json
import os

from sqlglot import exp

from .records import ToolError, json_value
from .sql import inspect_sql

OPTIONS = frozenset({"project", "dataset", "api_endpoint", "location"})
COST_REFUSAL = "bigquery cost estimation is not modelled"
UNREPORTED = "rows_affected_unreported"
SEED_BATCH = 500

BQ_TYPES = {
    "INTEGER": "BIGINT",
    "INT64": "BIGINT",
    "FLOAT": "DOUBLE",
    "FLOAT64": "DOUBLE",
    "NUMERIC": "DECIMAL",
    "BIGNUMERIC": "DECIMAL",
    "STRING": "VARCHAR",
    "BOOLEAN": "BOOLEAN",
    "BOOL": "BOOLEAN",
    "BYTES": "BLOB",
    "DATE": "DATE",
    "DATETIME": "TIMESTAMP",
    "TIMESTAMP": "TIMESTAMP WITH TIME ZONE",
    "TIME": "TIME",
    "JSON": "JSON",
}


def load_options(env_name: str) -> dict:
    raw = os.environ.get(env_name)
    if not raw:
        raise ToolError("warehouse_unconfigured", "BigQuery connection options are missing")
    try:
        options = json.loads(raw)
    except ValueError:
        raise ToolError("warehouse_unconfigured", "BigQuery options are not JSON") from None
    if not isinstance(options, dict):
        raise ToolError("warehouse_unconfigured", "BigQuery options must be a JSON object")
    return check_options(options)


def check_options(options: dict) -> dict:
    if "dry_run" in options:
        raise ToolError("warehouse_unconfigured", COST_REFUSAL)
    unknown = sorted(set(options) - OPTIONS)
    if unknown:
        raise ToolError(
            "warehouse_unconfigured", f"Unsupported BigQuery options: {', '.join(unknown)}"
        )
    missing = sorted({"project", "dataset", "api_endpoint"} - set(options))
    if missing:
        raise ToolError("warehouse_unconfigured", f"BigQuery options lack: {', '.join(missing)}")
    return options


def make_client(options: dict):
    from google.api_core.client_options import ClientOptions
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    return bigquery.Client(
        project=options["project"],
        location=options.get("location"),
        credentials=AnonymousCredentials(),
        client_options=ClientOptions(api_endpoint=options["api_endpoint"]),
    )


def bq_type(field) -> str:
    kind = (field.field_type or "").upper()
    if kind in ("RECORD", "STRUCT"):
        inner = ", ".join(f"{sub.name} {bq_type(sub)}" for sub in field.fields)
        name = f"STRUCT({inner})"
    elif kind in ("NUMERIC", "BIGNUMERIC") and field.precision is not None:
        name = f"DECIMAL({field.precision},{field.scale or 0})"
    else:
        name = BQ_TYPES.get(kind, f"BIGQUERY_{kind}")
    return f"{name}[]" if field.mode == "REPEATED" else name


def count_sql(statement) -> str | None:
    """A query counting the rows `statement` will touch, or None if no count is exact."""
    if statement.args.get("with"):
        return None
    if isinstance(statement, exp.Insert):
        source = statement.expression
        if isinstance(source, exp.Values):
            return str(len(source.expressions))
        if isinstance(source, exp.Query):
            return exp.select("COUNT(*)").from_(source.copy().subquery()).sql("bigquery")
        return None
    if isinstance(statement, exp.Update) and statement.args.get("from"):
        return None
    if isinstance(statement, (exp.Update, exp.Delete)):
        if statement.args.get("using"):
            return None
        query = exp.select("COUNT(*)").from_(statement.this.copy())
        where = statement.args.get("where")
        if where is not None:
            query = query.where(where.this.copy())
        return query.sql("bigquery")
    return None


def empty_result(statement):
    name, kind = (
        ("Success", "BOOLEAN")
        if isinstance(statement, (exp.Drop, exp.Alter))
        else ("Count", "BIGINT")
    )
    return {"columns": [{"name": name, "type": kind}], "rows": [], "truncated": False}


def counted(count):
    return {"columns": [{"name": "Count", "type": "BIGINT"}], "rows": [[count]], "truncated": False}


def error_message(exc) -> str:
    errors = getattr(exc, "errors", None) or []
    if errors and isinstance(errors[0], dict) and errors[0].get("message"):
        return errors[0]["message"]
    return getattr(exc, "message", None) or str(exc) or "BigQuery request failed"


class BigQueryWarehouse:
    dialect = "bigquery"

    def __init__(self, config):
        self.config = config

    def execute(self, sql, observation, *, read_only=False, limit=None, dry_run=False):
        if dry_run:
            raise ToolError("unsupported_sql", COST_REFUSAL)
        try:
            from google.api_core import exceptions as api_exceptions
            from google.cloud import bigquery
            from requests import exceptions as http_exceptions
        except ImportError:
            raise ToolError(
                "warehouse_unconfigured", "Install the gateway bigquery extra"
            ) from None
        statements = inspect_sql(sql, observation, read_only, self.dialect)
        options = load_options(self.config.bigquery_options_env)
        row_limit = min(limit or self.config.max_rows, self.config.max_rows)
        timeout = self.config.sql_timeout_s
        client = make_client(options)
        job_config = bigquery.QueryJobConfig(
            default_dataset=f"{options['project']}.{options['dataset']}",
            use_legacy_sql=False,
        )

        def run(text, max_results):
            job = client.query(
                text, job_config=job_config, retry=None, job_retry=None, timeout=timeout
            )
            return job, job.result(max_results=max_results, timeout=timeout, retry=None)

        results, counts, unreported = [], [], False
        failures = (
            api_exceptions.GoogleAPIError,
            http_exceptions.RequestException,
            concurrent.futures.TimeoutError,
        )
        try:
            for index, statement in enumerate(statements):
                text = sql if len(statements) == 1 else statement.sql(dialect=self.dialect)
                try:
                    if isinstance(statement, (exp.Query, exp.Describe, exp.Show)):
                        _, rows = run(text, row_limit + 1)
                        fetched = [list(row.values()) for row in rows]
                        results.append(
                            {
                                "columns": [
                                    {"name": f.name, "type": bq_type(f)} for f in rows.schema
                                ],
                                "rows": json_value(fetched[:row_limit]),
                                "truncated": len(fetched) > row_limit,
                            }
                        )
                        continue
                    pending = None
                    if isinstance(statement, (exp.Insert, exp.Update, exp.Delete, exp.Merge)):
                        counter = count_sql(statement)
                        if counter is not None and counter.isdigit():
                            pending = int(counter)
                        elif counter is not None:
                            pending = list(run(counter, 1)[1])[0][0]
                    job, _ = run(text, None)
                except failures as exc:
                    message = error_message(exc)
                    if index:
                        message = (
                            f"statement {index + 1} failed: {message}; {index} earlier "
                            "statement(s) were applied and are not rolled back (the BigQuery "
                            "emulator has no transactions)"
                        )
                    raise ToolError("warehouse_error", message) from None
                if not isinstance(statement, (exp.Insert, exp.Update, exp.Delete, exp.Merge)):
                    results.append(empty_result(statement))
                    continue
                reported = getattr(job, "num_dml_affected_rows", None)
                count = reported if reported is not None else pending
                if count is None:
                    unreported = True
                    results.append(empty_result(statement))
                else:
                    counts.append(count)
                    results.append(counted(count))
        finally:
            client.close()
        if unreported:
            observation.flags.append(UNREPORTED)
        elif counts and observation.op_class != "read":
            observation.rows_affected = sum(counts)
        return results[0] if len(results) == 1 else {"statements": results}


# --- seeding: the world builder and tests put rows into the emulator ----------


def _duckdb_to_bigquery(duck_type: str) -> str:
    kind = duck_type.upper()
    if kind in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "UTINYINT", "USMALLINT", "UINTEGER"):
        return "INT64"
    if kind.startswith("DECIMAL"):
        precision, scale = (int(part) for part in kind[8:-1].split(","))
        if scale <= 9 and precision - scale <= 29:
            return "NUMERIC"
        if scale <= 38 and precision - scale <= 38:
            return "BIGNUMERIC"
    simple = {
        "FLOAT": "FLOAT64",
        "DOUBLE": "FLOAT64",
        "VARCHAR": "STRING",
        "BOOLEAN": "BOOL",
        "DATE": "DATE",
        "TIME": "TIME",
        "TIMESTAMP": "DATETIME",
        "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
        "BLOB": "BYTES",
        "JSON": "JSON",
    }
    if kind in simple:
        return simple[kind]
    raise ValueError(f"DuckDB type {duck_type} has no exact BigQuery seed type")


def duckdb_rows(conn, sql: str):
    """`(columns, rows)` for `seed_table` from a DuckDB query; unmappable types raise."""
    cursor = conn.execute(sql)
    columns = [(name, _duckdb_to_bigquery(str(kind))) for name, kind, *_ in cursor.description]
    return columns, [list(row) for row in cursor.fetchall()]


def seed_table(options: dict, table: str, columns, rows) -> None:
    """Create `<dataset>.<table>` with `columns` [(name, BigQuery type)] and load `rows`.

    `rows` are dicts keyed by column name or sequences in column order. The
    dataset is created when absent; the table must not already exist. Any row
    the emulator rejects raises - a partial seed is not a seed.
    """
    from google.api_core import exceptions as api_exceptions
    from google.cloud import bigquery

    check_options(options)
    client = make_client(options)
    try:
        dataset = f"{options['project']}.{options['dataset']}"
        try:
            client.get_dataset(dataset, retry=None)
        except api_exceptions.NotFound:
            client.create_dataset(dataset, retry=None)
        schema = [bigquery.SchemaField(name, kind) for name, kind in columns]
        created = client.create_table(bigquery.Table(f"{dataset}.{table}", schema), retry=None)
        rows = list(rows)
        for start in range(0, len(rows), SEED_BATCH):
            errors = client.insert_rows(created, rows[start : start + SEED_BATCH], retry=None)
            if errors:
                raise ValueError(f"seeding {dataset}.{table} failed: {errors[:3]}")
    finally:
        client.close()
