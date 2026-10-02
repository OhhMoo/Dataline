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
when any of the three drift, which is what makes the duplication safe.

Credentials are obviously fake and every one of them is the same string,
`alkera-local-dev`: the repository rule is that nothing in a world may look
like a real key, and a value that appears in four files under four different
names is a value nobody mistakes for a secret.
"""
import json
from dataclasses import dataclass
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
SNOWFLAKE = {"SNOWFLAKE_HOST": "snowflake.internal",
             "SNOWFLAKE_ACCOUNT": "acme", "SNOWFLAKE_USER": FAKE_USER,
             "SNOWFLAKE_PASSWORD": FAKE_TOKEN}
#: The Jupyter plug (gateway/docker/jupyter/README.md): jupyter-server with the
#: audited kernel, served only when a profile plugs the notebook slot.
NOTEBOOK = {"JUPYTER_URL": "http://jupyter.internal:8888"}
#: What a `.internal` alias means, spelled once for the humans reading .envrc.
BACKEND_NOTES = (
    ("the warehouse", "postgres.internal:5432", "psql, dbt, psycopg"),
    ("object storage", "minio.internal:9000", "aws s3, boto3"),
    ("the scheduler", "airflow.internal:8080", "airflow CLI, REST"),
    ("the second warehouse", "snowflake.internal:8443", "snowsql, the connector"),
    ("the notebook server", "jupyter.internal:8888", "JupyterLab REST, nbclient"),
)
#: The slot behind each backend group, keyed by BACKEND_NOTES label. The
#: warehouse and dbt are always built (a profile selects planes, never the
#: data), but they are still keyed so that one rule decides every line.
BACKEND_SLOTS = {
    "the warehouse": "warehouse",
    "object storage": "object_store",
    "the scheduler": "orchestrator",
    "the second warehouse": "warehouse",
    "the notebook server": "notebook",
}

#: Slots whose plug is newer than the unprofiled box. "No profile" means the
#: full table as it stood before profiles, and that box is fingerprinted byte
#: for byte, so these appear only when a profile names them.
EXPLICIT_SLOTS = frozenset({"notebook"})


def _plugged(slot: str, slots) -> bool:
    if slots is None:
        return slot not in EXPLICIT_SLOTS
    return slot in slots


def _backends(slots):
    """The backend variable groups this profile plugs, as (comment, mapping)."""
    groups = (
        ("the warehouse", "# The warehouse. psql and psycopg need no arguments with these set.",
         POSTGRES),
        ("object storage",
         "# Object storage. `aws s3 ls` and boto3 resolve through this endpoint.", STORAGE),
        ("the scheduler", "# The scheduler.", CONTROL),
        ("the second warehouse", "# The second warehouse.", SNOWFLAKE),
        ("the notebook server", "# The notebook server.", NOTEBOOK),
    )
    return [(comment, mapping) for label, comment, mapping in groups
            if _plugged(BACKEND_SLOTS[label], slots)]


def _services(slots):
    return [s for s in SERVICES if _plugged(SERVICE_SLOTS[s.name], slots)]


def environment(slots=None) -> dict[str, str]:
    """Every variable the native box exports, as one ordered mapping.

    The single definition. `.envrc`, `services.toml` and the test that compares
    this against `compose.native.yml`'s `episode.environment` all read it, so
    an endpoint cannot be added to one and forgotten in the others.
    """
    env: dict[str, str] = {}
    for _, mapping in _backends(slots):
        env.update(mapping)
    env.update(DBT)
    for service in _services(slots):
        env[service.url_var] = service.url
        for name in service.alias_vars:
            env[name] = service.url
        if service.user_var:
            env[service.user_var] = FAKE_USER
        if service.auth_var:
            env[service.auth_var] = FAKE_TOKEN
    return env


def envrc_block(slots=None) -> str:
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

    for comment, mapping in _backends(slots):
        lines += ["", comment]
        emit(mapping)
    lines += ["", "# Where a bare `dbt` finds its profile."]
    emit(DBT)
    for service in _services(slots):
        lines += ["", f"# {service.name} - {service.note}"]
        lines.append(f'export {service.url_var}="{service.url}"')
        for name in service.alias_vars:
            lines.append(f'export {name}="{service.url}"')
        if service.user_var:
            lines.append(f'export {service.user_var}="{FAKE_USER}"')
        if service.auth_var:
            lines.append(f'export {service.auth_var}="{FAKE_TOKEN}"')
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


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def services_toml(root_in_container: str, slots=None) -> str:
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
        "",
        "[warehouse]",
        f'host = {_toml_string(POSTGRES["PGHOST"])}',
        f'port = {POSTGRES["PGPORT"]}',
        f'database = {_toml_string(POSTGRES["PGDATABASE"])}',
        f'user = {_toml_string(POSTGRES["PGUSER"])}',
        f'password = {_toml_string(POSTGRES["PGPASSWORD"])}',
    ]
    if _plugged(BACKEND_SLOTS["object storage"], slots):
        lines += [
            "",
            "[storage]",
            f'endpoint = {_toml_string(STORAGE["AWS_ENDPOINT_URL"])}',
            f'access_key_id = {_toml_string(STORAGE["AWS_ACCESS_KEY_ID"])}',
            f'secret_access_key = {_toml_string(STORAGE["AWS_SECRET_ACCESS_KEY"])}',
        ]
    if _plugged(BACKEND_SLOTS["the scheduler"], slots):
        lines += [
            "",
            "[scheduler]",
            f'api = {_toml_string(CONTROL["AIRFLOW_API_URL"])}',
            f'user = {_toml_string(CONTROL["AIRFLOW_USER"])}',
            f'password = {_toml_string(CONTROL["AIRFLOW_PASSWORD"])}',
        ]
    for service in _services(slots):
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


def onboarding_markdown(root: Path, manifest: dict, slots=None) -> str:
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
    for label, where, clients in BACKEND_NOTES:
        if _plugged(BACKEND_SLOTS[label], slots):
            lines.append(f"| {label} | `{where}` | {clients} | see `.envrc` |")
    for service in _services(slots):
        lines.append(
            f"| {service.name} | `{service.alias}:{service.port}` | "
            f"{service.note} | `{service.url_var}` |")
    lines += [
        "",
        "`~/.config/alkera/services.toml` holds the same table if you want to "
        "read it from a script. A bare `dbt` uses `~/.dbt/profiles.yml`; this "
        "repository's `config/profiles.yml` is a different target.",
        "",
    ]
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
        lines += ["## Where the runbooks are", "",
                  f"The wiki is the `{space['key']}` space ({space['name']}). "
                  f"Pages, by id:", ""]
        for page in sorted(kb["pages"], key=lambda p: p["id"]):
            indent = "  " * page["id"].count("/")
            lines.append(f"{indent}- `{page['id']}` - {page['title']}")
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


def write_for_built_world(root: Path, world_in_container: str = "/world") -> list[Path]:
    """Write discovery files for a world that did not go through theme seed native:true.

    Writes:
      workspace/.envrc          (merge envrc_block() via merge_envrc)
      workspace/docs/onboarding.md  (onboarding_markdown(root, {"jobs": jobs}))
      home/.config/alkera/services.toml  (services_toml)

    Does NOT write home/.dbt/profiles.yml (Postgres). Tasks already have DuckDB profiles.

    jobs come from var/sched/state.json key "jobs" if that file exists, else {}.
    Reuse envrc_block, merge_envrc, onboarding_markdown, services_toml, _write, HOME.
    """
    root = Path(root)
    workspace = root / "workspace"
    home = root / HOME
    envrc = workspace / ".envrc"
    existing = envrc.read_text(encoding="utf-8") if envrc.is_file() else ""
    block = envrc_block()
    state = root / "var" / "sched" / "state.json"
    payload = _load_json(state) or {}
    jobs = payload.get("jobs") if isinstance(payload, dict) else {}
    if not isinstance(jobs, dict):
        jobs = {}
    return [
        _write(envrc, merge_envrc(existing, block), authored=block),
        _write(workspace / "docs" / "onboarding.md",
               onboarding_markdown(root, {"jobs": jobs})),
        _write(home / ".config" / "alkera" / "services.toml",
               services_toml(world_in_container)),
    ]


def write(root: Path, theme, world_in_container: str = "/world",
          slots=None) -> list[Path]:
    """Write the four discovery files. Returns them, in the order written.

    Called by `world.build.build` when the seed says `native: true`, after the
    workflow corpus exists (the onboarding page reads it) and before the git
    commit (the two workspace files belong in it, because on a real box they
    are checked in). `slots` is the seed profile's plugged slots, or None.
    """
    root = Path(root)
    workspace = root / "workspace"
    home = root / HOME
    envrc = workspace / ".envrc"
    existing = envrc.read_text(encoding="utf-8") if envrc.is_file() else ""
    block = envrc_block(slots)
    return [
        _write(envrc, merge_envrc(existing, block), authored=block),
        _write(workspace / "docs" / "onboarding.md",
               onboarding_markdown(root, theme.manifest, slots)),
        _write(home / ".dbt" / "profiles.yml",
               profiles_yaml(theme.manifest["dbt_profile"])),
        _write(home / ".config" / "alkera" / "services.toml",
               services_toml(world_in_container, slots)),
    ]
