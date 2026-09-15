# LIVE-WORLD — the scheduler now executes, and the world moves

**Agent:** LIVE-WORLD · **Round:** R003 · **Scope:** `world/` only.
**Date:** 2026-09-14.

Replaces the 98-line `world/world/scheduler.py` that appended
`"status": "success"` as a literal. `trigger` now runs the job's real work and
derives the status from it; `advance` materialises the span — new partitions
land, scheduled jobs fire, in virtual-time order, for real.

---

## 1. What executes now, and what is still simulated

### Executes for real

| Surface | What runs | How it can fail |
|---|---|---|
| `dbt` jobs | `dbt build/run/test/compile` as a subprocess against the world's own dbt project and DuckDB file, `threads: 1` | dbt's return code. The run's `error` is dbt's own message, normalized |
| theme loader jobs | the theme's `gen.load_raw`, which for three of four themes shells out to the loader script **that is in the agent's workspace** (`loader/vendor_load.py`, `loader/load_extract.py`, `loader/import_close.py`) | the loader raises or exits non-zero |
| `sql` jobs | statements, or a `.sql` file inside `workspace/`, against the warehouse through an aliased `ATTACH` | DuckDB raises; the binder error is the run's `error` |
| `script` jobs | any python file inside `workspace/` | non-zero exit |
| `scan` jobs (vendor poll, extract delivery) | globs the real delivery folder and counts real files | folder missing, or fewer than `min` files |
| arrivals | the theme's `write_day(workspace, seed, day_index, day)` hook — the same code the initial build used, so day 31's bytes are what the build would have written | — |
| **nothing else** | a job with no `exec` block, or an unknown kind, is `status: failed`, `executed: false`, `error: "…declares no executable work…"` | by construction |

All four themes are wired: `revenue_daily`, `sftp_vendor`, `mainframe_batch`,
`excel_close`. Verified live — each lands a partition and runs its chain:

```
sftp_vendor      2026-03-04T05:00Z arrival vendor_drop   {'orders': 50, 'returns': 1}
                 05:30 sftp_poll success · 05:40 vendor_load success · 05:45 dbt_build success
mainframe_batch  2026-03-03T23:00Z arrival claims_extract {'partition': 'dt=2026-03-03', 'records': 40}
                 23:00 nightly_extract success · 23:45 batch_load success · 23:55 dbt_build success
excel_close      2026-03-04T07:00Z arrival close_workbook {'workbook': 'CLOSE_202603.xlsx', 'lines': 115}
                 08:00 close_import success · 08:10 dbt_build success
revenue_daily    2026-03-04T00:00Z arrival daily_revenue  {'partition': 'dt=2026-03-03', 'rows': 50}
                 01:00 daily_load success · 01:05 dbt_build success
```

### Still simulated, and named as such

- **Durations.** `vts_end - vts_start` is the job's seeded `duration_s`, not the
  measured wall time — deliberately: measured time is the single most
  host-dependent number there is, and it is inside the fingerprint. A dbt build
  that really takes 4.1 s is still recorded as the declared 90 s.
- **`nightly_extract` verifies rather than produces.** The mainframe extract is
  created by the arrival stream; the job scans for it. The run record says
  `scan data/landing/claims_extract: 31 file(s)`, so it is not claiming to have
  cut the extract.
- **Nothing fails on its own.** Arrivals always arrive, jobs never time out,
  nothing retries, no two jobs overlap, no partial write is ever observed. A
  broken world comes from an author planting a defect or an agent breaking it,
  never from the simulator. See §6.
- **Seeded history is still backstory.** The runs `initial_state` writes are
  byte-for-byte what they were in R002 — a described past, not an executed one.
  Only executor-produced runs carry `executed`/`action`/`error`/`effects`.

---

## 2. Where it lives: two files, one of them invisible

| file | holds | reachable by the agent |
|---|---|---|
| `var/sched/state.json` | `vnow`, `jobs` = {command, duration_s}, `runs` | yes — `sched_state` / `sched_list` / `sched_logs` |
| `var/world/live.json` | the schedule, each job's `exec` block, the arrival stream, the traffic parameters | **no gateway tool reads `/var/world`** |

