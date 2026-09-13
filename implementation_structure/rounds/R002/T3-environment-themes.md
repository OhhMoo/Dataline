# T3 — Environment themes: the 30 worlds this benchmark should ship

**Round R002 · 2026-09-13 · agent T3 · ID blocks claimed: S900–S942, T270–T299**

Today the project has **one** world shape: DuckDB + dbt + git + a deterministic scheduler + BI
values, built for GT001. That is correct for v1 and it is not a product. A benchmark with one
environment shape measures competence in that shape.

This document defines 30 themes, assesses what can actually be containerized, and reports where
archetype coverage is and is not.

**The headline, up front.** Of the **41** archetypes in `state/archetypes/`, **40 live in the modern
data stack.** Exactly one (A028) so much as names a legacy database. Zero touch a flat file, a
fixed-width record, a spreadsheet, an SFTP drop, a batch window, or any enterprise ETL tool. Meanwhile the
cheapest themes to build are precisely the ones with no coverage — the legacy and flat-file
families need almost no new infrastructure, because what a data engineer actually experiences of a
mainframe is a file, a copybook and a deadline, none of which require a mainframe.

---

## 1. The themes

**Build tiers**, defined by measurement rather than by feel (all figures in §3):

| Tier | Meaning | Marginal cost over today's world |
|---|---|---|
| **T1** | World-generator changes only. No new service, no new image. | ~0 MB RAM, ~0 s boot |
| **T2** | One light service or one gateway change. | < 100 MB RAM, < 5 s boot |
| **T3** | A heavy service. Fits only as the *sole* heavy service in a world. | 300 MB – 850 MB RAM, +0.7–2.3 GB disk |
| **T4** | Cannot be built faithfully. Proxy, or do not build. | — |

### Family A — Modern data stack (5)

| # | Theme | Who runs this, and why | Tool set | Signature pathology | Archetypes | Build |
|---|---|---|---|---|---|---|
| A1 | **Series-B SaaS modern stack** | 30–150 people, analytics engineering function founded 2 years ago, no DBA ever. Stack chosen from conference talks. | Managed ingestion → cloud warehouse → dbt → BI | dbt lineage sprawl; incremental models whose `unique_key` no longer means what it did; a model owned by someone who left | A001, A002, A009, A010, A017, A019, A022, A025, A026 | T1 |
| A2 | **Lakehouse on object storage** | Data volume outgrew a warehouse budget; someone read the Delta/Iceberg paper. Often ML-adjacent. | Object store + table format + Spark + catalog | Small-file accumulation; schema evolution that widens a column silently; retention/vacuum deleting the history an audit needs | *(none)* | T2 |
| A3 | **Streaming-first / CDC** | Product needs sub-minute data, or the source DB owner refused batch reads. | CDC → log → warehouse sink | At-least-once delivery becomes duplicates downstream; replication slot bloat; out-of-order events that a windowed aggregate silently drops | A004, A027 | T2 |
| A4 | **Semantic-layer-centric analytics** | Analytics org large enough that metric definitions became political. | dbt + semantic layer + 2–3 BI tools | The same metric defined three times and drifting; the semantic layer and the dashboard disagreeing while both are "right" | A006, A019, A029, A030 | T1 |
| A5 | **OSS ELT on a budget** | Cost-conscious or privacy-constrained; runs its own everything. | Postgres + OSS loader + OSS orchestrator + OSS BI | No isolation between application and analytics; an analytical query locking the production table; no warehouse, so no historization | A001, A009, A026 | T2 |

### Family B — Cloud-warehouse-centric (4)

