# RESEARCH-streaming — can a data engineer's Kafka and Spark instincts work inside our world?

**Round R004 · 2026-09-14 · agent RESEARCH-STREAMING**

Assignment: the user asked whether, since we cannot call Spark or Kafka directly, we can
"create a fake api or mcp that let agent believe they are using those corresponding tools."

Everything below is marked **[X]** executed by me today (command and output in §9),
**[R]** read from a source I or a delegated researcher actually fetched (URL given), or
**[INF]** my inference, named as such. I ran no benchmark I did not time and quote no
number I did not observe.

---

## 0. The headline

**The two halves have opposite answers, and both are the opposite of what "prestige" suggests.**

| | Verdict | Why, in one line |
|---|---|---|
| **Kafka / streaming** | **Build it — with the REAL broker (Redpanda), not a fake** | It boots in **2.0 s [X]** and is **byte-deterministic [X]** for exactly the surfaces our two authored streaming tasks need. A fake would have to reimplement consumer-group semantics, which is where a wrong fake does its damage. |
| **Spark / distributed compute** | **Do not run Spark. Ever. Build synthetic artifacts instead — and not this round** | A real `local[1]` Spark reports **`Memory Bytes Spilled: 0, Disk Bytes Spilled: 0, Shuffle Bytes Written: 0` [X]** on a deliberately skewed 3 M-row job. A synthetic event log I fabricated made a **real Spark History Server** report a **369× stage skew and 41 GB of disk spill [X]**. The fake is not a compromise here; it is strictly more honest. |

**The prior question — should we do either at all this round? Mostly no.** Streaming is
**2 of 41** archetypes and Spark is **0 of 41 [X]**. Batch fidelity is the larger
investment and it has named, open gaps. My ranking puts "not this round" above both
halves. But if exactly one thing ships, it is the Redpanda-backed streaming theme,
because it unblocks two already-authored tasks, one of which the corpus calls the
cleanest compound shape available.

---

## 1. Demand, counted rather than remembered

I counted `apps_touched` across all 41 archetypes in
`implementation_structure/state/archetypes/` myself **[X]**:

| Surface | Archetypes |
|---|---|
| warehouse | 41 |
| dbt | 28 |
| bi_dashboard | 25 |
| airflow | 17 |
| git | 15 |
| docs_wiki | 14 |
| notebook | 8 |
| ingestion_connector | 6 |
| … | … |
| **streaming** | **2** (A027, A063) |
| **spark** | **0 — the token does not appear as an app at all** |

A case-insensitive grep for `spark|kafka|kinesis|flink|pubsub|databricks` across the 41
files hits 8 files **[X]**: A001, A002, A009, A010, A017, A025, A027, A063. Most are
passing mentions; only A027 and A063 declare `streaming` as a touched app.

This independently reproduces the count in `rounds/R003/A5-licensed-surfaces.md`, which
recorded Redpanda as "stays dropped — never needed." **I disagree with that conclusion
now, and the reason is not demand volume — it is what the two tasks specifically need.**

### What A027 and A063 actually require (read in full **[X]**)

A027 — "Green-DAG triage of a failure that originates behind the orchestrator": a stalled
consumer group whose offset "committed past a poisoned message"; the agent must "trace the
empty payload through the consumer, reset or skip the poisoned offset safely, replay the
gap". Deterministic checks include **"consumer group lag equals zero"** and
**"message conservation: loaded + dead-lettered = produced count"**.

A063 — "A dormant append-only sink that only detonates when you unblock the stream":
"Consumer lag is 214,000 and climbing"; the correct repair "requires rewinding the consumer
past the poison record and replaying the gap"; and the planted trap is that "the reflexive
rewind — reset to earliest — replays the entire 30-day retained topic rather than the
four-day gap."

**Read that list again and notice what is absent.** Not one requirement touches throughput,
replication, broker failover, partition rebalancing under load, or distributed consensus.
Every single requirement is **offset arithmetic and consumer-group state**:
lag, committed offset, earliest/latest offset, reset-to-offset, reset-to-datetime,
dead-letter routing, produced-vs-consumed conservation.

That observation drives the whole Kafka recommendation, because it means the parts of Kafka
that are nondeterministic under a virtual clock are precisely the parts these tasks do not
use.

---

## 2. Kafka — the measurements

### 2.1 Redpanda boots fast enough. Measured, not assumed.

Image `docker.redpanda.com/redpandadata/redpanda:v26.2.2` (the current tag as published by
the Docker Hub tags API **[X]**): **124.7 MB compressed, 509 MB on disk [X]**.

Three cold starts, timing from `docker run` to a **successful Kafka-protocol round trip**
(`rpk cluster info` succeeding), then a create/produce/consume round trip **[X]**:

| run | `docker run` returns | Kafka API ready | produce+consume round trip |
|---|---|---|---|
| 1 | 1.22 s | **2.45 s** | 1.99 s |
| 2 | 1.05 s | **2.17 s** | 1.91 s |
| 3 | 0.93 s | **1.92 s** | 2.19 s |

Against the episode-boot budget of **< 60 s** (world/README.md acceptance gate 4), a ~2 s
broker is not a budget concern. Redpanda is a single binary with no ZooKeeper and no KRaft
quorum ceremony; `--smp 1 --memory 1G --node-id 0 --check=false` is the whole invocation.

It also does **not** touch the episode image size gate: Redpanda is a sibling container on
the `episode` internal network, not a layer in `alkera-world` (961 MB today **[X]**).

### 2.2 Kafka is byte-deterministic — under four pinning rules I verified

I produced 300 keyed records with **explicit producer timestamps** drawn from a fixed
virtual epoch, into a 3-partition topic with `message.timestamp.type=CreateTime`, then
consumed all of them and hashed the tuple stream `(partition, offset, key, value,
timestampType, timestamp)`. Run against **three separately created, fresh clusters [X]**:

| cluster | timestamp type | sha256 incl. timestamps | key→partition map |
|---|---|---|---|
| A (fresh) | CreateTime | `142685598ed8ceab863eab20` | `acb17c79d9a6edc6c2303535` |
| B (fresh) | CreateTime | `142685598ed8ceab863eab20` | `acb17c79d9a6edc6c2303535` |
| C (fresh, different listener config) | LogAppendTime | `0e0744faa4f6915741ad5de3` ← **differs** | `acb17c79d9a6edc6c2303535` |

Committed group offsets were identical across all three: `[(0,107),(1,104),(2,89)]` **[X]**.

**Reading:** with `CreateTime` + explicit producer timestamps, the log is byte-identical
across fresh brokers — offsets, partition placement, and timestamps. Under
`LogAppendTime` the payload/placement hash is unchanged but the full hash diverges,
because the broker stamps its own wall clock (observed value `1789419206133`, i.e. today).

Two nondeterminism sources I found and pinned:

**(a) Keyless produce is nondeterministic.** Same program, two fresh clusters, 60 keyless
records into 3 partitions **[X]**: placement `26/18/16` vs `23/17/20`, different hashes.
This is the sticky/random partitioner for null keys. **Pin: always produce with a key.**

**(b) `message.timestamp.after.max.ms` defaults to 3600000 — a one-hour future guard.**
I found this in `rpk topic describe` output **[X]** and then tested it directly. Producing a
record timestamped **10 days ahead of the container's wall clock [X]**:

```
default_cfg : KafkaError{code=INVALID_TIMESTAMP,val=32,str="Broker: Invalid timestamp"}
relaxed_cfg : accepted, stored_ts == requested_ts exactly
```

**This is the single most important operational trap in the whole Kafka half.** Our world
runs on a virtual clock. The moment `vnow` runs more than one hour ahead of the container's
real clock, every produce is rejected with `INVALID_TIMESTAMP`. It is silent in the sense
that it looks like a broker problem, not a design problem. **Pin:
`message.timestamp.after.max.ms` and `message.timestamp.before.max.ms` to
`9223372036854775807` on every topic.** (Independently corroborated **[R]**:
kafka.apache.org topic-configs documents the same default and the same semantics, and notes
the guard "is ignored if `message.timestamp.type=LogAppendTime`".)

### 2.3 Where wall clock genuinely leaks — and why it does not matter for us

Delegated research, each item fetched **[R]**:

| Surface | Clock | Pinnable? |
|---|---|---|
| Record timestamp | producer-supplied under CreateTime | **Yes, exactly** (verified **[X]**) |
| `--reset-offsets --to-datetime` | matched against **record** timestamps via `ListOffsets`/`OffsetSpec.forTimestamp` (verified in `GroupOffsetsResetter.java` **[R]**) | **Yes — no wall clock involved at all** |
| `--reset-offsets --to-earliest/--to-latest/--to-offset/--shift-by` | pure offset arithmetic | **Yes** |
| `--reset-offsets --by-duration` | the **CLI client's** `Instant.now()` (source-verified **[R]**) | No — avoid this flag |
| Retention deletion | broker wall clock vs segment's **largest record timestamp**; swept every `log.retention.check.interval.ms`, default 300000 **[R]** | **No** |
| Log compaction | background cleaner on `log.cleaner.backoff.ms` = 15 s; docs say the deadline "is not a hard guarantee" **[R]** | **No** |
| `commitTimestamp` in `__consumer_offsets` | broker-stamped; client cannot supply it since OffsetCommitRequest v2 **[R]** | No, but not observable via `OffsetFetch` **[R]** |
| Cluster UUID in `rpk cluster info` | random per cluster (observed `redpanda.c9efd028-…` **[X]**) | No — exclude from fingerprint |
| Broker clock injection | **None exists.** Kafka's `MockTime` is a `testFixtures` class; `KafkaClusterTestKit` hardcodes `Time.SYSTEM` at every construction site. Redpanda has no documented equivalent **[R]** | No |

