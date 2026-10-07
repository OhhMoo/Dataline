"""The discovery layer: what a data engineer finds on the box.

`gateway/NATIVE.md`: in native mode the subject gets bash and no tool
list, so everything the 63-tool schema used to announce has to be discoverable
the way it is on a real machine - from `.envrc`, `~/.dbt/profiles.yml`, a
config file under `~/.config`, and the onboarding page the team wrote. This
module writes those four, and nothing else.

    <root>/workspace/.envrc                    endpoints, as exports
    <root>/workspace/docs/onboarding.md        your first week
    <root>/home/.dbt/profiles.yml              the warehouse a bare `dbt` finds
    <root>/home/.config/alkera/services.toml   the same table, machine-readable

`<root>/home` is the episode container's `HOME`; `gateway/docker/
compose.native.yml` binds it at `/root`, which is what makes the two `~`-
relative files be at `~`. It is deliberately NOT inside `workspace/`: a home
directory committed into the project's own git repository is a tell, and the
workspace is a git repository.

OFF by default. `build.build` calls this only when the seed says `native:
true`, exactly as it calls `_seed_warehouses` only when the seed names a
warehouse, so every recorded fingerprint of every existing world is unchanged.

Under a seed `profile:` (build.resolve_profile) every function here takes the
plugged `slots` and names nothing from a slot the profile left out: an
onboarding page that lists Jira on a box with no Jira is a fact the world does
not hold (rule 3 below). `slots=None` - no profile - is the full table, byte
for byte what this module wrote before profiles existed.

The profile's optional `plugs` ({slot: [plug, ...]}) narrows one step further:
which plug fills a slot. A slot it names lists only the services of the plugs
it names (`bi: [powerbi]` is a Power BI shop, so no Metabase); a slot it does
not name lists what that slot always listed. The BI vendors (PLUG_SERVICES)
are EXPLICIT-ONLY on the same terms as the notebook (EXPLICIT_SLOTS): named
only when `plugs` names them, so neither an unprofiled box nor a profile that
plugs `bi` without saying which vendor ever grows a Power BI endpoint.

The warehouse slot follows the same rule through its backend groups
(BACKEND_PLUGS): a box whose warehouse plugs are `[duckdb]` names no Postgres
and no Snowflake, `[postgres]` names Postgres, `[snowflake]` names Snowflake
(as "the warehouse"). A record seldom names its warehouse plug, because the
profile's stack already says it (`da/laptop` is DuckDB); `profile_plugs` fills
it in from STACK_WAREHOUSE, and `write` applies it. A stack the table does not
know leaves the slot unnamed, which lists what it always listed.

A backend a profile plugs into the container topology (`alkera_gateway.
backends.BACKENDS`: Snowflake today) has one DISCOVERY row, and a box that
plugs it gets all of it: its variables (`.envrc`, `environment`), its row on the
onboarding page, its `[<backend>]` section in `services.toml`, and - when it has
a dbt adapter - a target in the box's dbt profile that becomes the default
(`box_profiles_yaml`, which the harness stages at `~/.dbt` in the container). A
box that does not plug it names it nowhere. Snowflake was listed before the
registry, so wherever no profile names its slot (no profile at all, an unknown
stack) it is still listed exactly as it was (UNNAMED_LISTING).

On a profiled box the wiki section lists each page by its Confluence content
id (`content_id`, the number the API routes), with its path beside it: the
path is what tickets, chat and the pages themselves link by, and
`/wiki/rest/api/content/<path>` is not a route. The unprofiled page keeps
listing paths, byte for byte.

Determinism, on the same three rules as the rest of the builder:

1. nothing reads a wall clock, a hostname, an environment variable or a random
   number - every byte written here is a function of the endpoint table below
   and of files the build has already produced;
2. everything is written LF-terminated and ASCII, sorted where a collection is
   involved, so two builds of one seed are byte-identical;
3. `docs/onboarding.md` **states no fact that is not already in the world.**
   Every system it names comes from the table, every runbook it points at is a
   page id out of `var/workflow/kb.json`, every job is a key of the theme's own
   `jobs:` block. An onboarding page that knew something the world did not
   would be a hint channel that no grader could see.

The endpoint table is not this module's to choose. `tasks/harness/
agent_under_test.NATIVE_SERVICES` is what `--driver native` puts in the
subject's environment, and `docs/implementation_plan/harness/NATIVE_DRIVER_REQUESTS.md` fixes the
names, the ports and the credentials; `.envrc` has to export the same names and
the same values or a human shell and the driver disagree about one endpoint.
The table is duplicated rather than imported because `world/` imports neither
`alkera_gateway` nor `tasks/`; `world/tests/test_native_workspace.py` reads
`gateway/docker/compose.native.yml` and `alkera_gateway.native.serve` and fails
when any of the three drift, which is what makes the duplication safe. DISCOVERY
is the same kind of copy of `alkera_gateway.backends.BACKENDS`
(world/tests/test_discovery_registry.py). The one exception is
`box_profiles_yaml`, which renders a plugged backend's dbt target with the
gateway's own `warehouses.dbt_output` and so imports it, lazily: it is called by
the harness when it stages the box, never by `world.build`.

Credentials are obviously fake and every one of them is the same string,
`alkera-local-dev`: the repository rule is that nothing in a world may look
like a real key, and a value that appears in four files under four different
names is a value nobody mistakes for a secret.
"""
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: The workspace's home directory inside the world root. compose.native.yml
#: binds it at /root.
HOME = "home"
#: The one obviously-fake identity every native surface shares - the Postgres
#: password, the MinIO access key and its secret, and every service token
#: (`agent_under_test.NATIVE_FAKE_CREDENTIAL`).
FAKE_USER = "alkera-local-dev"
FAKE_TOKEN = "alkera-local-dev"
#: The block marker in `.envrc`. The theme ships its own `.envrc` (a real one,
#: with the repo's dbt target and landing root in it), so this appends a
#: delimited block instead of overwriting the file, and rewrites that block in
#: place when it is already there. Worded as provisioning rather than as a
#: generator, because the file is something the agent reads.
ENVRC_BEGIN = "# >>> platform-managed: service endpoints. Regenerated on provision. >>>"
ENVRC_END = "# <<< platform-managed: service endpoints <<<"


