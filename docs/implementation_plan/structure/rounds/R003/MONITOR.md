# MONITOR — a detector for silent partial completion, and the R003 baseline

**Round R003 · 2026-09-14 · agent MONITOR · owns `scripts/` · measured against `3_track.md` §3–§4**

The failure this exists to catch: a track runs out of budget partway, every suite stays green,
and the gap is only found when someone happens to run the three tracks together. The deliverable
is therefore **a script the lead agent can re-run cheaply**, not a one-time look.

- `scripts/track_status.py` — stdlib only, no install, like `gates/run_gates.py`
- `scripts/track_status_baseline.json` — the saved baseline to diff against

---

## 0. The headline, up front

**The brief's premise was already false when I first ran.** I was told A and B "are being briefed
to start work" and that there would be little incompleteness to find. In fact:

| Track | State when I started scanning (02:20Z) | State 25 minutes later (02:45Z) |
|---|---|---|
| **A — Kimi** | 8/8 shape constants in `build.py`, 0 themes | A1 landed, **4 themes**, 14 tests, **1 failing** |
| **B — Astra** | nothing | `CONNECTOR_CONTRACT.md` published, **zero implementation** |
| **C — Claude** | — | 27 untracked files under `tasks/`, a whole rollout harness |

Two things the detector caught **live**, which is the entire point of building it:

1. **`MainframeBatchThemeTest::test_semantic_fingerprint_stable` was RED at 02:45Z.** The C1
   mainframe theme did not produce an identical semantic fingerprint across two from-scratch
   builds — a direct miss against A2's acceptance criterion, not a style nit. Track A's suite went
   `7 passed` → `13 passed, 1 failed`.
   **Follow-up, 02:53Z:** Kimi fixed it. `python scripts/track_status.py --baseline …` now prints
   `A1 in_progress -> done` and `A tests: 13 passed/FAIL -> 19 passed/pass`, exit 0. The baseline
   JSON deliberately keeps the red state — a baseline you can only move forwards from is the
   useful kind.
2. **19 files changed *while the scan was running*.** That is the "the corpus moved under me"
   report reproduced mechanically, with filenames.

Neither was findable by looking once.

---

## 1. How to run it

```bash
python scripts/track_status.py                    # human readable, ~40-90 s (runs both suites)
python scripts/track_status.py --no-tests         # ~1 s, structure only; test status = unknown
python scripts/track_status.py --json             # machine readable, every field
python scripts/track_status.py --all              # every finding, uncapped
python scripts/track_status.py --baseline scripts/track_status_baseline.json      # diff
python scripts/track_status.py --save-baseline scripts/track_status_baseline.json # re-baseline
```

Exit `0` = no failing suite, no blocker, no regression. Exit `1` = a suite fails, a blocker
appeared, or something went backwards against the baseline. Suitable for a watch loop.

It runs pytest with `-p no:cacheprovider` so it **never writes into `world/` or `gateway/`.**

---

## 2. The fixed reference point: round start, `HEAD` = `bbfd237`

Read straight out of git, so it does not drift while agents work. This, not the live snapshot
below, is the authoritative "before".

| Fact | At `bbfd237` |
|---|---|
| `build.py` shape constants | **8 of 8** present (`DBT_PROJECT_YML` … `DOC_RUNBOOK`) |
| Declarative theme specs | **0** — `world/world/themes/` does not exist |
| World tests | **7 passed** (measured 02:22Z against this tree) |
| `sql.py` duckdb couplings | **5**, at lines **3, 15, 50, 78, 119** — exactly as `3_track.md` claims |
| Connector interface | **absent**; no `CONNECTOR_CONTRACT.md` |
| Gateway tool count | **17** |
| Episode-zone services | **1** (`episode`); `harness` and `grading` are other zones, behind profiles |
| `egress_test.py` invocations | **1** (`episode` only) |
| Docs / BI / S3 surfaces | `docs_search` + `bi_render` only; **0 of 10** new tools |
| fakesnow / Snowflake in code | **none** |
| Gateway tests | **12 passed** |

---

## 3. Baseline snapshot — `scripts/track_status_baseline.json`, captured 02:45:08Z

**Caveat, stated plainly: this snapshot was taken while Track A was actively writing.** Four
files changed during the run. It is a truthful sample, not a quiescent state. Diff against it
anyway — that is what it is for — but read §2 as the fixed reference.

