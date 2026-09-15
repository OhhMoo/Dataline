from typing import Literal

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


class IngestConnector(Arguments):
    connector_id: str


class IngestRuns(Arguments):
    connector_id: str | None = None
    limit: int = Field(default=50, ge=1, le=500)
    continuation_token: str | None = None


class IngestRun(Arguments):
    run_id: str


class IngestAlerts(Arguments):
    connector_id: str | None = None
    acknowledged: bool | None = None


class IngestPolicy(IngestConnector):
    table: str
    column_added: Literal["propagate", "ignore", "alert"] | None = None
    column_removed: Literal["propagate", "ignore", "alert"] | None = None


class MetricRef(Arguments):
    metric: str


class MetricSeriesRef(MetricRef):
    limit: int = Field(default=200, ge=1, le=1000)


class AlarmList(Arguments):
    state: Literal["firing", "ok", "pending", "insufficient_data"] | None = None


class AlarmRef(Arguments):
    alarm: str


class AlarmHistory(Arguments):
    alarm: str | None = None
    limit: int = Field(default=50, ge=1, le=500)


class DeployRun(Arguments):
    deploy: str
    full_refresh: bool = False
    variables: dict = Field(default_factory=dict)


class DeployHistory(Arguments):
    deploy: str | None = None
    limit: int = Field(default=20, ge=1, le=200)


class LogList(Arguments):
    job: str | None = None
    status: str | None = None
    limit: int = Field(default=50, ge=1, le=500)


class LogRef(Arguments):
    run_id: str


class LogSearch(Arguments):
    query: str = Field(min_length=1)
    job: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class IssueList(Arguments):
    status: str | None = None
    assignee: str | None = None
    label: str | None = None
    limit: int = Field(default=50, ge=1, le=500)


class IssueRef(Arguments):
    issue: str = Field(min_length=1)


class IssueComment(IssueRef):
    body: str = Field(min_length=1)


class IssueUpdate(IssueRef):
    status: str | None = None
    assignee: str | None = None
    # Explicit, because "assignee: null" cannot be told apart from "leave the
    # assignee alone" once the argument is optional, and unassigning a ticket is
    # a real thing an engineer does when handing work back to triage.
    unassign: bool = False
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)


class IssueSearch(Arguments):
    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=200)


class ChatHistory(Arguments):
    channel: str = Field(min_length=1)
    limit: int = Field(default=50, ge=1, le=500)
    before: str | None = None


class ChatThread(Arguments):
    message_id: str = Field(min_length=1)


class ChatSearch(Arguments):
    query: str = Field(min_length=1)
    channel: str | None = None
    limit: int = Field(default=20, ge=1, le=200)


class ChatPost(Arguments):
    channel: str = Field(min_length=1)
    text: str = Field(min_length=1)
    thread_id: str | None = None


class PageRef(Arguments):
    page: str = Field(min_length=1)


class PageEdit(PageRef):
    body: str
    comment: str | None = None


class PageCreate(PageRef):
    title: str = Field(min_length=1)
    body: str
    parent: str | None = None
    labels: list[str] = Field(default_factory=list)


class PageSearch(Arguments):
    query: str = Field(min_length=1)
    limit: int = Field(default=20, ge=1, le=200)


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
    "ingest_list": (Empty, "List ingestion connectors with last-run status."),
    "ingest_get": (IngestConnector, "Read one connector's configuration and status."),
    "ingest_runs": (
        IngestRuns,
        "List sync runs, newest first, with continuation for additional pages.",
    ),
    "ingest_run": (IngestRun, "Read one sync run's timeline and per-table row counts."),
    "ingest_schema": (
        IngestConnector,
        "Read a connector's table-level schema configs and drift policies.",
    ),
    "ingest_alerts": (IngestAlerts, "List ingestion alerts, optionally filtered."),
    "ingest_trigger": (IngestConnector, "Trigger a sync run on the virtual clock."),
    "ingest_schema_update": (
        IngestPolicy,
        "Set the schema-drift policy for one connector table.",
    ),
    "metrics_list": (
        Empty,
        "List metrics with their current values, each measured from the world when called.",
    ),
    "metrics_get": (
        MetricRef,
        "Measure one metric now, with the raw reading it was derived from.",
    ),
    "metrics_series": (
        MetricSeriesRef,
        "Read a metric's history: points derived from the world, plus this session's readings.",
    ),
    "alarms_list": (
        AlarmList,
        "Evaluate every alarm against live metrics; optionally filter by state.",
    ),
    "alarms_get": (AlarmRef, "Evaluate one alarm and read its state history."),
    "alarms_history": (AlarmHistory, "Read recorded alarm state transitions."),
    "deploy_list": (Empty, "List deploy targets and the canary each one checks."),
    "deploy_run": (
        DeployRun,
        "Apply a deploy, then re-measure its canary metrics and alarms. "
        "Never blocked; breaches are reported.",
    ),
    "deploy_history": (DeployHistory, "Read recorded deploys and their canary outcomes."),
    "logs_list": (LogList, "List scheduler runs and whether each captured output."),
    "logs_get": (LogRef, "Read one run's output with numbered evidence lines."),
    "logs_search": (
        LogSearch,
        "Search run output for a literal phrase. Runs that could not be read are "
        "listed in 'skipped' with the reason.",
    ),
    "issues_list": (
        IssueList,
        "List issues newest-updated first, with the project's statuses and people.",
    ),
    "issues_get": (IssueRef, "Read one issue with its body, comments, history and links."),
    "issues_search": (
        IssueSearch,
        "Search issue titles, bodies and comments for a literal phrase, with evidence lines.",
    ),
    "issues_comment": (IssueComment, "Add a comment to an issue on the virtual clock."),
    "issues_update": (
        IssueUpdate,
        "Change an issue's status, assignee or labels. Every change is recorded in its history.",
    ),
    "chat_channels": (Empty, "List chat channels with topic, membership and message counts."),
    "chat_history": (
        ChatHistory,
        "Read a channel's thread starters oldest-first, with reply counts.",
    ),
    "chat_thread": (ChatThread, "Read one thread: its root message and every reply in order."),
    "chat_search": (
        ChatSearch,
        "Search message text for a literal phrase across channels or within one.",
    ),
    "chat_post": (ChatPost, "Post a message to a channel, or reply in a thread."),
    "kb_list": (
        Empty,
        "List wiki pages with parent, version, who last edited it and how old that edit is.",
    ),
    "kb_get": (PageRef, "Read a wiki page with numbered evidence lines and its revision history."),
    "kb_search": (
        PageSearch,
        "Search wiki page bodies for a literal phrase; each hit carries its page's edit date.",
    ),
    "kb_edit": (PageEdit, "Replace a wiki page's body; the previous revision is kept in history."),
    "kb_create": (PageCreate, "Create a new wiki page under an existing parent."),
}
