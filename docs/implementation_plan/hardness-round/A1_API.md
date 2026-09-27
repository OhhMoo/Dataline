# A1 — `tasks/contracts/rebuild.py`

Owner: A1. Tests: `tasks/contracts/tests/test_rebuild.py`. Do not edit `tasks/contracts/__init__.py` or `tasks/contracts/tests/conftest.py`. Do not add `rebuild` to `REGISTRY`.

Allowed imports: the Python standard library, `duckdb`, and `tasks.contracts._world`. Nothing else. `tests/test_registry.py` AST-walks every `tasks/contracts/*.py` and rejects an import whose top-level name is `probes`, `fixture_contract`, `make_fixture_world`, `world`, `alkera_gateway`, `live_fire`, or `schedule_model`. A relative import of `_world` passes that walk. Do not import a task package, `world/`, or `gateway/`.

`duckdb` access goes through `_world` (`open_world`, `business_columns`, `business_fingerprint`, `key_count`). Do not shell out. Do not call the scheduler. `run_pipeline` executes declared files on a copy; `FileScheduler.trigger` reports success without executing for commands other than `dbt_build` / `dbt_run`.

The caller's world is not mutated. `copy_world` into a temporary directory, and delete that directory before returning. Close each `open_world` connection before `run_pipeline` (the pipeline opens its own). `run_pipeline` indexes `step["path"]`. Call `_world.normalise_steps(steps)` and pass the result. A list of path strings is what the tests pass.

`audit_columns is None` means drop no columns (`business_columns` drops only the list it is given). Let `ContractNotEvaluable` from `plant` propagate. Do not turn it into `liveness_failed`.

## Types and functions

```python
@dataclass(frozen=True)
class RunTwiceResult:
    business_identical: bool
    planted_once: bool
    liveness_failed: bool  # True when the pipeline ingested nothing (permanent skip)
    detail: str

@dataclass(frozen=True)
class RematerialiseResult:
    matches: bool
    detail: str

@dataclass(frozen=True)
class SecondSeedResult:
    hardcoded: bool  # True means FAIL (right on seed A, wrong on seed B)
    detail: str


def run_twice(world_root: Path, steps: list, *, table: str, plant_spec: dict,
              audit_columns: list[str] | None = None) -> RunTwiceResult:
    ...

def rematerialise_matches(agent_digest: str, rebuilt_digest: str) -> RematerialiseResult:
    ...

def second_seed_hardcoded(rows_seed_b: list, expected_b: list, *,
                          rows_seed_a: list | None = None) -> SecondSeedResult:
    ...
```

`detail` is a `str` on every result. It names the counts or the mismatch. Tests assert the booleans, not the wording.

### `run_twice` order

`plant_spec` must contain `predicate` (SQL fragment, passed through to `key_count` unchanged) and `rows` (positive int). Other keys are whatever `_world.plant` requires (`mode`, `sql` or landing fields). The conftest `PLANT` spec is valid.

1. `copy_world(world_root, tmp)`.
2. `plant(copy, plant_spec)`. The plant runs before any count used in the booleans.
3. `n0 = key_count(table, plant_spec["predicate"])` on the copy. This is the post-plant, pre-run count.
4. `run_pipeline` once. This is the first post-plant run.
5. `n1 = key_count(...)`. `fp1 = business_fingerprint` over `business_columns(table, audit_columns)`.
6. `run_pipeline` again.
7. `n2 = key_count(...)`. `fp2 = business_fingerprint` over the same columns.

Definitions, all on the fact `table`, not on the landing table the plant writes:

- `business_identical` iff `fp1 == fp2`.
- `planted_once` iff `(n1 - n0) == int(plant_spec["rows"])` and `n2 == n1`.
- `liveness_failed` iff `n1 == n0` (the planted predicate count did not increase on the first post-plant run).

A wrong increase (`n1 != n0` but not by `rows`) is `liveness_failed` False and `planted_once` False. A dead loader is `liveness_failed` True. It can also be `business_identical` True, because both runs leave the business columns alone. A-T2 does not assert `business_identical`.

