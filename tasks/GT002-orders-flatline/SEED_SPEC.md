# GT002 — seed spec (FROZEN CONTRACT for Track A)

**Task:** "Orders stopped arriving" — a stalled stream consumer, and an append-only sink that
has never duplicated anything and will the moment the stream is unblocked.
**Archetype:** A063 (`composed_of: A027, A026`). **Frozen 2026-09-13.**

Track A builds the world from this. Track B's `sql_*`, `fs_*`, `sched_*` and `docs_search`
tools address these names. Track C owns everything under `tests/`.

Amendments go through Track C. Do not rename anything below without telling me — the grader
addresses these identifiers literally.

---

## 0. Read this first: the property the task is made of

There are two defects. **One of them does not exist as an observable until the other is
repaired.**

- **Defect A — the stall.** Visible from four surfaces. Consumer lag is 24,411 and climbing,
  `fct_orders` stopped at 05:13 on the 9th, every scheduled run since says "0 records", and the
  sink's source shows a strict cast over a payload the topic cannot satisfy.
- **Defect B — the sink.** `pipelines/orders_sink.sql` is a plain `INSERT INTO … SELECT` with no
  merge key. **There is no duplicate row anywhere in this world.** Row counts reconcile. Every
  checksum is clean. Every duplicate-business-key assertion in the corpus returns zero rows,
  *correctly*. The only route to defect B is reading the sink and predicting what a replay will
  do.

**The standard, and it is stricter than "no duplicates exist":**

> **No query an agent would run to diagnose defect A may surface defect B's trigger.**

That bar exists because an earlier build failed it (**F624**). The redelivery was triggered by a
"loaded but never committed" overlap at the stall boundary, and because topic offsets were
assigned in event-time order, offset order *was* time order — so `MAX(event_ts)` in `fct_orders`
came out 29 minutes later than the consumer's `committed_at`, and the entire 1,300-record overlap
fell out of two read-only queries that an agent runs *anyway* while diagnosing the stall.
Diagnosing defect A handed over defect B's trigger for free, and the compound collapsed into two
ordinary bugs sharing a container.

`tests/test_dormancy.py` runs twelve probes against the shipped world on every build — every
hygiene check A063 names, every metadata reconciliation in the F624 family, both directions of
row-count reconciliation, the audit columns, a grep of every agent-visible file, and a positive
check that the trigger is still *there* — and `run_baselines.py` refuses to print TASK VALID if
any of them finds anything. Measured output is in §11.

---

## 1. The causal chain

```
producer build rolled back 2026-03-08, starts emitting qty as "2,0" (locale regression)
                    │
                    ▼
pipelines/orders_sink.sql casts qty strictly ──► the whole INSERT raises
                    │                              nothing written
                    ▼                              orders_stream_commit never runs
consumer group orders_fct_sink frozen at offset 4,126,000
                    │   DAG has on_failure_policy: ignore
                    │   every run since reports success, "0 records"
                    ▼
warehouse.fct_orders stops growing 2026-03-09 05:13     ← the visible symptom
                    │
                    ▼
the exec revenue board flatlines                         ← where the agent starts


            ── and dormant, underneath all of it ──

the SAME rollback made the producer resume from a stale checkpoint, so it re-sent
1,300 orders from 2026-02-04 that are already in fct_orders
                    │   their original log records aged out of the 30-day
                    │   retention window weeks ago, so the topic holds exactly
                    │   ONE copy and the table holds exactly ONE row
                    ▼
they are simply 1,300 of the 24,411 records pending in the gap
                    │
                    ▼
orders_sink.sql has no merge key, so replaying the gap writes them a second time
                    │
                    ▼           ← THIS HAS NOT HAPPENED. Nothing in the world
                    ▼              records that it is about to.
```

One rollback causes both: the bad payload that stops the pipeline, and the stale checkpoint that
makes restarting it dangerous. **The bug that broke the pipeline is also the bug that is keeping
the second one hidden** — nothing is being replayed, so nothing is being duplicated.

**The re-sends are not evidence of defect B.** They are pending records, which is what all 24,411
records in the gap are. Knowing they exist tells you a replay will redeliver something; it does
not tell you the sink will write it twice, because a correctly written sink would no-op them.
Conversely an agent who makes the sink idempotent never has to find them at all. That relationship
is the design: idempotency is what protects you from the thing you did not notice.

## 2. Virtual clock

