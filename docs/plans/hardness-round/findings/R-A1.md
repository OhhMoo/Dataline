# R-A1 — permanent skip vs liveness

Hypothesis: an idempotency check can be satisfied by never loading data (permanent skip, `WHERE 1=0`, watermark pinned in the past).

Verdict: **BLOCK** a side-module `rebuild_check` that only compares fingerprints of an empty delta. It is **CLEAR** only when a zero increase in planted rows sets `liveness_failed` (the gate `liveness.ingests` already is). The empty-delta shortcut does not collect `liveness.idempotent` today. A counted-row absolute equality still does, for two inputs below.

`pytest tasks/contracts/tests/test_liveness.py -q` → `6 passed in 0.54s`.

## 1. The existing contract kills the named shortcut

Clause **`liveness.ingests`** (`tasks/contracts/liveness.py`). It is fatal. The fold in `tasks/contracts/_score.py` turns any failed fatal clause into contract points `0.0` and pays nothing for the other clauses (`score_clauses`, the `fatal_fail` branch).

The ingestion bit is absolute equality with the planted row count, not a fingerprint:

```115:120:tasks/contracts/liveness.py
    live_ok = after1 == rows
    if after1 == 0:
        live_detail = (f"the planted delivery ({rows} row(s), never seen by this "
                       f"pipeline) landed 0 rows - idempotent by never loading "
                       f"anything, which repairs nothing"
```

```156:159:tasks/contracts/liveness.py
    return [
        Clause.make(f"{NAME}.ingests", live_ok,
                    f"{live_detail} [{planted}]",
                    weight=0.45, fatal=True, checkpoint="run_1"),
```

`liveness.idempotent` does not pass on that input either. Its success condition requires the planted count after run 2, not fingerprint stability alone:

```137:140:tasks/contracts/liveness.py
    idem_ok = (after2 == rows and fp1 == fp2 and not window_moved_2)
    if after2 != rows and after1 == 0:
        idem_detail = (f"nothing loaded on either run ({after1} then {after2} "
                       f"row(s)) - vacuously stable")
```

`passed` is false on that branch. The words "vacuously stable" are the failure detail. `liveness.no_collateral` does pass (the protected window does not move), and it is fatal, so it gates without paying. It cannot rescue the score once `liveness.ingests` has failed.

DOC names this shortcut at `tasks/contracts/liveness.py` lines 19–22. GT004 `DESIGN.md` lines 75–78 require the paired liveness assertion so a permanent skip scores zero. The same design's shortcut battery (`DESIGN.md` lines 165–166) already includes `permanent_skip`. GT001's grader header states the same rule as a comment, not as a live probe (`grade()` never calls `idempotency_probe`):

```28:29:tasks/GT001-aov-halving/tests/grade.py
  F121  the idempotency probe plants a NEW partition, so a permanently-skipping
        loader scores zero instead of full marks
```

The unit that locks `WHERE 1=0` is `test_shortcut_permanent_skip_zeroes_the_contract` (`tasks/contracts/tests/test_liveness.py` lines 49–59): loader `dead` (`conftest.py` lines 52–58), `liveness.ingests` failed and fatal, detail contains "repairs nothing", `liveness.idempotent.passed is False`, `points == 0.0`.

Same observable, same clause, for a watermark that cannot see the plant. Canonical plant is `order_dt = 2026-03-08` into `wh.raw.orders_landing` (`conftest.py` `PLANT`, lines 159–164). Replacing the loader with `l.order_dt <= DATE '2026-03-05'` or `l.order_dt > DATE '2099-01-01'` both scored `points 0.0`, `liveness.ingests` false, `liveness.idempotent` false. There is no separate watermark test; the count gate is what kills them.

## 2. Residual hole

The named never-load inputs are not marked idempotent. Two other inputs are, and both repair nothing about the delivery. Both scored `points 0.35 / max 0.35` with `liveness.ingests` and `liveness.idempotent` passed. The contract snapshots `key_count` only before `plant` (`liveness.py` lines 75–76) and again after `run_pipeline` (lines 90–93). It never records the count after the plant and before the run, and it never checks that the business values are the planted ones (`key_count` is `COUNT(*)` under the predicate, `_world.py` lines 165–172). `live_ok = (after1 == rows)` is therefore true whenever that many predicate rows exist after run 1, including rows the pipeline did not read.

