from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: Path = Path("/workspace")
    warehouse: Path = Path("/var/warehouse/warehouse.duckdb")
    warehouse_backend: Literal["duckdb", "postgres", "snowflake"] = "duckdb"
    warehouse_dsn_env: str = "ALKERA_POSTGRES_DSN"
    snowflake_options_env: str = "ALKERA_SNOWFLAKE_OPTIONS"
    bi_state_dir: Path | None = None
    s3_endpoint: str | None = None
    s3_buckets: list[str] = Field(default_factory=list)
    scheduler: Path = Path("/var/sched/state.json")
    ingest: Path = Path("/var/ingest/state.json")
    dashboards: Path = Path("/var/bi/dashboards")
    audit_dir: Path = Path("/audit")
    profiles_dir: Path = Path("/etc/alkera/dbt")
    world_root: Path | None = None
    episode_id: str = "local"
    initial_world_fp: str | None = None
    virtual_time: str = "2026-03-05T09:00:00Z"
    max_rows: int = Field(default=1000, ge=1, le=100_000)
    max_payload_bytes: int = Field(default=1_048_576, ge=1024)
    command_timeout_s: int = Field(default=120, ge=1)
    sql_timeout_s: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def resolve_paths(self):
        for name in (
            "workspace",
            "warehouse",
            "scheduler",
            "ingest",
            "dashboards",
            "audit_dir",
            "profiles_dir",
        ):
            setattr(self, name, getattr(self, name).resolve())
        if self.world_root is not None:
            self.world_root = self.world_root.resolve()
        self.bi_state_dir = (self.bi_state_dir or self.audit_dir / "bi").resolve()
        if self.bi_state_dir.is_relative_to(self.workspace) or (
            self.world_root and self.bi_state_dir.is_relative_to(self.world_root)
        ):
            raise ValueError("bi_state_dir must be outside the world tree")
        if self.warehouse_backend != "duckdb" and not self.initial_world_fp:
            raise ValueError("Remote warehouses require an explicit initial_world_fp")
        if self.audit_dir.is_relative_to(self.workspace):
            raise ValueError("audit_dir must be outside workspace")
        if self.warehouse.is_relative_to(self.workspace):
            raise ValueError("warehouse must be outside workspace")
        return self

    @classmethod
    def load(cls, path: Path):
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
