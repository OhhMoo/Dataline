# Publication ledger contract

Frozen 2026-09-16. Writer `world/world/ledger.py` + `world/world/scheduler.py`;
reader `tasks/harness/ledger.py`. This seam is all the two share. Every claim below
is **[measured]** (a test or probe ran it) or **[designed]**.

**Why.** A grader reading only the final world cannot tell a pipeline that was right
all along from one that published a wrong number on Tuesday and corrected it on
Thursday; `var/sched/state.json` says a job succeeded, not what it made visible. The
ledger records, at the virtual moment of each publication, the canonical contents of
what a consumer could see — so a good final state cannot erase a bad intermediate
one, and invariants can be graded at every checkpoint.

## Schema

`<world_root>/var/ledger/receipts.jsonl` — one JSON object per line, canonical JSON
(`sort_keys`, tight separators, `ensure_ascii`), exactly these keys:

| key | value |
|---|---|
| `seq` | int, 1-based, contiguous |
| `vnow` | ISO-8601 virtual time: the run's `vts_end` — a publication is visible when the job finished |
| `day` | ISO date: the **execution** day the occurrence was responsible for |
| `job` | scheduler job name |
| `target` | `catalog.schema.table`, `schema.table` (primary warehouse), or a workspace-relative file path |
| `kind` | `"table"` \| `"file"` |
| `canonical_hash` | sha256 of canonical logical contents (below) |
| `row_count` | int, or null (file, or absent target) |
| `sample` | ≤5 rows as lists in canonical order, or null |
| `consumer` | string from the spec, or null |

**Canonical hash.** Table: exactly `fingerprint._warehouse_hash`'s per-table rule —
sha256 over `{"table": "<schema>.<table>", "columns": [...]}` then one canonical row
per line, rows ordered by every column in ordinal position. Typed values, sorted
rows, **not** file bytes. A primary-warehouse receipt's hash *is*
`fingerprint.semantic()["warehouse"]["tables"][target]`, and the same logical table
written to a second file in a different row order hashes the same while the files
differ **[measured]**. File: sha256 of the bytes **[measured]**. A target that does
not exist hashes to the sentinel `ledger.ABSENT` — not `sha256(b"")` — with null
`row_count`/`sample`, and does not raise **[measured]**.

## Trigger

A job spec may carry `publishes: [{target, kind, consumer?}]`, read first from
`live["jobs"][job]["publishes"]`, then `…["exec"]["publishes"]`, then
`state["jobs"][job]["publishes"]` for a fixture world with no live plane
**[measured]**. `state.json`'s frozen `{command, duration_s}` shape is never written
to; a task author opts in.

`WorldScheduler._simulate_job` appends one receipt per entry, after the job's
command completes, at **the three sites where `_execute` actually ran**: the clean
path, `job_overrun`, `partial_write`. `job_timeout` and a burned `job_retry` attempt
never reached the work, so they publish nothing **[measured]** — a receipt there
would claim a publication the world did not make. `partial_write` *does* publish:
the loader really wrote before it died, and the torn table is exactly the bad
intermediate publication this ledger exists to pin down **[designed — no test
enables that fault with a publishing job]**.

**`trigger()` does not write receipts** **[measured]**. A manual trigger runs
*inside* the episode container, where `var/ledger` is shadowed by an empty read-only
mount, so a receipt written there would land on a tmpfs and vanish at teardown — a
silently incomplete ledger is worse than one with a stated boundary. **Consequence:**
a publication an agent causes by `sched_trigger` is not in the ledger; grade
publications the clock advances over. This is the weakest part of the design.

## Determinism

Same seed + same advances ⇒ byte-identical `receipts.jsonl`: two independent builds
advanced 30h produce the same file hash, and `advance(1)×30` lands the same bytes as
`advance(30)` **[measured]**. Nothing reads a wall clock — `vnow` and `day` come from
the scheduler. Every line is canonical JSON, the file only ever grows and earlier
lines never move (`append_receipt` opens `"ab"`) **[measured]**.

## Fingerprint

`var/ledger` is **not** a fingerprint section: it is an output of advances, like
`var/sched` state — unlike `var/obs`, which is hashed. Forging a line leaves
`semantic_hash` unchanged **[measured]**, and no existing world changes, because a
world whose jobs declare no `publishes` never creates the file **[measured]**. No
theme declares `publishes`: adding one would move `var/world/live.json`, which *is*
fingerprinted, and every recorded world hash with it. The ledger's integrity
mechanism is the agent's inability to reach it, not the fingerprint. It *is* inside
`snapshot.SCOPE`, so restoring a snapshot restores that moment's ledger **[designed]**.

## Protection

**Gateway.** The `fs_*` root is `<world_root>/workspace`; `var/ledger` is a sibling
and no tool, config key or surface names it. Measured through the gateway: every
escaping spelling — `../var/ledger/…`, `../../…`, `/world/var/ledger/…`,
`/workspace/../var/ledger/…` — returns `path_denied` for `fs_read`, `fs_list` and
`fs_write`; `var/ledger/receipts.jsonl` with no leading slash is *allowed*, resolves
to the agent's own repo, and leaves the ledger's bytes unchanged **[measured: P5]**.

**Container shell.** `/world` is one bind mount of the whole world root and the
episode has a real shell, so the path is shadowed: `compose.yml` and
`compose.episode.yml` mount an empty read-only tmpfs at `/world/var/ledger`.
Measured from inside the episode container against a receipt planted in the world
volume from a second container: the kernel reports `tmpfs ro,nosuid,nodev,noexec` at
that target, the directory lists empty, reading gives `FileNotFoundError`, writing
gives `Read-only file system`, and a canary sweep of `/world` and `/workspace` finds
nothing **[measured: P5, `gateway/docker/evidence/containment-ledger.json`,
2026-09-16, docker 29.5.2]**. That sweep scanned 3 files (the fixture world is nearly
empty), so it is the weak evidence and the mount/read/write results are the strong
ones. The writer is unaffected: `advance` runs on the host, outside the container.

**Grading.** `tasks/harness/ledger.py` `stage(world_root, grading_dir)` copies the
ledger to `<grading>/ledger/receipts.jsonl` after teardown, before the grader runs
(`episode.grade`; `{ledger}` is then available to `grade_argv`) **[measured on the
copy path; no rollout has run the wiring]**. `load_receipts(root)` prefers that copy
over the live world tree and refuses malformed evidence — unknown or missing keys, a
hole in `seq` — rather than reading around it. `receipts_between(r, a, b)` is
half-open `[a, b)` so consecutive checkpoints partition the ledger;
`last_receipt(r, target)` follows `seq`, not `vnow` **[measured]**.

## Not covered

- A publication caused by `sched_trigger` (above); files record no
  `row_count`/`sample` (bytes are what a file consumer receives).
- P5 ran against the Track B fixture stack, not a live rollout world **[designed]**.
- A `target` the world never writes records `ABSENT` every time, loudly and forever.
