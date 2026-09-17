"""Two live warehouses in one episode, without changing what one warehouse does.

The whole design rests on a single rule: **a world that declares one warehouse
must be indistinguishable from today**. Not "close to", not "equivalent modulo
a null field" - indistinguishable, in the config bytes, in the JSON schema the
agent is served, and in the canonical bytes of every action-log row. So:

* `build_tools(config)` returns the `TOOLS` object *itself* when one warehouse
  is declared. Not a copy. Identity, so there is no way for the surface to
  drift and no test that can pass by accident.
* `create_registry(config).primary` is exactly `create_warehouse(config)` -
  same class, same construction - and `Registry.execute(None, ...)` calls it
  with the same arguments `Operations.sql_query` passes today.
* Targets are qualified (`legacy:warehouse.orders`) only when there is more
  than one warehouse to disambiguate between.

The second rule is that the *name* is the only addressing token. A host, port,
DSN or Snowflake account never reaches a tool argument, so
`gateway/docker/containment.py`'s `no_socket_primitive` property (it rejects
fields named host/port/url/dsn/endpoint/account/... by name) stays true with no
change to that probe.

What is actually possible across two warehouses - measured, not assumed:

  DuckDB primary + DuckDB peer     cross-catalog SQL. Real FULL OUTER JOIN and
                                   EXCEPT in one statement.
  DuckDB primary + Postgres peer   cross-engine SQL, but ONLY if the `postgres`
                                   extension is baked into the image. `INSTALL`
                                   needs the extension repository over the
                                   network and the episode zone has no egress,
                                   so it cannot be fetched at run time.
  anything + Snowflake             no SQL join is possible in either direction.
                                   The connector opens a session, runs one
                                   statement and closes; it cannot see another
                                   catalog. Reconciliation against Snowflake
                                   happens in the agent's own code: read both
                                   sides, diff in Python.

Containment survives all of it, and not by our good intentions. The gateway
attaches every peer BEFORE it locks the DuckDB connection down, and after the
lockdown DuckDB refuses a new ATTACH outright ("Attaching Postgres databases is
disabled through configuration", "file system operations are disabled by
configuration"). So the set of warehouses the agent can reach is exactly the set
the world declared, and a peer declared `read_only` is refused writes by the
engine rather than by a rule we would have to keep enforcing.
"""

import importlib.util
from functools import cache
from typing import Literal

import duckdb

from .config import GatewayConfig
from .records import Observation, ToolError
from .tools import SQL, TOOLS, Dbt, DbtRun, Query
from .warehouse import DuckDBWarehouse, create_warehouse
from .warehouse_spec import WarehouseSpec

PRIMARY_ALIAS = "wh"

# The dbt adapter each backend needs before `dbt --target <output>` can run
# against it. dbt-duckdb is in gateway/requirements.lock; dbt-postgres is not,
# and dbt-snowflake is not, so a world that declares a dbt target on either is
# refused at construction with the package named, instead of the first
# `dbt_run(warehouse=...)` failing with "Could not find adapter type" after the
# task instruction has promised the agent it can build the target with dbt.
DBT_ADAPTERS = {
    "duckdb": ("dbt.adapters.duckdb", "dbt-duckdb"),
    "postgres": ("dbt.adapters.postgres", "dbt-postgres"),
    "snowflake": ("dbt.adapters.snowflake", "dbt-snowflake"),
}


def dbt_adapter_missing(backend: str) -> str | None:
    """The distribution name of the adapter `backend` needs, if it is absent."""
    module, distribution = DBT_ADAPTERS[backend]
    return None if importlib.util.find_spec(module) else distribution


def warehouse_specs(config: GatewayConfig) -> dict[str, WarehouseSpec]:
    """Every warehouse this episode runs, primary first.

    The primary is synthesised from the fields it has always had, so a world
    that declares no `warehouses` still gets a one-entry mapping and every
    caller below can be written once.
    """
    primary = WarehouseSpec(
        name=getattr(config, "warehouse_name", "primary"),
        backend=config.warehouse_backend,
        path=config.warehouse if config.warehouse_backend == "duckdb" else None,
        dsn_env=config.warehouse_dsn_env,
        options_env=config.snowflake_options_env,
        read_only=False,
        attach=False,
    )
    specs = {primary.name: primary}
    for spec in getattr(config, "warehouses", ()) or ():
        if spec.name in specs:
            raise ValueError(f"warehouse name {spec.name!r} is declared twice")
        specs[spec.name] = spec
    return specs


