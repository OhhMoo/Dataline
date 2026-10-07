"""The backends a profile can plug into the container topology, one row each.

A BACKEND is a plug the agent reaches over the network with a vendor's own
client - a warehouse (Snowflake today) or a control plane - as opposed to a
file in the world or a module the MCP tools call. Each row says everything the
harness needs to stand one up for a rollout and to judge what the agent did
with it:

  where it answers     `host`:`port` over `scheme`, the `<name>.internal` alias
                       on episode_net (`endpoint`);
  how it runs          `hosted` - a module inside native-services - or its own
                       container built from `dockerfile` on the episode image;
  what the box needs   `clients` - requirements-<clients>.lock, installed by
                       the Dockerfile and suffixed onto the image tag
                       (gateway/docker/build_episode.py) - and `env`, the
                       agent-facing variables a profile that plugs it adds;
  what it is given     `seeds_world` - the world's warehouse FILES, read-only
                       at /seed - and `tls`, a certificate minted at boot whose
                       public bundle (certifi + cert) is published at
                       /etc/alkera/<name>-tls/bundle.pem;
  what it writes       one 9.2.2 row per statement as `statement_tool`, and
                       its accepted connections into `destinations_file`,
                       which the harness folds into destinations.jsonl after
                       teardown.

Nothing here runs anything. `tasks/harness/container_episode.py` reads the
rows to write the per-rollout overlay, `tasks/harness/agent_under_test.py` to
build the endpoint map an episode is judged against, and `tasks/harness/
path.py` to count a statement tool as SQL. A new backend is a new row plus its
own files; the shared code loops over `plugged(...)`.

AT MOST ONE TLS BACKEND PER PROFILE. The episode trusts exactly one bundle
(`REQUESTS_CA_BUNDLE` is one path), so a profile that plugs two TLS backends
would quietly trust one of them and fail the other's handshake mid-episode.
`plugged` refuses it up front, naming both.

Dependency-free, like `plugs.py`: the harness, the image build script and the
world-side tests import it without the gateway's runtime.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from .plugs import ProfileError

Scheme = Literal["https", "http", "postgres"]

#: The obviously fake credential every native surface shares
#: (`agent_under_test.NATIVE_FAKE_CREDENTIAL`, `native_workspace.FAKE_TOKEN`).
FAKE_CREDENTIAL = "alkera-local-dev"


@dataclass(frozen=True)
class Backend:
    name: str                      # plug name == compose service == compose profile
    host: str = ""                 # "<name>.internal" when left empty
    port: int = 0
    scheme: Scheme = "http"
    hosted: bool = True            # True: a module inside native-services; False: its own container
    dockerfile: str | None = None  # "Dockerfile.<name>" when not hosted
    clients: str | None = None     # requirements-<clients>.lock and image-tag suffix, or None
    seeds_world: bool = False      # world warehouse files mounted read-only at /seed
    tls: bool = False              # mints a cert, publishes /etc/alkera/<name>-tls/bundle.pem
    statement_tool: str | None = None   # e.g. "redshift.statement"
    env: Mapping[str, str] = field(default_factory=dict)  # agent-facing, added only when plugged

    def __post_init__(self) -> None:
        if not self.host:
            object.__setattr__(self, "host", f"{self.name}.internal")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


BACKENDS: dict[str, Backend] = {
    # fakesnow behind the TLS proxy (gateway/docker/snowflake_start.py), its own
    # container from Dockerfile.snowflake. Its compose stanza predates this
    # registry (compose.yml + compose.native.yml), and its variables are in the
    # unprofiled static table - both kept byte for byte.
    "snowflake": Backend(
        name="snowflake",
        host="snowflake.internal",
        port=8443,
        scheme="https",
        hosted=False,
        dockerfile="Dockerfile.snowflake",
        clients="snowflake",
        seeds_world=True,
        tls=True,
        statement_tool="snowflake.statement",
        env={"SNOWFLAKE_HOST": "snowflake.internal",
             "SNOWFLAKE_ACCOUNT": "acme",
             "SNOWFLAKE_USER": FAKE_CREDENTIAL,
             "SNOWFLAKE_PASSWORD": FAKE_CREDENTIAL},
    ),
}


def plugged(slots: Mapping[str, Sequence[str]]) -> tuple[Backend, ...]:
    """The backends a profile's plugged `slots` name, in registry order.

    Raises `ProfileError` naming every TLS backend when more than one is
    plugged: the episode has one CA bundle, and trusting one of two is a
    handshake failure the agent would meet half way through its work.
    """
    named = {plug for plugs in (slots or {}).values() for plug in plugs}
    found = tuple(b for name, b in BACKENDS.items() if name in named)
    tls = [b.name for b in found if b.tls]
    if len(tls) > 1:
        raise ProfileError(
            f"a profile plugs at most one TLS backend, and this one plugs "
            f"{len(tls)}: {', '.join(repr(n) for n in tls)} - the episode trusts "
            f"one CA bundle (REQUESTS_CA_BUNDLE)")
    return found


def endpoint(b: Backend) -> str:
    return f"{b.scheme}://{b.host}:{b.port}"


def destinations_file(b: Backend) -> str:
    """The backend's own destination log in the rollout's audit directory.

    Its own file because native-services rewrites destinations.jsonl whole on
    every accept; `container_episode.NativeContainerEpisode.fold_destinations`
    merges it in after teardown."""
    return f"destinations.{b.name}.jsonl"


def trust_dir(b: Backend) -> str:
    """Where a TLS backend publishes its public bundle (`native.tls.mint`)."""
    return f"/etc/alkera/{b.name}-tls"


def bundle(b: Backend) -> str:
    """The CA bundle an episode that plugs `b` points REQUESTS_CA_BUNDLE at."""
    return f"{trust_dir(b)}/bundle.pem"