- World "now": **2026-03-13T09:00:00Z**
- `fct_orders` history: **2025-12-14 → 2026-03-13** (90 days)
- Topic retention: **30 days** — the topic reaches back only to **2026-02-11**
- Last committed offset: 2026-03-09 **05:13:12** — the last poll window that did not contain a
  record the sink cannot decode
- First undecodable record: 2026-03-09 **13:45**, at offset 4,127,713 — inside the window that
  starts at the committed offset, which is why that window and every one after it writes nothing
- Re-sent February batch reaches the log: 2026-03-10 **02:00** onward

## 3. Schema

Warehouse file `/var/warehouse/warehouse.duckdb`, schemas `raw`, `staging`, `warehouse`.

**No new schema.** The streaming surface lives in `raw`, so §9.2.1's frozen schema list is
unchanged.

### `warehouse.fct_orders` — the table that stopped growing
| column | type | note |
|---|---|---|
| order_id | BIGINT | business key part 1 |
| revision | INTEGER | business key part 2 — **1 on first emission** |
| order_dt | DATE | date of the original order, stable across revisions |
| event_ts | TIMESTAMP | business time, **whole seconds only** — see §11 |
| customer_id | BIGINT | |
| channel_code | VARCHAR | joins `warehouse.dim_channel` |
| sku | VARCHAR | |
| qty | INTEGER | the column the poison payload cannot cast into |
| unit_price | DECIMAL(10,2) | |
| order_amount | DECIMAL(12,2) | `qty * unit_price` |
| load_ts | TIMESTAMP | audit col — EXCLUDED from all diffs |
| batch_id | VARCHAR | audit col — EXCLUDED from all diffs |

**Business key: `(order_id, revision)`.** Uniqueness holds everywhere, pre- and post-repair.
**There is NO source-offset column, and there must not be one** — a sink stamps the offset in
its checkpoint, not on the business row. Adding one would let a post-hoc cleanup de-duplicate on
the offset and defuse the entire ordering hazard.

### `raw.orders_topic` — the log
`offset_id BIGINT, event_ts TIMESTAMP, msg_key VARCHAR NULL, payload VARCHAR`

**Offsets are assigned by EMIT time, not by `event_ts`.** Emit time — when the record entered the
log — decides the offset and whether retention still holds the record, and it is deliberately
**not a column anywhere**. For almost every record the two are equal; ~1.5% arrive late (offline
clients syncing), and for the 1,300 re-sends they are five weeks apart. That separation is the
single thing that keeps the dormant defect dormant: offset order is not time order, so no
comparison of timestamps against offsets can say which records are about to arrive twice. Putting
an `emit_ts` column on this table would reintroduce F624 immediately.

Payload is JSON with every field serialised as a string or a number:
`{"order_id":700851168,"revision":1,"order_dt":"2026-03-09","event_ts":"2026-03-09T07:00:07",
"customer_id":19276,"channel_code":"CH-04","sku":"SKU-0037","qty":"2","unit_price":"73.58"}`

### `raw.consumer_offsets`
`consumer_group VARCHAR, topic VARCHAR, committed_offset BIGINT, committed_at TIMESTAMP, owner VARCHAR`

Two rows. `orders_fct_sink` is live and stalled, and its `committed_at` is exactly the emit time
of the last record below `committed_offset` — nothing in the table is past it and no timestamp
contradicts any other. `orders_archive_legacy` is a decommissioned group on the same topic whose
lag is 150,411 and stale — so "find the group with lag" returns
two candidates and the agent has to pick the one that feeds this table (F125 mitigation).

### `raw.topic_meta`, `raw.v_consumer_lag`
`topic, log_start_offset, log_end_offset, retention_days, partitions`; the view exposes
`lag = log_end_offset - committed_offset` per group.

### `raw.orders_dlq` — the dead-letter table
`offset_id BIGINT, event_ts TIMESTAMP, payload VARCHAR, error VARCHAR, dead_lettered_at TIMESTAMP`

Ships with **41 historic rows** whose offsets have aged out of retention. The quarantine step
works and has caught things before — which is what makes "why is the poison record not in here?"
a sharp question rather than a hint.

### `warehouse.dim_channel` — carries the decoy and its own repair hazard
`channel_code, channel_name, channel_group, is_curated BOOLEAN, updated_at`
Nine refreshed rows plus **four `is_curated = true` rows** the asset job does not produce.

### `warehouse.kpi_orders_daily` (view) — the symptom surface
`dt, channel_group, order_count, order_value`, joining `fct_orders` to `dim_channel`.

### `warehouse.pipeline_watermark`
`job, last_loaded_dt, updated_at`.

## 4. Scale, at seed 2029

