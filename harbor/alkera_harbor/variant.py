"""Environment variants: one agent-image Dockerfile per (role, clients, profile).

The unprofiled tasks (GT001-GT009) build from `harbor/template/environment/
Dockerfile` VERBATIM. Their variant is `UNPROFILED`, and rendering it is the
identity (tested), so their Dockerfile, their task.toml env and their digests
do not move when a profiled variant is added.

A task whose task.toml names an environment profile (`[environment] profile`,
`[environment.plugs]`) gets the same template with three edits, made at
anchors on the template's own lines (the template carries no placeholder, so
it stays a valid Dockerfile and the unprofiled bytes):

  header   a comment naming the variant, right after the canary line;
  role     the role's hash-locked libraries, `gateway/requirements-<role>.lock`
  clients  (what gateway/docker/Dockerfile installs for ALKERA_TOOLBELT=<role>),
           then the warehouse client locks the profile's warehouse plugs need
           (`gateway/requirements-<client>.lock`), then for `ds`/`da` the
           audited Jupyter kernelspec, as the episode image installs them -
           all into the image interpreter, right after the DuckDB extension
           layer. Every instruction before that point is the template's, so a
           variant reuses the unprofiled image's cached layers (apt, the
           toolbelt, the gateway locks and wheel, the extension: everything a
           build fetches from the network but the role's own lock). The
           episode image installs the role lock before the gateway wheel;
           the order changes nothing installed, because every lock is an
           export of the one gateway/uv.lock and the wheel goes in --no-deps.
           `de` adds nothing (the template);
  ENV      the agent-environment block becomes the profile's environment:
           `world.native_workspace.environment(slots, plugs, home)` (exactly
           what the world's `.envrc` exports for that box), plus `SCHED_URL`
           when the box has the served scheduler (unprofiled, or a profile
           that plugs `ops_sched`) and `REQUESTS_CA_BUNDLE` (the bundle the
           image always ships, as the episode image always sets it). The
           `ALKERA_*` audit variables and `PATH` stay exactly as the template
           has them.

The key is (role, clients, profile, plugs): the profile's NAME alone does not
fix its environment (BI001 and a hypothetical da/laptop task with only Power BI
differ), so "profile" in the plan's (role, clients, profile) is the resolved
profile, name and plugs. Two tasks with one key get byte-identical Dockerfiles
and share every build-cache layer up to the per-task block.

BACKENDS (plan Task 10). A profile that plugs a registry backend
(`alkera_gateway.backends`) or the `postgres` warehouse gets, per backend
(HARBOR_BACKENDS and its companion tables below):

  processes  episode.json `backends` entries, which world-up supervises
             beside the services (none for a backend the services host);
  layers     Dockerfile lines after that backend's client lock (a server
             package, a launcher, a private directory), and the repository
             files they COPY (`Variant.support_files`, never under tasks/);
  audit      episode.json `statement_logs`, the server logs world-down turns
             into action-log rows (a backend whose audit is its own log).

FENCES (loud, by name). A profile whose warehouse plug the Harbor image does
not serve is refused before its world is built: nothing would answer at its
address, or (REFUSED_WAREHOUSES) what would answer cannot be made safe in one
container. The notebook plug (Jupyter) is not served either: it is allowed,
because the BI tasks plug it without using it, and listed in harbor/README.md's
known deviations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import harness
from .constants import AGENT_HOME, AUDIT_IN_CONTAINER, WORLD_IN_CONTAINER

#: The template ENV names that are the image's own, not the box's discovery
#: environment: kept verbatim in every variant.
KEPT_ENV_PREFIXES = ("ALKERA_",)
KEPT_ENV_NAMES = ("PATH",)
#: The bundle every image ships (the template's RUN copies certifi there).
CA_BUNDLE_VAR = "REQUESTS_CA_BUNDLE"
#: Warehouse plugs the Harbor image serves: the world's own DuckDB file, and
#: each warehouse backend that has its HARBOR_BACKENDS entry.
SERVED_WAREHOUSES = ("duckdb", "databricks", "redshift", "snowflake", "postgres")
#: Warehouse plugs the image refuses, and why (the export's error says it).
REFUSED_WAREHOUSES: dict[str, str] = {
    "bigquery": (
        "its emulator (goccy bigquery-emulator) answers HTTP and gRPC on TCP ports "
        "with no credential check, and in a one-container image every process "
        "shares the loopback, so the agent could query and write the seeded copy "
        "around the audited proxy at bigquery.internal:9050, unrecorded; fencing a "
        "loopback port by user needs NET_ADMIN or a network namespace, which no "
        "Harbor provider grants (the emulator cannot listen on a unix socket, and "
        "in the container topology it sits on backend_net, out of the agent's "
        "reach)"),
}
#: The warehouse plug served by a process of its own that is not a registry
#: backend (no client lock, no agent-facing variables of its own: the box's PG*
#: are the static table's): a Harbor backend all the same.
POSTGRES_PLUG = "postgres"
#: Plugs that are not served in the image but do not block an export; each is
#: a known deviation (harbor/README.md).
UNSERVED_ALLOWED = {("notebook", "jupyter"): "jupyter.internal:8888 is not started"}
#: Roles whose box installs a role lock (gateway/docker/Dockerfile's
#: ALKERA_TOOLBELT): `de` installs nothing beyond the gateway's locks.
ROLE_LOCKS = ("ds", "da")
#: Roles whose box carries the audited Jupyter kernelspec.
KERNEL_ROLES = ("ds", "da")
#: Plan Task 10's extension points, keyed by Harbor backend name (a registry
#: backend's name, or POSTGRES_PLUG):
#:   HARBOR_BACKENDS  -> a function of the variant giving its episode.json
#:                    `backends` entries (name, argv, user, ports, hosts,
#:                    health; the environment runtime's contract). [] for a
#:                    backend the services process hosts. A plugged backend
#:                    with no entry here is refused at export.
#:   CLIENT_LAYERS    client lock name -> extra Dockerfile lines right after
#:                    that client's lock (a server package, a launcher)
#:   PLUG_LAYERS      a backend with no client lock (POSTGRES_PLUG) -> its
#:                    Dockerfile lines, after every client lock. A block (lines
#:                    between blank lines) already emitted is not emitted again,
#:                    so two backends can share one server package layer.
#:   SUPPORT_FILES    -> (repository file, path under the context's alkera/)
#:                    pairs those lines COPY
#:   STATEMENT_LOGS   -> its episode.json `statement_logs` entries ({"log",
#:                    "tailer"}): world-down folds each into the action log
#: (Defined at the end of this module, one block per backend.)

# ---- anchors on the template's own lines (each must match exactly once)
_PIP = "pip install --no-cache-dir --retries=5 --timeout=120 --require-hashes -r"
#: The last line of the DuckDB `postgres` extension RUN: the role block goes
#: right after it.
ANCHOR_DUCKDB_EXTENSION = "    && cp -a /root/.duckdb /home/engineer/.duckdb"
ANCHOR_ENV_COMMENT = "# The agent's environment:"
#: The agent template's apt RUN (its first and last lines), whose frozen
#: snapshot and `git=` pin the verifier variant reuses.
ANCHOR_APT_FIRST = "RUN set -eux; \\"
ANCHOR_APT_INSTALL = "    apt-get install -y --no-install-recommends \\"
ANCHOR_APT_LAST = "    rm -rf /var/lib/apt/lists/*"
#: The verifier template's last line of its pip RUN: git goes right after it.
ANCHOR_VERIFIER_PIP = "        -r /opt/alkera/requirements.lock"
#: What a profiled verifier adds: the graders of a profiled task read the
#: agent's repository with the git CLI (BI001/BI002: `git archive HEAD`, the
#: branch listing); the unprofiled graders never shell out to git, and the
#: unprofiled verifier stays byte-identical.
VERIFIER_PACKAGES = ("git",)
_ENV = re.compile(r"^ENV ([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class VariantError(ValueError):
    pass


@dataclass(frozen=True)
class Variant:
    """One agent-image variant. `slots` is the resolved profile's slots (a
    function of profile and plugs), carried for rendering, not compared."""

    role: str = "de"
    clients: tuple[str, ...] = ()
    profile: str | None = None
    plugs: tuple[tuple[str, tuple[str, ...]], ...] = ()
    slots: tuple[tuple[str, tuple[str, ...]], ...] | None = field(default=None,
                                                                  compare=False)
    #: The internal services the profile serves (Profile.services), for the
    #: task README.
    services: tuple[str, ...] = field(default=(), compare=False)
    #: The registry backends the profile plugs (`alkera_gateway.backends`), by
    #: name: the box dbt profile's extra targets (worldbuild, box_profiles_yaml).
    backends: tuple[str, ...] = field(default=(), compare=False)
    #: What the image runs or hosts for the profile beyond the unprofiled box:
    #: `backends` plus POSTGRES_PLUG when the warehouse slot plugs it, in that
    #: order (the keys of HARBOR_BACKENDS and its companion tables).
    harbor_backends: tuple[str, ...] = field(default=(), compare=False)

    @property
    def profiled(self) -> bool:
        return self.profile is not None

    @property
    def key(self) -> str:
        """Stable, human-readable identity: `unprofiled`, or
        `da/laptop+bi=powerbi,sigma,tableau` (role and clients follow from it,
        and are spelled anyway so a key read alone says what it installs)."""
        if not self.profiled:
            return "unprofiled"
        plugs = ";".join(f"{slot}={','.join(names)}" for slot, names in self.plugs)
        return (f"{self.profile}" + (f"+{plugs}" if plugs else "")
                + f" [role {self.role}, clients {','.join(self.clients) or 'none'}]")

    def plug_map(self) -> dict[str, list[str]]:
        return {slot: list(names) for slot, names in self.plugs}

    def slot_map(self) -> dict[str, tuple[str, ...]] | None:
        return None if self.slots is None else {s: tuple(n) for s, n in self.slots}

    def context_files(self) -> list[str]:
        """Repository files this variant's Dockerfile COPYs beyond the
        template's context.txt (destination = the same path under alkera/)."""
        locks = ([self.role] if self.role in ROLE_LOCKS else []) + list(self.clients)
        return [f"gateway/requirements-{name}.lock" for name in locks]

    def support_files(self) -> list[tuple[str, str]]:
        """(repository file, destination under alkera/) for every file the
        backends' Dockerfile lines COPY (SUPPORT_FILES), in backend order, once."""
        out: list[tuple[str, str]] = []
        for name in self.harbor_backends:
            for pair in SUPPORT_FILES.get(name, ()):
                if pair not in out:
                    out.append(pair)
        return out