@dataclass(frozen=True)
class Endpoint:
    """One HTTP service: its DNS alias, its port, and how the box names it."""

    name: str
    alias: str
    port: int
    url_var: str
    path: str = ""           # appended to the base URL, when the vendor has one
    auth_var: str = ""       # the variable carrying the token, if it takes one
    user_var: str = ""
    alias_vars: tuple = ()   # other names the same URL is also exported under
    note: str = ""
    #: (variable, value) pairs a client needs beside the URL and credentials
    #: (`TABLEAU_SITE`, `AZURE_TENANT_ID`). Empty for the nine.
    extra_vars: tuple = ()
    #: The onboarding page's "reached with" cell, when it is not `note`.
    clients: str = ""

    @property
    def url(self) -> str:
        return f"http://{self.alias}:{self.port}{self.path}"


#: The nine fake vendor services. Ports are `alkera_gateway.native.serve
#: .STUB_PORTS`, which is what serve.py listens on and what compose.native.yml
#: declares aliases for.
SERVICES = (
    Endpoint("metabase", "metabase.internal", 3000, "METABASE_URL",
             auth_var="METABASE_API_KEY", note="dashboards and saved questions"),
    Endpoint("loki", "loki.internal", 3100, "LOKI_URL",
             alias_vars=("LOKI_ADDR",), note="job logs; logcli reads LOKI_ADDR"),
    Endpoint("jira", "jira.internal", 8080, "JIRA_URL",
             auth_var="JIRA_API_TOKEN", user_var="JIRA_USER",
             note="the ticket queue"),
    Endpoint("slack", "slack.internal", 8081, "SLACK_BASE_URL",
             auth_var="SLACK_BOT_TOKEN", note="the team channels"),
    Endpoint("airbyte", "airbyte.internal", 8001, "AIRBYTE_URL",
             auth_var="AIRBYTE_API_KEY", note="connector control plane"),
    Endpoint("deploy", "deploy.internal", 8085, "DEPLOY_URL",
             auth_var="DEPLOY_API_TOKEN", note="releases; ops/deploy wraps it"),
    Endpoint("confluence", "confluence.internal", 8090, "CONFLUENCE_URL",
             auth_var="CONFLUENCE_API_TOKEN", user_var="CONFLUENCE_USER",
             note="the wiki: runbooks, postmortems, policies"),
    Endpoint("prometheus", "prometheus.internal", 9090, "PROMETHEUS_URL",
             note="metrics"),
    Endpoint("alertmanager", "alertmanager.internal", 9093, "ALERTMANAGER_URL",
             note="alarms"),
)

#: The explicit-only BI vendors (`alkera_gateway.native.services.PLUG_NAMES`),
#: served by `native/serve.py` only under a profile that plugs them, on the
#: ports in `serve.PLUG_PORTS`. Every variable is the one the service's own
#: author documents for its clients; the URL is the same for all three of
#: Power BI's hosts because one service answers the paths of all three
#: (api.powerbi.com, api.fabric.microsoft.com, login.microsoftonline.com).
#: `TABLEAU_SITE` is empty because the one site is Default, whose contentUrl
#: is "" - which is what tabcmd and the Tableau Server Client send for it.
PLUG_SERVICES = (
    Endpoint("powerbi", "powerbi.internal", 8095, "POWERBI_API_URL",
             alias_vars=("FABRIC_API_URL", "AZURE_AUTHORITY_HOST"),
             user_var="AZURE_CLIENT_ID", auth_var="AZURE_CLIENT_SECRET",
             extra_vars=(("AZURE_TENANT_ID", FAKE_USER),),
             note="Power BI: published semantic models, refreshes, DAX queries",
             clients="Power BI REST / Fabric REST, Entra client-credentials token"),
    Endpoint("tableau", "tableau.internal", 8096, "TABLEAU_SERVER_URL",
             user_var="TABLEAU_PAT_NAME", auth_var="TABLEAU_PAT_SECRET",
             extra_vars=(("TABLEAU_SITE", ""),),
             note="Tableau Server: published data sources, workbooks, extracts",
             clients="tabcmd, Tableau Server Client (tableauserverclient), REST"),
    Endpoint("sigma", "sigma.internal", 8097, "SIGMA_BASE_URL",
             user_var="SIGMA_CLIENT_ID", auth_var="SIGMA_CLIENT_SECRET",
             note="Sigma: published workbooks, data models, connections",
             clients="Sigma REST API v2, client-credentials token"),
)

#: Which environment slot (`alkera_gateway.plugs.SLOTS`) mounts each service:
#: the inverse of `Plug.services` in gateway/src/alkera_gateway/plugs.py.
#: Duplicated rather than imported, like the endpoint table itself;
#: world/tests/test_profile_world.py compares the two.
SERVICE_SLOTS = {
    "metabase": "bi",
    "loki": "observability", "prometheus": "observability",
    "alertmanager": "observability",
    "jira": "human", "slack": "human", "confluence": "human",
    "airbyte": "ingest",
    "deploy": "release",
    "powerbi": "bi", "tableau": "bi", "sigma": "bi",
}
#: Which PLUG mounts each service (`Plug.name` in the same catalog), which is
#: what a profile record's `plugs` names. Same duplication, same drift test.
SERVICE_PLUGS = {
    "metabase": "metabase_fake",
    "loki": "prom_stack", "prometheus": "prom_stack", "alertmanager": "prom_stack",
    "jira": "workflow", "slack": "workflow", "confluence": "workflow",
    "airbyte": "airbyte_fake",
    "deploy": "deploy_fake",
    "powerbi": "powerbi", "tableau": "tableau", "sigma": "sigma",
}