### Track A — Kimi, `world/`

| ID | Status | Evidence | What the script saw |
|---|---|---|---|
| **A1** | `in_progress` | reference | 0/8 constants left; builder consumes `seed['theme']` + a theme module. Was `done` at 02:43 when the suite was green; dropped when the suite went red. |
| **A2** | `in_progress` | reference | **4 theme dirs**, 3 structurally complete. C1, D1, D2 all present. `excel_close` missing `workspace/`. |
| **A3** | `in_progress` | reference | 3 `/grading` probe lines now in `acceptance.sh` (`$COMPOSE exec -T episode test ! -e /grading`). **No run artifact anywhere** — an unrun gate is not a demonstration. |
| **A4a** | `not_started` | existence | gate 1 mid-rewrite; no `ssh`/CI/cross-host marker; no `.github/workflows`. |
| **A4b** | `unknown` | existence | only `episode` mounts `worlddata` writable, which is by design. This check is the **tripwire for B2** — it turns into a hazard the moment a second service mounts the fingerprinted tree. |
| tests | **FAIL** | executed | **13 passed / 1 failed** in 66 s; 14 test defs on disk (baseline 7). |

**The failure, in full:**
```
FAILED tests/test_theme_mainframe_batch.py::MainframeBatchThemeTest::test_semantic_fingerprint_stable
AssertionError: {'war[451 chars]e': '7c35ea7b...'} != {'war[451 chars]e': '9a97b6fc...'}
```

### Track B — Astra, `gateway/`

| ID | Status | Evidence | What the script saw |
|---|---|---|---|
| **B1** | `in_progress` | existence | 5 duckdb couplings still at **3, 15, 50, 78, 119**. `CONNECTOR_CONTRACT.md` is published and **nothing in `src/` implements it**: no connector module, no `WarehouseConnector` symbol, no `create_warehouse`, no `warehouse_backend` field in `config.py`. A declared interface with zero implementation. |
| **B2** | `not_started` | existence | 1 episode-zone service. `egress_test` runs from `episode` only. |
| **B3** | `not_started` | existence | **0 of 10** owed surface tools registered (`docs_list`, `docs_get`, `bi_list`, `bi_get`, `bi_refresh`, `bi_cached`, `s3_*`). Tool count still 17. |
| **B4** | `not_started` | existence | no `fakesnow`/`snowflake` in gateway or world code or dependencies; named only in docs. |
| tests | **PASS** | executed | **12 passed / 0 failed** in 21 s; 12 test defs. |

### Incompleteness scan

`0 blocker · 7 warn · 2 note` at capture time (the warns are all `changed_during_scan`).

| Kind | Where | Note |
|---|---|---|
| `cross_track_import` | `gateway/src/alkera_gateway/core.py:23` | `from world.fingerprint import semantic_hash`, deferred inside `Gateway.__init__` and guarded. **Track B soft-depends on Track A's package.** If Kimi renames it, the gateway raises only when `world_root` is set — and gateway's own 12 tests need not go red. This is the composition seam, and no track's suite owns it. |
| `defined_never_referenced` | `gateway/src/alkera_gateway/surfaces.py:12` | `Scheduler` Protocol is declared and never named anywhere. `FileScheduler` satisfies it structurally, so it is documentation, not a gap. |

### Git — the interrupted-session signal

`HEAD bbfd237` · **0 commits this round** · 0 stashes

| Bucket | untracked | modified | deleted |
|---|---|---|---|
| A (`world/`) | 53 | 7 | **1** (`world/world/gen.py`) |
| B (`gateway/`) | 1 | 0 | 0 |
| C (`tasks/`) | 32 | 2 | 0 |
| corpus (`implementation_structure/`) | 10 | 26 | 0 |

**This is the single largest interruption risk on the board.** 99 untracked files and 35 modified
ones exist only in the working tree. A `git checkout`, a stash, or a crashed session loses all of it.
`world/world/gen.py` is deleted-not-committed; its content moved to
`world/world/themes/revenue_daily/gen.py`, which is untracked. Right now git records neither.

---

## 4. Safe to pick up — candidates only, nothing edited

Per the rules of engagement I edited nothing under `world/` or `gateway/`. Three candidates,
in descending value:

1. **Ask each track to commit.** Not a code change; the highest-value action available. Nothing
   else on this list matters if the tree is lost.
