# Dynamic worlds (A+B+C) Implementation Plan

> **For agentic workers:** executed with superpowers:dispatching-parallel-agents. Phase 1 tasks
> are independent and run in parallel, each in its own git worktree; Phase 2 starts after Phase 1
> is merged. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make an Alkera world keep running during and after an episode, drive its operational
shocks with a seeded Hawkes process, measure per-contract damage at every tick, and score the
agent against no-op and oracle twin worlds replayed on the same shocks.

**Architecture:** Four new pure-Python modules in `world/world/` (hawkes, damage, clock) plus a
pilot theme + task (DW-1) in Phase 1; twin runner, attribution and episode integration in
Phase 2; scripted ladder and Opus runs in Phase 3. Shocks compile to the existing fault
catalogue (`only:` occurrence lists), so all execution goes through `world.scheduler.advance`.

**Tech Stack:** Python 3.11, DuckDB, dbt-duckdb, PyYAML, pytest. Interpreter
`gateway/.venv/bin/python`.

**Spec:** `docs/implementation_plan/experimental_ideas/2026-09-30-dynamic-world-design.md`

## Global Constraints

- Deterministic: same seeds + same command sequence ⇒ byte-identical ledgers and identical $J$.
- Every random choice is `world.faults.draw(fault_seed, <id>, <key>)` (keyed; no PRNG streams).
- No new execution path: shocks execute only through `world.scheduler.advance`.
- Never fabricate state: damage is measured from the world, never asserted.
- Ledgers (`shock_schedule.json`, `damage.jsonl`, `tick_schedule.json`) are author-side and must
  never be readable by the agent.
- Do not modify existing tasks GT001–GT009, their graders, fixtures or baselines.
- Do not change `world.scheduler` / `world.faults` behaviour for worlds without the new files;
  `pytest -q world/tests` must stay green.
- No git commits by agents; leave changes uncommitted in the worktree (the owner commits).
- Tests: `PYTHONPATH=world gateway/.venv/bin/python -m pytest -q <path>`; set
  `TMPDIR=$HOME/.cache/alkera-dyn-<task>`.

## Shared interfaces (fixed here; every task codes against them)

### `shock_spec.yaml` (input to hawkes)

```yaml
fault_seed: 7
rho: 0.6                    # target branching ratio, 0 <= rho < 1
beta_per_hour: 0.05         # kernel decay
horizon_hours: 168          # occurrences enumerated over [vnow_t0, vnow_t0 + horizon]
excitation: registry        # or an explicit square matrix `A: [[...]]` in kinds order
kinds:                      # order = precedence when two fire on one occurrence
  - kind: duplicate_arrival # a world.faults kind
    target: {arrival: daily_revenue}
    mu: 0.04                # base intensity per occurrence
    params: {}
  - kind: late_arrival
    target: {arrival: daily_revenue}
    mu: 0.04
    params: {delay_minutes: 240, missed_by: [daily_load]}
  - kind: job_overrun
    target: {job: dbt_build}
    mu: 0.03
    params: {}
```

### `shock_schedule.json` (output of hawkes; ledger)

```json
{"fault_seed": 7, "rho_target": 0.6, "rho_empirical": 0.57, "beta_per_hour": 0.05,
 "A": [[...]], "alpha": [[...]],
 "shocks": [{"n": 0, "id": "hawkes:duplicate_arrival:0", "kind": "duplicate_arrival",
             "key": "day:31", "target": {"arrival": "daily_revenue"}, "vts": "2026-03-02T00:00:00Z",
             "parent": null, "generation": 0, "suppressed": false}]}
```

Catalogue entries produced for `live.json` `live.faults`: one per non-suppressed shock,
`{"id": <id>, "kind": <kind>, "enabled": true, "only": [<key>], "params": {...}}` plus
`"jobs": [<job>]` for job targets.

### `damage.yaml` (input to damage) and `damage.jsonl` rows

Table and column names below are illustrative; Task 4 fixes DW-1's real ones. The keys and
their meaning are the contract.

```yaml
db: var/warehouse/warehouse.duckdb        # relative to world root
weights: {exactly_once: 1.0, liveness: 1.0, consumer_consistency: 1.0}
contracts:
  exactly_once:          {table: warehouse.fct_revenue_lines, key: [order_id]}
  liveness:              {table: raw.daily_revenue, partition_col: dt,
                          arrival: daily_revenue, grace_hours: 6}
  consumer_consistency:  {table: warehouse.fct_revenue_daily, key: [order_date],
                          compare: [revenue], reference_sql: "SELECT ..."}
  version_precedence:    {table: ..., key: [...], version_col: ..., source_sql: "..."}
  cutoff_integrity:      {table: ..., key: [...], closed_through: "2026-02-28",
                          date_col: ..., snapshot: var/world/published_snapshot.parquet}
  auditability:          {files: [var/sched/state.json], baseline: var/world/audit_baseline.json}
```

