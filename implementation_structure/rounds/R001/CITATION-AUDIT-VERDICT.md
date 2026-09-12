# R001 — citation audit verdict (R4a + R4b)

**Date:** 2026-09-12 · Closes the last structural hole in phase 5. G1 proved quotes *match*;
this is the first check that they *support* their claims.

> ## ⚠️ THE EVIDENCE BASE IS WEAKER THAN THE GATES SUGGESTED
>
> R4b audited 19 of 68 in-scope practitioner sources and **10 failed — a ~47% pass rate**,
> far below the 90% bar its own role prompt sets for re-running a phase. Every one of those
> 19 had already passed G1 with a verbatim quote match. **Mechanical citation checking was
> never evidence of source quality**, and the corpus has been treating it as such.

## What a string match could not see

| id | target | type | what |
|---|---|---|---|
| F450 | S004 (+S005–S007) | vendor_as_practitioner | **high** — the hub finding, below |
| F452 | S010 | wrong_page | **high** — an invented vignette recorded as an attested incident |
| F453 | S017 | wrong_page | **high** — a course-site anecdote used as independent corroboration |
| F454 | S013 (+S014) | quote_misread | **high** — closed-and-fixed bugs carried as live traps |
| F451 | S001 (+S002, S003) | quote_misread | medium |
| F455 | S014 | anecdote_as_frequency | medium |
| F456 | S114 | anecdote_as_frequency | medium |
| F457 | S205 | vendor_as_practitioner | medium |
| F458 | S208 | quote_misread | medium |
| F459 | A033 | anecdote_as_frequency | medium |
| F400 | S400 (+S401) | vendor_as_practitioner | **high** — E8 pricing, see C502 |
| F401 | S405 (+S406, S407) | vendor_as_practitioner | medium |
| F402/F403 | S312 | quote_misread / unsourced_number | medium / low |

## The hub finding — F450

The "50 data pipeline postmortems" survey behind **S004–S007** is written by the CTO/founder
of a data-integration vendor and **names not one of the 50 postmortems** — no titles, URLs,
dates, or inclusion criteria. Its headline splits (38% schema drift / 24% backpressure /
19% silent loss / 14% cascade) are therefore unfalsifiable.

S004's own notes say: *"The 38% figure is the most useful number I found for weighting
archetype coverage."* **We were setting task-category weights from an unauditable vendor
blog.**

It is also the centre of a corroboration ring: S001 upgrades a blog anecdote to
"high-frequency" by citing it, and it cites S010 / S013–S015 / S022 back — while F452 shows
**S010 is fiction** and F454 shows **S013/S014 are closed and fixed**. The ring corroborates
itself out of sources that do not hold.

## Archetypes resting on failed sources

| archetype | failed source | via |
|---|---|---|
| A001, A003, A009, A025 | S004 hub | F450 |
| A019, A022 | S010 (invented vignette) | F452 |
| A004 | S017 — its CDC leg now has **zero attested incidents** | F453 |
| A002, A010, A017, A025 | S013/S014 — bug fixed in dbt-databricks 1.11.7, 2026-04-17, carried at `confidence: high` as a live trap | F454 |
| A033 | S122 weight + inert S106 | F459 |

That is **11 distinct archetypes**, spanning much of the data-engineering corpus — none of
which were among the five the red team had already condemned.

## The S122 ruling, which cuts the other way

**S122 is clean; A033's use of it is not.** Every element — the day and a half, the "all
garbage" realisation, the medium confidence, the explicit "one interviewee's anecdote"
caveat — is verbatim-supported. It is one of the more honestly-written records in the set.

A033 is what failed: its `why_hard` makes a class-level claim (*"notebook-driven work produces
exactly this class of incident"*) from n=1, contradicted by S121 which it also cites; **none
of its four planted defects come from S122**; and S106 (MLE-bench agents exhausting container
RAM) is decorative — A033 has no resource-budgeting element at all. Recommended
`derived_from` on redesign: S122 for motivation only, plus S121, S120, S111/S112; drop S106.

**This is the useful shape of the finding: honest sources, dishonest derivation.** The
literature phase was more careful than the strategy phase that consumed it.

## What passed, and it matters

S008 (Astronomer field engineers across customers — frequency framing defensible), S015
(genuinely still open), S022 (real Airbyte report), **S120–S123** (the Berkeley n=18 interview
study — every quote correctly attributed and contextualised, including the two separate
speakers behind the alert-fatigue figures), S207, and S114's factual content. All of R4a's
prior-art adopt/cannot-use verdicts held in context.

## Known-unaudited exposure

- **49 of 68 in-scope sources were never audited**, budget-capped.
- **S118/S119 are second-hand** — a Cambridge survey citing Booking.com KDD'19 and Ackermann
  et al.; the originals were never checked. S118 feeds A020, A031, A032.
- **Concentrated vendor exposure, flagged but unfetched:** S210, S211, S212, S217, S218 are
  all dbt Labs blog content typed as practitioner writing, across A019, A022, A028, A030.
- **S500-series unaudited from the source side**, and A021, A025–A030 lean on it.
- S300–S526 outside R4b's scope; R4a covered only the pricing and prior-art slices.

## Verdict

1. **The practitioner literature phase needs re-running** against the 90% bar, not patching.
   The sample rate was ~47%.
2. **No archetype resting on S004, S010, S013/S014 or S017 should ship** until re-derived.
3. **Task-category weights must not come from S004.** Re-derive from attested incidents only.
4. **Add a source-quality gate.** G1 checks that a quote exists; nothing checks whether the
   page is a vendor blog, a course site, or a constructed vignette. Candidate mechanical
   signals: `author_role` vs the domain's registrant, whether a cited survey names its
   underlying cases, and whether a GitHub issue cited as a live trap is closed or fixed —
   that last one caught F454 and is trivially automatable.
5. **Second-hand citations need an explicit field.** S118/S119 cite papers through a survey;
   nothing in the schema records that, so the distinction is invisible to every consumer.
