from .commands import Commands
from .files import Workspace
from .sql import Warehouse
from .surfaces import FileScheduler, render_dashboard


class Operations:
    def __init__(self, config, clock, scheduler=None):
        self.config = config
        self.clock = clock
        self.workspace = Workspace(config)
        self.warehouse = Warehouse(config)
        self.commands = Commands(config, self.workspace)
        self.scheduler = scheduler or FileScheduler(config.scheduler, self.commands, clock)

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

    def fs_read(self, path, observation):
        return self.workspace.read(path, observation)

    def fs_write(self, path, content, observation):
        return self.workspace.write(path, content, observation)

    def fs_list(self, path, recursive, observation):
        return self.workspace.list(path, recursive, observation)

    def docs_search(self, query, limit, observation):
        return self.workspace.search(query, limit, observation)

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
