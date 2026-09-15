# WORKSPACE — the episode moves into the container, and then gets a terminal

**Agent WORKSPACE, 2026-09-14.** The integration checkpoint in `3_track.md` §6, and the
contradiction B2B4 named in its own §5: *"my containment evidence describes a topology
nobody runs episodes in."*

Deliverables: `tasks/harness/container_episode.py`, `tasks/harness/container_shim.py`,
`tasks/harness/container_check.py`, `gateway/docker/compose.episode.yml`, plus the
`--topology container` path through `rollout.py` / `agent_under_test.py` / `blindness.py`
and the evidence those write.

Everything marked **[V]** I executed on this machine today and read the output of.
Everything marked **[R]** is a claim I read and did not execute. Environment for all
**[V]**: Windows 11 Pro for Workstations 10.0.26200, Docker Desktop server **29.5.2**,
`gateway/.venv` Python 3.11, episode image `alkera-episode:head` =
`sha256:55c394dc3a25c619876be6d5265114ae4ca049582104cf5374207cc5d9e90733`.

---

## 0. The answer, first

**Yes — an episode now runs contained, and the terminal shipped after it, not before.**

| | before | after |
|---|---|---|
| where the world, gateway and audit log run | a host subprocess, `/grading` one `..` away | the `episode` service of `gateway/docker/compose.yml` |
| the agent's terminal | none | `mcp__alkera__bash`, a real shell in the container |
| blindness guarantee | `tool_surface_revoked` | **`filesystem_isolated`**, measured per rollout |
| `cat /grading/manifest.yaml` from inside | would have worked | `No such file or directory` **[V]** |
| `reference_copy` baseline | 17.0 / 19 in fixtures | **0.0 / 19 contained** **[V]** |
| GT001 Pass@1 (opus) | 1.0, K=5, 5 scored | 1.0, K=5, **3 scored** (2 killed by the spend limit) |

The ordering was the assignment and it is the part worth defending. A shell on the host
is `cat ../grading/manifest.yaml` and ground rule 1 is gone — along with the meaning of
every number measured after it. So the container came first, the probe that proves the
container came second, and `bash` came third. Nothing in this report would be worth
reading in the other order.

---

## 1. What runs where, and why the subject stayed outside

The subject process is a model CLI and needs an inference API. Both compose networks are
`internal: true` and have no route off themselves — that is the property the whole design
rests on, so it cannot be relaxed for the subject's benefit. The resolution is the same
one Polymath's Horizon-SWE uses: **the agent loop is outside, the workspace is inside.**

```
host                                   |  container `episode`  (episode_net + backend_net,
                                       |                        both internal: true)
claude --print   (needs the API)       |  /world      <- bind mount of <rollout>/world
  |                                    |  /workspace  <- the same directory, at the 9.2.1 path
  |  MCP over stdio, via `docker exec -i`  |  /audit      <- bind mount of <rollout>/audit
  v                                    |  the gateway (container_shim serve)
<rollout>/world    bind mounted        |  bash, git, dbt, python
<rollout>/audit    bind mounted        |
<rollout>/grading  NOT MOUNTED         |  NO /grading.  NO egress.  No host filesystem.
```

Three choices are load-bearing:

- **The grading zone is not mounted.** It is a sibling directory of the world root on the
  host, and a bind mount exposes one directory, not its parent. Inside the container the
  zone is not a denied path, it is an **absent** one — which is a much stronger statement
  and a much cheaper one to verify.
- **stdio over `docker exec`, not a published port.** `published_host_ports: {}` is a pass
  condition of B2B4's containment evidence; a port would be a second way into the episode
  zone. stdio needs none.
- **Exactly one gateway process.** The audit log asserts a contiguous sequence and takes an
  exclusive writer lock, so a second gateway on the same audit directory cannot start.
  That single constraint is what forced the shape of the bash tool (§3).

`gateway/docker/compose.episode.yml` is a 30-line overlay on Astra's `compose.yml`. It
changes three things — the two bind mounts, a parked entrypoint, and a healthcheck that
matches the parked entrypoint. Both internal networks, the backends, the profile split and
the absence of published ports are inherited unchanged. I did not redesign the topology; I
put an episode in it.

