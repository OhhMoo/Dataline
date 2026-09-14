# B3 — the five untested surfaces

**Agent:** B3-FINISH · **Date:** 2026-09-13 · **Scope:** `gateway/tests/` only.
Astra registered ten B3 surface tools. Five had no test at all: `bi_list`, `s3_list`,
`s3_get`, `s3_put`, `s3_delete`. All five now execute against a real service and are
asserted on their content.

```
before   16 passed
after    26 passed, 2 xfailed        (16 existing untouched + 10 new + 2 known defects)
```

Files added: `gateway/tests/test_object_storage.py`, `gateway/tests/test_surface_contents.py`.
One additive edit to `gateway/tests/conftest.py`: `config()` gained `**overrides`, so a test
can set `s3_endpoint`, `s3_buckets` or `max_payload_bytes` without changing any existing call
site. **No file under `gateway/src/` or `gateway/docker/` was touched.**

---

## 1. The new tests, and what each actually asserts

### `tests/test_object_storage.py` — s3_put / s3_get / s3_list / s3_delete

Runs against `adobe/s3mock` pinned to the **same digest as `docker/compose.yml`**
(`sha256:e7c36014…`), started per session on an ephemeral host port and removed in teardown.
The Track B compose stack was not touched.

| Test id | What it asserts |
|---|---|
| `test_object_storage_round_trip` | `s3_put` → `s3_get` returns **the same bytes**, the same ETag and the metadata that went in. `s3_list` shows the key with the right **size and ETag**. `s3_delete` returns `{"key":…, "deleted": true}`, and afterwards `s3_get` fails with `storage_error` / `NoSuchKey` **and** the listing is empty — both views have to agree that it is gone. |
| `test_object_storage_preserves_bytes_that_are_not_utf8` | cp037 (EBCDIC), cp1252, and all 256 byte values each survive put→get **byte-identical**; U+FFFD appears nowhere; the cp1252 object still decodes to `José` / `München` with the right codec. This is the encoding check (§3). |
| `test_s3_get_refuses_an_oversized_object_loudly` | With `max_payload_bytes=2048` and an object seeded out of band at 2112 bytes, `s3_get` returns `object_too_large` with `result: null` — **not a truncated body**. The logged row is `exit: error`, `op_class: read`. |
| `test_s3_list_filters_by_prefix_and_pages` | `prefix` really filters (3 of 4 keys), `limit` really limits, a truncated page **hands back a continuation token**, and following that token returns the remaining key with no duplicate and a null token. An ignored filter returns a plausible wrong answer that no `ok` check would catch. |
| `test_object_storage_writes_wellformed_action_log_rows` | §9.2.2. `validate_trace()` over four calls: contiguous `seq` 1–4, `args_hash` and `result_hash` verified against `digest()`. `op_class` is exactly `["read","write","read","destructive"]` — **`s3_delete` classifies destructive**. `s3_delete` flags `["delete"]`, `s3_put` flags `["possible_overwrite"]`, targets are `s3://<bucket>/<key>`, `s3_put` has empty `read_targets`, and `op_class` is **absent from every response body** (it lives in the log, not the response). The put body is recoverable from `args` so a grader can replay the write. |
| `test_object_storage_rejects_bad_input_cleanly` | `bucket_denied` for a bucket outside the world's surface, `invalid_base64` for a bad body (and the rejected write **created nothing**), `NoSuchKey` for a missing key, `invalid_arguments` for an unknown field. Five rows logged with the right `exit` values and `result: null` on every error. |
| `test_storage_tools_report_a_world_without_object_storage` | On a world with no `s3_endpoint`, all four tools return `storage_unconfigured` rather than hanging or returning an empty listing that reads like an empty bucket. **This one needs no container** and always runs. |

### `tests/test_surface_contents.py` — bi_list