UNPROFILED = Variant()


def _canonical(mapping) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(sorted((str(slot), tuple(sorted(str(n) for n in names)))
                        for slot, names in (mapping or {}).items()))


def plugged_backends(slots: dict) -> tuple:
    """The registry backends a profile's slots plug
    (`alkera_gateway.backends.plugged`: a ProfileError for two TLS backends)."""
    harness.modules()                           # the gateway's src on sys.path
    from alkera_gateway import backends

    return backends.plugged(slots)


def clients_for(slots: dict) -> tuple[str, ...]:
    """The warehouse client locks a profile's box installs: each plugged
    registry backend's `clients` (`requirements-<clients>.lock`), the rule
    container_episode.profile_image and build_episode.image_tag share."""
    return tuple(sorted({b.clients for b in plugged_backends(slots) if b.clients}))


def variant_of(spec) -> Variant:
    """The variant of a loaded task spec (taskspec.load)."""
    profile = str(getattr(spec, "profile", "") or "")
    plugs = getattr(spec, "plugs", None) or {}
    if not profile:
        if plugs:
            raise VariantError("[environment.plugs] without [environment] profile")
        return UNPROFILED
    m = harness.modules()
    try:
        resolved = m.episode.environment_profile(profile, plugs)
    except m.episode.HarnessError as exc:
        raise VariantError(f"profile {profile!r}: {exc}") from None
    slots = {slot: tuple(names) for slot, names in resolved.slots.items()}
    warehouses = slots.get("warehouse", ())
    refused = [f"warehouse={name}: {REFUSED_WAREHOUSES[name]}" for name in warehouses
               if name in REFUSED_WAREHOUSES]
    if refused:
        raise VariantError(
            f"profile {profile!r} plugs a warehouse the Harbor image refuses to serve:\n  "
            + "\n  ".join(refused))
    unserved = [f"warehouse={name}" for name in warehouses
                if name not in SERVED_WAREHOUSES]
    if unserved:
        raise VariantError(
            f"profile {profile!r} plugs {', '.join(unserved)}, which the Harbor image "
            f"does not serve (only {', '.join(SERVED_WAREHOUSES)}): nothing would "
            f"answer at its address.")
    role = profile.split("/", 1)[0]
    try:
        plugged = plugged_backends(slots)
    except ValueError as exc:                    # plugs.ProfileError: two TLS backends
        raise VariantError(f"profile {profile!r}: {exc}") from None
    names = tuple(b.name for b in plugged)
    return Variant(role=role,
                   clients=tuple(sorted({b.clients for b in plugged if b.clients})),
                   profile=profile, plugs=_canonical(plugs), slots=_canonical(slots),
                   services=tuple(resolved.services), backends=names,
                   harbor_backends=names + ((POSTGRES_PLUG,) if POSTGRES_PLUG in warehouses
                                            else ()))