#: The planes the workspace reaches through a CLI OF ITS OWN, not a vendor
#: client. One entry: `./ops/sched`. Mirrors
#: `tasks.harness.agent_under_test.NATIVE_WORKSPACE_SERVICES`, and is kept out
#: of SERVICES on purpose - that table is the nine vendor-shaped services the
#: aggregate health reports and containment probes by registry.
#:
#: DELIBERATELY NOT IN `environment()` AND SO NOT IN `.envrc`. `SCHED_URL`
#: unset is how `./ops/sched` selects its local mode, which is what the HOST
#: topology runs (the shim does the three operations in-process against the
#: world's own files). Under the CONTAINER topology those files are not
#: mounted, so compose.native.yml sets `SCHED_URL` in the episode's process
#: environment and the shim dials the served plane at `sched.internal:8120`.
#: The `.envrc` is one file written at build time for both topologies; an
#: `export SCHED_URL` in it would flip a host-topology shim into dialling a
#: host that nothing resolves. The drift test reads this table so compose and
#: the shim cannot disagree about the alias, the variable or the port.
WORKSPACE_PLANES = (
    Endpoint("sched", "sched.internal", 8120, "SCHED_URL",
             note="the team's own scheduler CLI, ./ops/sched; served, not a vendor"),
)

#: The real backends. Their variables are the vendor's own, because the whole
#: point is that `psql`, `aws` and `dbt` work with no arguments.
POSTGRES = {"PGHOST": "postgres.internal", "PGPORT": "5432",
            "PGDATABASE": "alkera", "PGUSER": "alkera",
            "PGPASSWORD": FAKE_TOKEN}
STORAGE = {"AWS_ENDPOINT_URL": "http://minio.internal:9000",
           "AWS_ACCESS_KEY_ID": FAKE_USER,
           "AWS_SECRET_ACCESS_KEY": FAKE_TOKEN,
           "AWS_DEFAULT_REGION": "us-east-1"}
CONTROL = {"AIRFLOW_API_URL": "http://airflow.internal:8080/api/v2",
           "AIRFLOW_USER": FAKE_USER, "AIRFLOW_PASSWORD": FAKE_TOKEN}
#: Where a bare `dbt` finds its profile. The driver exports the same variable
#: (NATIVE_DRIVER.md, "the world's profiles_dir"); this is that directory as
#: the episode container sees it, which is `$HOME/.dbt` with `home/` bound at
#: /root by compose.native.yml.
DBT = {"DBT_PROFILES_DIR": "/root/.dbt"}
#: `CONTAINER_HOME` is that default HOME. A box whose engineer has a different
#: HOME (the Harbor export runs the agent as `engineer`, HOME /home/engineer)
#: passes `home=` to `environment`, `envrc_block` and `write_for_built_world`,
#: and `DBT_PROFILES_DIR` follows it; the default writes the same bytes as before.
CONTAINER_HOME = "/root"


def dbt_env(home: str = CONTAINER_HOME) -> dict[str, str]:
    """`DBT` for a box whose HOME is `home`: `$HOME/.dbt` (`DBT` at the default)."""
    return {"DBT_PROFILES_DIR": f"{str(home).rstrip('/')}/.dbt"}
SNOWFLAKE = {"SNOWFLAKE_HOST": "snowflake.internal",
             "SNOWFLAKE_ACCOUNT": "acme", "SNOWFLAKE_USER": FAKE_USER,
             "SNOWFLAKE_PASSWORD": FAKE_TOKEN}
#: The Jupyter plug (gateway/docker/jupyter/README.md): jupyter-server with the
#: audited kernel, served only when a profile plugs the notebook slot.
NOTEBOOK = {"JUPYTER_URL": "http://jupyter.internal:8888"}
#: What a `.internal` alias means, spelled once for the humans reading .envrc.
#: The registry's backends (DISCOVERY) are listed between the scheduler and the
#: notebook server, where the second warehouse always was.
BACKEND_NOTES = (
    ("the warehouse", "postgres.internal:5432", "psql, dbt, psycopg"),
    ("object storage", "minio.internal:9000", "aws s3, boto3"),
    ("the scheduler", "airflow.internal:8080", "airflow CLI, REST"),
    ("the notebook server", "jupyter.internal:8888", "JupyterLab REST, nbclient"),
)
#: The slot behind each backend group, keyed by BACKEND_NOTES label. The
#: warehouse and dbt are always built (a profile selects planes, never the
#: data), but they are still keyed so that one rule decides every line.
BACKEND_SLOTS = {
    "the warehouse": "warehouse",
    "object storage": "object_store",
    "the scheduler": "orchestrator",
    "the notebook server": "notebook",
}

#: The warehouse PLUG behind each remote-warehouse backend group (a
#: BACKEND_NOTES label -> `Plug.name` in alkera_gateway.plugs). Under a profile
#: whose plugs name the warehouse slot, a group is listed only when its plug is
#: named; DuckDB, the world's own file, has no group (it is not on the network).
BACKEND_PLUGS = {
    "the warehouse": "postgres",
}


@dataclass(frozen=True)
class Discovery:
    """What a box that plugs one registry backend is told about it.

    `backend` is the key into `alkera_gateway.backends.BACKENDS` (the same
    plug name). `label` and `clients` are its onboarding row's "system" and
    "reached with" cells; "where" is `services_toml`'s host:port. A label of
    "the warehouse" reads "the second warehouse" on a box that also lists
    Postgres. `services_toml` is the `[<backend>]` section, a digit-only value
    written as a number. `dbt_type` is the adapter of the box profile's target
    for it (`box_profiles_yaml`), None for no dbt.

    Two more fields than the onboarding row needs, because the world cannot
    read the gateway's row: `env`, the variables a box that plugs it exports
    (`.envrc`, `environment`; equal to `BACKENDS[backend].env`, which the
    container's process environment gets), and `dbt_env`, the box target's
    fields and the variable each one reads (`warehouses.dbt_output(env=...)`):
    the dbt profile names variables, never values.

    `replaces` names the static groups (BACKEND_NOTES labels, e.g. "the
    scheduler") a box that plugs this backend no longer lists: their
    variables, their services.toml section and their onboarding row give way
    to this row's, so the box names one address for the thing and not two.
    """

    backend: str
    label: str
    clients: str
    services_toml: Mapping[str, str]
    dbt_type: str | None
    env: Mapping[str, str] = field(default_factory=dict)
    dbt_env: Mapping[str, str] = field(default_factory=dict)
    replaces: tuple[str, ...] = ()