| Test id | What it asserts |
|---|---|
| `test_bi_list_returns_whole_definitions_not_stubs` | Two dashboards come back sorted; the seeded one is asserted **field by field** — `owner`, `version`, the full `metrics` list including `expression` and `doc`, the panel's literal `sql`, its `metric` binding and its `watermark_sql`. The listed object is `==` what `bi_get` serves, so enumerate-then-drill-in cannot disagree. Finally the SQL **from the listing is executed** and returns the fixture's real total (`30.75`) — proving the listing carries runnable SQL, not a placeholder. |
| `test_bi_list_reads_live_definitions_and_logs_a_read` | `bi_list` reflects a `bi_update` immediately (no stale index). §9.2.2 rows: `seq` 1–3, tools `bi_list/bi_update/bi_list`, `op_class` `read/write/read`, targets naming each dashboard read, `flags: []` on a create and `["overwrite"]` on the re-update, `args == {}` with `args_hash == digest({})`, and no `op_class` in the response. |
| `test_bi_list_fails_loudly_on_an_unreadable_definition` | A non-dashboard `.yml` beside the dashboards makes `bi_list` **fail** rather than return a shorter listing that looks complete. Failing is the correct behaviour; a silent partial listing would hide a broken world from both the agent and the grader. |

---

## 2. Verified working vs. not exercised

**Verified working, on this machine, 2026-09-13:**
`s3_put`, `s3_get`, `s3_list`, `s3_delete` — full round trip through the gateway against
`adobe/s3mock@sha256:e7c36014…`, including binary bodies, user metadata, prefix filtering and
continuation paging. `bi_list` — full definitions, live reads, correct log rows.