def derive_config(config: GatewayConfig, spec: WarehouseSpec) -> GatewayConfig:
    """The config a driver would have been given if this warehouse were the only one.

    `model_copy` rather than a constructor on purpose: the validators must not
    re-run. `resolve_paths` asserts things about the *episode* (the audit
    directory is outside the workspace, a remote backend has a fingerprint)
    which are already settled, and re-running them against a per-warehouse copy
    would either repeat work or reject a legal secondary. The paths this copy
    carries are resolved before it is built.
    """
    return config.model_copy(
        update={
            "warehouse": (spec.path if spec.path is not None else config.warehouse),
            "warehouse_backend": spec.backend,
            "warehouse_dsn_env": spec.dsn_env,
            "snowflake_options_env": spec.options_env,
            "warehouses": [],
        }
    )


class ReadOnlyWarehouse:
    """A peer the world declared read-only. The refusal names the warehouse.

    This is the decommissioned writer. It exists so that "the old warehouse is
    no longer written to" is a property of the world the grader can read off
    the action log, rather than a claim the agent makes in prose.
    """

    def __init__(self, name: str, inner):
        self.name = name
        self.inner = inner
        self.dialect = inner.dialect

    def execute(self, sql: str, observation: Observation, *, read_only=False, limit=None):
        if not read_only:
            raise ToolError(
                "warehouse_read_only",
                f"Warehouse {self.name!r} is read-only in this world; it accepts queries only",
            )
        return self.inner.execute(sql, observation, read_only=True, limit=limit)


class AttachedDuckDBWarehouse(DuckDBWarehouse):
    """A DuckDB primary that carries its peers into the same catalog.

    Order is the entire mechanism, and it is measured:

      ATTACH primary; LOAD postgres; ATTACH peers; USE; disable external
      access; lock configuration

    Everything the episode may reach is attached while the connection is still
    open, and then the door is shut. After `enable_external_access = false` and
    `lock_configuration = true`, a further `ATTACH` - of a file or of a Postgres
    database - is refused by DuckDB itself, so `sql_execute` cannot be used to
    reach a warehouse the world did not declare. Already-attached catalogs stay
    queryable, and a READ_ONLY attachment stays read-only.
    """

    def __init__(self, config: GatewayConfig, peers):
        super().__init__(config)
        # peers: list of (alias, kind, target, read_only)
        self.peers = list(peers)

    def connect(self, read_only: bool):
        conn = duckdb.connect(
            ":memory:",
            config={
                "threads": 1,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
            },
        )
        path = str(self.config.warehouse).replace("'", "''")
        mode = " (READ_ONLY)" if read_only else ""
        conn.execute(f"ATTACH '{path}' AS {PRIMARY_ALIAS}{mode}")
        if any(kind == "postgres" for _, kind, _, _ in self.peers):
            # Explicit, because autoload is off. The extension must already be
            # present in the image: INSTALL reaches extensions.duckdb.org and
            # the episode zone has no egress.
            conn.execute("LOAD postgres")
        for alias, kind, target, peer_read_only in self.peers:
            escaped = str(target).replace("'", "''")
            options = ["READ_ONLY"] if peer_read_only else []
            if kind == "postgres":
                options.insert(0, "TYPE POSTGRES")
            suffix = f" ({', '.join(options)})" if options else ""
            conn.execute(f'ATTACH \'{escaped}\' AS "{alias}"{suffix}')
        conn.execute(f"USE {PRIMARY_ALIAS}.warehouse")
        conn.execute("SET enable_external_access = false")
        conn.execute("SET lock_configuration = true")
        return conn


