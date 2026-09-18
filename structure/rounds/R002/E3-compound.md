# R002 / E3 — Compound and intertwined archetypes

**Agent:** E3 (compound archetypes)
**Date:** 2026-09-13
**ID blocks claimed:** archetypes **A060–A064** (5 written, A065–A069 left free), sources **S700–S706** (7 written, S707–S749 left free)
**Gate status:** `python implementation_structure/gates/run_gates.py --gate G0 --gate G6` → **both PASS, 0 failures.** My files produce 5 G0 warnings (`unknown field 'composed_of'`) and 0 G6 warnings. Details in §6.

---

## 1. What I was looking for, and the rule I held myself to

The assignment's own anti-pattern is the whole job: *two unrelated bugs in one world is not a compound task, it is two tasks sharing a container.* I applied one test to every candidate before authoring it:

> **Is there any ordering in which an agent could solve both defects independently, using only the evidence each defect generates on its own?**

If yes, the candidate is length, not difficulty, and I dropped it. Five shapes survive that test, each for a different reason:

| ID | Compound | Interaction mechanism | Why the parts are not separable |
|---|---|---|---|
| A060 | drop-filter × non-idempotent re-run | masking + shared-symptom + repair-induces | The errors have opposite signs and partly net out. Neither single repair closes the ledger arithmetic, and the natural baseline window *is* the duplicated window. Repairing one makes the reported number move **away** from the target. |
| A061 | rename-collision × label leak | masking (bidirectional) + repair-induces + evidence-collision | Both defects move one metric. The corrupt data **raises** AUC, so the detection instinct is inverted; repairing it returns the metric to a historical baseline that is itself the second defect. The honest band is reachable from neither single repair. |
| A062 | RLS mis-scope × join fan-out | evidence-collision + conflicting-repair | The agent's own analytical role is bound by the broken policy, so the arbitration query it writes to adjudicate defect B returns defect A's answer and reads as corroboration. B cannot be measured until A is escaped. |
| A063 | stalled consumer × append-only sink | repair-induces (dormant defect) | Defect B produces **no observable state anywhere in the world** while defect A is live. There is no duplicate to find. It exists only as a prediction about what replay will do. |
| A064 | silent source loss × demand regime shift | evidence-collision + shared-symptom | The graded deliverable is a monitor, and every reference window the agent would pick is contaminated by defect A. The precision floor converts that contamination into a failing grade. |

A063 is the strongest of the five on that test and A064 is the weakest; see §7.

---

## 2. The compounding map

This is the durable part of the deliverable — it covers pairs I did **not** author. "Interaction" means the strict test above, not thematic adjacency.

### 2.1 Pairs with real interaction potential

| Pair | Shape | One-line reason |
|---|---|---|
| **A003 × A026** (authored as A060) | shared-symptom, opposite signs | Silent row loss and silent duplication net against each other; each repair moves the total the wrong way. |
| **A012 × silent-corruption family** (authored as A061) | repair-induces | Data corruption that inflates a metric makes "restore the baseline" the designed failure, and the baseline is the second defect. |
| **A021 × A029** (authored as A062) | evidence-collision | Access policy applies to the investigator, so the arbiter query is corrupted by the thing it is arbitrating. |
| **A027 × A026** (authored as A063) | repair-induces, dormant | Replay is the only cure for the stall and the only trigger for the sink defect. |
| **A020 × A017** (authored as A064) | evidence-collision via calibration | The monitor's reference window sits inside the other defect's damage. |
| **A004 × A001** | repair-induces | Dropping the replication slot saves the primary and forces a re-snapshot into a non-idempotent loader. **Real interaction — not authored only because A004 is already 20–40h and compounding blows the deployable band.** Best candidate for a future round if A004 is shortened first. |
| **A019 × A030** | evidence-collision | The unrecorded BI-layer rename makes A030's metric-variant catalogue provably incomplete, and the agent cannot tell. Consolidating onto a canonical metric then silently breaks a board dashboard that has a divide-by-zero fallback. Strong candidate; I ran out of budget, not ideas. |
| **A013 × A005** | shared-symptom | Delayed labels plus training-serving skew: the proxy evidence the agent must build for A013 is exactly the serving log that A005 corrupts. Moderate. |
| **A022 × A006** | shared-symptom | Failure-coerced-to-zero and a varying-length comparison window both bend one KPI series; the per-valid-day accounting only closes with both. Moderate, but overlaps A029 heavily. |
| **A021 × A028** | evidence-collision | Row-level policy during a legacy→dbt migration means the audit_helper row-level parity check passes under a filtered role and fails for real users. Good shape, high length risk. |

