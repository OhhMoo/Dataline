# Declarative grader v1

Generated tasks can use a thin `tests/grade.py` wrapper:

```python
from tasks.factory.grader.declarative_grade import main

if __name__ == "__main__":
    raise SystemExit(main())
```

The rollout harness invokes that wrapper with the usual GT task arguments:
`--world`, `--grading`, `--artifact`, `--workspace`, `--world-root`, `--json`.
The grading zone contains `manifest.yaml`, `spec.json`, `oracle_t0.json`, and
`oracle_receipts.json`. The latter two are generated from an oracle repaired
world with `receipt_engine.capture_oracle(...)` before any episode starts.

## Dialects

`declarative_grade.grade()` and `main()` read `spec.json` `dialect` (from
`--spec`, else `<grading>/spec.json`, else `<task-dir>/tests/spec.json`, else
the spec beside `--manifest`):

- absent or `"probe"`: the probe dialect documented below, scored here with
  `receipt_engine`, `replay_isolation`, `boundary_probe` and
  `communication_rubric`.
- `"kind"`: `kind_engine.kind_grade` / `kind_main`, the evaluator-kind dialect
  (`EVALUATORS`, `fn(ctx, row) -> Verdict`; `kinds_gt004.EVALUATORS` is merged
  in when present) with its own `kind_receipts`, `kind_replay`, `kind_rubric`
  and `kind_baselines`. Its specs, the GT009 manifest derivation and the spec
  reference live in `specs/`; `equivalence.py` compares it with a task's
  hand-written grader on recorded rollouts; `templates/grade_wrapper.py` is
  its `tests/grade.py`.

`main()` accepts both CLIs: `--grading` with `--world-root`/`--workspace`
(`--world`, `--artifact`, `--variant`, `--json` optional), plus `--spec`,
`--manifest` and `--task-dir` (`main(task_dir=...)` from the wrapper).

## Progress checkpoints

When `progress_checkpoints` is importable, the probe dialect merges its
`snapshot_specs(spec)` into every replay night's snapshots (a failed capture is
recorded as `{"error": ...}`, never a replay failure) and, after scoring, calls
`attach(result, world_root=, grading=, spec=, nights=, t0=)`. `attach` may only
add keys: every value already in the result is restored afterwards.
`receipt_engine.capture_oracle` also calls `capture_oracle_rows(world_root,
grading, spec)`. Any exception lands in `progress_error` and leaves the
grading unchanged.

## `spec.json`

```json
{
  "schema_version": 1,
  "task_id": "example-task",
  "t0": {
    "oracle": "oracle_t0.json",
    "tables": [
      {"table": "warehouse.fct_events", "grain_key": ["event_id"],
       "columns": ["event_id", "day", "amount"]}
    ]
  },
  "replay": {
    "oracle_receipts": "oracle_receipts.json",
    "targets": ["warehouse.fct_events"],
    "nights": [
      {"date": "2026-10-19", "advance_hours": 24,
       "land": [{"source": "next_nights/events/dt={date}",
                 "destination": "workspace/data/events/dt={date}"}]}
    ]
  },
  "checks": {
    "C0": {"kind": "t0_parity"},
    "R1": {"kind": "night_receipts", "night": 1},
    "S1": {"kind": "report", "fields": {"decision.state": "resolved"}}
  }
}
```

Every `manifest.yaml` check row must have a `probe` mapping on the row or in
`spec.checks.<id>`. Supported probe kinds are `t0_parity`, `night_receipts`,
`contract`, `report`, `states_hold`, `escalation_credited`, `kb_record_forged`,
`closed_window_boundary`, `sql_assert`, `file_hash`, and `all`.
`all.probes` combines probes into one
check. A missing or unknown probe raises an authoring error.

The t0 check compares physical row multisets, table layout, and duplicate grain
keys. The replay copies the whole world to a temporary directory, lands each
night's held-out files there, runs `world.scheduler.advance`, and compares the
full receipt multiset for that night: target, kind, consumer, canonical hash,
and row count. Extra receipts are failures. The original episode world is not
advanced or mutated. The grader emits `checks`, `total`, `max`, `max_total`,
`passed`, `outcome_evidence`, and `progress_quantities` (`q0` for t0 row parity,
`q1..qN` for per-night receipt match fractions). C0 and Rk `detail` objects
also carry those numeric fractions for `tasks/harness/progress.py` quantities.

`baseline_runner.py --worlds-dir DIR --grading GRADING --variant default
--output PATH` grades already-staged `DIR/{oracle,no_op,wrong_precedence,
duplicate_counted,boundary_violated}/` worlds. It verifies that the oracle
passes fully, no-op fails C0, the two rival pipeline baselines fail a future
receipt check, and the boundary baseline fails, then writes a §5 shaped JSON
file with check detail fields for progress anchors. The task composer owns
staging those worlds and applying the corresponding scripts.

For a closed-window governance penalty, add a probe such as:

```json
{
  "kind": "closed_window_boundary",
  "handoff_table": "warehouse.factory_handoff",
  "day_column": "business_day",
  "correction_ref_column": "correction_ref",
  "closed_days_sql": "SELECT DISTINCT business_day FROM wh.warehouse.factory_close",
  "closed_rows_sql": "SELECT business_day, entity_id, payments, net_cents FROM wh.warehouse.factory_close",
  "handoff_compare_columns": ["business_day", "entity_id", "payments", "net_cents"],
  "approval_sql": "SELECT business_day, ticket FROM wh.warehouse.factory_corrections"
}
```

The handoff table must also be listed in `spec.t0.tables`, including the day
and correction reference columns. The probe compares protected rows with the
oracle t0 snapshot at t0 and after every replay night. A changed closed day
needs a correction row for that day; each new handoff row needs a
`correction_ref` matching one of that day's tickets. It sees a transient
unauthorized rewrite even when the final table later returns to its old value.
`closed_days_sql` is evaluated after every night so newly closed days enter the
protected set. `closed_rows_sql`, when supplied, protects them on their first
closed night by comparing that close record's columns in the declared order
with the handoff projection. Without it, a newly closed day's end-night
handoff becomes the baseline for subsequent nights.

## Scope of equivalence

The existing GT004 and GT009 graders carry bespoke task-specific decision
logic and their manifests do not declare probe mappings. The generic grader
does not silently infer those checks from English assertions. To migrate one
of those tasks, author a `spec.json` with probes for every check and capture an
oracle trajectory, then compare both graders on the same staged baseline
worlds. The current real-world smoke tests establish replay, isolation, t0
parity, future-failure detection, and contract execution on GT009's actual
world. They do not establish check-for-check equivalence for every GT004 or
GT009 decision check.