#: One row per backend `alkera_gateway.backends.BACKENDS` registers (copied,
#: not imported; world/tests/test_discovery_registry.py compares the two).
DISCOVERY: dict[str, Discovery] = {
    # fakesnow behind the TLS proxy (gateway/docker/snowflake_start.py), seeded
    # from the world's warehouse: database WAREHOUSE, the world's schemas, and
    # PUBLIC, which every Snowflake database has and a session with no schema
    # of its own lands in.
    "snowflake": Discovery(
        backend="snowflake",
        label="the warehouse",
        clients="snowflake-connector-python, dbt-snowflake",
        services_toml={"host": "snowflake.internal", "port": "8443",
                       "account": "acme", "user": FAKE_USER, "password": FAKE_TOKEN,
                       "database": "WAREHOUSE", "schema": "PUBLIC"},
        dbt_type="snowflake",
        env={**SNOWFLAKE, "SNOWFLAKE_DATABASE": "WAREHOUSE"},
        dbt_env={"account": "SNOWFLAKE_ACCOUNT", "host": "SNOWFLAKE_HOST",
                 "user": "SNOWFLAKE_USER", "password": "SNOWFLAKE_PASSWORD",
                 "database": "SNOWFLAKE_DATABASE"},
    ),
    # Airflow 3's REST API v2 over this world's scheduler ledger
    # (gateway native/services/airflow.py, world/airflow_view.py). It IS the
    # scheduler on a box that plugs it, so it replaces the static group: one
    # address (8100), the same three variables. The `airflow` CLI is not on the
    # box - it needs a metadata database, and this box has none - which the
    # onboarding row says, so nobody goes looking for it.
    "airflow": Discovery(
        backend="airflow",
        label="the scheduler",
        clients=("Airflow REST API v2 (token from POST /auth/token), airflowctl; "
                 "no `airflow` CLI: this box has no Airflow metadata database"),
        services_toml={"host": "airflow.internal", "port": "8100",
                       "api": "http://airflow.internal:8100/api/v2",
                       "user": FAKE_USER, "password": FAKE_TOKEN},
        dbt_type=None,
        env={"AIRFLOW_API_URL": "http://airflow.internal:8100/api/v2",
             "AIRFLOW_USER": FAKE_USER, "AIRFLOW_PASSWORD": FAKE_TOKEN},
        replaces=("the scheduler",),
    ),
    # The BigQuery REST API at bigquery.internal:9050 (an audited proxy in
    # front of goccy's emulator), seeded from the world's warehouse: project
    # alkera-local, one dataset per warehouse schema (`warehouse.fct_orders`
    # is `alkera-local.warehouse.fct_orders`). The client needs no Google
    # credential: AnonymousCredentials, and the endpoint as api_endpoint. No
    # dbt target (`dbt_type` None): dbt-bigquery is not in the image
    # (gateway/docker/bigquery-clients.in), so the box's dbt profile stays the
    # world's own.
    "bigquery": Discovery(
        backend="bigquery",
        label="the warehouse",
        clients=("google-cloud-bigquery: `bigquery.Client(project=BIGQUERY_PROJECT, "
                 "credentials=AnonymousCredentials(), client_options={\"api_endpoint\": "
                 "BIGQUERY_API_URL})`, or the REST API v2 at that URL"),
        services_toml={"host": "bigquery.internal", "port": "9050",
                       "endpoint": "http://bigquery.internal:9050",
                       "project": "alkera-local", "dataset": "warehouse"},
        dbt_type=None,
        env={"BIGQUERY_API_URL": "http://bigquery.internal:9050",
             "BIGQUERY_PROJECT": "alkera-local"},
    ),
}

#: Registry backends the box listed BEFORE the registry, and so still lists the
#: way it always did wherever no profile names their slot - no profile at all,
#: or a stack whose warehouse the record leaves unnamed: (slot, variables,
#: onboarding "reached with"). Byte for byte the pre-registry box. A backend
#: added to DISCOVERY later has no row here: it is explicit-only.
UNNAMED_LISTING = {
    "snowflake": ("warehouse", SNOWFLAKE, "snowsql, the connector"),
}
#: Each stack's default warehouse plug(s), `alkera_gateway.plugs.STACKS[stack]
#: ["warehouse"]`: what a profile named "<role>/<stack>" plugs into the
#: warehouse slot when its record's `plugs` does not say. Duplicated, not
#: imported (world/ never imports the gateway); world/tests/
#: test_profile_world.py compares the two.
STACK_WAREHOUSE = {
    "laptop": ("duckdb",),
    "postgres": ("postgres",),
    "snowflake": ("snowflake",),
    "databricks": ("databricks",),
    "aws": ("redshift",),
    "gcp": ("bigquery",),
}

#: Slots whose plug is newer than the unprofiled box. "No profile" means the
#: full table as it stood before profiles, and that box is fingerprinted byte
#: for byte, so these appear only when a profile names them.
EXPLICIT_SLOTS = frozenset({"notebook"})


def profile_plugs(name, plugs=None):
    """A profile record's `plugs`, with the warehouse slot's plug filled in
    from the profile's stack (STACK_WAREHOUSE) when the record names none.

    `name` is the record's "<role>/<stack>"; None (no profile), or a stack the
    table does not know, adds nothing and returns the plugs as given (None for
    none), so the slot lists what it always listed.
    """
    out = dict(plugs or {})
    stack = str(name or "").partition("/")[2]
    if "warehouse" not in out and stack in STACK_WAREHOUSE:
        out["warehouse"] = list(STACK_WAREHOUSE[stack])
    return out or None


def content_id(page_id: str) -> str:
    """A wiki page path -> the numeric content id the Confluence API serves
    it under (`alkera_gateway.native.services.confluence.content_id`,
    duplicated; test_profile_world.py compares the two)."""
    return str(int(hashlib.sha256(page_id.encode("utf-8")).hexdigest()[:9], 16))


def _plugged(slot: str, slots) -> bool:
    if slots is None:
        return slot not in EXPLICIT_SLOTS
    return slot in slots