1. **`WHERE 1=0` plus `plant.sql` into the counted table.** Same `dead` loader as the lock test. Plant SQL is `INSERT INTO wh.warehouse.fct_orders ...` with `{id}` / `{date}` instead of `INSERT INTO wh.raw.orders_landing`. `before_planted` is 0, so the isolation check passes. The insert happens inside `plant` (`_world.py` `plant`, mode `sql`, lines 369–380), which is the default mode. Run 1 loads nothing. `after1 == 3`. The contract reports "the planted delivery landed exactly 3 row(s) on run 1" and the second run is idempotent. This is the hypothesis, satisfied, because the grader wrote the rows the clause counts.

2. **Predicate stub, canonical plant left on the landing table.** Loader never references `orders_landing`. It inserts `order_id` 990000001–990000003 (the predicate `order_id >= 990000001`, `rows == 3`) with amount `0.01`. The plant wrote amount `12.50` into landing and those rows stay there. Run 2 is a no-op via `NOT EXISTS`. Full marks. The delivery was not loaded.

A fingerprint-only `rebuild_check` is a third acceptance path for the inputs section 1 already rejects. On `dead` and on both watermark pins, run 1 and run 2 do not change a business column, so `fp1 == fp2` and the protected window is unchanged. The sole failing conjunct in `idem_ok` is `after2 == rows`. Drop that conjunct and the empty delta is idempotent.

## 3. Falsifier

Grader test, meant to be pasted into `tasks/contracts/tests/test_liveness.py`. It fails on the current contract (both cases scored full marks when evaluated against this tree).

```python
def test_zero_delta_and_predicate_stub_are_not_idempotent(world):
    """Permanent skip must not pass because the plant wrote the counted
    table, or because the loader minted predicate rows and never read
    the delivery. Fingerprint equality of an empty delta is not a pass.
    """
    into_fact = dict(PLANT)
    into_fact["sql"] = (
        "INSERT INTO wh.warehouse.fct_orders "
        "(order_id, line_nbr, order_dt, amount, load_ts) VALUES "
        "({id}, 1, DATE '{date}', {amount}, TIMESTAMP '2026-03-09 05:00:00')"
    )
    root = world(loader="dead")
    (root / "workspace" / "pipelines" / "load_orders.sql").write_text(
        "INSERT INTO wh.warehouse.fct_orders "
        "(order_id, line_nbr, order_dt, amount, load_ts)\n"
        "SELECT 990000000 + i, 1, DATE '2026-03-08', 0.01, "
        "TIMESTAMP '2026-03-09 05:00:00'\n"
        "FROM range(1, 4) t(i)\n"
        "WHERE NOT EXISTS (SELECT 1 FROM wh.warehouse.fct_orders "
        "WHERE order_id >= 990000001);\n",
        encoding="utf-8", newline="",
    )
    for label, kwargs in (
        ("plant_writes_counted_table", {"plant": into_fact}),
        ("predicate_stub_skips_landing", {}),
    ):
        result = _run(root if label.startswith("predicate") else world(loader="dead"),
                      **kwargs)
        clauses = _by_id(result)
        assert clauses["liveness.ingests"].passed is False, label
        assert clauses["liveness.ingests"].fatal, label
        assert clauses["liveness.idempotent"].passed is False, label
        assert result.points == 0.0, (label, result.reason)
```

The fixer this is aimed at: count the predicate after `plant` and before run 1; `liveness.ingests` fails when that count does not increase by `plant.rows` on the run. Requiring `after1 == rows` with a pre-plant baseline of 0 does not do that.

## 4. Verdict

**BLOCK** `rebuild_check` if it only compares fingerprints of an empty delta. That comparison is true for `WHERE 1=0` and for a watermark pinned before the plant date, and those loaders repair nothing.

**CLEAR** only if the module sets `liveness_failed` when planted rows do not increase across the pipeline run (post-plant count versus post-run count). `liveness.ingests` already does the weaker form of that for a landing plant — `after1 == 0` is fatal — and `test_shortcut_permanent_skip_zeroes_the_contract` locks it. The module must not replace that gate with `fp1 == fp2`.