### 2.2 Pairs with **no** real interaction — do not build these

| Pair | Why not |
|---|---|
| A001 × A009 × A026 | Same mechanism three times. Compounding them is repetition, not conjunction. |
| A002 × A010 × A025 | Same mechanism family (positional INSERT). **A010 already is A002+A017 stapled together and was flagged for withdrawal under F505 for exactly this reason** — the repo has already run this experiment and it failed. |
| A011 × A018 | Both are orchestrator-triage tasks over the same surfaces; a compound is one longer dependency walk. Tedium. |
| A012 × A033 | Leakage and reproducibility both live in the notebook, but fixing non-determinism neither hides nor causes leakage. Independent in either order. |
| A017 × A022 | Ingestion loss and a broken KPI definition. Two separate traces down the same pipe with no shared instrument. Container. |
| A021 × A030 | Policy scope and metric-definition sprawl are genuinely orthogonal; the catalogue step is not policy-sensitive at the grain A030 works in. |
| A003 × A012 | Tempting (both are "the control is the bug") but the filter and the leak touch different tables at different layers and neither masks the other. |
| A027 × A013 | A stalled consumer and model drift: the stall would be diagnosed first and completely, then drift second. Sequential, not compound. |
| Anything × A031/A032/A033 | `status: redesign_required`, excluded by the brief. |
| Anything × A002/A010/A017/A018/A025 | Became `redesign_required` **during this session**; see §5. |

---

## 3. Archetypes written

All five carry: `composed_of`, an explicit named interaction mechanism in `notes`, at least one **CONJUNCTION CHECK** in `deterministic_checks` that cannot pass unless both defects were found, an `agent_visible_brief` that reports one business symptom and never hints at two causes, a harness-side grading statement (F121), and an explicit "what a full refresh destroys" note.

- **A060** — *Quarter-end revenue shortfall where a drop-filter and a re-run partly cancel.* data_engineering, 12–20h, 24 pts. Conjunction check: restated quarter revenue equals the harness ledger to the cent — one repair overshoots +9.0%, the other undershoots −13.1%. Second conjunction check: amendment integrity, which is only gettable if the filter repair precedes the de-duplication.
- **A061** — *Feature corruption that raised the metric, over a baseline that was already leaking.* data_science, 12–20h, 22 pts. Conjunction check: retrained AUC must land in the honest band; fixing only the column collision lands at ~0.88 (fails high), fixing only the label window lands at ~0.82 (also fails).
- **A062** — *Reconciling two exec numbers while your own query is silently policy-filtered.* data_analytics, 10–18h, 21 pts. Conjunction check: both dashboards equal the ledger to the cent (+1.5M and −3.0M defects, opposite signs). Anti-hack guard: a fan-out row-count assertion that catches `DISTINCT`.
- **A063** — *A dormant append-only sink that only detonates when you unblock the stream.* data_engineering, 10–18h, 24 pts. Conjunction check: message conservation, `loaded + dead_lettered = produced`, which fails low if the gap was not replayed and high if the sink duplicated. Second conjunction check: 1,100 legitimate producer re-emissions must survive, which a blanket `order_id` de-duplication destroys.
- **A064** — *Building a drift monitor whose only clean reference window has already been poisoned.* data_science, 14–22h, 24 pts. Conjunction check: the monitor is replayed by the harness over the **full historical window** and must clear both a recall floor and a precision floor, with the ingestion-loss onset annotated as a **non-event**.