Row: `{"tick": 3, "vnow": "...", "phase": "episode"|"post", "V": {"exactly_once": 0.012, ...},
"detail": {"exactly_once": {"dup_keys": 14, "keys": 1180}, ...}}`. Every `V` value in [0, 1].

### `tick_schedule.json` (output of clock; ledger)

```json
{"m": 10, "h_hours": 6.0, "T_hours": 168.0,
 "ticks": [{"tick": 0, "phase": "episode", "after_command": 10,
            "vnow_before": "...", "vnow_after": "..."}]}
```

`phase: "post"` ticks have `after_command: null`.

### Python signatures

```python
# world/world/hawkes.py
def load_spec(path: Path) -> dict
def occurrences(root: Path, horizon_hours: float) -> list[dict]      # {key, cls, target, vts}
def excitation_matrix(spec: dict, registry_path: Path | None) -> list[list[float]]
def spectral_radius(M: list[list[float]]) -> float
def sample(root: Path, spec: dict, registry_path: Path | None = None) -> dict   # schedule
def to_catalogue(schedule: dict) -> list[dict]
def install(root: Path, schedule: dict) -> None   # writes live.json faults + var/world/shock_schedule.json

# world/world/damage.py
CONTRACTS = ("exactly_once", "liveness", "version_precedence",
             "cutoff_integrity", "consumer_consistency", "auditability")
def load_spec(path: Path) -> dict
def sample(root: Path, spec: dict) -> dict            # {vnow, V, detail}
def append(ledger: Path, row: dict) -> None
def integral(rows: list[dict], weights: dict, h_hours: float, terminal_gamma: float = 1.0) -> float

# world/world/clock.py
@dataclass(frozen=True)
class TickSpec: m: int; h_hours: float; T_hours: float
class Clock:
    def __init__(self, root: Path, spec: TickSpec,
                 on_tick: Callable[[Path, dict], None] | None = None): ...
    def before_command(self) -> dict | None    # tick record if a tick fired
    def finish(self) -> list[dict]             # post-episode ticks up to T
    def schedule(self) -> dict                 # tick_schedule.json content
def replay(root: Path, schedule: dict, policy: "Policy",
           on_tick: Callable[[Path, dict], None] | None = None) -> list[dict]

class Policy(Protocol):                          # defined in world/world/clock.py
    name: str
    def act(self, root: Path, tick: int, vnow: str) -> list[str]   # actions taken, for the log
```

`replay` semantics: before tick 0 call `policy.act(root, -1, vnow_t0)`; then for each tick in
`schedule["ticks"]` (episode then post), call `policy.act(root, tick, vnow_before)`, then
`advance(root, h)`, then `on_tick(root, record)`. Policies act only at tick boundaries.

## Review Focus

1. Two shocks on one occurrence: only the first in spec order executes; the other is recorded
   `suppressed: true` and never reaches the catalogue (hawkes test).
2. `rho: 0` yields only generation-0 shocks; `rho` ≥ 1 is rejected with a clear error (hawkes test).
3. A world with no damage-relevant rows (empty fact) samples `V = 0`, not a division by zero
   (damage test).
4. `Clock.finish()` when the episode already passed `T` produces no post ticks and no negative
   advance (clock test).
5. Re-running `install` + `replay` on a fresh build with the same inputs reproduces identical
   ledgers byte for byte (DW-1 determinism test).

---

## Phase 1 (parallel)

### Task 1: Hawkes shock generator

**Files:** Create `world/world/hawkes.py`, `world/tests/test_hawkes.py`.

**Interfaces:** Consumes `world.faults.draw`, `world.faults.arrival_key/job_key`,
`world.scheduler.plan/load/load_live/parse_vtime` (dry-run occurrence enumeration),
`tasks/stressors/registry.yaml`. Produces the `hawkes` signatures and the schedule/catalogue
formats above.

- [ ] Write tests first:
  - `test_same_seed_same_schedule` (two `sample` calls, identical JSON);
  - `test_rho_zero_has_no_offspring` (all `generation == 0`);
  - `test_rho_ge_one_rejected` (`ValueError`, message names rho);
  - `test_empirical_branching_tracks_target`: over 200 seeds on a synthetic lattice of 2,000
    occurrences, mean offspring per shock within ±0.08 of rho for rho ∈ {0.3, 0.6};
  - `test_one_shock_per_occurrence` (collision → `suppressed: true`, absent from catalogue);
  - `test_catalogue_executes`: build a `revenue_daily` world with live faults enabled via
    `world.build`, `install` a hand-written schedule with one `duplicate_arrival`, `advance`
    past it, and assert `fault_log` has exactly that id.
