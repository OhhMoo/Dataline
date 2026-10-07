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

FENCES (loud, by name). A profile whose warehouse plug the Harbor image does
not serve yet is refused (plan Task 10 adds those backends as `episode.json`
`backends`); nothing would answer at its address. The notebook plug (Jupyter)
is not served either: it is allowed, because the BI tasks plug it without
using it, and listed in harbor/README.md's known deviations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import harness
from .constants import AGENT_HOME

#: The template ENV names that are the image's own, not the box's discovery
#: environment: kept verbatim in every variant.
KEPT_ENV_PREFIXES = ("ALKERA_",)
KEPT_ENV_NAMES = ("PATH",)
#: The bundle every image ships (the template's RUN copies certifi there).
CA_BUNDLE_VAR = "REQUESTS_CA_BUNDLE"
#: Warehouse plugs the Harbor image serves today: the world's own DuckDB file.
SERVED_WAREHOUSES = ("duckdb",)
#: Plugs that are not served in the image but do not block an export; each is
#: a known deviation (harbor/README.md).
UNSERVED_ALLOWED = {("notebook", "jupyter"): "jupyter.internal:8888 is not started"}
#: Roles whose box installs a role lock (gateway/docker/Dockerfile's
#: ALKERA_TOOLBELT): `de` installs nothing beyond the gateway's locks.
ROLE_LOCKS = ("ds", "da")
#: Roles whose box carries the audited Jupyter kernelspec.
KERNEL_ROLES = ("ds", "da")
#: Plan Task 10's two extension points, empty today (no profile the image
#: serves plugs a registry backend):
#:   HARBOR_BACKENDS  registry backend name -> its episode.json `backends`
#:                    entries for a variant (name, argv, user, ports, hosts,
#:                    health; the environment runtime's contract)
#:   CLIENT_LAYERS    client lock name -> extra Dockerfile lines after that
#:                    client's lock (a server package, a pinned binary)
HARBOR_BACKENDS: dict = {}
CLIENT_LAYERS: dict[str, tuple[str, ...]] = {}

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
    unserved = [f"warehouse={name}" for name in slots.get("warehouse", ())
                if name not in SERVED_WAREHOUSES]
    if unserved:
        raise VariantError(
            f"profile {profile!r} plugs {', '.join(unserved)}, which the Harbor image "
            f"does not serve yet (only {', '.join(SERVED_WAREHOUSES)}): nothing would "
            f"answer at its address. Backends land as episode.json `backends` "
            f"(plan Task 10).")
    role = profile.split("/", 1)[0]
    try:
        plugged = plugged_backends(slots)
    except ValueError as exc:                    # plugs.ProfileError: two TLS backends
        raise VariantError(f"profile {profile!r}: {exc}") from None
    return Variant(role=role,
                   clients=tuple(sorted({b.clients for b in plugged if b.clients})),
                   profile=profile, plugs=_canonical(plugs), slots=_canonical(slots),
                   services=tuple(resolved.services),
                   backends=tuple(b.name for b in plugged))


def backend_specs(variant: Variant) -> list[dict]:
    """The episode.json `backends` of a variant: HARBOR_BACKENDS' entries
    for each registry backend it plugs, in plugged order. Empty today."""
    specs: list[dict] = []
    for name in variant.backends:
        if name not in HARBOR_BACKENDS:
            raise VariantError(f"the {variant.key} variant plugs backend {name!r}, which "
                               f"the Harbor image has no process for (plan Task 10)")
        specs += [dict(spec) for spec in HARBOR_BACKENDS[name](variant)]
    return specs


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
    source of the bundle path, so the two cannot drift."""
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
                   f"RUN {_PIP} {lock}",
                   *(CLIENT_LAYERS.get(name, ()) if kind == "clients" else ())]
    if variant.role in KERNEL_ROLES:
        insert += ["",
                   f"# Role {variant.role}: the Jupyter plug's audited kernelspec, "
                   f"system-wide (gateway/docker/Dockerfile).",
                   "RUN python -c \"from alkera_gateway.native.notebook import "
                   "install_kernelspec; install_kernelspec('/usr/local/share/jupyter', "
                   "interpreter='/usr/local/bin/python')\""]
    lines[at + 1:at + 1] = insert

    _header(lines, variant)
    return "\n".join(lines)


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
