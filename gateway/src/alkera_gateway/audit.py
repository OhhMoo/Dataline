import json
import os
from importlib.metadata import version
from pathlib import Path

from filelock import FileLock

from .config import GatewayConfig
from .records import canonical, digest


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class AuditLog:
    def __init__(self, config: GatewayConfig):
        self.directory = config.audit_dir
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(self.directory / "writer.lock")
        self.lock.acquire(timeout=0)
        try:
            self.actions = self.directory / "actions.jsonl"
            self.requests = self.directory / "requests.jsonl"
            rows = read_rows(self.actions)
            requests = read_rows(self.requests)
            if [row["seq"] for row in rows] != list(range(1, len(rows) + 1)):
                raise ValueError("Audit sequence is not contiguous")
            if [row["seq"] for row in requests] != [row["seq"] for row in rows]:
                raise ValueError(
                    "Unfinished request; inspect the world before starting a new episode"
                )
            self.seq = len(rows)
            manifest_path = self.directory / "session.json"
            versions = {
                name: version(name)
                for name in (
                    "alkera-gateway",
                    "dbt-core",
                    "dbt-duckdb",
                    "duckdb",
                    "mcp",
                    "pydantic",
                    "PyYAML",
                    "sqlglot",
                    "psycopg",
                    "boto3",
                )
            }
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest["episode_id"] != config.episode_id:
                    raise ValueError("Audit directory belongs to another episode")
                if manifest["initial_world_fp"] != config.initial_world_fp:
                    raise ValueError("Audit directory belongs to another initial world")
                if manifest["versions"] != versions:
                    raise ValueError("Audit directory uses another dependency set")
            else:
                manifest = {
                    "schema_version": 1,
                    "episode_id": config.episode_id,
                    "initial_world_fp": config.initial_world_fp,
                    "versions": versions,
                }
                manifest_path.write_bytes(canonical(manifest))
        except Exception:
            self.lock.release()
            raise

    def append(self, path: Path, row: dict):
        with path.open("ab") as stream:
            stream.write(canonical(row) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def begin(self, request: dict):
        self.append(self.requests, request)

    def finish(self, row: dict):
        self.append(self.actions, row)
        self.seq = row["seq"]

    def close(self):
        self.lock.release()


def validate_trace(path: Path) -> list[dict]:
    rows = read_rows(path)
    for seq, row in enumerate(rows, 1):
        if row["seq"] != seq:
            raise ValueError(f"Invalid sequence at row {seq}")
        if digest(row["args"]) != row["args_hash"]:
            raise ValueError(f"Argument hash mismatch at row {seq}")
        if digest(row["result"]) != row["result_hash"]:
            raise ValueError(f"Result hash mismatch at row {seq}")
    return rows
