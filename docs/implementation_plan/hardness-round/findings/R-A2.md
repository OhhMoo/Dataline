# R-A2 — hand-patch and copy-expected vs table equality

Hypothesis: an agent can pass a table-equality grader by `UPDATE`/`INSERT` on the mart (or by copying an expected table) while leaving the dbt model or loader broken, so a rebuild from raw would diverge.

Searches actually run:

- `rg -n "rematerial" tasks --glob 'grade.py'` — no matches
- `rg -n "second.seed" tasks --glob 'grade.py'` — no matches

Nine official graders exist (`tasks/GT001`–`GT009` `tests/grade.py`). None of them rebuilds the agent's pipeline from raw and diffs that result against the table the agent left.

Nearest neighbours, and why they are not that check:

- `tasks/GT001-aov-halving/tests/grade.py` `d1_window_diff` fingerprints `w.warehouse.fct_orders` against `e.warehouse.fct_orders` for `2026-02-01`..`2026-03-02`. The world file is the mart at teardown.
- `tasks/GT006-quality-gate-drops-rows/tests/grade.py` `a2_model` renders the agent's model and runs that SELECT against the agent's current warehouse (`ATTACH` of the graded db as `wh`). Upstream rows the agent already patched are the inputs. The comment in that function names the hand-patched-table case; the implementation does not drop the mart and reload it from the seeded raw layer.
- `tasks/contracts/liveness.py` copies the graded world, plants a delivery the pipeline has never seen, and runs the pipeline twice. The historical mart is the one the agent left. `no_collateral` treats a window that does not move as success. A hand-patched window that the loader no longer touches therefore survives.
- `tasks/GT004-doubling-window/tests/probes/idempotency.py` is that contract, wired as G2. It is a re-run on the patched copy, not a from-raw rebuild.
- `tasks/GT003-tuesday-figure/tests/grade.py` D6 executes the agent's rebuild script on a copy to test a half-published mart. It does not digest a from-raw rebuild against the leftover table.

`docs/research/LLM_Judge_Research.md` (memo, §3 Borrow and §9 step 2) names clean-state re-materialisation and second-seed grading as borrowings. No `grade.py` implements either.

## 1. Does any official grader rebuild from raw and diff?

No current `grade.py` does this.

## 2. Hand-patch path

What the agent writes, on GT001: an in-place mutation of `warehouse.fct_orders`, and nothing in `dbt_project/models/warehouse/fct_orders.sql`.

The shipped model is `materialized='incremental'`, `incremental_strategy='append'`, `SELECT` from `stg_orders`, and when incremental `order_dt >= max(order_dt)` (`tasks/GT001-aov-halving/tests/fixtures/workspace/dbt_project/models/warehouse/fct_orders.sql`). `SEED_SPEC.md` §5 puts the defect in the fact: each window row appears twice, the second copy carrying `batch_id = 'retry-20260303-0412'`. The bug to fix is the loader, not a wrong `SELECT`.

`tasks/GT001-aov-halving/tests/run_baselines.py` already performs that mutation. `DEDUPE_KEEP_ORIGINAL` deletes extra `rowid`s partitioned by `(order_id, line_nbr)`. `p_acted_unresolved` calls it and leaves the model on append. `task.toml` declares that agent `acted_unresolved`, not `resolved`. `resolved` on the control arm requires `D7` (`task.toml`). `d7_loader_fixed` is a regex on `incremental_strategy` and `unique_key`. It does not run the model. Append scores 0 on D7, so the outcome stays off `resolved`. D1–D3 still read the leftover mart, so the table-equality checks pass.

What a rebuilt model produces depends on which rebuild:

- Clean full materialization (empty target, `is_incremental()` false): the `SELECT` from `stg_orders` runs once. That is the same grain as the deduped mart. Digests match. A helper that does only this returns true for the hand-patch.
- The pipeline's real mode: incremental append against a target that already holds the window. `order_dt >= max(order_dt)` re-appends. A second run diverges. That is the liveness docstring's permanent-skip / append hole (`tasks/contracts/liveness.py` `DOC`), and GT001's `grade()` never calls it.

