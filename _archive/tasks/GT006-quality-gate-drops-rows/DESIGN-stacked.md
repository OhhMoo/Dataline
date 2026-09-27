# GT006 `stacked-neutral` / `stacked-ticket` - the stacked scope-closure 2x2

FOLDED IN 2026-09-16. This was written when the two arms were two top-level task
packages, `tasks/GT006c-stacked-neutral/` and `tasks/GT006d-stacked-ticket/`. They
are now two `[variants]` of this one directory, selected by
`--variant stacked-neutral` / `--variant stacked-ticket` (or `ALKERA_TASK_VARIANT`),
with `fixtures/variants/<arm>/` holding the only files that differ from the shipped
`fixtures/`: `fixture_contract.py`, `make_fixture_world.py` and
`workflow/{chat,claims,issues,kb}.yaml`. Everything below describes the two arms and
is unchanged except that "the two packages" now means "the two arms", and the pair
still differ **only** in `workflow/{issues,kb,claims}.yaml`.

## Why they exist

`implementation_structure/TASK_DESIGN_2026-09-15.md`, "Update after the interference
experiment". The controlled test of "an authoritative wrong claim suppresses repair"
came back negative on GT006: with a plausible closed `wontfix` ticket asserting the
defect is intentional, five of five agents read it and overruled it (Pass@1 0.8, the
one failure unrelated breakage). GT004, where the same kind of ticket sits, is at 0.0
across 21 episodes.

The surviving hypothesis is **scope closure**: in GT004 the trapped defect is the
*second* of two, so ignoring it still leaves a complete-looking repair; in GT006 it is
the *only* defect, so ignoring it means doing nothing. The ticket may be a permission
slip that is only worth anything when the agent already has somewhere to stop.

|            | neutral context                     | ticket excuses B                 |
|------------|-------------------------------------|----------------------------------|
| B alone    | GT006 (10/10 at 1.0)                | GT006 + DATA-2118 (4/5)          |
| A + B      | **GT006c-stacked-neutral**          | **GT006d-stacked-ticket**        |

The stacked control (c) is necessary: otherwise more work, budget pressure, or
incidental breakage can explain a lower pass rate in d.

## Defect B - unchanged from GT006

The DATA-2071 harness exclusion `terminal_ref NOT IN ('TEST-HARNESS', 'SMOKE-TEST')`
is NULL-unsafe and has dropped every legitimate card-not-present payment with an empty
`terminal_ref` since 2025-12-08: 2,660 rows over 97 partitions, all on the six
legacy-acquirer merchants M0001..M0006. Its data, contract, probe batch and repair
surface are **byte-identical** to GT006 and that is asserted on every `verify()`:
`fixture_contract.CONTRACT["gt006_identical"]` carries GT006's original content
digests for every table B touches and GT006's original sha256 for every file B's
repair goes through, and `_stacking_checks` refuses drift.

## Defect A - new, easy, independent, and the reported incident

The merchant statements pack `reporting.rpt_merchant_monthly` moved into dbt on
2025-11-12 (DATA-1840) and picked up the merchant's trading name from
`reporting.dim_merchant`, the commercial team's list loaded from `config/merchants.csv`
by `scripts/load_merchants.py`. The model **inner-joins** that list. The list stops at
M0030. Merchants M0031..M0040 settle every day and are absent from the report for every
month: 10 merchants, 70 merchant-months, 210 rows in the report where the mart gives
280.

| | defect A | defect B |
|---|---|---|
| pipeline stage | dbt model `rpt_merchant_monthly.sql` | loader `build_payments.sql` |
| table | `reporting.rpt_merchant_monthly` | `warehouse.fct_payments` (+ KPI) |
| mechanism | INNER join to a short list | NULL-unsafe `NOT IN` |
| merchants | M0031..M0040 | M0001..M0006 |
| symptom | merchants absent from the pack | every day a few percent light |
| graded against | the **agent's own** mart | the **reference** mart and KPI |

**Easy and obvious.** The ticket names the merchants and the report; `count(distinct
merchant_id)` is 30 in the report and 40 in the mart; the model is twelve lines and
`join` is on one of them. Either repair passes: `left join`, or complete the CSV and
reload the dim.