| # | Theme | Who runs this, and why | Tool set | Signature pathology | Archetypes | Build |
|---|---|---|---|---|---|---|
| B1 | **Snowflake-first enterprise** | Mid-to-large company that bought the platform and the consulting engagement together. | Cloud warehouse + managed ingestion + enterprise BI + RBAC | Credit burn nobody attributes; role grants that are simultaneously over-broad and missing; a clone that has silently diverged from its parent | A006, A021, A029 | T1 (dialect shim) |
| B2 | **BigQuery / GCP analytics estate** | Company already on GCP; analytics followed the cloud, not the other way round. | Columnar warehouse + scheduled queries + sheets-connected BI | Partitioning and clustering chosen once and never revisited; cost that scales with carelessness rather than value; streaming-buffer rows invisible to a point-in-time read | *(none)* | T1 (dialect shim) |
| B3 | **Redshift shop grown out of Postgres** | Started on Postgres, hit a wall, took the migration path with the least SQL rewriting. | MPP warehouse + Postgres habits | Distribution and sort keys inherited from a single-node mental model; vacuum and analyze debt; a leader-node bottleneck nobody profiles | *(none)* | T1 (T170 exists) |
| B4 | **Notebook-as-production lakehouse** | Platform bought for ML; data engineering happened to it rather than being designed. | Managed lakehouse where every job is a notebook | Notebooks chained by import side effects; cluster configuration drift between dev and job; no lineage because nothing declares its inputs | A033 | T2 |

### Family C — Legacy / Fortune 500 (7)

| # | Theme | Who runs this, and why | Tool set | Signature pathology | Archetypes | Build |
|---|---|---|---|---|---|---|
| C1 | **Mainframe extract + nightly batch window** | Bank, insurer, government agency. The system of record predates everyone working on it and cannot be replaced. | Fixed-width EBCDIC extract + copybook + downstream loader + batch cutoff | Code-page confusion that substitutes characters without erroring (S900); packed and zoned decimal parsed to plausible wrong numbers (S904, S910); a batch window missed so the downstream runs on yesterday | *(none)* | **T1** |
| C2 | **Teradata enterprise warehouse** | Bought in the 1990s or 2000s, holds 20 years of history, migration has been "next year" for six years. | MPP warehouse + BTEQ-style scripts + enterprise BI | `SET` tables silently discarding duplicate rows on a bulk load, loudly rejecting them on a single-row insert (S932, S933) — so probing by hand tells you the opposite of the truth | *(none)* | T1 (dialect shim) |
| C3 | **GUI-ETL estate (Informatica / DataStage)** | Large enterprise; the ETL layer was bought, staffed by a contractor team, and outlived the contract. | GUI-authored mappings + relational warehouse + scheduler | Business logic that exists only inside a non-diffable artifact (S941, S942); rows rejected to a bad file that no alert reads (S937) | *(none)* | T2 |
| C4 | **Microsoft shop** | Mid-market or divisional; everything came from one vendor and one reseller. | SQL Server + SSIS + Excel + Power BI | A flat-file column pinned to a declared width and code page, truncating when the data grows past it (S930, S931); stored-procedure sprawl; import-mode dashboards showing last refresh | *(none)* | T2 (T3 if real engine) |
| C5 | **Oracle / PL-SQL estate** | ERP came with the database; analytics grew inside it because that is where the data was. | Oracle + PL/SQL packages + scheduled jobs | Empty string and NULL being the same thing until they are not (S939); numeric precision lost at the driver boundary (S940); logic in packages nobody can read | A028 *(partial)* | T1 (dialect shim) |
| C6 | **SAP-sourced reporting** | Any large manufacturer, retailer or logistics operation. | ERP extractors + staging + reporting layer | Delta queues and setup tables producing a reconciliation gap between the ERP's own number and the warehouse's | *(none)* | **T4 — do not build yet** |
| C7 | **Enterprise batch scheduling (Control-M / AutoSys)** | The scheduling layer of most of C1–C6. Usually a separate team from the people who write the jobs. | Time-triggered job scheduler + file watchers | Dependencies expressed as *clock* rather than as *data*, so a late-but-successful upstream yields a green downstream run over stale inputs | A011, A018 *(partial)* | **T1** |

### Family D — Flat file / spreadsheet (5)

