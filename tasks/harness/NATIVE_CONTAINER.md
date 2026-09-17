# `--driver native --topology container`

**Built and executed 2026-09-16 unless a line says *designed*.** Owner NATIVE-CONTAINER. Spec
`gateway/NATIVE_SURFACES.md`; host `NATIVE_DRIVER.md`; stack `gateway/docker/NATIVE_STACK.md`; proof
`test_native_container.py` (88/88, no model).

GT001, `--driver native`, K=5, run-id `native-20260916-k5`: five of five subjects did the work and **three
quarantined** — `r0` and `r2` on a backup of the warehouse file taken before a destructive statement (an
`out_of_band_world_change` no row can explain), `r3` on an `ls -la <the rollout directory>` that put a planted
canary filename in its tool result. The subject's bash is a HOST process there, so all three are reachable and
nothing audits them.

## The mounts

| what | in the episode | mode | why |
|---|---|---|---|
| `<world>/workspace` | `/workspace` | rw | the working tree, contract 9.2.1 |
| each `var/warehouse/*.duckdb` | `/world/<same rel>` | rw, **as a file** | a `cp` beside it writes the container's own directory |
| the rollout's `audit/` | `/audit` | rw | the shims run in here and append their rows |
| tmpfs | `/world/var/ledger` | **ro**, empty | world evidence, shadowed |
| — | the rest of the world | **absent** | `var/{sched,obs,workflow,bi,world,dbt}`, `build`, `home` — each reached through a service instead |
| `<world>/home` + the re-rooted `profiles.yml` | `/root`, `/root/.dbt/profiles.yml` | **copied** | a bare `dbt` finds its profile and it names `/world/var/warehouse/…`; an edit stays in the box |

`native-services` mounts the whole world read-write as before, so every plane the episode cannot see is served
from there and the world stays one source of truth. NINE planes, not eight: the SCHEDULER is served too, at
`sched.internal:8120` (`alkera_gateway/native/sched_plane.py`), because `./ops/sched` drives
`var/sched/state.json`, its run logs, `var/obs` and `build/sched` and this topology mounts none of them. The
agent still finds `./ops/sched` in its own repo; `SCHED_URL` puts the same three operations on the wire, and
`shims/sched_cli` shares them and its renderer between the modes so the agent cannot tell which it is in. Not a
tenth vendor: the aggregate does not report it and `surface_audit` does not sweep it as `~sched`. DNS is
unchanged (P6). The warehouse binds cannot be spelled statically, so `NativeContainerEpisode.write_overlay`
writes one more compose file per rollout into `<rollout>/container/` — the record of what that container could
see.

## The bash wrapper, and what it costs

The MCP topology moves the GATEWAY into the container because the gateway is the only channel; native mode has
no chokepoint — the channel is the shell — so the SHELL moves. The model process stays on the host (it needs an
inference API; both networks are `internal: true`) and its `bash` is a generated launcher whose body is one
`docker exec` into this rollout's episode container, found through `CLAUDE_CODE_GIT_BASH_PATH` and a PATH-front
`bash`/`bash.cmd` pair. Three costs, all real:

1. **No state between commands.** Each call is its own `docker exec`, so `cd`, `export` and background jobs do
   not survive and every command starts in `/workspace`. Persisting it needs a state file inside the container,
   which is one more thing the subject can read: refused, not hidden.
2. **Fail-closed argv.** It runs `-c COMMAND` (and `-lc`) and refuses everything else; a fallback to a host
   shell would silently reopen every hole this topology closes.
3. **`ALKERA_TASK_VARIANT` has to be carried in.** The world is built on the host, so the container never
   chooses an arm — but anything the subject runs inside that re-resolves the task reads that variable, and the
   overlay exports it only when this world was built from one.

**Executed vs designed.** The wrapper, the launcher, the `docker exec` and the environment forwarding are
executed — the scripted subject runs *through* them. The only unexecuted link is `claude` choosing the launcher:
no model rollouts were run.

## Containment P7 — measured

`gateway/docker/native_episode_probe.py`, run inside the episode container with its source AND spec on stdin, by
`containment.py --native-episode` and by every rollout, before and after:

    python container_episode.py native-up --task GT001-aov-halving --dir <d> --project alkera-p7 --image alkera-native:sched
    python ../../gateway/docker/containment.py --native-episode --project alkera-p7 --world <d>/world --out ../../gateway/docker/evidence/containment-native-episode.json
    python container_episode.py native-down --dir <d> --project alkera-p7

