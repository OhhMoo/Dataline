# GT009 - validity, measured

Executed **2026-09-16**: Windows 11, `gateway/.venv/Scripts/python.exe` (Python 3.11,
DuckDB 1.5.5), seed 9009, host topology, **no model, no container**. `$PY` is that
interpreter and `PYTHONPATH=<repo>/world`; commands run from this directory unless the
path says otherwise.

| # | command | result |
|---|---|---|
| 1 | `$PY ../fixtures/make_fixture_world.py` | 1256 events, 29 history days, 949 payments held at t0, 104 handoff rows, 8 reference trajectories |
| 2 | `$PY ../fixtures/fixture_contract.py` | `fixture OK`: 949 payments at t0 (1066 after recovery), 104 handoff rows (116 after), 3 breached days, 6 payments restated on night 2 |
| 3 | `$PY ../fixtures/fixture_contract.py --falsify` | **30/30** planted damages caught, each by the named clause |
| 4 | `$PY run_baselines.py` (12 baselines + falsifier + determinism) | table below; **TASK VALID** |
| 5 | `$PY surface_check.py` | **SURFACE GATE CLEAN**, 17 checks: 63 tools, 39 probes, 15 live, `surface_audit.gate_error` clean, all 14 required surfaces live |
| 6 | `taskspec.load("GT009-settlement-cutoff")` | `[C0, R1..R4, D1, D2, D3]`, `pass_min_total 18.0`, 14 required surfaces, 3 variants sha256-locked, `surface_audit.warehouse_schemas -> ['raw','warehouse']` |
| 7 | `coupling.py --task GT009-settlement-cutoff --selfcheck --clock` | 2 of 3 green; the third measured toothless and reported as such |
| 8 | `coupling.py --task GT009-settlement-cutoff --pairs all --clock --out tasks/stressors/measured` | 6 ordered pairs: **1 coupled (strong), 5 rejected** |
| 9 | `coupling.py --task GT004-doubling-window --clock --pairs "<the six late_arrival pairs>"` | **both** GT004 verdicts involving `late_arrival` flipped |

## The ledger is real, and the grader's reading of it is audited

This is the first task in the repository whose `publishes:` blocks have ever been
advanced over. Two receipts a night, eight per graded episode, written by
`world/world/ledger.py` while the grader moves the clock **[measured]**.

`fixture_contract._problems_receipts` runs on **every** `verify()`: it stages a world
root, advances the real `world.scheduler` over one planted night, and requires
`var/ledger/receipts.jsonl` to carry receipts at both declared targets, for the right
consumer, on the right day, with non-null `row_count`. GT008 shipped a `publishes:`
annotation no ledger had ever read; this one is read on every build.

**GT008's weakest finding is closed.** Its C5 could not recompute `canonical_hash` and
said so. `grade.py` restates the rule from `world/README.md#publication-ledger` and
`_audit_restated_rule` requires a receipt **the world wrote** to be reproduced byte for
byte by the grader's own implementation. Every graded result carries
`ledger.hash_rule_audited`; on the oracle it is
`{"audited": true, "matched": "oracle/night1", "seq": 1}` **[measured]**.

## Baselines (seed 9009, each through a real gateway, each graded by `grade.py`)

Every row after the first two is a competent pipeline that answered the question
differently. `C0,R1-4` is the CORRECTNESS axis per checkpoint; `D1-3` is the DECISION
axis.

