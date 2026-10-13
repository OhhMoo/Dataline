"""The agent image's static environment, read from the template Dockerfile.

Some Harbor providers apply a Dockerfile's `ENV` at build time only (E2B
documents this), so a variable the audit depends on can be missing from the
agent's exec. The generator therefore repeats those variables in task.toml
`[environment.env]`, which every provider passes to every exec. The values are
PARSED from the environment template's Dockerfile, never retyped, so the image
and task.toml cannot drift; `checks.environment_env_problems` re-verifies it on
every generated task.

Mirrored: every `ENV` of the image EXCEPT `PATH` - the `ALKERA_*` audit
variables world-up checks before the agent starts, the service URLs
(`SCHED_URL`, `JIRA_URL` ...) the agent and the oracle discover the stack
through, `DBT_PROFILES_DIR`, ... `PATH` is never mirrored (`NOT_MIRRORED`).
"""
from __future__ import annotations

import re
import shlex

_ENV_LINE = re.compile(r"^\s*ENV\s+(.*)$", re.IGNORECASE)
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: Per-arm variables: task.toml carries them, the template Dockerfile must not.
#: `ALKERA_VIRTUAL_TIME` is the world's clock as the episode starts (what the
#: measured container overlay sets: container_episode.world_clock), so the
#: rows the agent's shims write carry the world's time; profiled tasks only,
#: so the unprofiled export is byte-identical (harbor/README.md).
PER_ARM_KEYS = ("ALKERA_TASK_VARIANT", "ALKERA_SHIM_WATCH", "ALKERA_VIRTUAL_TIME")
#: Image ENV that task.toml must NOT repeat. `PATH`: Harbor's docker provider
#: puts a Dockerfile-only task's [environment.env] into its persistent env
#: (harbor 0.23.0 environments/base.py:282-284) and merges that OVER the host's
#: os.environ for the `docker compose` process it runs ON THE HOST
#: (environments/docker/docker.py:617-674, `_compose_env_vars` used by
#: `_run_docker_compose_command`). A task PATH would therefore decide where the
#: host finds `docker` (Homebrew, snap and rootless installs break). The
#: shims-first PATH is the image's job (its ENV + /etc/profile.d/alkera.sh),
#: and world-up refuses to start when an exec lacks it, so a provider that
#: drops image ENV fails loudly instead of running the agent unaudited.
NOT_MIRRORED = ("PATH",)


class DockerEnvError(ValueError):
    pass


def selected(name: str) -> bool:
    """Which image ENV task.toml repeats: all of it but NOT_MIRRORED."""
    return bool(_NAME.match(name)) and name not in NOT_MIRRORED


def _instructions(dockerfile_text: str):
    """(line number, logical instruction) pairs: comment lines dropped,
    backslash continuations joined (so a RUN's continuation line that happens
    to start with `ENV` is never mistaken for an instruction)."""
    current: list[str] = []
    start = 0
    for number, raw in enumerate(dockerfile_text.splitlines(), start=1):
        line = raw.rstrip()
        if not current and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if not current:
            start = number
        if line.endswith("\\"):
            current.append(line[:-1])
            continue
        current.append(line)
        yield start, len(current) > 1, " ".join(part.strip() for part in current)
        current = []
    if current:
        yield start, len(current) > 1, " ".join(part.strip() for part in current)


def parse_env(dockerfile_text: str) -> dict[str, str]:
    """Every `ENV` of a Dockerfile, in order, the last assignment winning.

    Literal values only: a `$` (Docker would expand it at build time) or a
    multi-line ENV is refused, because the value task.toml repeats must be the
    value the image holds."""
    env: dict[str, str] = {}
    for number, multiline, instruction in _instructions(dockerfile_text):
        match = _ENV_LINE.match(instruction)
        if not match:
            continue
        body = match.group(1).strip()
        if multiline:
            raise DockerEnvError(f"Dockerfile:{number}: multi-line ENV is not supported")
        if "$" in body:
            raise DockerEnvError(f"Dockerfile:{number}: ENV with $-expansion is not "
                                 f"a literal value: {body!r}")
        try:
            words = shlex.split(body)
        except ValueError as exc:
            raise DockerEnvError(f"Dockerfile:{number}: {exc}") from None
        if words and "=" not in words[0]:                 # legacy `ENV KEY value`
            if len(words) < 2 or not _NAME.match(words[0]):
                raise DockerEnvError(f"Dockerfile:{number}: cannot parse {body!r}")
            env[words[0]] = " ".join(words[1:])
            continue
        for word in words:
            name, sep, value = word.partition("=")
            if not sep or not _NAME.match(name):
                raise DockerEnvError(f"Dockerfile:{number}: cannot parse {word!r}")
            env[name] = value
    return env


def static_agent_env(dockerfile_text: str) -> dict[str, str]:
    """Every ENV of the image but NOT_MIRRORED, in Dockerfile order.

    The image must still set the audited PATH itself (it is not mirrored):
    an `ENV PATH`, starting with the image's own ALKERA_SHIM_BIN when it
    declares one."""
    image = parse_env(dockerfile_text)
    path = image.get("PATH", "")
    if not path:
        raise DockerEnvError("the environment Dockerfile sets no ENV PATH; the "
                             "audited shims would not be first on the agent's PATH")
    shims = image.get("ALKERA_SHIM_BIN")
    if shims and path.split(":")[0] != shims:
        raise DockerEnvError(f"the environment Dockerfile's PATH starts with "
                             f"{path.split(':')[0]!r}, not its ALKERA_SHIM_BIN "
                             f"{shims!r}: the agent would bypass the audited shims")
    env = {k: v for k, v in image.items() if selected(k)}
    clash = sorted(set(env) & set(PER_ARM_KEYS))
    if clash:
        raise DockerEnvError(f"the environment Dockerfile sets per-arm variables "
                             f"{clash}; they belong to task.toml only")
    return env
