import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil
import yaml

from .config import GatewayConfig
from .records import Observation, ToolError


def run_command(argv: list[str], cwd: Path, config: GatewayConfig, env=None):
    with tempfile.TemporaryFile() as output:
        # Children must not inherit the MCP JSON-RPC input pipe.
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        timed_out = False
        try:
            process.wait(timeout=config.command_timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.kill()
            process.wait()
        output.seek(0)
        data = output.read(config.max_payload_bytes // 2 + 1)
    limit = config.max_payload_bytes // 2
    return {
        "returncode": process.returncode,
        "output": data[:limit].decode("utf-8", errors="replace"),
        "truncated": len(data) > limit,
        "timed_out": timed_out,
    }


class Commands:
    def __init__(self, config: GatewayConfig, workspace):
        self.config = config
        self.workspace = workspace

    def dbt(self, command: str, selector: str, variables: dict, full_refresh: bool, observation):
        if self.config.warehouse_backend != "duckdb":
            profile_path = self.config.profiles_dir / "profiles.yml"
            profiles = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            outputs = [
                profile["outputs"][profile["target"]]
                for profile in profiles.values()
                if isinstance(profile, dict) and "outputs" in profile and "target" in profile
            ]
            if not outputs or any(
                output.get("type") != self.config.warehouse_backend for output in outputs
            ):
                raise ToolError("backend_mismatch", "dbt profile must match the warehouse backend")
        # A direct venv Python launch does not add its scripts directory to PATH.
        executable = shutil.which("dbt", path=str(Path(sys.executable).parent)) or shutil.which(
            "dbt"
        )
        if executable is None:
            raise ToolError("dependency_missing", "Install the dbt extra to run dbt tools")
        observation.op_class = "exec"
        observation.targets = ["dbt_project"]
        if full_refresh:
            observation.op_class = "destructive"
            observation.flags.append("full_refresh")
        argv = [
            executable,
            "--no-use-colors",
            "--no-send-anonymous-usage-stats",
            command,
            "--project-dir",
            str(self.workspace.path("dbt_project")),
            "--profiles-dir",
            str(self.config.profiles_dir),
            "--threads",
            "1",
            "--target-path",
            str(self.config.audit_dir / "dbt-target"),
            "--log-path",
            str(self.config.audit_dir / "dbt-logs"),
            "--select",
            selector,
            "--vars",
            json.dumps(variables, sort_keys=True),
        ]
        if full_refresh:
            argv.append("--full-refresh")
        env = {**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false", "PYTHONHASHSEED": "0"}
        for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            env[name] = "1"
        return run_command(argv, self.config.workspace, self.config, env)

    def git(self, args: list[str], *, vts: str, observation: Observation, paths=None):
        observation.targets = ["workspace"]
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Agent",
            "GIT_AUTHOR_EMAIL": "agent@alkera.local",
            "GIT_COMMITTER_NAME": "Agent",
            "GIT_COMMITTER_EMAIL": "agent@alkera.local",
            "GIT_AUTHOR_DATE": vts,
            "GIT_COMMITTER_DATE": vts,
        }
        base = [
            "git",
            "--no-pager",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.quotePath=false",
        ]
        if paths:
            names = [self.workspace.name(self.workspace.path(path)) for path in paths]
            observation.op_class = "write"
            staged = run_command(
                [*base, "--literal-pathspecs", "add", "--", *names],
                self.config.workspace,
                self.config,
                env,
            )
            if staged["returncode"]:
                return staged
        return run_command([*base, *args], self.config.workspace, self.config, env)