def backend_specs(variant: Variant) -> list[dict]:
    """The episode.json `backends` of a variant: HARBOR_BACKENDS' entries for
    each backend it plugs, in `harbor_backends` order (none for a backend the
    services host). A plugged backend the image knows nothing of is refused."""
    specs: list[dict] = []
    for name in variant.harbor_backends:
        if name not in HARBOR_BACKENDS:
            raise VariantError(f"the {variant.key} variant plugs backend {name!r}, which "
                               f"the Harbor image neither runs nor hosts")
        specs += [dict(spec) for spec in HARBOR_BACKENDS[name](variant)]
    return specs


def statement_logs(variant: Variant) -> list[dict]:
    """The episode.json `statement_logs` of a variant (STATEMENT_LOGS), in
    `harbor_backends` order: [] for every variant whose backends write their
    rows themselves."""
    return [dict(entry) for name in variant.harbor_backends
            for entry in STATEMENT_LOGS.get(name, ())]


def unserved_notes(variant: Variant) -> list[str]:
    """The known deviations this variant carries (UNSERVED_ALLOWED)."""
    slots = variant.slot_map() or {}
    return [f"{slot}={plug}: {why}" for (slot, plug), why in sorted(UNSERVED_ALLOWED.items())
            if plug in slots.get(slot, ())]


