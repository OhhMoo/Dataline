# B1 — de-hardcode the warehouse

**Agent:** B1-FINISH · **Date:** 2026-09-14 · **Scope:** `gateway/src/alkera_gateway/`,
`gateway/tests/`, `gateway/CONNECTOR_CONTRACT.md`.
Astra published the contract and built `PostgresWarehouse` + `create_warehouse`, then ran out of
budget with `sql.py` still importing `duckdb` and hardcoding `dialect="duckdb"`. The interface
existed and the original backend did not go through it. It does now.

```
before   28 collected   26 passed, 2 xfailed
after    36 collected   35 passed, 1 xfailed
```

`world/` and `tasks/` untouched. The running Track B compose stack was **not** rebuilt or
restarted (see §6).

---

## 1. What landed

| Deliverable | Where | State |
|---|---|---|
| `DuckDBWarehouse` implementing `WarehouseConnector` | `warehouse.py:32-109` | done |
| `sql.py` free of every DuckDB coupling | `sql.py` | done — `grep -n duckdb src/alkera_gateway/sql.py` returns nothing |
| Dialect comes from the connector | `warehouse.py`, `snowflake.py` | done |
| `warehouse_backend` config field, default duckdb | `config.py:12` | Astra's; now **asserted** |
| Identical action-log rows across backends | `tests/test_warehouse_backends.py` | done, measured |

**The aliased ATTACH is carried, not reimplemented.** `DuckDBWarehouse.connect` is Astra's
`sql.Warehouse.connect` moved verbatim: in-memory connection, `ATTACH '<path>' AS wh`,
`USE wh.warehouse`, external access off, config locked. The docstring now says *why* — at
`/var/warehouse/warehouse.duckdb` the catalog is also named `warehouse`, so every two-part
reference is ambiguous, which is what made GT001 unsolvable while every suite stayed green.

**The dialect is now a connector property.** `inspect_sql(sql, observation, read_only, dialect)`
takes the dialect explicitly with **no default** — there is no longer a string in `sql.py` that a
new backend could forget to override. `DuckDBWarehouse.dialect = "duckdb"`,
`PostgresWarehouse.dialect = "postgres"`, `SnowflakeWarehouse.dialect = "snowflake"`.

`CONNECTOR_CONTRACT.md` records this as **v1.1, additive**: `dialect: str` joins the Protocol.
Consumers that only call `execute` — which is A and C — are unaffected, so nothing A or C has
already integrated against moves.

## 2. The DuckDB path is unchanged, and the tests say so

The constraint was byte-identical behaviour, because Track C has rollouts in flight. The move
was mechanical: same connection config, same `result()` shape, same single-vs-multi statement
split, same rollback, same `Count`/`BIGINT` counting, same timer. All 16 of Astra's original
tests plus B3's 12 still pass untouched, and `test_action_log_rows[duckdb]` now pins the exact
§9.2.2 bytes so a future refactor cannot drift quietly.

## 3. Proof each backend was actually exercised

The monitor named the exact defect to avoid: *a factory, a config field, a registered tool, a
test, and a green suite, while every fixture still runs DuckDB.* Its proposed defence — one
parametrised id per backend — is adopted literally. There is no `if backend ==` anywhere in the
test bodies.

```
tests/test_warehouse_backends.py::test_sql_query[duckdb] PASSED
tests/test_warehouse_backends.py::test_sql_query[postgres] PASSED
tests/test_warehouse_backends.py::test_sql_execute[duckdb] PASSED
tests/test_warehouse_backends.py::test_sql_execute[postgres] PASSED
tests/test_warehouse_backends.py::test_action_log_rows[duckdb] PASSED
tests/test_warehouse_backends.py::test_action_log_rows[postgres] PASSED
tests/test_warehouse_backends.py::test_a_world_without_a_declared_backend_still_gets_duckdb PASSED
tests/test_warehouse_backends.py::test_create_warehouse_selects_the_declared_backend PASSED
8 passed in 8.52s
```

