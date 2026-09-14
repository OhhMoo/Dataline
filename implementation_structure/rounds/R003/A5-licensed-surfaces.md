# A5 — Licensed surfaces: re-evaluating the R002 drops under "licence blocks redistribution, not simulation"

**Round R003 · 2026-09-14 · agent A5**

The assignment: R002 T1 dropped 21 items, many on licence grounds, and a year-one north star
says a real data engineer's laptop *has* these tools. Re-evaluate every one under the rule
that what we cannot ship as a binary we must still reproduce as an **effect** — the way
fakesnow reproduces Snowflake without a Snowflake binary, and the way T204 reproduces Tableau
without Tableau.

**The headline, up front.** R002's drop table conflated two different decisions: *"do not
ship this product"* and *"do not have this surface."* Re-examined, the 20 items split into
three groups, and only the third is a real drop:

1. **Effect already shipped (7).** Tableau, Power BI, Metabase, Looker-server, Wiki.js,
   Outline, MinIO — the BI-values surface (T204), the seeded docs tree (T205) and the S3
   surface (T109/T201) were built in Track B and are tested (`gateway/tests/`,
   R003 B3 report: 26 passed). The licence rejections were correct and are **moot**.
2. **Effect covered by another surface (5).** Databricks (= notebook + scheduler), Hex (=
   notebook), Sigma (= BI values), Spark (= seeded object-store litter), Airbyte (= the T203
   control plane, in flight today as gateway `ingest_*` tools).
3. **Genuinely absent, conditionally cheap (5).** Slack, GitHub Actions, Confluent Schema
   Registry, LocalStack-beyond-S3, reverse ETL. Zero archetypes touch any of them; each is a
   0.5–2 day fake *when a task is authored that needs it*.
