# Three tracks — build and validate

**Created 2026-09-14.** Driven by the 2026-09-12 Environments Project meeting.

Three agents build in parallel: **Kimi (A)**, **Astra (B)**, **Claude (C)**. Each owns the
code it already built. **Read §0–§2 before touching your own track**, then read only your own
section. Do not read another track's section and start doing its work.

Background: [`agent.md`](agent.md) — §9 is the ground rules and frozen contracts, §10 is the
repository architecture. Last round's findings: `implementation_structure/rounds/R002/`.

---

## 0. Where the project actually is

R001 and R002 were **research**, and they are done. Everything the meeting asked for as
research now exists on disk:

| Meeting TODO | Where it landed |
|---|---|
| Priority list of tools to mock | `rounds/R002/T1-mock-priority.md` — 43 unknowns resolved, 25 ranked |
| Environment themes (20–30) | `rounds/R002/T3-environment-themes.md` — 30 themes, build-tiered |
| Snowflake mock feasibility | `rounds/R002/T2-snowflake.md` — adopt fakesnow, reject ClickHouse |
| Task archetypes | `state/archetypes/` — 41 designs (32 shippable) |

**The gap is that almost none of it is built, and none of it has been run against a model.**

- **One** world shape exists. T3 found 40 of 41 archetypes live in it.
- **One** task is built (GT001). The other 40 archetypes are YAML designs.
- **Zero** frontier rollouts have ever been attempted. The sub-20% Pass@1 thesis — the number
  the whole project rests on — is still unfalsified.

This phase moves from *"agents generate; machines judge"* to **build the worlds, build the
tasks, and put a real model in front of them.**

### Current state, honestly

```
world/     seed -> world builder, fingerprint, snapshot, scheduler, BI      7 tests green
gateway/   17 tools, MCP + CLI over one audited core                       12 tests green
tasks/     GT001 only                                          TASK VALID, 12 integration
corpus     41 archetypes (32 shippable) · 285 sources · 132 tools · 80 failures · 0 verified
gates      G0-G8 green, except G7 (78 of 79 red-team rulings never executed — by design)
```

---

## 1. Ground rules — all tracks

Non-negotiable. Each was learned by having a red-team agent destroy an earlier design.
Violating one silently invalidates the work. Full list and failure IDs in `agent.md` §9.1.

1. **The grader does not exist during the episode.** It starts after teardown and mounts the
   world read-only. An earlier design scored 11 of 14 points by copying a reachable reference,
   with zero diagnosis (F121).
2. **The agent sees only `agent_visible_brief`.** Author-side prose enumerates the planted
   defects; one draft printed all four "hidden" defects in its `why_hard` (F202).
3. **No network egress from the episode zone.** Containment *and* contamination control.
4. **Official score is fully deterministic for v1.** The LLM judge is shadow-mode only.
   Process signals are report-only.
5. **Destructive operations are flagged, not blocked.** Whether the agent gates its own
   destructive actions is a measurement; blocking destroys it.
6. **Adversarial baselines are CI.** They run on every change to a task, grader, or generator.
7. **Never fabricate.** No invented URL, quote, image tag, version, or measurement. Mark the
   difference between *"I verified this"* and *"I read a claim about it."* A fabricated result
   costs more to detect than it ever saved.

### Two rules this round adds

8. **Measure, don't extrapolate.** R002's most useful correction came from running `dbt parse`
   instead of scaling a number from a bug report — the estimate was wrong by 4–6×. If a claim
   is checkable on this machine in under an hour, check it.
9. **Report your own weakest output.** Every R002 agent ended with the biggest flaw in its own
   work, and those sections were the most valuable part of the round. Do the same.

---

## 2. Frozen contracts

Do not change these without telling the other two tracks. Full text in `agent.md` §9.2.

- **§9.2.1 World layout** — `/workspace`, `/var/warehouse/warehouse.duckdb`, `/var/sched`,
  `/var/bi/dashboards`, and `/grading` which is *never* mounted in-episode.
- **§9.2.2 Action log** — one row per tool call, byte-identical from MCP and CLI.
- **§9.2.3 Task package** — `task.toml`, `instruction.md`, `SEED_SPEC.md`, `solution/`, hidden
  `tests/`.
- **§9.2.4 `grade.py` interface** and **§9.2.5 the agent's final artifact**.

