# The coupling filter — layer 3 of the contract-maintenance generator

## The rule

> Two stressors compound only if they share state — same table, checkpoint,
> repair window, or metric. Mechanically checkable: perturb defect A; if the
> correct repair of B changes, they are coupled. The generator auto-rejects
> disconnected compositions (two easy tasks in a trench coat) before any model
> token is spent.
> — `implementation_structure/TASK_DESIGN_2026-09-15.md`, Framework proposal, layer 3

The cost of not having it is on the board already: GT006c/GT006d stacked an easy
defect A on GT006's defect B across two context conditions and measured **1.0,
28/28 ×5, four times** (agent.md §8.10 Correction 4) — twenty episodes of quota to
learn that two defects on disjoint merchant keys do not compound.

Files: `tasks/stressors/registry.yaml` (35 stressors that exist as fixture code),
`coupling.py` (`predeclared_overlap`, `measured_coupling`, `selfcheck`, CLI),
`coupling_adapters.py` (`GT004Adapter`, `GT004ClockedAdapter`, `GT009Adapter`),
and the measured JSON under `tasks/stressors/measured/`. Nothing under
`tasks/GT001..GT008/`, `world/` or `gateway/` was edited, and no model was run.

**2026-09-16: the filter has a clock.** Limitation 2 below was the weakest thing
here and is now measured rather than conjectured. See *The clock* and the two
tables under it; the original limitation text is kept because the finding is
what replaced it, not an author's opinion of it.

## Algorithm

**Pre-filter** — `predeclared_overlap(a, b, registry)`. Set intersection over
declared `tables` (direct ∪ derived), `checkpoints`, `metrics`. No world is built;
it may only **reject** (`overlap: true` means "measure it", never "accept"). Two
choices, both from things that went wrong in a first cut:

- **Calendar window is not substrate.** GT004's history is 181 days and every
  stressor lives inside it, so a date-range intersection accepted 100% of pairs.
  A window counts only as a refinement of a shared table.
- **`derived_tables` fold into `tables`.** Direct writes alone would have rejected
  the one pair here that couples: the doubling writes `fct_orders`, the stale
  layout writes `fct_revenue`, and they meet in `kpi_daily`.

A stressor that writes no relation — GT005's `.dbtignore`, GT007's widened offset
— returns `overlap: null`, **undecidable**, not `false`: it acts on whether another
stressor is *observed* or *scheduled*, which no data-state intersection can see.
GT005 is the worked example — the ignored assertion is exactly why nobody saw the
inner-join row loss, and the substrate test calls the two disjoint.

**Measured test** — `measured_coupling(task_dir, a, b, workdir)`:

1. build `base`, `base+{a}`, `base+{b}`, `base+{a,b}` through the adapter;
2. `footprint(A)`, **measured not declared**: every `(table, key)` differing
   between `base` and `base+{a}`, ∪ every one A's own oracle repair moves;
3. apply **B's** oracle repair to `base+{b}` and to `base+{a,b}`;
4. evaluate B's contract clauses on both;
5. **coupled** iff **(i)** every B clause passes on `base+{b}` and ≥1 fails on
   `base+{a,b}` — B's repair stops working; **or (ii)** the post-repair canonical
   state differs at some `(table, key)` **outside** `footprint(A)` — A reached,
   through B's own repair, state it never touched;
6. `repair_order_dependent` — if (i) fired, does `repair_A` then `repair_B` rescue
   the failing clauses? Separates a genuine acquired prerequisite from a clause
   that merely restates A.

Strength: `strong` (ii) / `ordered` (only i, rescued) / `weak` (only i, not
rescued) / `none`. **Coupling is directed**; `(a→b)` and `(b→a)` differ on every
coupled pair below. `--selfcheck` gates everything: per stressor, clean world passes, planted world
fails, repaired world passes. Vacuous clauses would return "uncoupled" for every
pair and look like a working filter. The clean leg is skipped for `kind: event` —
an event's clauses are obligations *created by* the event, so a world where it
never arrived failing them is correct. The self-check found that; I did not.