| # | Theme | Who runs this, and why | Tool set | Signature pathology | Archetypes | Build |
|---|---|---|---|---|---|---|
| D1 | **SFTP-and-cron vendor exchange** | Mid-market anything with external partners. The integration predates any API the partner offers. | SFTP drop folders + cron + a loader script | A file read while still being written (S919); a redelivery under the same name with different content, where both available loader settings are wrong (S921); a sentinel file on three feeds and not the fourth (S929) | *(none)* | **T1** |
| D2 | **Excel as the integration layer** | Finance, FP&A, or a mid-market operations team. The close runs on a workbook and everyone knows it. | Spreadsheets + email + a warehouse nobody trusts more than the workbook | Identifiers coerced to dates and leading zeros lost (S927); four distinct IDs collapsing into one at 15 significant digits (S926); a 1462-day date-system offset (S925); manual adjustments that live nowhere else | *(none)* | **T1** |
| D3 | **Fixed-width interchange (healthcare / financial / government)** | Any organisation exchanging regulated records under a published positional spec. | Positional record files + a versioned layout spec + a validator | An offset shift that produces a plausible record rather than an error; a spec version bump on one segment; trailing-space and low-value semantics | A025 *(analogous)* | **T1** |
| D4 | **CSV-by-email ingestion** | Small analytics team receiving data from humans rather than systems. | Attachments + a landing folder + a fragile parser | Encoding drift between senders (S928); a BOM attaching itself to the first column name (S920); header renames with no notice | *(none)* | T2 (gateway change) |
| D5 | **Shadow-IT departmental database** | One analyst built something in the 2010s. It is now load-bearing and nobody owns it. | A desktop database or shared-folder file + a production pipeline joining to it | A mapping table with no version control that quietly determines a reported number | *(none)* | T1 |

### Family E — Hybrid, half-migrated (5)

| # | Theme | Who runs this, and why | Tool set | Signature pathology | Archetypes | Build |
|---|---|---|---|---|---|---|
| E1 | **Legacy warehouse → cloud, dual-running** | The migration is in year two of an eighteen-month plan. Both systems are authoritative for different consumers. | Old MPP warehouse + new cloud warehouse + two BI layers | A reconciliation gap between old and new that **nobody owns**, because each team's system is correct by its own definition | A028 *(partial)* | T1 |
| E2 | **On-prem Hadoop → cloud lakehouse, stalled** | The easy tables moved. The hard ones did not, and the team that knew them was reorganised. | HDFS-era metastore + cloud catalog + two compute engines | Two catalogs disagreeing about the same table's schema; a job reading through one and writing through the other | *(none)* | T2 |
| E3 | **GUI-ETL → modern transformation, partial** | Migration funded per-pipeline. Half of each pipeline is in each tool. | Legacy ETL + dbt-style transformation + two schedulers | A transaction boundary that used to exist inside one tool and now spans two, so a partial failure leaves a state neither side can describe | A028 | T2 |
| E4 | **Operational DB + warehouse + reverse ETL** | Modern stack that closed the loop back into the product. | Application DB → warehouse → reverse ETL → application DB | Circular lineage; a warehouse-computed field written back and re-ingested, so a definition change compounds instead of propagating | *(none)* | T2 |
| E5 | **Post-acquisition integration** | Two companies, two stacks, one board deadline. | Both companies' entire stacks, plus a mapping layer | Two customer masters at different grains; currency and timezone conventions that disagree; a metric that is the sum of two incompatible things | *(none)* | T2 |

### Family F — Ungoverned and regulated extremes (4)

| # | Theme | Who runs this, and why | Tool set | Signature pathology | Archetypes | Build |
|---|---|---|---|---|---|---|
| F1 | **Notebook-driven research group** | Science, quant research, or an ML team with no platform partner. | Notebooks + a shared drive + ad-hoc extracts | Results that do not reproduce because state is hidden in kernel order and file paths; leakage nobody looked for | A005, A012, A013, A020, A031, A032, A033 | T2 |
| F2 | **Postgres that grew into a warehouse** | Startup that never migrated. The reporting replica *is* the warehouse. | One Postgres + views + a BI tool | Mutable rows with no history, so last month's number cannot be reproduced today; reporting queries competing with the application | *(none)* | T2 |
| F3 | **The one heroic script** | Two-person data team, or one engineer inside a non-data company. | A single cron'd script + a database | No tests, no lineage, no idempotency; a rerun that double-counts because the author never expected a rerun | A001, A009, A026 *(degenerate)* | **T1** |
| F4 | **Regulated estate (audit, retention, RLS)** | Healthcare, finance, or anything post-incident. Governance arrived after the data did. | Warehouse + row-level security + masking + retention + lineage | Access controls that are simultaneously too broad and too narrow; erasure obligations colliding with immutable history; a masked column silently changing an aggregate | A003, A021 | T1 |

---

