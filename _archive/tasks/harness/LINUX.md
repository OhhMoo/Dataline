# Running the harness on Linux

The harness was built and first measured on Windows with Docker Desktop. A lab runs it on
Linux. This page covers what differs and where each difference is handled. The measurement
itself (`--driver native --topology container`, the containment probes, the grader) is the
same on both.

## Setup

```bash
scripts/setup_linux.sh                  # packages, gateway/.venv, claude CLI, images, preflight
claude setup-token                      # once; prints a long-lived token
export CLAUDE_CODE_OAUTH_TOKEN=<token>  # or ANTHROPIC_API_KEY=...
gateway/.venv/bin/python tasks/harness/linux_preflight.py --live --model-smoke opus
scripts/run_decision_pass1.sh --k 1     # one rollout per decision arm, then --k 8
```

Supported: Fedora/RHEL (dnf) and Ubuntu/Debian (apt). Two engines:

| `--engine` | what you get | ownership of agent-written files |
|---|---|---|
| `docker` | Docker CE, rootful daemon, compose plugin | root inside → handed back to you at teardown |
| `podman` | rootless podman behind `podman-docker` + the compose v2 plugin, `DOCKER_HOST` at the user socket | yours already (container root = your uid) |

Compose ≥ 2.24 is required, because the compose files use `!override`. `podman-compose`
does not work, and the setup script does not use it.

## What Docker Desktop did that Linux does not

**1. SELinux (Fedora/RHEL).** An engine that labels containers refuses a bind of an
unlabelled host directory. That covers rootless podman and Fedora's `moby-engine`. When
`/sys/fs/selinux/enforce` is 1, `container_episode.selinux_overlay` adds one compose file
per project with `security_opt: [label=disable]` on every service. Compose lists the
services (`config --services`), so the harness keeps no list of its own. Host files are not
relabelled. The containment boundary is the mount set and the two `internal: true` networks,
and this option does not change either. `ALKERA_SELINUX_LABEL=keep` turns the overlay off.
The native topology writes the file into `<rollout>/container/compose.selinux.yml`, beside
the mounts overlay, as part of the record.

**2. File ownership.** Under a rootful engine the agent's writes land in the world as host
root. The grader, the fingerprint and `rmtree` then run on the host as you and fail on
them. Before `compose down`, both topologies run `container_episode.release_ownership`,
which executes `chown -R --reference=/audit` over `/workspace /world /audit` inside the
episode container. `/audit` is always a bind of your own directory, so its owner as seen
inside is the id that maps back to you: your uid when rootful, 0 when rootless. The same
command is correct for both engines.

**3. The subject's login.** The shell redirect gives the subject its own `HOME`, the
rollout's `shell/` directory. That is how `.bash_profile` reaches the hook, and it also
keeps your home untouched. On Windows the CLI reads its login from `USERPROFILE`. On Linux
it reads `$HOME/.claude/.credentials.json`, which the subject can no longer see.
Copying that file in is refused: it holds a refresh token, and a subject that rotates it
logs you out. So the login travels in the environment instead:
`CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`), `ANTHROPIC_API_KEY` or
`ANTHROPIC_AUTH_TOKEN`. Without one of them, `require_subject_auth` refuses the episode
before a world is built.

**4. Which shell the CLI spawns.** On Windows the CLI is pinned by
`CLAUDE_CODE_GIT_BASH_PATH`, which it ignores on Linux. There it spawns `$SHELL`. If your
login shell is zsh or fish, that shell never reads `BASH_ENV`, and the redirect would not
fire. The wrapper now also sets `SHELL` to the bash it verified.

**5. The model smoke.** A Linux host's `ls /` looks like the container's, so the Git Bash
markers say nothing there. The smoke also fails when the host's own hostname appears in the
model's output (`host_hostname_seen`).

**6. Parent sessions.** A harness started from inside a Claude Code session inherits
`CLAUDECODE`, the session id and the IPC socket. `container_episode.PARENT_SESSION_VARS` is
removed from every subject environment: the episode, the smoke and the quota probe.

## The boundary

`--driver native --topology container` now runs the model's CLI in its own container by
default (`--subject-sandbox container`). That container has no host mounts, no
capabilities and no route anywhere except an allowlisting proxy to the API, so dropping
the shell redirect lands the model in an empty jail, not on your host. The design and
what it does not claim are in `SANDBOX.md`. `--subject-sandbox host` restores the old
host-subject topology, which offers detection only.

## Checks

| command | needs | proves |
|---|---|---|
| `linux_preflight.py` | nothing running | engine, compose ≥ 2.24, SELinux handling, image current, venv, CLI, credential, disk |
| `test_subject_sandbox_pure.py` | nothing | the command bridge and the egress allowlist, on loopback |
| `linux_preflight.py --live` | engine, both images | GT001 stack with the sandbox, its proof, and every escape attempted |
| `linux_preflight.py --live --model-smoke opus` | + credential | the real CLI, inside the subject container, reaches the episode (one turn) |
| `run_rollouts.py --model scripted --k 1 …` | engine, both images | a full rollout and grade through the sandbox, no quota |
