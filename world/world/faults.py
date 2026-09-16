"""Seeded fault layer: the world misbehaves on its own, and reproducibly.

LIVE-WORLD made the scheduler execute. It could still only break when somebody
broke it: arrivals always arrived, jobs never timed out, nothing retried, no
two jobs overlapped, no partial write was ever observed. This module supplies
the weather.

Three rules govern everything here.

**1. Declared, not random.** A theme declares a fault catalogue under
`live.faults` in `theme.yaml`; a seed file may override any entry. `advance`
then applies them. "This world has a flaky vendor feed" is an authoring
decision that reproduces byte for byte on every rebuild.

**2. The draw is a pure function of (fault_seed, fault_id, occurrence).**
Not of iteration order, not of accumulated PRNG state, not of the wall clock,
and - this is the property that makes the catalogue composable - not of which
*other* faults are declared or enabled. Enabling `vendor_late_delivery` does
not shift a single draw of `sftp_connection_reset`. A world advanced in one
step and the same world advanced in thirty draw identically, because the key is
the occurrence and never the step.

**3. Nothing races. The interleave is staged as committed artifacts.**
LIVE-WORLD's predecessor established the rule: on DuckDB appends never conflict,
so a live race produces no error, forces no winner, and does not rebuild. So an
overlap here is overlapping `vts_start`/`vts_end` in the run history plus the
data state that interleaving genuinely produced (a partial table, a truncated
file) - built strictly sequentially. We build the *evidence* of a race, never
the race.

And one prohibition, inherited: **never fabricate a run status.** A job the
fault layer times out is `failed` with a timeout reason, and its work does not
run. A retry's first attempt is `failed` with a transport error, and its work
does not run. A `partial_write` run really executed, really wrote, and is then
recorded `failed` because the process died before it finished - which is what
leaving the rows behind means. No fault path can produce `success`.

## The catalogue

| kind | fires on | what it leaves behind |
|---|---|---|
| `late_arrival` | an arrival | the partition lands `delay_minutes` late; the consumer that ran at its normal slot found nothing |
| `missing_arrival` | an arrival | the partition never lands at all; a gap, then the next day |
| `duplicate_arrival` | an arrival | the same delivery twice, as a second file the loader's glob picks up |
| `torn_write` | an arrival | the file truncated mid-write and the sentinel absent: a read landing inside a write |
| `job_timeout` | a job occurrence | one `failed` run, `executed: false`, killed at the timeout; no work ran |
| `job_retry` | a job occurrence | N-1 short `failed` attempts with a transport error, then a real attempt `retry_delay_minutes` later |
| `job_overrun` | a job occurrence | the run really executes but its recorded window stretches over the next occurrence - overlap, or still running at `vnow` |
| `partial_write` | a job occurrence | the work runs, then the tail of the newest partition is gone and the run is `failed` |

## Declaration

```yaml
live:
  faults:
    - id: vendor_late_delivery      # unique; the draw is keyed on it
      kind: late_arrival
      enabled: true
      rate: 0.2                     # per-occurrence probability
      params: {delay_minutes: 150}
    - id: sftp_connection_reset
      kind: job_retry
      enabled: true
      jobs: [sftp_poll]             # job faults may be scoped to named jobs
      only: ["sftp_poll@2026-03-05T05:30:00Z"]   # exact occurrences, no draw
      params: {attempts: 2, retry_delay_minutes: 7}
```

`rate` draws; `only` names occurrences outright (and wins over `rate`), which is
how a task author plants one specific incident rather than a distribution. At
most one fault applies per occurrence: the catalogue is scanned in declaration
order and the first that fires wins, so a catalogue is read top to bottom like a
rule table.

## The backstory

`faults_in_history` replays the catalogue over the seeded past, so a world can
open on a pipeline that has been flaky for three weeks instead of one with
thirty perfect nights. The line between what history can carry and what it
cannot is **data coherence**, not kind.

`initial_state` constructs the past; it runs nothing. So a history fault is
honest exactly when the world the builder actually produced is the world that
fault would have left behind. Every theme's `write_landing` writes days 0..N-1
complete, and every theme reloads every partition each night, which splits the
catalogue in two:

- **Carried.** `job_timeout`, `job_retry`, `job_overrun` - one night's failure
  heals on the next, which is what the built warehouse already shows. And
  `late_arrival`, the one arrival fault that heals: the delivery was not there
  at its slot, it landed later, and the next reload picked it up. The landing
  zone ends up exactly as the builder wrote it, which is the whole point.
- **Not carried.** `missing_arrival`, `duplicate_arrival` and `torn_write` each
  leave a permanent divergence - a hole nobody backfilled, a day counted twice,
  a file that stays truncated - and the seeded landing zone carries none of
  them. `torn_write` is here on MEASUREMENT, not on inspection: a live
  sftp_vendor world advanced through a torn ORDERS upload is still missing that
  day from `raw.vendor_orders` three nights later, because the loader skips
  unsentinelled files and never revisits them. `partial_write` is the same
  story on the job side. These are skipped, not faked: the draw is still
  consumed (so enabling one shifts nothing else, rule 2) and the night records
  clean. Carrying them needs the landing zone itself faulted between
  `write_landing` and `load_raw`, which is `build.py`'s call, not this module's.

An enabled `late_arrival` in a world with `faults_in_history` must say what it
cost, in `params.missed_by`: the jobs that read the delivery at the slot it
covers, with `params.missed_error` as what they reported. Declared, not
measured - same reason `timeout_s` is declared. An EMPTY `missed_by` is a legal
and deliberate answer, meaning "this theme has no job that fails when the
delivery is not there"; the key is required either way so that a fault cannot
quietly cost nothing. A job named there that *really executes* under a job
fault too (a retry's final attempt, an overrun) fails on the missing delivery;
one the scheduler never started (a timeout) does not, because it never got as
far as looking.

This used to read "every shipped theme declares `missed_by: []`", and that was
a measurement of a DEFECT rather than of the world: every `scan` job globbed
the CUMULATIVE landing tree with `min: 1`, so `sftp_poll` at 05:30 counted
sixty older `.done` sentinels and succeeded and `nightly_extract` at 23:00
counted thirty older partitions and succeeded. A poll that cannot fail after
day one makes `late_arrival` inert, whatever the catalogue says, and declaring
`missed_by: []` to match was the honest reading of an inert mechanism.

Both scans are now scoped to the execution day's own delivery
(`pattern: "*/*_{day_compact}.done"`, `pattern: "dt={day}/CLAIMS.DAT"`), so a
withheld delivery is a real count of zero against a real minimum and the poll
FAILS. `missed_by` follows the measurement, not the other way round:
`vendor_late_delivery` declares `[sftp_poll]` and `extract_late` declares
`[nightly_extract]`, each with the `missed_error` the live path produces
verbatim for that night. The consumers - `vendor_load`, `batch_load` - are
still NOT named, and that too is measured: both are theme `load_raw` hooks that
parse whatever is in the landing zone and succeed with one day fewer in it,
leaving `rows_after == rows_before` as their whole signature.

The run history is not the only seeded plane that describes those nights, and
the moment the poll could fail, sftp_vendor's connector control plane started
contradicting it: `write_ingest` seeded a green sync per connector per day
unconditionally, so six nights had `ingest_runs` reporting rows out of a drop
folder `sched_logs` says was empty for another two hours. `history_catalogue`
and `history_arrivals` below are the fix for the class, not the instance: one
decision about which nights were late, read by every plane the builder seeds.

## What is NOT here, and why

- **True concurrency.** See rule 3. Deliberate.
- **Measured timeouts.** A timeout derived from real elapsed time is the single
  most host-dependent number available; `timeout_s` is declared.
- **A historical gap that never healed.** See "Not carried" above: it needs
  `build.py` to fault the landing zone. Named in R003/FAULTS.md, not pretended
  away.
"""
import random
from datetime import timedelta
from fnmatch import fnmatch
from pathlib import Path

