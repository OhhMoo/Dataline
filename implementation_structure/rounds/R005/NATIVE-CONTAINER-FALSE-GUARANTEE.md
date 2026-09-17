# Native-container false guarantee

Recorded 2026-09-17 from the stopped campaigns. Draft only — no harness
edit in this note.

The claim that failed: a native + container rollout records
`blindness_guarantee: filesystem_isolated` and therefore that the
subject's Bash never saw the host. The first real-model run of that
topology (`native-container-20260917-k5`) recorded the claim while the
real `claude` process ran Git Bash on Windows.

Sources: `STATUS.md` §4.1, `agent.md` §12 ("Container topology, first
model run"), the two campaign trees below, and the comments now in
`tasks/harness/container_episode.py` / `rollout.py` / `blindness.py`
(committed at `6ee7db9`). Those comments cite measurements that are
**not on disk**.

## What happened

The harness started a real Docker episode stack (`alkera-native:sched`,
`sleep infinity`, healthy). Containment probes ran *inside* that
container via `python -` on stdin and correctly listed a Linux root
(`.dockerenv`, `/workspace`, `/world`). The record then set
`topology: container` and `blindness_guarantee: filesystem_isolated`.

The subject was the host CLI:

- `agent_argv.json`: `C:\Users\OhhMoo\.local\bin\claude.EXE --tools Bash`
- `native_env.json`: `CLAUDE_CODE_GIT_BASH_PATH` pointed at
  `…\rN\shell\bash.cmd`, a `@echo off` trampoline into
  `container_bash.py` (the wrapper that only accepts `-c COMMAND` and
  `docker exec`s it)
- Transcript `system/init.cwd` is the host
  `…\native-container-20260917-k5\rN\agent_cwd`

The CLI **ignored** a `CLAUDE_CODE_GIT_BASH_PATH` that does not end in
`.exe`. The wrapper was never spawned. Bash was Git for Windows. The
episode container sat idle while the model used the host Docker CLI as
a tool.

That is why a scripted acceptance (the wrapper invoked as
`bash -c …`) is not a model acceptance — the third time this project
has paid for that distinction (`agent.md` §12).

## Proof on disk

### GT001 `native-container-20260917-k5`

Completed records: `r0\rollout.json`, `r1\rollout.json`. Both:

- `driver: native`, `topology: container`,
  `blindness_guarantee: filesystem_isolated`
- `containment.contained: true`, `containment.root_listing` is a Linux
  episode root (probe, not the model)
- `scored: false`, `classification: blindness_violation`,
  `canary_leak` — the model listed the host rollout directory
- `surfaces.fs.result_summary` already names a host
  `C:\Users\OhhMoo\AppData\Local\Temp\alkera-rollouts\…` path

`r2` and `r3` have transcripts and wrappers, no `rollout.json`
(campaign stopped).

Transcript evidence (model Bash, first turns):

| episode | command | result |
|---|---|---|
| r0 L5 / r1 L4 / r2 L4 / r3 L4 | `env \| sort` | Windows: `ALLUSERSPROFILE=C:\ProgramData`, `APPDATA=…`, `CLAUDE_CODE_EXECPATH=C:\Users\OhhMoo\.local\bin\claude.exe` |
| r1 L5, r2 L5, r3 L5, GT006 r0/r1 L4 | `ls /workspace` | `ls: cannot access '/workspace': No such file or directory` |
| r1 L20–L21 | `pwd; ls -la /` | cwd = host `…/r1/agent_cwd`. `/` is the **Git Bash root**: `bin`, `cmd`, `dev`, `etc`, `git-bash.exe`, `mingw64`, `LICENSE.txt`, `ReleaseNotes.html` — not `.dockerenv` / `workspace` / `world` |
| r0 L14–L15, r1 L26, r2 L22 | `docker ps` | Host Docker: idle episode containers `alkera-nc-native-container-20260917-k5-r0-episode-1` (`051615d41959`) and `…-r1-episode-1` (`7c68cb770a04`), plus the sibling GT006 stack |
| r0 L22–L23 | `ls` / `cat` the wrapper | Model reads `shell\bash.cmd` and `container_bash.py` on the host rollout tree |
| r0 L34+, r1 L31+, r2 L29+, r3 L28+ | `docker exec <id> bash -c '…'` | Model *enters* the container itself after discovering it from the host |

r0 and r1 then did real warehouse work (both graded 17/19 before
quarantine) from that hybrid: host shell + ad-hoc `docker exec`.
Blindness still failed because the host rollout dir, canaries, and
sibling episodes were readable.

### GT006 `native-container-20260917-k5`

No `rollout.json` (stopped earlier). `r0` and `r1` transcripts repeat
the same first-turn pattern: `ls /workspace` missing, Windows `env`,
`docker ps`, then the model writes a host helper
(`/c/Users/OhhMoo/bin/dx` / `dex`) that wraps `docker exec`. Same
defect, second task.

## What the committed code already does (`6ee7db9`)

Read-only; not applied here.

`container_episode.write_bash_wrapper` no longer ships a `.cmd` as
`CLAUDE_CODE_GIT_BASH_PATH`. It points the CLI at a real `bash.exe`
and arms a `BASH_ENV` / `.bash_profile` hook that `exec docker exec`s
`$BASH_EXECUTION_STRING` into the episode. `verify_container_execution`
runs `bash -lc hostname` under that subject env and compares it to
`docker exec <container> hostname`. `rollout.py` raises
`HarnessError` and does not start the episode if `verified` is false.
`blindness.guarantee` will not emit `filesystem_isolated` for native +
container without that proof; the fallback label is
`detection_only+unverified_container`.

The comments that document the three CLI configurations cite
`gateway/docker/evidence/shell-redirect-*.json`. That directory exists
and holds containment / image / toolbelt JSON. **There is no
`shell-redirect-*.json`.** The measurement the comments narrate is
not reproducible from the tree.

The hook is also not a boundary. A subject that runs
`bash --noprofile --norc -c …`, or unsets `BASH_ENV`, is back on the
host. The committed proof is a harness-side `hostname` through the
same binary, not a model turn.

## Required fix (draft; do not apply)

Two missing pieces, both required before any native-container number
is valid.

### 1. One-turn *model* smoke inside the episode

A harness `hostname` is necessary and not sufficient. The failure mode
was "the CLI ignores our wrapper and the record never notices." The
acceptance that would have caught `native-container-20260917-k5` is
the one the comments already describe:

- Start a real native + container episode with the production subject
  (`claude --print --tools Bash`, same argv the rollouts use).
- First user turn, nothing else: `hostname; ls /`.
- Persist the raw tool_result (the missing
  `gateway/docker/evidence/shell-redirect-*.json` shape).
- Pass only if **both** hold:
  - `hostname` equals `docker exec <episode> hostname` (Linux
    container id / name, not `OhhMooZephyrus` / the Windows host).
  - `ls /` is an episode root: `.dockerenv`, `workspace`, `world`,
    `audit` — and is **not** a Git Bash root (`bin`, `cmd`, `mingw64`,
    `git-bash.exe`).
- Fail closed if the model instead prints Windows `env`, lists
  `C:\Users\…`, or reaches `docker ps` on the host.

This is a one-turn smoke, not a scored task. It must use the real
CLI. A scripted `container_bash.py -c hostname` must not be allowed
to stand in for it.

### 2. Refuse to *record* `filesystem_isolated` without that proof

`rollout.py` already refuses to *start* without
`container_execution_proof.verified`. Keep that. Add a second gate
on the way out, because a record that claims the strong guarantee
after a start-time proof that later diverged (or after a regrade that
lacks the field) is how this bug ships:

- `blindness.guarantee` (already) returns
  `filesystem_isolated` only when topology is container, containment
  probes are clean, **and** `container_execution.verified` is true.
  A regrade of a pre-fix record must not be able to mint the strong
  label by omitting the field.
- The written `rollout.json` must carry the proof object
  (`how`, `expected`, `observed`, `verified`) and, once the model
  smoke exists, a pointer to that evidence file. No proof, no
  `filesystem_isolated` — write
  `detection_only+unverified_container` (or refuse the run).
- Do not treat containment-probe `root_listing` as substitute
  evidence. That listing is taken by `docker exec python -` and was
  already green on the false-guarantee episodes.

Until (1) is on disk and (2) is the only path that can write the
label, every native-container Pass@1 stays withheld. Host-topology
native numbers stay caveated (`STATUS.md` §3.3 / §4.1).