4. **Stays dropped (3).** Oracle the binary (its persona is already kept, as T1's row 15),
   Redpanda (never needed), Mattermost/Rocket.Chat (zero demand, no redeeming mechanism).

Everything below marked **[V]** I verified on disk today (greps quoted in §1). Everything
marked **[T1]** is a claim from `rounds/R002/T1-mock-priority.md` I did not re-run.

---

## 1. Corpus demand, measured not remembered

`apps_touched` across all 41 archetype files in `state/archetypes/` (A001–A064, excluding
`withdrawn/`), counted by awk on 2026-09-14 **[V]**:

| Surface | Archetypes | IDs |
|---|---|---|
| warehouse | 41 | all |
| dbt | 28 | — |
| bi_dashboard | 25 | — |
| airflow | 17 | — |
| git | 15 | — |
| docs_wiki | 14 | A004 A006 A011 A018 A019 A020 A021 A022 A028 A030 A031 A033 A053 A062 |
| notebook + jupyter | 9 | A005 A012 A013 A020 A031 A032 A033 A061 A064 |
| ingestion surfaces | 8 | A010 A017 A018 A022 A025 A051 A061 A064 |
| docs | 4 | A041 A042 A043 A044 |
| catalog | 4 | A019 A021 A052 A062 |
| quality + quality_monitoring | 4 | A013 A020 A032 A064 |
| ticketing | 2 | A011 A029 |
| streaming | 2 | A027 A063 |
| orchestration_secondary | 2 | — |
| semantic_layer | 1 | A030 |
| legacy_database | 1 | A028 |

Case-insensitive whole-file grep over the same 41 files for the dropped product names **[V]**:

- **Zero files:** `slack`, `github_actions`, `power_bi`, `sigma`, `reverse_etl`, `minio`,
  `localstack`, `redpanda`, `schema_registry`, `metabase`, `mattermost`, `rocket`, `chat`,
  `tableau`, `sftp`, `excel`, `mainframe`.
- **Mentions that are provenance, not demand:** `databricks` (4 files: A002 A010 A017 A025 —
  all citing dbt-databricks bug reports S013/S014 as *failure sources*); `spark` (2 files:
  A001 A009 — both citing S755, SPARK-8513, as a *mechanism source*); `airbyte` (3 files) and
  `fivetran` (2 files) — narrative names for the ingestion_connector surface T203 already
  owns; `looker` (A030 — "Looker-style semantic model", i.e. LookML, T1 row 14); `hex`
  (A012 — a comment citing S520 as a hiring-frequency signal, not a surface).
- **False positives worth recording:** `oracle` matched A052 ("the lineage graph is a
  trustworthy oracle" — a common noun) and A063 ("oracle 24/24" — the grading harness). Only
  A028 means the database. `kafka` matched A027 and A063, and A063's own surface note says it
  was *built* as "a deterministic log-and-offset state machine with the same semantics … not
  [a broker]" — Kafka demand satisfied without Kafka.

Theme-level demand (T3's 30 themes, `rounds/R002/T3-environment-themes.md`): Power BI appears
only in C4 (0 archetypes), Spark only in A2 (0), Databricks-shape in B4 (A033), reverse ETL
only in E4 (0), Oracle in C5 (A028 partial). No theme names Slack, CI, Sigma, Hex, Metabase,
or a chat product.

---

## 2. Item by item

Layer key: **(a)** world seed state · **(b)** gateway surface tools · **(c)** network-level
fake (hostname + TLS + wire protocol) · **(d)** static seeded artifacts.

### Group 1 — effect already shipped; the licence rejection is moot

| Tool | Corpus demand | What the engineer experiences | Replication design | Effort | Verdict |
|---|---|---|---|---|---|
| **Tableau** · T113 | 0 name it; 25 touch `bi_dashboard` **[V]** | A number, the definition behind it, when it last refreshed; "the dashboard disagrees with the warehouse" | T204's four objects (dashboard, tile, metric def, refresh event) behind `bi_list/bi_get/bi_cached/bi_update/bi_refresh` — **shipped, tested** **[V]** (`gateway/src/alkera_gateway/bi.py`, B3 report). Tableau is a *name on this surface* (T1's own argument, p. "One build serves fifteen tasks") | 0 marginal | `stays_dropped` as a product; **effect: built** |
| **Power BI** · T114 T139 | 0 archetypes; theme C4 (0 archetypes) | Same four objects, plus an import-mode "last refresh" timestamp | Same T204 surface; C4's pathology (stale import-mode cache) is exactly `bi_cached` vs `bi_render` divergence | 0 marginal | `stays_dropped`; **effect: built** |
| **Metabase** · licence NOASSERTION [T1] | 0 archetypes **[V]** | A saved question with a number and "updated 3 days ago" | Same T204 surface. The NOASSERTION licence question never needs a human read, because nothing of Metabase is needed | 0 | `stays_dropped`; **effect: built** |
| **Looker server** · T115 T140 | 0 archetypes; A030's "Looker-style" is LookML | Explores and Looks over a LookML model; the *model* being wrong, not the server | LookML half survives as T1 row 14 (`lkml`, probed [T1]) + semantic_layer surface; the dashboards are T204 | 0 marginal | `stays_dropped`; **effect: built** |
| **Wiki.js / Outline** · T144 T145 | 0 name them; 18 archetypes touch docs surfaces **[V]** | A page tree, stale content, a last-modified date, search that finds the wrong page too | T205 seeded markdown tree + `docs_list/docs_get/docs_search` — **shipped, tested** **[V]**; rebuilds byte-identical, which a DB-backed wiki does not. Write path: BookStack (MIT) when an archetype grades on editing a runbook (T205's own note) | 0 marginal | `stays_dropped`; **effect: built** |
| **MinIO** · T153 | S3 surface: 4 archetypes (A010 A017 A022 A025) [T1] | An S3 endpoint that stores bytes | adobe/S3Mock (probed round trip [T1]) behind `s3_list/s3_get/s3_put/s3_delete` — **shipped, byte-preservation tested incl. cp037** **[V]** (B3 report); moto (T201, probed, Apache-2.0) as second option | 0 marginal | `stays_dropped`; **effect: built** |

### Group 2 — effect covered by an existing surface

| Tool | Corpus demand | What the engineer experiences | Replication design | Effort | Verdict |
|---|---|---|---|---|---|
| **Airbyte** · T156 (ELv2) | 8 archetypes on ingestion surfaces **[V]**; narrative name in A010 A017 A025 | A connection list and a **sync history that says success while rows are missing**; a schema-change policy that made a bad drift decision | T203's four objects (connector, sync run, table schema config, alert) as gateway `ingest_*` tools over seeded state — **(a)+(b), in flight today** (sibling agent; not yet in `gateway/src/` as of this writing **[V]**). Airbyte's code was never the requirement; its *timeline* was. The ELv2 rejection blocked a product we did not need | 4–6 d [T1], in flight | **`build_now`** — the only one in this document |
| **Databricks** · T112 T166 | 0 in `apps_touched`; 4 narrative mentions are bug-report provenance **[V]**; theme B4 (A033) | Notebooks chained by import side-effects, a job-run history, cluster/library drift | B4's pathology is served by the notebook surface (T104, probed [T1]; 9 archetypes) + the deterministic scheduler's run history + a seeded environment manifest. The Databricks *name* is costume; no archetype grades on the control plane | 0 marginal | `stays_dropped`; reopen only if B4-family archetypes are authored |
| **Hex** · T124 T141 | 0; one comment in A012 citing S520 as a hiring signal **[V]** | A collaborative notebook with app-publish | The notebook surface (Jupyter, probed) covers the working surface; "published app" = T204 dashboard | 0 | `stays_dropped` — zero archetypes is sufficient |
| **Sigma** · T128 T142 | 0 **[V]** | A spreadsheet-shaped BI front end over the warehouse | T204 covers the graded effect (a number, its definition, its refresh). The spreadsheet *input* modality is theme D2's, and D2 is a generated-artifact theme (openpyxl), not a Sigma mock | 0 | `stays_dropped` — zero archetypes |
| **Spark** · T118 | 0 in `apps_touched`; S755 (SPARK-8513) is a mechanism *source* for A001/A009 **[V]**; theme A2 (0 archetypes) | In the mechanisms we actually cite, Spark's footprint is **output litter**: `_temporary/` directories, `_SUCCESS` markers, partial-write files in object storage | **(a) seeded artifacts**: plant `_temporary` and `_SUCCESS` state in the s3 surface. No JVM, no 686 MB image — the experience of a failed Spark job *is a directory listing* | ~0.5 d when an A2/lakehouse task exists | `stays_dropped` as a container; effect is a seeding recipe, `build_when_a_task_needs_it` |

### Group 3 — genuinely absent; cheap; gated on an authored task

| Tool | Corpus demand | What the engineer experiences | Replication design | Effort (honest) | Verdict |
|---|---|---|---|---|---|
| **Slack** · T101 | 0 archetypes; 0 themes **[V]**. *But* GT003's premise — "the figure Finance escalated" — is a Slack-shaped event currently carried only by task-brief prose **[V, tasks/GT003 manifest]** | A channel transcript where a human states a wrong number confidently; the informal claim that contradicts the docs; "did anyone notice this before me?" | **(d) static artifact**: a seeded markdown/JSONL transcript under the docs tree, found by `docs_search`. If a task needs "the thread continued after the deploy", **(b)** two read-only tools (`chat_search`, `chat_get`) over seeded JSONL. No server, no sockets. T1's own drop note already conceded this ("ship a static transcript if texture is wanted") | 0.5 d static; +1 d as tools | `build_when_a_task_needs_it` — upgraded from `drop`: the effect is nearly free and the docs surface already hosts the file format |
| **GitHub Actions** · T111 | 0 archetypes **[V]** | A green check on a commit and a log for why it failed; **CI status as false confidence** — "tests passed" where the test was vacuous is a first-class decoy our corpus currently cannot plant | **(a)+(b)**: workflow YAML in the repo (fs tools already read it) + a seeded `ci_runs` JSON (commit sha, status, log excerpt, virtual time) + `ci_list/ci_get` read tools. `act` [T1] stays the reopen for a task needing real execution | 1–2 d | `build_when_a_task_needs_it` — upgraded from `drop`; the "green CI, broken pipeline" archetype is unauthorable today, and that is a demand-side gap, not a build order |
| **Confluent Schema Registry** (Confluent Community) | 0 archetypes; streaming is 2 archetypes and A063 deliberately has no broker **[V]** | A versioned schema list and a **compatibility check that says no**; "consumer broke after schema v3 went backward-incompatible" | **(b)** a JSON REST fake — the registry API is ~a dozen routes — over seeded schema/version state, planted alongside the log-and-offset state machine. Apicurio (Apache-2.0, T213, *documented* [T1]) if a real server is ever wanted. Licence blocks Confluent's binary; a 12-route JSON API is not Confluent's binary | 1–2 d | `build_when_a_task_needs_it` — upgraded from licence-rejection; a schema-compatibility incident is good material and zero archetypes is the only honest reason not to build it today |
| **LocalStack** beyond S3 (archived, NOASSERTION [T1]) | 0 archetypes touch Secrets Manager, Glue, or any non-S3 AWS **[V]** | An AWS control plane | S3 is shipped (Group 1). For anything broader, moto (T201, Apache-2.0, probed pass [T1]) already covers the API space LocalStack would | 0–1 d | `stays_dropped` — moot, moto absorbs the residual |
| **Reverse ETL** · T129 T159 | 0 archetypes; theme E4 (0 archetypes) **[V]** | A sync that writes warehouse-computed fields *back* into the app DB, with a sync history — circular lineage compounding a definition change | It is **T203 pointed the other way**: same four objects, destination = the Postgres persona (T103, probed). Marginal cost is a direction flag on `ingest_*`, not a new build | ~1 d on top of T203 | `stays_dropped` until an E4 archetype exists; then it is a configuration of the ingestion surface, not a build |

### Group 4 — stays dropped

| Tool | Why |
|---|---|
| **Oracle binaries** · T127 T167 | Unchanged verdict, reframed reason. A028 (the only `legacy_database` archetype **[V]**) needs legacy-*looking* SQL, NUMBER precision and empty-string-is-NULL — **semantics, not a runtime**. Those come from the sqlglot `oracle` dialect shim (T291; T3 §3.6 measured zero-cost transpile **[T1/V there]**) plus the world builder materializing semantic consequences, i.e. T1's row 15 persona. The binary's licence was never the binding constraint; it just ends a debate that did not need to happen |
| **Redpanda** (BSL) | Never had a job. Streaming demand is 2 archetypes; A063 built the effect as a deterministic log-and-offset state machine (graded: oracle 24/24 **[V, A063 record]**), and real Kafka (Apache-2.0) is probed and kept at T1 row 16 for the day a broker is wanted. Redpanda only ever entered the list as "lighter Kafka" |
| **Mattermost / Rocket.Chat** | Zero archetypes, zero themes, and no distinct mechanism — anything a chat server adds over a seeded Slack transcript is a write path no archetype grades on. If comms ever becomes interactive, Zulip (Apache-2.0) is the clean one [T1] |

---

## 3. Priority-ordered build list

1. **Finish the ingestion control plane (T203) as gateway `ingest_*` tools — in flight today.**
   Four objects, ~6 reads, 2 actions, per the frozen scope in the T203 record. This is the only
   `build_now`, and it dissolves the Airbyte, Fivetran *and* future reverse-ETL rows at once.
2. **Nothing else.** Every other row in this document is either already shipped (Group 1),
   absorbed by an existing surface (Group 2), or gated on an archetype that does not exist
   (Group 3). The honest second priority is **authoring**, not building: a comms-decoy
   archetype (Slack transcript), a green-CI archetype, and a schema-compatibility archetype
   would each convert a `build_when_a_task_needs_it` row into a half-to-two-day build with a
   named customer.
3. **When those archetypes land, in this order:** Slack static transcript (0.5 d, rides the
   docs tree) → CI run records (1–2 d) → schema-registry JSON fake (1–2 d) → Spark litter
   seeding recipe (0.5 d, with the first A2/lakehouse task).

## 4. What changed vs. T1's drops, and why

| T1 verdict | A5 verdict | Reason |
|---|---|---|
| Slack · drop | `build_when_a_task_needs_it` (static transcript) | T1's own note conceded the pattern; cost is 0.5 d on an existing surface, and GT003's premise shows the shape already occurs in authored tasks |
| GitHub Actions · drop | `build_when_a_task_needs_it` | "Green CI while the pipeline is wrong" is an unauthorable decoy class today; the fake is seed state, not a runner |
| Confluent Schema Registry · licence rejection | `build_when_a_task_needs_it` | A 12-route JSON API is not the licensed artifact; zero archetypes is the only real objection |
| Airbyte · licence rejection | **moot — `build_now` via T203** | The rejection blocked a product; the requirement was always the sync-history timeline, which is ours to build |
| MinIO, Wiki.js, Outline, Metabase, Power BI, Looker server, Tableau · dropped/licence | **moot — effect shipped** in T201/T204/T205 | The drop table recorded "no binary"; the surfaces were built the same round |
| Databricks, Hex, Sigma, Spark · drop | `stays_dropped` (effect absorbed) | Their graded effects are notebook, BI-values and file-litter surfaces we already have |
| Reverse ETL · drop | `stays_dropped`, ~1 d config of T203 when E4 gets an archetype | Zero archetypes, zero themes with archetypes |
| Oracle, Redpanda, Mattermost/Rocket.Chat, LocalStack · drop/rejection | `stays_dropped`, unchanged | Persona kept (Oracle), never needed (Redpanda), zero demand (chat servers), absorbed by moto (LocalStack) |

---

## 5. What would make a frontier lab disbelieve this environment

The residual gaps no current fake covers, in the order I would worry about them:

1. **There is nothing to look at.** Every surface is API/CLI/JSON. A real data engineer's
   machine is half browser: the warehouse worksheet, the dashboard, the scheduler grid, the
   chat client. For a text agent this is arguably fine — agents experience computers through
   terminals. The disbelief risk is to the **buyer watching a demo**, whose mental model of "a
   working computer" includes a screen, and to any future vision-capable agent. Cheapest
   honest mitigation: `bi_render` can already compute values; emitting a static HTML page per
   dashboard is a day, needs no JS framework, and gives the demo a screen without faking
   interactivity. The same trick covers a read-only scheduler grid and a chat-transcript page.
2. **Humans only enter through the task brief.** Real incidents arrive as a page, a Slack
   thread, an email from Finance — GT003's premise is exactly this and today it exists only as
   prose the harness hands the agent. There is no surface where a *person's* wrong claim lives
   on equal footing with the docs. This is the strongest task-design argument for the Slack
   transcript in Group 3.
3. **The world is frozen between agent actions.** Real laptops have exogenous change:
   marketing renames a column mid-incident; another engineer's deploy lands while you debug.
   Our determinism contract forbids this, and the tension is genuine — but an environment
   where *nothing* moves unless the agent moves it is detectably a diorama. Virtual-time
   scripted exogenous events (seeded, deterministic, replayable) are the honest version of
   this and nobody has scoped them.
4. **No identity or expiry surface.** No SSO redirect, no expired token, no 403 that a
   `USE ROLE` would fix. The Snowflake S2 plan (RBAC subset) covers one corner; everywhere
   else, every credential works forever, which is not what a working Tuesday feels like.
5. **No cost texture outside the Snowflake plan.** Cloud bills, credit burn, egress anxiety —
   a whole on-call genre — exist only in the S2 `WAREHOUSE_METERING_HISTORY` proposal.
6. **No binary artefacts.** No Excel workbooks, PDFs, screenshots. Theme D2 (Excel as the
   integration layer) is the documented gap; openpyxl-generated damaged workbooks are the
   documented fix (T3 §3.6), and zero archetypes demand it yet.

**Position on the UI question, stated plainly:** for grading validity the API-only design is
correct — a faked UI is a fabricated artifact we would then grade against, the same argument
T2 made against faked query profiles. The sales-story gap is real but it is a *demo* gap, and
the fix is static rendering of state we already have, not interactive application servers.
Build the HTML render when the first demo is scheduled, not before.

---

## 6. Caveats

- Demand counts are `apps_touched` fields plus whole-file keyword grep over 41 archetype
  files **[V]**; they do not capture demand a task *brief* might create without naming a
  product. The Slack/GT003 connection is my reading of the GT003 manifest, not an authored
  demand record.
- "What the engineer actually experiences" for Tableau/Power BI refresh semantics and the
  Confluent registry route count are from general knowledge, not from sources in
  `state/knowledge/` — I did not probe any of these products (they are SaaS or
  licence-blocked; probing was never possible). Effort estimates for the unbuilt Group-3
  fakes are estimates without probes, same epistemic status as T1's *documented* rows.
- The `ingest_*` tools are asserted by the round brief to be in flight; as of this writing
  there is no `ingest` symbol in `gateway/src/` **[V]**. If that build slips, Airbyte's row
  reverts from "moot" to "the hole T1 said would hurt."