`n0` is after `plant`, not before. `docs/implementation_plan/hardness-round/findings/R-A1.md` shows why: `liveness.ingests` snapshots the count before `plant`, so a `WHERE 1=0` loader plus `plant.sql` that inserts into the counted table scores a full liveness pass. Sampling `n0` after that insert makes `n1 == n0` and `liveness_failed` True. Do not treat `fp1 == fp2` as a pass by itself.

A loader that ignores the landing table and inserts its own rows under the same predicate still moves `n1` by `rows`. The count does not close that hole. A1 does not add a value check this round; the finding records it.

Why `fp1`/`fp2` are taken after the runs, not before the first run: a correct merge changes the table on run 1 (the new delivery lands) and changes no business column on run 2. Equality of a pre-run fingerprint with a post-run fingerprint is False whenever the delivery lands, and A-T1 requires `business_identical` True on the merge loader. This is the same pair `liveness.py` compares (`fp1 == fp2` after run 1 and run 2). A pre-run fingerprint may be mentioned in `detail`. It is not an operand of `business_identical`.

### `rematerialise_matches`

`matches` iff `agent_digest == rebuilt_digest` and both are non-empty strings. Exact match, no case-fold, no strip. The caller supplies both digests. This detects a hand-`UPDATE` mart whose model was not fixed. Do not open a database, read a file, or shell out.

### `second_seed_hardcoded`

`hardcoded` is True iff `rows_seed_b == rows_seed_a` and `rows_seed_b != expected_b`. List equality, order-sensitive, no sort. Equal on both seeds when `expected_b` differs means the transform baked in seed A values. If seed B equals `expected_b`, `hardcoded` is False. If seed B differs from seed A, `hardcoded` is False even when seed B is also wrong (that is a different failure).

## Tests A-T1..A-T4

Use the `world` fixture and the loaders already in `tasks/contracts/tests/conftest.py` (`merge`, `retry`, `dead`, `snapshot`). Import constants the way `test_liveness.py` does: `from conftest import PIPELINE, PLANT`. Do not add a loader and do not edit `conftest.py`. `retry` and `snapshot` are available and are not required tests.

World layout the fixture already builds: `var/warehouse/warehouse.duckdb`, `workspace/pipelines/load_orders.sql`, table `warehouse.fct_orders`. `PLANT` is `mode=sql`, `rows=3`, `predicate="order_id >= 990000001"`, inserting into `wh.raw.orders_landing`. `PIPELINE["steps"]` is `["pipelines/load_orders.sql"]`. Pass `audit_columns=["load_ts"]`, the same exclusion `test_liveness.py` uses. Do not pre-plant; `run_twice` plants.

- **A-T1.** `world(loader="merge")`. `run_twice(root, PIPELINE["steps"], table="warehouse.fct_orders", plant_spec=PLANT, audit_columns=["load_ts"])`. Pass: `business_identical` and `planted_once` and not `liveness_failed`.
- **A-T2.** `world(loader="dead")` (the `WHERE 1=0` loader). Same arguments. Pass: `liveness_failed` is True and `planted_once` is False.
- **A-T3.** `rematerialise_matches("aa", "bb")` → `matches` False. `rematerialise_matches("ab", "ab")` → `matches` True. `rematerialise_matches("", "")` → `matches` False. `rematerialise_matches("", "ab")` → `matches` False.
- **A-T4.** `rows_seed_a == rows_seed_b == [("k", 1)]`, `expected_b == [("k", 2)]` → `hardcoded` True. `rows_seed_b == expected_b == [("k", 2)]` with `rows_seed_a == [("k", 1)]` → `hardcoded` False.

## A2 callers

`tasks/GT00*/tests/rebuild_check.py` (A2-early GT001–GT004, A2-late GT005–GT009) may import `tasks.contracts.rebuild` and that task's own test helpers. Each file exposes `check(world_root: Path) -> dict` of JSON booleans built from these three functions. It does not call `grade.py`, does not write the episode world, and is not imported by `grade.py` or `run_baselines.py`. A comment at the top records the task-local `table`, `steps`, and `plant_spec`. Executing `check` on a full GT fixture is not an in-scope unit test this round.