**Never open the warehouse file directly.** DuckDB names the catalog after the file stem, so
`warehouse.duckdb` holding a schema named `warehouse` makes every two-part reference ambiguous.
This made GT001 unsolvable end to end while every track's own tests stayed green. Always:

```python
con = duckdb.connect(":memory:", config={"threads": 1})
con.execute("ATTACH '<path>' AS wh (READ_ONLY)")
con.execute("USE wh.warehouse")
```

### The one contract change this round

**Track B owns a new warehouse-connector interface** (§4). Tracks A and C both consume it.
**B must freeze and publish that interface before building against it**, the way §9.2 contracts
were frozen in R002 — otherwise A and C integrate against a moving target.

---

## 3. Track A — Kimi — More worlds

**Owner rationale:** you built `world/`.

### The problem

T3's finding, and it is the strongest result of R002:

> Of 41 archetypes, **40 live in the modern data stack.** Fifteen of them were authored during
> R002 by three agents blind to each other — and all fifteen came out modern-stack. The
> monoculture is not an authoring preference. **It is an artifact of there being one world to
> author against. Adding authors will not fix it; only adding worlds will.**

Meanwhile the cheapest themes to build are exactly the ones with zero coverage, because what an
engineer actually experiences of a mainframe is a file, a copybook and a deadline — none of
which need a mainframe.

### Deliverables

**A1 — Lift the world shape out of the builder.** Today the shape is hardcoded as module-level
string constants in `world/world/build.py`: `DBT_PROJECT_YML`, `SOURCES_YML`, `STG_SQL`,
`FCT_SQL`, `DASHBOARD`, `README`, `DOC_DATA_DICTIONARY`, `DOC_RUNBOOK`. A world *theme* must
become a declarative spec the builder consumes, not code you edit to make a second world.

**A2 — Build three new world shapes**, all **T1 tier** per T3 §3 (generator changes only, no
new service, no new image):

| Theme | What it is | Signature pathology |
|---|---|---|
| **C1** Mainframe extract + batch window | fixed-width EBCDIC extract, copybook, nightly cutoff | code-page confusion substitutes characters without erroring; packed decimal parses to a plausible wrong number |
| **D1** SFTP-and-cron vendor exchange | drop folders, cron, a loader script | a file read while still being written; a redelivery under the same name with different content, where *both* loader settings are wrong |
| **D2** Excel as the integration layer | workbooks, manual adjustments that live nowhere else | identifiers coerced to dates; leading zeros lost; four IDs collapsing into one at 15 significant digits |

Sources are already harvested and gate-clean: **S900–S942**. T3 §2 ranks these as the top three
by value per unit of build effort, and §6 lists the four *proposed component changes* (T284,
T291, T294, T295) that unlock them.

**A3 — Prove `/grading` is unreachable in the real container.** This is Track A's owed item.
`reference_copy` scores **17/19** in fixtures *by design* — the grader honestly reporting that a
reachable reference can be copied. Only container topology makes it 0. This is a **build gate,
not a test**.

**A4 — Close two determinism hazards T3 found.** `scripts/acceptance.sh` gate 1 builds twice on
the *same* host, which proves far less than the acceptance criterion claims — make it
cross-host. And any service writing logs or temp state into the shared `worlddata` volume
perturbs the semantic fingerprint and breaks the reproducible-reset gate.

### Acceptance

Each new world: identical semantic fingerprint across two from-scratch builds, boots under 60s,
under 2GB, egress fails from inside. `/grading` unreachability demonstrated in a live container,
not asserted.

### Out of scope

The gateway (B), task authoring (C), Snowflake (Andrew), any new heavy container.

---

## 4. Track B — Astra — The real stack

**Owner rationale:** you built `gateway/`. Self-contained and testable in isolation.

### The problem

The meeting asked for *"Docker Compose with isolated containers"* and *"data applications run in
separate container."* Today there is one container and one hardcoded engine.

`gateway/src/alkera_gateway/sql.py` couples to DuckDB in five places — `import duckdb` (line 3),
`duckdb.connect(...)` (line 78), and `dialect="duckdb"` at lines 15, 50 and 119. A world cannot
currently have any other warehouse.

### Deliverables

**B1 — De-hardcode the warehouse.** A connector interface so a world *declares* its backend,
served behind the same `sql_*` tools, producing **byte-identical action-log rows** regardless of
backend. **Freeze and publish this interface first** (see §2) — A and C build against it.