class Registry:
    """The episode's warehouses, addressed by name."""

    def __init__(self, specs, connectors, primary_name, attachments):
        self.specs = specs
        self.connectors = connectors
        self.primary_name = primary_name
        self.attachments = attachments
        self.names = list(specs)
        self.multi = len(self.names) > 1
        # The warehouses dbt can be pointed at: the primary always (the
        # profile's own default target, exactly as today), plus every peer
        # that declares a `dbt_target`. A peer without one is a
        # reconciliation view only, and the dbt tools do not list it.
        self.dbt_names = [
            name for name, spec in specs.items()
            if name == primary_name or spec.dbt_target
        ]

    @property
    def primary(self):
        return self.connectors[self.primary_name]

    def dbt_plan(self, name: str | None) -> dict:
        """The keyword arguments `Commands.dbt` needs to aim dbt at `name`.

        Empty for the primary (and for every call in a one-warehouse world),
        so `Commands.dbt` receives exactly the arguments it always did and the
        invocation is unchanged. For a peer with a `dbt_target` it is
        `{"target": <output>, "spec": <WarehouseSpec>}`: the output name goes
        to `dbt --target`, the spec lets the command check the output is the
        engine the world declared. A declared warehouse without a dbt target
        is refused by name; an undeclared one never reaches here, because the
        served schema is a Literal over `dbt_names`.
        """
        if name is None or name == self.primary_name:
            return {}
        spec = self.specs.get(name)
        if spec is None:
            raise ToolError(
                "unknown_warehouse",
                f"No warehouse named {name!r}; this world declares "
                + ", ".join(repr(item) for item in self.names),
            )
        if not spec.dbt_target:
            raise ToolError(
                "no_dbt_target",
                f"Warehouse {name!r} is not a dbt target in this world; dbt can build "
                + ", ".join(repr(item) for item in self.dbt_names),
            )
        return {"target": spec.dbt_target, "spec": spec}

    def get(self, name: str | None):
        if name is None:
            return self.primary
        connector = self.connectors.get(name)
        if connector is None:
            raise ToolError(
                "unknown_warehouse",
                f"No warehouse named {name!r}; this world declares "
                + ", ".join(repr(item) for item in self.names),
            )
        return connector

    def execute(self, name, sql, observation, *, read_only=False, limit=None):
        """Run a statement against one warehouse.

        In a one-warehouse world this is `create_warehouse(config).execute(...)`
        with the arguments `Operations` has always passed, and it must stay
        that way: the action row it produces is compared byte for byte against
        a frozen literal in `tests/test_warehouse_backends.py`.
        """
        if not self.multi:
            return self.primary.execute(sql, observation, read_only=read_only, limit=limit)
        resolved = name or self.primary_name
        connector = self.get(resolved)
        try:
            return connector.execute(sql, observation, read_only=read_only, limit=limit)
        finally:
            # Qualify only when there is something to disambiguate. An
            # unqualified `warehouse.fct_revenue` in a two-warehouse world is
            # worse than useless to the process signals: `ungated_destructive`
            # asks whether a target was READ before it was written, and
            # crediting a read of the legacy table against a write to the
            # Snowflake table is precisely the "correct change, broken
            # production" mistake this whole build exists to measure.
            observation.targets = [f"{resolved}:{item}" for item in observation.targets]
            observation.read_targets = [f"{resolved}:{item}" for item in observation.read_targets]