ARRIVAL_KINDS = ("late_arrival", "missing_arrival", "duplicate_arrival",
                 "torn_write")
JOB_KINDS = ("job_timeout", "job_retry", "job_overrun", "partial_write")
KINDS = ARRIVAL_KINDS + JOB_KINDS

# An arrival fault that reschedules the event, vs one that alters what landed.
RESCHEDULING = ("late_arrival", "missing_arrival")

# What the seeded backstory can carry. The test is data coherence, not kind:
# `initial_state` runs nothing, so a history fault is honest only when the
# landing zone `write_landing` wrote is the landing zone that fault would have
# left. `late_arrival` is the only arrival fault that qualifies - the delivery
# lands later and the next reload picks it up, so only the RUN HISTORY differs.
# `missing_arrival`, `duplicate_arrival` and `torn_write` each leave a
# permanent divergence the seeded world does not carry (torn_write measured,
# not assumed: see the module docstring), so they are skipped in history rather
# than faked, exactly as `partial_write` is on the job side.
HISTORY_ARRIVAL_KINDS = ("late_arrival",)
HISTORY_JOB_KINDS = ("job_timeout", "job_retry", "job_overrun")

# What a seed file may re-aim on a declared fault. Not `kind`, not `id`.
OVERRIDABLE = frozenset({"enabled", "rate", "only", "jobs", "params"})