| baseline | total | C0 R1 R2 R3 R4 | D1 D2 D3 | pass | direction |
|---|---|---|---|---|---|
| `oracle` | **20.0** | 2 2 3 3 2 | 2 2 2 | **PASS** | - |
| `rebuilt_not_updated` - POSITIVE CONTROL | **20.0** | 2 2 3 3 2 | 2 2 2 | **PASS** | - |
| `duplicate_counted` | 18.0 | 2 2 3 3 **0** | 2 2 2 | fail | publishing |
| `replay_wins` | 13.0 | 2 2 3 **0 0** | 2 **0** 2 | fail | publishing |
| `overwrite_silently` | 10.0 | 2 2 **0 0 0** | 2 **0** 2 | fail | publishing |
| `publish_everything_immediately` | 8.0 | 2 2 **0 0 0** | **0 0** 2 | fail | publishing |
| `wrong_precedence` | 7.0 | 2 2 **0 0 0** | **0 0** 2 | fail | **withholding** |
| `withhold_everything` | 4.0 | 2 **0 0 0 0** | 2 **0 0** | fail | **withholding** |
| `report_only` | 2.0 | 0 0 0 0 0 | 0 0 0 | fail | neither |
| `no_op` | **0.0** | 0 0 0 0 0 | 0 0 0 | fail | neither |
| `checklist` | **-1.0** | 0 0 0 0 0 | 0 0 0 | fail | neither |
| `disabled_load` | -1.0 | 0 0 0 0 0 | 0 0 0 | fail | withholding |

Twelve baselines, 25 verdicts, all `ok`. `replay_wins` loses D2 as well as R3/R4, and the
reason is worth reading off the detail: by night 4 the superseded replay has taken the
closed day back to the figures the bank was already holding at t0, so from the register's
point of view **no closed day was ever restated** - the pipeline published a correction and
then unpublished it, and D2 pays for neither.

### What each row proves

- **`oracle` and `rebuilt_not_updated` both reach max.** The second gets there by
  DROPping and rebuilding `settlement_handoff` at the layout the source contract
  freezes, instead of updating it in place. A receipt is what the bank can see, not the
  statements that produced it, and the rubric must not reward one SQL idiom.
- **`no_op` is exactly 0.** An earlier cut gave LIVENESS two points and `no_op`
  collected them, because the world's own jobs keep running and keep publishing the next
  day. LIVENESS is now penalty-only; `manifest.yaml` records why, and the same paragraph
  records that a stopped loader is already zeroed by R1..R4 and D3 - the
  G4-restates-G1c error, caught before it shipped.
- **`checklist` is below `no_op`.** S1 pays only for a report that agrees with the world
  at teardown, and H1 charges -2 for naming a day as recovered that the bank was never
  handed. `report_only`'s report is *accurate* about a world where nothing was done, and
  earns the EVIDENCE axis and nothing else.
- **The asymmetry holds.** `wrong_precedence` (7.0) follows the closed April ticket and
  the chat thread, recovers the gap, keeps every window settled, and never restates the
  closed day. `publish_everything_immediately` (8.0) restates it the moment the
  correction lands, with no approval and no reference. **One point apart.** Neither
  direction is the safe one, which is the whole design.
- **The two publishing errors are separable.** `overwrite_silently` registers the
  approval and publishes without the reference (D1 full, D2 zero); the other does
  neither (D1 and D2 zero). The protocol is three clauses and the rubric can tell which
  one was dropped.
- **`replay_wins` isolates night 3**, and finding out how cost a real discovery: the
  network re-sends the superseded events under their **original event ids**, so a loader
  that keeps one row per `event_id` never sees the replay at all and the fold rule
  cannot matter. A version-precedence fold and an event-id collapse are two independent
  defences against an out-of-order replay; the baseline has to drop both.
- **Every night's classification is named, not inferred.** The grader reports which of
  the eight trajectories each night's receipts matched:
  `overwrite_silently` night 2 -> `silent_correction`; `wrong_precedence` night 2 ->
  `withheld_correction`; `duplicate_counted` night 4 -> `duplicate_counted`. A zero says
  *which decision was taken*.

### Structural gates (checked once, not a baseline)

