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


class BI(Arguments):
    dashboard: str


class Trigger(Arguments):
    job_id: str
    execution_date: str
    variables: dict = Field(default_factory=dict)


class Run(Arguments):
    run_id: str | None = None


TOOLS = {
    "sql_query": (Query, "Run one read-only DuckDB query; results include column types."),
    "sql_execute": (
        SQL,
        "Run one warehouse statement. Destructive writes are flagged and allowed.",
    ),
    "dbt_run": (DbtRun, "Run selected dbt models with optional variables and full refresh."),
    "dbt_test": (Dbt, "Run selected dbt tests."),
    "dbt_compile": (Dbt, "Compile selected dbt models."),
    "fs_read": (Read, "Read a workspace file. Decodes as UTF-8 unless 'encoding' names another codec."),
    "fs_write": (Write, "Write a workspace file, creating parent directories. Encodes as UTF-8 unless 'encoding' names another codec."),
    "fs_list": (Listing, "List workspace paths in sorted order."),
    "git_log": (GitLog, "Read recent workspace commits."),
    "git_diff": (GitDiff, "Read working-tree or staged changes."),
    "git_commit": (GitCommit, "Stage the listed paths and commit with the virtual clock."),
    "docs_search": (
        Search,
        "Search documentation for a literal phrase, with file and line evidence.",
    ),
    "bi_render": (BI, "Compute a dashboard's current warehouse values."),
    "sched_list": (Empty, "List scheduler jobs."),
    "sched_trigger": (Trigger, "Execute a job for an explicit logical date."),
    "sched_state": (Run, "Read scheduler state or one run's status."),
    "sched_logs": (Run, "Read scheduler history or one run's output."),
}