**The crucial structural point.** Every unpinnable item — retention deletion, compaction,
the cleaner loop — is a *deletion* mechanism. Our tasks never need deletion to happen; they
need a topic that *contains* a 30-day history so that "reset to earliest" is a trap
(A063). That is authored content, not retention behaviour.

**So: set `retention.ms=-1` and `cleanup.policy=delete`, never let the broker delete
anything, and the entire nondeterministic surface of Kafka is switched off** while every
surface the tasks need stays real.

### 2.4 The surface an agent actually reads

Captured from a live broker **[X]** — this is the thing a fake would have to reproduce:

```
GROUP                  determ-probe
COORDINATOR-NODE       0
COORDINATOR-PARTITION  __consumer_offsets/0
STATE                  Empty
BALANCER
MEMBERS                0
TOTAL-LAG              0

TOPIC   PARTITION  CURRENT-OFFSET  LOG-START-OFFSET  LOG-END-OFFSET  LAG  MEMBER-ID  CLIENT-ID  HOST
orders  0          107             0                 107             0
orders  1          104             0                 104             0
orders  2          89              0                 89              0
```

Ranked practitioner weight **[R]**, with the caveat the researcher flagged honestly: **no
published dataset quantifies this split; the ranking is inferred from how much dedicated
CLI surface, doc real estate and metric families each activity has.**

1. **Consumer lag inspection** — the only activity with its own section in Kafka's ops
   guide, its own metric family (`records-lag`, `records-lag-max`), and its own Redpanda
   metrics (`redpanda_kafka_consumer_group_lag_max/_sum`). Read-only, so run constantly.
2. **Topic config / partition layout** — `kafka-topics.sh --describe`; prerequisite to
   nearly everything else.
3. **Stuck consumer group** — `max.poll.interval.ms` (default 300000), `session.timeout.ms`
   (default 45000); 8 dedicated rebalance metrics.
4. **Offset reset / replay** — first-class verb, 8 scenario flags, but deliberate and bursty.
5. **Rebalance storms** — real, but the fix is a config change, and KIP-848 (GA in Kafka
   4.0) is designed to remove the class.
6. **Schema Registry compatibility** — sharp at deploy time; Confluent-stack, not core Kafka.
7. **DLQ** — narrowest: Kafka Connect DLQ is **sink-connector-only**, and plain consumers get
   nothing from Kafka at all — it is application code.

Note item 7 carefully: **A027 and A063 both require a dead-letter queue, and Kafka does not
provide one.** In a real stack the DLQ is the consumer's own code writing to a second topic
or a quarantine table. That is good news — it means the DLQ half of those tasks lives in
the workspace repo we already control, not in the broker.

`rpk` covers what we need **[R]**: `rpk group describe`, `rpk group list`,
`rpk group seek --to start|end|<offset>|@<unix-ts>`, `rpk topic describe`.
Two gaps worth knowing before authoring: **`rpk` has no `--shift-by` and no
`--by-duration` equivalent [R]**. If a task's expected solution uses those, either ship the
Java `kafka-consumer-groups.sh` alongside, or author around them.

### 2.5 The fake option, and why I rank it second

A Kafka-shaped MCP toolbelt (`kafka_topics`, `kafka_consumer_lag`, `kafka_consume`,
`kafka_produce`) over a deterministic append-only log we control is very much in our
wheelhouse — it is the same shape as the `ingest_*` control plane already shipped.

**Fidelity ceiling, stated precisely.** A fake is fine for: lag numbers, topic/partition
listings, offsets, produce/consume of authored records. A fake starts teaching false things
at exactly these points:

- **Consumer group state machine.** `Empty` / `Stable` / `PreparingRebalance` /
  `CompletingRebalance`, and the rule that you cannot reset offsets for a group with live
  members. Real brokers refuse the reset; a naive fake will happily accept it, and an agent
  that learns "reset works any time" will fail on a real cluster in the most common way
  there is.
- **Commit-vs-position distinction.** `records-lag-max` is documented as "based on current
  offset and not committed offset" **[R]**. A fake that computes lag from the committed
  offset gets a *plausible* number that is wrong in exactly the stuck-consumer scenario
  A027 is about.