class BadFault(SystemExit):
    """A malformed catalogue. Loud at plan time, never silently ignored."""


# --------------------------------------------------------------------------
# declaration
# --------------------------------------------------------------------------

def catalogue(spec: dict | None, overrides: dict | None = None) -> list:
    """Normalize a theme's `live.faults` list, applying seed-file overrides.

    `overrides` is the seed file's `faults:` mapping, id -> partial fault dict
    (`{enabled: true}`, `{rate: 0.5}`, `{params: {...}}`). Params merge key by
    key so a seed can move one knob without restating the block. An override
    naming an id the theme does not declare is an error, not a no-op - a typo
    that silently disables a planted fault is exactly the failure this project
    calls disqualifying.
    """
    faults, seen = [], set()
    for raw in spec or []:
        for key in ("id", "kind"):
            if key not in raw:
                raise BadFault(f"fault declaration is missing {key!r}: {raw}")
        if raw["kind"] not in KINDS:
            raise BadFault(f"unknown fault kind {raw['kind']!r} for {raw['id']!r} "
                           f"(known: {', '.join(sorted(KINDS))})")
        if raw["id"] in seen:
            raise BadFault(f"duplicate fault id {raw['id']!r}")
        seen.add(raw["id"])
        fault = {"id": raw["id"], "kind": raw["kind"],
                 "enabled": bool(raw.get("enabled", False)),
                 "rate": float(raw.get("rate", 0.0)),
                 "params": dict(raw.get("params") or {})}
        if raw.get("jobs"):
            fault["jobs"] = list(raw["jobs"])
        if raw.get("only") is not None:
            fault["only"] = list(raw["only"])
        over = (overrides or {}).get(raw["id"])
        if over:
            # A seed may re-aim a declared fault; it may not redefine what the
            # fault IS. Changing `kind` or `id` from the seed would make the
            # theme's catalogue a lie about the world it describes.
            illegal = sorted(set(over) - OVERRIDABLE)
            if illegal:
                raise BadFault(
                    f"seed override for {raw['id']!r} may not set "
                    f"{', '.join(illegal)} (allowed: {', '.join(sorted(OVERRIDABLE))})")
            params = {**fault["params"], **dict(over.get("params") or {})}
            fault.update({k: v for k, v in over.items() if k != "params"})
            fault["params"] = params
            fault["enabled"] = bool(fault["enabled"])
            fault["rate"] = float(fault["rate"])
        faults.append(fault)
    unknown = sorted(set(overrides or {}) - seen)
    if unknown:
        raise BadFault("seed overrides name faults this theme does not declare: "
                       + ", ".join(unknown))
    return faults


def declared(live: dict | None) -> list:
    return (live or {}).get("faults") or []