| quantity | value |
|---|---|
| orders/day | 4,800 (one every 18 seconds) |
| late arrivals (emit time after event time) | ~1.5%, plus a 960-order backlog from a late-syncing client |
| `fct_orders` rows, broken world | **407,155** |
| `fct_orders` rows, repaired | **430,240** |
| topic offsets | 4,000,000 … 4,150,410 |
| `committed_offset` at start | **4,126,000** |
| consumer lag | **24,411** |
| first undecodable record | offset **4,127,713** |
| records produced in the gap | **24,411** |
| gap records that must load | **24,385** |
| records that must be dead-lettered | **26** (17 undecodable + 9 schema-invalid) |
| re-sends of already-loaded orders (the trigger) | **1,300**, from `order_dt` 2026-02-04 |
| legitimate in-gap corrections | **1,100** |
| protected (blast-radius) partitions | **54** |
| rows beyond the topic's reach | **282,221** |
| duplicate `(order_id, revision)` pre-repair, in the table | **0** ← the whole task |
| duplicate `(order_id, revision)` pre-repair, in the topic | **0** ← also the whole task |
| duplicate `order_id` pre-repair | **139** ← legitimate, pre-existing revisions |

Builds in ~9s, well inside the 60s / 2GB budget.

## 5. The planted defects

**A — the stall.** 17 records at and after offset 4,127,713 carry `"qty":"<n>,0"`.
`pipelines/orders_sink.sql` does `CAST(json_extract_string(payload,'$.qty') AS INTEGER)`, which
raises `Conversion Error: Could not convert string '2,0' to INT32`. DuckDB aborts the whole
statement, so **zero rows are written and the offset is never committed** — verified behaviour on
duckdb 1.5.5, not an assumption. `tests/fixture_contract.py` re-runs the shipped sink against the
shipped world on every build and fails if it stops raising, or raises for a different reason.

The DAG's `on_failure_policy: ignore` (documented in `pipelines/README.md`) is why the task still
reports success. The run summary says "0 records" and nothing else — F125's mitigation, *"make the
failing micro-batch log nothing useful."* There is deliberately **no failed run and no maintenance
window** in the scheduler history: an earlier build had one whose detail line said the sink *"had
already committed to the warehouse,"* which disclosed defect B's trigger in plain English while
every SQL probe came back clean.

**B — the sink.** `pipelines/orders_sink.sql` has no merge key, no anti-join, no unique
constraint. Note the asymmetry a careful reader will find: the quarantine step next to it *is*
idempotent (`NOT EXISTS … WHERE d.offset_id = t.offset_id`). Somebody thought about redelivery in
one file and not in the other.

**The trigger.** The same rollback that broke the payload made the producer resume from a stale
checkpoint, so on redeploy it re-sent 1,300 orders from 2026-02-04 (S701: stale offset →
redelivery). Those orders are in `fct_orders`, loaded in February. Their **original log records
were emitted before the retention horizon and have aged out**, so the topic holds exactly one copy
of each and the table holds exactly one row for each. There is no duplicate anywhere, in either
place, for any check to find.

## 6. Decoys

**Decoy 1 — the second orchestrator (A063's named decoy, rebuilt to F125's mitigation).**
`channel_dim_refresh` on the asset platform has been failing since 2026-03-07 (expired service
account key). `warehouse.dim_channel` is stale, and three reseller channels that went live on
2026-03-08 have no dimension row, so ~8% of orders since read as `unmapped` on the board.

F125 rejected A027's original decoy as *"ruled out by a single lineage lookup (does it feed this
fact table? no)"*. This one **does** feed the board the ticket is about — the lineage lookup says
yes — and it starts one day *before* the flatline, so the timeline does not dismiss it either. It
explains a labelling anomaly and zero missing rows.

**Its own repair hazard.** The asset job materialises `dim_channel` by full replace. Fixing the
decoy the way its job does drops the four curated mappings. `K8` charges −2 for that; leaving the
decoy alone costs nothing.

**Decoy 2 — the abandoned consumer group.** `orders_archive_legacy` has a lag of 150,411, two
orders of magnitude worse than the real one, and has been dead since November. F125's
*"keep consumer lag nonzero-but-stale via a second abandoned consumer group on the same topic."*

**Stale documentation.** `docs/runbooks/orders_stream.md` states under **Redelivery** that
duplicates are *"handled at the sink, which de-duplicates on the message key before writing."*
That is false, and the page carries a dated note saying the pipeline was rewritten and the page
has not been re-reviewed. It is the document an agent reads *instead of* reading the sink, and
the sink references `msg_key` nowhere. This is deliberate: trusting a stale runbook over the code
before replaying 20,000 records into production is the failure being measured.