def _plug_named(label: str, slots, plugs) -> bool:
    """False only on a profiled box whose plugs name this backend group's
    slot and not its plug: the box has that slot, filled by something else."""
    plug = BACKEND_PLUGS.get(label)
    if slots is None or plug is None or not plugs:
        return True
    slot = BACKEND_SLOTS[label]
    return slot not in plugs or plug in plugs[slot]


def _backend_listed(label: str, slots, plugs) -> bool:
    return _plugged(BACKEND_SLOTS[label], slots) and _plug_named(label, slots, plugs)


@dataclass(frozen=True)
class _Listed:
    """One registry backend as this box lists it."""

    discovery: Discovery
    label: str                    # as shown: "the warehouse" / "the second warehouse"
    variables: Mapping[str, str]
    clients: str
    plugged: bool                 # named by the profile's plugs; False: UNNAMED_LISTING

    @property
    def where(self) -> str:
        return f"{self.discovery.services_toml['host']}:{self.discovery.services_toml['port']}"


def _backend_plugged(backend: str, slots, plugs) -> bool:
    """A profiled box whose plugs name `backend` in a slot the box has."""
    return slots is not None and bool(plugs) and any(
        backend in names and _plugged(slot, slots) for slot, names in plugs.items())


def _replaced(slots, plugs) -> set[str]:
    """The static groups (BACKEND_NOTES labels) a plugged backend supersedes
    (`Discovery.replaces`)."""
    return {label for name, d in DISCOVERY.items()
            if _backend_plugged(name, slots, plugs) for label in d.replaces}


def _static_listed(label: str, slots, plugs) -> bool:
    """A static group (BACKEND_NOTES) this box lists: plugged, its plug named,
    and not superseded by a plugged registry backend."""
    return _backend_listed(label, slots, plugs) and label not in _replaced(slots, plugs)


def _discovered(slots, plugs=None) -> list[_Listed]:
    """The registry backends this box lists, in DISCOVERY order: the ones its
    profile plugs, with their DISCOVERY row, and - where no profile names their
    slot - the ones the box always listed, as it always did (UNNAMED_LISTING)."""
    second = _static_listed("the warehouse", slots, plugs)
    out = []
    for name, d in DISCOVERY.items():
        label = "the second warehouse" if d.label == "the warehouse" and second else d.label
        if _backend_plugged(name, slots, plugs):
            out.append(_Listed(d, label, d.env, d.clients, True))
        elif name in UNNAMED_LISTING:
            slot, variables, clients = UNNAMED_LISTING[name]
            if _plugged(slot, slots) and (slots is None or not plugs or slot not in plugs):
                out.append(_Listed(d, label, variables, clients, False))
    return out


def _backends(slots, plugs=None):
    """The backend variable groups this profile plugs, as (comment, mapping)."""
    groups = (
        ("the warehouse", "# The warehouse. psql and psycopg need no arguments with these set.",
         POSTGRES),
        ("object storage",
         "# Object storage. `aws s3 ls` and boto3 resolve through this endpoint.", STORAGE),
        ("the scheduler", "# The scheduler.", CONTROL),
    )
    out = [(comment, mapping) for label, comment, mapping in groups
           if _static_listed(label, slots, plugs)]
    out += [(f"# {listed.label.capitalize()}.", listed.variables)
            for listed in _discovered(slots, plugs)]
    if _static_listed("the notebook server", slots, plugs):
        out.append(("# The notebook server.", NOTEBOOK))
    return out


def _chosen(service: str, slots, plugs) -> bool:
    """Does this box serve `service`? Its slot must be plugged; when the
    profile's `plugs` names that slot, the service's own plug must be named."""
    slot = SERVICE_SLOTS[service]
    if not _plugged(slot, slots):
        return False
    if not plugs or slot not in plugs:
        return True
    return SERVICE_PLUGS[service] in plugs[slot]


def _services(slots, plugs=None):
    """The nine, as plugged, then any BI vendor the profile names by plug.
    A vendor is never listed on the strength of its slot alone: explicit-only,
    like the notebook, because "no plugs" is the box as it stood before them."""
    vendors = [s for s in PLUG_SERVICES
               if plugs and SERVICE_PLUGS[s.name] in plugs.get(SERVICE_SLOTS[s.name], ())
               and _chosen(s.name, slots, plugs)]
    return [s for s in SERVICES if _chosen(s.name, slots, plugs)] + vendors


def environment(slots=None, plugs=None, home: str = CONTAINER_HOME) -> dict[str, str]:
    """Every variable the native box exports, as one ordered mapping.

    The single definition. `.envrc`, `services.toml` and the test that compares
    this against `compose.native.yml`'s `episode.environment` all read it, so
    an endpoint cannot be added to one and forgotten in the others.
    """
    env: dict[str, str] = {}
    for _, mapping in _backends(slots, plugs):
        env.update(mapping)
    env.update(dbt_env(home))
    for service in _services(slots, plugs):
        env[service.url_var] = service.url
        for name in service.alias_vars:
            env[name] = service.url
        if service.user_var:
            env[service.user_var] = FAKE_USER
        if service.auth_var:
            env[service.auth_var] = FAKE_TOKEN
        for name, value in service.extra_vars:
            env[name] = value
    return env


def envrc_block(slots=None, plugs=None, home: str = CONTAINER_HOME) -> str:
    """The delimited block appended to (or replaced inside) `workspace/.envrc`."""
    lines = [
        ENVRC_BEGIN,
        "# Everything below is written by the platform when the box is",
        "# provisioned. The tokens are local development tokens; the real ones",
        "# come from the vault and are injected by the deployment.",
    ]

    def emit(mapping):
        for key in sorted(mapping):
            lines.append(f'export {key}="{mapping[key]}"')

    for comment, mapping in _backends(slots, plugs):
        lines += ["", comment]
        emit(mapping)
    lines += ["", "# Where a bare `dbt` finds its profile."]
    emit(dbt_env(home))
    for service in _services(slots, plugs):
        lines += ["", f"# {service.name} - {service.note}"]
        lines.append(f'export {service.url_var}="{service.url}"')
        for name in service.alias_vars:
            lines.append(f'export {name}="{service.url}"')
        if service.user_var:
            lines.append(f'export {service.user_var}="{FAKE_USER}"')
        if service.auth_var:
            lines.append(f'export {service.auth_var}="{FAKE_TOKEN}"')
        for name, value in service.extra_vars:
            lines.append(f'export {name}="{value}"')
    lines += ["", ENVRC_END]
    return "\n".join(lines) + "\n"