- [ ] Implement: occurrence lattice from `scheduler.plan` (arrivals `day:{i}`, jobs `job@vts`);
  discrete-time intensity $\lambda_k(o) = \mu_k + \sum \alpha_{kj} e^{-\beta \Delta t}$; fire iff
  `draw(seed, f"hawkes:{kind}", key) < 1 - exp(-lambda)`; parent chosen by a second keyed draw
  proportional to each earlier shock's contribution (immigrant if the base term is chosen);
  `excitation: registry` builds $A_{kj}$ as the Jaccard overlap of the two kinds' targets'
  declared state (jobs/tables from the theme manifest's `exec.tables`), diagonal = 1;
  $\alpha = \rho\,\beta\, A/\mathrm{sr}(A)$; `spectral_radius` by power iteration.
- [ ] `pytest -q world/tests/test_hawkes.py world/tests/test_faults.py world/tests/test_live_world.py` green.

### Task 2: Damage monitor

**Files:** Create `world/world/damage.py`, `world/tests/test_damage.py`.

**Interfaces:** Produces the `damage` signatures and `damage.jsonl` rows above. Reads DuckDB
read-only (`duckdb.connect(db, read_only=True)`); never writes the world.

- [ ] Tests first, each on a tiny DuckDB built in the test:
  - per contract: a clean table gives 0.0; a planted defect gives the exact known value
    (e.g. 3 duplicated keys of 100 → 0.03);
  - an empty table gives 0.0 (Review Focus 3);
  - contracts absent from the spec are absent from `V`;
  - `integral` of a constant 0.5 over 4 ticks with `h_hours=6` and `terminal_gamma=1` equals
    `0.5*24 + 0.5`;
  - `sample` opens the db read-only (a write attempt inside a contract function raises).
- [ ] Implement one `_v_<contract>(con, root, cfg, vnow) -> tuple[float, dict]` per contract, as
  defined in the spec's table: exactly_once (dup keys / keys), liveness (partitions due by
  `vnow - grace` and absent / due, due dates from the arrival stream in `live.json`),
  version_precedence (keys whose served version < max delivered / keys), cutoff_integrity (rows in
  the closed period differing from the snapshot / rows in it), consumer_consistency (rows where
  table ≠ `reference_sql` on `compare` cols / rows), auditability (files whose hash differs from
  the baseline / files).
- [ ] `pytest -q world/tests/test_damage.py` green.

### Task 3: World clock (offline) and replay

**Files:** Create `world/world/clock.py`, `world/tests/test_clock.py`.

**Interfaces:** Consumes `world.scheduler.advance`. Produces `TickSpec`, `Clock`, `replay`,
`Policy`, and `tick_schedule.json`.

- [ ] Tests first, on a built `revenue_daily` world:
  - `before_command` fires exactly on commands m, 2m, … and advances `vnow` by `h_hours`;
  - `finish` advances in `h_hours` steps until total elapsed = `T_hours`, the last step shortened
    if needed; none when already at/after T (Review Focus 4);
  - `replay` of a schedule with a no-op policy lands the same `vnow` and the same
    `var/sched/state.json` as the live `Clock` run;
  - policy `act` is called with `tick=-1` first, then once per tick, before each advance;
  - `advance(h)` × n ≡ one run with the same ticks (state.json identical).
- [ ] Implement as specified; `on_tick(root, record)` is called after each advance.
- [ ] `pytest -q world/tests/test_clock.py world/tests/test_live_world.py` green.

### Task 4: Pilot world DW-1 (theme + task)

**Files:** Create `world/world/themes/revenue_live/` (derived from `revenue_daily`),
`tasks/DW001-live-revenue/` with `task.toml`, `instruction.md`, `damage.yaml`,
`shock_spec.yaml`, `policies.py`, `solution/`, `README.md`, and
`tasks/DW001-live-revenue/tests/test_dw001.py`.

**Interfaces:** Consumes the schemas above (not the modules; write direct SQL assertions so this
task does not wait for Tasks 1–3). Produces a world where each latent defect turns a named
shock into measurable damage, the oracle patch, and the seven scripted policies
(`noop, oracle, root_late, symptom, partial, overreach, vandal`) implementing `Policy`.

