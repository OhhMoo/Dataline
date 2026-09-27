# Native mode — measurements of 2026-09-16

Moved verbatim from `gateway/NATIVE.md`, which now carries only the spec. The commands,
numbers and suite counts below are the record of the day native mode was integrated;
later measurements live in `gateway/docker/NATIVE_STACK.md` and
`tasks/harness/NATIVE_CONTAINER.md`.

## Measured — 2026-09-16, the stack integrated

Host: Windows 11, docker 29.5.2, `gateway/.venv` 3.11.15.

- **The image carries the `native` extra; P6 is real.** `requirements-native.lock` (110
  packages, `--require-hashes`, from the one `uv.lock`); inside the image `services.load()`
  → 9 loaded. `containment.py --native` → `evidence/containment-native.json` **passes**:
  each `/health` 200 with a real count (jira 8, slack 3, confluence 9, metabase 1,
  prometheus 10, alertmanager 5, loki 30, airbyte 2, deploy 1); seven control names fail to
  resolve, 18 port probes refused, no published host port, positive control positive.
  A world that seeds no ingest plane makes `airbyte` 503 and P6 fail on it — P6 needs a
  world seeding all nine planes.
- **Postgres logging, role-scoped.** Server `log_statement=none`; `ALTER ROLE alkera SET
  log_statement='all'` + `log_min_duration_statement=0`. Measured: 24 log lines holding
  exactly the 2 `alkera` statements. Both settings must move together.
- **MinIO's audit receiver** is `tailers.minio.build_app()` mounted by `serve.py` on
  `native-services.internal:8110`, inside the process that writes `actions.jsonl` (one
  writer for the `seq` lock); absent from `destinations.jsonl` on purpose — MinIO dials it,
  the episode never does.
- **The services carry the world's clock.** `serve.py` exports `ALKERA_GATEWAY_CONFIG`,
  `ALKERA_NATIVE_AUDIT`, `ALKERA_SCHEDULER` from `--world`; the P6 run's first row carries
  the world's `vnow`, not `GatewayConfig().virtual_time`. Without it all nine answered 503.
- **`<root>/home` is fingerprinted**: default seed still `85ce4cbb…`; a `native: true`
  build `252d5a5c…`; one appended profiles line moves it.
- **Phase 3 acceptance**: `run_rollouts.py --task GT001 --k 1 --driver native --model
  scripted`. `native_stack.py` brought up nine services + aggregate + receiver on loopback
  with shims on PATH; the subject ran as a real bash (`duckdb` through the shim, `curl
  $JIRA_URL/…`, `sed -i`); 12 rows, each the 23 keys — 10 harness (`<service>.health`,
  `workspace.edit`) and 2 the subject's (`duckdb.statement` read of
  `warehouse.warehouse.fct_orders`; `jira.issues_list`, `exit: error` because GT001 seeds
  no human plane — the gate records it dead, correctly).
- `deploy_probe.py` suffix-matches the tool (`deploy.deploy_run` visible to GT007's grader,
  TASK VALID). No `duckdb` CLI ships on the host or in the image, so the shim runs
  statements through the `duckdb` module — same libduckdb, same file; rows say
  `duckdb_library`.

## Measured — 2026-09-16, NATIVE-FIXES

1. **A DuckDB write is attributable.** `blindness._unattributed` reads the MCP name off the
   tool SUFFIX (`sched.sched_trigger` IS `sched_trigger`), and a `duckdb.statement` /
   `shim.dbt` / `shim.python` WRITE naming a declared warehouse file (`args.database` or a
   target) explains that file and no other. `declared_warehouses` = manifest `warehouse` +
   `warehouses` specs + every `var/warehouse/*.duckdb` (the rule `fingerprint.py` hashes
   by). Measured on real bytes: a `DELETE` row naming the file against a changed
   `warehouse.duckdb` is clean; the same change with only a `jira.issues_list` row is hard.
2. **The native warehouse check is DuckDB; dbt is its own surface.** `surface_audit` opens
   the world's file through `ATTACH … AS wh (READ_ONLY)` (no write lock, no catalog
   collision); Postgres remains the check for `warehouse_backend: postgres`; a probe with
   `warehouse = "legacy"` asks about that file. The content rule binds only where the task
   declares `[harness].warehouse_schemas` — GT008's primary holds no table at t0 (building
   it IS the task) while the MCP probe is `SELECT 1`; a stricter native gate would make one
   `task.toml` two tasks. `dbt` is live iff it resolves on PATH AND a profiles output
   resolves to this world's file; `native_stack` puts the interpreter's scripts dir behind
   the shims (else `dbt` exits 127). A 503 carrying a health document reads as **dead**.
3. **`./ops/sched`** (above) materialised by `native_stack.install_workspace_cli` BEFORE
   `fingerprint` and `tree_manifest`. Swapping the acceptance subject's `advance 1` for a
   real `trigger` found `build/sched/logs/run-*.log` — the raw engine log `executor.py`
   keeps outside the fingerprint but inside the diff — now covered by the `sched_trigger`
   branch alongside `var/obs`. `native/context.py` builds `Operations` with
   `scheduling.episode_scheduler(config)` (same as `cli serve`), so services and CLI agree
   on `WorldScheduler`; `native_stack.venv_env` puts `world/` on the services'
   `PYTHONPATH` (without it all nine answered 500).
4. **Acceptance**, scripted subject (`./ops/sched list`, duckdb CREATE/DELETE/DROP,
   `./ops/sched trigger <first job>`, a jira request, `sed -i`):

   | task | gate | required live | dead | audit clean | blindness |
   |---|---|---|---|---|---|
   | GT004 | pass | 16/16 | — | yes | clean |
   | GT006 | pass | 17/17 | — | yes | clean |
   | GT008 | pass | 16/16 | `~airbyte`, declared `dead_by_design` | yes | clean |

   The three statements really change the warehouse file (sha `577b96b8…` →
   `6c177622…`), so the runs are clean because the attribution rule holds.
5. **Trigger-all smoke** (`ALKERA_TRIGGER_ALL=1`): every job every world declares, in
   declaration order — GT001 `daily_load, dbt_build`; GT004 `build_marts,
   load_orders_delta, refresh_bi_extract`; GT006 `build_payments, land_payments,
   refresh_bi_extract`; GT008 `legacy_nightly, nova_nightly` — ten triggers over a dbt
   build, a sql loader, a noop and two script nightlies; **no allowlist addition needed**,
   blindness clean on all four. GT001 reports `gate: not_declared` (no
   `required_surfaces` in its task.toml, under both drivers) and `dbt` dead with
   `no_dbt_profile` — recorded, not fixed.
6. Suites at the time: `selftest.py` 147/147, `test_native_driver.py` 122/122,
   `test_native_stack.py` 37/37, `test_native_context.py` 19, gateway 321 passed.