2. **`MainframeBatchThemeTest::test_semantic_fingerprint_stable`** — a real, named, reproducible
   failure against A2's acceptance criterion. **Kimi's to fix**, and it may already be in hand;
   it is listed so it cannot pass silently.
3. **`Scheduler` Protocol (`surfaces.py:12`)** — annotate `Operations(scheduler: Scheduler)` or
   delete it. Trivial, cosmetic, **Astra's file**. Do not touch it while B1 is in flight.

---

## 5. What the script catches — precisely

### Acceptance-criterion checks (mechanism, not existence)

| Check | How it is decided | Can reach `done`? |
|---|---|---|
| A1 | AST of `build.py`: are the 8 named constants still module-level assignments? Does the builder consume a theme (`--theme`, `seed['theme']`, theme import)? | Yes — only with the constants gone **and** the world suite green in the same run |
| A2 | **The theme contract is derived from what the builder actually consumes**: `theme.gen.<hook>` and `theme.manifest["<key>"]` in `build.py`, `d / "<file>"` in `themes/__init__.py`. Each theme dir is then checked against that derived contract. | Yes — all three themes structurally complete **and** suite green |
| A3 | Is there an in-container probe (`exec … test ! -e /grading`), *and* a recorded run artifact? Volume-mount lines are excluded. | **No** — caps at `unknown`; only a live container can prove it |
| A4a | Gate-1 block of `acceptance.sh`: build count, self-declared "same host", cross-host markers (`ssh`, `docker run`, `CROSS_HOST`), `.github/workflows` | **No** — caps at `in_progress` |
| A4b | Compose scan: which services mount `worlddata` writable vs `:ro` | **No** — it is a tripwire, not a gate |
| B1 | Line-exact `import duckdb` / `duckdb.connect(` / `dialect="duckdb"` sites in `sql.py`; then four independent implementation signals (connector module, `WarehouseConnector` symbol, `create_warehouse` factory, `warehouse_backend` config field) | **No** — byte-identical action rows across backends is not verified here |
| B2 | Compose services on `episode_net` with no `profiles:`; `egress_test` invocations parsed for their target service; host-published ports | **No** — docker is never run |
| B3 | AST of the `TOOLS` dict (not a grep), each owed tool marked `registered` and separately `exercised_by_tests` | Yes — all registered **and** exercised |
| B4 | `fakesnow`/`snowflake` split into code refs, dependency refs and doc refs; wire markers `/session/v1/login-request`, `/queries/v1/query-request`, TLS, hostname | **No** — caps at `in_progress` |
| tests | Actually runs both suites, parses passed/failed/skipped/xfailed, records the interpreter and duration | — |

`done` is never awarded for a file existing. Every check prints its **evidence strength**:
`existence` (a name is present, proves nothing ran) · `reference` (something consumes it) ·
`executed` (this script ran it and saw the result).

### Incompleteness rules — all 38 verified against synthetic fixtures