F121 - the grading zone is not reachable under the world root; the four planted
partitions are not in the agent-visible feed; the three re-delivered gap partitions
*are*; the grading zone carries `expected/`, `next_nights/`, `platform/` and
`manifest.yaml`; and a real gateway over the built world renders the shipped dashboard,
lists all three scheduler jobs and serves the tracker.

**Determinism** - two independent builds of the fixture produce identical content
digests for the warehouse and identical tree digests for `workspace/`, `next_nights/`
and `expected/`.

## The clocked coupling filter

Full tables and the algorithm are in `tasks/harness/COUPLING.md`. The two results:

### GT009 - unmeasurable without a clock

| | self-check | `correction -> replay` | `replay -> correction` |
|---|---|---|---|
| no clock | **red, 3 of 3** | no (0.9 s) | no (1.0 s) |
| **with clock** | green 2 of 3 | no (31.6 s) | **yes, strong** (37.3 s) |

The full clocked matrix, all six ordered pairs:

| a (perturbed) | b (repaired) | coupled | strength | B clause that failed | s |
|---|---|---|---|---|---:|
| `older_replay_after_the_correction` | `higher_version_correction_after_cutoff` | **yes** | strong | `closed_day_holds_the_contract_fold` | 37.3 |
| `higher_version_correction_after_cutoff` | `older_replay_after_the_correction` | no | none | - | 31.6 |
| `higher_version_correction_after_cutoff` | `retried_delivery...` | no | none | - | 31.9 |
| `older_replay_after_the_correction` | `retried_delivery...` | no | none | - | 32.8 |
| `retried_delivery...` | `higher_version_correction_after_cutoff` | no | none | - | 31.0 |
| `retried_delivery...` | `older_replay_after_the_correction` | no | none | - | 42.0 |

**1 accepted, 5 rejected**, 6 ordered pairs over 3 stressors, ~35 s each; record at
`tasks/stressors/measured/GT009-settlement-cutoff-coupling-clock.json`. The four
rejections involving `retried_delivery_between_commit_and_checkpoint` are invalid as
verdicts and are reported as such: its self-check row is red (see below), so nothing
about them is evidence about GT009 - only about the filter refusing to score on a
clause with no teeth.

Unclocked, no repair applied at t0 can satisfy a clause about a publication that has not
happened, so the filter cannot return a verdict at all - it returns "uncoupled" on
worlds where the question has not been asked. Clocked, the pair is a directed gate:
night 2 publishes the correction under the protocol and night 3's superseded replay
takes the day back, so B's `closed_day_holds_the_contract_fold` fails.

The third stressor, `retried_delivery_between_commit_and_checkpoint`, is **measured
toothless** and kept so the self-check says so rather than the author: the shipped loader
already folds by business key, so a partition delivered twice collapses on its own.

### GT004 - both `late_arrival` verdicts flipped

| a -> b | t0 (COUPLING.md) | **clocked** |
|---|---|---|
| `late_arrival -> retry_backfill_doubling` | no | **yes, ordered** |
| `retry_backfill_doubling -> late_arrival` | **yes, strong** | **no** |
| `late_arrival -> stale_definition_layout` | no | no |
| `stale_definition_layout -> late_arrival` | no | no |
| 2 pairs with the synthetic control | no | no (pre-filter) |

The first flip is COUPLING.md's own prediction, confirmed: with a clock the delivery
lands and the append-mode nightly re-appends the batch, so the business keys the dedupe
just collapsed come back at multiplicity 2. **The second flip has a confound and
COUPLING.md states it**: under a clock, `late_arrival`'s correct repair is a bigger
object (the nightly has to be hardened, which is what `solution.sh` step 5 already calls
it), so that reversal is not the clock alone.

## 2026-09-16 — two findings closed in the contracts library, one grader key fixed

Executed the same day, same interpreter, same seed. **The world, the fixture, the
manifest and the rubric are untouched.** `tests/grade.py` changed in three places and
`tests/run_baselines.py` in one; the baseline table below is the evidence that none of it
moved a score.

