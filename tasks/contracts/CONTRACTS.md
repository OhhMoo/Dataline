# tasks/contracts — layer 1 of the composition framework

**Written 2026-09-16.** A CONTRACT is an invariant a data platform must keep
while the world moves. Six of them, fixed, lifted out of the checks
GT004–GT008 each implement one-off. A composed task **declares** which contracts
hold instead of re-writing `grade.py`:

```toml
[composition.contracts.exactly_once]
table = "warehouse.fct_orders"
business_key = ["order_id", "line_nbr"]
date_column = "order_dt"
window = { start = "2026-02-09", end = "2026-02-22" }
pipeline = { steps = ["pipelines/load_orders_delta.sql"] }
```

```python
from tasks.contracts import REGISTRY, evaluate_all
from tasks.contracts._types import Context
ctx = Context(world_root=root, before=facts, receipts=ledger,
              params=toml["composition"]["contracts"])
results = evaluate_all(ctx)          # {name: ContractResult}
```

Interface, fixed: each module exposes `NAME`, `DOC` (≤10 lines) and
`evaluate(ctx) -> list[Clause]`. `Clause` is
`(id, passed, points, max, detail, fatal=False, checkpoint=None)` and
`_score.score_clauses` folds clauses by **exactly** the rule
`tasks/*/tests/probes/_common.py` folds them by — copied, not imported, and
`tests/test_score_equivalence.py` loads all six shipped `_common.py` files by
path and proves the two folds agree on points, max, passed and the reason
string over all 64 (ok × fatal) combinations.

**The rule that copy exists to carry: a fatal clause GATES and never PAYS.**
Fatal clauses are excluded from the weight base, so they can take a contract to
zero but never add to it. Measured on GT004: paying for clauses a no-op
satisfies by construction handed the `no_op` baseline 6.25 of 33 points.

**Dependency rule.** Nothing under `tasks/contracts/` imports from a task
directory, from `world/` or from `gateway/`; `duckdb` is the only third-party
import. `test_registry.py` asserts this with an AST walk over every module.

## The six

| contract | invariant | stressed by | fatal gate | status |
|---|---|---|---|---|
| `exactly_once` | one business key, one row; a late or repeated delivery is incorporated once and a re-run adds nothing | retried backfill that appends; redelivery into a repaired window | `stressed`, `late_incorporated_once` | **executed** (GT004 G1b + G3) |
| `liveness` | an unseen delivery lands on the next real run, exactly once, disturbing nothing else; a second run changes no business column | any loader repair — "stop loading" satisfies every check-shaped gate | `ingests`, `no_collateral` | **executed** (GT004 G2) |
| `version_precedence` | each key held at its highest delivered version; delete at the highest version removes it; a replay of a superseded version changes nothing | arrival order ≠ version order; byte-identical replay; delete then stale redelivery | `highest_version` | **executed on synthetic fixtures**; fold rule lifted verbatim from GT008 |
| `cutoff_integrity` | nothing published for a period changes after its cutoff except through the declared correction path — evaluated per receipt | backfill across a signed report; late correction after a cutoff; recovery that republishes history | `ledger_monotonic` | **designed** — no task writes receipts yet |
| `consumer_consistency` | every receipt's `canonical_hash` equals the hash of the object the oracle would have published *at that vnow* | a repair that runs while a consumer keeps publishing | `hashes_match` | **designed** — no task writes receipts yet |
| `auditability` | a declared assertion executed, its verdict is recorded as a definite state, and firing it at the input it exists to catch still turns the build red | `.dbtignore`d schema file; `severity: warn`; `enabled: false` | `discriminates` | **executed on synthetic fixtures**; phase logic lifted from GT005 |

Each `DOC` names the shortcut the contract exists to kill. That list is what a
generator's shortcut-baseline battery should be built from: permanent skip,
snapshot restore, naive merge on arrival order, full reload, assertion that
fails unconditionally, recompute-everything.

## Parameters that matter

- **`exactly_once`**: `table`, `business_key`, `date_column`, `window`,
  `audit_columns`, `pipeline.steps`, and optionally `late = {predicate,
  advance_to, payload{mode, sql|dir/filename/header/row_template, events[]}}`.
  An event may carry `adds` (a *restatement* replaces a row rather than adding
  one) — counting restatements as arrivals zeroed GT004's gate for every
  baseline including the oracle.
  It also reads `before.duplicate_keys_before`: **no free points**. If the
  shipped world never carried a duplicate key, the `stressed` gate fails and
  the contract pays nothing; if the fact is undeclared, the contract reports
  itself not evaluable rather than guessing.