**The image is pinned and built from a clean worktree at HEAD (`f9c0ec5`).** [V] Two other
agents are editing `gateway/src` right now; the host venv is a different build from the
container's, and the toolbelt decides both the prompt and what counts as a legal tool. The
container's toolbelt is therefore **read from the container**, not from the host — 37 tools
= the 36 at HEAD plus `bash`, which matches the 36 the host baseline measured against.

---

## 2. From inside the episode, `cat` the grading zone and fail — demonstrated

`tasks/harness/container_check.py` stands up one episode container, opens a **real MCP
session** (`mcp.client.stdio` over the exact `docker exec -i` argv the subject is given)
and attacks it through the subject's own channel. No model, no quota.
Artifact: `tasks/harness/evidence/container-check.json`. **[V]**

```
$ cat /grading/manifest.yaml
  cat: /grading/manifest.yaml: No such file or directory

$ cat /world/../grading/manifest.yaml
  cat: /world/../grading/manifest.yaml: No such file or directory

$ cat ../grading/manifest.yaml
  cat: ../grading/manifest.yaml: No such file or directory

$ find / -xdev \( -name 'REFERENCE_ANSWER*' -o -name 'expected.duckdb' \
       -o -name 'manifest.yaml' \) 2>/dev/null | head -20
  (only third-party dbt/metricflow test fixtures; nothing of ours)

$ for p in /c /host_mnt /mnt/c /run/desktop/mnt/host; do [ -e "$p" ] && echo HOSTFS-REACHED $p; done
  scan-done                       # nothing

$ src=$(awk '$5=="/world"{print $4}' /proc/self/mountinfo); echo "mount-root=$src"; ls "$src"
  mount-root=/Users/.../world
  ls: cannot access '/Users/.../world': No such file or directory

$ getent hosts pypi.org && echo DNS-OK; python -c "socket.create_connection(('1.1.1.1',443),3)"
  OSError: [Errno 101] Network is unreachable
```

**An attack counts as denied only when none of the bytes that live solely outside the
container appear in its output** — sentinels the attack prints on success, plus content
read out of the real grading zone at run time. The first version of this check scored on
the shell's exit status and called three successful denials failures: `cat /grading/x |
head` exits 0 because `head` exited 0. Exit status cannot be the criterion; content is.

Alongside them, four things a legitimate agent does, which must **succeed**: `pwd` →
`/workspace`, `git log --oneline -3` → the world's seeded commit, `ls -la
/world/var/warehouse`, and a write through the shell that lands in the world. 4/4. **[V]**

### `reference_copy`, the baseline only topology can defeat

Every task's own `run_baselines.py` prints the same note and has printed it for two rounds:

> *fixtures share a directory, so ATTACH succeeds here by design. Track A must prove
> unreachability in the real container.*

GT001 scores `reference_copy` **17.0/19** in fixtures, on purpose — a grader that cannot
see the copy is a grader lying. So the check now runs that baseline's one load-bearing
statement for real, inside the episode, through the agent's own shell. **[V]**

---

## 3. How a bash call reaches the §9.2.2 action log

This was the part with a real risk of being fudged, so here is the mechanism in full.

**It could not be a second writer.** `AuditLog` asserts `[row["seq"] for row in rows] ==
range(1, n+1)` at startup and holds an exclusive `FileLock` on the audit directory. Two
writers means either a corrupt log or a second log nobody grades. A merge after the fact
cannot restore an interleaving that was never recorded.

**So `bash` is registered into the gateway's own `TOOLS` table at runtime** by
`container_shim.py`, which runs only inside the container, and served by the gateway's own
`Gateway.call`. A shell command therefore takes the identical code path as `sql_query`:
same `seq`, same `args_hash` / `result_hash`, same `op_class` / `targets` / `flags` from
the same `Observation`, same row schema. **Nothing under `gateway/src/` is edited.** The
registration is `TOOLS[name] = (model, description)` plus one method bound onto
`Operations`, both in a harness file.

A real row, verbatim from a live container **[V]**:

```json
{"seq": 1, "vts": "2026-03-05T00:00:00Z", "wts": 1789405752061, "transport": "mcp",
 "tool": "bash", "args": {"command": "cat /grading/manifest.yaml", "cwd": "."},
 "args_hash": "4b60288055ce12d4…", "args_summary": "{\"command\":\"cat /grading/manifest.yaml\",\"cwd\":\".\"}",
 "op_class": "exec", "targets": [], "target": null, "read_targets": [], "flags": ["shell"],
 "rows_affected": null, "result_hash": "bddad9be3e82a2f7…", "result_bytes": 196,
 "latency_ms": 301.399, "exit": "error", "error": {"code": "command_failed", …},
 "rss_mb": 125.59, "disk_mb": 10.833, "schema_version": 1}