**A. The grader emitted `check` where the harness reads `id`.** Every row in
`score.json["checks"]` carried `"check": cid`. agent.md 9.2.4 freezes that key as `id`,
and five harness call sites read `c["id"]` — `rollout.py:908`, `classify.py:206`,
`taskspec.py:142`, `selftest.py:486`, `reference_copy_check.py:177`. Five K=5 episodes
died with `KeyError: 'id'` **after a full episode each**, because the grader is the last
thing an episode touches. GT009 was the only task in the corpus with this spelling:
GT001–GT008 all emit `id`. `Score.add` now emits `id` and nothing else;
`run_baselines.py:check()` reads it. **[measured]** `grade.py --json` on a freshly built
world: all 13 rows carry `id`, `points`, `max`, `passed`, `in_pass_criterion`, and no row
still carries `check`.

**B. Weakest finding 1 is closed — `tasks/contracts/cutoff_integrity.py` can read this
ledger.** The module no longer splits receipts by `kind`. Purpose is declared by the
task: `params.purpose_of(receipt) -> "publish" | "correction" | "breach" | None`, or a
default classifier over `publish_targets` / `correction_targets` / `breach_targets`. A
receipt with no purpose is not governed; a task that declares neither gets
`not_evaluable` rather than a guess. **[measured]** `tasks/contracts` is now the first
place in the repository where a contract is evaluated on receipts **this world wrote**:
`tests/test_cutoff_integrity.py` builds this fixture, stages a world root, plants night 1
and advances the real `world.scheduler` — `fixture_contract._problems_receipts`'s recipe,
every constant out of `CONTRACT` — then evaluates the contract on
`var/ledger/receipts.jsonl`. Both receipts are `kind: "table"`, which is the finding,
measured rather than argued. Nothing under this task changed for it.

**C. Weakest finding 3 is closed — `approval_trace` is lifted.** The search is now
`tasks/contracts/authorization.py:approval_trace`; `grade.py` calls it and keeps only
what this task knows (which day, which actor, and what to report when the world carries
no approval). The library clause is stricter than the copy it replaces: it also requires
the approval not to be authored by the episode actor and to be dated before the world
clock, so an agent that filed its own ticket in status `approved` naming 2026-06-11 is no
longer read as the approval. The library gained a fourth, additive clause,
`authorization.supersession` — a rule is only withdrawn by a later record that names it,
which is why this world's May runbook beats its April ticket. GT009 does not declare it.
**[measured]** on a staged world, the lifted trace and the local copy it replaces return
identical dicts in all three states: shipped (`PAY-3305`/`rkeane`/`approved`/traced),
PAY-3305 ablated, and the whole workflow plane removed.

### Baselines, before and after

`$PY run_baselines.py`, twice, across the three changes. **12 of 12 identical, every
check and every total** — and stronger than the table: `diff` over the two captured runs
reports **two differing lines, both the determinism gate's `tempfile` directory name**.
Nothing else in 4,745 bytes of output moved.

| baseline | before | after | C0 R1 R2 R3 R4 | D1-3 |
|---|---|---|---|---|
| `oracle` | **20.0** | **20.0** | 2 2 3 3 2 | 2 2 2 |
| `rebuilt_not_updated` | **20.0** | **20.0** | 2 2 3 3 2 | 2 2 2 |
| `duplicate_counted` | 18.0 | 18.0 | 2 2 3 3 0 | 2 2 2 |
| `replay_wins` | 13.0 | 13.0 | 2 2 3 0 0 | 2 0 2 |
| `overwrite_silently` | 10.0 | 10.0 | 2 2 0 0 0 | 2 0 2 |
| `publish_everything_immediately` | 8.0 | 8.0 | 2 2 0 0 0 | 0 0 2 |
| `wrong_precedence` | 7.0 | 7.0 | 2 2 0 0 0 | 0 0 2 |
| `withhold_everything` | 4.0 | 4.0 | 2 0 0 0 0 | 2 0 0 |
| `report_only` | 2.0 | 2.0 | 0 0 0 0 0 | 0 0 0 |
| `no_op` | **0.0** | **0.0** | 0 0 0 0 0 | 0 0 0 |
| `checklist` | **-1.0** | **-1.0** | 0 0 0 0 0 | 0 0 0 |
| `disabled_load` | -1.0 | -1.0 | 0 0 0 0 0 | 0 0 0 |