## 2. Coverage gap analysis

Counting each archetype against its best-fitting theme family:

| Family | Themes | Archetypes with a home here | Status |
|---|---|---|---|
| A — Modern data stack | 5 | ~29 | **Saturated.** More archetypes here have sharply diminishing returns. |
| B — Cloud warehouse | 4 | ~4, all shared with A | Thin but cheap; B2 and B3 have **zero** dedicated archetypes. |
| C — Legacy / F500 | 7 | **1, partial** (A028) | **The gap.** Six of seven themes have no archetype at all. |
| D — Flat file | 5 | **0** | **The gap.** Zero archetypes across five themes. |
| E — Hybrid | 5 | 1 (A028) | **The gap.** Four of five themes have no archetype. |
| F — Ungoverned / regulated | 4 | ~9, mostly data-science | F2 and F3 have none; three are `redesign_required`. |

**Orphaned themes — 19 of 30 have no archetype whatsoever.** Listed so they are not lost:
A2, B2, B3, C1, C2, C3, C4, C6, D1, D2, D3, D4, D5, E2, E4, E5, F2, plus B4 and E3 which have one
weak claim each.

**Orphaned archetypes.** The inverse problem barely exists: every archetype has a home, because
every archetype was authored inside the one world shape we have. That asymmetry *is* the finding.
The corpus is not mis-specified, it is **mono-cultural** — and a monoculture is invisible from the
inside, which is exactly why this document exists.

### A natural experiment ran while I was writing this

Fifteen archetypes (A040–A044, A050–A054, A060–A064) were authored by other R002 agents *during*
this round. Per the brief, those authors are **blind to each other and to me** by construction.

All fifteen are modern stack. Their `apps_touched` lists draw from the same six surfaces —
`warehouse`, `dbt`, `airflow`, `bi_dashboard`, `git`, `docs` — and not one reaches a flat file, a
fixed-width record, a spreadsheet, a batch window, or a legacy engine.

This is the strongest evidence in the document, and it was free. Independent authors with no
coordination reproduced the monoculture exactly, because **the monoculture is not an authoring
preference — it is an artifact of there being one world to author against.** Adding authors will
not fix it. Only adding worlds will. That reframes the priority: the bottleneck is not task
authoring capacity, it is environment shape, and every round spent authoring more modern-stack
archetypes deepens the concentration rather than relieving it.

### Where the next round of task authoring should go

Ordered by value per unit of build effort, which is not the same as ordered by interest:

1. **C1 (mainframe extract + batch window) — T1, best-sourced gap.** Eight verified sources
   (S900–S913), two of them live open issues, two crown-jewel silent-wrong-answer mechanisms,
   and it needs no new container. This is the highest-value single theme in the document.
2. **D1 (SFTP-and-cron) — T1, three distinct mechanisms, all file-and-clock.** S921's
   same-name-different-content dilemma is unusually good material because *both* available loader
   settings are wrong, so the task cannot be solved by flipping a flag.
3. **D2 (Excel as integration layer) — T1, and the only theme with `frequency`-licensed sources.**
   S915 and S916 are peer-reviewed with named authors and quantified populations; they are the two
   records in this entire block that G6 permits to say "this happens often."
4. **C7 (time-triggered batch scheduling) — T1, and it unlocks others.** Clock-versus-data
   dependency is a scheduler option (T294), and it is a precondition for C1's batch window and for
   most of family E's reconciliation gaps.
5. **C2 (Teradata SET-table dedup) — T1 via dialect shim.** Vendor-documented (S932) with the
   silent/loud asymmetry (S933) that punishes interactive probing.
6. **E1 (dual-running migration).** The reconciliation-gap-nobody-owns shape is the most
   commercially legible legacy theme, and it composes from C2 + A1 with no new services.

Deliberately **not** recommended: C6 (SAP), because this round produced zero verified sources for
it and a theme without mechanism sources is a tool list wearing a costume; and any theme requiring
a real legacy engine, for the reasons in §3.

---

## 3. Docker Compose feasibility — measured, not estimated

Every figure in this section came from a command run on this machine on 2026-09-13, or is marked
as reported-not-measured.

### 3.1 What today's world actually costs