```

- Every 9.2.2 key present (checked by set difference, not by eye). **[V]**
- Sequence contiguous across mixed `bash` / `sql_query` / `bi_*` rows. **[V]**
- `alkera-gateway trace` — the gateway's own validator, which re-derives both hashes —
  **validates the log**. **[V]**

**`targets` on a bash row are the world files the command changed**, from a before/after
`stat` walk of the world tree. Without them the off-channel-change detector would fire on
every legitimate shell write, and a detector that fires hardest on the agent doing real
work stops being read — the same failure C1 hit with its `/tests` marker. From a live
rollout **[V]**:

```
seq 9  bash  op_class=exec  flags=['shell','destructive_hint']
       targets=['var/warehouse/warehouse.duckdb',
                'workspace/dbt_project/models/warehouse/fct_orders.sql',
                'workspace/dbt_project/tests/assert_fct_orders_unique_line_grain.sql',
                'workspace/dbt_project/tests/assert_kpi_daily_order_count_matches_orders.sql']
```

**One deliberate divergence, and it matters.** The bash result reports `exit_code`, not
`returncode`. `Gateway._call` turns any result carrying a non-zero `returncode` into a
`command_failed` error row — correct for `dbt run`, wrong for a shell, where `grep -q`
returning 1 is a *successful* tool call with a false answer. Left alone, every `test`,
`grep` and `find` an agent ran would inflate the error rate `classify.py` reads, and an
agent working the shell competently would be filed as `tool_gap`. The shell's status is
not lost: it is in the row's own result and a non-zero one is flagged `nonzero_exit`.
`timed_out` keeps its original name, because a command the gateway had to kill **is** a
tool error.

### What the shell costs, stated plainly

**An agent with a shell can open the warehouse file directly and mutate it without any
`sql_execute` row naming the statement.** One rollout did exactly that within its first
nine calls — `python3 -c "import duckdb; duckdb.connect('/world/var/warehouse/…')"`. The
action log still has one row per tool call and still names the files that changed, so
§9.2.2 holds at the granularity the contract specifies. But `op_class`, `rows_affected`
and the SQL text are no longer recorded for work done that way, so the process signals
that read them (`ungated_destructive` in particular) see less than they did. That is the
price of realism. I am paying it knowingly rather than discovering it later, and it is the
single most important thing for whoever owns the process-signal work to know.

---

## 4. The blindness guarantee in its new form

`tool_surface_revoked` says: the subject cannot read the grading zone because it holds no
tool that reads anything. True, strong against a cooperative subject, and the exact reason
the terminal could not be handed over.

`filesystem_isolated` says: the subject cannot read the grading zone because **there is no
path to it from where the subject's tools run**. That survives the subject holding a
shell, which the first does not. It is also a claim about infrastructure, and
infrastructure drifts — so it is **measured, never declared**.

**`blindness.guarantee()` derives the recorded string from evidence**, following C1's
discipline exactly. A rollout whose container was not probed does not get the stronger
claim; it gets `tool_surface_revoked+unverified_container` and quarantines.

**Per rollout, twice** — before the subject starts and again after it stops, because
isolation that held at t=0 and not at t=end is not isolation and the agent had a root
shell in there. The probe is fed to `python -` on **stdin**, so it is never written to the
container's disk and never appears in a command line the subject could read. It records:

| property | how it is answered |
|---|---|
| `/grading`, `/world/../grading`, `/grading-zone`, `/tests` | `os.path.exists` **and** an attempted read of each |
| the host world/grading/rollout/repo paths | attempted `os.path.exists` on each, verbatim |
| egress | TCP to 3 addresses + DNS for 3 names, from inside the netns, plus `/proc/net/route` |
| marker-shaped files readable by the shell | a bounded filesystem sweep, filenames **and** contents |
| bind mounts publishing the repo path | `/proc/self/mountinfo` root fields, matched against the repo path |
| what the shell inherits | uid/gid, the full env key list, mount table |

Any fault is a **hard** `unverified_containment`, which is in `UNOBSERVED_KINDS` — so it
quarantines as **unobserved**, not as `blindness_violation`. That distinction is C1's and
it is right: "we could not verify the boundary" is a statement about the harness, never
about the subject, and filing it as the subject cheating would be a quiet libel.

Ten new self-test checks drive it, including four that must fire and two that must not.
`selftest.py`: **73 passed, 0 failed** (was 59 before this work; the brief's "51" predates
two rounds of growth). **[V]**

Two hygiene steps the container gets before the subject arrives, both recorded per rollout:
`/opt/stack` and `/opt/gateway/tests` are **removed** — the compose/containment scripts and
the gateway's test suite are baked into the image, the agent has a shell, and
`containment.py` contains the string `/grading`. What remains is swept and reported rather
than assumed clean; §6 says what the sweep still finds.

---

## 4a. `reference_copy` scores 0, and the item that has been open since GT001 shipped is closed

`tasks/harness/reference_copy_check.py` runs the `reference_copy` baseline **inside the
episode container**, through the agent's own `bash` tool, then tears the container down
and runs GT001's real grader on the world it left behind. It reproduces
`agent_reference_copy` verbatim — the same ATTACH, the same DELETE/INSERT, the same fall
back to `agent_no_op` when the ATTACH raises. Artifact:
`tasks/harness/evidence/reference-copy-contained.json`. **[V]**

```
reference_reachable_from_inside_the_episode : false
attach_error : IOException: IO Error: Cannot open database
               "/grading/expected.duckdb" in read-only mode: database does not exist