def enabled(live: dict | None, kinds=None) -> list:
    """Enabled faults, in declaration order, optionally filtered by kind."""
    return [f for f in declared(live)
            if f.get("enabled") and (kinds is None or f["kind"] in kinds)]


def any_enabled(live: dict | None) -> bool:
    return bool(enabled(live))


# --------------------------------------------------------------------------
# the draw
# --------------------------------------------------------------------------

def draw(fault_seed, fault_id: str, key: str) -> float:
    """The single source of chance in this module.

    `random.Random(str)` seeds from SHA-512 of the encoded string (CPython's
    version-2 seeding), so this is stable across hosts and interpreter runs -
    the same construction `gen.day_rows` already uses and which the cross-host
    gate has proven. One draw per (fault, occurrence): no shared stream, so no
    fault can perturb another's sequence.
    """
    return random.Random(f"{fault_seed}:{fault_id}:{key}").random()


def fires(live: dict, fault: dict, key: str) -> bool:
    if fault.get("only") is not None:
        return key in fault["only"]
    rate = float(fault.get("rate", 0.0))
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    return draw(live.get("fault_seed", 0), fault["id"], key) < rate


def arrival_key(day_index: int) -> str:
    return f"day:{day_index}"


def job_key(job: str, vts_start: str) -> str:
    return f"{job}@{vts_start}"


def arrival_decision(live: dict, day_index: int) -> dict | None:
    """The first enabled arrival fault that fires for this day, or None."""
    key = arrival_key(day_index)
    for fault in enabled(live, ARRIVAL_KINDS):
        if fires(live, fault, key):
            return fault
    return None


def job_decision(live: dict, job: str, vts_start: str) -> dict | None:
    """The first enabled job fault that fires for this occurrence, or None."""
    key = job_key(job, vts_start)
    for fault in enabled(live, JOB_KINDS):
        if fault.get("jobs") and job not in fault["jobs"]:
            continue
        if fires(live, fault, key):
            return fault
    return None


def delay_for(fault: dict) -> timedelta:
    return timedelta(minutes=int(fault["params"].get("delay_minutes", 60)))


# --------------------------------------------------------------------------
# the backstory
# --------------------------------------------------------------------------

def history_catalogue(manifest: dict, seed: dict | None = None) -> list:
    """The catalogue the seeded backstory replays, or [] when the theme opts out.

    One definition of "which faults does this world's PAST carry", because more
    than one seeded plane has to agree about it. `build.py` hands the result to
    `initial_state`, which decides which nights the scheduler recorded a failed
    poll on; a theme's `write_ingest` hook reads the same list so the connector
    control plane fails on exactly those nights and no others. Two planes each
    deriving their own answer is how the two surfaces drift apart.
    """
    live = (manifest or {}).get("live") or {}
    if not (live.get("faults_in_history") and live.get("faults")):
        return []
    return catalogue(live["faults"], (seed or {}).get("faults"))


def history_arrivals(catalogue_: list, fault_seed, days: int) -> dict:
    """{day_index: fault} for the seeded nights a CARRIED arrival fault took.

    The single answer to "was the delivery there at its slot that night". It is
    the same draw `initial_state` makes - `arrival_decision` over the same
    catalogue and the same `fault_seed` - filtered to the kinds the backstory
    can honestly carry (`HISTORY_ARRIVAL_KINDS`). A day carrying an arrival
    fault the history does NOT carry is absent from the result, because the
    builder wrote that night's delivery whole and every seeded plane must read
    it as a normal night.

    Consumers must not re-decide. `write_ingest` calling this is what makes a
    night whose 05:30 poll failed the same night whose 05:31 sync failed: one
    decision, two surfaces, rather than two surfaces guessing.
    """
    live = {"faults": catalogue_ or [], "fault_seed": fault_seed}
    out = {}
    for day_index in range(int(days)):
        fault = arrival_decision(live, day_index)
        if fault and fault["kind"] in HISTORY_ARRIVAL_KINDS:
            out[day_index] = fault
    return out