| Quantity | Measured |
|---|---|
| World image `track_a-world:0.1.0` on disk | **880 MB** (198 MB shared with `python:3.11-slim`, 682 MB unique) |
| Seed → materialized world | **6.4 s** |
| Materialized world on disk | **3.5 MB** (warehouse.duckdb: 784 KB) |
| Peak process RSS during build | **49 MB** |
| Idle container RSS | ~1 MB |

**The three zones share one image**, so the topology costs 880 MB of disk once, not three times.
That is a genuinely good property and it should be preserved.

**The 2 GB RAM budget is essentially unspent.** Today's world uses about 49 MB of it. The
constraint everyone has been worrying about is not the binding one.

### 3.2 Measured idle RSS of candidate services

Each run with `docker run`, sampled after settling. Idle, not under load — treat as a floor.

| Service | Idle RSS | Image on disk | Shared layers |
|---|---|---|---|
| `postgres:16` | **28 MB** | 642 MB | 117 MB |
| `bigquery-emulator` | **52 MB** | 422 MB | 0 B |
| `minio` | **69 MB** | 241 MB | 0 B |
| `gitea` | **82 MB** | 266 MB | 13 MB |
| `clickhouse-server` | **172 MB** | 1.19 GB | 34 B |
| `adobe/s3mock` (JVM) | **297 MB** | 223 MB | 0 B |
| `apache/kafka` (JVM) | **318 MB** | 686 MB | 32 B |
| `mssql/server:2022` | **848 MB** | **2.34 GB** | **0 B** |
| `apache/airflow:3.0.2` | not run | **2.53 GB** | 32 B |

SQL Server reached "ready for client connections" in **13 s** — fast enough, but see §3.4.

### 3.3 What actually breaks, and where

**RAM is not the wall.** A five-service world — world (49) + postgres (28) + gitea (82) +
minio (69) + a docs/BI mock (~80) — is **≈ 310 MB**, comfortably inside 2 GB. You can afford five
or six light services.

**Disk is the wall.** Per-world image footprint for a heavy theme:

```
world image          880 MB
+ SQL Server       2 340 MB      (0 B deduplicated — different base entirely)
+ Kafka              686 MB
                  ─────────
                   3 906 MB per world, on disk
```

Across thousands of parallel sandboxes that is the number that ends the conversation, and it is
four times larger than the registry-compressed figure (~600 MB for SQL Server) that a naive
estimate would use. **Quote uncompressed size when budgeting disk; compressed size only for pull
bandwidth.** Conflating them understates per-sandbox disk fourfold.

**Layer sharing is real on disk and does not exist in RAM.** Measured: our world image shares
198 MB with `python:3.11-slim`; `postgres:16` shares 117 MB with `debian:bookworm-slim`; SQL Server,
BigQuery emulator, MinIO and s3mock share **zero bytes** with anything. Docker's own documentation
describes layer dedup as a *storage* property; RAM sharing between containers is at best incidental
page-cache reuse of identical read-only files and never covers a database's buffer pool, a JVM heap,
or any process working set. **Budget RAM as fully additive.**

The rule that falls out: **anything we build ourselves must derive from the same pinned base as
`world/docker/Dockerfile`.** That buys a measured ~198 MB dedup per service. Any third-party vendor
image pays its full unique size, every time.

### 3.4 Legal blockers, which are worse than the technical ones

Reported by a sub-search from vendor pages; **not verified by me**, and every one needs a real legal
read before it enters a plan:

| Product | Status |
|---|---|
| **SSIS** | Microsoft's own Linux docs state installing SSIS on containers is not supported. No maintained OSS executor exists — the `.dtsx` projects read or convert, none execute. **Hard blocked.** |
| **Teradata** | Vantage Express still exists and is still free, as an **80 GB VM** behind a login, labelled an evaluation copy. No engine container. **Hard blocked by size.** |
| **Informatica PowerCenter** | Container tooling is bring-your-own-licence; the old free local edition is retired. **Hard blocked.** |
| **SAP** | A real trial container exists at a reported ~23 GB compressed with guidance to allocate 16 GB RAM — roughly eight times the entire world budget for one service. **Hard blocked.** |
| **z/OS** | Cannot legally run on the open emulator except in narrow circumstances. The only free-ish OS is a 1970s-era MVS with a murky licence. **Note also that the `s390x` Docker platform is not z/OS** — it is ordinary Linux for IBM Z, with no JCL, no VSAM, no z/OS behaviour. **Hard blocked, and should be rejected on product grounds anyway.** |
| **SQL Server** | Technically fine (measured above). Developer edition may not be used in production, and whether a paid benchmark product counts as production is a legal question, not a technical one. |
| **Oracle Database Free** | The engine **hard-caps itself at 2 GB RAM** — the entire world budget, self-enforced. Free Use Terms reportedly permit redistribution only if end users are charged no additional fee, which directly conflicts with a paid product. |
| **IBM Db2 Community** | Reported ~1.31 GB compressed, 512 MB+ RAM, stated non-production use only; the obvious Docker Hub path carries a sunset notice and a 2022 last push. |

