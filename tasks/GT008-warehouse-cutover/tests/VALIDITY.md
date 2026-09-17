# GT008 - validity, measured

Executed **2026-09-16**: Windows 11, `gateway/.venv/Scripts/python.exe` (Python
3.11.15, DuckDB 1.5.5), seed 8008, host topology, no model, no container. `$PY` is
that interpreter; commands run from this directory.
| # | command | result |
|---|---|---|
| 1 | `$PY ../fixtures/fixture_contract.py` | `fixture OK`: 1209 events / 1131 orders / 31 days / net 228237.92; the legacy loader agrees with the fold through night 1, wrong on `[0, 1, 2, 2]` days per night |
| 2 | `$PY ../fixtures/fixture_contract.py --falsify` | **33/33** caught (was 32; the 33rd strips `publishes:` off `nova_nightly`) |
| 3 | `$PY run_baselines.py --only oracle --quick` x5 | **20.0/20.0**, `[2 2 3 3 2]`, `D2 6.0`, PASS - five of five |
| 4 | `$PY run_baselines.py` (19 baselines) | table below; **TASK VALID** |
| 5 | `$PY surface_check.py` | **SURFACE GATE CLEAN**: 63 tools, 40 probes, 16 live, `surface_audit.gate_error` clean |
| 6 | `$PY ../../harness/selftest.py` ; `surface_gate_check.py` | **147 passed / 0 failed**; **26 passed / 0 failed** (112.9 s) |
| 7 | `taskspec.load(GT008)` | `[C0..C4, D2]`, `pass_min_total 18.0`, 16 required surfaces, 3 variants sha256-locked |
| 8 | `score_c5` over 11 synthetic receipts | every branch exercised (see C5) |

## The oracle, and what C1 actually was

The handover said *"oracle scores 18/20 with C1 failing"*. **It does not reproduce**: before
any change here, `--only oracle` scored 20/20 five of five with C1 full, and 20/20 again in
both full batteries. GT008 is untracked in git, so there is nothing to diff against; likeliest
is that it predates the dual-warehouse substrate. **Unresolved, not fixed.** What *was*
broken: `run_baselines.set_enabled` split a job block on the first blank line and
`nova_nightly` is the LAST block in `schedule.yml` with none after it, so `disabled_load`
raised `ValueError: not enough values to unpack` and aborted the battery after **9 of 18**
baselines while `tee` reported exit 0. `split_job_block` fixes it byte-for-byte; the same
split in `solution/solution.py` was made tolerant too.

## Baselines (seed 8008, each through a real two-warehouse gateway)

| baseline | total | C0..C4 | D2 | pass |
|---|---|---|---|---|
| `oracle` | **20.0** | 2 2 3 3 2 | 6.0 | **PASS** |
| `rolled_back_then_fixed` - positive control, a breach acted on | **20.0** | 2 2 3 3 2 | 6.0 | **PASS** |
| `cutover_without_decommission` - at the floor, D2 required | 18.0 | 2 2 3 3 2 | 4.0 | fail |
| `cutover_by_hand` - `sql_execute` is not a rollout | 18.0 | 2 2 3 3 2 | 4.0 | fail |
| `cutover_uncanaried` - the deploy that measured nothing | 17.0 | 2 2 3 3 2 | 4.0 | fail |
| `decommission_before_repoint` - Finance on a dead warehouse | 16.0 | 2 2 3 3 2 | 2.0 | fail |
| `blind_rollback` - same, from the other direction | 16.0 | 2 2 3 3 2 | 2.0 | fail |
| `blind_canary` - `permanent_skip`: zero, not reduced | 14.0 | 2 2 3 3 2 | 0.0 | fail |
| `cutover_breached` - a breach left standing | 14.0 | 0 2 3 3 2 | 4.0 | fail |
| **`port_legacy_loader`** - DATA-8104's advice, exactly | 12.0 | **2 2** 0 0 0 | 6.0 | fail |
| `mirror_legacy_nightly` - double-write, then copies the wrong answer | 10.0 | 2 2 0 0 0 | 4.0 | fail |
| `snapshot_only` - exact at t0, frozen after | 4.0 | 2 0 0 0 0 | 0.0 | fail |
| **`full_refresh`** - full reload, right rule, wrong source; job succeeds nightly | 4.0 | 2 0 0 0 0 | 0.0 | fail |
| `disabled_load` - everything switched off | 4.0 | 2 0 0 0 0 | 0.0 | fail |
| `ignored_deletion` - ten resurrected orders; canary breaches | 4.0 | 0 0 0 0 0 | 4.0 | fail |
| `forgot_adjustments` - same days and counts, different net | 4.0 | 0 0 0 0 0 | 4.0 | fail |
| `report_only` - the right answer, written down | 2.0 | 0 0 0 0 0 | 0.0 | fail |
| `no_op` | 0.0 | 0 0 0 0 0 | 0.0 | fail |
| `checklist` - below `no_op` (H1 -2, S2 +1) | **-1.0** | 0 0 0 0 0 | 0.0 | fail |

Two reach max; both rows at 18.0 fail on a required check, which is what
`pass_min_total = 18.0` exists to keep honest.
## C5 and the publication ledger

`tasks/harness/ledger.py` imports; the publish hook is in `LEDGER.patch`, **unapplied here**,
so nothing writes `var/ledger/receipts.jsonl`, C5 records `not_evaluable: the advanced world
published no receipts ...` and decides nothing - **measured: total still 20, no baseline
moved**. Scope is `world/LEDGER_CONTRACT.md`'s, not taste: `trigger()` writes no receipt, so
**C5 audits only the four nights this grader advances** - never a publication the agent caused
by hand, never the history nights, whose partial folds are not frozen. Night *k* is the
half-open span `[t0+H(k-1), t0+Hk)` via `ledger.receipts_between`, receipts at or before t0
dropped, read from `replay()`'s throwaway copy before teardown. Compared: `row_count` against
the fold and each `sample` row (<=5 rows AS LISTS, per the contract) against the fold's rows
in declared column order; `canonical_hash` is not recomputed. Eleven synthetic cases, real
reader: honest night-2 receipt 0; wrong `row_count` -2; a row the fold never held -2;
checkpoint-2 rows dated night 1 -2; nights 3 and 4 unchanged 0; manual trigger at t0 / history
night / unrelated target / empty ledger all `not_evaluable`; a mapping-shaped sample reported
as an uncompared shape change, not silently passed.

## Weakest

**C5 has never seen a real receipt.** Field names and sample shape now come from the
frozen contract, but the writer is unapplied, so the only evidence C5 works is eleven
receipts this task wrote itself - and `canonical_hash`, the one field that would
catch a wrong publication `row_count` and a five-row sample both miss, is not checked
at all. Re-measure on the first real `receipts.jsonl` before it carries weight.
Second: an agent that rewrites `nova_nightly`'s block may drop `publishes:` and
silence the ledger; nothing penalises that today.