- **The poison-pill mechanic itself.** A063 turns on a consumer that "retries that record
  forever and commits nothing". That is a client-side deserializer loop. A fake `kafka_consume`
  tool that returns records cannot express "the consumer is stuck", because in a fake the
  consumer is our tool, not the agent's code.

That third point is decisive. **The fake cannot express the central mechanic of the two
tasks that motivate the work.** With a real broker, the stuck consumer is a real Python
process in the workspace repo that really throws on a real record and really fails to
commit — and `rpk group describe` really shows lag climbing.

### 2.6 Recommendation and buildable proposal — Kafka

**Build it with a real Redpanda broker, pinned.** Ranked below batch fidelity, above Spark.

**The pinning contract** (each rule traceable to a measurement above):

1. `message.timestamp.type=CreateTime` on every topic, and **every** producer sets an
   explicit timestamp from `vnow`. Never `LogAppendTime`. *(§2.2)*
2. **Every** record carries a key. No keyless produce, anywhere. *(§2.2a)*
3. `message.timestamp.after.max.ms` and `message.timestamp.before.max.ms` =
   `9223372036854775807` on every topic. *(§2.2b — the virtual-clock trap)*
4. `retention.ms=-1`, `cleanup.policy=delete`. Nothing is ever deleted or compacted, so the
   two unpinnable clocks never run. *(§2.3)*
5. Single broker, `--smp 1`, replication factor 1, fixed partition counts.
6. Fingerprint **through the Kafka API**, never from the broker's data directory: for each
   topic, the ordered `(partition, offset, key, value, timestamp)` stream plus each group's
   committed offsets. Exclude the cluster UUID. *(§2.2, §2.3)*
7. Task authoring uses `--to-earliest` / `--to-latest` / `--to-offset` / `--to-datetime`
   only. **Never `--by-duration`.** *(§2.3)*

**Shape of the work:**

- A fifth theme, `streaming_orders`, under `world/world/themes/`. Its `live:` block gains an
  arrival stream that produces to Redpanda instead of (or in addition to) writing a CSV
  partition — the existing `write_day` per-`(seed, day_index)` PRNG discipline carries over
  unchanged, because producing a keyed record with an explicit timestamp is as deterministic
  as writing a CSV line (measured, §2.2).
- A new `exec` kind, `kafka`, alongside the existing `dbt` / `theme` / `script` / `sql` /
  `scan`, so a job can run a real consumer script from the workspace repo.
- Gateway tools mirroring `rpk`: `kafka_topics`, `kafka_group_describe`, `kafka_group_seek`,
  `kafka_consume`. These are **thin proxies to the real broker**, not a simulation — which
  is the entire point, and is also why they are cheap.
- The stuck consumer and the DLQ live in the workspace repo as real Python, because that is
  where they live in a real stack (§2.4, item 7).
- Compose: one more service on the `episode` internal network. No egress implications.

**Cost [INF, anchored on measured numbers]:** broker plumbing + compose + fingerprint
extension ≈ **2–3 days**; the `streaming_orders` theme with a deterministic producer ≈
**2–3 days**; gateway tool surface ≈ **1–2 days**; porting A027 and A063 to built tasks ≈
**3–5 days each**. Call it **2–3 weeks** to two shipped streaming tasks. The infrastructure
half (≈1 week) is the part I am confident about; the task-authoring half is the usual
estimate risk.

---

## 3. Spark — the measurements, and the question that matters most

### 3.1 Cost, measured

`apache/spark:4.1.3-python3`: **808 MB compressed, 2.19 GB on disk [X]**. That single image
is **larger than the entire current `alkera-world` image (961 MB) [X]** and on its own
exceeds the **< 2 GB** image gate in the acceptance battery.

`spark-submit --master local[1]` in that container, three runs **[X]**:

| run | `import pyspark` | SparkSession | first job | **total** |
|---|---|---|---|---|
| 1 | 0.54 s | 4.54 s | 4.49 s | **9.56 s** |
| 2 | 0.61 s | 4.76 s | 4.18 s | **9.54 s** |
| 3 | 0.54 s | 4.88 s | 4.10 s | **9.52 s** |

**This corrects the brief's premise.** "JVM startup blows the determinism budget" is not
what I measured: a SparkSession costs **~4.7 s**, and a whole submit ~9.5 s. That is slow
but it is not disqualifying on its own. The disqualifying facts are elsewhere — §3.2 and
§3.3. I flag this because the round's standing instruction is to measure rather than
inherit an estimate, and here the inherited estimate was wrong in the *permissive*
direction.

### 3.2 Spark local mode is more deterministic than expected — and that is not the problem

Same aggregate, three runs, `local[1]` **[X]**: identical result hash
`f3a050d10975ae1689ee7a12`. A second, harder job (3 M rows, 90 % of rows on one key, AQE
disabled, 8 input partitions), run twice at `local[1]` and twice at `local[4]` **[X]**:
**all four runs produced the identical result hash `ca1928103a3291e9d48197f8`.**