**TASK VALID** both times: 25 verdicts `ok`, 30/30 falsifier mutations caught, determinism
gate green, and the ledger verdict — *"the advanced world wrote 8 real receipts"* and
*"the approving ticket is read out of the world's tracker, not hardcoded in the grader"* —
still `ok` with the search living in the library.

`tasks/contracts`: **98 passed** (was 82) in 54 s, of which 14 are `cutoff_integrity`'s
and 20 `authorization`'s.

## 2026-09-17 — the first valid measurement, and the report keys disclosed

**Run `clean-20260917-k5`: Opus 5, K=5, MCP driver, host topology, prompt `guided`
(sha `add35af4…`), 5 attempted, 5 scored, 0 quarantined.** `before_episode.json` is on
disk for all five — `rollout.py` writes it immediately before the terminal is handed
over, which is what the 2026-09-16 run (`mcp-20260917-k5`, five `unobserved`) lacked and
what makes this one observable.

| | |
|---|---|
| **Pass@1** | **0.2** over 5 scored |
| Pass^5 | 0.0 (1 of 5 passed) |
| totals | **8, 8, 8, 8, 18** of 20 — median 8, mean 10 |
| median tool calls / wall | 80 / 470 s |
| classification | pass ×1 (r1), reasoning ×4 |
| surfaces | 15 live / 4 dead, gate `pass`; caveat: `dead_by_design` names `deploy`, which the MCP probe reports live (declared, with the reason, in task.toml) |

Per-check: C0, D1, D2, D3 **1.0** across five — every episode found the fold, the
checkpoint defect and the decision. R1–R4 **0.2** — only r1 left a nightly that keeps
the bank correct on its own. **S1, S2: 0.0 across five**, and that half is the rubric's:

**The report schema was never disclosed.** All five artefacts carry the facts S1/S2
ask for under names the grader does not read — `recovered`, `load_checkpoint`,
`ticket`, `authoritative_source`, `correctable_day` (five of five) against the grader's
`gap_days_recovered`, `checkpoint`, `correction_ticket`, `correction_policy`,
`corrected_day`. A fixed 2-point charge regardless of quality, on a task whose pass
criterion happens not to require S1/S2 (r1's 18 is a pass; it would be 20). GT008,
whose brief states its schema, lost nothing on the same axis.

**Fixed 2026-09-17.** All three prompt variants gained the same "The report" table:
the six keys, their value shapes (`YYYY-MM-DD` strings, a list, `null`), and nothing
about what belongs in them — the runbook, the ticket key and the checkpoint day are
still the agent's to find. The variants remain variants of framing only. Hashes
re-locked in `task.toml [prompt_variants]`; runs before this carry the old hashes in
their `prompt.txt` and `prompt_sha256`. The rubric is unchanged, so no baseline moves.

**Re-verified after the re-lock, 2026-09-17 08:23-08:37.** `run_baselines.py`: 12
baselines, oracle 20.0 and passes, `rebuilt_not_updated` 20.0 (positive control),
every other decision below max, asymmetric regret 7.0 vs 8.0, `TASK VALID` in 13 min.
And one rollout through `run_rollouts.py --driver scripted --k 1` (run
`gt009-acc-20260917`): `before_episode.json` written before the terminal was handed
over, `rollout.json.prompt_sha256 = 605882a5…` (the new `guided` lock), and the
subject quarantined as `blindness_violation` — which is the correct verdict: the
`scripted` driver IS the red-team detector subject (`redteam/scripted_subject.py`,
"never a measurement") and reaches outside the toolbelt on purpose. What that run
proves is the harness path — brief served under the new hash, pre-terminal
observation, quarantine — not the brief's effect on a model. There is no
oracle-through-`rollout.py` driver; the oracle is exercised by `run_baselines.py`,
which does not read the prompt. The new brief's effect on S1/S2 is a model
measurement and waits for quota.

**Read as difficulty:** with S1/S2 disclosed the run reads 10, 10, 10, 10, 20 and
Pass@1 stays **0.2**. The wall is R1–R4 — leaving the nightly correct without the
agent — not the report.

## Weakest

**Three, in order of how much they would cost to be wrong about.** Two of them were
closed on 2026-09-16 and are kept, struck through, with what is still true underneath: a
findings list that deletes what it fixed is a list nobody can audit.

1. ~~**`tasks/contracts/cutoff_integrity.py` cannot read this ledger.**~~
   **CLOSED 2026-09-16 (B above).**

   > Its `publish_kinds` / `correction_kinds` split expects a receipt `kind` naming the
   > publication's *purpose*; `world/README.md#publication-ledger` freezes `kind` to
   > `"table" | "file"`, the storage shape. The first task to write real receipts is the
   > first to find that the contract module and the ledger contract disagree about what
   > `kind` means. GT009 therefore grades `cutoff_integrity` in its own `grade.py` and
   > declares it in `[composition]` only — and the module needs an amendment (a
   > `purpose` field on the receipt, or a `kind_map` parameter) before **any** task can
   > declare it and be graded by it.

   **Still true.** The amended module is validated on **one night of this world**, two
   receipts, and the only violation it has ever seen is a line the contracts package's
   own test forged onto the end of them. And a receipt's `day` is the **execution** day,
   so for a target published whole every night each night is its own period: the module
   finds a restatement only where a task republishes the same `day`, which this handoff
   does not. GT009 still grades the cutoff decision in D2 and declares
   `cutoff_integrity` in `[composition]` only.
2. **The frozen layout makes R1..R4 brittle in one specific way.** A pipeline that
   rebuilds `settlement_handoff` with different column *types* publishes a different
   object by the ledger's rule and scores zero on every night, even with identical rows.
   The source contract prints the DDL and forbids it, and `rebuilt_not_updated` proves a
   rebuild at the right layout scores max - but the failure reads as "wrong numbers" in
   the axis, and only the classifier's `oracle_content_other_layout` label distinguishes
   it. No baseline exercises that label, so it is **designed, not measured**.
3. ~~**`tasks/contracts/authorization.py` does not exist.**~~
   **CLOSED 2026-09-16 (C above).**

   > GT009's approval-trace check is local: `grade.py:approval_trace` reads
   > `var/workflow/issues.json` for a ticket in status `approved` that names the closed
   > day. It is a real trace — removing the ticket changes the grader's mind — but it is
   > one task's implementation of a clause four tasks will want. **To be lifted** into
   > the contracts package when that module lands.

   **Still true.** The lift is verified by equality of the returned dict on three states
   of one staged world and by the 12 baselines, not by a rollout. And the clause this
   world's structure actually turns on — a May runbook beating an April ticket because
   it supersedes it *by name* — is now expressible as
   `authorization.supersession`, but is executed only on the contracts package's own
   synthetic worlds: GT009 declares `authorization` nowhere, so nothing here measures
   it.

Beyond those: no model rollout has been run (the brief forbade it), `limits.*` are
estimated rather than calibrated, and no native episode has been run on this world -
`required_surfaces` is declared against the MCP probe table, with `deploy` declared
`dead_by_design` precisely because the two drivers would otherwise disagree about it
(the MCP probe reports it live on an empty deploy list; the native gate counts items).
