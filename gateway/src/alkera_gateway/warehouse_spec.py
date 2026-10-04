"""One declared warehouse. Imports nothing from this package, so `config` can
hold a list of these without a cycle.

The primary warehouse is NOT spelled here. It keeps the fields it has always
had - `warehouse`, `warehouse_backend`, `warehouse_dsn_env`,
`snowflake_options_env` - because every existing world, task and theme is
written against those names, and a migration that rewrites them is not a
migration. `GatewayConfig.warehouses` lists the *additional* warehouses an
episode runs, so a world that declares one warehouse declares nothing here and
its config bytes, its tool surface and its action log are unchanged by
construction rather than by care.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME_PATTERN = r"^[a-z][a-z0-9_]{0,30}$"


class WarehouseSpec(BaseModel):
    """An additional warehouse: a name the agent can say, and how to reach it.

    `name` is a LOGICAL name. It is the only warehouse-addressing token that
    ever reaches a tool argument or the action log; a host, port, DSN or
    Snowflake account never does. That is deliberate and it is enforced from
    outside: `gateway/docker/containment.py` walks every registered tool's
    pydantic fields and fails the run if any field is named host, hostname,
    port, url, uri, endpoint, dsn, server, address, addr, proxy, connection,
    conn, account or network (`no_socket_primitive`). A name resolved against
    config keeps that property; a connection string in a tool argument would
    destroy it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    backend: Literal["duckdb", "postgres", "snowflake", "bigquery"] = "duckdb"
    # duckdb only. Resolved against the episode's world root by the registry
    # when relative, so a seed file can name `var/warehouse/legacy.duckdb`.
    path: Path | None = None
    # The environment variable holding the secret. Never the secret itself:
    # config files are copied into scratch directories, quoted in reports and
    # read by the grader.
    dsn_env: str = "ALKERA_POSTGRES_DSN"
    options_env: str = "ALKERA_SNOWFLAKE_OPTIONS"
    # Read-only warehouses are attached READ_ONLY and refuse every write at the
    # engine, not at the tool layer. This is how "decommission the old writer"
    # becomes a state the world can be *in*, rather than an instruction the
    # grader has to take the agent's word for.
    read_only: bool = False
    # Whether a DuckDB primary should ATTACH this warehouse into the same
    # catalog so the agent can join across the two in one statement. Only
    # meaningful when both ends are reachable from DuckDB; the registry
    # decides and records what it actually managed.
    attach: bool = True
    # The profiles.yml output the dbt_* tools select (`dbt --target <name>`)
    # when the agent passes `warehouse=<this name>`. None means dbt cannot be
    # pointed at this warehouse: the dbt tools do not list it, and a world
    # that has only a reconciliation peer keeps the dbt tool schema it has
    # today. The world builder sets it to the warehouse's own name for a
    # DuckDB file, because world/world/executor.py renders an output per
    # `var/warehouse/<name>.duckdb` named by the stem.
    dbt_target: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

    @model_validator(mode="after")
    def check_backend_fields(self):
        # The options variable follows the backend unless the spec names one.
        if self.backend == "bigquery" and "options_env" not in self.model_fields_set:
            self.options_env = "ALKERA_BIGQUERY_OPTIONS"
        if self.backend == "duckdb" and self.path is None:
            raise ValueError(f"warehouse {self.name!r} is duckdb and needs a path")
        if self.backend != "duckdb" and self.path is not None:
            raise ValueError(f"warehouse {self.name!r} is {self.backend} and must not carry a path")
        return self