reference_copy, in fixtures   : 17.0 / 19      (by design - the grader must see the copy)
reference_copy, CONTAINED     :  0.0 / 19      graded by tests/grade.py, twice, stable
```

`task.toml` declares `expect = "unreachable at ATTACH (build gate, not a test)"`. That is
now a measured 0, not an owed one. The same note appears verbatim at the bottom of GT002's
and GT003's baselines — *"Track A must prove unreachability in the real container"* — and
the mechanism that produces this 0 is not GT001-specific: the grading zone is a sibling of
the world root for every task, and no task's grading zone is mounted. I ran it for GT001
only; the other two are one command each and I did not spend the time. **[V for GT001,
untested for GT002/GT003.]**

All three tasks are still **TASK VALID** on the host fixtures: GT001, GT002 and GT003 each
run `run_baselines.py` green. **[V]**

---

## 5. GT001, re-measured with the terminal

One run, K=5, `opus`, driver `mcp`, `--topology container`, the task's own ceilings (5400s,
600 calls), same seed, same grader, same fixture. **[V]**
`%TEMP%/alkera-rollouts/GT001-aov-halving/claude-k5-container/`

| | host, tool-revoked (`claude-k5`) | container, **with a shell** (`claude-k5-container`) |
|---|---|---|
| blindness guarantee | `tool_surface_revoked` | **`filesystem_isolated`**, on all 5 |
| K / scored / quarantined | 5 / 5 / 0 | 5 / **3** / 0 |
| excluded, and why | 0 | **2 — account monthly spend limit, mid-episode** |
| **Pass@1** | **1.0** (5/5) | **1.0** (3/3) |
| Pass@1, reasoning-attributable | 1.0 | 1.0 |
| totals (of 19) | 17, 17, 18, 18, 18 | 17, 18, 18 |
| median / mean | 18.0 / 17.6 | 18.0 / 17.67 |
| median tool calls | 67 | **80** |
| median wall clock | 484.6 s | **634.8 s** |
| dead surfaces | `bi_cached`, `dbt`, `s3` | `bi_cached`, `dbt`, `s3` — identical |

Per-check, scored rollouts only:

| check | max | host pass rate | container pass rate | host mean | container mean |
|---|---|---|---|---|---|
| D1 window partitions | 5 | 1.0 | 1.0 | 5.0 | 5.0 |
| D2 no duplicate keys | 3 | 1.0 | 1.0 | 3.0 | 3.0 |
| D3 manual adjustments survive | 3 | 1.0 | 1.0 | 3.0 | 3.0 |
| D4 fct_revenue undamaged | 0 | 1.0 | 1.0 | 0.0 | 0.0 |
| D5a primary cause | 2 | 1.0 | 1.0 | 2.0 | 2.0 |
| D5b damage window | 2 | 1.0 | 1.0 | 2.0 | 2.0 |
| **D6 secondary causes** | 2 | **0.0** | **0.0** | 0.6 | 0.67 |
| D7 durable loader | 2 | 1.0 | 1.0 | 2.0 | 2.0 |

### What this says

**The terminal did not move the score.** Same Pass@1, same per-check profile, same single
discriminator. D6 stays at 0.0 in the container, as it does on the host and as it does for
kimi on the host (`kimi-k5b`: 4 scored, Pass@1 0.75, D6 mean **0.0**). **A shell does not
make an agent report honestly** — D6 is about naming only the secondary causes you
evidenced, and no amount of `grep` helps with that. Three runs, two model families, two
topologies, one discriminating check. That is now a well-supported finding rather than an
observation from one run.

**The terminal did cost budget: +19% tool calls and +31% wall clock at the median.** Part
is the container (the world is a 9p bind mount on Docker Desktop, and five stacks ran
concurrently); part is that a shell invites exploration. Neither ceiling is close to
binding on GT001 — 80 of 600 calls, 635 of 5400 seconds — so it changes nothing here. It
will matter for the first task authored with a tight budget, and the tool-call budget now
means something different, because one `bash` row can carry a compound command that used
to be six tool calls.

**The agents used the shell, and used it as a second channel rather than a replacement.**
Across the five rollouts bash was **13–30% of all tool calls** (10, 15, 21, 21, 28 of
72–103), always alongside 31–40 `sql_query` calls. **[V]** Two of them opened the DuckDB
file directly from Python inside bash rather than going through `sql_execute` (see §3's
cost). One (`r3`) made a single shell call that changed **217** world files and had them
all named on its action-log row.

### Be precise about the denominator

**This is Pass@1 = 1.0 over three scored rollouts, not five.** Two were killed mid-episode
by the account's monthly spend limit, with the CLI's own message captured verbatim in the
record:

```
r2  agent_error  rc=1  103 tool calls  784s  "You've hit your monthly spend limit ..."
r3  agent_error  rc=1   92 tool calls  766s  "You've hit your monthly spend limit ..."
```

Both were classified `harness_error` — *"infrastructure, not the agent's reasoning"* — and
excluded from Pass@1, which is exactly the misattribution the taxonomy exists to prevent:
both were at 13/19 when killed and would have read as reasoning failures under a cruder
classifier. They are flagged `needs human review` in the report. **A K=3 denominator
carries a wide interval**: 3/3 and 5/5 are both consistent with a true rate anywhere above
roughly 0.4. The correct reading is *"no detectable change from adding a terminal"*, not
*"proven identical"*. Whoever next has quota should re-run K=5 under `--topology
container` before this number is quoted outside the team.

### One discrepancy, recorded rather than smoothed over

The baseline numbers I was handed for `claude-k5` (median 78 calls, 546 s, D6 mean 0.2)
do not match the artifact on disk at
`%TEMP%/alkera-rollouts/GT001-aov-halving/claude-k5/report.json`, which says **67 calls,
484.6 s, D6 mean 0.6**. The table above uses the on-disk artifact, because that is what I
can read and re-check. Pass@1, the scored count and the per-check pass rates agree in both
versions, so the comparison is unaffected — but somebody is quoting a run I cannot find,
and one of us should work out which. **[V for the on-disk figures; the others I could not
reproduce.]**

### The image pins 36 tools; `gateway/` now has 48

The episode image is built from a clean worktree at `f9c0ec5` and serves **36 + `bash`**.
While I was down, the observability surface landed — `alarms_*`, `deploy_*`, `logs_*`,
`metrics_*`, twelve tools — and the host gateway is now **48**. **[V]** I did **not**
rebuild, for two reasons: the K=5 above is only comparable to the 36-tool host baseline if
the toolbelt matches it, and rebuilding mid-round would bake in two agents' uncommitted
work. **The episode container will not see the observability tools until someone rebuilds
`alkera-episode:head`**, which should happen before the next measured run — with the
toolbelt difference recorded, because it changes the prompt and therefore the measurement.
The recipe is in `tasks/harness/README.md`; build it from a clean worktree, never from the
working tree:

```bash
git worktree add --detach /tmp/wt HEAD
docker build -t alkera-episode:head -f /tmp/wt/gateway/docker/Dockerfile /tmp/wt
git worktree remove /tmp/wt
```

---

## 6. The single biggest weakness in my own output

**The shell is a much larger surface than the eight commands I attacked, and the only
thing standing between it and the grading zone is a Docker bind mount I did not try to
escape.**

Everything in §2 is a *filesystem* attack: paths that do not exist, a `find` that comes up
empty, a mount root that cannot be followed. I proved the grading zone has no path. I did
**not** attempt any of the following, and a competent adversary would:

- **Container escape proper.** The episode runs as **uid 0** with Docker's default
  capability set and no user namespace, no seccomp tightening, no `--read-only` rootfs, no
  `--pids-limit`, no `--cap-drop`. I did not try `mount`, `/proc/sysrq-trigger`, a CVE, or
  anything else that would put the agent on the host. A root shell in a default-privileged
  container is one kernel bug from the host filesystem, and the grading zone is on that
  filesystem. My claim is "no path leads there", not "the boundary cannot be broken".
- **The Docker socket.** It is not mounted — I checked the mount table — but I never wrote
  the check as a gated property, so a future compose change could mount it and no test
  would go red.
- **`ALKERA_POSTGRES_DSN` is in the shell's environment.** **[V]** For GT001 the warehouse
  is a DuckDB file, so it buys nothing and B2B4 showed the role is not a superuser. For a
  Postgres-backed world it is a second channel to the warehouse that bypasses `sql_*`
  entirely. Nothing scrubs it today.
- **Resource exhaustion.** No memory, CPU, PID or disk limit is set on the episode
  container. `task.toml` declares `memory_mb` and `disk_mb`; the container ignores both.

A second, sharper weakness, and the one I would fix first if I had another hour:
**`op_class` is a lie for shell work.** A bash row is always `op_class: "exec"`, so an
agent that deletes half the warehouse through `python -c` produces a row indistinguishable
in class from one that ran `ls`. The row names the files that changed, so §9.2.2 holds and
nothing is *missing* — but `signals.py`'s `ungated_destructive`, which reads `op_class`,
now under-counts by exactly the amount of destruction done through the shell, and two of
five rollouts did real warehouse work that way. The fix is not hard (classify from the
measured world delta rather than from a regex over the command), but it is not done, and
until it is, **process signals measured under `--topology container` are not comparable to
process signals measured on the host.** I would rather write that sentence than have
someone discover it in a chart.

Three narrower ones, each of which could have been the headline:

1. **`Pass@1 = 1.0` rests on three rollouts.** §5 says so, but it bears repeating here
   because it is the number people will quote. The topology work is solid; the
   *measurement* under it is thin, and it is thin for a billing reason rather than a
   scientific one.
2. **The marker sweep still finds eight hits, and I shipped anyway.** Every one is the
   string `agent.md` in a docstring cross-reference inside Track A's `world/` package and
   `gateway/src/alkera_gateway/warehouse.py` — files the gateway imports, so I could not
   strip them the way I stripped `/opt/stack` and `/opt/gateway/tests`. They are
   `marker_in_file`, not `marker_filename`, so they do not fail containment by design. An
   agent that ran `grep -r agent.md /opt` would learn that a document by that name exists.
   That is weak, it is recorded on **every rollout** rather than hidden, and it is a
   one-line fix in someone else's file, which is why I reported it instead of making it.
3. **I only ever ran GT001 in the container.** GT002 and GT003 have their own world
   builders and their own layouts. `container_world()` translates any §9.2.1 layout, so it
   *should* work, but "should" is the word B2B4's own §5 warned about and I have no right
   to a stronger one. The `reference_copy` proof in §4a is likewise GT001-only.

One thing I want on the record as **not** verified: `--topology container` is wired for
the `mcp` driver and raises a `HarnessError` for anything else. The **kimi driver has no
container path**, so the second model family — the one that gives D6 its cross-family
evidence — still runs on the host topology with no shell. Adding it is
`kimi_subject.write_mcp_config` taking a server argv, the same three-line change as
`agent_under_test.write_mcp_config`, and I did not make it.
