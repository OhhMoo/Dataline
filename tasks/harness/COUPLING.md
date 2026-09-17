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

Files, all new — nothing under `tasks/GT*/`, `world/`, `gateway/` or the rest of
`tasks/harness/` was edited, and no model was run: `tasks/stressors/registry.yaml`
(30 stressors that exist as fixture code), `coupling.py` (`predeclared_overlap`,
`measured_coupling`, `selfcheck`, CLI), `coupling_adapters.py` (`GT004Adapter`:
subset builder, per-stressor repairs and clauses), and the measured JSON under
`tasks/stressors/measured/`.

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

## Limitations

1. **Shared table is necessary, not sufficient.** Four of six real ordered pairs
   share `fct_orders`, `kpi_daily` and `bi_revenue_extract` and are uncoupled.
   Never accept a composition on the pre-filter.
2. **No clock — the weakest thing here.** Worlds are compared at t0; `late_arrival`
   is staged as rows already in the landing stream, a static stand-in for an event
   defined by arriving *after* a repair. GT004's G3 gate advances the clock and
   re-runs; that ordering is layer 4 and this filter cannot see it.
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