# ------------------------------------------------------------------ the env
def environment(variant: Variant, template_env: dict[str, str],
                home: str = AGENT_HOME) -> dict[str, str]:
    """The box's discovery environment for `variant` (the Dockerfile's agent-
    environment block minus the `ALKERA_*` and `PATH` lines), in Dockerfile
    order: sorted by name, `REQUESTS_CA_BUNDLE` last (as in the template).

    `template_env` is the template's parsed ENV (dockerenv.parse_env), the
    source of the bundle path, so the two cannot drift - unless the profile
    plugs a TLS backend, whose own bundle (`backends.bundle`: certifi plus the
    certificate it mints at boot) is the one the box trusts, as the measured
    container overlay sets it (one TLS backend per profile: `backends.plugged`)."""
    harness.modules()                           # world/ on sys.path
    from world import native_workspace as nw

    slots = variant.slot_map()
    plugs = nw.profile_plugs(variant.profile, variant.plug_map()) if variant.profiled \
        else None
    env = dict(nw.environment(slots, plugs, home=home))
    if slots is None or "ops_sched" in slots.get("orchestrator", ()):
        sched = next(p for p in nw.WORKSPACE_PLANES if p.name == "sched")
        env[sched.url_var] = sched.url
    if CA_BUNDLE_VAR not in template_env:
        raise VariantError(f"the template sets no {CA_BUNDLE_VAR}")
    env.pop(CA_BUNDLE_VAR, None)
    ordered = {name: env[name] for name in sorted(env)}
    ordered[CA_BUNDLE_VAR] = template_env[CA_BUNDLE_VAR]
    if variant.backends:
        from alkera_gateway import backends as registry

        for name in variant.backends:
            if registry.BACKENDS[name].tls:
                ordered[CA_BUNDLE_VAR] = registry.bundle(registry.BACKENDS[name])
    for name, value in ordered.items():
        if "$" in value or any(c.isspace() for c in value) or '"' in value:
            raise VariantError(f"{name}={value!r} is not a literal ENV value")
    return ordered


def _kept(name: str) -> bool:
    return name in KEPT_ENV_NAMES or name.startswith(KEPT_ENV_PREFIXES)