def _peer_plan(config: GatewayConfig, specs: dict[str, WarehouseSpec]):
    """Which peers a DuckDB primary can carry, or a refusal that says why.

    A world that declares a cross-engine join the stack cannot perform is a
    world whose task is unsolvable as written, so this raises at construction
    rather than surfacing a confusing binder error thirty tool calls later.

    **Every peer is attached READ_ONLY, whatever its `read_only` setting.** One
    rule instead of two: the attached catalog is the reconciliation view, and
    writes go through `warehouse=<name>`, which reaches that warehouse's own
    connector. The alternative - a peer writable through two different paths,
    one of which silently opens a second handle on the same DuckDB file or a
    second Postgres transaction - buys nothing a cutover task needs and costs a
    class of bug nobody would enjoy debugging inside a rollout.
    """
    primary = specs[next(iter(specs))]
    peers, notes = [], {}
    for name, spec in list(specs.items())[1:]:
        if spec.dbt_target:
            if spec.read_only:
                # `ReadOnlyWarehouse` refuses writes at the connector and the
                # attachment is READ_ONLY at the engine, but dbt opens the
                # file (or the DSN) itself and neither rule is in its way. A
                # world that says "decommissioned" and "dbt builds it" is
                # wrong about one of them; refuse it where the author looks.
                raise ValueError(
                    f"warehouse {name!r} is read_only and declares dbt target "
                    f"{spec.dbt_target!r}; dbt would write straight past the read-only "
                    "rule. Drop one of the two."
                )
            missing = dbt_adapter_missing(spec.backend)
            if missing:
                raise ValueError(
                    f"warehouse {name!r} declares dbt target {spec.dbt_target!r} on a "
                    f"{spec.backend} backend, but the {missing} adapter is not installed "
                    "(it is not in gateway/requirements.lock). dbt cannot be pointed at "
                    "this warehouse in this image; add the adapter to the lock and rebuild, "
                    "or drop `dbt_target` and let the agent build it another way."
                )
        if spec.backend == "snowflake" and spec.attach:
            raise ValueError(
                f"warehouse {name!r} is snowflake and cannot be attached to any catalog: the "
                "connector runs one statement per session and cannot see another database. "
                "Declare `attach: false` and reconcile against it in the agent's own code "
                "(read both sides, diff in Python)."
            )
        if primary.backend != "duckdb":
            notes[name] = f"primary backend is {primary.backend}; only duckdb can attach peers"
            continue
        if not spec.attach:
            notes[name] = "attach disabled by the world"
            continue
        if spec.backend == "duckdb":
            if spec.path is not None and primary.path is not None:
                if spec.path.resolve() == primary.path.resolve():
                    raise ValueError(
                        f"warehouse {name!r} names the same file as the primary; DuckDB refuses "
                        "to attach one file twice ('Unique file handle conflict')"
                    )
            peers.append((name, "duckdb", spec.path, True))
            notes[name] = "attached read-only as a duckdb catalog"
        elif spec.backend == "postgres":
            import os

            dsn = os.environ.get(spec.dsn_env)
            if not dsn:
                raise ValueError(
                    f"warehouse {name!r} is postgres and {spec.dsn_env} is unset; the registry "
                    "needs the DSN at construction because the catalog is attached before the "
                    "DuckDB connection is locked down"
                )
            peers.append((name, "postgres", dsn, True))
            notes[name] = "attached read-only as a postgres catalog"
    return peers, notes


def _probe_attachments(config: GatewayConfig, peers):
    """Open the real connection once, so a broken plan fails at boot.

    The expensive failure this prevents: `LOAD postgres` raising on the first
    agent query, in an image where the extension was never baked in, after the
    task instruction has already promised a cross-engine reconciliation.
    """
    if not peers:
        return
    probe = AttachedDuckDBWarehouse(config, peers)
    try:
        probe.connect(read_only=True).close()
    except duckdb.Error as exc:
        message = str(exc)
        if "postgres" in message.lower() and "not found" in message.lower():
            raise ValueError(
                "this world attaches a postgres warehouse to a duckdb primary, but the duckdb "
                "`postgres` extension is not present in this image. It cannot be fetched at run "
                "time: INSTALL reaches extensions.duckdb.org and the episode zone has no egress. "
                "Bake it in at build time (`INSTALL postgres`) or declare `attach: false` on that "
                f"warehouse and reconcile in the agent's own code. Underlying error: {message}"
            ) from None
        raise ValueError(
            f"the declared warehouses could not be attached together: {message}"
        ) from None