**Postgres is a real server, not a stub.** `postgres@sha256:f1c3376c…` — the same digest
`docker/compose.yml` pins — started per session on an ephemeral host port, seeded through the
non-superuser `episode` role exactly as `docker/postgres.sql` does, `DROP SCHEMA … CASCADE` and
reseeded before every test, removed in teardown. `ALKERA_TEST_POSTGRES_DSN` overrides it for a
machine that already has one.

**Negative control, run to confirm the skip is real rather than a fallback:**

```
$ ALKERA_TEST_POSTGRES_DSN=postgresql://nobody@127.0.0.1:1/none pytest tests/test_warehouse_backends.py
test_sql_query[duckdb] PASSED   test_sql_query[postgres] SKIPPED
test_sql_execute[duckdb] PASSED test_sql_execute[postgres] SKIPPED
test_action_log_rows[duckdb] PASSED  test_action_log_rows[postgres] SKIPPED
5 passed, 3 skipped
SKIPPED: ALKERA_TEST_POSTGRES_DSN is set but unreachable: connection timeout expired
```

With no Postgres the ids still appear, as skips that name the reason. They never quietly become
a second DuckDB run — which is the whole point of the shape.

### The action-log parity claim, measured

`test_action_log_rows[duckdb]` and `test_action_log_rows[postgres]` each assert against **the
same module-level `EXPECTED_ROWS` literal**, so equality between the backends follows from both
passing. Four calls — a decimal read, an UPDATE, a `count(*)`, an unbounded DELETE — produce
identical `records.operation_record` bytes on both engines, including:

- `DECIMAL(10,2)` and `INTEGER` column type names, identical from DuckDB's `description` and
  from Astra's `PG_TYPES`/`pg_type` OID mapping
- decimals as `{"type":"decimal","value":"10.50"}`, not floats, on both
- `{"name":"Count","type":"BIGINT"}` rows for DML on both
- `op_class: destructive`, `flags: ["delete","unbounded_delete"]`, `rows_affected: 2`

This is the first time that claim is checked by `pytest tests`. Astra had written the equivalent
check in `docker/acceptance_inside.py`, but it only runs inside the compose stack, so the test
count never moved and the claim was invisible to `scripts/track_status.py`.

## 4. A finding worth more than this task: Windows excluded port ranges

Standing up Postgres nearly failed for a reason that had nothing to do with Postgres, and it will
bite Tracks A and C the same way.

`docker run -p 127.0.0.1:55432:5432` **succeeded**, `HostConfig.PortBindings` was populated, and
`NetworkSettings.Ports` came back `{"5432/tcp": []}` — nothing published, no error, container
healthy. Six ports failed the same way before the cause showed up:

```
$ netsh int ipv4 show excludedportrange protocol=tcp
     55418       55517          <- every port tried was inside this
```

Hyper-V/WSL reserves large TCP blocks on this host. Anything binding a *fixed* port in the 50000s
gets a silent no-op. A fixture written the obvious way would have skipped Postgres forever with a
plausible "server never came up" message, and the Postgres id would have sat there yellow while
looking like an environment quirk. The fixture therefore binds `127.0.0.1::5432` — port 0, the OS
picks — and reads the assignment back with `docker port`. Recommended for any other track
publishing a container port on this machine.

## 5. `docs_search` — B3's xfail, fixed (out of original scope)

Handed over by the coordinator mid-task. `Workspace.search` swallowed `ToolError`/`UnicodeError`
with a bare `continue` and still returned `limit_reached: false` — an affirmative claim of a
complete search while a cp1252 document and an oversized document, **both containing the query**,
vanished. Same failure class as the `fs_read` encoding fix, one method below its own docstring.

- `Search` gains `encoding`, symmetric with `fs_read`; a bad codec fails the call with
  `unknown_encoding` rather than skipping every document
- every unreadable document is named in a new `skipped` list with its `code` and the diagnostic
  reason (`decode_error` carries the byte and offset; `file_too_large` says so)