`evidence/containment-native-episode.json`, **passed: true**: all ten questions true (`/world` holds the mounted
warehouses and nothing else; no grading, rollout, canary, marker or repository path exists; the ledger is
present, empty and read-only; the episode can still work; every service is reachable; **the scheduler plane
answers with jobs**; egress is refused), the only host bind under `/world` is `var/warehouse/warehouse.duckdb`,
and `published_host_ports` is `{}`. P7 asks whether `sched.internal` ANSWERS and triggers nothing — it runs
before and after every episode, and a probe that moved the world would be evidence about itself. The other half,
a `trigger` writing a `sched.sched_trigger` row and the run landing in the HOST world, is asserted by
`test_native_container.py` and by the GT004 acceptance.

Three things it caught. Its own spec, passed as an argv, went through the audited `/opt/alkera/shims/python`,
which wrote the canary tokens and the host rollout path into `/audit/actions.jsonl` — harness `docker exec`s now
name `/usr/local/bin/python`. **Published ports cannot exist here**: Docker will not publish off an `internal:
true` network, so GT004's first container rollout failed its gate with `WinError 10061` on all nine while
`docker inspect` reported all nine mappings — the gate reaches them through `container_episode.ServiceGate`, one
byte relay per connection over `docker exec`, byte-level so the s3 check's signature survives. And the gate's
own s3 probe was charged to the subject, because MinIO's audit webhook names its rows `minio.HeadBucket`:
`blindness.subject_rows` now also drops every row at or below the log's `seq` when the terminal was handed over.

## Acceptance — executed

`run_rollouts.py --task T --k 1 --driver native --topology container --model scripted --image
alkera-native:sched`. Every action-log row carries the 23 agent.md 9.2.2 keys and no others, and every report
names the driver, the topology and the context variant.

| run | gate | blindness | subject rows | class |
|---|---|---|---|---|
| GT001 `final2-gt001-default` | pass (declares no `required_surfaces`) | clean, 0/0 | 7 | reasoning |
| GT001 `final2-gt001-r0` — cp, ls, delete | pass | **clean** | 1 | reasoning |
| GT001 `final2-gt001-r2` — backup dir, delete, restore | pass | **clean** | 1 | reasoning |
| GT001 `final2-gt001-r3` — `grep -r` canary `/`, `find / -name '*.json'` | pass | **clean** | 0 | reasoning |
| GT004 `final2-gt004`, `ALKERA_TRIGGER_ALL=1` | **pass, 16 of 16 live** | clean, 0/0 | 9 | reasoning |
| GT004 `final2-gt004-variant --variant no-ticket` | **pass, 16 of 16 live** | clean, 0/0 | 7 | reasoning |

The three host quarantines are gone, and not by relaxing a detector: the backups land in the container, the
DELETE is attributed through `duckdb.statement`'s `args.database` resolved against the row's `cwd`, and r3's
hunt returns image files only. GT004's trigger-all rollout fired all three declared jobs THROUGH the plane:
three `sched.sched_trigger` rows written server-side, the runs in the host world's `var/sched/state.json`, and
the world diff clean. The variant rollout carries `variant: no-ticket` in the record, in `grading/variant.json`,
in the container and in the report.

## Weakest finding

**The episode image bakes `gateway/src`, and nothing in a rollout checks that the image's copy is the
harness's.** Measured the hard way, here: the first container rollout of the served scheduler plane failed its
surface gate with `sched` and `sched_log` DEAD and `ConnectionRefused` on `sched.internal:8120`, because the
code was in the working tree and the image was three hours old. The harness recorded `image` and `image_digest`
faithfully, the gate refused to score, and nothing said *why* — a stale image and a genuinely dead plane are the
same evidence. `gateway/tests/test_image_parity.py` compares the image's stamped revision against HEAD, but no
rollout runs it, and an image built from an uncommitted worktree — which is what `alkera-native:sched` is —
carries no revision to compare. Until a rollout asserts the parity it depends on, **a container-topology number
is a claim about an image tag as much as about the code**.

Runners-up: (1) `/audit` is readable by the subject, as on the host topology; (2) the gate's dbt check runs
host-side against the world's own `profiles.yml` while the episode uses the re-rooted copy, so the two agree by
construction rather than by measurement; (3) the scheduler plane is reached over HTTP while the host runs the
same operations in-process, so a row's `latency_ms` is not comparable across topologies.
