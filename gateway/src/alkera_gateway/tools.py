from pydantic import BaseModel, ConfigDict, Field


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(Arguments):
    pass


class SQL(Arguments):
    sql: str = Field(min_length=1)


class Query(SQL):
    limit: int = Field(default=1000, ge=1, le=100_000)


class Dbt(Arguments):
    selector: str = "*"
    variables: dict = Field(default_factory=dict)


class DbtRun(Dbt):
    full_refresh: bool = False


class Read(Arguments):
    path: str
    # Real data work runs into files that are not UTF-8 - EBCDIC mainframe
    # extracts, cp1252 exports, BOMs. Hard-coding utf-8 made that whole failure
    # class unrepresentable, which is worse than useless in a benchmark whose
    # point is to plant such defects.
    encoding: str = "utf-8"


class Write(Read):
    content: str


class Listing(Arguments):
    path: str = "."
    recursive: bool = False


class GitLog(Arguments):
    limit: int = Field(default=20, ge=1, le=200)


class GitDiff(Arguments):
    staged: bool = False


class GitCommit(Arguments):
    message: str = Field(min_length=1)
    paths: list[str] = Field(min_length=1)


class Search(Arguments):
    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=100)
    # Symmetric with fs_read: a document the agent cannot decode is named in
    # `skipped`, and this is how it asks for that document in the right codec.
    encoding: str = "utf-8"


class BI(Arguments):
    dashboard: str


class BIUpdate(BI):
    definition: dict


class Trigger(Arguments):
    job_id: str
    execution_date: str
    variables: dict = Field(default_factory=dict)


class Run(Arguments):
    run_id: str | None = None


class Doc(Arguments):
    path: str


class Bucket(Arguments):
    bucket: str


class ObjectList(Bucket):
    prefix: str = ""
    continuation_token: str | None = None
    limit: int = Field(default=1000, ge=1, le=1000)


class ObjectKey(Bucket):
    key: str = Field(min_length=1)


class ObjectPut(ObjectKey):
    body_base64: str
    metadata: dict[str, str] = Field(default_factory=dict)


TOOLS = {
    "sql_query": (Query, "Run one read-only warehouse query; results include column types."),
    "sql_execute": (
        SQL,
        "Run warehouse statements atomically. Destructive writes are flagged and allowed.",
    ),
    "dbt_run": (DbtRun, "Run selected dbt models with optional variables and full refresh."),
    "dbt_test": (Dbt, "Run selected dbt tests."),
    "dbt_compile": (Dbt, "Compile selected dbt models."),
    "fs_read": (
        Read,
        "Read a workspace file. Decodes as UTF-8 unless 'encoding' names another codec.",
    ),
    "fs_write": (
        Write,
        "Write a workspace file and create parents. Defaults to UTF-8; accepts another encoding.",
    ),
    "fs_list": (Listing, "List workspace paths in sorted order."),
    "git_log": (GitLog, "Read recent workspace commits."),
    "git_diff": (GitDiff, "Read working-tree or staged changes."),
    "git_commit": (GitCommit, "Stage the listed paths and commit with the virtual clock."),
    "docs_search": (
        Search,
        "Search documentation for a literal phrase, with file and line evidence. "
        "Documents that could not be read are listed in 'skipped' with the reason; "
        "re-run with 'encoding' to search one in its own codec.",
    ),
    "bi_render": (BI, "Compute a dashboard's current warehouse values."),
    "docs_list": (Empty, "List the seeded documentation tree."),
    "docs_get": (Doc, "Read a documentation page with numbered evidence lines."),
    "bi_list": (Empty, "List dashboards and their metric definitions."),
    "bi_get": (BI, "Read dashboard ownership, panel SQL, metrics and source documents."),
    "bi_cached": (BI, "Read cached values and refresh events without recomputing."),
    "bi_update": (
        BIUpdate,
        "Replace a dashboard definition. Cached values change only on refresh.",
    ),
    "bi_refresh": (BI, "Refresh cached values, recording virtual time and source watermarks."),
    "s3_list": (ObjectList, "List bucket objects, with continuation for additional pages."),
    "s3_get": (ObjectKey, "Read an object's exact bytes as base64 and its metadata."),
    "s3_put": (ObjectPut, "Write base64 bytes and metadata to an object; may overwrite."),
    "s3_delete": (ObjectKey, "Delete an object. Destructive operations are flagged and allowed."),
    "sched_list": (Empty, "List scheduler jobs."),
    "sched_trigger": (Trigger, "Execute a job for an explicit logical date."),
    "sched_state": (Run, "Read scheduler state or one run's status."),
    "sched_logs": (Run, "Read scheduler history or one run's output."),
}
