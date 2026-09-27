from .bi import BIStore
from .commands import Commands
from .files import Workspace
from .ingest import IngestStore
from .observability import ObservabilityStore
from .records import ToolError
from .runlogs import RunLogs
from .storage import ObjectStorage
from .surfaces import FileScheduler, render_dashboard
from .warehouses import create_registry
from .workflow import Chat, IssueTracker, KnowledgeBase


def named(value, fallback: dict) -> dict:
    """`{key, name}` from a workflow vocabulary header - the tracker's `project`,
    the wiki's `space`, the chat `workspace` - which a theme may declare as a
    mapping or as the bare key (GT002 says `project: DATA` and `space: de`;
    GT001 says `workspace: {id, name}`). The same rule as `_named` in
    world/world/native_workspace.py, plus `id`, so the page the agent reads
    first and the services it then calls name the same thing. `fallback` only
    when the world declares nothing."""
    if isinstance(value, dict):
        key = str(value.get("key") or value.get("id") or value.get("name") or "")
        if key:
            return {"key": key, "name": str(value.get("name") or key)}
    elif value:
        return {"key": str(value), "name": str(value)}
    return dict(fallback)


class Operations:
    def __init__(self, config, clock, scheduler=None):
        self.config = config
        self.clock = clock
        self.workspace = Workspace(config)
        # One warehouse: `registry.primary` IS `create_warehouse(config)`.
        self.warehouses = create_registry(config)
        self.warehouse = self.warehouses.primary
        self.bi = BIStore(config, self.warehouse, clock)
        self.storage = ObjectStorage(config)
        self.commands = Commands(config, self.workspace)
        self.scheduler = scheduler or FileScheduler(config.scheduler, self.commands, clock)
        self.ingest = IngestStore(config.ingest, clock)
        self.observability = ObservabilityStore(
            config, self.warehouse, self.scheduler, self.commands, clock
        )
        self.runlogs = RunLogs(self.scheduler, config)
        actor = config.workflow_actor
        self.issues = IssueTracker(config.workflow_dir, clock, actor)
        self.chat = Chat(config.workflow_dir, clock, actor)
        self.kb = KnowledgeBase(config.workflow_dir, clock, actor)

    def sql_query(self, sql, limit, observation, warehouse=None):
        return self.warehouses.execute(
            warehouse, sql, observation, read_only=True, limit=limit
        )

    def sql_execute(self, sql, observation, warehouse=None):
        return self.warehouses.execute(warehouse, sql, observation)

    # `warehouse` on the three dbt tools follows the sql_query rule exactly:
    # the argument exists in the served schema only when more than one
    # warehouse can be a dbt target (warehouses.build_tools), so a world with
    # one warehouse - or one warehouse plus a peer dbt cannot build - keeps
    # today's schema and today's action rows. When it is present the registry
    # resolves it to a profiles.yml output and Commands.dbt runs `--target`.
    def dbt_run(self, selector, variables, full_refresh, observation, warehouse=None):
        return self.commands.dbt(
            "run", selector, variables, full_refresh, observation,
            **self.warehouses.dbt_plan(warehouse),
        )

    def dbt_test(self, selector, variables, observation, warehouse=None):
        return self.commands.dbt(
            "test", selector, variables, False, observation,
            **self.warehouses.dbt_plan(warehouse),
        )

    def dbt_compile(self, selector, variables, observation, warehouse=None):
        return self.commands.dbt(
            "compile", selector, variables, False, observation,
            **self.warehouses.dbt_plan(warehouse),
        )

    def fs_read(self, path, observation, encoding="utf-8"):
        return self.workspace.read(path, observation, encoding)

    def fs_write(self, path, content, observation, encoding="utf-8"):
        return self.workspace.write(path, content, observation, encoding)

    def fs_list(self, path, recursive, observation):
        return self.workspace.list(path, recursive, observation)

    def docs_search(self, query, limit, observation, encoding="utf-8"):
        return self.workspace.search(query, limit, observation, encoding)

    def docs_list(self, observation):
        return self.workspace.list("docs", True, observation)

    def docs_get(self, path, observation):
        resolved = self.workspace.path(path)
        if not resolved.is_relative_to(self.config.workspace / "docs"):
            raise ToolError("path_denied", "Documentation must be inside workspace/docs")
        result = self.workspace.read(path, observation)
        result["lines"] = [
            {"line": n, "text": line} for n, line in enumerate(result["content"].splitlines(), 1)
        ]
        return result

    def bi_list(self, observation):
        return self.bi.list(observation)

    def bi_get(self, dashboard, observation):
        return self.bi.get(dashboard, observation)

    def bi_cached(self, dashboard, observation):
        return self.bi.cached(dashboard, observation)

    def bi_refresh(self, dashboard, observation):
        return self.bi.refresh(dashboard, observation)

    def bi_update(self, dashboard, definition, observation):
        return self.bi.update(dashboard, definition, observation)

    def s3_list(self, bucket, prefix, continuation_token, limit, observation):
        return self.storage.list(bucket, prefix, continuation_token, limit, observation)

    def s3_get(self, bucket, key, observation):
        return self.storage.get(bucket, key, observation)

    def s3_put(self, bucket, key, body_base64, metadata, observation):
        return self.storage.put(bucket, key, body_base64, metadata, observation)

    def s3_delete(self, bucket, key, observation):
        return self.storage.delete(bucket, key, observation)

    def git_log(self, limit, observation):
        observation.read_targets = ["workspace"]
        return self.commands.git(
            ["log", f"-{limit}", "--format=%H %aI %s"],
            vts=self.clock(),
            observation=observation,
        )

    def git_diff(self, staged, observation):
        observation.read_targets = ["workspace"]
        args = ["diff", "--no-ext-diff", "--no-textconv"]
        if staged:
            args.append("--cached")
        return self.commands.git(args, vts=self.clock(), observation=observation)

    def git_commit(self, message, paths, observation):
        return self.commands.git(
            ["commit", "-m", message],
            vts=self.clock(),
            observation=observation,
            paths=paths,
        )

    def bi_render(self, dashboard, observation):
        return render_dashboard(self.config.dashboards, dashboard, self.warehouse, observation)

    def sched_list(self, observation):
        observation.read_targets = observation.targets = ["scheduler"]
        return self.scheduler.list()

    def sched_trigger(self, job_id, execution_date, variables, observation):
        observation.op_class = "exec"
        observation.targets = [f"scheduler:{job_id}", "dbt_project"]
        return self.scheduler.trigger(job_id, execution_date, variables)

    def sched_state(self, run_id, observation):
        target = f"scheduler:{run_id}" if run_id else "scheduler"
        observation.read_targets = observation.targets = [target]
        return self.scheduler.state(run_id)

    def sched_logs(self, run_id, observation):
        target = f"scheduler:{run_id}" if run_id else "scheduler"
        observation.read_targets = observation.targets = [target]
        return self.scheduler.logs(run_id)

    def ingest_list(self, observation):
        observation.read_targets = observation.targets = ["ingest"]
        return self.ingest.list_connectors()

    def ingest_get(self, connector_id, observation):
        observation.read_targets = observation.targets = [f"ingest:{connector_id}"]
        return self.ingest.get_connector(connector_id)

    def ingest_runs(self, connector_id, limit, continuation_token, observation):
        target = f"ingest:{connector_id}" if connector_id else "ingest"
        observation.read_targets = observation.targets = [target]
        return self.ingest.list_runs(connector_id, limit, continuation_token)

    def ingest_run(self, run_id, observation):
        observation.read_targets = observation.targets = [f"ingest:{run_id}"]
        return self.ingest.get_run(run_id)

    def ingest_schema(self, connector_id, observation):
        observation.read_targets = observation.targets = [f"ingest:{connector_id}"]
        return self.ingest.get_schema(connector_id)

    def ingest_alerts(self, connector_id, acknowledged, observation):
        target = f"ingest:{connector_id}" if connector_id else "ingest"
        observation.read_targets = observation.targets = [target]
        return self.ingest.list_alerts(connector_id, acknowledged)

    def ingest_trigger(self, connector_id, observation):
        observation.op_class = "exec"
        observation.targets = [f"ingest:{connector_id}"]
        return self.ingest.trigger(connector_id)

    def ingest_schema_update(self, connector_id, table, column_added, column_removed, observation):
        observation.op_class = "write"
        observation.targets = [f"ingest:{connector_id}"]
        return self.ingest.set_policy(connector_id, table, column_added, column_removed)

    def metrics_list(self, observation):
        return self.observability.list_metrics(observation)

    def metrics_get(self, metric, observation):
        return self.observability.get_metric(metric, observation)

    def metrics_series(self, metric, limit, observation):
        return self.observability.get_series(metric, limit, observation)

    def alarms_list(self, state, observation):
        return self.observability.list_alarms(state, observation)

    def alarms_get(self, alarm, observation):
        return self.observability.get_alarm(alarm, observation)

    def alarms_history(self, alarm, limit, observation):
        return self.observability.alarm_history(alarm, limit, observation)

    # The alarm surface's write. Until this landed the 63-tool list had three
    # alarm reads and no write at all, so the native Alertmanager's silences
    # lived in its own process: audited, invisible to the world, gone on restart
    # (SERVICES_B_REQUESTS.md item 2). `alarms_ack` puts the state in the world
    # where both transports reach it. It is an operation, not yet a registered
    # MCP tool - adding one changes the served tool list, which is `tools.py`'s
    # to decide and is recorded as the remaining step in INTEGRATION_GATEWAY.md.
    def alarms_ack(self, alarms, until, comment, actor, observation):
        return self.observability.silence(alarms, until, comment, actor, observation)

    def alarms_silences(self, observation):
        return self.observability.list_silences(observation)

    def alarms_unack(self, silence_id, observation):
        return self.observability.expire_silence(silence_id, observation)

    def deploy_list(self, observation):
        return self.observability.list_deploys(observation)

    def deploy_run(self, deploy, full_refresh, variables, observation):
        return self.observability.run_deploy(deploy, full_refresh, variables, observation)

    def deploy_history(self, deploy, limit, observation):
        return self.observability.deploy_history(deploy, limit, observation)

    # An operator's verdict on a deploy, on the same footing as `alarms_ack`:
    # the native release service used to hold these in memory too.
    def deploy_status(self, deploy, state, description, actor, observation):
        return self.observability.deploy_status(deploy, state, description, actor, observation)

    def deploy_statuses(self, deploy, limit, observation):
        return self.observability.deploy_statuses(deploy, limit, observation)

    def logs_list(self, job, status, limit, observation):
        return self.runlogs.list(job, status, limit, observation)

    def logs_get(self, run_id, observation):
        return self.runlogs.get(run_id, observation)

    def logs_search(self, query, job, limit, observation):
        return self.runlogs.search(query, job, limit, observation)

    # SERVICES_A_REQUESTS.md item 3. A Jira issue has to name a project and a
    # Slack `auth.test` has to name a workspace, and neither was on any result,
    # so the two native services opened the raw document for that one header -
    # the only place either of them went round `workflow.py`, which is the code
    # that strips the author-side `authoring` block on the way out of a record.
    # Both headers are in the documents already and neither is author-side.
    PROJECT = {"key": "JIRA", "name": "JIRA"}
    WORKSPACE = "alkera"

    def issues_list(self, status, assignee, label, limit, observation):
        observation.read_targets = observation.targets = ["issues"]
        listing = self.issues.list_issues(status, assignee, label, limit)
        return {**listing, "project": named(self.issues.load().get("project"), self.PROJECT)}

    def issues_get(self, issue, observation):
        observation.read_targets = observation.targets = [f"issue:{issue}"]
        return self.issues.get_issue(issue)

    def issues_search(self, query, limit, observation):
        observation.read_targets = observation.targets = ["issues"]
        return self.issues.search(query, limit)

    def issues_comment(self, issue, body, observation):
        observation.op_class = "write"
        observation.targets = [f"issue:{issue}"]
        return self.issues.comment(issue, body)

    def issues_update(
        self, issue, status, assignee, unassign, add_labels, remove_labels, observation
    ):
        observation.op_class = "write"
        observation.targets = [f"issue:{issue}"]
        result = self.issues.update(issue, status, assignee, unassign, add_labels, remove_labels)
        if not result["changes"]:
            # A write that changed nothing says so rather than reporting success
            # over an unchanged ticket.
            observation.flags.append("no_change")
        return result

    def chat_channels(self, observation):
        observation.read_targets = observation.targets = ["chat"]
        listing = self.chat.list_channels()
        # Served as one string (Slack's `team`), the name a person would read.
        workspace = named(self.chat.load().get("workspace"),
                          {"key": self.WORKSPACE, "name": self.WORKSPACE})
        return {**listing, "workspace": workspace["name"]}

    def chat_history(self, channel, limit, before, observation):
        observation.read_targets = observation.targets = [f"chat:{channel}"]
        return self.chat.history(channel, limit, before)

    def chat_thread(self, message_id, observation):
        result = self.chat.thread(message_id)
        observation.read_targets = observation.targets = [
            f"chat:{result['channel']}",
            f"thread:{result['thread_id']}",
        ]
        return result

    def chat_search(self, query, channel, limit, observation):
        target = f"chat:{channel}" if channel else "chat"
        observation.read_targets = observation.targets = [target]
        return self.chat.search(query, channel, limit)

    def chat_post(self, channel, text, thread_id, observation):
        observation.op_class = "write"
        observation.targets = [f"chat:{channel}"]
        return self.chat.post(channel, text, thread_id)

    def kb_list(self, observation):
        observation.read_targets = observation.targets = ["kb"]
        return self.kb.list_pages()

    def kb_get(self, page, observation):
        observation.read_targets = observation.targets = [f"kb:{page}"]
        return self.kb.get_page(page)

    def kb_search(self, query, limit, observation):
        observation.read_targets = observation.targets = ["kb"]
        return self.kb.search(query, limit)

    def kb_edit(self, page, body, comment, observation):
        observation.op_class = "write"
        observation.targets = [f"kb:{page}"]
        result = self.kb.edit(page, body, comment)
        observation.flags.append("overwrite")
        return result

    def kb_create(self, page, title, body, parent, labels, observation):
        observation.op_class = "write"
        observation.targets = [f"kb:{page}"]
        return self.kb.create(page, title, body, parent, labels)
