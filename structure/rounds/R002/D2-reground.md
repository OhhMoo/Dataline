# R002 · D2 — evidence repair

**Agent:** D2 (evidence repair) · **Date:** 2026-09-13 · **ID blocks taken:** sources **S750–S756** (7 of 50 used), failures **F550–F553** (4 of 30 used).

Gate result, repo root, `python implementation_structure/gates/run_gates.py --gate G0 --gate G6`:

| | before | after |
|---|---|---|
| **G6 failures** | **162 of 162** sources | **0 of 262** |
| G0 failures | 486 (345 artifacts) | 0 (523 artifacts) |
| G0/G6 warnings | 0 / 0 | 0 / 8 |

The G6 failure count went 162 → 0. Every one of the original 486 G0 failures was the same three missing fields on the same 162 records. The artifact count grew mid-session because other R002 agents were writing concurrently; their sources arrived already carrying the fields. **All 162 pre-existing sources are classified.** The 8 remaining G6 warnings are the `vividness: high` + short-quote check on S008, S010, S014, S015, S022, S122, S208, S214 — in each case the page carries the specifics and the stored quote does not; worth a later quote-widening pass, not a defect.

---

## 1. A correction I had to make mid-task

The brief told me S001, S005 and S006 were already withdrawn. They never were, and F450's target is S004 (S005–S007 are `also_applies_to`). Worse, **F450 and F451 both recommended `downgrade_confidence` and neither had ever been executed.**

Acting on the wrong premise, my first pass moved **S010 and S017 into `withdrawn/`**. That was an escalation: F452 and F453 both ruled `re-source`, and F452's mitigation explicitly offered "keep S010 with the claim rewritten as *an illustrative scenario in a data-contracts argument describes…*". **Reversed.** Both records are back in `state/knowledge/`, claims rewritten in the rulings' own prescribed wording, typed honestly. `state/knowledge/withdrawn/` is empty and **nothing was withdrawn in this round**. F550/F551 were rewritten from withdrawal recommendations into execution records that keep my own error on the file rather than erasing it.

---

## 2. The finding you asked me to seed — F553

**77 of 78 failure records carry a `recommended_action`. Nothing verifies any of them happened.** F450 and F451 landed 2026-09-12 with precise mitigations; a day later not one instruction had been carried out. S001 still read `confidence: high` with "Author states this is a real incident" in its notes (he does not). S004–S007 still read `source_type: blog` and still carried 38/24/19/14, and S004's notes still called 38% "the most useful number I found for weighting archetype coverage". **The corpus was still weighting its task mix from an unauditable vendor blog a full round after that was formally a high-severity finding — with every gate green.** The brief itself inherited the illusion.

That is worse than the original error. The original error was a mistake; this is a mistake that survived detection.

**Mechanical scan, 2026-09-13** (does the target carry a `repair_note` naming the failure id, or a status change consistent with the ruling?). 26 R001-era rulings show no trace of execution:

```
F101 F102 F103 F104 F105 F111 F112 F120 F121 F122 F123 F124 F125 F126 F127 F128
F202 F255 F400 F401 F402 F403 F454 F455 F456 F457
```

Six more (F502 F503 F505 F509 F510 F514) are R002-era and legitimately in flight. Read the 26 as a worklist, not a verdict — the heuristic cannot see a fix made without being recorded, and F121 in particular appears to have shaped its targets' grading text without ever being cited. **That ambiguity is the finding: there is no way to tell.** The cheapest gate is close to the scan itself — an unresolved failure whose target carries no `repair_note` mentioning its id is a warning.

**Executed in this round, so they come off the worklist:** F450, F451, F452, F453, F454, F455, F456, F457, F458, F459. Details in F553's `mitigation`.

---

## 3. The 11 damaged archetypes

**9 re-grounded · 2 marked broken.**