## Executed vs designed

**Executed** — GT004, all 12 ordered pairs over 4 stressors (3 real + 1 synthetic
control), plus the 4-stressor self-check, all `ok`. ~14 s per ordered pair (5
181-day world builds), 3 min for the matrix, host `gateway/.venv`. Rerun:
`coupling.py --task GT004-doubling-window --pairs all --out tasks/stressors/measured`.

**Designed, not executed** — every non-GT004 pair. `stageable_subset: false` on 24
of 30 entries: GT005/6/7/8 each build one world with everything planted, and a
subset builder for each is one adapter apiece. The *cross-task* negative control
(GT004 doubling × GT006's quality gate) is not stageable at all — two warehouses,
no shared relation — so it was replaced by an in-world synthetic: GT006's mechanism
(a NULL in a `NOT IN` list) on a reporting table fed from frozen `raw.orders_snapshot`.

## Measured — GT004, 2026-09-16

| a (perturbed) | b (repaired) | pre-filter | coupled | strength | evidence |
|---|---|---|---|---|---|
| retry_backfill_doubling | stale_definition_layout | overlap | **yes** | strong | `one_revenue_row_per_business_key` fails, 218,343 rows / 202,700 keys. B's repair is `CREATE OR REPLACE fct_revenue` **from `fct_orders`**, so the doubling propagates into a table it never wrote: 15,643 keys outside footprint(A) |
| retry_backfill_doubling | late_arrival | overlap | **yes** | strong | `restated_lines_present_exactly_once` fails, all 70 restated keys at multiplicity 2 — the merge updates both copies of a doubled key; 70 `fct_revenue` keys outside footprint(A) |
| stale_definition_layout | retry_backfill_doubling | overlap | no | none | dedupe reads only `fct_orders`; 94,292 diff keys, all inside footprint(A) |
| stale_definition_layout | late_arrival | overlap | no | none | as above; the discovered tax rate is unchanged |
| late_arrival | retry_backfill_doubling | overlap | no | none | un-merged late rows sit in `orders_landing`, which the dedupe never reads; 196 diff keys, all footprint(A) |
| late_arrival | stale_definition_layout | overlap | no | none | as above |
| 6 pairs with `synthetic_null_unsafe_exclusion` | | disconnected | no | none | rejected without building a world |

Both coupled pairs are `repair_order_dependent: true` — `repair_A` then `repair_B`
passes every clause, which is the compounding the framework wants: B's correct
repair acquires a prerequisite it does not have alone. **Demonstrated: 2 accepted,
10 rejected.** The load-bearing rejections are the **four real GT004 pairs the
pre-filter accepted and the measurement rejected** — the only evidence that the
measured test adds information over the declaration. GT004 is a one-directional
gate: `retry_backfill_doubling` couples into both others; neither couples back.

## The clock — `coupling.py --clock`

**The gap.** `late_arrival` is not a defect, it is an **event**: rows that arrive
*after* a repair. The unclocked adapter can only stage it as rows already sitting
in `raw.orders_landing` when the comparison starts, which is a world where the
repair and the arrival are simultaneous — and in that world the dedupe never reads
the landing table, so the pair reads uncoupled.

**The mechanism.** `--clock` asks the adapter for a **world root** instead of a bare
duckdb file (`clocked`, `world`, `warehouse`, `advance`). The four subset worlds are
built, **B's repair lands at t0 exactly as an agent would apply it**, and only then
does `world.scheduler.advance` move the world over the planted events, executing the
world's own jobs for real. B's clauses are evaluated after that. The verdict criteria
are unchanged; what changed is the moment they are computed at.

### GT004, re-measured with the clock — 2026-09-16

The clocked world is the same warehouse under a two-job nightly: `late_delivery`
(02:00, only when `late_arrival` is in the subset) inserts the late batch into
`raw.orders_landing` at the moment it is delivered, and `revenue_nightly` (03:00) is
the world's own loader **in append mode**, which is the defect GT004 is built around.
Both are `sql` exec specs, so nothing needs a workspace script and the live plane
carries no host path.

| a (perturbed) | b (repaired) | t0 verdict | **clocked verdict** | strength | B clause that failed |
|---|---|---|---|---|---|
| `late_arrival` | `retry_backfill_doubling` | no | **yes** | ordered | `no_duplicate_business_keys_in_window` |
| `retry_backfill_doubling` | `late_arrival` | **yes** (strong) | **no** | none | — |
| `late_arrival` | `stale_definition_layout` | no | no | none | — |
| `stale_definition_layout` | `late_arrival` | no | no | none | — |
| 2 pairs with `synthetic_null_unsafe_exclusion` | | no | no | none | rejected by the pre-filter |

**Both directions of GT004's one-directional gate flipped.** 17 s per ordered pair,
6 pairs, host `gateway/.venv`. Reproduce:
`coupling.py --task GT004-doubling-window --clock --pairs "late_arrival;retry_backfill_doubling,..." --out tasks/stressors/measured`
(record: `tasks/stressors/measured/GT004-doubling-window-coupling-clock.json`).

- **`late_arrival → retry_backfill_doubling` is the prediction, confirmed.** With the
  clock, the delivery lands and the append-mode nightly re-appends the late batch into
  `warehouse.fct_orders`, so the business keys the dedupe just collapsed come back at
  multiplicity 2. B's repair stops working. `repair_order_dependent: true` — hardening
  the loader first rescues it, which is the compounding the framework is after.
- **The reverse flipped the other way, and the reason is a confound worth stating.**
  Under a clock, `late_arrival`'s correct repair is not the same object: a one-shot
  merge applied at t0 cannot incorporate a delivery that has not happened, and the
  world's append-mode nightly then duplicates it. `GT004ClockedAdapter._repair_late_arrival`
  therefore also hardens the nightly, which is what `solution.sh` step 5 already calls
  "the hardened loader's job". So `retry_backfill_doubling → late_arrival` going from
  `strong` to `none` is **not** the clock alone — it is the clock plus a repair that the
  clock forced to be bigger. The `late_arrival → retry_backfill_doubling` flip has no
  such confound: B's repair (the dedupe) is byte-identical in both runs.
- The self-check found the confound before the matrix did: with the *unclocked* repair,
  `late_arrival`'s `repaired_world_passes` came back **False**, because the repair did
  not survive its own event. That is GT004's own G3 gate, reproduced by the filter.

### GT009, which is unmeasurable without one — 2026-09-16

Every GT009 stressor is an event, and the obligation each creates does not exist until
the night it arrives.

| | self-check | `correction → replay` | `replay → correction` |
|---|---|---|---|
| **no clock** | **red, 3 of 3** | no | no |
| **with clock** | green 2 of 3 (see below) | no | **yes, strong** |

Unclocked, no repair applied at t0 can satisfy a clause about a publication that has
not happened: `higher_version_correction_after_cutoff` reports
`repaired_world_passes: False`, and the ordered pairs return `coupled: false` in under a
second on worlds where the question has not been asked. Clocked, ~35 s per ordered pair,
**1 accepted and 5 rejected over all six**:

| a (perturbed) | b (repaired) | coupled | strength | B clause that failed | s |
|---|---|---|---|---|---:|
| `older_replay_after_the_correction` | `higher_version_correction_after_cutoff` | **yes** | strong | `closed_day_holds_the_contract_fold` | 37.3 |
| `higher_version_correction_after_cutoff` | `older_replay_after_the_correction` | no | none | — | 31.6 |
| `higher_version_correction_after_cutoff` | `retried_delivery…` | no | none | — | 31.9 |
| `older_replay_after_the_correction` | `retried_delivery…` | no | none | — | 32.8 |
| `retried_delivery…` | `higher_version_correction_after_cutoff` | no | none | — | 31.0 |
| `retried_delivery…` | `older_replay_after_the_correction` | no | none | — | 42.0 |

Record: `tasks/stressors/measured/GT009-settlement-cutoff-coupling-clock.json`. The
accepted pair is `older_replay_after_the_correction → higher_version_correction_after_cutoff`:
the correction is published under the protocol on night 2 and night 3's superseded replay
takes the day back, so B's `closed_day_holds_the_contract_fold` fails. The reverse is
uncoupled — a directed gate, the same shape GT004 has. **The four rejections involving
`retried_delivery…` are not evidence about GT009**: that stressor's self-check row is red
(below), so the filter is refusing to score on a clause with no teeth, which is the
self-check doing its job rather than a verdict.

### Two things the clock found about the filter itself

1. **The teeth rule has the same hole the clean-world rule had.** `selfcheck` asserts
   "base + {s} fails at least one clause", which presumes a stressor harmful **alone** —
   and a stressor harmful only beside another is precisely a **coupled** one. Re-sending
   version 1 of six payments breaks nothing in a world where version 2 was never
   delivered. An adapter may now declare `conditional_on = {stressor: [partner, …]}`, and
   the planted and repaired legs are staged with those partners present and unrepaired;
   `staged_with` records it on the row so a green self-check never silently means
   something weaker than it used to.
2. **`retried_delivery_between_commit_and_checkpoint` is measurably toothless as a
   coupling stressor**, and is kept in the adapter so that the self-check says so rather
   than the author. GT009's shipped loader already folds by business key, so a partition
   delivered twice collapses on its own; the duplicate only reaches the consumer under a
   publisher that counts *deliveries*, which is a rubric adversary and not a property of
   the world. It still discriminates inside the task — GT009's R4 separates
   `duplicate_counted` from everything else — it is simply not a coupled stressor.

## Limitations

1. **Shared table is necessary, not sufficient.** Four of six real ordered pairs
   share `fct_orders`, `kpi_daily` and `bi_revenue_extract` and are uncoupled.
   Never accept a composition on the pre-filter.
2. ~~**No clock — the weakest thing here.**~~ **Closed 2026-09-16, and the
   replacement limitation is narrower.** `--clock` stages world roots and advances
   over the planted events; both GT004 verdicts involving `late_arrival` flipped.
   What is still true: a clocked measurement costs ~2.5× an unclocked one (17 s vs
   14 s per GT004 pair, 39 s vs 1 s per GT009 pair), the advance executes **one**
   planted schedule rather than exploring interleavings, and GT009's adapter advances
   the *whole* four-night schedule per staging because its stressors sit on three
   different nights — so "the next planted event" is per-task, not per-stressor.
3. **Criterion (ii) depends on how broad A's repair is.** Had A's oracle repair
   rebuilt B's table, that table would enter footprint(A) and (ii) would fall
   silent; (i) would still fire, at lower strength. `ordered` and `weak` are
   untested paths — both coupled pairs fired (ii), so (i)-alone never occurred.
4. **The repairs are transcriptions.** `solution.sh` repairs both GT004 defects in
   one fixed order; the halves were reduced from it by hand. Writing
   `repair_late_arrival` as a full `fct_revenue` refresh would have silently
   performed the stale-layout repair too — a judgment call, not mechanics.
5. **Six of ten rejections are the synthetic control**, whose defect *and* repair
   are mine. Read those as a property of the filter, not of GT004. One registry
   entry (`gt006d_closed_ticket_excusing_defect`) is `verified: false`.
6. **A clocked repair is not always the same object as an unclocked one**, and when it
   is not, a flipped verdict has two causes. Stated for `retry_backfill_doubling →
   late_arrival` above. The rule to follow: report the repair alongside the verdict, and
   treat a flip whose repair changed as evidence about the REPAIR until a run with the
   repair held fixed says otherwise.
7. **`conditional_on` is declared, not derived.** The filter now stages a partner when
   the adapter names one; nothing searches for which partner a toothless stressor needs.
   A generator would have to try the partners, which is the same combinatorial cost the
   pair matrix already pays.