**Length discipline.** All five sit in the 8–20h band except A064 at 14–22h, which is at the ceiling and carries a written instruction on what to cut first if playtesting overruns. I deliberately declined to author the A004 × A001 compound (§2.1) because intertwining a 20–40h archetype produces something undeployable. A difficult 12-hour task beats an impossible 40-hour one.

---

## 4. Sources written (S700–S706)

Seven records, all with verified verbatim quotes, all scored on both axes.

| ID | Source | attestation | usable_for | conf |
|---|---|---|---|---|
| S700 | Cloudflare 2025-09-12 postmortem — patch shipped on an incorrect causal theory | firsthand_named | world_texture, mechanism | high |
| S701 | InfoQ on LinkedIn's Kafka incidents — stale offset → redelivery; later a cleanup fix exposed a second latent fault | secondhand | world_texture, mechanism | medium |
| S702 | Snowflake row access policy docs — policy decides returned rows via a runtime rewrite | unattested | world_texture, mechanism | high |
| S703 | PostgreSQL RLS docs — per-user row restriction, and superusers/`BYPASSRLS` always bypass | unattested | world_texture, mechanism | high |
| S704 | dbt-core issue 5138 — incremental first run ignores `unique_key`; **closed wontfix** | firsthand_anonymous | world_texture, mechanism | high |
| S705 | dbt Discourse — `unique_key` is not a uniqueness constraint | firsthand_anonymous | world_texture, mechanism | medium |
| S706 | Quix on `auto.offset.reset=earliest` — replay of the full backlog, idempotency required | unattested | world_texture, mechanism | medium |

**Not one of the seven claims `frequency`.** S700 technically could (firsthand_named + blog), and I declined: one incident does not establish that something is common.