Per-task metrics, though, split the two masters apart **[X]**:

| master | stage-1 shuffle-read records per task, run 1 | run 2 | stable? |
|---|---|---|---|
| `local[1]` | `[8, 8, 16, 24, 0, 0, 0, 0]` | `[8, 8, 16, 24, 0, 0, 0, 0]` | **yes** |
| `local[4]` | `[16, 8, 8, 24, 0, 0, 0, 0]` | `[16, 8, 24, 8, 0, 0, 0, 0]` | **no** |

And in **every** configuration, task *durations* varied run to run (stage 0 task 0:
325 ms vs 397 ms at `local[1]`) **[X]**. Wall-clock timing is never reproducible.

So: `local[1]` gives deterministic data and deterministic per-task byte/record counts, but
never deterministic durations. Corroborating documentation **[R]**: Spark's RDD guide states
that after a shuffle "the set of elements in each partition … will be deterministic … the
ordering of these elements is not"; and `repartition` + task retry is a known *correctness*
hazard — SPARK-23207 (fixed), **SPARK-38388 (still Open)**, SPARK-51756.

### 3.3 The question the brief called most important, answered with data

> *A single-node Spark that never skews, never spills and never OOMs may teach an agent that
> Spark has no failure modes — which could be worse than a synthetic event log and Spark UI
> that does exhibit them.*

**This is correct, and I can now put numbers on it rather than argue it.**

**Real `local[1]`, default input partitioning, 500 K rows, groupBy + orderBy.** The genuine
event log Spark wrote **[X]**:

```
1 job · 1 stage · 1 task
"Memory Bytes Spilled": 0
"Disk Bytes Spilled": 0
"Shuffle Bytes Written": 0
"Shuffle Read Metrics": all zero
```

I then tried hard to make real Spark misbehave: 3 M rows, a key with 90 % of the mass, AQE
turned off, `spark.sql.shuffle.partitions=8`, a join, 512 MB executor. Result **[X]**:
**8 tasks per stage, total shuffle write 2,713 bytes, zero memory spill, zero disk spill.**
The skew is invisible because partial aggregation collapses 3 M rows to 7 groups before the
exchange. To make real Spark genuinely spill you need data volumes that annihilate a
per-episode time budget — and you would still get nondeterministic durations (§3.2).

**Now the synthetic log.** I wrote a ~800-event log by hand from a fixed virtual epoch
(`1700000000000`) and a seeded PRNG, with one deliberately skewed partition, and served it
to a **real Spark 4.1.3 History Server** with no cluster of any kind. The **real server's
own REST API** computed **[X]**:

```
stage 1 "aggregate at orders.py:57"  numTasks=200
  shuffleReadBytes    2,399,040,000
  memoryBytesSpilled 12,884,901,888   (12 GB)
  diskBytesSpilled   44,023,414,784   (41 GB)

taskSummary quantiles [0.25, 0.5, 0.75, 0.95, 1.0]
  executorRunTime      [2131, 2271, 2469, 2578, 838000]     <- 369x skew
  shuffleRead records  [20000, 20000, 20000, 20000, 46000000] <- 2300x skew
  memoryBytesSpilled   [0, 0, 0, 0, 12884901888]
  diskBytesSpilled     [0, 0, 0, 0, 44023414784]
```

And the generator is byte-deterministic: two runs, identical sha256
`9a8a3edf55f6c889afc06799611febe4` **[X]**.

**The conclusion is not close.** The real thing, at a size we can afford, teaches that
Spark stages have one task, never spill, and never skew. The fake teaches the median-vs-max
percentile read that is the actual professional skill — and it teaches it *through the real
Spark History Server UI and the real `/api/v1` endpoints*, so the agent is reading the
genuine instrument. **Here the fake is not a degraded substitute for the real thing. It is
more faithful to real Spark than real Spark-at-our-scale is.**

