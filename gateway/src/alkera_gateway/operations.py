from .bi import BIStore
from .commands import Commands
from .files import Workspace
from .ingest import IngestStore
from .observability import ObservabilityStore
from .records import ToolError
from .runlogs import RunLogs
from .storage import ObjectStorage
from .surfaces import FileScheduler, render_dashboard
from .warehouse import create_warehouse
from .workflow import Chat, IssueTracker, KnowledgeBase


class Operations:
    def __init__(self, config, clock, scheduler=None):
        self.config = config
        self.clock = clock
        self.workspace = Workspace(config)
        self.warehouse = create_warehouse(config)
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

    def sql_query(self, sql, limit, observation):
        return self.warehouse.execute(sql, observation, read_only=True, limit=limit)

    def sql_execute(self, sql, observation):
        return self.warehouse.execute(sql, observation)

    def dbt_run(self, selector, variables, full_refresh, observation):
        return self.commands.dbt("run", selector, variables, full_refresh, observation)

    def dbt_test(self, selector, variables, observation):
        return self.commands.dbt("test", selector, variables, False, observation)

    def dbt_compile(self, selector, variables, observation):
        return self.commands.dbt("compile", selector, variables, False, observation)

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

    def deploy_list(self, observation):
        return self.observability.list_deploys(observation)

    def deploy_run(self, deploy, full_refresh, variables, observation):
        return self.observability.run_deploy(deploy, full_refresh, variables, observation)

    def deploy_history(self, deploy, limit, observation):
        return self.observability.deploy_history(deploy, limit, observation)

    def logs_list(self, job, status, limit, observation):
        return self.runlogs.list(job, status, limit, observation)

    def logs_get(self, run_id, observation):
        return self.runlogs.get(run_id, observation)

    def logs_search(self, query, job, limit, observation):
        return self.runlogs.search(query, job, limit, observation)

    def issues_list(self, status, assignee, label, limit, observation):
        observation.read_targets = observation.targets = ["issues"]
        return self.issues.list_issues(status, assignee, label, limit)

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
        return self.chat.list_channels()

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