- [ ] Plant three latent defects in the theme's workspace, each with a registry id comment:
  1. exactly_once: the dbt fact does not deduplicate `order_id`, so a `duplicate_arrival` (the
     loader's glob reads the sibling copy) doubles that day's lines;
  2. liveness: replace the full-reload loader with a checkpointed loader (`exec.kind: script`)
     that advances its checkpoint to the max `dt` seen, so a `late_arrival` partition is never
     loaded;
  3. consumer_consistency: a NULL-unsafe `NOT IN` quality gate that drops lines with a NULL
     `terminal_ref` (a column added to the generator, NULL in a seeded fraction of rows).
- [ ] Tests first (`test_dw001.py`), each building the world with `world.build` into `tmp_path`:
  - each defect × its shock (declared with `only:` directly in live.json) produces damage
    visible by SQL, and the same shock after applying `solution/` produces none;
  - defect 3 produces damage with no shock at all;
  - `oracle` → zero damage after a week of shocks; `noop` → positive;
  - determinism: two builds + same live.json + same advances → identical warehouse row counts
    and identical `fault_log` (Review Focus 5).
- [ ] Write `damage.yaml` and `shock_spec.yaml` for DW-1 against the schemas above;
  `instruction.md` is an on-call handover ("revenue looks off; keep the pipeline healthy this
  week") that names no defect.
- [ ] `pytest -q tasks/DW001-live-revenue/tests world/tests/test_world.py` green.

## Phase 2 (after Phase 1 merges)

### Task 5: Twin runner, score and attribution (`tasks/harness/twins.py`)
- Rebuild from seed, `hawkes.install` the episode's schedule, `clock.replay` with `noop` and
  `oracle`, sample damage on every tick; compute $J$, $S$; per-fault onset, time-to-detect
  (audit log), time-to-mitigate, fixed-vs-patched (recurrence); exact Shapley attribution over
  the fault set for the H4 closure gate; `report.md` damage-curve section.

### Task 6: Episode integration
- Tick hook in `gateway/docker/subject/rexec.py` driven by `ALKERA_CLOCK_SPEC`, counting model
  commands; ledgers in a directory the agent user cannot read (containment probe extended to
  prove it); harness copies ledgers out and runs `Clock.finish` to T after the agent exits.

## Phase 3

### Task 7: Scripted ladder
- 7 policies × rho ∈ {0, 0.3, 0.6, 0.9} × 10 seeds; assert the spec §6 ordering, H2's growing
  gap, determinism, empirical branching; variance of paired vs unpaired $S$ (H3).

### Task 8: Opus runs
- K=8 at the four rho values with the audit; dial curve (H1), root-vs-symptom classification (H2).

---

## Phase 1 outcome (2026-09-30) and decisions carried into Phase 2

Merged on `experimental_ideas` (uncommitted): `world/world/{hawkes,damage,clock}.py` + tests,
`world/world/themes/revenue_live/`, `tasks/DW001-live-revenue/`. `pytest world/tests`: 249 passed
(192 before + 57 new). End-to-end smoke (real Hawkes schedule, 24 h ticks, 168 h): S = oracle 1.0,
root_late 0.46, symptom 0.16, noop 0.

Decisions (supersede the text above where they differ):
1. **Replay advances to the recorded `vnow_after`**, not by a fixed `h`: `scheduler.trigger` moves
   `vnow`, so a policy (or agent) that triggers a job would otherwise desynchronise the twins.
   Elapsed time toward T is actual virtual time. `schedule()` carries `vnow_start`.
2. **Branching ratio: report the measured ρ̂, not the nominal ρ.** On the daily lattice the
   process starts empty, the `1-e^{-λ}` link saturates, and one-shock-per-occurrence suppresses
   children: ρ=0.6 gives ρ̂≈0.37 over 168 h. α is calibrated against the lattice kernel mass
   (`α = ρ (A/sr A) / S`), so the *theoretical* ratio is exact. H1 and Task 7 use ρ̂ as the x-axis.
   Optional `link: linear` reduces saturation.
3. **damage.py attaches the warehouse read-only under an alias** (`ATTACH ... AS wh (READ_ONLY)`;
   `warehouse.<t>` names stay valid). Auditability `files` entries are `{path, rows}`
   (`rows: runs` for state.json), so ordinary appended runs are not damage.
4. **DW-1**: `late_arrival` uses `delay_minutes: 1560` (lands after the next 01:00 load, which is
   what the checkpoint defect needs); liveness `grace_hours: 50` so the fixed loader scores 0.
   Registry ids are author-side only (`theme.yaml`, `gen.py`, `task.toml`), never in the workspace.
5. **Overreach S<0 holds only past ~1–2 weeks** (stopped loads cost only after the grace period);
   at T=168 h S≈+0.04. Task 7 runs T ∈ {168, 336} h and reports the crossover rather than tuning it.