This also happens to match where the work actually is. The read-vs-run ratio the brief asked
about: no published survey quantifies it and I will not invent one **[R, flagged
UNVERIFIED by the researcher]**. What *is* verifiable is structural and points one way —
Spark's official tuning guide is a read-the-UI loop ("look at the 'Storage' page in the web
UI"), Databricks' own diagnostic guide is a five-step inspection sequence (Jobs timeline →
longest stage → "Look for skew or spill"), and Spark ships a **stable, versioned REST API
over the artifacts** (`/api/v1`, with the published promise that "Endpoints will never be
removed from one version" and "Individual fields will never be removed") but no stable API
over a live session **[R]**. Spark's own compatibility posture is that the *artifacts* are
the contract.

### 3.4 Divergence analysis — where a synthetic Spark surface teaches something false

This is the part that matters for a substrate sold to labs, so I will be blunt about it.

| Divergence | What the agent could learn that is false | Mitigation |
|---|---|---|
| **No code→plan coupling.** The agent edits the PySpark job; the event log does not change. | "Changing `repartition(200)` to `repartition(20)` had no effect" — or worse, the agent learns that its edits are unobservable. | **This is the hard limit and it must bound the task design.** Author tasks where the deliverable is *diagnosis and a written/committed fix*, graded on the code change and the explanation, not on a re-run. Never author "tune it until the metric improves". |
| **No real OOM.** A fabricated `ExecutorLostFailure` is text, not a dead JVM. | Agent may believe an OOM is always recoverable by rerun. | Fabricate the failure faithfully (`"Reason":"ExecutorLostFailure"`, `"Exit Caused By App"`) and never let a rerun "succeed" — the log is fixed. |
| **Event log is `@DeveloperApi`, not a public contract [R].** | Nothing false is taught; but our generator can drift from a future Spark. | Pin the Spark version in the image and round-trip the generated log through that exact History Server in CI. I did this today; it is cheap. |
| **Durations are authored, not earned.** | If authored carelessly, an agent could learn implausible physics (a 200-task stage finishing before its parent). | Generate from an explicit model (task start times, a fixed executor count) rather than sprinkling numbers. |
| **`local[1]` if we ever run it** would show 1-task stages and zero spill. | Directly teaches "Spark has no failure modes." | **Do not run Spark.** This is the recommendation. |

**The mitigation I most want on record:** the agent must never be able to *rerun* the job
and get a stale log back. Either the log is explicitly historical (a completed run from
last night, which is the honest and common framing), or running the job must be impossible
in the world. The dishonest middle — a "run" button that replays a canned log — is exactly
the "wrong fake" that propagates a false belief into a model.

### 3.5 Two implementation traps I hit, both verified

**(1) The History Server silently ignores logs whose JSON is not compactly serialized.**
My first synthetic log parsed without error — the server logged `Finished parsing` — and
then listed **zero applications**. So did a byte-faithful re-serialization of a *real*
working log. The only difference was `{"Event": "…"}` versus `{"Event":"…"}`.

`FsHistoryProvider.scala` v4.1.3 **[R]**, lines 1526–1532 and the filter at 832–835:

```scala
private val APPL_START_EVENT_PREFIX = "{\"Event\":\"SparkListenerApplicationStart\""
private val APPL_END_EVENT_PREFIX   = "{\"Event\":\"SparkListenerApplicationEnd\""
private val LOG_START_EVENT_PREFIX  = "{\"Event\":\"SparkListenerLogStart\""
private val ENV_UPDATE_EVENT_PREFIX = "{\"Event\":\"SparkListenerEnvironmentUpdate\","
...
eventString.startsWith(APPL_START_EVENT_PREFIX) || ...
```

The listing scan does **raw string prefix matching** on the exact compact serialization.
`json.dumps(o, separators=(',', ':'))` is mandatory. I confirmed the fix end-to-end: after
compacting, my synthetic app listed and rendered **[X]**. I lost real time to this; whoever
implements it should not have to.

**(2) The rolling directory layout is the default and is required.** A bare file named
`<appId>` produces `FileNotFoundException: File file:/evlog/eventlog_v2_<appId> does not
exist` **[X]**. Required shape, matching a genuine log I captured **[X]**:

```
eventlog_v2_<appId>/
  appstatus_<appId>        (zero bytes; the ".inprogress" suffix means incomplete)
  events_1_<appId>         (JSON lines, LF, compact; no codec suffix when uncompressed)
```

Field spellings verified against a genuine log I generated **[X]** and against
`JsonProtocol.scala` **[R]**, which agree: core events use the **bare** class name with
`"Title Case With Spaces"` keys (`"Memory Bytes Spilled"`, `"Disk Bytes Spilled"`,
`"JVM GC Time"`, `"Shuffle Bytes Written"`); SQL events use the **fully-qualified** name
(`org.apache.spark.sql.execution.ui.SparkListenerSQLExecutionStart`) with camelCase keys.
That asymmetry is the easiest thing to get wrong.

### 3.6 Recommendation — Spark

**Not this round. When it comes, generate artifacts; never run a cluster.**

Ranked last because: 0 of 41 archetypes touch it **[X]**; the image alone breaks the size
gate **[X]**; and the §3.4 code→artifact decoupling limits what can honestly be asked. It
is a genuine gap in "a real data engineer's computer", but it is the gap with the least
corpus demand and the most authoring constraint.

When it is built, the shape is settled by today's evidence and is small:

- A `world/world/spark/eventlog.py` generator: stages, tasks, a skew model, a spill model,
  a failure model → compact JSON lines at a fixed virtual epoch. Deterministic from
  `(seed, run_id)`, exactly like the existing themes **[X: proven today]**.
- The stock `apache/spark` History Server as a read-only sidecar — or, to dodge the 2.19 GB
  image, a small static server over the same `/api/v1` JSON shapes, since **the REST API is
  the documented-stable surface and the event log is not [R]**. I would build the generator
  first and decide the server second.
- Gateway tools `spark_apps`, `spark_stages`, `spark_task_summary` mirroring `/api/v1`.

**Cost [INF]:** generator ≈ 3–4 days (the format is now fully mapped and round-trip proven);
serving surface ≈ 1–3 days depending on the sidecar choice; first task ≈ 3–5 days.

---

## 4. Databricks-shaped vs Spark-shaped

Scope note first: a separate agent owns the SQL/warehouse surface including Databricks SQL.
**I own compute and streaming, so this covers jobs, clusters, runs and cluster logs only.**

**Databricks-shaped matters more than Spark-shaped for the *job* surface, and less for the
*debugging* surface.** The Jobs/Clusters APIs are ordinary JSON over HTTP and are cheap to
mock; the skill they exercise (read a run's output, inspect a cluster config, find why a
job failed) overlaps heavily with the `sched_*` and `ingest_*` surfaces we already ship.
The genuinely Spark-specific skill — reading a stage's task percentiles to spot skew — is
the event-log work in §3, and it is identical whether the cluster is Databricks or not.

Verified shape **[R]**: Jobs API is **2.2** (`POST /api/2.2/jobs/create`, `run-now`;
`GET /api/2.2/jobs/runs/get-output` returns `notebook_output`, `logs`, `logs_truncated`,
`error`, `error_trace`, and is capped at the **first 5 MB**). Clusters is a *different*
version, **2.1** (`POST /api/2.1/clusters/create`, `POST /api/2.1/clusters/events`).
Workspace and DBFS are **2.0**. Unity Catalog is `/api/2.1/unity-catalog/…` (catalogs and
tables confirmed literally; the sibling families were **not** verified — do not hardcode
them without checking).

**There is no official Databricks emulator [R].** Databricks Connect is a *remote* client
requiring a live workspace, not an emulator, and Databricks' own testing guidance is to use
`unittest.mock`. One OSS project exists (`calvinchengx/databricks-emulator`) but GitHub API
metadata confirms it is **one month old with 1 star and 0 forks [R]** — a design reference
to read, not a dependency to take.

**My recommendation: do not build a Databricks surface this round either**, and when the
warehouse agent's work lands, revisit whether the *jobs* half is worth ~3 days on top of it.
The mock-honesty risk here is low (JSON endpoints either match the docs or do not), but so
is the marginal skill taught over the existing scheduler surface.

---

## 5. The ranking

1. **Batch fidelity first — neither Spark nor Kafka.** Four themes, 41 archetypes, and the
   existing product has *named* open gaps: `world/README.md` records that arrival faults do
   not reach the seeded backstory (`faults_in_history` covers job faults only), and that the
   fault layer "stages contention; it never races." Those are fidelity defects in the
   surface **41 of 41** archetypes touch. Streaming is prestigious; `warehouse` is the
   product.
2. **Kafka via a real, pinned Redpanda** — if one new surface ships this round. Unblocks
   A027 and A063, one of which the corpus calls the cleanest compound shape it has. ~2 s
   boot, byte-deterministic under §2.6's contract, and the real broker is *cheaper* than a
   faithful fake.
3. **Spark as a synthetic event log** — real skill, proven feasible today, near-zero corpus
   demand. Build the generator when a task needs it.
4. **Databricks compute surface** — revisit after the warehouse agent lands.

---

## 6. My weakest finding

**That a real Redpanda stays byte-deterministic across a full episode, not just across my
300-record probe.**

I proved determinism for produce-then-consume on three fresh clusters with an idempotent
producer and a single-threaded consumer that I assigned partitions to manually. A real
episode is harder in ways I did not test:

- I used `assign()`, **not `subscribe()`**. I never exercised the consumer **group
  coordinator's rebalance path**, which is where member IDs, generation IDs and assignment
  order are decided. `rpk group describe` has `MEMBER-ID` / `CLIENT-ID` / `HOST` columns
  that were empty in my capture only because the group was `Empty` **[X]**. With live
  members those carry values I have every reason to expect are random per run, and I did
  not measure them or confirm they can be excluded from a fingerprint cleanly.
- I never ran a consumer **concurrently** with a producer, which is the actual A027/A063
  situation; my probe produced fully, then consumed fully.
- I ran no episode-length test. Three 300-record probes is a floor, not a guarantee.

If this finding is wrong, the Kafka recommendation weakens from "real broker, pinned" to
"real broker, pinned, **plus a normalization layer** that strips member/generation IDs
before fingerprinting" — a day of work, not a reversal. But I want it recorded that I have
not earned the stronger claim.

Two smaller ones, named honestly: the practitioner rankings in §2.4 and the read-vs-run
ratio in §3.3 are **inferences from doc structure, not measured survey data**, and the
delegated researcher flagged both as unverified. I have not corrected for that beyond
saying so. And all cost estimates in this document are **[INF]** — only the boot times,
image sizes, hashes and startup latencies are measured.

---

## 7. What would change my mind

- **On Kafka:** if a rebalance-path determinism test (the §6 gap) shows member/generation
  IDs bleeding into anything we must fingerprint and resisting normalization, the cost rises
  and the fake becomes competitive again.
- **On Spark:** if a task author wants an agent to *iterate* — edit the job, rerun, observe
  improvement — the synthetic log cannot support it honestly (§3.4) and the answer becomes
  "not at all" rather than "artifacts".
- **On both:** if the corpus grows streaming archetypes from 2 toward 10, item 2 moves above
  item 1.

---

## 8. Answering the user's question directly

> *"if we cannot directly call them, can we create a fake api or mcp that let agent believe
> they are using those corresponding tools?"*

**For Kafka: we don't need to — we can call it directly.** The premise that Kafka is blocked
is wrong. Redpanda boots in 2 s offline and is byte-deterministic under four pinning rules I
verified. The thing that made Kafka look impossible — exactly-once replay semantics under a
virtual clock — lives in retention and compaction, which our tasks never use and which we
switch off with `retention.ms=-1`.

**For Spark: yes, and the fake is better than the real thing.** Not as a compromise — on
measured fidelity. Real Spark at a size we can afford reports zero spill and zero skew; a
synthetic event log makes a *real Spark History Server* report a 369× skew and 41 GB of
spill. The agent reads the genuine instrument either way. The honest limit is that the
agent's code edits cannot change the artifact, and that limit must bound what the tasks ask.

---

## 9. Reproduction log

Every number above came from one of these. All run on this host today (Docker Desktop
29.5.2, Windows 11, 16 cores).

| # | Command / probe | Result |
|---|---|---|
| 1 | Docker Hub tags API, `redpandadata/redpanda` | v26.2.2, 124.7 MB compressed |
| 2 | `docker pull …redpanda:v26.2.2`; `docker image inspect` | 509 MB on disk |
| 3 | `docker run` → poll `rpk cluster info` → create/produce/consume, ×3 | ready in 2.45 / 2.17 / 1.92 s |
| 4 | 300 keyed records, explicit ts, CreateTime, 3 fresh clusters | sha `142685598ed8ceab863eab20` ×3 |
| 5 | same, `LogAppendTime` | full hash diverges; broker stamps wall clock |
| 6 | 60 keyless records, 2 fresh clusters | placement 26/18/16 vs 23/17/20 — nondeterministic |
| 7 | produce ts = wall + 10 days, default vs relaxed topic config | `INVALID_TIMESTAMP` (32) vs stored exactly |
| 8 | `rpk group describe` / `topic describe` / `cluster info` | output shapes captured; random cluster UUID |
| 9 | `docker pull apache/spark:4.1.3-python3` | 808 MB compressed, **2.19 GB on disk** |
| 10 | `spark-submit --master local[1]`, ×3 | session 4.54–4.88 s, total 9.52–9.56 s |
| 11 | real event log from #10, decompressed (zstd) | all zero shuffle/spill; 1 stage, 1 task |
| 12 | 3 M rows, 90 % skewed key, AQE off, `local[1]` | 2 tasks, **0 shuffle bytes, 0 spill** |
| 13 | same with 8 input partitions, `local[1]` ×2 / `local[4]` ×2 | data hash identical ×4; per-task metrics stable only at `local[1]`; durations never stable |
| 14 | ~800-event synthetic log → **real** History Server 4.1.3 | listed; `/api/v1` reports 369× skew, 41 GB disk spill |
| 15 | `FsHistoryProvider.scala` v4.1.3 (fetched raw) | compact-JSON `startsWith` filter, L832–835 / L1526–1532 |
| 16 | generator run twice | sha `9a8a3edf55f6c889afc06799611febe4` ×2 |
| 17 | `apps_touched` census over 41 archetype YAMLs | streaming 2, spark 0 |

Scratch artifacts (synthetic log generator, probes, captured event logs) are in this
session's scratchpad, outside the repo. Nothing outside `rounds/R004/` was modified and
nothing was committed.