# ------------------------------------------------------------ the Dockerfile
def _index(lines: list[str], wanted: str, what: str, *, startswith: bool = False) -> int:
    hits = [i for i, line in enumerate(lines)
            if (line.startswith(wanted) if startswith else line == wanted)]
    if len(hits) != 1:
        raise VariantError(f"the environment template has {len(hits)} {what} lines "
                           f"({wanted!r}); harbor/alkera_harbor/variant.py anchors on "
                           f"exactly one - update it with the template")
    return hits[0]


def render_dockerfile(template: str, variant: Variant, template_env: dict[str, str],
                      home: str = AGENT_HOME) -> str:
    """The agent Dockerfile for `variant`. The template itself for UNPROFILED."""
    if not variant.profiled:
        return template
    lines = template.split("\n")

    # ENV: replace the discovery lines of the agent-environment block
    start = _index(lines, ANCHOR_ENV_COMMENT, "agent-environment comment", startswith=True)
    first = next(i for i in range(start, len(lines)) if lines[i].startswith("ENV "))
    end = first
    while end < len(lines) and lines[end].startswith("ENV "):
        end += 1
    block = lines[first:end]
    for line in block:
        if not _ENV.match(line):
            raise VariantError(f"cannot parse the template's {line!r}")
    kept = [line for line in block if _kept(_ENV.match(line).group(1))]
    replaced = [line for line in block if not _kept(_ENV.match(line).group(1))]
    if not replaced or any(not _kept(_ENV.match(line).group(1))
                           for line in block[len(replaced):]):
        raise VariantError("the template's agent-environment block must list the "
                           "discovery variables first, then ALKERA_* and PATH")
    env = environment(variant, template_env, home)
    comment = (f"# VARIANT {variant.profile}: the profile's environment (world.native_"
               f"workspace.environment for its")
    new_block = [
        comment,
        f"# slots and plugs, HOME {home}) in place of the unprofiled box's.",
        *(f"ENV {name}={value}" for name, value in env.items()),
        *kept]
    lines[first:end] = new_block

    # role lock, client locks, kernelspec: right after the DuckDB extension
    # layer, so every layer before them is the unprofiled image's (cached)
    at = _index(lines, ANCHOR_DUCKDB_EXTENSION, "DuckDB extension install")
    insert: list[str] = []
    emitted: set[tuple[str, ...]] = set()
    for kind, name in ([("role", variant.role)] if variant.role in ROLE_LOCKS else []) \
            + [("clients", c) for c in variant.clients]:
        lock = f"/opt/gateway/requirements-{name}.lock"
        what = (f"Role {name}: the libraries the {name} box adds to this interpreter "
                f"(gateway/docker/Dockerfile ALKERA_TOOLBELT={name})." if kind == "role"
                else f"Warehouse clients {name} (gateway/docker/Dockerfile "
                     f"ALKERA_WAREHOUSE_CLIENTS).")
        insert += ["",
                   f"# {what}",
                   f"COPY alkera/gateway/requirements-{name}.lock {lock}",
                   f"RUN {_PIP} {lock}"]
        if kind == "clients":
            insert += _blocks(CLIENT_LAYERS.get(name, ()), emitted)
    for name in variant.harbor_backends:
        insert += _blocks(PLUG_LAYERS.get(name, ()), emitted)
    if variant.role in KERNEL_ROLES:
        insert += ["",
                   f"# Role {variant.role}: the Jupyter plug's audited kernelspec, "
                   f"system-wide as `alkera-python` and over ipykernel's `python3` "
                   f"(gateway/docker/Dockerfile).",
                   "RUN python -c \"from alkera_gateway.native.notebook import "
                   "install_audited_kernelspecs; "
                   "install_audited_kernelspecs('/usr/local/share/jupyter', "
                   "interpreter='/usr/local/bin/python')\""]
    lines[at + 1:at + 1] = insert

    _header(lines, variant)
    return "\n".join(lines)


def _blocks(layer_lines: tuple[str, ...], emitted: set[tuple[str, ...]]) -> list[str]:
    """`layer_lines` split into blocks at blank lines, each preceded by a blank
    line, minus any block already in `emitted` (which is updated): two backends
    that need one server package install it once."""
    blocks: list[list[str]] = [[]]
    for line in layer_lines:
        if line == "":
            blocks.append([])
        else:
            blocks[-1].append(line)
    out: list[str] = []
    for block in blocks:
        if block and tuple(block) not in emitted:
            emitted.add(tuple(block))
            out += ["", *block]
    return out