Also confirmed: **Airflow's own docs recommend a minimum of 4 GB.** That is guidance rather than a
hard floor, but combined with the measured 2.53 GB image it settles §7.8's open question 1 — the
98-line deterministic scheduler shim was the right call and should stay.

### 3.5 Composing multiple services without breaking containment

The existing topology already supports this and the change is smaller than it looks: `episode_net`
is declared `internal: true`, which is a **network** property, not a per-service one. Additional
services joined to that network inherit no-egress for free. Four things must hold, and three of them
are not currently enforced:

1. **The grading zone stays on `network_mode: none`.** No added service may be joined to any network
   the grader can reach. This one is currently correct.
2. **`egress_test.py` must run from every service, not just the episode container.** Today it proves
   one container has no egress. With N services that is N−1 unproven claims.
3. **The gateway must remain the only channel.** Adding a Postgres to a world must not let the agent
   connect to it directly — it must arrive through a gateway connector. `gateway/src/alkera_gateway/sql.py`
   currently hardcodes DuckDB, so this is real work, not configuration.
4. **Determinism hazard, not previously noted:** `worlddata` is a shared volume and
   `world/world/fingerprint.py` hashes schemas and canonical contents. A service that writes its own
   logs, PID files or temp state into that volume will perturb the fingerprint nondeterministically
   and break the bit-reproducible reset gate. Every added service needs its writable state on a
   volume that is **outside** the fingerprinted tree.

### 3.6 The fallback ladder, in the order it should be applied

1. **Dialect shim over engine.** Measured: sqlglot 30.18.0 is *already pinned in the gateway lock*
   and ships `teradata`, `oracle`, `tsql`, `redshift`, `snowflake`, `bigquery`, `databricks`
   dialects. A QUALIFY query transpiled from the `teradata` dialect to DuckDB round-tripped
   correctly. **Cost: zero new dependencies, zero new containers.** This generalises what T170
   already does for Redshift, and it is the single most important buildability finding in this
   document. It does **not** give you engine *semantics* — SET-table dedup, NUMBER precision
   binding, empty-string-is-NULL — which the world builder must materialize as consequences.
   (sqlglot has **no** `db2` dialect; measured.)
2. **Generated artifact over application.** `openpyxl` and `XlsxWriter` are reported at under
   300 KB each against ~1 GB for a headless office suite. Writing a damaged workbook directly is
   both cheaper *and more faithful*, because the corruption becomes deterministic and seedable.
3. **File-and-clock over daemon.** A drop-folder theme needs files that appear and change on the
   virtual clock, not an SSH daemon. Adding a daemon adds a service, a credential surface and a
   second network participant to a zone whose entire security model is having no network.
4. **Same base image for everything we build.** Measured 198 MB dedup.
5. **Lazy service start.** Boot a service only when a task's manifest declares it. Most themes are
   T1 and need nothing.
6. **Snapshot restore over seed build.** `entrypoint.sh` already prefers a snapshot tar when
   present; the measured 6.4 s build is the slow path, and it is the one used today.

### 3.7 Honest boot-time answer

For T1 and T2 themes, the sub-60-second target is not close to binding: containers reached running
in ~1 s and the full world build is 6.4 s. For a T3 theme, SQL Server alone reached ready in 13 s
*before* any schema or data was loaded; seeding a realistic warehouse on top of that has not been
measured and is where the 60 s budget would actually be spent. **No T3 theme should be committed to
until someone measures seed-on-top-of-engine, not just engine boot.**