| Rule | Fires on |
|---|---|
| `stub` | body is `pass`, `...`, `raise NotImplementedError`, or docstring-only. **Not** on empty pydantic subclasses, exception types, or `Protocol` method `...` — but **does** fire on `class PostgresConnector(WarehouseConnector): pass`, which is the case that matters here |
| `unparseable` / `truncated` | Python that does not parse; invalid JSON; markdown ending mid-sentence or with an unclosed ``` fence; YAML/shell with an unterminated string; missing trailing newline (weak) |
| `test_skipped` | `@pytest.mark.skip` / `skipif` / `xfail`, or `pytest.skip()` in the body |
| `test_asserts_nothing` | a `test_*` with no `assert`, no `assert_*` helper call, no `pytest.raises` |
| `import_missing` | module-level import that does not resolve in the track's own venv (**blocker**); relative `from .foo import` where `foo` was renamed or never written |
| `import_missing_deferred` | same, but inside a function or `try/except` — optional by intent, so a **note** |
| `cross_track_import` | Track B importing `world`, Track A importing `alkera_gateway` |
| `interface_unimplemented` | a `Protocol` whose method set no class in `world/` or `gateway/` satisfies |
| `defined_never_referenced` | a top-level `def`/`class` whose name appears nowhere else (note — dynamic dispatch is a false positive) |
| `changed_after_test_run` | mtime newer than that track's test run start — **that green run does not cover this file** |
| `changed_during_scan` | mtime newer than the scan start — another agent is editing it *right now* |
| git | untracked / modified / staged / **deleted** / stashes, bucketed by track (A, B, C, corpus, monitor) |

Every rule was exercised against purpose-built fixtures — stubs, assert-less tests, truncated
files of five types, renamed imports, a compose file with a second episode-zone service, and a
half-built theme — including the false-positive cases. 38 assertions, all passing. I did not
extrapolate that the detector works; I measured it.

---

## 6. What the script will **not** catch — precisely

This is the more useful half of the document.

1. **Anything requiring Docker.** A3, A4a and B2 all have acceptance criteria that only a live
   container can satisfy. The script reports whether a *probe* and an *artifact* exist and says
   `unknown` when it cannot distinguish a real run from a written-down claim. It never runs
   `docker`, never builds an image, never measures the 60 s boot or the 2 GB budget.
2. **Determinism.** It does not build two worlds and compare fingerprints. It found the C1
   fingerprint failure **only because Kimi wrote a test for it** — had that test not existed, a
   non-deterministic theme would have read as structurally complete and green.
3. **Semantic wrongness inside structurally complete code.** A connector returning rows in a
   different order, an action-log row differing by one field, a theme whose EBCDIC page is wrong:
   every structural check passes.
4. **A test that asserts the wrong thing.** `asserts_something` only proves an assertion exists.
   `assert True` passes. A stale expected value passes.
5. **A test silently weakened in place.** If an existing test is edited to assert less, the count
   stays the same and the suite stays green. Test *count* and pass/fail are tracked; assertion
   content and coverage are not.
6. **Dynamically loaded code.** Kimi's themes load via `importlib.util.spec_from_file_location`,
   invisible to a static import scan. I compensated with the derived theme-contract check — but
   only for *that* pattern. A different plugin mechanism would be a blind spot again.
7. **Prose that is complete but wrong.** `CONNECTOR_CONTRACT.md` passes every truncation check. A
   contract that contradicts the implementation reads as fine.
8. **mtime signals across checkouts.** A `git checkout`, clone or archive extraction rewrites
   mtimes; `changed_after_test_run` would then fire on everything or on nothing.
9. **Track C.** `tasks/` gets git signals only — no acceptance checks. Out of my remit.
10. **A green suite is not coverage.** A1 reached `done` because the world suite was green. That
    suite is Kimi's own 7 (now 14) tests, which need not exercise the new code at all. "Green" is
    evidence of *not visibly broken*, never of *complete*.

---

## 7. The single biggest weakness in my own detector

**Fixture-shaped completion: work that is finished at the seam and hollow behind it.**

Concretely, the thing I would most expect to slip past. Astra implements `create_warehouse()`,
registers `PostgresConnector`, adds `test_postgres_connector`, and goes green — while every test
fixture still runs DuckDB, because standing up Postgres in the test harness was the expensive
part and the budget ran out first. My detector then sees: the constants gone, a connector module
present, a factory symbol present, `warehouse_backend` in config, the tool registered, a test
referencing it by name, twelve-plus tests passing. It would report B1 and B3 as advancing on
`reference` evidence and promote them to `done` the moment the counts line up.

The same hole exists on Track A (a theme registered and tested against the `revenue_daily`
fixture) and on B2 (a Postgres service declared in compose with an `egress_test` line that never
runs because the service never starts).

This matters because **fixture-shaped completion is exactly this project's documented failure
mode** — "every track passed its own tests while the composition was broken." My detector checks
whether the *new path exists and is named by a test*. It cannot check whether the test's fixture
actually traversed the new path rather than the old one. That distinction is invisible to
structure and invisible to a pass count, and it is precisely where a budget-exhausted agent
stops.

**The cheap mitigation, if the lead wants it.** Require that each new backend, container or theme
show up as a *distinct parametrised test id* rather than a branch inside one test — e.g.
`test_sql_query[postgres]`, not `test_sql_query` with an `if backend == …`. Test *count* is
something my detector already tracks and diffs. Under that convention a second backend that was
never actually exercised fails to move the count, and the hollow completion becomes visible to a
tool that costs one second to run. Without it, the only instrument that catches this is the
integration checkpoint in `3_track.md` §6 — which, as that section says, is the one nobody owns
until someone is assigned it.