The split is the main design decision and it is what kept the frozen surface
intact. The live world needs the seed's traffic parameters to keep generating
after the seed file is gone (a restored snapshot has no seed). Putting them in
`state.json` would have handed `sched_state` the generator's parameters — a
disclosure hazard of the F202 family, since a task can plant a defect *via* a
seed parameter. `/var/world/` is outside `/workspace` (so `fs_*` cannot reach
it) and is not any tool's configured path.

Only the seed keys a theme **declares** are copied — an auditable list in
`theme.yaml`, e.g. `params: [seed, orders_per_day, customers, amount_cents]`.

`live.json` is conditionally fingerprinted, the same way `var/ingest/state.json`
already was: a world that does not seed it keeps exactly the sections it had.

---

## 3. Determinism evidence — verbatim

Seed `world/seed.example.yaml` (theme `revenue_daily`, seed 1, 30 days).
All hashes are `world.fingerprint` semantic hashes.

### Gate 1 — two from-scratch builds, then the same `advance(30)`

```
build A semantic_hash : 76e3062b379c385385b093374eac1947c03585b86646b3023842f0378ece07f6
build B semantic_hash : 76e3062b379c385385b093374eac1947c03585b86646b3023842f0378ece07f6
equal at build        : True
A after advance(30)   : 1b9e498773f5f15897621086399527bdd8b774e6d93ccd68b986e8a6d63b90a5
B after advance(30)   : 1b9e498773f5f15897621086399527bdd8b774e6d93ccd68b986e8a6d63b90a5
equal after advance   : True
advance moved the fp  : True
state.json bytes equal: True
```

### Gate 2 — path independence: `advance(1)` × 30 == `advance(30)`

```
C after advance(1)x30 : 1b9e498773f5f15897621086399527bdd8b774e6d93ccd68b986e8a6d63b90a5
equal to advance(30)  : True
```

### Gate 3 — CROSS-HOST: Windows host vs Linux container, same seed

`alkera-world:0.1.0`, built from this tree, `python -m world.build` then
`python -m world.scheduler /tmp/w advance 30`:

```
container built    : 76e3062b379c385385b093374eac1947c03585b86646b3023842f0378ece07f6
container advanced : 1b9e498773f5f15897621086399527bdd8b774e6d93ccd68b986e8a6d63b90a5
```

Identical to the host, digit for digit, **after execution**. This is the gate
the task called non-negotiable, and it is proven cross-host, not same-host.

### Gate 4 — the failure path is deterministic too, cross-host

Advance 30 h, then break `fct_revenue.sql` (`group by order_dat`), then
`trigger dbt_build`. The engine's error goes into `state.json`, which is
fingerprinted, so the *error text* is a determinism surface:

```
                            host (Windows)                     container (Linux)
state.json sha256   d38d156e6827ef28552d06362e470b5d543328fdb7a5d668554eaa8ce5f1dfbb   (identical)
semantic_hash       c900bff777201fea766807e06523991a88a690088f88f8e4431d236b69964b23   (identical)
```

The run record, verbatim:

```
status: failed   executed: True
2 of 2 ERROR creating sql table model warehouse.fct_revenue .................... [ERROR in <elapsed>]
[ERROR]: in model fct_revenue (models/warehouse/fct_revenue.sql)
  Runtime Error in model fct_revenue (models/warehouse/fct_revenue.sql)
  Binder Error: Referenced column "order_dat" not found in FROM clause!
  Candidate bindings: "order_date", "order_id", "customer_id", "amount"
  LINE 12: group by order_dat
                    ^
```

Note `models/warehouse/fct_revenue.sql` on **both** hosts. dbt printed
`models\warehouse\fct_revenue.sql` on Windows.

**How the normalization was designed: by measurement, not assumption.** Before
writing a line of it I ran a broken `dbt build --quiet` and captured the raw
bytes. dbt emits exactly four host-varying things and `normalize_output`
removes all four:

```
'\x1b[0m17:03:00  2 of 2 ERROR creating sql table model warehouse.fct_revenue [\x1b[31mERROR\x1b[0m in 0.16s]\n
 \x1b[0m17:03:00    Runtime Error in model fct_revenue (models\\warehouse\\fct_revenue.sql)\n ...'
   ANSI colour · a wall-clock HH:MM:SS per line · `in 0.16s` elapsed · host path separators
```

Raw, un-normalized output is written to `build/sched/logs/run-NNNNNN.log`, which
is outside the fingerprint **and** outside `snapshot.SCOPE`. Nothing volatile is
hashed.

### Gate 5 — break → fail → repair → succeed → world moves