def _header(lines: list[str], variant: Variant, template: str = "environment",
            edits: str = "the role/client block after the DuckDB extension layer, "
                         "and the ENV block") -> None:
    """The variant comment, right after the canary line (in place)."""
    lines[1:1] = [
        "#",
        f"# VARIANT {variant.key}",
        f"# Rendered from harbor/template/{template}/Dockerfile by",
        "# harbor/alkera_harbor/variant.py: byte-identical in every task of this",
        "# variant (the unprofiled tasks build from the template verbatim). The",
        f"# edits are this comment, {edits}.",
    ]


def verifier_apt_run(environment_template: str,
                     packages: tuple[str, ...] = VERIFIER_PACKAGES) -> list[str]:
    """The agent template's apt RUN (its frozen snapshot sources, its version
    pins) narrowed to `packages`: the verifier gets the same git, from the
    same snapshot, and the two can never drift."""
    lines = environment_template.split("\n")
    first = _index(lines, ANCHOR_APT_FIRST, "apt RUN start")
    install = _index(lines, ANCHOR_APT_INSTALL, "apt-get install")
    last = _index(lines, ANCHOR_APT_LAST, "apt RUN end")
    if not first < install < last:
        raise VariantError("the environment template's apt RUN is not in the expected shape")
    pins = {line.strip().split("=", 1)[0]: line for line in lines[install + 1:last]
            if "=" in line and line.strip().endswith("\\")}
    missing = [p for p in packages if p not in pins]
    if missing:
        raise VariantError(f"the environment template pins no {missing} to reuse")
    return [*lines[first:install + 1], *(pins[p] for p in packages),
            *lines[last - 1:last + 1]]


def render_tests_dockerfile(template: str, variant: Variant,
                            environment_template: str) -> str:
    """The verifier Dockerfile for `variant`. The template itself for
    UNPROFILED; a profiled variant adds VERIFIER_PACKAGES (git) right after
    the hash-locked pip layer, so the layers before it stay shared."""
    if not variant.profiled:
        return template
    lines = template.split("\n")
    at = _index(lines, ANCHOR_VERIFIER_PIP, "verifier pip install")
    lines[at + 1:at + 1] = [
        "",
        f"# VARIANT {variant.profile}: {', '.join(VERIFIER_PACKAGES)} for the graders, which "
        f"read the agent's",
        "# repository with the git CLI (`git archive HEAD`, its branches). Same",
        "# frozen snapshot and pin as the agent image (environment/Dockerfile).",
        *verifier_apt_run(environment_template),
    ]
    _header(lines, variant, template="tests", edits="and git after the pip layer")
    return "\n".join(lines)


# ------------------------------------------------------------- the backends
def _hosted(_variant: Variant) -> list[dict]:
    """A backend the services process hosts: serve.py mounts its module on its
    registry port when the profile plugs it, and world-up aliases its host from
    the services' own destinations. No process of its own."""
    return []


def _registry(name: str):
    harness.modules()                           # the gateway's src on sys.path
    from alkera_gateway import backends

    return backends, backends.BACKENDS[name]


#: Where a backend run as root keeps its private per-statement record (the
#: runtime's log directory: root 0700).
PRIVATE_LOGS = "/var/log/alkera"
#: The image's interpreter (the services' and the agent's).
PYTHON = "/usr/local/bin/python"
#: Where the Harbor-only backend launchers (harbor/template/backends/) go.
BACKEND_DIR = "/opt/alkera/backends"