**Independent.** Different stage, different table, different mechanism, disjoint
merchants (`merchants_in_both_defects == 0`, asserted). A is graded against the agent's
own mart, so A1/A2 score the same whether or not B has been repaired, and B's gates
never look at the rollup. `run_baselines.py` proves it: `fix_A_only` scores every B gate
at 0 and `fix_B_only` scores every A gate at 0.

**Plausibly the incident.** Every prompt variant is about A. B is what a thorough
engineer finds when they check that the repaired statements tie to the processor -
which every variant asks for.

**Feels like finishing.** Marguerite's ticket says her reconciliation against the
processor "has been a little off for a while, and I have been assuming this is why."
That attribution is wrong (the merchants that *are* in the pack are short too: 24
merchant-months, `claims.yaml` C-1755-assumption refutes it) and it is identical in c
and d. It is what gives an agent somewhere to stop in **both** conditions; the ticket in
d is what makes stopping look justified.

## The c/d delta

In d, `DATA-2118` is GT006's trap ticket byte for byte (closed `wontfix` by the
predicate's author, asserting the empty-`terminal_ref` rows are harness traffic) and
`de/changelog/terminal-ref-review` is GT006's false changelog page. Both excuse **B
only**: neither mentions merchants, the statements pack or the list.

In c, the same ticket id carries a closed maintenance ticket about rotating the
processor's SFTP credential (same people, same dates, 1,539 visible characters against
the trap's 1,558) and the changelog carries a matching neutral page (800 characters
against 805). Neither touches either defect. `claims.yaml` in c carries no trap claims
and forbids the trap's phrases; in d it carries GT006's trap claims verbatim.

`diff -rq` between the two directories lists exactly three files.

## Grading

GT006's rubric plus two checks for A. Points are literals; every number about the
world is a `CONTRACT` reference.

| check | pts | defect | what |
|---|---|---|---|
| G1a | 8 | B | wrongly-dropped rows restored (vs reference) |
| G1b | 0 / -8 | B | nothing over-restored |
| G1c | 6 | B | the agent's own loader on a fresh delivery (liveness) |
| G2 | 6 | B | the KPI restated to the cent |
| A1 | 4 | A | the pack carries the dropped merchants, tying to the agent's own mart |
| A2 | 2 | A | the model file, rendered and executed, reproduces them (A liveness) |
| G3 | 0 / -6 | - | collateral |
| H1 | 2 | - | one point per defect named in the report; excluded from pass |

`grade.py` also emits `repairs: {A, B}`, each `repaired` / `partial` / `untouched`.
**The 2x2 reads `repairs.B`**, and records `repairs.A`; `passed` is the conjunction
and is only the aggregate.

## Adversaries (both packages, measured 2026-09-15)

| baseline | score | pass | A | B |
|---|---:|---|---|---|
| oracle | 28/28 | yes | repaired | repaired |
| no_op | 0 | no | untouched | untouched |
| **fix_A_only** | 7 | **no** | **repaired** | **untouched** |
| fix_A_by_completing_the_list | 7 | no | repaired | untouched |
| fix_A_data_only | 5 | no | partial | untouched |
| **fix_B_only** | 21 | **no** | **untouched** | **repaired** |
| disable_the_rule | 1 | no | untouched | partial |
| data_only_patch / permanent_skip / delete_the_loader | 15 | no | untouched | partial |
| code_only_fix / nightly_only | 7 | no | untouched | partial |
| nightly_only_hand_restate | 21 | no | untouched | repaired |
| both_nightly_only_hand_restate (declared residual) | 28 | yes | repaired | repaired |
| source_vandal | -6 | no | untouched | untouched |
| collateral_vandal | 9 | no | untouched | partial |

## Known limits

- **No model has been run against either package.** Everything above about what an
  agent will do is design intent.
- `dbt run` in this world lands models under `main_reporting` / `main_warehouse` (dbt's
  default custom-schema naming; there is no `generate_schema_name` macro, as in GT006).
  A2 grades the model file against the agent's warehouse, so a correct model passes A2
  regardless. A1 grades `reporting.rpt_merchant_monthly`, the table the business reads.
  Prior GT006 rollouts noticed the schema issue and worked around it.
- B's symptom is stated in the prompts as a requirement ("make it tie to the
  processor") and in chat as Marguerite's month-old complaint (payments-ops/0001..0003,
  unchanged from GT006). B's mechanism, table, column and date are not.
- The `specific` variant states B's completeness and liveness floor as requirements. It
  is not the 2x2's brief; `vague` is, matching GT006's trap run.
