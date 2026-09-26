# Hardness round — exclusive write sets

Same-directory agents do not edit each other's files. A path not listed for an agent is not theirs, including a file that sits next to one that is. Do not create files outside the set. Do not git commit.

Blind red-team files live under `docs/plans/hardness-round/findings/`. SPEC does not write there. R-A1 and R-A2 do not read each other's file and do not edit product code.

| agent | exclusive write set |
|---|---|
| SPEC | `docs/plans/hardness-round/INVARIANTS.md`, `OWNERSHIP.md`, `A1_API.md`, `B1_INTERFACE.md`, `C_HARDNESS_FLAGS.md` |
| A1 | `tasks/contracts/rebuild.py`, `tasks/contracts/tests/test_rebuild.py` only. Do not edit `__init__.py` or `conftest.py`. Do not register `rebuild` in `REGISTRY`. |
| B1 | `tasks/harness/redteam/exploit_agent/__init__.py`, `prompt.md`, `harness.py`, `README.md`, `test_harness.py` |
| C-ASK | `gateway/src/alkera_gateway/ask_stakeholder.py`, `gateway/tests/test_ask_stakeholder.py` only. Do not edit `tools.py`. |
| A2-early | `tasks/GT001-*/tests/rebuild_check.py`, `tasks/GT002-*/tests/rebuild_check.py`, `tasks/GT003-*/tests/rebuild_check.py`, `tasks/GT004-*/tests/rebuild_check.py` only |
| A2-late | `tasks/GT005-*/tests/rebuild_check.py`, `tasks/GT006-*/tests/rebuild_check.py`, `tasks/GT007-*/tests/rebuild_check.py`, `tasks/GT008-*/tests/rebuild_check.py`, `tasks/GT009-*/tests/rebuild_check.py` only |
| B2 | `tasks/harness/redteam/exploit_loop.py`, `tasks/harness/redteam/test_exploit_loop.py` only |
| R-A1 | `docs/plans/hardness-round/findings/R-A1.md` only |
| R-A2 | `docs/plans/hardness-round/findings/R-A2.md` only |
| C-HARD | `tasks/GT004-doubling-window/hardness/*`, `tasks/GT009-settlement-cutoff/hardness/*` only |

Directory names in full:

- `tasks/GT001-aov-halving/tests/rebuild_check.py`
- `tasks/GT002-orders-flatline/tests/rebuild_check.py`
- `tasks/GT003-tuesday-figure/tests/rebuild_check.py`
- `tasks/GT004-doubling-window/tests/rebuild_check.py`
- `tasks/GT005-assertion-never-ran/tests/rebuild_check.py`
- `tasks/GT006-quality-gate-drops-rows/tests/rebuild_check.py`
- `tasks/GT007-ran-long-or-started-late/tests/rebuild_check.py`
- `tasks/GT008-warehouse-cutover/tests/rebuild_check.py`
- `tasks/GT009-settlement-cutoff/tests/rebuild_check.py`

A2 modules call `tasks.contracts.rebuild` and are not imported by that task's `tests/grade.py` or `tests/run_baselines.py`. They do not mutate the episode world. Task-local `table`, `steps`, and `plant_spec` are chosen in a comment at the top of `rebuild_check.py`. This round does not require those functions to be executed against the full fixture (that run is not the 428-row ladder). The in-scope gate for A2 is A-T5: `grade.py` does not import them.

Nobody except the owner of a row writes that row's files. SPEC does not implement the APIs it specifies.