def merge_envrc(existing: str, block: str) -> str:
    """Append the block, or replace it in place when it is already there.

    Idempotent on purpose: the same seed built twice has to produce the same
    bytes, and a build that ran over a tree somebody had already provisioned
    must not end up with the block twice.
    """
    if ENVRC_BEGIN in existing and ENVRC_END in existing:
        head, _, rest = existing.partition(ENVRC_BEGIN)
        _, _, tail = rest.partition(ENVRC_END)
        return head + block.rstrip("\n") + tail
    separator = "" if existing.endswith("\n\n") or not existing else "\n"
    return existing + separator + block


def profiles_yaml(profile: str) -> str:
    """`~/.dbt/profiles.yml`: the warehouse a bare `dbt` finds.

    Postgres, not the DuckDB file in the tree. NATIVE.md's standing
    decision is that the native-mode warehouse is served and never a file -
    a served warehouse is what turns `log_statement=all` into action-log rows,
    and a file the agent opens directly leaves none. The repository's own
    `config/profiles.yml` still points at the developer DuckDB file, which is
    the difference the fixture's own comment already warns about.
    """
    document = {
        profile: {
            "target": "dev",
            "outputs": {
                "dev": {
                    "type": "postgres",
                    "host": POSTGRES["PGHOST"],
                    "port": int(POSTGRES["PGPORT"]),
                    "user": POSTGRES["PGUSER"],
                    "password": POSTGRES["PGPASSWORD"],
                    "dbname": POSTGRES["PGDATABASE"],
                    "schema": "warehouse",
                    "threads": 1,
                }
            },
        }
    }
    header = (
        "# The warehouse a bare `dbt` uses. The project's own config/profiles.yml\n"
        "# targets the developer DuckDB file instead; that is the usual reason two\n"
        "# people get two different answers out of the same model.\n"
    )
    return header + yaml.safe_dump(document, sort_keys=True, default_flow_style=False,
                                   allow_unicode=False)


def box_profiles_yaml(world_profile: str, plugged: Sequence[str]) -> str:
    """The dbt profile a bare `dbt` finds on a box that plugs `plugged`.

    `world_profile` is the world's own profiles.yml as the box sees it (the
    harness re-roots its DuckDB paths onto the container: container_episode
    .reroot_profiles); `plugged` names the profile's plugged backends. Each one
    with a dbt adapter (DISCOVERY `dbt_type`) gets a target of its own, named
    after it, and the first becomes every profile's default `target`, so a bare
    `dbt debug` on a Snowflake box talks to Snowflake. The re-rooted DuckDB
    targets stay, unchanged. Every connection field of a new target reads one of
    the box's variables (`{{ env_var(...) }}`, the gateway's own
    `warehouses.dbt_output` with `env=`): the file names variables, never values.

    With nothing to add the world's profile comes back as it was given - the
    same bytes the box has always had.
    """
    targets = [d for name, d in DISCOVERY.items() if name in plugged and d.dbt_type]
    if not targets:
        return world_profile
    # The one gateway import in world/, and the reason it is here rather than
    # at the top: the harness calls this when it stages the box; world.build
    # never does.
    from alkera_gateway.warehouses import dbt_output

    document = yaml.safe_load(world_profile) or {}
    for profile in document.values():
        if not isinstance(profile, dict) or not isinstance(profile.get("outputs"), dict):
            continue
        for d in targets:
            if d.backend in profile["outputs"]:
                # The world's own target of that name is not this module's to
                # replace: refused, loudly, rather than silently overwritten.
                raise ValueError(
                    f"the world's dbt profile already has an output named "
                    f"{d.backend!r}; the box target for the plugged {d.backend} "
                    f"backend would replace it")
            profile["outputs"][d.backend] = dbt_output(
                d.dbt_type, schema=d.services_toml["schema"], env=d.dbt_env,
                port=int(d.services_toml["port"]))
        profile["target"] = targets[0].backend
    return yaml.safe_dump(document, sort_keys=False)


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def services_toml(root_in_container: str, slots=None, plugs=None) -> str:
    """`~/.config/alkera/services.toml`: the endpoint table, machine-readable.

    Hand-rolled rather than through a writer, because `tomllib` is read-only in
    3.11 and the only alternative is a dependency the world factory does not
    have. The shape is small and fixed, and the test round-trips it through
    `tomllib` so "hand-rolled" cannot quietly mean "invalid".
    """
    lines = [
        "# The services this box can reach. Written when the box is provisioned.",
        "# Names resolve on the internal network only; nothing here is routable.",
        "",
        "[world]",
        f"root = {_toml_string(root_in_container)}",
        f'workspace = {_toml_string(root_in_container.rstrip("/") + "/workspace")}',
    ]
    # Postgres, unless the profile's warehouse plugs leave it out (a DuckDB or
    # a Snowflake box). Not keyed on the slot being plugged: the table has
    # always carried it, and only a named plug may take it away.
    replaced = _replaced(slots, plugs)
    if _plug_named("the warehouse", slots, plugs) and "the warehouse" not in replaced:
        lines += [
            "",
            "[warehouse]",
            f'host = {_toml_string(POSTGRES["PGHOST"])}',
            f'port = {POSTGRES["PGPORT"]}',
            f'database = {_toml_string(POSTGRES["PGDATABASE"])}',
            f'user = {_toml_string(POSTGRES["PGUSER"])}',
            f'password = {_toml_string(POSTGRES["PGPASSWORD"])}',
        ]
    if _plugged(BACKEND_SLOTS["object storage"], slots) and "object storage" not in replaced:
        lines += [
            "",
            "[storage]",
            f'endpoint = {_toml_string(STORAGE["AWS_ENDPOINT_URL"])}',
            f'access_key_id = {_toml_string(STORAGE["AWS_ACCESS_KEY_ID"])}',
            f'secret_access_key = {_toml_string(STORAGE["AWS_SECRET_ACCESS_KEY"])}',
        ]
    if _plugged(BACKEND_SLOTS["the scheduler"], slots) and "the scheduler" not in replaced:
        lines += [
            "",
            "[scheduler]",
            f'api = {_toml_string(CONTROL["AIRFLOW_API_URL"])}',
            f'user = {_toml_string(CONTROL["AIRFLOW_USER"])}',
            f'password = {_toml_string(CONTROL["AIRFLOW_PASSWORD"])}',
        ]
    # A registry backend the profile plugs (an unnamed-slot listing keeps the
    # table it always had, which had no section for it).
    for listed in _discovered(slots, plugs):
        if not listed.plugged:
            continue
        lines += ["", f"[{listed.discovery.backend}]"]
        lines += [f"{key} = {value if value.isdigit() else _toml_string(value)}"
                  for key, value in listed.discovery.services_toml.items()]
    for service in _services(slots, plugs):
        lines += [
            "",
            f"[service.{service.name}]",
            f"url = {_toml_string(service.url)}",
            f"alias = {_toml_string(service.alias)}",
            f"port = {service.port}",
            f"env = {_toml_string(service.url_var)}",
            f"description = {_toml_string(service.note)}",
        ]
        if service.user_var:
            lines.append(f"user = {_toml_string(FAKE_USER)}")
        if service.auth_var:
            lines.append(f"token = {_toml_string(FAKE_TOKEN)}")
            lines.append(f"token_env = {_toml_string(service.auth_var)}")
        for name, value in service.extra_vars:
            lines.append(f"{name.lower()} = {_toml_string(value)}")
    return "\n".join(lines) + "\n"


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _named(value) -> dict:
    """`{key, name}` from a workflow vocabulary field - the tracker's `project`,
    the wiki's `space` - which a theme may declare as a mapping or as the bare
    key (GT002's manifest says `project: DATA`). A bare key is used as its own
    name: the ids the agent will read are built from the key, so the key is the
    one name this page cannot get wrong."""
    if isinstance(value, dict):
        key = str(value.get("key") or value.get("name") or "")
        return {"key": key, "name": str(value.get("name") or key)}
    return {"key": str(value or ""), "name": str(value or "")}