- `limit_reached: false` is now only a claim about the limit

`test_docs_search_does_not_silently_drop_unreadable_documents` flipped from `xfail(strict)` to
passing; the marker is removed. I extended the test rather than weakening it: it now also asserts
the `skipped` reasons are diagnostic **and** that `{"encoding": "cp1252"}` actually recovers the
document — naming a file the agent still cannot reach would be half a fix.

The other xfail, `test_bi_list_blames_the_world_not_the_agents_arguments`, is **left red on
purpose**. It was not assigned to me and it is a real defect: a junk `.yml` beside the dashboards
makes `bi_list` return `invalid_arguments`, blaming the agent for a broken world.

## 6. What is not verified

- **The compose stack still runs pre-refactor code.** `alkera-track-b-episode-1` has been up two
  hours from an image built before this change. I did not rebuild: the brief is explicit that
  changing gateway behaviour mid-rollout corrupts a measurement in flight. So
  `docker/acceptance_inside.py` — which is the only coverage for BI, docs and S3 **on Postgres** —
  has not been re-run against this code. Someone must rebuild and re-run it once Track C's
  rollouts are quiet. Nothing I changed should affect it; that is a prediction, not a measurement.
- **Cross-track coupling, unchanged and still live.** `core.py:23` holds a guarded
  `from world.fingerprint import semantic_hash`. A rename in Track A breaks Track B at runtime
  and **neither suite goes red** — the gateway tests always pass `initial_world_fp` explicitly, so
  the import never executes under test. Untouched, and still exactly as fragile as it was.
- **Residual DuckDB coupling outside `sql.py`.** `core.py:6` still imports `duckdb` and line 111
  catches `duckdb.Error` in the generic handler. A Postgres-only deployment therefore still
  requires the duckdb package installed. I left it deliberately: that `except` clause decides the
  logged `error.code` for every uncaught driver error, and changing it changes log rows — the one
  thing this task was told not to do.

## 7. The single biggest weakness in my own output

**Postgres is proven for four SQL statements and nothing else.**

DuckDB is exercised by 30+ tests across the whole toolbelt — dbt, the scheduler, BI rendering and
watermarks, git, docs, external-access denial, path denial, replay, trace validation. Postgres is
exercised by **three test ids over four statement shapes**. Every surface that reaches the
warehouse *through* another component — `render_dashboard`, `BIStore.refresh`, the watermark
queries, `sched_trigger` → dbt — runs only on DuckDB in `pytest tests`. Those paths call
`warehouse.execute` with SQL the world author wrote, which is exactly where an engine difference
would surface, and I have no pytest evidence about any of them.

The honest statement of what B1 now supports is: *a world can declare a Postgres backend, and the
`sql_query`/`sql_execute` tools serve it with byte-identical log rows.* Not: *the gateway works on
Postgres.* `docker/acceptance_inside.py` does cover BI-on-Postgres, but only inside the stack, and
per §6 it has not been re-run since the refactor — so today that coverage is a claim from a
previous build, not a result from this one.

Second, smaller, and in the same direction: `EXPECTED_ROWS` contains only **successful** calls. I
excluded error rows because the contract permits engine-specific error text — but errors are
precisely where engines diverge, so the `exit: "error"` row shape across backends is the part of
§9.2.2 that remains unchecked. A Postgres failure logs
`warehouse_error`; the same failure on DuckDB logs `CatalogException`. That difference is legal
under the contract and currently untested in either direction.

---

**Files:** `gateway/src/alkera_gateway/warehouse.py` (+78, `DuckDBWarehouse`),
`sql.py` (−74, driver removed), `snowflake.py`, `files.py`, `operations.py`, `tools.py`,
`gateway/tests/test_warehouse_backends.py` (new, 329 lines),
`gateway/tests/test_surface_contents.py` (xfail removed, test extended),
`gateway/CONNECTOR_CONTRACT.md` (v1.1 additive note).
`commands.py`'s `stdin=subprocess.DEVNULL` verified still present and untouched.