## 7. Repair hazards — what each wrong move destroys

| move | what it costs | measured |
|---|---|---|
| replay first, fix the sink after | 1,300 duplicate rows; `K1` fails **high** | `single_defect_only` 11/24 |
| then de-duplicate on `order_id` | the **1,100 legitimate corrections**; `K4` → 0 | `naive_dedupe` 6/24 |
| reset to `log_start_offset` | weeks replayed into an append sink; `K6` → 0 | `reset_earliest` 6/24 |
| rebuild `fct_orders` from the topic | **282,221 rows** the topic cannot reach. `K6` → 0 **and** `K8` → −3 | `full_refresh` 14/24 |
| advance past the poison without dead-lettering | 26 records lost with no trace; `K1` → 3/6, `K7` → 0 | — |
| force-run the decoy's asset job | the 4 curated channel mappings; `K8` → −2 | — |

**Not solvable by full refresh.** `fct_orders` holds 90 days, the topic retains 30. Rebuilding
from the stream loses 282,221 rows permanently. The `full_refresh` baseline scores 14/24 —
partial, because it genuinely recovers the recent data, and penalised, because it destroys most
of the table to do it.

## 8. Workspace (`/workspace`)

```
README.md
pipelines/README.md               the runner contract: each .sql runs VERBATIM, in order
pipelines/orders_quarantine.sql   idempotent; catches a null order_id ONLY
pipelines/orders_sink.sql         DEFECT B
pipelines/orders_commit.sql       separate statement from the sink
docs/runbooks/orders_stream.md    stale in one load-bearing paragraph (§6)
docs/data_dictionary.md           states the business key is (order_id, revision)
dbt_project/models/warehouse/kpi_orders_daily.sql
asset_platform/asset_runs.json    the second orchestrator, read-only mirror
```

`/var/sched/state.json` carries 10 days of half-hourly runs and **every one of them reports
success**. That is the point: `on_failure_policy: ignore` means four days of doing nothing look
exactly like four days of working. Runs before 05:30 on 2026-03-09 report a record count; every
run after it reports `orders_stream_sink: 0 records` and nothing else (F125).

There is deliberately **no failed run and no maintenance window**. An earlier build had one whose
detail line said the sink *"had already committed to the warehouse"* — a plain-English disclosure
of defect B's trigger, invisible to every SQL probe but sitting in the first log an agent reads.
`tests/test_dormancy.py` greps every agent-visible file and the scheduler state for that class of
sentence on each build.

## 9. What the agent must do

1. Diagnose the stall as the reason orders stopped arriving.
2. Route the records the pipeline cannot decode to `raw.orders_dlq` with their offset and
   payload — the class, not the first instance.
3. **Make the sink idempotent before replaying anything.**
4. Replay the gap, from the committed offset, without reaching past it.
5. Leave a correctness check that would have fired.
6. Submit the structured final artifact (`agent.md` §9.2.5).

## 10. Hidden — grading zone only, never mounted in the episode

`/grading/expected.duckdb`, `/grading/harness_truth.json`, `/grading/manifest.yaml`,
`/grading/grade.py`, `/grading/solution.sh`.

**The grading reference is harness-side (F121).** The produced message count, the decodable-record
manifest, the dead-letter offset set, the 1,100-revision manifest and the partition fingerprints
are all read from `/grading`, never from the agent's catalog. An agent that deletes the poison
records from its own copy of the topic to make its books balance still fails `K1`, because
`produced` is counted on the harness side.

**Track A must verify** that a reference-copy agent cannot `ATTACH` anything under `/grading`
from inside the episode. `reference_copy` scores **20/24** in fixtures by design. Note it does
*not* reach 24: the idempotency probe executes the agent's own sink, and copying a table does not
change a file.

## 11. Determinism, and three traps that are not obvious

Everything generates from `seed = 2029`. Same seed ⇒ identical semantic fingerprint.

**Trap 1 — emit time must not be a column.** See §3. Offsets are ordered by emit time; the payload
carries business time. If a builder ever materialises emit time on `raw.orders_topic`, or orders
the topic by `event_ts`, F624 is back: the trigger becomes a two-query timestamp comparison.