def missed_by(fault: dict | None, job: str, tokens: dict | None = None) -> str | None:
    """What `job` reported when this arrival fault took its delivery, or None.

    None means the job was untouched: either no fault applied to the day, or
    the fault is one the backstory does not carry, or the job simply did not
    read the delivery at the slot the fault covered. `initial_state` then
    records the night it always recorded.

    `tokens` are the day's spellings (`executor.day_tokens`), applied to
    `missed_error` so the backstory can quote the error the LIVE path produces
    for that night verbatim - `data/drop holds 0 file(s) matching
    */*_20260205.done, ...` rather than a paraphrase. The caller supplies them;
    this module deliberately imports nothing from the executor. A message with
    no tokens in it formats to itself, so an existing catalogue is unaffected.
    """
    if not fault or fault["kind"] not in HISTORY_ARRIVAL_KINDS:
        return None
    if job not in (fault["params"].get("missed_by") or []):
        return None
    message = fault["params"]["missed_error"]
    try:
        return str(message).format(**(tokens or {}))
    except (KeyError, IndexError, ValueError) as exc:
        raise BadFault(
            f"{fault['id']!r} params.missed_error {message!r} names a token "
            f"this day cannot resolve ({type(exc).__name__}: {exc}); known "
            f"tokens: {', '.join(sorted(tokens or {})) or 'none'}") from exc


def check_history(catalogue_: list, jobs) -> None:
    """Refuse a backstory catalogue that has not said what its faults cost.

    An enabled arrival fault the history CAN carry must declare `missed_by` -
    the jobs that read the delivery at the slot it covers. The list may be
    EMPTY, which is an author saying "measured: no job in this theme fails when
    the delivery is not there", and that is the honest answer for all four
    shipped themes today. What is refused is the key being absent, because then
    nobody has decided, and a fault that quietly costs nothing is worse than no
    fault at all. A non-empty list needs `missed_error` (a failure with no
    reason is not evidence) and every name must be a job this theme declares -
    a typo there would silently disable a planted incident, which is exactly
    the failure this project calls disqualifying.

    Loud at build time, never at read time.
    """
    for fault in catalogue_ or []:
        if not fault.get("enabled") or fault["kind"] not in HISTORY_ARRIVAL_KINDS:
            continue
        names = fault["params"].get("missed_by")
        if names is None:
            raise BadFault(
                f"{fault['id']!r} ({fault['kind']}) is enabled in a world with "
                "faults_in_history but declares no params.missed_by. Name the "
                "jobs that fail when the delivery is not there at their slot, "
                "or declare `missed_by: []` to record that none do.")
        if not isinstance(names, (list, tuple)):
            raise BadFault(f"{fault['id']!r} params.missed_by must be a list "
                           f"of job names, got {names!r}")
        if names and not fault["params"].get("missed_error"):
            raise BadFault(f"{fault['id']!r} declares params.missed_by without "
                           "params.missed_error: the run history would record a "
                           "failure with no reason")
        unknown = sorted(n for n in names if n not in jobs)
        if unknown:
            raise BadFault(f"{fault['id']!r} params.missed_by names jobs this "
                           f"theme does not declare: {', '.join(unknown)}")


# --------------------------------------------------------------------------
# arrival effects on the landed bytes
# --------------------------------------------------------------------------

def file_index(base: Path) -> set:
    """Every file under `base`, as sorted relative POSIX paths.

    Paths only - never mtimes, never sizes, never directory iteration order.
    Diffing this set across the arrival hook is how the fault layer learns
    which files the day produced without every theme having to tell it.
    """
    base = Path(base)
    if not base.is_dir():
        return set()
    return {p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()}