Through the real gateway tool surface (`Gateway(config,
scheduler=WorldScheduler(root))`, `gw.call("sched_trigger", …)`):

```
sched_trigger dbt_build (model BROKEN)
  ok: False | error: {'code': 'command_failed', ...}
  status: failed | executed: True
  engine error: 2 of 2 ERROR creating sql table model warehouse.fct_revenue ...
repair, trigger again
  ok: True | status: success
  warehouse fingerprint moved: True

action log
  seq=1 tool=sched_list     exit=ok     op_class=read
  seq=2 tool=sched_trigger  exit=error  op_class=exec
  seq=3 tool=sched_trigger  exit=ok     op_class=exec
  seq=4 tool=sched_logs     exit=ok     op_class=read
```

A failed run leaves the warehouse section of the fingerprint untouched
(`b0d387ea… → b0d387ea…`); a successful one moves it (`b0d387ea… →
add619b3…`). **"Did the fix work?" is now an answerable question.**

### How to reproduce

```bash
cd world && PYTHONPATH=. ../gateway/.venv/Scripts/python -m pytest tests     # 37
bash scripts/acceptance.sh        # gate 2b is the new live-world gate
docker run --rm --entrypoint sh alkera-world:0.1.0 -c \
  'python -m world.build --seed /opt/world/seed.yaml --root /tmp/w --lock /opt/world/requirements.lock >/dev/null
   python -m world.scheduler /tmp/w advance 30 >/dev/null
   python -m world.fingerprint /tmp/w'
```

---

## 4. Which tasks broke

**None.** Every suite is green, unchanged.

| suite | before | after |
|---|---|---|

| `world/tests` | 21 | **37** (16 new in `test_live_world.py`) |
| `gateway/tests` | (not re-measured before) | **86 passed** |
| GT001 `run_baselines.py` | TASK VALID | TASK VALID (oracle 19/19, no_op 0) |
| GT001 `test_integration.py` | 12 | 12 |
| GT002 `run_baselines.py` | TASK VALID | TASK VALID (oracle 24/24) |
| GT003 `run_baselines.py` | TASK VALID | TASK VALID (oracle 20/20) |

The reason nothing broke is worth stating, because it was the design
constraint, not luck:

- `initial_state(seed, jobs)` keeps its signature and its **byte-identical
  output**. GT001 calls it directly from `tests/build_world.py`. Confirmed: the
  `scheduler` section of the fingerprint is unchanged before/after this work
  (`12bc81b4…` both sides), and `state.json` hashes identically.
- `state.json`'s `jobs` still carries only `{command, duration_s}`, so
  `sched_list` returns what it always returned. The new configuration went into
  `live.json`, not into the frozen surface.
- GT002 and GT003 write their own `sched_state.json` and never import the
  scheduler. Untouched.
- Tasks whose worlds are hand-built (all three) have no `var/world/live.json`.
  There, `trigger` returns `unsupported` and `advance` moves only the clock and
  says so in its result (`"live": false`, `"note": "…nothing was executed"`).
  That is a **behaviour change from silent success to loud unsupported** — the
  point of the exercise — but no task exercises it, so no baseline moved.

### One genuine finding for the task authors

**GT003's D6 can stop grading by convention.** Its author noted the two script
files are graded by convention because "the fixture scheduler is a JSON log with
no executor". The executor now exists, but GT003's fixture world does not
declare `exec` blocks, so D6 cannot be converted without regenerating that
fixture. The path is: give GT003's `make_fixture_world.py` a `var/world/
live.json` with `exec: {kind: sql, script: pipelines/mart_rebuild.sql}` on
`mart_hourly_rebuild`, and D6 becomes "trigger the job and see whether the
rebuild is atomic" instead of "read the file and look for a pattern". That is
**Track C's call, not mine** — I did not touch `tasks/`.

---

## 5. Changes worth announcing to the other tracks

Nothing frozen changed. Two things are near enough to say out loud.

1. **A fifth conditional fingerprint section, `live`.** A world built by
   `world.build` from a theme with a `live:` block now has five sections
   instead of four, so **its recorded semantic hash moves**. Nothing in the
   repo asserts one against a literal — checked: every 64-hex literal in the
   tree is a pinned docker image digest or a recorded action-log hash, and both
   places that compare a world fingerprint (`audit.py`, `test_integration.py`)
   compute it live — and GT001/2/3 fixtures do not seed `live.json` at all, so
   they are unaffected. `world/tests/test_world.py::test_no_ingest_section_without_
   control_plane` was renamed and extended to assert the new set honestly.

2. **`WorldScheduler` sets `returncode` on `sched_trigger`.** The gateway's
   `core.py` already treats `result["returncode"] != 0` as `command_failed`
   (it does this for `dbt_run`), so a failed job shows up as `exit: "error"` in
   the action log rather than only inside the result blob — which is what a
   grader reading the log has to see. `FileScheduler` does not do this. It is
   the **one envelope-level difference** between the two, it only applies when
   a world injects `WorldScheduler`, and the gateway owner can drop the key if
   they would rather keep `ok: true` on a failed job.

### The handoff the gateway owner should take

`alkera_gateway.surfaces.FileScheduler.trigger` still fabricates:

```python
else:
    result = {"intent": command, "variables": variables}
    run["status"] = "success"          # ← nothing ran