**Falsification (measure, don't extrapolate — ground rule 8).** I checked the tests are not
silently dead, twice:

- Pointed `ALKERA_TEST_S3_ENDPOINT` at a dead port: **6 of 7 error out**; only the
  no-storage-configured test (which deliberately uses the storage-less gateway) passes. The
  six cannot pass without a live service.
- Removed `docker` from `PATH`: **6 skip**, each saying
  `object storage not exercised: no \`docker\` on PATH and ALKERA_TEST_S3_ENDPOINT is unset`.

**Nothing was left unexercised for lack of a container.** No skip is recorded in the normal run.

---

## 3. The encoding path

**`s3_get` does not have the `fs_read` hole, and I have made that structural property
executable.** `ObjectStorage.get` base64-encodes the raw bytes and never calls `.decode()`, so
a cp037 extract or a cp1252 export arrives byte-identical. The oversized case fails loudly with
`object_too_large` rather than truncating. Both are now asserted, so a later "text convenience"
parameter cannot quietly reintroduce the failure class.

**The `docs_search` hole is real, and it is worse than reported.** I reproduced it and it does
not merely *omit* a field:

```
docs/vendor.md   cp1252, contains "idempotent"   -> absent from matches
docs/huge.md     over max_payload_bytes, contains "idempotent"  -> absent from matches
result keys: ["limit_reached", "matches"]        limit_reached: false
```

`Workspace.search` (`files.py`) swallows `ToolError` and `UnicodeError` with a bare `continue`.
So the response does not just fail to mention the two documents it could not read — it returns
`limit_reached: false`, which **affirmatively claims the search was complete**. And `Search` in
`tools.py` has no `encoding` parameter, so the agent has no way to ask for them either. This is
the `fs_read`/`fs_write` fix's own failure class, one module away from its docstring.

I did not fix it: `files.py` and `tools.py` are `B1-FINISH`'s files this round. Instead it is
recorded as `test_docs_search_does_not_silently_drop_unreadable_documents`, marked
**`xfail(strict=True)`** — it asserts the behaviour we want (the result must account for every
document it could not search), stays out of the green count today, and turns the suite **red**
the moment someone fixes it without deleting the marker. It does not assert the broken
behaviour, so it cannot turn the bug into a specification.

**Source change needed (for B1, not made here).** `Workspace.search` must record what it
skipped, e.g. return `{"matches": …, "limit_reached": …, "skipped": [{"path":…, "reason":…}]}`,
and `Search` should take `encoding` the way `Read` does. Both are behaviour changes and Track C
has live rollouts, so they are a proposal, not an edit.

---

## 4. Two findings outside my own tests

### 4.1 `bi_list` blames the agent for a broken world

A single non-dashboard `.yml` in the dashboards directory makes `bi_list({})` return
`code: "invalid_arguments"` with a raw pydantic dump of the `Dashboard` model. The agent's
arguments were `{}`; nothing about them was invalid. A model told its call was malformed when
the *world* is malformed will spend turns rewriting a call that was already correct — and in
C1's classifier that lands as `reasoning`. Recorded as
`test_bi_list_blames_the_world_not_the_agents_arguments`, also `xfail(strict=True)`.
The cause is `BIStore.list` letting `ValidationError` escape into `core.py`'s generic
`except ValidationError -> invalid_arguments` handler, which exists for *argument* validation.

### 4.2 The compose `landing` bucket does not exist — for `B2B4-FINISH`

This blocks `docker/acceptance_inside.py`, which does `s3_put(bucket="landing", …)`. I probed
it five ways on the pinned digest (`adobe/s3mock@sha256:e7c36014…`, container labels report
S3Mock build `20260910`):

| Route tried | Result |
|---|---|
| `-e initialBuckets=landing` (exactly compose.yml's form) | no bucket; `ListAllMyBuckets` returns `<Buckets/>` |
| `-e INITIALBUCKETS` / `-e COM_ADOBE_TESTING_S3MOCK_DOMAIN_INITIALBUCKETS` | no bucket |
| `-e SPRING_APPLICATION_JSON='{"initialBuckets":"landing"}'` | no bucket |
| `-e JAVA_TOOL_OPTIONS="-DinitialBuckets=landing"` (confirmed picked up in the JVM log) | no bucket |
| program args `… image --initialBuckets=landing` | no bucket |

`root=/s3-data` is ignored the same way: the directory is never created, and after an explicit
`CreateBucket` + `PutObject` the data lands in a random `/tmp/s3mockFileStore<random>/`. So the
compose `s3data` volume and `retainFilesOnExit: "true"` are both no-ops, and object state does
not survive a container recreate.

And the bucket is not created anywhere else — `docker/start.py` sets
`s3_buckets=["landing"]` (an allow-list, not a creation) and nothing calls `CreateBucket`.
A `PutObject` into a never-created bucket returns **HTTP 404 `NoSuchBucket`**, verified on a
throwaway container. The live `alkera-track-b-s3-1` has no `/tmp/s3mockFileStore*` directory at
all, which means no bucket has ever been created in it — the S3 half of
`acceptance_inside.py` has never actually passed.

**Recommended fix (B2/B4's call):** seed the bucket explicitly at startup — one `create_bucket`
in `docker/start.py`, or an init container — rather than trusting `initialBuckets`. That is
also the right shape: buckets are world fixtures, and the gateway deliberately exposes no
bucket-creation tool. My test fixture does exactly this out of band, the same way
`make_world()` seeds the warehouse with `duckdb` directly.

*Verified on this machine: Docker 29.5.2, Windows 11, 2026-09-13. Probe containers
`alkera-b3-s3-probe`, `b3ib1`–`b3ib5`, `b3nb` were all removed; the `alkera-track-b` stack was
never stopped, modified or written to.*

---

## 5. The single biggest weakness in my own output

**Nothing here proves the S3 surface works *in the container where it matters*.**

Every S3 test runs on the host: host pytest, host boto3, a throwaway s3mock on a published
localhost port, and a bucket my own fixture creates. The real path is different in three ways
that my tests cannot see, and §4.2 is direct evidence that the difference is not hypothetical —
the compose stack's bucket does not exist, and I only found that by probing the container by
hand, *not* by running a test:

1. **Networking.** In compose the gateway reaches `http://s3:9090` over the internal
   `backend_net`. I tested `http://127.0.0.1:<ephemeral>`. DNS resolution, the internal network,
   and the no-egress rules are all untested by me.
2. **Bucket provisioning.** My fixture creates the bucket, so my tests pass on a world whose
   provisioning is broken. That is precisely the shape of defect this task was sent to kill: a
   green test alongside a dead surface. I converted it into a written finding for B2/B4 rather
   than a failing test, because the fix lives in `docker/`, which is not mine this round.
3. **Payload realism.** Every object I put is well under a kilobyte. `max_payload_bytes` is
   exercised only by a synthetic 2 KB ceiling. A multi-megabyte staged file through base64 —
   which is what archetypes A010/A017/A022/A025 actually stage — has never been through this
   tool, and base64 inflates by 4/3 into a limit that also has to hold the JSON envelope and
   the action-log row. That interaction is untested.

Secondary weakness: `bi_list` is tested on **two** dashboards. The 1000-row truncation
behaviour that `fs_list` has does not exist in `BIStore.list` at all — it globs and validates
every `*.yml` with no cap — so a world with many dashboards will blow the `result_too_large`
gate and fail the whole listing with no partial answer. I did not test that and did not file it
as a defect because I have not measured where the ceiling falls.

The honest summary: **the five tools are proven to work; the world that is supposed to serve
them is not.**