def _project(issues: dict) -> dict:
    return _named(issues.get("project"))


def onboarding_markdown(root: Path, manifest: dict, slots=None, plugs=None) -> str:
    """`docs/onboarding.md`: which systems exist, and where the runbooks are.

    Assembled from what the build has already written - the workflow corpus,
    the dashboards, the theme's own job list - so the page can be wrong about
    the world only by the world being wrong about itself. Sections whose source
    a theme does not seed are left out rather than invented, which is why a
    theme with no wiki gets a shorter page and not a made-up one. Under a
    profile, a system in an unplugged slot is left out on the same terms, and
    so is the job table when there is no orchestrator to run it.
    """
    workflow = Path(root) / "var" / "workflow"
    issues = _load_json(workflow / "issues.json")
    kb = _load_json(workflow / "kb.json")
    chat = _load_json(workflow / "chat.json")
    lines = ["# Your first week", ""]
    if issues:
        team = sorted({u["team"] for u in issues["users"] if u.get("team")})
        lines += [
            f"You are on call for {_project(issues)['name']} "
            f"(`{_project(issues)['key']}`). "
            f"Teams you will hear from: {', '.join(team)}.",
            "",
        ]
    lines += [
        "Everything below is on the internal network. None of it is routable, "
        "none of the tokens are real, and all of them are already in `.envrc`.",
        "",
        "## The systems",
        "",
        "| system | where | reached with | variable |",
        "|---|---|---|---|",
    ]
    rows = [(label, where, clients) for label, where, clients in BACKEND_NOTES
            if _static_listed(label, slots, plugs)]
    # the registry's backends, where the second warehouse always was: before
    # the notebook server
    notebook = rows.pop() if rows and rows[-1][0] == "the notebook server" else None
    rows += [(listed.label, listed.where, listed.clients)
             for listed in _discovered(slots, plugs)]
    rows += [notebook] if notebook else []
    for label, where, clients in rows:
        lines.append(f"| {label} | `{where}` | {clients} | see `.envrc` |")
    for service in _services(slots, plugs):
        lines.append(
            f"| {service.name} | `{service.alias}:{service.port}` | "
            f"{service.clients or service.note} | `{service.url_var}` |")
    lines += [
        "",
        "`~/.config/alkera/services.toml` holds the same table if you want to "
        "read it from a script. A bare `dbt` uses `~/.dbt/profiles.yml`; this "
        "repository's `config/profiles.yml` is a different target.",
        "",
    ]
    # Where each plugged BI vendor's project is in the repository
    # (world/world/bi_projects.py). Only a vendor the profile names AND whose
    # directory the build wrote - rule 3: the page points at what is there, so
    # an unprofiled box, or a vendor box whose theme ships no project, gets no
    # such section and keeps its bytes.
    workspace = Path(root) / "workspace"
    projects = [s for s in _services(slots, plugs) if s in PLUG_SERVICES
                and (workspace / "bi" / s.name).is_dir()]
    if projects:
        lines += ["## The BI projects", "",
                  "| tool | project in this repository | service |", "|---|---|---|"]
        for service in projects:
            lines.append(f"| {service.name} | `bi/{service.name}/` | "
                         f"`{service.alias}:{service.port}` |")
        lines.append("")
        if (workspace / "bi" / "README.md").is_file():
            lines += ["`bi/README.md` describes how they are laid out.", ""]
    jobs = (manifest.get("jobs") or {}) if _plugged("orchestrator", slots) else {}
    if jobs:
        lines += ["## The pipeline", "",
                  "| job | command | runs after |", "|---|---|---|"]
        for name in sorted(jobs):
            spec = jobs[name] or {}
            after = spec.get("after")
            when = spec.get("time") or (f"`{after['job']}` + {after['minutes']}m"
                                        if after else "-")
            lines.append(f"| `{name}` | `{spec.get('command', '-')}` | {when} |")
        lines.append("")
    if kb:
        space = _named(kb["space"])
        pages = sorted(kb["pages"], key=lambda p: p["id"])
        if slots is None:
            # The box as it stood before profiles, byte for byte - including
            # listing the path, which the Confluence API does not route.
            lines += ["## Where the runbooks are", "",
                      f"The wiki is the `{space['key']}` space ({space['name']}). "
                      f"Pages, by id:", ""]
            for page in pages:
                indent = "  " * page["id"].count("/")
                lines.append(f"{indent}- `{page['id']}` - {page['title']}")
        else:
            lines += ["## Where the runbooks are", "",
                      f"The wiki is the `{space['key']}` space ({space['name']}). "
                      "Pages, by content id, with the path pages and tickets link "
                      "them by:", ""]
            for page in pages:
                indent = "  " * page["id"].count("/")
                lines.append(f"{indent}- `{content_id(page['id'])}` - {page['title']} "
                             f"(`{page['id']}`)")
        lines += [
            "",
            "Two of these overlap. The wiki records who edited a page and when, "
            "and so does this repository's git history; when they disagree, that "
            "is the evidence.",
            "",
        ]
    if chat:
        channels = sorted(c["id"] for c in chat["channels"])
        lines += [
            "## Where the people are", "",
            f"Workspace `{_named(chat['workspace'])['key']}`. Channels: "
            + ", ".join(f"`#{name}`" for name in channels) + ".",
            "",
        ]
    if issues:
        counts: dict[str, int] = {}
        for issue in issues["issues"]:
            counts[issue["status"]] = counts.get(issue["status"], 0) + 1
        lines += [
            "## The queue", "",
            f"`{_project(issues)['key']}`, statuses "
            + ", ".join(f"`{s}`" for s in issues["statuses"]) + ".",
            "",
        ]
        lines += ["| status | tickets |", "|---|---|"]
        for status in issues["statuses"]:
            lines.append(f"| `{status}` | {counts.get(status, 0)} |")
        lines.append("")
    dashboards = sorted((Path(root) / "var" / "bi" / "dashboards").glob("*.yml"))
    if dashboards:
        lines += ["## Dashboards", "",
                  "Defined in `var/bi/dashboards/`: "
                  + ", ".join(f"`{p.stem}`" for p in dashboards) + ".", ""]
    return "\n".join(lines)


