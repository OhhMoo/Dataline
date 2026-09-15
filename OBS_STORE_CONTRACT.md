# Observability store — frozen seam

**Frozen 2026-09-14.** Two agents build against this concurrently and neither may change it
without saying so loudly: **FAULTS** owns `world/world/` and *writes* samples; **OBS-HISTORY**
owns `gateway/src/alkera_gateway/observability.py` and *reads* them. This file is the only
thing they share.

## Why a store at all

`OPS-SURFACE` shipped metrics and alarms, then named their defect precisely:

> *"Because I required each reading to be re-derivable from the data, reading the alarms is
> strictly dominated by reading the data — an agent that consults the dashboard can be misled,
> one that ignores it cannot."*

A real observability system **records what happened**. Ours **computes what is**. Those differ
the moment the world moves: once `advance` has run, the state that produced yesterday's
freshness reading is gone, and the recorded sample becomes the *only* evidence it ever held
that value. That is a correctness property of the surface, not a scoring device.

## Location and format

```
/var/obs/samples.jsonl      append-only, one JSON object per line, ordered by vts
/var/obs/definitions.yaml   metric + alarm definitions for this world, seeded by the theme
```

`/var/obs/` sits beside `/var/sched/` and `/var/bi/` in the §9.2.1 layout. It is
**agent-readable** — an observability store the agent cannot read is not a surface.

## Sample row

```json
{"vts": "2026-03-04T05:30:00Z", "metric": "orders_freshness_seconds",
 "value": 32400.0, "source": "advance", "run_id": "sftp_poll-0031",
 "detail": {"watermark": "2026-03-03T20:30:00Z"}}
```

- `vts` — **virtual** time, never wall clock. A sample stamped with wall clock breaks the
  reproducible-rebuild gate.
- `value` — `null` is legal and means *insufficient data*, which is different from `0.0`.
  Never write `0.0` for "could not measure"; that is the silent-wrong failure this project
  treats as disqualifying.
- `source` — `advance` | `trigger` | `deploy` | `build`.
- `detail` — the components the value was derived from, so a reader can re-derive it at the
  time it was taken.

## Who does what

**FAULTS (writer).** During `build` and `advance`, after each job runs, append one sample per
defined metric. Writes must be deterministic: same seed plus same span produces a byte-identical
`samples.jsonl`. Appending is the *only* permitted operation — never rewrite or reorder history,
because an observability store that can be edited after the fact is not evidence.

**OBS-HISTORY (reader).** `metrics_series` and `alarms_history` read this file rather than
recomputing. `metrics_get` may still compute live current state — the point is that *history*
comes from the record. Apply a retention window so old samples age out, which is what makes the
store a genuine record with genuine limits rather than an infinite oracle.

## The invariant that matters

**A reader must never be able to reconstruct a past sample from present state.** If it can, the
store is decoration and we are back where OPS-SURFACE started. Test it: advance the world, then
assert a sample recorded before the advance is *not* derivable from the world as it now stands.

## Fingerprint

`world/world/fingerprint.py` must include `/var/obs/` in the semantic fingerprint — it is world
state. OPS-SURFACE wrote the patch shape into `gateway/OBSERVABILITY_CONTRACT.md`. **FAULTS owns
that edit**, since it owns `world/`.

---

## Reader-side notes (appended by OBS-HISTORY; the seam above is unchanged)

Three things were settled while building the reader. None of them change the
row shape, the location, or who writes what.

1. **One definitions file per world.** This document names `definitions.yaml`;
   `gateway/OBSERVABILITY_CONTRACT.md` named `observability.yml` first, and
   `world/world/observe.py` reads either. The gateway now reads either too, and
   **refuses a world that ships both** (`invalid_observability`), because a
   writer and a reader describing different metric sets is the one way this
   surface can be wrong without anything looking wrong. Every theme ships
   exactly one, named `obs/definitions.yaml`.

2. **Definition spelling is accepted both ways.** `observe.py` reads a job
   metric as `metric.job` or `metric.source.job`, and reads `window_runs` only
   at the top level; the gateway's model wants them under `source`. The gateway
   lifts the top-level spelling into `source` rather than rejecting it, so the
   theme files use the top-level form and `window_runs` means the same number on
   both sides. Anything genuinely unrecognised still fails loudly.

3. **Retention is read-side.** `retention: {days: N}` is declared in the
   definitions file. Appending stays the writer's only operation and nothing is
   truncated on disk; the reader will not *serve* a sample outside the window
   and reports how many it refused (`aged_out`). If the writer ever does want to
   prune, that is a separate decision and this paragraph is the place to say so.

Two observations for the writer, neither of them a request:

- **`not_yet_observed`.** On `mainframe_batch`, ten rows of a five-day advance
  carry a `vts` *after* the resulting `vnow` - the documented consequence of
  appending in measurement order when `job_overrun` pushes a run past the clock.
  The reader refuses to serve them, because a reader at `vnow` cannot honestly
  claim to have observed a reading stamped later, and it reports the count. That
  seems right, but it does mean ten real samples are invisible until the world
  advances again.
- **A recorded sample is always a scalar.** That is correct and it changes what
  `evaluate_on: series` means: `datapoints_to_alarm` counts readings, not days,
  and a floor over a cumulative `row_count` can never fire. Three of the four
  themes needed a per-period `sql_scalar` metric added for their series alarms.
  Both hazards are written up in `gateway/OBSERVABILITY_CONTRACT.md`.