**B2 — The multi-container compose.** Add a second service (Postgres or MinIO — both probed by
T1) joined to the internal `episode_net`. `episode_net` is already declared `internal: true`,
which is a *network* property, so added services inherit no-egress for free. Three things are
not currently enforced and must be:
- `egress_test.py` runs from **every** service. Today it proves one container has no egress;
  with N services that leaves N−1 unproven claims.
- The gateway stays the **only** channel — adding a Postgres must not let the agent connect to
  it directly.
- Added services keep writable state **outside** the fingerprinted tree (see A4).

**B3 — Build the three highest-demand surfaces** from T1's ranked list, weighted by how many
archetypes actually need them — not by product fame:

| Surface | Archetypes needing it | Verdict |
|---|---|---|
| Docs / wiki (seeded markdown tree) | 12 | custom, ~1 day |
| BI values (4 objects) | 15 | custom by choice, 3–4 days |
| Object storage (S3Mock — probed, full round trip) | 4 | `http_intercept`, ~1 day |

T1's framing is worth keeping: four inventory rows (Tableau, Power BI, Looker, Sigma) each
looked like a hopeless mock, but **no archetype needs Tableau** — fifteen need *a number on a
dashboard, the definition behind it, and when it last refreshed.* That is four objects, and it
dissolves all four rows.

**B4 — Lay the fakesnow plumbing, and stop there.** Per T2: fake hostname, local TLS, the
connector route. **Andrew owns the Snowflake semantic build** — deliver the socket so his mock
drops in. Two probed facts to build against: fakesnow implements the **connector wire protocol**
(`/session/v1/login-request`, `/queries/v1/query-request`), and does **not** implement the public
REST API `/api/v2/statements` — that 404s (S951).

### Acceptance

A two-container world runs end to end through the gateway; MCP and CLI produce identical log
rows on both backends; `egress_test` passes from every container; the docs/BI/S3 surfaces serve
the archetypes that name them.

### Out of scope

Task authoring (C), world generation (A), Snowflake semantics (Andrew).

### Licence landmines — check before adopting anything

From T1, verified against the GitHub licence API 2026-09-13:
**LocalStack is archived** (2026-03-23, NOASSERTION) — the reflex AWS-emulation answer is gone.
**Confluent `cp-*` images are Confluent Community licence** even though Kafka itself is
Apache-2.0 — the best-disguised trap in the set. **MinIO is AGPL and archived.** **Metabase
reports NOASSERTION.** One correction the other way: **MetricFlow is Apache-2.0, not BSL** —
anyone working from Transform-era memory will drop a shippable dependency for nothing.

---

## 5. Track C — Claude — Tasks and the difficulty harness

**Owner rationale:** built `tasks/GT001`, holds the red-team and audit history, and this track
is the integration-heavy, multi-agent-parallel one.

### The problem

The meeting: *"Try Fable 5.1 & GPT-6 on these tasks as a sense of how hard these tasks need to
be."* Nobody has ever done this. Every difficulty claim in the repo is **derived from archetype
text, not measured** — including the red team's own cheap-win estimates, which D1 flagged as
such rather than presenting them as measurements.

### C1 — The parallel rollout and difficulty harness

**The agent-under-test runner.** A *fresh, blind* agent receives **only `instruction.md` and the
gateway toolbelt** — no repo access, no manifest, no knowledge of the planted defect, no web.
It works the episode; the full trajectory is captured to the §9.2.2 action-log contract.

**K parallel rollouts per task.** Dispatched concurrently, each graded by the hidden grader.

**Every failure classified by cause** — reasoning, timeout, tool gap, or grader defect. This is
the part that makes the number mean anything: *a 12% pass rate where a third of failures are
timeouts is not a 12% task.* Failures must be attributable to reasoning, not to a missing tool.

**The output is the first real Pass@1 distribution on GT001.** If it lands above 20%, the
harness shows which sub-checks the models cleared, which tells us how to harden it. If it lands
at zero, that is equally informative and probably means a tool gap, not a hard task.

Runnable today with fresh Claude/Fable-family agents as the subject. **GPT-6 requires API
access from Michael**; until then the number is labelled for the family it was measured on.

### C2 — Author the next task batch

Turn the strongest R002 archetypes into runnable tasks, prioritising the shapes the meeting
identified as where models actually fail:

| Batch | Archetypes | Why these |
|---|---|---|
| Compound | A060–A064 | conjunction is the only difficulty lever that survives a capable model |
| Concurrent | A040–A044 | Michael's DA-Comp observation — models struggle most here |
| New-world | *(from Track A)* | at least one, so the task set is not a monoculture either |

E3 and E1 both named their own weakest entry — **cut A064 first** if the batch must shrink, and
attack A044 specifically on whether its evidence trail is too generous.

**Close F513 corpus-wide.** Not one of the 26 R001-era archetypes has an `agent_visible_brief`.
The contract says `instruction.md` is generated *only* from that field, so there is currently no
legal input to the generator — and no attack has ever tested the text the agent actually sees.
The 15 R002 archetypes have it; the older 26 do not.

**Execute or defer the G7 rulings** on any archetype built on. 78 of 79 are open (43 `redesign`,
20 `annotate`, 9 `re-source`, 5 `downgrade_confidence`, 1 `withdraw`). No task ships on an
un-actioned ruling. Record `executed_by` + `executed_on`, or `deferred` + `deferred_reason` —
silence is not closure.

### Acceptance

GT001 carries a real, failure-classified Pass@1 at K≥5. At least two new tasks are TASK VALID
(oracle at max, no-op at 0, every baseline in band) and carry a brief. Every task built has its
rulings closed on the record.

### Out of scope

World generation (A), the gateway and connectors (B).

---

## 6. Dependencies and sequence

```
B1 connector interface  ─freeze first─▶  consumed by A and C
A2 new worlds           ──────────────▶  C2 new-world tasks
B3 surfaces             ──────────────▶  C2 tasks needing docs/BI/S3
```

- **C1 is unblocked** — it runs against GT001 today. Fastest path to the number; start there.
- **C2 modern-stack tasks are unblocked.** New-world tasks wait on A2 and B3.
- **B1 is the critical path for everyone else.** Freeze the interface early even if the second
  backend lands late.

**Integration checkpoint:** one new task, on a new world, through the multi-container gateway,
graded, with a frontier model actually attempting it. R002's lesson was that every track passed
its own tests while the composition was broken — the integration test is the one nobody owns
until someone is assigned it.

---

## 7. Verify your own work

```bash
cd gateway  && .venv/Scripts/python -m pytest tests                    # 12
cd world    && PYTHONPATH=. ../gateway/.venv/Scripts/python -m pytest tests   # 7
cd tasks/GT001-aov-halving/tests && python run_baselines.py            # TASK VALID
                                    python test_integration.py         # 12
python implementation_structure/gates/run_gates.py                     # G0-G8
```

G7 is red on purpose — it lists every ruling nobody has executed. Do not silence it; shrink it.

Gate meanings: **G0** schema · **G1** citation resolves *and* quote matches · **G2** licence and
liveness · **G3** coverage and dedup · **G4** attack cross-linking · **G5** replication probe
actually ran · **G6** source quality, the vividness/attestation split · **G7** rulings executed ·
**G8** archetype claims stay inside their sources' licences.

---

## 8. What is not true yet

Read this before quoting any number from this repo.

- **Nothing has been promoted to `state/verified/`.** Zero, out of 41.
- **9 archetypes are `redesign_required`**; the corpus is 32 shippable, against a target of 40.
- **6 of the original 162 sources are a practitioner describing their own incident** — 3.7%.
  That is precisely the thing this project sells. D2's assessment: *"roughly half needs redoing,
  and it is the half that matters."*
- **Reddit is hard-blocked** from this environment (HTTP 400), confirmed independently by five
  agents. The richest vein of practitioner war stories is structurally unreachable. GitHub
  issues turned out to be the better pool anyway.
- **`reference_copy` scores 17/19** in fixtures. Only Track A's container topology makes it 0.
- **G4 checks that attacks are cross-linked**, not that an archetype survived two independent
  attacks. **G5 confirms a probe was recorded**, not that the thing works in our stack.
- **The difficulty target is an empirical calibration target, not a derivation.** "Five checks
  at 70% gives 17% all-green" assumes independence, and these checks are strongly correlated — a
  wrong diagnosis fails the repair, the idempotency probe and the regression test together.

The recurring lesson, and it has now appeared at three different levels — sources, code, and
rulings: **mechanical verification is not quality.** Every failed source passed its citation
check. Every track passed its own tests while the composition was broken. Every red-team ruling
was written down and none were carried out.