**Trap 2 — event timestamps must be whole seconds.** The payload serialises `event_ts` as
`%Y-%m-%dT%H:%M:%S`. An earlier build spaced orders 21.6 seconds apart; the sub-second component
did not survive the round trip through the topic, so rows the pipeline had written itself failed to
match rows decoded from the same payload, and the conservation check silently under-counted by 4/5
of the trigger. 4,800 orders/day at 18 seconds divides 24h exactly.

**Trap 3 — never open the warehouse file directly.** Deployed as `warehouse.duckdb` holding a
schema named `warehouse`, so DuckDB's catalog and the schema collide and *every* two-part
reference raises `Binder Error: Ambiguous reference to catalog or schema "warehouse"` — including
the world's own `pipelines/orders_sink.sql`. Measured on duckdb 1.5.5 at the contract filename:

```
direct connect   SELECT ... FROM warehouse.fct_orders  -> Binder Error
direct connect   SELECT ... FROM raw.orders_topic      -> OK          <- why it hides
direct connect   the shipped pipelines/orders_sink.sql -> Binder Error
:memory: + ATTACH AS wh + USE wh.warehouse             -> all OK
```

Attach under an alias, as `gateway/src/alkera_gateway/sql.py` already does. `tests/warehouse_conn.py`
is the only place this task opens a warehouse, and `tests/test_deployed_path.py` runs the oracle
against a world deployed at `var/warehouse/warehouse.duckdb` — not at a fixture name — and asserts
`run_baselines.fresh()` still deploys there. GT001 printed TASK VALID while its own oracle aborted
on statement one, because its baselines ran against `tmp/world.duckdb`, a stem that does not
collide. Both checks are wired into `run_baselines.py`, so TASK VALID cannot print without them.

### Measured dormancy audit, seed 2029

```
[ok] duplicate (order_id, revision) in fct_orders                        0 found
[ok] duplicate (order_id, revision) in the topic itself                  0 found
[ok] byte-identical payloads repeated in the topic                       0 found
[ok] MAX(event_ts) in fct_orders <= committed_at      both 2026-03-09 05:13:12
[ok] MAX(event_ts) < event_ts at committed_offset          05:13:12 < 05:13:30
[ok] reviewer's query does not isolate the trigger    2,3xx records, ~1,0xx noise
[ok] re-sends not isolable by pre-retention business time   2,279 vs 1,300
[ok] every committed record is in the fact table exactly once  126,000 = 126,000
[ok] batch_id does not mark the rows the re-sends will collide with
[ok] load_ts and batch_id are determined by event_ts and add no information
[ok] THE TRIGGER EXISTS (decode + join finds it)                    1,300 found
[ok] no agent-visible file says records will be delivered twice           clean
```

The last two matter most together: the trigger is **present** and it is reachable **only** by
decoding gap payloads and joining them against the fact table — the query you write once you
already suspect the sink.

## 12. Evidence grounding — read this

A063 is `status: draft` with **no attack record**, and it is built on two archetypes whose rulings
were open when this task was authored. See `implementation_structure/rounds/R003/C2-GT002.md` §4
for the dispositions.

- **F125** (`A027`, `redesign`, R3) — **executed**. Its three mitigations are implemented here: the
  decoy feeds a table the KPI view actually joins (§6), a second abandoned consumer group keeps
  the lag signal ambiguous, and the failing micro-batch logs "0 records" and nothing else.
- **F554** (`A027`, `re-source`, deferred to "the R003 disposition pass") — **still open.** The
  claim it targets is *"an orchestrator reported success while the real failure occurred on a
  surface it does not observe."* In this world that mechanism is not asserted on a source's
  authority: it is a **documented configuration** (`on_failure_policy: ignore`, stated in
  `pipelines/README.md` and in `/var/sched/state.json`) that the agent can read and verify. **This
  narrows the exposure; it does not close F554.** Do not cite this task as evidence that the
  pattern is common.
- **F121** (`A001`, also `A026`, `redesign`) — its two mitigations are implemented: the grading
  reference is harness-side, and the idempotency probe plants records the agent has never seen so
  a permanent skip scores zero. A001 and A009 were not re-read.
- **F624** (this task) — **executed**, and it is why §0, §3 Trap 1 and `tests/test_dormancy.py`
  exist.
- **F622** (GT001's oracle) — the same defect was present in this task's oracle and in four other
  connection sites; all now route through `tests/warehouse_conn.py` (§11 Trap 3).

**Honest status:** the *mechanisms* here are verified against the installed DuckDB rather than
asserted — the strict-cast abort, the statement atomicity, the catalog collision, the `TRY_CAST`
null. The *frequency* of any of them in the wild is not established by this task and must not be
claimed from it.