def _container_backend(name: str, argv: list[str], **extra: str) -> list[dict]:
    """A registry backend that has its own container in the measured topology,
    run here as root with the environment backend_overlay gives that container
    (container_episode.NativeContainerEpisode.backend_overlay): the action log,
    the world's warehouse files to seed from (read before the agent starts),
    its own destinations file (world-down folds it), its private statement
    record; plus `extra` (ALKERA_<NAME>_<KEY>=value) for what one container's
    isolation gave it and this image must say."""
    backends, b = _registry(name)
    upper = name.upper()
    env = [f"ALKERA_NATIVE_AUDIT={AUDIT_IN_CONTAINER}/actions.jsonl",
           *([f"ALKERA_{upper}_SEED={WORLD_IN_CONTAINER}/var/warehouse"]
             if b.seeds_world else []),
           f"ALKERA_{upper}_DESTINATIONS={AUDIT_IN_CONTAINER}/{backends.destinations_file(b)}",
           f"ALKERA_{upper}_STATEMENT_LOG={PRIVATE_LOGS}/{name}-statements.jsonl",
           *(f"ALKERA_{upper}_{key.upper()}={value}" for key, value in extra.items())]
    return [{"name": name, "argv": ["/usr/bin/env", *env, *argv], "user": "root",
             "ports": [b.port], "hosts": [b.host], "health": "tcp"}]


def _snowflake(_variant: Variant) -> list[dict]:
    """fakesnow behind the audited TLS proxy (snowflake_start.py), with its
    upstream on a unix socket in a root-only directory: in its own container
    the upstream's loopback port is out of the agent's reach, here it would be
    a second door, unaudited."""
    return _container_backend("snowflake", ["/opt/snowflake/bin/python",
                                            "/opt/stack/snowflake_start.py"],
                              upstream_socket="/snowflake-data/run/fakesnow.sock")


def _postgres(_variant: Variant) -> list[dict]:
    """The `postgres` warehouse plug: PostgreSQL seeded from the world, as the
    server's own user (harbor/template/backends/postgres_start.py), listening on
    127.0.0.1:5432 only once seeded. It writes no row itself: the server logs
    the agent's role's statements, and world-down folds that log
    (STATEMENT_LOGS). Connections are not counted (as in the measured
    topology)."""
    return [{"name": "postgres", "argv": [PYTHON, f"{BACKEND_DIR}/postgres_start.py"],
             "user": "postgres", "ports": [5432], "hosts": ["postgres.internal"],
             "health": "tcp"}]


def _redshift(_variant: Variant) -> list[dict]:
    """The Postgres persona, server and proxy in ONE session (redshift_start.py's
    bare form): the server has no TCP port to be supervised by (a private unix
    socket only, by design), the proxy listens only once the seed is in - what
    world-up waits for - and the two live and die together."""
    return _container_backend("redshift", [PYTHON, "/opt/stack/redshift_start.py"],
                              data="/var/lib/alkera/redshift")


#: PostgreSQL 17's server, from the frozen snapshot the toolbelt's apt layer
#: resolved against (its sources stay in the image) - the same build as its
#: postgresql-client-17 - with no default cluster. Shared by the backends that
#: need it (one layer when two are plugged).
POSTGRES_SERVER_LAYER = (
    "",
    "# PostgreSQL 17, the server (a warehouse backend's): the same frozen snapshot",
    "# and build as the toolbelt's postgresql-client-17. No default cluster; each",
    "# backend initialises its own, fresh, at every start.",
    "RUN set -eux; \\",
    "    mkdir -p /etc/postgresql-common/createcluster.d; \\",
    "    echo 'create_main_cluster = false' > "
    "/etc/postgresql-common/createcluster.d/alkera.conf; \\",
    "    apt-get update; \\",
    "    apt-get install -y --no-install-recommends postgresql-17=17.11-0+deb13u1; \\",
    "    rm -rf /var/lib/apt/lists/*; \\",
    "    /usr/lib/postgresql/17/bin/postgres --version",
)


