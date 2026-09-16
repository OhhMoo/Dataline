"""Virtual clock for Airflow - the R004 section 2.4 shim, made reusable.

WHY THIS EXISTS
---------------
`var/sched/state.json` is this world's single time authority. Airflow, left
alone, reads the host wall clock in a handful of places on the execution path,
and every one of those reads is a determinism leak: a `run_id` with a
microsecond suffix, a `hostname` that is the container id, a `task_instance.id`
that is a fresh uuid7, a retry that really sleeps `retry_delay` seconds.

R004 measured the whole of that surface on the `dag.test()` path and found it
collapses to four names. This module patches exactly those four and nothing
else. The patch is a dispatcher that is virtual only inside `vclock.at(...)`,
so importing this file and calling `install()` changes nothing outside a
scope - see "WHY THE PATCH IS A PERMANENT DISPATCHER" below for why it cannot
be installed and removed per scope.

WHAT IT PATCHES (and why each one)
----------------------------------
  airflow.utils.timezone.utcnow   the one clock read on the core execution path
  airflow.utils.net.get_hostname  stamped onto every task_instance
  airflow.utils.net.getfqdn       same, via a different caller
  uuid6.uuid7                     task_instance.id, and it is time-seeded

`utcnow` is imported BY NAME into several airflow modules
(`from airflow.utils.timezone import utcnow`), so patching the defining module
is not enough - the from-imported copies must be rebound too. `_rebind` walks
`sys.modules` and replaces every attribute that is still the original object.

WHY THE PATCH IS A PERMANENT DISPATCHER, NOT A SCOPED SWAP (measured)
--------------------------------------------------------------------
The first version of this file installed the patch on entry to `at()` and
removed it on exit. It left `task_instance.updated_at` and
`dag_run.updated_at` reading the HOST clock, measured:

    "scheduled_dttm": "2026-03-04 01:00:15.000000"   <- virtual, good
    "updated_at":     "2026-09-15 07:55:07.350308"   <- host clock

Cause: those columns are declared
`Column(UtcDateTime(), default=timezone.utcnow, onupdate=timezone.utcnow)`,
so SQLAlchemy captures the *function object* when `airflow.models` is first
imported. A later rebind of the module attribute cannot reach a reference
already stored inside a Column. R004's shim did not hit this because its
comment says it verbatim - "imported before any airflow.models module" - and
that ordering requirement is easy to lose in a library.

So `install()` installs `_clock`, a permanent dispatcher, ONCE, and the right
time to call it is before `airflow.models` is imported. `_clock` returns
virtual time while an `at()` scope is active and the real wall clock
otherwise, so installing it early changes nothing outside a scope.

THE CLOCK MUST ADVANCE, NOT FREEZE (R004 section 2.5)
-----------------------------------------------------
A `vnow()` that returns a constant makes `end_date + retry_delay` never arrive
and `dag.test` spins forever. So `vnow()` is monotone: `tick_seconds` of virtual
time per call (default one second), from a counter that resets at the start of
every `at()` scope. Two identical runs make the same number of calls in the same
order and therefore see the same timestamps - which is what makes the A/B
determinism check pass, at any tick size.

WHY THE TICK SIZE IS A KNOB (measured - it is the settle-window tax)
--------------------------------------------------------------------
`mainframe_batch`'s `claims_nightly` holds a `TimeDeltaSensor(delta=45min,
mode="reschedule")`. Its success condition is `utcnow() > data_interval_end +
delta`, i.e. 2700 virtual seconds must elapse. At one virtual second per call
that is 2700 `utcnow()` calls, and `dag.test`'s wait loop between reschedules
sleeps on the HOST clock, so the virtual seconds are bought with real ones.

Measured, Airflow 2.10.5, the real DAG with `SETTLE_MINUTES` cut to 2:

    virtual seconds consumed: 179     task_reschedule rows: 2
    WALL_SECONDS=57                   dag.test returned: success

Three pokes to close a 120-second window: each reschedule cycle advances the
virtual clock by about `poke_interval` (60 s) and costs about fifteen seconds of
wall clock. The shipped 45-minute window is therefore ~45 cycles, and the same
run at `SETTLE_MINUTES = 45` had not closed after 150 seconds of wall clock and
ten pokes.

A bigger tick fixes this without touching the DAG, and without weakening
anything: determinism comes from *the same number of calls in the same order*,
not from the size of the step. `at(vts, tick_seconds=60)` closes the settle
window in ~45 calls instead of ~2700.

The cost of a big tick is that a run consumes more virtual time, so keep it well
under the gap to the next occurrence, and remember the warning below: none of
these stamps may be promoted to the ledger.

NEVER PROMOTE THESE TIMESTAMPS (R004 section 2.5)
--------------------------------------------------
How much virtual time a run consumes is a function of how many times Airflow
happened to call `utcnow()`. That is stable for a fixed Airflow version and
will change on an upgrade. These stamps are decoration on the catalogue.
`vnow` after `advance()` stays exactly `start + hours`, from the ledger.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import sys
import uuid as _uuid

__all__ = ["at", "install", "uninstall", "vnow", "base", "ticks", "elapsed",
           "frozen", "FrozenClockError"]

# The uuid7 prefix the shim hands out. Arbitrary but fixed; it is recognisable
# in a dump (01a0...) and it sorts stably.
_UUID7_PREFIX = 0x01A0


class FrozenClockError(AssertionError):
    """A wall-clock read escaped the shim."""


class _State:
    base = None
    tick = 0
    tick_seconds = 1
    handed_out = []
    saved = []          # [(module, attr, original), ...]
    real_utcnow = None  # airflow.utils.timezone.utcnow, as shipped
    real_getfqdn = None
    real_uuid7 = None


_S = _State()


def base():
    if _S.base is None:
        raise RuntimeError("vclock is not active; use `with vclock.at(vts):`")
    return _S.base


def ticks() -> int:
    """How many clock reads this scope has served (NOT virtual seconds)."""
    return _S.tick


def elapsed() -> int:
    """Virtual seconds this scope has consumed: `ticks() * tick_seconds`."""
    return _S.tick * _S.tick_seconds


def vnow():
    """The next virtual instant. Monotone, `tick_seconds` per call."""
    if _S.base is None:
        raise RuntimeError("vclock is not active; use `with vclock.at(vts):`")
    _S.tick += 1
    ts = _S.base + _dt.timedelta(seconds=_S.tick * _S.tick_seconds)
    _S.handed_out.append(ts)
    return ts


def _clock():
    """The permanent replacement for `airflow.utils.timezone.utcnow`.

    Virtual while an `at()` scope is active; the real clock otherwise. Being
    permanent is the point: SQLAlchemy Column defaults capture this object.
    """
    if _S.base is None:
        return _S.real_utcnow()
    return vnow()


def _vuuid7():
    if _S.base is None:
        return _S.real_uuid7()
    _S.tick += 1
    return _uuid.UUID(int=(_UUID7_PREFIX << 112) | _S.tick)


def _const_host(*a, **k):
    # Outside a scope, fall back to the real `socket.getfqdn`, NOT to the real
    # `get_hostname`. Measured: `airflow.utils.net.get_hostname` resolves its
    # default through `conf.getimport("core", "hostname_callable",
    # fallback="airflow.utils.net.getfqdn")`, which looks the name up again in
    # the (patched) module and recurses:
    #     RecursionError: maximum recursion depth exceeded
    if _S.base is None:
        return _S.real_getfqdn()
    return "airflow-worker"


def _rebind(original, replacement, name):
    """Replace `name` with `replacement` in every module that holds `original`.

    Airflow does `from airflow.utils.timezone import utcnow` in several modules
    (airflow/timetables/trigger.py among them, R004 section 2.3). Patching only
    the defining module leaves those copies pointing at the real clock.
    """
    saved = []
    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        # Scoped to Airflow and uuid6 on purpose. `airflow.utils.net.getfqdn`
        # IS `socket.getfqdn`, and rebinding it in `socket` would change the
        # meaning of getfqdn for the whole process, which is far more than we
        # are entitled to do.
        root = mod_name.split(".")[0]
        if root not in ("airflow", "uuid6"):
            continue
        try:
            if getattr(mod, name, None) is original:
                saved.append((mod, name, original))
                setattr(mod, name, replacement)
        except Exception:      # modules with exotic __getattr__
            continue
    return saved


def install():
    """Install the dispatchers. Call BEFORE `import airflow.models`.

    Idempotent, and a no-op outside an `at()` scope, so calling it at the top
    of a process that also does ordinary Airflow work is safe.
    """
    if _S.saved:
        return

    import airflow.utils.timezone as aftz
    import airflow.utils.net as afnet
    _S.real_utcnow = aftz.utcnow
    _S.real_getfqdn = afnet.getfqdn

    saved = []
    saved += _rebind(aftz.utcnow, _clock, "utcnow")
    saved += _rebind(afnet.get_hostname, _const_host, "get_hostname")
    saved += _rebind(afnet.getfqdn, _const_host, "getfqdn")
    try:
        import uuid6
        _S.real_uuid7 = uuid6.uuid7
        saved += _rebind(uuid6.uuid7, _vuuid7, "uuid7")
    except ImportError:
        pass                                               # Airflow 2.x
    _S.saved = saved


def uninstall():
    """Restore the shipped functions. Rarely what you want - see `install`."""
    for mod, attr, original in reversed(_S.saved):
        try:
            setattr(mod, attr, original)
        except Exception:
            pass
    _S.saved = []


@contextlib.contextmanager
def at(vts, tick_seconds=1):
    """Run a block with Airflow reading virtual time starting at `vts`.

    `vts` may be a datetime or an ISO-8601 string. The tick counter resets on
    entry, so two identical blocks see identical timestamps - that is the whole
    determinism argument, and it holds at any `tick_seconds`.

    `tick_seconds` is how much virtual time one clock read buys. Leave it at 1
    unless the DAG contains a time-based sensor; see "WHY THE TICK SIZE IS A
    KNOB" in the module docstring for the measurement that motivates it.

    `install()` is called here as a fallback, but by then `airflow.models` may
    already be imported and the SQLAlchemy `updated_at` defaults will have
    captured the real clock (see the module docstring). Call `install()` at
    process start, before importing Airflow models, for a clean dump.
    """
    if isinstance(vts, str):
        vts = _dt.datetime.fromisoformat(vts.replace("Z", "+00:00"))
    if vts.tzinfo is None:
        vts = vts.replace(tzinfo=_dt.timezone.utc)

    if not isinstance(tick_seconds, int) or tick_seconds < 1:
        raise ValueError("tick_seconds must be a positive integer, not %r"
                         % (tick_seconds,))
    install()
    prev = (_S.base, _S.tick, _S.handed_out, _S.tick_seconds)
    _S.base, _S.tick, _S.handed_out, _S.tick_seconds = vts, 0, [], tick_seconds
    try:
        yield _S
    finally:
        _S.base, _S.tick, _S.handed_out, _S.tick_seconds = prev


def activate(vts, tick_seconds=1):
    """Pin the clock for the rest of the process. No scope, no restore.

    `at()` is what library code should use. `activate()` exists for the one
    case that has no enclosing block: making the real `airflow` CLI
    deterministic, from a `sitecustomize.py` that runs before Airflow is
    imported. See `from_env`.
    """
    if isinstance(vts, str):
        vts = _dt.datetime.fromisoformat(vts.replace("Z", "+00:00"))
    if vts.tzinfo is None:
        vts = vts.replace(tzinfo=_dt.timezone.utc)
    install()
    _S.base, _S.tick, _S.handed_out = vts, 0, []
    _S.tick_seconds = int(tick_seconds)
    return vts


def from_env(var="ALKERA_VNOW", tick_var="ALKERA_VTICK"):
    """Activate from an environment variable, or do nothing if it is unset.

    Put this in a `sitecustomize.py` on the episode container's PYTHONPATH and
    every `airflow ...` invocation the agent types runs on virtual time - which
    is what makes `airflow dags test` produce
    `manual__2026-03-04T01:00:00+00:00` instead of a wall-clock run_id
    (R004 section 2.2 measured the CLI producing the latter).
    """
    import os
    v = os.environ.get(var)
    if not v:
        return None
    return activate(v, int(os.environ.get(tick_var) or 1))


@contextlib.contextmanager
def frozen(window_hours=24.0):
    """Assert, on exit, that nothing inside read the host wall clock.

    Two checks, both on the timestamps actually handed out:

      1. every one lies in [base, base + window_hours] - a host-clock read
         would be years away from a 2026-03 virtual base;
      2. none is within 10 minutes of the real `datetime.now(utc)`.

    This is an assertion, not a sandbox: it catches a leak that surfaced in a
    timestamp we handed out. A module that calls `datetime.now()` directly and
    never asks us (the Airflow 3 Task SDK does exactly this, R004 section 2.4)
    is invisible here and is handled by normalisation instead.
    """
    if _S.base is None:
        raise RuntimeError("vclock.frozen() requires an active vclock.at()")
    start_n = len(_S.handed_out)
    try:
        yield
    finally:
        real = _dt.datetime.now(_dt.timezone.utc)
        lo = _S.base
        hi = _S.base + _dt.timedelta(hours=window_hours)
        for ts in _S.handed_out[start_n:]:
            if not (lo <= ts <= hi):
                raise FrozenClockError(
                    "timestamp %s is outside the virtual window [%s, %s]"
                    % (ts.isoformat(), lo.isoformat(), hi.isoformat()))
            if abs((ts - real).total_seconds()) < 600:
                raise FrozenClockError(
                    "timestamp %s is within 10 minutes of the host wall clock "
                    "(%s) - that is a leak" % (ts.isoformat(), real.isoformat()))
