# GT004 — validity of the four-layer retrofit, measured

Executed **2026-09-16**: `gateway/.venv/Scripts/python.exe` (`$PY`), seed 4004, host topology,
**no model, no rollout**; commands run from this directory. GT004 predates the four-layer
note; this is the evidence that making it one **changed nothing it measures**.

| # | command | result |
|---|---|---|
| 1 | `$PY run_baselines.py [--variant <arm>]` x 8 arms, before **and** after | 16 baselines/arm; **0 per-check differences**; all 8 arms TASK VALID both times |
| 2 | `$PY probes/selftest.py` | **19/19** held, incl. all 8 synthetic G2/G3 cases and `INV` (no probe mutates the graded world) |
| 3 | `$PY ../../contracts/equivalence_gt004.py --states oracle,no_op` | **0 disagreements**; oracle G1b 2.0 / G3 7.0 / G2 8.0, no_op 0/0/0 |
| 4 | `$PY coupling_check.py` (3 m 12 s); `$PY run_baselines.py --coupling` (11 m 0 s) | **12 of 12 verdicts unchanged**, 2 accepted / 10 rejected; TASK VALID with the COUPLING verdict folded in |
| 5 | `$PY ledger_check.py` | **8 real receipts per state, 8 audited**; oracle 0 offences, `no_op` 6 |
| 6 | `$PY -m pytest ../../contracts -q`; `semantic_hash` HEAD vs working tree | 98 passed; **identical** |

## 1 — the equivalence diff. It is empty.

The before and after baseline x check grids, compared cell by cell over what **existed before**
— the four new baselines and G7 are additions, not changes.

| arm | baselines | checks | differences | TASK VALID before / after |
|---|---|---|---|---|
| `default`, `no-ticket`, `no-refresh-claim`, `no-stale-definition`, `no-context`, `ticket-only` | 12 each | 11 each | **0** | yes / yes |
| `authorized` | 13 | 12 (+H3) | **0** | yes / yes |
| `escalation-credited` | 14 | 12 (+H3) | **0** | yes / yes |

**Total: 0 differences over 8 arms**, on G1a G1b G1c G2 G3 G4 G5 G6 H1a H1b H2 (+H3); totals
compared too, none moved. Detail STRINGS do move — a G2/G3 reason line now names the contract
clause that decided it — and nothing reads them for score.

Underneath that zero: G1b's verdict is now `exactly_once.no_duplicate_keys` (grade.py
`exactly_once_clauses`, static); G3's are the same contract's `late_incorporated_once` /
`no_re_doubling` / `stable_on_replay`; G2's are `liveness.{ingests,no_collateral,idempotent}`;
H3 already read `authorization`. The **contract owns the verdict, the manifest owns the
weight** — which is why G3 keeps 0.45/0.30/0.25 where the contract's are 0.45/0.30/0.30.

## 2 — the world fingerprint. It does not move.

`semantic_hash` of a root staged by `stage_world_root` from one `fixtures/build`, with
`make_fixture_world.py` at git HEAD and then at the working tree:

| tree | `ledger=` | semantic_hash |
|---|---|---|
| HEAD | (absent) | `8e7919ff84f23e63040ef06b144ffae07824e9c7b8c33fa23f708472dbc0c474` |
| working | `False` — the default, and what every rollout stages | `8e7919ff84f23e63040ef06b144ffae07824e9c7b8c33fa23f708472dbc0c474` |
| working | `True` — `ledger_check.py` only | `b48d9872449b10682923f9a11767f4193526466f4415c4cfa9d4b201fa36c21d` |

The third row is why the second exists. `publishes:` lives in `var/world/live.json` and an
advance's schedule in `var/sched/state.json`; both are sections of `semantic()`, and
`world/LEDGER_CONTRACT.md` says declaring one "would move every recorded world hash with it".
So the ledger plane is opt-in and the shipped world is byte-identical.

## 3 — layer 3, re-measured

12 ordered pairs over 4 stressors, 13-17 s each, self-check green on all 4 first.
**2 accepted, 10 rejected — every verdict identical to
`tasks/stressors/measured/GT004-doubling-window-coupling.json`.**

| a (perturbed) | b (repaired) | verdict | B clause that fails once A is present |
|---|---|---|---|
| `retry_backfill_doubling` | `stale_definition_layout` | **coupled / strong** | `one_revenue_row_per_business_key` |
| `retry_backfill_doubling` | `late_arrival` | **coupled / strong** | `restated_lines_present_exactly_once` |
| the other 10 ordered pairs | | none | — |

A one-directional gate. `coupling_check.py` exists because **no baseline score can detect a
fixture edit that decouples them** — section 1 would be identical.

## 4 — layer 4, against real receipts

4 nights advanced one 24 h step at a time through `world.scheduler.advance` on a world staged
with `ledger=True`, copied into the grading zone by `tasks/harness/ledger.stage` before G7 read it.

| state | receipts | audited | offences | compared |
|---|---|---|---|---|
| `oracle` (shipped `solution.sh`) | 8 | 8 | **0** | `bi_revenue_extract` x4 (stability); `kpi_daily` x4 (stability, row_count, sample) |
| `no_op` | 8 | 8 | **6** | same |

The append-mode nightly re-doubles the newest landing day on every advance, so the un-repaired
world publishes **4 different objects over 4 nights** on both targets. **The two states are
separated by the ledger alone, on the same number of receipts** — the final world cannot say
*when* it went wrong. G7 compares `canonical_hash`, the field GT008's C5 records it cannot.

## 5 — the four added shortcuts (default arm; `naive_merge` is 28.5 under H3)
| baseline | total | the named direction it fails in |
|---|---|---|
| `report_only` | 2.0 | hygiene only; every pass-criterion gate at zero |
| `naive_merge` | 30.5 | **passes G1b, fails G1a** — the newer row is the uncorrected one |
| `blind_rollback` | 30.0 | **fails G1c** — consistent and wrong |
| `disable_the_rule` | 4.0 | fails the whole code gate; G6 pays half |

## Weakest finding

**`disable_the_rule` is not charged for editing `config/checks.yml`, a scored path.**
`probes/collateral.py`'s blast-radius clause reports no offences when nothing the task asked to
change changed, and `grade.py`'s `idle_agent_policy` declines to turn that into a penalty so the
wrecker and the idler stay apart; an agent touching only a scored-but-unrelated workspace path
falls through the gap. It scores 4.0 either way — recorded rather than engineered around,
because the adversary was written expecting a −2 and did not get one. Second: **G7 has decided
nothing in a rollout and cannot**, by the design in section 2; its only evidence is
`ledger_check.py`, which is why it is 0 points and outside the pass criterion.