HARBOR_BACKENDS: dict = {
    # Airflow 3 REST v2 over the world ledger (native/services/airflow.py), at
    # airflow.internal:8100; its client lock (airflowctl) is the box's.
    "airflow": _hosted,
    # Databricks tier 1 (native/services/databricks.py) at
    # https://databricks.internal (443: the services run as root): its
    # lakehouse copy in the services' private state, its certificate minted at
    # start (the key in a root-only temporary directory), its bundle published
    # to the trust directory below.
    "databricks": _hosted,
    # Redshift: the Postgres persona (gateway/docker/redshift_start.py), as root
    # (the proxy writes the action log; the server it starts runs as
    # `postgres`), 5439 at redshift.internal.
    "redshift": _redshift,
    # Snowflake: fakesnow behind the audited TLS proxy, as root, 8443 at
    # snowflake.internal; the certificate minted at start, its bundle at
    # /etc/alkera/snowflake-tls/bundle.pem (the box's REQUESTS_CA_BUNDLE).
    "snowflake": _snowflake,
    # The postgres warehouse plug: PostgreSQL as `postgres`, 5432 at
    # postgres.internal, audited by world-down's fold of its statement log.
    POSTGRES_PLUG: _postgres,
}
CLIENT_LAYERS: dict[str, tuple[str, ...]] = {
    "redshift": (
        *POSTGRES_SERVER_LAYER,
        "",
        "# Redshift (the Postgres persona behind the audited wire proxy, run whole:",
        "# redshift_start.py). Its data directory is root's; the server's directories",
        "# in it are postgres' own (0700), so only root and postgres reach its socket.",
        "COPY alkera/gateway/docker/redshift_start.py /opt/stack/redshift_start.py",
        "RUN chmod 0700 /opt/stack \\",
        "    && mkdir -p /var/lib/alkera/redshift && chmod 0711 /var/lib/alkera/redshift",
    ),
    "snowflake": (
        "",
        "# Snowflake (fakesnow behind the audited TLS proxy, snowflake_start.py), as",
        "# Dockerfile.snowflake installs it - into a venv of its own over this",
        "# interpreter's packages, so fakesnow's pins (an older sqlglot, a newer",
        "# certifi) never replace the services' or the agent's. Root's alone, like",
        "# its key (/tls) and its data and upstream socket (/snowflake-data).",
        "COPY alkera/gateway/docker/snowflake.lock /opt/snowflake.lock",
        "RUN python -m venv --system-site-packages /opt/snowflake \\",
        f"    && /opt/snowflake/bin/{_PIP} /opt/snowflake.lock \\",
        "    && chmod 0700 /opt/snowflake",
        "COPY alkera/gateway/docker/snowflake_fidelity.py /opt/stack/snowflake_fidelity.py",
        "COPY alkera/gateway/docker/snowflake_start.py /opt/stack/snowflake_start.py",
        "RUN chmod 0700 /opt/stack \\",
        "    && mkdir -p /tls /snowflake-data && chmod 0700 /tls /snowflake-data",
    ),
    "databricks": (
        "# Databricks (hosted by the services): the trust directory they publish",
        "# the minted certificate's bundle to at start, holding certifi until then,",
        "# so REQUESTS_CA_BUNDLE and SSL_CERT_FILE always name a bundle that exists.",
        "RUN mkdir -p /etc/alkera/databricks-tls \\",
        "    && python -c \"import certifi, shutil; shutil.copyfile(certifi.where(), "
        "'/etc/alkera/databricks-tls/bundle.pem')\"",
    ),
}
PLUG_LAYERS: dict[str, tuple[str, ...]] = {
    POSTGRES_PLUG: (
        *POSTGRES_SERVER_LAYER,
        "",
        "# The postgres warehouse plug: its launcher (harbor/template/backends/",
        "# postgres_start.py) and compose.yml's own seed SQL beside it; its data,",
        "# socket and statement log are the server user's alone.",
        f"COPY alkera/harbor/backends/postgres_start.py {BACKEND_DIR}/postgres_start.py",
        f"COPY alkera/gateway/docker/postgres.sql {BACKEND_DIR}/postgres.sql",
        "RUN mkdir -p /var/lib/alkera/postgres \\",
        "    && chown postgres:postgres /var/lib/alkera/postgres \\",
        "    && chmod 0700 /var/lib/alkera/postgres",
    ),
}
SUPPORT_FILES: dict[str, tuple[tuple[str, str], ...]] = {
    "redshift": (("gateway/docker/redshift_start.py", "gateway/docker/redshift_start.py"),),
    "snowflake": tuple((f"gateway/docker/{name}", f"gateway/docker/{name}") for name in
                       ("snowflake.lock", "snowflake_fidelity.py", "snowflake_start.py")),
    POSTGRES_PLUG: (("harbor/template/backends/postgres_start.py",
                     "harbor/backends/postgres_start.py"),
                    ("gateway/docker/postgres.sql", "gateway/docker/postgres.sql")),
}
STATEMENT_LOGS: dict[str, tuple[dict, ...]] = {
    POSTGRES_PLUG: ({"log": "/var/lib/alkera/postgres/log/postgresql.log",
                     "tailer": "alkera_gateway.native.tailers.postgres"},),
}