---

## 4. The blocker nobody has hit yet

Found by reading the code, not by running it, and it stops four themes dead:

`Workspace.read` in `gateway/src/alkera_gateway/files.py` ends in `content.decode("utf-8")` with no
`errors` argument. `core.py` catches `UnicodeError`, so the agent receives a clean tool error and
**no bytes**.

Consequently an EBCDIC extract, a cp1252 CSV, a UTF-8 BOM and a packed-decimal field are all
currently **unreadable through the toolbelt**. The tool surface sanitizes away precisely the defect
that themes C1, D1, D3 and D4 exist to plant. `fs_read` needs an optional encoding parameter and a
base64 bytes mode, and `fs_write` needs the symmetric change. Also note `max_payload_bytes` defaults
to 1 MiB, which caps how large a generated extract may be. Recorded as **T295**.

The related good news, measured: CPython 3.11 ships `cp037`, `cp273`, `cp500`, `cp1140`, `cp875` and
`cp424` in the standard library, so a code-page-confusion world needs no new dependency at all.
`cp1047` is **not** in the stdlib, so any task turning on it must ship a codec table. Demonstration,
measured rather than asserted: `"[]!"` encoded as cp037 and decoded as cp500 returns `¬|]` — three
different characters, no exception raised. That is the whole mechanism, and it costs nothing.

---

## 5. What I could not verify

- **Reddit is unreachable.** All three source-harvesting passes failed to fetch any
  `reddit.com` thread. The brief specifically pointed at r/dataengineering as the richest vein of
  legacy war stories, and **this corpus contains zero Reddit sources** as a result. This is a
  tooling limitation, not evidence the threads do not exist. Re-run with a session that can reach
  Reddit before concluding the legacy source pool is thin.
- **Stack Overflow essentially never surfaced**, across all three passes and many query shapes —
  for EBCDIC/COMP-3, for SSIS type coercion, and for CSV delimiter/BOM mechanics alike. I do not
  know whether this is a search-index gap or genuine scarcity, and I am not going to guess.
- **SAP produced zero verified sources.** `community.sap.com` returned 403 on every attempt. C6 is
  listed as a theme and explicitly *not* recommended for authoring because of this.
- **Control-M / AutoSys produced zero incident sources.** The mechanism in C7 is well understood and
  the attestation is missing; only the feature is documented, never a named failure.
- **Informatica's own documentation was unreachable** (403 across many attempts), which means this
  corpus cannot currently support the claim that a PowerCenter workflow reports success while
  rejecting rows. C3's most interesting mechanism is **unanchored**. Do not author it yet.
- **A near-miss worth recording.** A sub-search attributed a precise CP037-versus-CP1047 hex quote
  to an AWS Database Blog migration post. I fetched that post: **the sentence is not on it.** The
  near-identical sentence is on a 2006 mailing-list archive, which I then fetched and confirmed
  verbatim. The source was dropped rather than cited; the incident is recorded in S900's notes. This
  is exactly the failure the round brief warns about, and it happened on the first batch.
- Most `T29x` container figures are **sub-search-reported, not measured by me**. The only legacy
  engine I actually pulled, ran and measured is SQL Server (T285). Everything else in that family
  says so in its own notes.

---

## 6. Record index

**Sources S900–S942 (43).** S900–S914 mainframe / EBCDIC / fixed-width · S915–S928 flat file,
spreadsheet, SFTP · S929 sentinel-file idiom · S930–S942 legacy enterprise ETL.

Only **S915 and S916** are licensed for `frequency` — both peer-reviewed, named, quantified. Every
forum and vendor source is `world_texture` and/or `mechanism` only, per G6.

**Tools T270–T299 (30).** T270–T283 simulate targets (the legacy estate) · T284–T299 substitutes,
harness pieces and explicit rejections.

Four records are **proposed changes to existing components** rather than new tools, and they are
where the cheap wins are: **T284** fixed-width/EBCDIC surface, **T291** sqlglot dialect shim,
**T294** time-based batch dependency mode for the scheduler, **T295** byte-mode file read.

**Gates:** `python implementation_structure/gates/run_gates.py --gate G0 --gate G6` — both pass
(G0: 507 checked, 0 failures; G6: 260 checked, 0 failures).