def create_registry(config: GatewayConfig) -> Registry:
    specs = warehouse_specs(config)
    primary_name = next(iter(specs))
    if len(specs) == 1:
        # The single-warehouse path, untouched. `create_warehouse` is the
        # frozen v1 factory and it is what runs.
        return Registry(specs, {primary_name: create_warehouse(config)}, primary_name, {})
    peers, notes = _peer_plan(config, specs)
    primary_config = derive_config(config, specs[primary_name])
    _probe_attachments(primary_config, peers)
    connectors = {
        primary_name: (
            AttachedDuckDBWarehouse(primary_config, peers)
            if peers
            else create_warehouse(primary_config)
        )
    }
    for name, spec in list(specs.items())[1:]:
        connector = create_warehouse(derive_config(config, spec))
        connectors[name] = ReadOnlyWarehouse(name, connector) if spec.read_only else connector
    return Registry(specs, connectors, primary_name, notes)


# ------------------------------------------------------------------ tool surface

SUFFIX = (
    " This world runs more than one warehouse: pass 'warehouse' to choose one "
    "(default {primary}). Declared: {names}."
)
DBT_SUFFIX = (
    " This world has more than one dbt target: pass 'warehouse' to choose which "
    "warehouse dbt builds (default {primary}). Targets: {names}."
)


@cache
def _dual_models(names: tuple[str, ...], primary: str):
    choice = Literal[names]

    class DualSQL(SQL):
        warehouse: choice = primary

    class DualQuery(Query):
        warehouse: choice = primary

    DualSQL.__name__ = "SQLAt"
    DualQuery.__name__ = "QueryAt"
    return DualSQL, DualQuery


@cache
def _dual_dbt_models(names: tuple[str, ...], primary: str):
    choice = Literal[names]

    class DualDbt(Dbt):
        warehouse: choice = primary

    class DualDbtRun(DbtRun):
        warehouse: choice = primary

    DualDbt.__name__ = "DbtAt"
    DualDbtRun.__name__ = "DbtRunAt"
    return DualDbt, DualDbtRun


def dbt_target_names(config: GatewayConfig) -> tuple[str, ...]:
    """The warehouses dbt can build, primary first. Same rule as Registry.dbt_names."""
    specs = warehouse_specs(config)
    primary = next(iter(specs))
    return tuple(name for name, spec in specs.items() if name == primary or spec.dbt_target)


def build_tools(config: GatewayConfig) -> dict:
    """The tool table this episode serves.

    One warehouse returns `TOOLS` itself - the same object, so `list_tools`,
    `/health`'s tool count, `test_image_parity`'s set comparison and
    `containment.py`'s field walk all see exactly what they see today, and
    `container_shim.register_bash()`'s runtime mutation of `TOOLS` still reaches
    the served table.

    With two or more warehouses the sql tools gain `warehouse`. The three dbt
    tools gain it too, but on a narrower condition: only when more than one
    warehouse is a dbt target. A world with a primary and a reconciliation-only
    peer (a Snowflake target the agent diffs in Python, a read-only legacy
    source with no `dbt_target`) keeps today's dbt schema, because there is
    nothing for the argument to choose between.
    """
    names = tuple(warehouse_specs(config))
    if len(names) == 1:
        return TOOLS
    dual_sql, dual_query = _dual_models(names, names[0])
    suffix = SUFFIX.format(primary=names[0], names=", ".join(names))
    tools = {
        **TOOLS,
        "sql_query": (dual_query, TOOLS["sql_query"][1] + suffix),
        "sql_execute": (dual_sql, TOOLS["sql_execute"][1] + suffix),
    }
    dbt_names = dbt_target_names(config)
    if len(dbt_names) > 1:
        dual_dbt, dual_dbt_run = _dual_dbt_models(dbt_names, dbt_names[0])
        dbt_suffix = DBT_SUFFIX.format(primary=dbt_names[0], names=", ".join(dbt_names))
        tools["dbt_run"] = (dual_dbt_run, TOOLS["dbt_run"][1] + dbt_suffix)
        tools["dbt_test"] = (dual_dbt, TOOLS["dbt_test"][1] + dbt_suffix)
        tools["dbt_compile"] = (dual_dbt, TOOLS["dbt_compile"][1] + dbt_suffix)
    return tools
