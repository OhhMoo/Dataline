# RESEARCH — orchestration under a virtual clock

**R004. Written 2026-09-14.** Question from the user, verbatim:

> "these point here because i think this is what we actually want to deliver and
> following our northstar as a real data engineer will use. if we cannot directly
> call them, can we create a fake api or mcp that let agent believe they are using
> those corresponding tools?"

**Short answer: we do not need a fake.** The premise I reported earlier — "a real
running Airflow is blocked because its clock fights ours" — is **wrong for the
execution path**, and I can now show that with measurements rather than argument.
Airflow ships a supported, in-process, scheduler-free execution entry point; its
timetables are clock-free; and the whole of its clock surface on that path
collapses to **one function** (`airflow.utils.timezone.utcnow`). With a 12-line
shim, `dag_run` came back **byte-identical across two independent runs on all 25
columns**, and `task_instance` on **34 of 37**.

The thing that *is* genuinely blocked is the **scheduler daemon** — and we do not
want it, because `advance()` already is a scheduler loop, and a better one for our
purposes: ours is deterministic and instantaneous.

---

## 0. Evidence log — what I ran and what I read

Everything below is marked **[executed]** (I ran it and the output is quoted or
summarised from the real run) or **[read]** (I fetched and read the source or doc).
Nothing here is recalled from memory.