| archetype | verdict | what was withdrawn/demoted | what replaced it |
|---|---|---|---|
| **A001** | re-grounded | S005, S006 dropped (vendor survey, F450); S001 retyped `unattested` | **S754** (dbt docs: append-only without `unique_key`), **S755** (SPARK-8513), S008 |
| **A002** | re-grounded | S013 → `medium`, both dbt-databricks issues **closed and fixed** | **S753** (dbt-clickhouse #258, verified OPEN); version-pin constraint now explicit |
| **A003** | re-grounded | S005 dropped — it was the *entire* core mechanism; S003 → `world_texture` only | **S756** (PostgreSQL three-valued logic), S008 for the DST leg |
| **A004** | re-grounded — **biggest win** | S017 re-sourced; S016 → `world_texture` only. CDC leg had **zero** attested incidents | **S750** (Zalando, `firsthand_named`), **S751** (Debezium list, `firsthand_anonymous`), **S752** (PostgreSQL docs) |
| **A009** | re-grounded, one leg flagged | S006, S023 → `world_texture` only | S754, S755, S008 |
| **A010** | re-grounded | same liveness correction as A002 | S753; leans harder on S022 (Airbyte #77627, verified OPEN) |
| **A017** | re-grounded (lightest) | S013 retyped; S002 → `unattested` | primary source S022 was never damaged |
| **A019** | **redesign_required** | S010 dropped; S210/S211/S217/S218 all → `vendor_marketing` | **nothing found** — see below |
| **A022** | re-grounded | S010 dropped; S205 → `world_texture` only | S207/S208 (`firsthand_named`) carry it; S208's arithmetic corrected per F458 |
| **A025** | re-grounded | S004 dropped | S753; S508/S511/S512 kept as texture only |
| **A033** | **redesign_required** (already was) | S106 dropped as decorative (F459) | S120, S111, S112 added to source the fit-before-split defect |

### A019 is the one that broke — F552

Its load-bearing mechanism is the BI-layer rename boundary that "exists nowhere in any system". The only record stating that is **S217 — a dbt Labs blog post.** S210, S211 and S218 are the same channel. Once typed honestly, all four cap at `world_texture` by the F400 rule, and S216 (the one genuine `firsthand_anonymous` practitioner voice) attests that a rename has a wide unowned blast radius, not *how* the boundary comes to be unrecorded. **After honest typing, the archetype's entire `why_hard` rests on a vendor blog post — the F400/F450 shape one layer up.** I could not find a replacement. S207 (Thicket, `firsthand_named`) would support a redesign around failure-to-zero coercion instead.

### Three honest gaps I did not paper over

- **A001/A009:** S755 attests a failed write leaves `_temporary` output behind. **No source attests the second half** — that the next scheduled run silently consumes it. Labelled a design choice, not cited.
- **A009:** the table→incremental materialisation-change leg came only from S023, now `vendor_marketing`/texture-only. **No replacement found.** Flagged in the archetype, not quietly left.
- **A033:** three of four planted defects (out-of-order cells, overwritten CSV, unpinned seed) still have no source.

### A026 and A029 — outside my 11, but affected

F451 named them and the brief's list did not. Both cite S001/S002/S003; A029 also cites S205. I annotated both with the licence change rather than re-deriving them — I have not read them end to end.

---

## 4. Sources added — S750–S756, all quote-verified

Every quote was checked with **gate-identical logic** (urllib + `<[^>]+>` strip + whitespace collapse + case-fold substring) before writing, and re-checked after. All 7 pass exact match; none is a partial.

| id | what | attestation | verified |
|---|---|---|---|
| S750 | Zalando: uncontrolled WAL growth on **low-activity** DBs persists even with Debezium heartbeat | `firsthand_named` | 142 chars exact |
| S751 | Debezium list: slot 152 kB → 16 MB after three inserted rows, Debezium 0.8.3 | `firsthand_anonymous` | 86 exact |
| S752 | PostgreSQL docs: WAL-retention guarantee | `unattested` | 151 exact |
| S753 | dbt-clickhouse #258, **verified OPEN** via GitHub API | `firsthand_anonymous` | 126 exact |
| S754 | dbt docs: append-only without `unique_key` | `unattested` | 151 exact |
| S755 | SPARK-8513: failed write leaves `_temporary` | `firsthand_named` | 103 exact |
| S756 | PostgreSQL docs: three-valued logic / `IS DISTINCT FROM` | `unattested` | 97 exact |

**Liveness re-checked independently via the GitHub API, 2026-09-13** — confirming F454 and catching what F454 caught: dbt-databricks **#1211 closed, zero comments**; **#1289 closed**, labelled *not an issue* / *external issue*, fixed in **dbt-databricks 1.11.7 (2026-04-17)**. Still open and safe: dbt-adapters #601 (S015, 9 comments), airbyte #77627 (S022, 6 comments), dbt-clickhouse #258 (S753). **Four archetypes now state the version-pin constraint that F454 punished their absence of.**

### Rejected — did not write these

- **dev.to "The Outage That Wasn't in the Runbook: A Timezone Bug Postmortem"** — fetched, and it is a generic illustrative scenario with no error strings, cron expressions, offsets, table names or versions, framed as "the first post in a recurring series". Exactly the S010/S017 shape. A tempting fill for A003's timezone leg; rejected.
- **SPARK-39195** — description not retrievable from the JIRA page and the fix was later reverted (SPARK-48292). Not written rather than written at `confidence: low`.

---

## 5. Back-classification — 162 of 162

I did S001–S125 (47 records) by hand; two subagents did S200–S218 + S500–S526 (46) and S300–S447 (67), and I applied their mappings through a validator that enforces both hard rules mechanically.

| attestation | n | | vividness | n | | usable_for | n |
|---|---|---|---|---|---|---|---|
| `unattested` | 67 | | high | 19 | | `world_texture` only | 90 |
| `secondhand` | 41 | | medium | 83 | | `+ mechanism` | 52 |
| `firsthand_named` | 24 | | low | 60 | | `+ frequency` | 13 |
| `vendor_marketing` | 24 | | | | | `+ mechanism + frequency` | 7 |
| `firsthand_anonymous` | **6** | | | | | | |

**20 of 162 sources may license a frequency claim.** They are: S008 S018 S019 S104 S105 S111 S112 S113 S120 S121 S125 S201 S414 S416 S423 S435 S436 S438 S444 S445.

Two numbers in that table are the story.

**`firsthand_anonymous` = 6.** The brief says to weight heavily toward practitioners describing failure in their own voice — Reddit, Stack Overflow, HN, open issues. That is 3.7% of this corpus. The practitioner literature phase produced almost no practitioner voice; it produced blogs *about* practitioners, vendor content, and academic studies. The 47% audit failure was not bad luck, it was the predictable result of that composition.

**`vendor_marketing` = 24, and 90 of 162 sources are `world_texture`-only.** Over half the corpus can now legally supply nothing but flavour.

---

## 6. What I could not verify

- **Reddit is hard-blocked.** `reddit.com` returns HTTP 400 to the research user-agent, and the gate's urllib fetcher cannot reach it either, so a Reddit URL could never pass G1 anyway. **r/dataengineering — the brief's first-named source pool — is structurally unavailable to this project.** This is the single biggest reason `firsthand_anonymous` is at 6.
- **Microsoft Fabric / Power BI community threads 403-block the gate** and are citable only through Wayback, as S216 already is. That closed off the most promising replacements for A019.
- **`api.stackexchange.com` is not fetchable**, so I could not search Stack Overflow systematically by view count.
- Subagent flags I did not verify myself: **S447's quote is corrupted as stored** ("we interviewed 1 18 people" — a footnote marker fused to the number) and will not match verbatim under G1; **S505's quote** (a sentence of Berkeley project history) does not support its claim about operational realism or licensing, having been swapped purely to satisfy G1; **S413's claim** (a 10–20x ratio) is absent from its own quote. Also **S309** contains a real n=1,407 hand-labeled study that the vendor-page rule strips of its mechanism licence — the first record to revisit if the model ever grows a `vendor_firsthand` tier.

---

## 7. How much of this corpus is actually trustworthy

**Roughly half needs redoing, and it is the half that matters most.**

The sharpest way to say it: **6 of 162 sources are a practitioner describing their own incident.** Everything this project sells — realistic, rebuildable data-agent failures — is supposed to come from there. Meanwhile 90 of 162 can license nothing but texture, and only 20 can support a claim that anything is common.

What I would actually trust:

- **Solid (~25 sources).** S020/S021 (GitLab's own postmortem), S022 (Airbyte, open, named versions, control experiment), S011/S012 (Airbnb), S111/S112 (Princeton leakage), S120–S123 (Berkeley n=18), S207/S208 (Thicket), S008 (Astronomer), S125 (Google n=53), plus S750–S756. These attest what they are cited for.
- **Usable as texture, worthless as evidence (~90).** The `world_texture`-only block. Fine, provided nobody cites them for mechanism or frequency again — which is precisely what kept happening.
- **Needs re-doing (~50).** The whole practitioner-literature slice. The R001 verdict said "the practitioner literature phase needs re-running against the 90% bar, not patching." **I patched it. It still needs re-running** — I repaired the records that 11 archetypes stood on, and I did not touch the question of whether the pool was ever sampled from the right places. It was not: the pool has almost no forum, issue-tracker or postmortem content, because the two richest sources of it are network-blocked.

Two structural cautions:

1. **G6 checks that a source declares what it may license. Nothing checks that an archetype stays inside its sources' licences.** That gap is exactly where F459 landed ("honest sources, dishonest derivation") and it is still wide open. An archetype can cite six `world_texture`-only sources and assert a mechanism, and every gate passes. Of the two archetypes I marked broken, one broke *only* because I read its licences — no gate would have caught A019.
2. **Nothing verifies that a ruling was acted on** (F553). Until that gate exists, every finding in this report has the same half-life as F450 and F451 had: one day.

The corpus is now *honest about itself*, which it was not this morning. That is not the same as being strong.

---

## 8. Biggest weakness in my own output

**I trusted two subagents' judgement on 113 of 162 classifications and spot-checked rather than re-derived them.** Their reasoning was careful and both flagged their own hard cases unprompted, but roughly 70% of the back-classification is not my own reading. If one of them was systematically generous on `attestation`, I would not have caught it.

Second: **my archetype re-grounding is a licence audit, not a design review.** I checked whether each archetype's sources can support what it claims. I did not check whether the archetypes are still *good* — several carry unresolved high-severity red-team findings (F120, F121, F509, F510) that this pass did nothing about.

Third, and I want it on the record: **I escalated two `re-source` rulings into withdrawals and had to be corrected.** The instinct to make the corpus look decisively cleaned is the same instinct that produced the problem I was sent to fix.
