# The hard sandbox: `--subject-sandbox container`

**Default for `--driver native --topology container`.** Built 2026-09-25.

**Measured so far** (2026-09-25, Fedora 44, rootless podman 5.8, `alkera-subject:head` with
claude 2.1.283). The jail was stood up by hand, without compose: the three networks, the
subject with the compose file's hardening flags, the proxy, and a stand-in episode running
`rexec.py serve`.
- `SUBJECT_PROBE`, the script the harness runs every rollout: uid 1000, `CapEff` 0,
  `NoNewPrivs` 1, root read-only, no default route, no engine socket, no host path, no
  direct egress to 1.1.1.1:443 or 8.8.8.8:53, no public DNS, proxy 403 for example.com and
  200 for api.anthropic.com, bridge reachable, services unreachable. Every check held.
- Redirect: `bash -c` and `bash -lc` inside the subject ran in the stand-in episode, and the
  exit status came back.
- Escapes with the redirect dropped: root write refused (read-only filesystem); curl to
  example.com got 403 through the proxy and could not resolve around it; `su` refused; no
  engine socket. api.anthropic.com was reachable (401 without a key), and the proxy log
  recorded every attempt.
- `test_subject_sandbox_pure.py`: 12/12.

**Not yet measured:** the full compose stack with the real episode image
(`test_subject_sandbox.py`), a model turn (`--model-smoke`), and Docker CE. Until
`gateway/docker/evidence/subject-sandbox.json` exists, those are *designed*.

## Why

Before this, the model's CLI ran on the host, and its bash was redirected into the episode
container through `BASH_ENV`. A redirect is not a boundary. `env -u BASH_ENV bash -c …` put
the model back on the host, with the operator's uid and `bypassPermissions`, one `..` from
the grader, the seed spec and the operator's credentials. `blindness.py` caught it after the
fact. Nothing stopped it, and nothing would stop a model that did real damage before it was
caught. A result the labs can use needs an enforced boundary, not detection alone.

## Shape

```
 host (harness only; no model process)
   │ docker exec -i subject claude --print …        stream-json on stdout, as before
   ▼
┌────────────── subject ──────────────┐   api_net (internal)   ┌── egress-proxy ──┐  outbound_net
│ the claude CLI, uid 1000            │ ─────────────────────▶ │ CONNECT allowlist│ ─────────────▶ api.anthropic.com:443
│ read-only root, cap_drop ALL,       │                        │ logs every conn  │
│ no-new-privileges, pids/mem limits  │                        └──────────────────┘
│ tmpfs home + /tmp, no host mounts   │
│ bash → BASH_ENV → rexec.py client   │
└──────────────┬──────────────────────┘
               │ subject_net (internal; two members)
               ▼ rexec.py serve :7070, token-gated
┌────────────── episode ──────────────┐  episode_net (internal)
│ unchanged: /workspace, /world,      │ ─────────▶ postgres.internal, jira.internal, …
│ /audit, the shims, the toolbelt     │
└─────────────────────────────────────┘
```

The only network with a default route is `outbound_net`, and only the proxy is on it.

## What each property rests on

| property | enforced by | measured by (`subject_sandbox_probe`, every rollout) |
|---|---|---|
| no host filesystem | no bind mounts on `subject`; tmpfs only | host paths (world, grading, rollout dir, repo, `$HOME`) do not exist inside |
| no engine | no socket mounted | none of `ENGINE_SOCKETS` exists |
| no privilege | `user: 1000`, `cap_drop: [ALL]`, `no-new-privileges`, `read_only` | uid ≠ 0, `CapEff` = 0, `NoNewPrivs` = 1, root not writable |
| no egress but the API | internal networks only; `HTTPS_PROXY` → allowlist | no default route; direct TCP to 1.1.1.1/8.8.8.8 fails; public DNS fails; proxy returns 403 for example.com and 200 for the API |
| commands land in the episode | `BASH_ENV` + `/etc/profile.d` hook → rexec | `bash -c hostname` and `bash -lc hostname` return the episode's hostname |
| the subject is not a side door into the services | `subject` is not on `episode_net` | `postgres.internal` and `jira.internal` are unreachable from the subject |

`rollout.py` refuses to hand over the terminal unless every row holds
(`container_execution_proof.verified` requires `sandbox.verified`). The result is saved on
the record as `subject_sandbox`. At teardown, the proxy's log is saved as
`<rollout>/container/egress.jsonl`.

## What an escape now gets you

`test_subject_sandbox.py` plays this case. Drop the redirect, and you have a bash in
`subject`: an empty tmpfs home, the CLI binary, and a proxy that only speaks to the API.
Every other escape it tries must fail: host paths, root writes, direct egress, going around
the proxy, the services, the engine socket, and `sudo`/`su`.

## What is not claimed

- **The credential is inside the jail.** The CLI needs it, so an escaped subject can read it
  from its parent's environment and spend it, but only against the allowlisted API. It is
  never on disk and never in a recorded argv (`docker exec -e NAME` carries it by name).
  Use a token scoped to this work (`claude setup-token`) and rotate it after a run you
  distrust.
- **The episode container is unchanged.** The agent is still root inside it, as a data
  engineer's box is. Its boundary is the one containment P7 already measures (mounts, no
  egress), and a new internal interface does not add a route.
- **The kernel is shared.** This is container isolation, not a VM. For a stronger claim, run
  the engine under gVisor (`--runtime=runsc`) or in a Kata/Firecracker VM. The compose files
  need no change for that.
- **Only the native driver is covered.** Under `--driver mcp` the model's only tools are the
  gateway's, so there is no shell to escape with. Its CLI still runs on the host with no
  tools.
- `--subject-sandbox host` keeps the old topology for comparison and for hosts without the
  subject image.

## Fidelity

The model sees the same thing as before. Each command runs in the episode at
`/workspace`, with no state between commands. The transcript and the audit rows are
unchanged, and the prompt is byte-identical. Three differences:

- `rexec` streams stdout and stderr separately, where `docker exec` did the same.
- A backgrounded child no longer holds the turn open past 2 s.
- The CLI's own files live in the subject's tmpfs, not in a rollout directory on the host.

## Files

`gateway/docker/Dockerfile.subject`, `gateway/docker/subject/{rexec.py,egress_proxy.py,bash_env.sh}`,
`gateway/docker/compose.subject.yml`, and the "hard sandbox" section of
`tasks/harness/container_episode.py`. Tests: `test_subject_sandbox_pure.py` (no engine) and
`test_subject_sandbox.py` (live, `--model-smoke`).

Knobs: `ALKERA_EGRESS_ALLOW` (comma list of `host:port`, default `api.anthropic.com:443`),
`--subject-image`, and `--subject-sandbox host|container`.
