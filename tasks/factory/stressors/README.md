# Factory sidecar composition

The composer attaches a partner feed to a theme and writes three single-path
pipeline modules. Shipped code is the broken path for the active stressors and
the vulnerable readers for the active hazards. The repaired modules live only
under `solution/`.

```python
from tasks.factory.stressors.oracle_composer import (
    compose_task, stage_world, oracle_trajectory,
)

# Minimal DuckDB world. out_dir must be outside the git repository.
result = compose_task(theme, stressors, hazards, governance_mode, seed, out_dir)

# Already built world, injected in place.
result = compose_task(task_dir, blueprint, built_world_root)
stage_world(task_dir, fresh_world_root, blueprint)
evidence = oracle_trajectory(task_dir, built_world_root, output_root)
```

`blueprint` accepts `stressors` or `t0_stressors` and `hazards` or `r_hazards`.
Each side takes 1 to 3 distinct ids. `S01_name` and `H01_name` canonicalize to
`S01` and `H01`. Also pass `seed`, `governance_mode`, and optional
`closed_day`, `ticket`, and `controller`.

Shipped files, relative to the world:

- `workspace/pipelines/ingest_feed.py` — scheduler actions `arrive`, `load`,
  and `retry`. `load` calls `fold_ledger.py`.
- `workspace/pipelines/fold_ledger.py` — action `fold`.
- `workspace/pipelines/publish_handoff.py` — actions `publish` and `close`.

There is no `factory_events.py` and no `POLICY` dict. Jobs stay
`factory_arrive`, `factory_load`, optional `factory_load_retry` when H12 is
active, `factory_publish`, and `factory_close`.

Grade `tests/factory_sidecar_spec.json`:

- `execution_tables` — relations actually written at t0, including the checkpoint.
- `future_tables` — raw, fact, and handoff replayed on R1–R4.
- `publication_table` — handoff that was written. A theme with a null handoff
  uses `warehouse.factory_handoff`.
- `tables` — native contract names for surfaces the theme declares. Null
  surfaces are omitted. Generic `raw.factory_events`,
  `warehouse.fct_factory_events`, and `warehouse.factory_handoff` are omitted
  here even when execution fell back to them because a built theme already
  owned the native table with a different shape.
- `grain_keys` — physical feed columns (`event_id`, `business_id`,
  `business_day` + `entity_id`, `through_day`).
- `domain` and `domain_grain_keys` — the theme contract, including its own grain.

`solution/solve.py` installs `solution/oracle/` and repairs t0.
`solution/partial_fix.py` repairs t0, then leaves `solution/partial/` in place
for later nights. Rivals are `solution/wrong_precedence.py`,
`solution/duplicate_counted.py`, and `solution/boundary_violated.py`.
Active hazards also get an indirect clue under `workspace/docs/` or
`var/logs/`. R1–R4 payloads stay in `build/factory_future`.