- **`liveness`**: `plant{mode, date, rows, id_base, predicate, sql}`, `window`,
  `pipeline.steps`. `runner` is `pipeline` only — triggering through the
  scheduler reports success **without executing** for every command that is not
  `dbt_build`/`dbt_run`, so a run count there proves nothing ran.
- **`version_precedence`**: `target`, `key_column`, `version_column`, plus
  either `events` (inline) or `feed_dir` (`dt=*/ *.csv`, arrival order),
  `type_field`/`delete_value`, `identity_fields`, `compare{target_col: field}`.
- **`cutoff_integrity`**: `cutoffs{day: iso}` or `cutoff_rule.offset_hours`,
  `publish_kinds`, `correction_kinds`, `corrections_allowed`, `consumer`.
- **`consumer_consistency`**: `expected{seq|consumer\|target\|day: hash}` or
  `oracle = "<file.py>::<fn>"`, called as `fn(receipt, ctx)`.
- **`auditability`**: `declared[]`, `results` (a JSON path) or receipts of kind
  `assertion`, `definite_states[]`, and `phases[{name, expect: green|red,
  plant_sql[], steps[]}]`.

Receipts are `{seq, vnow, day, job, target, kind, canonical_hash, row_count,
sample, consumer}`. With an **empty ledger**, `cutoff_integrity` and
`consumer_consistency` each return exactly one clause, `passed=False`,
`fatal=False`, detail `"no receipts: contract not evaluable"` — a missing ledger
is visible, not silent, and does not zero the rest of a composition.

## Executed vs designed

**Executed.** `gateway/.venv/Scripts/python.exe -m pytest tasks/contracts -q`
→ **75 passed** (≈12 s). Every contract has holding / canonical-violation /
named-shortcut cases against a real duckdb world in `tmp_path`; the scoring
fold is proved equal to all six shipped `_common.py` copies.

**Executed.** `tasks/contracts/equivalence_gt004.py` against GT004's **real**
fixture, staged through `make_fixture_world.stage_world_root` exactly as
`tests/run_baselines.py` stages it, with GT004's own baseline agents, graded
both by GT004's `tests/grade.py` and by the contracts. Nothing under
`tasks/GT004-doubling-window/` was modified.

| GT004 baseline | G1b / `no_duplicate_keys` | G3 / `late_*` + `no_re_doubling` + `stable_on_replay` | G2 / `liveness.*` |
|---|---|---|---|
| `oracle` | True = True | True = True | True = True |
| `no_op` | False = False | False = False | False = False |
| `permanent_skip` | False = False | False = False | False = False (`ingests` fatal) |
| `repair_before_late_arrival` | True = True | **False = False** | True = True |
| `defect_one_only` | True = True | True = True | True = True |
| `full_refresh` | True = True | True = True | True = True |

**18 of 18 cells agree; 0 disagreements.** Three of the six states split the
verdicts (`repair_before_late_arrival` passes G1b/G2 and fails G3;
`permanent_skip` fails G2 on the liveness clause specifically), so the
agreement is not an artifact of two saturated end points.

**Designed, not executed against any real world.** `cutoff_integrity` and
`consumer_consistency` — no task in this repo writes consumer receipts, so both
are exercised only against receipts written by their own tests. Their clause
logic is a proposal until a task emits a real ledger.
`version_precedence`'s fold is GT008's rule verbatim, but it has **not** been
run against GT008's fixture: GT008 grades by replaying four planted nights
through `world.scheduler.advance`, and this package does not import `world/`.
`auditability` fires real phases, but against a SQL assertion this package's
own tests wrote, not against dbt.

## Known weaknesses

1. **Two of six contracts are unvalidated by anything but themselves.** A
   receipts-writing task is the next thing to build; until then treat
   `cutoff_integrity` and `consumer_consistency` as a specification.
2. **`liveness`'s default max is 0.35**, because two of its three clauses are
   gates and gates are excluded from the weight base. A composed rubric must
   pass an explicit `max_points`, or liveness will be worth a third of what the
   author intended. This is the fold behaving correctly and the default being a
   trap.
3. **The equivalence run proves agreement, not sufficiency.** It shows the
   lifted clauses reach GT004's verdicts on six states GT004 itself authored.
   It does not show the contracts would catch a seventh behaviour neither of
   them anticipated.
4. **`runner: "advance"` is refused, not implemented.** A contract that needs
   the scheduler to execute a real night (GT008's shape) cannot yet be
   evaluated by this package.