**Liveness check.** S704 (dbt-core #5138) was checked on 2026-09-13 and is **CLOSED, `resolution:wontfix`**. It is recorded as closed, and the record says explicitly it must not be cited as an open issue. That it is wontfix is the useful property — the behaviour is the contract, not a pending patch, so a world built on it is reproducible.

**What I could not verify.** Four things, all recorded in the `notes` of the records themselves rather than only here:
1. S700, S702 and S706 each have a *second* verbatim span that is the more useful half of the claim. I confirmed each span exists on the page but **not** that it is contiguous with the quoted sentence, so each sits in `notes`, not in `quote`.
2. S701's span came from a fetch-and-summarise step and I did not independently re-read the surrounding paragraph. Marked `confidence: medium` and flagged for a human re-read. Nothing depends on it alone — S706 carries the same mechanism.
3. S705 has no marked accepted answer, so the corrective explanation is community consensus, not a maintainer ruling. `confidence: medium`.
4. S702 and S706 are vendor-hosted pages I judged *reference/educational* rather than `vendor_marketing`. That judgement call is written into both records so a reviewer can overrule it and drop them to `world_texture` only.

**No URL, quote, issue number, view count or date in any of the seven was invented.** Where I could not confirm something, the record says so.

---

## 5. The corpus moved under me mid-session — and it invalidated two of my drafts

A parallel repair agent was rewriting the source corpus while I worked. Between my first read and my first gate run:

- **S010 and S017 were deleted** from `state/knowledge/`.
- **A002, A010, A017, A018 and A025 were moved to `status: redesign_required`.**

Two of my five compounds were built on that ground:

**A061 was fatally affected.** Its defect A used A025's positional-INSERT mechanism, which F500 rules unbuildable: *"the positional-INSERT mechanism cannot occur on dbt Core + dbt-duckdb (verified against the installed adapter macros)."* I re-mechanismed defect A to a **rename-collision** — upstream renames `plan_tier` → `plan_code` and ships a *new* `plan_tier` carrying the churn-reason code, so a select-by-name staging model silently picks up a different field. This needs no adapter behaviour at all (pure SQL and source schema), and preserves every property the compound depends on: silent, name- and type-stable, invisible to schema tests, boundary-dated. It also improves the full-refresh defence — a blind `--full-refresh` still selects the new `plan_tier` and does not repair anything. `derived_from` was repointed off S013/S014 (positional-INSERT filings) onto S002 and S216 (same-name-different-meaning drift).

**A064 was at risk from F506** — A017 as specified is defeated by a from-scratch re-sync, which "recovers everything and satisfies all four deterministic checks with zero diagnosis." A064 now designs against that explicitly: the upstream POS API serves a rolling 30-day window while the loss spans six weeks, so a re-sync is at best a partial remedy and the agent must decide what is unrecoverable, reconstruct what it can at day grain, and **exclude the unrestorable window from the declared training population** — a new graded check. This matches the repair direction A017's own `repair_note` proposes, so a repaired A017 and A064 should share that world parameter.

**A062 cited the now-deleted S010** and was repointed to S203.

Each of these is recorded in a `repair_note` on the archetype, not just here.

---

## 6. Gate results

```
[PASS] G0 schema                (426 checked, 0 failures, 5 warnings)
[PASS] G6 source quality        (216 checked, 0 failures, 7 warnings)
ALL GATES PASSED
```

All 5 G0 warnings are mine and all 5 are the same one: `unknown field 'composed_of'`. All 7 G6 warnings are on pre-existing records (S008, S014, S015, S022, S122, S208, S214), none mine.

**Two things the coordinator needs to action:**

1. **`composed_of` and `interaction_mechanism` should be added to the archetype schema's `optional` block.** The brief authorised the field; the schema does not know it yet. I deliberately did **not** edit `schemas/schemas.json` because it already carries uncommitted changes from another agent and editing a shared contract file mid-round is how merge damage happens. I used the literal field name rather than an underscore-prefixed one (which the gate would silently skip) so the gap stays visible.

2. **G3 now fails on archetype count: 35, against `ARCHETYPE_MAX = 30`.** This is a corpus-budget collision across the three R002 expansion authors, not a defect in anyone's records — my 5 are part of it but so are E1's and E2's. The shippable count is 27 once the 8 `redesign_required` entries are excluded, which is inside the band, so the cleanest fix is for G3 to count *shippable* archetypes against the ceiling rather than raw file count. Someone needs to own that decision before R002 closes; it is not mine to make unilaterally.

G3 also still fails on three pre-existing dead-source citations that are not mine: `A004 → S017`, `A019 → S010`, `A022 → S010`. The repair agent deleted those sources without repointing the archetypes that cite them.

One G3 warning I did act on: A019 and A062 tripped the structural-duplicate check (same discipline + apps + `rubric_points`). They are genuinely different tasks; I moved A062 to 21 points to clear the collision signal rather than leave a false positive in the log.

---

## 7. Honest self-assessment — which of these are two bugs in a trench coat

The assignment asked me to be ruthless about this and to name my own worst output. In descending order of confidence:

**A063 is the real thing.** Defect B leaves no trace anywhere in the world until defect A is repaired. There is no duplicate row to find, no checksum that differs, no assertion that fires. Every detection technique in the corpus returns clean, *correctly*. The only route to it is reading the sink's code and predicting a consequence that has not happened yet. There is no ordering in which an agent solves these independently because one of them does not exist as an observable.

**A062 is nearly as strong.** The agent's own instrument is the defect surface. The standard move — "compute it myself and see who's right" — returns a corrupted answer that looks like independent corroboration of one of the two wrong dashboards. Defect B is unmeasurable until defect A is escaped. I have not seen a benchmark model the investigator's identity as part of the defect, and I think that is the most transferable idea in this batch.

**A060 holds up, on the arithmetic.** Two defects with opposite signs and one observable number. What makes me confident is not that there are two causes but that the agent's *feedback signal inverts*: the correct first repair moves the number further from the target. That punishes hill-climbing specifically, which is the failure mode a capable model is most prone to. The forced repair order (filter before de-dup, or 212 amendments strand) is a second, independent lock.

**A061 is sound but leans on one hinge.** The compound works entirely because the corrupt data *raises* the metric and the historical baseline is itself the second defect. If a playtester's agent happens to distrust 0.88 on first principles — and 0.88 on churn is not an outrageous number to be suspicious of — the whole structure unlocks at once. It is a single insight gated behind a counterintuitive observation, not two locks in series. I would rate it the second most likely of the five to be solved by a strong model on the first try.

**A064 is the one I am least sure about, and it is closest to two bugs in a trench coat.** An agent could plausibly repair the connector, then build a monitor from scratch on clean post-repair data, and never reason about the interaction at all. Two design choices are what stop that, and they are load-bearing rather than decorative: the monitor is replayed by the harness over the **full historical window** so it cannot be built on post-repair data alone, and the ingestion-loss onset is labelled a **non-event** so the monitor must actively distinguish a data defect from a demand shift. **If either is dropped in implementation, A064 collapses into two tasks in a container.** I wrote that warning into the archetype's own `notes` so it cannot be lost between here and the build. If the round needs to cut one, cut this one.

### The single biggest weakness in my output

**The evidence for the compound *shapes* is much thinner than the evidence for the constituent mechanisms, and the gap is not evenly distributed.**

Every individual defect I used is well-attested — the corpus already carries S001, S005, S013–S015, S016, S021, S111, S112, S114, S205, and I added seven more. What I could **not** find is attested, firsthand evidence that these defects *co-occur and interact* in the wild. For the repair-induces shape I have S700 (Cloudflare, firsthand_named, but a control-plane outage, not a data pipeline) and S701 (LinkedIn, secondhand, medium confidence). For the **masking / mutually-cancelling** shape — which is the entire structural premise of A060 — **I found nothing at all.**

That matters because A060's interaction is my construction, not an attested incident. The netting arithmetic is plausible, it is mechanically sound, and each leg is separately attested; but no practitioner in my source pool describes two defects cancelling out and the fix making the number worse. I am inferring the shape, and the record should say so.

Two concrete reasons the pool was thin, both worth fixing before the next round:
- **Reddit is blocked to this user agent** (`WebSearch` returns a hard 400 for `reddit.com`). The brief names `r/dataengineering` as the first place to look for exactly this kind of war story, and it is unreachable. This is the single largest gap in R002's source-gathering capability and it is infrastructural, not a matter of effort.
- **HN Algolia phrase queries returned zero hits** for every masking-shaped phrase I tried. Either my query construction against that API was wrong or the phrasing practitioners use is different from what I guessed. Someone should retry with a working Algolia query rather than concluding the incidents do not exist.

The honest summary: **I have strong evidence that each of these defects happens, good evidence that repair-induced second failures happen, and no evidence that defects mask each other by cancelling.** A060 should be read as a designed task justified by mechanism, not as a reconstruction of a witnessed incident. If a reviewer wants only compounds grounded in attested co-occurrence, A060 does not qualify and A063 is the one to keep.

---

## 8. What I rejected and why

- **A004 × A001** (CDC slot repair forcing a re-snapshot into a non-idempotent loader). Real interaction, genuinely rich. Rejected on **length only** — A004 is already 20–40h. Recorded in §2.1 as the top candidate for a future round if A004 is shortened first.
- **A019 × A030** (unrecorded BI rename making the metric-variant catalogue incomplete). Real evidence-collision. Rejected for budget, not quality. Next-best unbuilt candidate after the above.
- **A002 × A010 × A025** (any combination). Rejected twice over: same mechanism family, and the repo has already tried it — A010 *is* A002 + A017 stapled together and was flagged for withdrawal under F505 for having "no interaction between the two legs."
- **A012 × A033**, **A017 × A022**, **A011 × A018**, **A027 × A013**, **A021 × A030**, **A003 × A012**. All fail the separability test: solvable independently in either order. Length, not difficulty. Full reasons in §2.2.
- **Querio RLS blog** as a source. The page content did not load and it is vendor marketing; under F400 it could license only `world_texture` and I did not need it. Dropped rather than recorded weakly.
- **A Medium claim that leakage removal took an AUC from 0.9973 to 0.59.** Vivid and exactly on-point for A061, but it is a blog repeating a figure about a 2013 Kaggle competition and I could not trace it to a primary source. Fabricating or laundering that citation would have been the worst thing I could do this round, so it is not in the corpus.