def _write(path: Path, text: str, authored: str | None = None) -> Path:
    """Write LF/UTF-8 and refuse anything this module authored that is not LF-ASCII.

    `authored` is the part of `text` this module produced, which is the whole
    file except for `.envrc` - the theme ships its own, it already contains an
    em dash, and it already reproduces byte for byte. The workspace tree is
    hashed byte-exact, so a CR or a stray non-ASCII character in a GENERATED
    file would move the fingerprint under a world nobody changed; the same
    character in a file the theme committed is just the theme's file.
    """
    blob = (text if authored is None else authored)
    if "\r" in blob or not blob.isascii():
        raise SystemExit(
            f"the generated content of {path} is not LF-ASCII; the workspace tree "
            "is hashed byte-exact and this would move the fingerprint under a "
            "world nobody changed")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def write_for_built_world(root: Path, world_in_container: str = "/world",
                          home: str = CONTAINER_HOME) -> list[Path]:
    """Write discovery files for a world that did not go through theme seed native:true.

    Writes:
      workspace/.envrc          (merge envrc_block(home) via merge_envrc)
      workspace/docs/onboarding.md  (onboarding_markdown(root, {"jobs": jobs}))
      home/.config/alkera/services.toml  (services_toml)

    Does NOT write home/.dbt/profiles.yml (Postgres). Tasks already have DuckDB profiles.

    jobs come from var/sched/state.json key "jobs" if that file exists, else {}.
    Reuse envrc_block, merge_envrc, onboarding_markdown, services_toml, _write, HOME.

    `home` is the HOME of the box the world is served on (`CONTAINER_HOME` by
    default); the exported `DBT_PROFILES_DIR` follows it. The files are still
    written under `<root>/home`, whatever the box calls it.
    """
    root = Path(root)
    workspace = root / "workspace"
    home_dir = root / HOME
    envrc = workspace / ".envrc"
    existing = envrc.read_text(encoding="utf-8") if envrc.is_file() else ""
    block = envrc_block(home=home)
    state = root / "var" / "sched" / "state.json"
    payload = _load_json(state) or {}
    jobs = payload.get("jobs") if isinstance(payload, dict) else {}
    if not isinstance(jobs, dict):
        jobs = {}
    return [
        _write(envrc, merge_envrc(existing, block), authored=block),
        _write(workspace / "docs" / "onboarding.md",
               onboarding_markdown(root, {"jobs": jobs})),
        _write(home_dir / ".config" / "alkera" / "services.toml",
               services_toml(world_in_container)),
    ]


def write(root: Path, theme, world_in_container: str = "/world",
          slots=None, plugs=None, name=None) -> list[Path]:
    """Write the four discovery files. Returns them, in the order written.

    Called by `world.build.build` when the seed says `native: true`, after the
    workflow corpus exists (the onboarding page reads it) and before the git
    commit (the two workspace files belong in it, because on a real box they
    are checked in). `slots` is the seed profile's plugged slots, or None;
    `plugs` its optional {slot: [plug, ...]}, or None; `name` its
    "<role>/<stack>", which supplies the warehouse plug when `plugs` does not
    name one (`profile_plugs`).
    """
    plugs = profile_plugs(name, plugs) if slots is not None else plugs
    root = Path(root)
    workspace = root / "workspace"
    home = root / HOME
    envrc = workspace / ".envrc"
    existing = envrc.read_text(encoding="utf-8") if envrc.is_file() else ""
    block = envrc_block(slots, plugs)
    return [
        _write(envrc, merge_envrc(existing, block), authored=block),
        _write(workspace / "docs" / "onboarding.md",
               onboarding_markdown(root, theme.manifest, slots, plugs)),
        _write(home / ".dbt" / "profiles.yml",
               profiles_yaml(theme.manifest["dbt_profile"])),
        _write(home / ".config" / "alkera" / "services.toml",
               services_toml(world_in_container, slots, plugs)),
    ]