def apply_duplicate(base: Path, new_files: list, params: dict) -> dict:
    """Redeliver the day's files: a second copy the loader's glob will find.

    The duplicate is a byte copy under a sibling name, which is what a vendor
    re-sending a batch actually looks like on a drop folder. It is NOT a
    rewrite of the original, so nothing about day N's own bytes changes.
    """
    base = Path(base)
    match = params.get("match", "*")
    suffix = params.get("suffix", ".dup")
    made = []
    for rel in new_files:
        path = base / rel
        if not fnmatch(path.name, match) or not path.is_file():
            continue
        dup = path.with_name(path.stem + suffix + path.suffix)
        dup.write_bytes(path.read_bytes())
        made.append(dup.relative_to(base).as_posix())
    return {"duplicated": made}


def apply_torn_write(base: Path, new_files: list, params: dict) -> dict:
    """Stop the delivery mid-write: truncate the payload, withhold the sentinel.

    The cut is `floor(len * keep_fraction)` bytes - deliberately mid-line,
    because that is what a half-written file is, and because a reader choking
    on a short final record is the pathology worth reproducing. The byte count
    is a pure function of the file's own contents, so it reproduces exactly.

    `remove` withholds the delivery sentinel, so a poll that checks for one
    genuinely reports the vendor as not finished.
    """
    base = Path(base)
    match = params.get("match", "*.csv")
    remove = params.get("remove")
    frac = float(params.get("keep_fraction", 0.5))
    if not 0.0 <= frac < 1.0:
        raise BadFault(f"torn_write keep_fraction must be in [0, 1): {frac}")
    truncated, removed = [], []
    for rel in new_files:
        path = base / rel
        if not path.is_file():
            continue
        if remove and fnmatch(path.name, remove):
            path.unlink()
            removed.append(rel)
            continue
        if fnmatch(path.name, match):
            data = path.read_bytes()
            keep = int(len(data) * frac)
            if keep < len(data):
                path.write_bytes(data[:keep])
                truncated.append({"file": rel, "bytes_kept": keep,
                                  "bytes_lost": len(data) - keep})
    return {"truncated": truncated, "sentinels_withheld": removed}


def apply_arrival_effect(workspace: Path, fault: dict, before: set,
                         after: set) -> dict:
    """Post-process what the arrival hook just wrote. Returns an effect record."""
    new_files = sorted(after - before)
    if fault["kind"] == "duplicate_arrival":
        return apply_duplicate(workspace, new_files, fault["params"])
    if fault["kind"] == "torn_write":
        return apply_torn_write(workspace, new_files, fault["params"])
    return {}


# --------------------------------------------------------------------------
# the partial write
# --------------------------------------------------------------------------

def apply_partial_write(root: Path, params: dict) -> dict | None:
    """Drop the tail of the newest partition: a load that died mid-commit.

    Deterministic by construction: the surviving rows are the first
    `floor(n * keep_fraction)` in `order_column` order within the newest
    `partition_column` value. No sampling, no randomness - the interleave is
    staged, not raced (module rule 3), and this is the data half of that
    staging. Returns None when there is nothing to trim, so the caller can tell
    "the write was partial" from "there was no write".
    """
    from . import executor            # local: executor imports nothing from here

    table = params.get("table")
    order = params.get("order_column")
    if not table or not order:
        raise BadFault("partial_write needs `table` and `order_column`")
    part = params.get("partition_column")
    frac = float(params.get("keep_fraction", 0.5))
    if not 0.0 <= frac < 1.0:
        raise BadFault(f"partial_write keep_fraction must be in [0, 1): {frac}")

    con = executor._connect(root, read_only=False)
    try:
        if part:
            value = con.execute(f"select max({part}) from {table}").fetchone()[0]
            if value is None:
                return None
            where, args = f"{part} = ?", [value]
        else:
            value, where, args = None, "1 = 1", []
        keys = [r[0] for r in con.execute(
            f"select {order} from {table} where {where} order by {order}",
            args).fetchall()]
        if not keys:
            return None
        keep = int(len(keys) * frac)
        if keep >= len(keys):
            return None
        con.execute(f"delete from {table} where {where} and {order} >= ?",
                    args + [keys[keep]])
        return {"table": table,
                "partition": None if value is None else str(value),
                "rows_kept": keep, "rows_lost": len(keys) - keep}
    finally:
        con.close()