```

It executes `dbt_build`/`dbt_run` and asserts success for everything else — a
load, an extract, a vendor poll. I did not touch `gateway/src/` (out of scope,
another agent is in there). The fix needs no gateway code at all: construct the
gateway as `Gateway(config, scheduler=WorldScheduler(config.world_root))`. The
`Scheduler` Protocol and the `scheduler=` parameter already exist; this is the
seam they were built for. `world/world/gateway_scheduler.py` imports nothing
from `alkera_gateway`, so §10.2's dependency arrow stays pointed one way.

---

## 6. The single biggest weakness in my own output

**The world only breaks when someone breaks it. Every failure mode this
simulator can produce is one an author planted or an agent caused — the
environment itself is incapable of misbehaving, and that is exactly the class
of incident the benchmark sells.**

Concretely, `advance` cannot produce any of the following, which are the
pathologies the corpus is full of:

- a partition that arrives **late**, or not at all, or **twice**;
- a job that **times out**, **retries**, or is still running when the next
  occurrence is due;
- two jobs **overlapping**, which is GT003's entire premise — a read that lands
  inside a rebuild;
- a file **read while it is still being written**, which is D1's signature
  pathology and the reason the `sftp_vendor` theme exists;
- any **partially applied** write.

So the honest summary is: I fixed *"the scheduler lies about whether your fix
worked"*, which was the disqualifying defect. I did **not** deliver
Horizon-SWE's *"data, traffic, background jobs and metrics change over time"* in
its interesting sense. What I built is a metronome that executes: it runs the
happy path forward, deterministically, and reports honestly when the happy path
breaks. It is not yet a world with weather.

I made that trade on purpose — the brief said determinism over realism, every
time, and each of the behaviours above is a fresh determinism hazard that has to
be designed rather than bolted on (a retry needs a seeded failure schedule; an
overlap needs a concurrency model with a defined interleaving; a torn read needs
a defined observation point). Dropping one in without that design is how a world
stops being reproducible. But I want the omission recorded as a gap in the
deliverable, not filed as future work: **a task author reading "the world is
stateful and evolving" today will find that it evolves only in one direction and
only when everything works.**

Two smaller weaknesses, in order:

- **The normalized error text is a determinism surface I do not fully control.**
  It is proven stable across Windows and Linux at the pinned dbt and duckdb
  versions, which is real evidence, not an assumption. But the content is a
  third party's prose: a dbt patch that renumbers `2 of 2`, retitles
  `Runtime Error`, or changes the dotted padding width moves every fingerprint
  of every advanced world. The mitigation is that `requirements.lock` pins it
  and `provenance.json` attests it — the same guarantee the rest of the build
  has — but it is a new, wider dependency of the fingerprint on dbt's output
  formatting, where before the fingerprint depended only on dbt's *results*.
  If this bites, the fix is to fingerprint an error *classification* and keep
  the prose out of `state.json`; that costs the agent the message, so I did not
  do it pre-emptively.
- **`var/world/live.json` is unreachable by tool, not by topology.** No gateway
  tool exposes `/var/world`, which is why the traffic parameters are safe to
  store there. If the workspace-terminal work now in flight gives the agent a
  shell with a filesystem view wider than `/workspace`, that protection
  evaporates and the seed parameters become readable. It is fingerprinted, so
  *tampering* is detectable; *reading* is not. Whoever lands the terminal should
  treat `/var` as needing the same containment as `/grading`.