Falsifier, fail closed: scripted baseline `hand_patch_mart` performs the same `DELETE` (or an equivalent `INSERT` of the corrected window) and does not change the model file. Declared class is not `resolved`. It must stay off `resolved` even if D1 is perfect. A check that returns match on this baseline has failed open. Today the close is D7's static read, not a rebuild diff.

## 3. Copy-expected path

F121 in `tasks/GT001-aov-halving/tests/grade.py`: the reference lives in the grader, never in the agent's catalog. `connect()` does `ATTACH` of the world as `w (READ_ONLY)` and `ATTACH` of `grading/expected.duckdb` as `e (READ_ONLY)`. D1 and D4 read `e`. The same harness-side attach is how GT002–GT006 bind `expected.duckdb`.

The episode service does not see that file. `world/docker/docker-compose.yml` mounts `../grading:/grading:ro` only on the `grading` service (`profiles: ["grading"]`, `network_mode: none`). The `episode` service mounts `worlddata` and `auditdata` only. `world/README.md` acceptance item 7: `/grading` is absent from episode and harness during the episode and is brought up post-episode with `NetworkMode: none`. `world/grading/README.md`: the zone is mounted read-only into the grading container and never runs during the episode.

`tasks/harness/reference_copy_check.py` is the copy path, already scripted: from inside the container it `ATTACH`es `/grading/expected.duckdb` and `DELETE`/`INSERT`s `wh.warehouse.fct_orders` from `ref.warehouse.fct_orders`, and falls back to a no-op when the attach raises. The module states that fixture runs score this baseline high on purpose because the grading directory sits beside the world there (GT001 17.0/19). That score is the grader reporting a reachable answer key. It is not a measurement topology.

Native+host is already documented as not `measurement_valid`. Root `README.md`: native+host is not `measurement_valid` because bash can see the rollout dir and `var/workflow` authoring notes. `tasks/harness/README.md` topology table: under `host`, a shell is one `..` from `/grading` and ground rule 1 is gone. Measurement substrate is `--driver native --topology container`.

`var/ledger` unreachability (`world/README.md`, Publication ledger) is a different seal: empty read-only tmpfs at `/world/var/ledger` inside the episode, gateway `path_denied` on escaping paths, writer on the host. It records what a publication contained. It does not rebuild a mart, and it is not the grading zone.

A successful copy of `expected.duckdb` onto `fct_orders`, with this model left on append, matches a clean full refresh of that model for the same reason the hand-dedupe does: the `SELECT` is the reference grain. The container not mounting `/grading` is what makes the copy fail. This finding does not propose mounting or opening the grading zone.

## 4. Verdict for `rematerialise_matches(agent_digest, rebuilt_digest)`

**BLOCK** as a sufficient closed check on this hypothesis.

**CLEAR** only if the caller computes `rebuilt_digest` by re-running the agent's transformations from the seeded raw layer, and `agent_digest` is the digest of the table the agent left. If `rebuilt_digest` is a hash of that same leftover table, the helper is tautological: hand-patch and copy-expected both return true, and the function has not looked at the model.

A from-raw full refresh is not enough for GT001. The unchanged append model, run once onto an empty target, reproduces `hand_patch_mart`. That call is a false clear, not a proof the loader is fixed. The rebuild that can fail this baseline closed is a re-execution of the model the agent left, in the incremental mode the nightly job uses, from the seeded raw layer, compared with the mart the agent left. Hashing the leftover table for both arguments does not do that.

## 5. Proposed baseline

- id: `hand_patch_mart`
- declared class: not `resolved`
- body: in-place `DELETE`/`INSERT` on `warehouse.fct_orders` so the window matches the reference grain; `fct_orders.sql` left on `incremental_strategy='append'` with no `unique_key`
- fail closed: outcome stays off `resolved`. A `rematerialise_matches` result of true on this baseline means the caller did not re-run the broken loader.