| # | What | Mark |
|---|---|---|
| E1 | `docker pull apache/airflow:3.0.2` → `docker images` reports **2.53GB**; `apache/airflow:2.10.5` also pulled | executed |
| E2 | `pip install apache-airflow==3.0.2` on **native Windows** → `AttributeError: module 'os' has no attribute 'register_at_fork'` at `settings.configure_orm`, after a `RuntimeWarning` naming [apache/airflow#10388](https://github.com/apache/airflow/issues/10388). Airflow does not run on Win32; our container is Linux so this is a dev-box note only | executed |
| E3 | `airflow dags test revenue_daily 2026-03-02T01:00:00+00:00` in 3.0.2 — ran both tasks in dependency order with **no scheduler, no webserver**, SQLite metadata DB | executed |
| E4 | `inspect.getsource(DAG.test)` on 3.0.2 and `inspect.signature` on both 3.0.2 and 2.10.5 | executed (source read via the installed package) |
| E5 | `DagRun.generate_run_id` source + live calls on 3.0.2 and 2.10.5 | executed |
| E6 | Timetable class + `infer_manual_data_interval` on both versions | executed |
| E7 | **Determinism A/B**: three `dag.test()` runs, full `dag_run` + `task_instance` dump, two independent containers, column-by-column diff — *before* and *after* the clock shim | executed |
| E8 | `airflow tasks clear` semantics with no scheduler running | executed |
| E9 | `airflow api-server` cold boot on `--network none` | executed |
| E10 | Live `GET /api/v2/dags`, `/dagRuns`, `/taskInstances`, `/ui/grid/{dag_id}`, `/ui/structure/structure_data` response bodies | executed |
| E11 | Endpoint inventory from the **shipped** OpenAPI specs in both images (not from docs) | executed |
| E12 | DAG-file edit / DAG-file-breakage behaviour | executed |
| E13 | `timetable.next_dagrun_info` clock-freeness + source | executed |
| E14 | `du -sh` of the official image's `site-packages`, per-package breakdown | executed |
| E15 | `pip install apache-airflow==3.0.2` into `python:3.11-slim` with the official `constraints-3.11.txt`, **no extras** → **210 MB** of site-packages, **129** packages, imports and reports `3.0.2` on Python 3.11 | executed |
| E16 | Retry/failure path: a DAG with `retries=2`, timed with and without the clock shim, then A/B determinism including `task_instance_history` | executed |
| R1 | [airflow.apache.org CLI reference](https://airflow.apache.org/docs/apache-airflow/stable/cli-and-env-variables-ref.html) — page identifies itself as **3.3.1** | read |
| R2 | Dagster / Prefect APIs — delegated, source-verified, URLs in §6 | read |
| R3 | [prefect.io/prefect-acquires-dagster](https://www.prefect.io/prefect-acquires-dagster) — Prefect acquired Dagster Labs, **2026-07-13** | read (I fetched it myself) |
| R4 | `afsurvey/airflow-user-survey-responses-2025.csv`, 5,856 responses, found in this machine's scratchpad from an earlier session | read — **provenance caveat in §5** |

**Versions I verified against: Airflow 3.0.2 and 2.10.5** (both pulled and run
today). The `stable` docs page currently describes **3.3.1**, which I did **not**
run; where I quote the CLI reference I say so, and where I quote my own runs I say
3.0.2. Do not assume 3.3.1 behaves as 3.0.2 does — Airflow changed a great deal
between 2.10 and 3.0 (see §3), and it can change again.

---

## 1. The sharpest question first: what happens when the agent edits a DAG file?

The user asked this directly and it deserves the direct answer, measured.

**In real Airflow 3.0.2 [E12]:**

| Action after appending a new `@task` to `dags/revenue_daily.py` | Result |
|---|---|
| `airflow tasks list revenue_daily` | **new task appears immediately** — this command parses the file |
| `airflow dags test revenue_daily <date>` | **new task ran**, same invocation, no re-registration |
| `airflow dags list` | **stale** — reads the serialized DAG from the metadata DB |
| `airflow dags list --local` | fresh — parses the folder |
| `airflow dags reserialize` | re-registers; DB-backed views catch up |

Then I appended `this is not python (((`:

| Action | Result |
|---|---|
| `airflow dags reserialize` | `SyntaxError: '(' was never closed` |
| `import_error` table | one row: `('/opt/airflow/d2/revenue_daily.py', 'Traceback (most recent call last):…')` — surfaced by `GET /api/v2/importErrors` |
| `airflow dags test` | `AirflowException: Dag 'revenue_daily' could not be found; either it does not exist or it failed to parse.` |
| `airflow dags list` | **still lists the DAG** — Airflow keeps the last good serialized version |

Real Airflow's answer, then, is: *editing a DAG file changes execution immediately,
and changes the catalogue views after a re-parse.* The re-parse latency is
configured, and I read the 3.0.2 defaults off the running config **[E12]**:

```
[dag_processor] bundle_refresh_check_interval = 5
[dag_processor] min_file_process_interval     = 30
[dag_processor] refresh_interval              = 300
[dag_processor] dag_file_processor_timeout    = 50
[api]           auto_refresh_interval         = 3      # the UI's own poll
```

**So the requirement on us is precise:** an edited DAG file must change what the
next `advance()` executes, and must change what `airflow dags list` / the API / the
grid report — the second after an explicit re-parse, not instantly. Anything that
makes DAG files decoration teaches the agent that editing DAGs is inert, which is
the single worst thing this substrate could teach, because "edit the DAG, re-run
the task" is most of the job.

**The recommendation in §7 satisfies this by construction**, because it executes
the agent's actual DAG file through Airflow's own parser and task loop. It is not
a property we have to remember to implement; it is the mechanism.

---

## 2. Can real Airflow be bent to a virtual clock? — the crux, measured

### 2.1 Airflow ships a scheduler-free execution path, and its own source calls it that

`DAG.test()` in 3.0.2, read from the installed package **[E4]**:

```python
def test(self, run_after: datetime | None = None, logical_date: datetime | None = None,
         run_conf: dict | None = None, conn_file_path: str | None = None,
         variable_file_path: str | None = None, use_executor: bool = False,
         mark_success_pattern: Pattern | str | None = None):
```

with this comment in the body, verbatim:

```python
# Instead of starting a scheduler, we run the minimal loop possible to check
# for task readiness and dependency management.
```

That is Airflow's own in-process mini-scheduler. It resolves dependencies, honours
trigger rules, renders templates, pushes XComs, and writes real `dag_run` and
`task_instance` rows. It needs no scheduler, no webserver, no triggerer, and
**[E9]** no network.

Airflow 2.10.5's equivalent is `DAG.test(execution_date=...)` **[E4]**.

### 2.2 The run_id is deterministic — if you call the Python API, not the CLI

Source **[E5]**:

```python
@staticmethod
def generate_run_id(*, run_type: DagRunType, logical_date: datetime | None = None,
                    run_after: datetime) -> str:
    if logical_date:
        return DagRunType(run_type).generate_run_id(suffix=run_after.isoformat())
    return DagRunType(run_type).generate_run_id(suffix=f"{run_after.isoformat()}_{get_random_string()}")
```

and inside `DAG.test`:

```python
run_after = timezone.coerce_datetime(run_after) or timezone.coerce_datetime(timezone.utcnow())
```

So:

- `airflow dags test <dag> <logical_date>` (CLI, 3.0.2) passes **no** `run_after`,
  falls through to `utcnow()`, and I measured the resulting id: **[E3]**
  `manual__2026-09-14T21:05:54.178547+00:00`. Wall clock. Non-deterministic.
- `dag.test(run_after=ld, logical_date=ld)` (Python, 3.0.2) gives **[E7]**
  `manual__2026-03-02T01:00:00+00:00`. Deterministic.
- Airflow 2.10.5's CLI is *already* deterministic, because 2.x derives the id from
  `execution_date` **[E5][E6]**: `manual__2026-03-02T01:00:00+00:00`.
- `logical_date=None` (legal in Airflow 3 for asset-triggered and some manual runs)
  appends `get_random_string()`. **Never let a logical_date be None in our world.**

### 2.3 Timetables are clock-free when catchup is on — so Airflow can tell us what is due

This is the finding that makes the whole thing work. `TimetableProtocol.next_dagrun_info`
**[E13]**:

```
next_dagrun_info(*, last_automated_data_interval: DataInterval | None,
                    restriction: TimeRestriction) -> DagRunInfo | None
```

Walking it with `catchup=True` and **no clock at all**, five iterations, measured:

```
logical_date=2026-03-01 01:00:00+00:00  run_after=2026-03-01 01:00:00+00:00
logical_date=2026-03-02 01:00:00+00:00  run_after=2026-03-02 01:00:00+00:00
logical_date=2026-03-03 01:00:00+00:00  run_after=2026-03-03 01:00:00+00:00
logical_date=2026-03-04 01:00:00+00:00  run_after=2026-03-04 01:00:00+00:00
logical_date=2026-03-05 01:00:00+00:00  run_after=2026-03-05 01:00:00+00:00
```

The one wall-clock read in `CronTriggerTimetable.next_dagrun_info` is on the
`catchup=False` branch only, and it is a single call **[E13]**:

```python
else:
    start_time_candidates = [self._align_to_prev(coerce_datetime(utcnow()))]
```

`utcnow` there is `airflow.utils.timezone.utcnow`, imported by name into
`airflow/timetables/trigger.py` — patchable at the module attribute.

**What this buys us:** our `advance(root, hours)` can ask *Airflow's own timetable
code* which runs fall in the virtual span `(vnow, vnow+hours]`, and get cron
semantics, `catchup`, `start_date` restriction, DST handling and every custom
timetable a theme might ship, for free and correctly. We do not reimplement cron.
We do not approximate Airflow. We call it.

### 2.4 The determinism measurement — the number that matters

**[E7]** Three `dag.test()` calls at logical dates 2026-03-02/03/04, full dump of
every column of `dag_run` and `task_instance`, two independent containers, compared
column by column.

**Before any patching:**

| table | non-deterministic columns |
|---|---|
| `dag_run` (25 cols) | `end_date`, `last_scheduling_decision`, `updated_at` |
| `task_instance` (37 cols) | `duration`, `end_date`, `hostname`, `id`, `last_heartbeat_at`, `scheduled_dttm`, `start_date`, `updated_at` |

Everything else was already identical: `run_id`, `logical_date`,
`data_interval_start/end`, `run_after`, `start_date` (dag_run), `state`,
`try_number`, `max_tries`, `operator`, `pool`, `queue`, `priority_weight`,
`task_display_name`, `dag_version_id`.

**After a 12-line shim** — the entire patch:

```python
# vclock.py — imported before any airflow.models module
VNOW = pendulum.datetime(2026, 3, 2, 1, 0, 0, tz="UTC")
_tick = itertools.count(1)
def vnow(): return VNOW.add(seconds=next(_tick))

import airflow.utils.timezone as aftz;  aftz.utcnow = vnow
import airflow.utils.net as afnet
afnet.get_hostname = lambda: "airflow-worker"
afnet.getfqdn      = lambda name="": "airflow-worker"
import uuid6, uuid, itertools
_uu = itertools.count(1)
uuid6.uuid7 = lambda: uuid.UUID(int=(0x01a0 << 112) | next(_uu))
```

| table | non-deterministic columns, after |
|---|---|
| `dag_run` (25 cols) | **none — 25/25 identical** |
| `task_instance` (37 cols) | `start_date`, `end_date`, `duration` — **34/37 identical** |

Sample of what the shim produced, from the real dump:

```
ti.id                = 01a00000-0000-0000-0000-000000000003
ti.hostname          = airflow-worker
ti.updated_at        = 2026-03-02 01:00:21
ti.scheduled_dttm    = 2026-03-02 01:00:10
dag_run.end_date     = 2026-03-02 01:00:36
dag_run.run_after    = 2026-03-02 01:00:00
```

Virtual timestamps, in virtual 2026-03-02, produced by unmodified Airflow.

**The residual three columns have a known cause, located.** They are stamped by the
Airflow 3 **Task SDK**, which runs the task in a supervised subprocess that talks
back over a local Task Execution API, and which calls `datetime.now(tz=timezone.utc)`
directly rather than going through `airflow.utils.timezone` **[E4, grep]**:

```
airflow/sdk/execution_time/supervisor.py:806   start_date = datetime.now(tz=timezone.utc)
airflow/sdk/execution_time/supervisor.py:1265  start_date = datetime.now(tz=timezone.utc)
airflow/sdk/execution_time/task_runner.py:895  end_date=datetime.now(tz=timezone.utc)
…  (11 sites total in those two modules)
```

Two honest ways to close them, in order of preference:

1. **Normalise, don't patch.** We already do exactly this for dbt: the world
   strips `HH:MM:SS` stamps and `in 0.16s` elapsed from engine output before it
   reaches the fingerprint (`world/README.md`, "engine output is **normalized**").
   `task_instance.start_date/end_date/duration` are the same class of artifact —
   wall-clock measurements of how long real work took. Derive them from the
   virtual clock (`vts_start`, `vts_start + duration_s`) when we project, and
   never fingerprint the raw values.
2. **Patch the two SDK modules' `datetime`.** Mechanically easy, but it reaches
   into a module that is explicitly versioned separately from core Airflow
   (`apache-airflow-task-sdk`) and has no stability contract for this. I would not.

### 2.5 The retry path — measured, and it is the best argument in this document

This was going to be my named weakest finding (everything above was measured on one
happy-path DAG with no failures and no retries). I went and measured it instead.
A DAG with `@task(retries=2, retry_delay=timedelta(seconds=30))` whose task always
raises, run through `dag.test()` **[E16]**:

| | wall seconds for the whole `dag.test` | final state |
|---|---|---|
| **no shim** | **68.3 s** | `flaky` try_number=3 max_tries=2 `failed`; `downstream` `upstream_failed` |
| **with the §2.4 shim** | **20.8 s** | identical |

Two things fall out, and both matter more than the timing.

**(a) `dag.test` really sleeps the retry delay in wall time.** That is why my first
attempt at this measurement, with the realistic `retry_delay=timedelta(minutes=10)`,
was killed by a 300-second timeout having produced nothing **[E16]**. Real retry
delays are 5–30 minutes. **An unshimmed `dag.test` is unusable for any DAG with
retries**, which is every production DAG.

**(b) The virtual clock collapses them.** The retry-due comparison goes through the
patched `utcnow()`, which advances one virtual second per call, so `end_date +
retry_delay` arrives after ~30 polls instead of after 30 real seconds — and it
would arrive just as fast for a 30-*minute* delay. The virtual clock is not a tax
we pay for determinism here. It is the thing that makes a realistic DAG runnable at
all.

**And the A/B determinism test re-run on this failing, retrying DAG [E16]** — two
independent containers, full column dump, including `task_instance_history` (the
per-attempt table, 36 columns) which the happy-path test never populated:

| table | rows | non-deterministic columns |
|---|---|---|
| `dag_run` (25 cols) | 1 | **none** — `state='failed'`, `run_id='manual__2026-03-02T01:00:00+00:00'` |
| `task_instance` (37 cols) | 2 | `duration`, `end_date`, `start_date` — **on 1 row only**, the one that actually executed. `downstream` (`upstream_failed`, never ran) is fully deterministic |
| `task_instance_history` (36 cols) | 2 | `duration`, `start_date` only — `end_date` was deterministic here |

`flaky` came back `try_number=3, max_tries=2, failed` with both earlier attempts in
history at `try_number` 1 and 2, identically, in both containers. **The residual
non-determinism is confirmed to be exactly "wall-clock measurements of work that
really ran", and nothing else.**

Confirmation of the mechanism, from the same dump: `downstream`, which never
executed (`upstream_failed` is set by Airflow's core-side loop, not by the Task
SDK), carries a **virtual** timestamp `2026-03-02 01:01:40`, while `flaky`, which
did execute, carries wall-clock stamps from the Task SDK. Exactly the split §2.4
predicted.

**Two implementation warnings that follow directly, and are not optional:**

1. **The virtual clock must advance, not freeze.** A `vnow()` that returns a
   constant makes `end_date + retry_delay` never arrive and `dag.test` spins
   forever. Monotone-per-call, as in the §2.4 shim.
2. **Never promote these derived virtual timestamps to the time authority.** How
   much virtual time a run consumes is a function of *how many times Airflow
   happened to call `utcnow()`* — deterministic for a fixed code path (that is why
   the A/B dumps matched), but it will change on any Airflow upgrade. `vnow` after
   `advance` must stay exactly `start + hours`, from our ledger, as it is today.
   The Airflow-side timestamps are decoration on the catalogue.

**Conclusion for §2:** "Airflow's clock fights ours" is false on the execution
path. It is true only of the scheduler daemon, which we do not need — and on the
retry path our clock does not merely coexist with Airflow's, it rescues it.

---

## 3. Airflow 3 vs Airflow 2 — they differ enough to change the design

All measured today on the two images **[E3, E5, E6, E11, E12]**.

| | **2.10.5** | **3.0.2** |
|---|---|---|
| `schedule="0 1 * * *"` builds | `CronDataIntervalTimetable` | `CronTriggerTimetable` |
| manual data interval for `logical_date=2026-03-02T01:00` | `(2026-03-01T01:00, 2026-03-02T01:00)` — a real 24h window | `(2026-03-02T01:00, 2026-03-02T01:00)` — **degenerate** |
| `dags test` run_id | `manual__2026-03-02T01:00:00+00:00` | `manual__<wall clock, µs>` |
| `DAG.test` params | `execution_date` | `run_after`, `logical_date` |
| `DAG.iter_dagrun_infos_between` | present | **removed** |
| `airflow dags list` source | parses the DAG folder | reads the metadata DB (needs `reserialize` or the dag-processor); `--local` parses |
| `airflow tasks clear` on an unregistered DAG | — | `AirflowException: Dag 'revenue_daily' could not be found` until `reserialize` |
| REST API | `/api/v1`, OpenAPI 3.0.3, **62 paths**, title `Airflow API (Stable)` | `/api/v2`, OpenAPI 3.1.0, **72 paths**, title `Airflow API 2` |
| backfill CLI | `airflow dags backfill` | `airflow backfill create --dag-id --from-date --to-date` [R1, 3.3.1 docs] |
| task execution | in the worker process | Task SDK + supervised subprocess over a Task Execution API (`http://in-process.invalid./…` under `dags test`) **[E3]** |
| `/users`, `/roles`, `/permissions` endpoints | present | **gone** (auth manager replaced) |

**The `data_interval` difference is the one that will bite an agent.** In Airflow 2
a data engineer's mental model is "`ds` is yesterday's partition and
`data_interval_end` is when the window closed". In Airflow 3, with a plain cron
schedule, `data_interval_start == data_interval_end == logical_date`. Any DAG code
we ship that partitions on `data_interval_start` behaves differently on the two
majors, and an agent that learned one will write wrong code against the other.

**Which major to target.** From the Airflow user survey CSV, 5,856 responses
**[R4]**, "Which version of Airflow do you currently use?":

```
2.8 – 2.11+   2327  (42.1%)
3.0 – 3.1     1445  (26.1%)
2.4 – 2.7      955  (17.3%)
2.0 – 2.3      582  (10.5%)
1.x            218  ( 3.9%)
```

**≈70% of respondents are on Airflow 2.x.** Also, "active DAGs in your largest
instance": `11-50` 26.1%, `51-250` 23.0%, `<5` 16.5%, `5-10` 16.1% — i.e. **two
thirds run 250 DAGs or fewer**, which is worth knowing because it says a world with
3–8 DAGs is small but not absurd; 1 DAG is absurd.

**Provenance caveat on [R4]:** this CSV was already on this machine, in a scratchpad
directory from an earlier session, not fetched by me in this session. The header row
matches the Airflow user survey's published question set, and the Astronomer press
release for *State of Apache Airflow 2026* (fetched in this session) states the
survey ran 2025-09-15 → 2025-11-20 with "over 5,800 data practitioners", which
matches the row count. I am confident it is what it says it is, but I did not
download it from an official URL today. **Treat the percentages as strong, not
certain.** If the version split is going to drive a build decision, re-download the
CSV first.

**Recommendation on the major: build the bridge version-agnostic, ship Airflow 3
DAGs by default, and keep at least one theme on 2.x idioms.** The 70/30 split says
an agent that has only ever seen Airflow 3 will meet Airflow 2 in the field more
often than not; and the divergences above are precisely the kind of thing a
training substrate should be exercising, not hiding.

---

## 4. What `airflow tasks clear` actually does — and why a naive fake gets it wrong

**[E8]**, measured. After a successful `dags test` run, with **no scheduler
running**:

```
$ airflow tasks clear -y revenue_daily -s 2026-03-01 -e 2026-03-03
$ airflow tasks states-for-dag-run revenue_daily <run_id> -o json
  daily_load  state: null
  dbt_build   state: null
$ airflow dags list-runs revenue_daily
  manual__…  queued           # the DagRun was reset from success to queued
$ sleep 8 ; states again
  daily_load  state: null     # nothing re-ran. nothing will.
```

**Clear is a mark, not a run.** It sets `task_instance.state = NULL` and resets the
DagRun to `queued`. The scheduler is what later notices and re-queues. A data
engineer's lived experience of clearing a task is: click clear → the squares go
white → a few seconds later they go light-green (queued) → then running → then
green or red.

This is the highest-value divergence in the whole document, so name it plainly:

> **A fake that re-executes synchronously on `tasks clear` teaches the agent that
> clearing is a blocking call that returns a result. Against a real Airflow, that
> agent will read the response, see no state change yet, and conclude the clear
> failed — then clear again, and again. It will never learn to poll.**

The honest mapping is the one real Airflow itself uses, and we already have the
scheduler it needs: **`clear` marks; the next `advance()` runs.** Our `advance` *is*
the scheduler loop. This is not a compromise; it is the same semantics with the
latency made explicit and deterministic instead of implicit and flaky.

---

## 5. What a data engineer actually does with an orchestrator in a working week

Ranked by frequency (my ordering, from the shape of the job — **I have no dataset
that ranks these and I am not going to invent one**), against measured cost to
support honestly under the recommendation in §7.

| # | Action | Frequency | Cost to support honestly | Notes |
|---|---|---|---|---|
| 1 | **Read the DAG code** to work out what a pipeline does | several times daily | **zero** — REPO is shipping `dags/*.py` this round | already in the workspace tree, therefore fingerprinted |
| 2 | **Look at the grid** — which squares are red, since when | several times daily | **low** — one endpoint, `GET /ui/grid/{dag_id}`, plus Airflow's own 4.0MB `ui/dist` React bundle **[E10, E14]** | see §7.4; this is far cheaper than I expected |
| 3 | **Read a failed task's log** | several times daily | **medium** — see the log trap in §7.5 | we already keep raw engine output in `build/sched/logs/` |
| 4 | **Clear a task and re-run downstream** | daily | **low-medium** — `clearTaskInstances` + `include_downstream`; the mark/run split of §4 | the single most instructive interaction in the set |
| 5 | **Trigger a DAG manually with a conf** | daily | **low** — `POST /dags/{id}/dagRuns` already takes `logical_date`, `dag_run_id`, `conf` **[E11]** | maps onto our virtual clock exactly (§7.3) |
| 6 | **Check why a task is stuck / not scheduled** — pool full, upstream not done, DAG paused, sensor waiting | weekly | **medium-high** — needs `taskInstances/{id}/dependencies`, pools, paused state, and a sensor that can actually be pending | honest answer for R004: ship pools + paused + dependencies; **do not** fake a poke-mode sensor |
| 7 | **Backfill a date range** | weekly | **medium** — `airflow backfill create` / `POST /api/v2/backfills` | falls out of §7.3 almost free, because catchup is already how our `advance` enumerates |
| 8 | **Edit a schedule** (cron string, catchup, retries) | weekly-ish | **zero-to-low** — it is a DAG file edit, and §2.3 means the timetable change takes effect | the payoff for using Airflow's real timetable instead of our YAML |
| 9 | **Debug a dependency** — reorder tasks, add a trigger rule | weekly | **zero** — Airflow's own task loop enforces it | |
| 10 | **Add/rotate a Connection or Variable** | monthly | **low** — `/connections`, `/variables` are plain CRUD **[E11]** | high archaeology value (§7.6) |

Items 1, 8 and 9 are free *only* under the "run the real DAG" design. Under a fake
that projects our YAML into Airflow-shaped JSON, they are the three most expensive
things on the list and will never be right.

The survey CSV **[R4]** has a multi-select of areas respondents named for
improvement: `Web UI` 1938, `Examples/how-to/documentation` 2176, `Local developer
experience` 1628, `Logging, monitoring and alerting` 1514, `Testing` 1435,
`REST API` 746. I cannot recover the exact question stem from the CSV header, so I
am not reading intent into it beyond "the UI and logs are what people are most
conscious of". It is consistent with ranking the grid and the logs at #2 and #3,
and that is all I will claim from it.

---

## 6. Dagster and Prefect

Researched with source verification; URLs below were fetched.

**Dagster** is, on the merits, an excellent fit — arguably a better one than
Airflow on determinism specifically:

- `materialize(assets, instance=…, partition_key=…)` and
  `JobDefinition.execute_in_process(...)` run synchronously with no daemon and an
  ephemeral instance if none is given
  ([docs.dagster.io/api/dagster/execution](https://docs.dagster.io/api/dagster/execution)).
- **`dagster.partition_loading_context(effective_dt=…)`** is a `@public` context
  manager that overrides "now" for *all* partition resolution — a documented,
  supported virtual-clock injection point, which Airflow does not have an
  equivalent of ([source](https://github.com/dagster-io/dagster/blob/master/python_modules/dagster/dagster/_core/definitions/partitions/context.py)).
- `build_schedule_context(scheduled_execution_time=…)` + `schedule.evaluate_tick(ctx)`
  gives the `RunRequest`s for an arbitrary virtual instant with zero wall-clock
  reads ([source](https://github.com/dagster-io/dagster/blob/master/python_modules/dagster/dagster/_core/definitions/schedule_definition.py)).
- Run IDs are `uuid4` (`make_new_run_id()`), but `DagsterRun` accepts an explicit
  `run_id`, so they are injectable ([source](https://github.com/dagster-io/dagster/blob/master/python_modules/dagster/dagster/_core/storage/dagster_run.py)).
- Telemetry off with `telemetry: {enabled: false}` in `dagster.yaml`
  ([docs.dagster.io/about/telemetry](https://docs.dagster.io/about/telemetry)).
  `dagster` wheel is **2.0 MB** ([pypi](https://pypi.org/project/dagster/#files)).

**Prefect 3 is disqualified for a hermetic world.** From
[`src/prefect/client/orchestration/__init__.py`](https://github.com/PrefectHQ/prefect/blob/main/src/prefect/client/orchestration/__init__.py), verbatim:

```python
if not api and PREFECT_SERVER_EPHEMERAL_ENABLED:
    from prefect.server.api.server import SubprocessASGIServer
    server = SubprocessASGIServer(); server.start()
elif not api and not PREFECT_SERVER_EPHEMERAL_ENABLED:
    raise ValueError("No Prefect API URL provided. …")
```

There is no "just call the function" mode: every flow run either forks a uvicorn
subprocess on a TCP port or raises. Every server ORM row carries `default=uuid.uuid4`
and `default=lambda: now("UTC")` ([source](https://github.com/PrefectHQ/prefect/blob/main/src/prefect/server/database/orm_models.py)),
and `FlowRunCreate` has no `id` field, so the IDs are not injectable. Three separate
telemetry switches must be turned off (`DO_NOT_TRACK=1`,
`PREFECT_SERVER_ANALYTICS_ENABLED=false`,
`PREFECT_TELEMETRY_ENABLE_RESOURCE_METRICS=false`). Wheel is **15.6 MB**.

**And yet: Dagster does not win, on mindshare.** Stars fetched via `gh api`
today: `apache/airflow` 46,855 / `PrefectHQ/prefect` 23,841 / `dagster-io/dagster`
16,150. More decisively, ~70% of surveyed practitioners are running Airflow **2.x**
**[R4]** — an agent fluent in Dagster assets is fluent in a tool most of the
market does not run. We sell a substrate for training agents that must work in real
data teams; Airflow is what real data teams have.

**One more fact that changed today's calculus:** on **2026-07-13 Prefect acquired
Dagster Labs** ([prefect.io/prefect-acquires-dagster](https://www.prefect.io/prefect-acquires-dagster),
fetched by me). The page says "Dagster and Dagster+ are here to stay… fully
committed to their long-term maintenance" and does **not** address the OSS license
or roadmap. That is reassuring but non-binding. Building our orchestration fidelity
on the newly-acquired smaller product, against the Apache-Foundation-governed larger
one, would be a poor bet independent of the technical merits.

**Verdict:** Airflow. Keep Dagster as a *second theme* later — a world where the
shop runs Dagster is a legitimate and cheap variant once the bridge pattern exists,
and `partition_loading_context` makes it the easiest one to build. Not this round.

---

## 7. The recommendation — "headless Airflow", and the build

### 7.1 The ranking

| # | Option | Verdict |
|---|---|---|
| **1** | **Headless real Airflow.** Airflow as a *library*, driven by our `advance()`; real DAG parsing, real timetables, real task loop; `var/sched/state.json` stays the time authority; real `airflow` CLI and real `api-server` over the real metadata DB | **Recommended.** Highest fidelity ceiling, and the only option where an edited DAG genuinely changes behaviour |
| **2** | **Fake REST API + CLI shim** over our existing JSON scheduler | **Fallback, and a component of #1's endgame.** Cheaper, capped fidelity, and its cap sits exactly on the three highest-frequency actions (§5 #1, #8, #9) |
| **3** | **Full Airflow deployment** — scheduler + api-server + triggerer + Postgres, real daemons | **Reject.** Fights the virtual clock at 11+ `utcnow()` sites in `scheduler_job_runner` alone, adds a heartbeat loop and DB-time comparisons, and buys nothing over #1 except the ability to wait |
| **4** | **Dagster** | Technically excellent, strategically wrong for now (§6) |
| **5** | **Prefect** | Disqualified — no daemon-free mode (§6) |

Options 1 and 2 are **not exclusive** and should not be treated as a fork in the
road. #1 gives you real semantics; #2 is how you fill in whichever endpoints #1's
metadata DB does not naturally populate. Build #1; reach for #2 for specific gaps.

### 7.2 Where it sits

```
agent
 │  `airflow dags list|test|tasks clear|backfill create`   (the REAL CLI)
 │  http://localhost:8080/api/v2/…  +  /ui/grid/…          (the REAL api-server + UI)
 ▼
Airflow metadata DB  ── the catalogue and the marks (cleared TIs, paused, conf, vars)
 ▲                                            │
 │ projection                                 │ intents
 │                                            ▼
 └──────────  world/world/airflow_bridge.py  ─────────────┐
                    │                                     │
                    │ timetable.next_dagrun_info()        │ under vclock.py
                    │   → which (dag, logical_date) are due in (vnow, vnow+h]
                    │ dag.test(run_after=ld, logical_date=ld)
                    │   → Airflow's own task loop runs the REAL work
                    ▼                                     ▼
       var/sched/state.json  (FROZEN, fingerprinted)   real dbt / loaders / DuckDB
             the single time authority
```

**The load-bearing rule: one ledger.** `var/sched/state.json` stays the time
authority and the run record — its shape is frozen (`agent.md` 9.2), three built
tasks depend on it, the fingerprint hashes it, and `sched_*` returns it unchanged.
The Airflow metadata DB is a **derived catalogue plus an intent queue**, never a
second source of truth about what executed. Two consequences worth stating:

- If the Airflow DB drifts, we **rebuild** it from the ledger. We do not fail a
  determinism gate on it.
- We do **not** add the Airflow DB to `fingerprint.semantic()`. It is a cache.
  (Which also means it is un-gated — see §9, this is a real risk and I name it.)

### 7.3 The `advance()` loop, concretely

```python
# world/world/airflow_bridge.py   (~400–600 lines, new file, new owner)

def due(live, state, start, target):
    """Which (dag_id, logical_date) fall in (start, target] — asked of Airflow."""
    for dag in dagbag(root / "workspace" / "dags").dags.values():
        last = last_automated_interval(state, dag.dag_id)      # from OUR ledger
        r = TimeRestriction(earliest=dag.start_date, latest=None, catchup=True)
        while (info := dag.timetable.next_dagrun_info(
                   last_automated_data_interval=last, restriction=r)):
            if info.run_after > target: break
            if info.run_after > start:  yield dag, info
            last = info.data_interval

def run_one(root, dag, info, state):
    with vclock.at(info.run_after):                 # §2.4 shim, scoped
        dag.test(run_after=info.run_after, logical_date=info.logical_date)
    # dag.test wrote dag_run + task_instance; we read them back and append
    # ONE run record per task to state["runs"], with vts from the virtual clock
    # and duration_s from the theme manifest — never from the wall clock.
```

Three details that are not optional:

1. **Always pass `logical_date`.** A `None` logical_date makes the run_id carry
   `get_random_string()` **[E5]**. That is the one way to lose determinism by
   accident.
2. **`run_after = logical_date`** on scheduled runs. This is what makes the run_id
   `scheduled__2026-03-02T01:00:00+00:00` rather than a wall-clock string, and it
   is also what our ledger's `vts_start` already is.
3. **`catchup=True` in the `TimeRestriction` we construct**, regardless of what the
   DAG declares, because that is the branch with no `utcnow()` **[E13]**. If a
   theme's DAG says `catchup=False`, honour that by *truncating the enumeration to
   the latest due run*, not by taking Airflow's `catchup=False` code path.

**The bridge to the real work.** REPO's DAG file already uses
`BashOperator(bash_command="cd /workspace && ./run.sh load")`. That is the right
seam and it should stay: the DAG says what runs, `run.sh` is what runs, and
`theme.yaml`'s `exec:` blocks become `run.sh` subcommands rather than a parallel
universe. One consequence to accept deliberately — **the theme manifest's `jobs:`
schedule stops being the schedule.** The DAG file becomes the schedule, and
`live.json`'s `jobs` keeps only the `exec` mapping and `duration_s`. Anything else
means two schedules that can disagree, and the agent editing one of them.

**This is also where I expect the design to be hardest — read §10 before
estimating it.** Our `after: {job: daily_load, minutes: 5}` has no Airflow
equivalent: in Airflow that is a task dependency, and the downstream task starts
when the upstream *finishes*, not five minutes later. So `after.minutes` and
`duration_s` stop being schedule inputs and become **recorded-window metadata
only** — what `initial_state` and the `job_overrun` fault need in order to draw a
plausible window. Validating that reinterpretation against all four themes is the
riskiest piece of work in the plan, and it is why §9 puts a read-only agreement
check in front of everything else.

### 7.4 The surface — and the cheapest surprise in this document

`airflow api-server` **[E9]**: cold boot on `--network none`, SQLite,
`AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS=True`, from `db migrate` to a
`200` on `/api/v2/monitor/health`: **10.45 seconds**. Health body, verbatim:

```json
{"metadatabase":{"status":"healthy"},
 "scheduler":{"status":"unhealthy","latest_scheduler_heartbeat":null},
 "triggerer":{"status":null,…},"dag_processor":{"status":null,…}}
```

(`scheduler: unhealthy` is correct and should stay — it is honest, and an agent
that learns to read that field learns something true.)

**The grid view is one endpoint.** Airflow 3's React UI is a **4.0 MB** static
bundle at `airflow/ui/dist` backed by exactly **11** private `/ui/*` endpoints
**[E10, E11, E14]**:

```
/ui/grid/{dag_id}                    ← the grid
/ui/structure/structure_data         ← the graph view
/ui/dags/recent_dag_runs
/ui/dashboard/dag_stats
/ui/dashboard/historical_metrics_data
/ui/dependencies   /ui/next_run_assets/{dag_id}
/ui/config   /ui/auth/menus   /ui/backfills   /ui/connections/hook_meta
```

Real `GET /ui/grid/revenue_daily` response, measured:

```json
{"dag_runs":[{"dag_run_id":"manual__…","state":"success","run_type":"manual",
  "logical_date":"2026-03-02T01:00:00Z","data_interval_start":"…","data_interval_end":"…",
  "task_instances":[{"task_id":"daily_load","try_number":1,"state":"success",
     "child_states":{"failed":0,"upstream_failed":0,"up_for_retry":0,
        "up_for_reschedule":0,"queued":0,"scheduled":0,"deferred":0,"running":0,
        "restarting":0,"no_status":0,"success":1,"skipped":0,"removed":0},
     "task_count":1,"note":null}, …]}]}
```

Under the recommendation we get all of this by **running the real api-server against
the real metadata DB** — zero endpoints implemented. This is the argument that
settles #1 over #2: the fake option has to write 11 + ~25 endpoints to get a grid;
the real option has to write none.

**Write endpoints already fit our virtual clock.** From the shipped 3.0.2 spec
**[E11]**, `TriggerDAGRunPostBody` — `required: ['logical_date']`, and it also
accepts `dag_run_id`, `data_interval_start`, `data_interval_end`, `run_after`,
`conf`, `note`. A caller-supplied `dag_run_id` and `logical_date` is exactly what a
virtual clock wants to send. `ClearTaskInstancesBody` carries `dry_run`,
`start_date`, `end_date`, `only_failed`, `only_running`, `reset_dag_runs`,
`task_ids`, `dag_run_id`, `include_upstream/downstream/future/past`.

### 7.5 The log trap — read this before estimating

**[E10]** `GET /api/v2/dags/{id}/dagRuns/{rid}/taskInstances/daily_load/logs/1`
returned, verbatim:

```json
{"content":[{"event":"::group::Log message source details","sources":[
 "Could not read served logs: HTTPConnectionPool(host='84a67173c3e2', port=8793):
  Max retries exceeded … Failed to resolve '84a67173c3e2'"]},
 {"event":"::endgroup::"}],"continuation_token":null}
```

Airflow serves task logs **over HTTP from the worker**, addressed by the task
instance's `hostname`, on port 8793. On a hermetic network that does not resolve,
and the fallback to the local `base_log_folder` did not produce content either.

**This is why the shim sets `hostname` to a constant** (§2.4) — but that alone is
not enough. Budget real time for making log retrieval work off the local filesystem
(`[logging] base_log_folder`, and running the api-server in the same container as
the executor so the path is shared). "Read a failed task's log" is #3 on the
frequency list; if it returns a `NameResolutionError` string, the whole surface is
a demo.

### 7.6 Ship list, in order

1. `world/world/vclock.py` — the shim of §2.4, plus `vclock.at(vts)` as a context
   manager and a `frozen()` assertion for tests.
2. `world/world/airflow_bridge.py` — `due()`, `run_one()`, ledger projection, and
   intent consumption (`clear` → re-run on next `advance`).
3. `run.sh` in each theme workspace, mapping DAG task_ids to the existing `exec:`
   kinds. Coordinate with REPO — the DAG files are theirs, `run.sh` is the seam.
4. Metadata DB rebuild-from-ledger, so a restored snapshot has a coherent Airflow
   catalogue. Postgres is already in compose; SQLite is fine and lighter, and I
   measured everything on SQLite.
5. `airflow api-server` in the episode container: `--network none`-safe, auth
   disabled via `SIMPLE_AUTH_MANAGER_ALL_ADMINS`, `JWT_SECRET` **pinned to a
   constant** (it is generated otherwise, which is another determinism leak).
6. Local log plumbing (§7.5).
7. Acceptance: `advance(1)×N == advance(N)` with the Airflow path on, across all
   four themes; DAG-edit test (append a task, `advance`, assert it ran);
   DAG-break test (assert `import_error` and a failed `advance` that says why).
8. Image: `apache-airflow` core only, no provider extras. **Measured [E15]:
   `pip install apache-airflow==3.0.2 --constraint constraints-3.11.txt` into
   `python:3.11-slim` — our exact base — gives 210 MB of site-packages across 129
   packages, and imports cleanly on 3.11.** Do **not** start from
   `apache/airflow:3.0.2` (2.53 GB **[E1]**): its 1.4 GB `site-packages` is
   *providers*, not Airflow — `airflow/` itself is 35 MB, while `google` alone is
   254 MB, `pyarrow` 130 MB, `scipy` 91 MB, `azure` 80 MB, `mysql` 69 MB,
   `sklearn` 42 MB **[E14]**. Our image is 961 MB–1.36 GB today against a <2 GB
   gate, so **+210 MB fits**.

---

## 8. Divergence analysis — where each option teaches something false

This is the section the user asked for above the recommendation, so it is the
longest. Each row: what diverges, and **what a model would learn there that is
wrong**.

### 8.1 Headless real Airflow (the recommendation)

| Divergence | What the agent would learn that is false | Mitigation |
|---|---|---|
| **No scheduler daemon.** Nothing happens between `advance()` calls. | That an orchestrator is synchronous — that after a trigger, the next read shows the result. In production you trigger and wait; runs sit `queued` behind pool slots and concurrency for minutes. | **Do not hide it.** `/api/v2/monitor/health` already reports `scheduler: unhealthy` **[E9]**; leave it. Make `clear`/`trigger` write a `queued` row that only the next `advance` resolves (§4), so the agent must observe the queued→running→success transition even though the wall time is zero. |
| **`start_date`/`end_date`/`duration` on task instances are virtual.** Real ones measure real seconds. | That durations are stable and exactly `duration_s`. An agent trained here will not learn that "this task usually takes 4 minutes and today took 40" is the signal. | The fault layer already models `job_overrun` with a stretched recorded window. Project that into `task_instance.duration` so durations *do* move, deterministically. This converts a divergence into a feature. |
| **One worker, one process, no executor.** No `queued`-behind-pool, no `LocalExecutor` fan-out, no zombie detection, no task_instance heartbeat timeouts. | That concurrency and pools are decorative. Real on-call is substantially "why is nothing running" → pool exhausted / max_active_runs / scheduler behind. | Model pools and `max_active_runs` in the bridge's own admission check and reflect them in the DB. Accept that zombie/heartbeat semantics are out of scope and say so in the world README rather than faking them. |
| **Log retrieval is local-file, not served-logs.** | Nothing false, as long as it *works*. If it returns the `NameResolutionError` body I measured **[E10]**, the agent learns that log-reading fails, which is worse than false — it is a dead surface. | §7.5. Gate it in acceptance. |
| **No triggerer, so no deferrable operators / `@task.sensor` reschedule.** | That sensors are simply blocking. Deferrable operators are increasingly the idiom, and "the sensor is deferred, not stuck" is a real diagnosis. | Out of scope for R004. Do not ship a DAG that uses a deferrable operator, because a deferrable operator that silently runs in poke mode is a *wrong* fake, not an incomplete one. |
| **`catchup=True` forced in our `TimeRestriction`** even when the DAG says `False` (§7.3). | If done carelessly: that `catchup=False` still backfills. That is a real and consequential production behaviour to get wrong. | Enumerate with `catchup=True` for clock-freeness, then **discard all but the latest due run** when the DAG declares `catchup=False`. Assert this in a test, with a DAG that declares each. |
| **Airflow 3's degenerate data interval** (§3) if we ship 3.x DAGs. | Real, not our artifact — but an agent that only ever sees 3.x learns 3.x's `ds`/`data_interval` model and will write wrong partition logic against the ~70% of installs on 2.x **[R4]**. | Ship at least one theme on 2.x idioms. This is a curriculum decision, not a bug. |

**Net: the false lessons under this option are all about *time and concurrency*,
not about *semantics*.** The agent learns correct Airflow semantics — DAG parsing,
dependencies, trigger rules, timetables, clearing, templating — and an
optimistically fast world. That is the right trade for a training substrate, and
it is the same trade every test fixture makes.

### 8.2 Fake REST API + CLI shim over our JSON scheduler

| Divergence | What the agent would learn that is false |
|---|---|
| **Editing a DAG file does nothing.** The projection reads `theme.yaml`, not `dags/*.py`. | **The worst one.** That DAG code is documentation. The agent will "fix" a pipeline by editing YAML, or by editing the DAG and reporting success without verifying, and will have learned an anti-pattern that is *specifically rewarded* here and punished everywhere else. |
| **Changing a cron string does nothing.** | That schedules are inert. #8 on the frequency list. |
| **Adding a task or reordering dependencies does nothing.** | That dependency edits are inert. #9 on the list. |
| **Task/run identity is ours, not Airflow's.** Our runs are keyed `(job, vts_start)`; Airflow's are `(dag_id, run_id, task_id, map_index, try_number)`. | Any habit built on our key shape transfers wrongly. Mapped tasks (`map_index`) do not exist at all in our model. |
| **We invent response bodies.** I measured the real ones **[E10]** and they carry `dag_versions[]`, `bundle_name`, `bundle_version`, `relative_fileloc`, `is_stale`, `file_token` (a signed token), `rendered_fields`, `child_states{13 keys}`. | Every field we omit is a field the agent learns not to look for. `dag_versions` in particular is how Airflow 3 answers "which version of the code did this run use" — a genuinely important question we would be erasing. |
| **The grid needs 11 `/ui/*` endpoints reimplemented** to serve Airflow's own bundle, or a UI of our own that is *visibly not Airflow*. | Either a maintenance treadmill against a private, unversioned API, or an agent that has never seen the thing it will actually be looking at. |

**The fake's fidelity ceiling is exactly this:** it can be perfect for *reading*
run history and *triggering* — items 2, 3, 5, 7, 10 on the frequency list — and it
is structurally incapable of items 1, 8, 9, which are the top-frequency and the
most characteristic. That is not a ceiling I would accept as the primary design,
and it is why I am not recommending it despite it being cheaper.

Where it *is* right: as a **completion layer** on top of #1, for endpoints the
metadata DB does not populate because we have no daemon
(`/api/v2/jobs`, triggerer health, `eventLogs`). Filling those by projection from
the ledger is correct and cheap.

### 8.3 Full Airflow deployment (rejected)

| Divergence | What it costs |
|---|---|
| `SchedulerJobRunner` reads `timezone.utcnow()` at 11 sites I counted **[E4, grep]**, including `if dag_run.logical_date and dag_run.logical_date > timezone.utcnow()` (line 1881 — the scheduler **refuses to start runs whose logical_date is in the future**, which under a virtual clock set to 2026-03 and a host clock at 2026-09 is a permanent silent stall), plus heartbeat timeouts, `queued_dttm` staleness sweeps and `dagrun_timeout` comparisons against DB-side times. | Every one of these must be patched consistently or the scheduler makes decisions on host time. |
| Wall-clock latency re-enters. `advance(720)` would take 720 real hours, or need the clock to jump, which reintroduces every stall/timeout path above. | Kills the product property that `advance` is instantaneous. |
| Heartbeats write rows continuously → the world is never quiescent. | Breaks acceptance gate 5 (fingerprint, idle 5s, re-fingerprint must be equal). |

Rejected. It buys only "the agent can wait and see something change on its own",
which the fault layer already provides in a deterministic form.

---

## 9. Costs

Engineer-days, one competent engineer, one theme end-to-end then generalised.

| Option | Build | Determinism risk | Offline | Maintenance | Container |
|---|---|---|---|---|---|
| **1. Headless Airflow** | **18–22 d** (vclock 1, bridge 5, run.sh+theme mapping 3, intents 2, DB rebuild 2, api-server+compose 2, logs 2, acceptance 3, image 2) | **Medium.** The 3 residual TI columns are known and normalisable (§2.4); the real risk is that the Airflow DB sits **outside** `fingerprint.semantic()` and can drift undetected | **Proven** — `--network none`, 10.45 s boot **[E9]** | **Medium-high.** Pinned to one Airflow minor; `dag.test`'s internals and the Task SDK are the two upgrade hazards | **+210 MB measured [E15]** — core-only pip install on `python:3.11-slim`, our exact base. Fits the <2 GB gate |
| **2. Fake API + CLI shim** | 10–14 d for the ~25 endpoints that carry daily value; **+8–12 d** more if the real UI bundle is served (11 `/ui/*`) | **Low** — it is our JSON, already fingerprinted | Trivially | **High and permanent** — every Airflow minor moves the target, against a spec we do not own | +~0 |
| **3. Full deployment** | 30 d+ and an unbounded tail | **High** | Yes, but heartbeats break quiescence | High | +2.5 GB image, 3 processes |
| **4. Dagster (headless)** | 12–16 d — genuinely less than Airflow, thanks to `partition_loading_context` | **Low-medium** | Yes, one telemetry key | Medium | +~150 MB est. |
| **5. Prefect** | — | High | **No** — forks a TCP server per run | — | — |

**Sequencing suggestion.** #1 is a four-to-five-week item for one engineer and it
does not have to land whole. A defensible R004/R005 split:

- **R004 (now):** REPO's DAG files land as artifacts. Add `vclock.py` and a
  read-only `airflow_bridge.due()` that asserts *Airflow's timetable and our
  `job_starts()` schedule agree* on all four themes. That is 3–4 days, it proves
  the central claim of this document against our real themes, and it fails loudly
  if I am wrong.
- **R005:** execution, intents, api-server, logs.

---

## 10. Gaps and the weakest finding

**Not tested, and I want this on the record:** the real `airflow scheduler`
daemon under the shim; Postgres as the backend (everything here is SQLite);
Airflow 3.1/3.3 (the `stable` docs are 3.3.1, I ran 3.0.2); deferrable operators and
the triggerer; sensors in reschedule mode; mapped tasks (`map_index != -1`);
`depends_on_past`; `airflow backfill create` end-to-end.

### My weakest finding

The one I had lined up — "does the shim survive a retry?" — I went and measured
(§2.5), and it does. So the weakest claim left is a structural one, and it is
worse than the one I replaced.

**I measured components. I never measured the composition, against our actual
themes.** Every number in this document comes from a two-task toy DAG I wrote for
the purpose. I have not run `timetable.next_dagrun_info` → `dag.test` → ledger
projection against `revenue_daily`, `sftp_vendor`, `mainframe_batch` or
`excel_close` even once. The §7.1 ranking rests on the assumption that those parts
compose, and I can point at the seam where I most expect them not to:

**Our schedule model and Airflow's do not have the same shape.** `theme.yaml`
gives every job its own schedule slot, either absolute (`time: "01:00"`,
`day_offset: 1`) or **relative to another job** (`after: {job: daily_load,
minutes: 5}`). Airflow has no equivalent of the second form. In Airflow,
"5 minutes after daily_load" is not a schedule at all — it is a task dependency
inside one DAG, and Airflow runs the downstream task **as soon as the upstream
finishes**, not five minutes later. REPO's DAG file already makes the right call
(`daily_load >> dbt_build`, one DAG, one `schedule="0 1 * * *"`), which means:

- `after.minutes` has **no representation** in the DAG and silently disappears;
- `duration_s` likewise — Airflow's task takes however long the real work takes;
- our four themes' `jobs:` maps to *fewer DAGs than jobs*, and the mapping is
  per-theme judgement, not a mechanical transform.

Those three facts are load-bearing for our seeded history (`initial_state` lays out
run windows using exactly `after.minutes` and `duration_s`) and for the fault layer
(`job_overrun` stretches a recorded window *over the next occurrence*, which is
only meaningful because occurrences have declared start times). **A bridge that
delegates scheduling to Airflow's timetable may not be able to reproduce the
world's own seeded history**, and if the history and the live schedule disagree,
the agent is looking at an incoherent past — which is a defect of the kind this
whole document is about.

I do not think this is fatal — the resolution is probably "the DAG owns the
occurrence start time, `after.minutes`/`duration_s` become recorded-window
metadata only" — but I have not verified it, it touches the frozen `state.json`
shape, and it is the first thing that would blow up the §9 estimate. **Hence the
R004 proposal in §9: spend 3–4 days on a read-only `due()` that asserts Airflow's
timetable and our `job_starts()` produce the same occurrence list on all four
themes, before committing to anything else.** If that assertion fails, this
document's recommendation needs revisiting, and better to learn it in four days
than in four weeks.

**Second-weakest:** the survey percentages in §3 and §5 rest on a CSV I found
locally rather than downloaded today (§3 provenance caveat). The version split
drives the "ship a 2.x theme too" recommendation, so re-verify it before that
becomes a build item.
