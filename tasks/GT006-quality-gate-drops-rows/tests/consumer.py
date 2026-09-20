#!/usr/bin/env python3
"""
GT006 consumer reading - one scheduler night on a scratch copy, and the hash of
what the board pack would read the next morning.

    from consumer import make_advanceable, run_night, business_hash, receipts

WHY A SCRATCH NIGHT. The consumer check of this task (DECISION_TEMPLATE §4) reads
the consumer-visible table as a whole object: `warehouse.bi_payments_extract`, which
`refresh_bi_extract` rebuilds from the KPI at 02:45. An agent that patches the
KPI and leaves the extract is right until 02:45 and wrong after; an agent that
patches the extract by hand and leaves the KPI is the other way round. Neither is
what the consumer reads. So the grader copies the world the agent left behind,
lets the scheduler run one night over it, and hashes the extract the consumer
would open at 09:00 the next day. The agent's world is never touched.

WHY `make_advanceable`. The shipped `var/world/live.json` carries only each
job's `exec` block (that is all `sched_trigger` needs). `scheduler.advance`
additionally plans from `vtime_start` and each job's `time`/`day_offset`/
`duration_s`, which the schedule in `var/sched/state.json` already carries. This
copies them across, adds nothing else, and is idempotent: on the decision arm
the world ships advance-capable (fixtures/variants/decision/schedule.yml) and the
call is a no-op.

`land_payments` is `kind: noop`, which the executor does not implement; on the
scratch night it is recorded as `unsupported` and nothing new lands. The extract
the consumer reads is therefore exactly the KPI the agent left, refreshed - which
is the point.
"""
from __future__ import annotations

import decimal
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent

LIVE = Path("var") / "world" / "live.json"
STATE = Path("var") / "sched" / "state.json"
RECEIPTS = Path("var") / "ledger" / "receipts.jsonl"

# HIST_START at midnight, so planned days align with the payments calendar.
# Only the alignment matters for a scratch night; the value is the fixture's.
DEFAULT_VTIME_START = "2025-09-15T00:00:00Z"
DEFAULT_DAYS_LANDED = 181
NIGHT_HOURS = 24


def _canon(doc) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def make_advanceable(root: Path, schedule: dict | None = None) -> dict:
    """Fill var/world/live.json so `scheduler.advance` can plan a night.

    Adds only what is missing; never changes an `exec` block. `schedule` is the
    decision overlay's schedule.yml (`live:` and `jobs.<name>.publishes`), or
    None for the control arm's defaults. Returns the live block written.
    """
    root = Path(root)
    state = json.loads((root / STATE).read_text(encoding="utf-8"))
    live_path = root / LIVE
    live = (json.loads(live_path.read_text(encoding="utf-8"))
            if live_path.is_file() else {"jobs": {}})
    sched = schedule or {}
    live_defaults = sched.get("live") or {}
    live.setdefault("vtime_start",
                    live_defaults.get("vtime_start", DEFAULT_VTIME_START))
    live.setdefault("days_landed",
                    int(live_defaults.get("days_landed", DEFAULT_DAYS_LANDED)))
    live.setdefault("jobs", {})
    for name, job in state.get("jobs", {}).items():
        entry = live["jobs"].setdefault(name, {})
        entry.setdefault("time", job.get("time", "02:00"))
        entry.setdefault("day_offset", 0)
        entry.setdefault("duration_s", int(job.get("duration_s", 60)))
        entry.setdefault("detail", job.get("command", name))
        if "exec" not in entry and job.get("exec"):
            entry["exec"] = job["exec"]
        publishes = ((sched.get("jobs") or {}).get(name) or {}).get("publishes")
        if publishes:
            entry["publishes"] = publishes
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_bytes(_canon(live) + b"\n")
    return live


def is_advanceable(root: Path) -> bool:
    live_path = Path(root) / LIVE
    if not live_path.is_file():
        return False
    live = json.loads(live_path.read_text(encoding="utf-8"))
    if "vtime_start" not in live or "days_landed" not in live:
        return False
    return all({"time", "day_offset", "duration_s"} <= set(j)
               for j in (live.get("jobs") or {}).values())


def declared_publishes(root: Path, job: str) -> list[dict]:
    live_path = Path(root) / LIVE
    if not live_path.is_file():
        return []
    live = json.loads(live_path.read_text(encoding="utf-8"))
    return list(((live.get("jobs") or {}).get(job) or {}).get("publishes") or [])


def run_night(root: Path, hours: float = NIGHT_HOURS) -> dict:
    """`world.scheduler.advance` on `root`, which MUST be a scratch copy.

    Returns the scheduler's own result ({"vnow", "live", "events"}). Raises
    whatever the scheduler raises; the caller decides whether that is the
    agent's doing (a world the scheduler cannot run) or ours.
    """
    sys.path.insert(0, str(REPO / "world"))
    try:
        from world import scheduler
    finally:
        sys.path.pop(0)
    return scheduler.advance(Path(root), hours)


def receipts(root: Path) -> list[dict]:
    path = Path(root) / RECEIPTS
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# --------------------------------------------------------------- hashing
def _canon_value(value):
    """Type-insensitive: DECIMAL(16,2) 12.30 and DOUBLE 12.3 hash the same, a
    DATE and its ISO string hash the same. A repair that rebuilt the extract
    with `CREATE TABLE AS` from a differently-typed expression is still the
    same reading to the consumer."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, (float, decimal.Decimal)):
        d = decimal.Decimal(str(value)).quantize(decimal.Decimal("0.0001"))
        return format(d.normalize(), "f") if d != 0 else "0"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def business_hash(con, relation: str, columns: list[str]) -> tuple[str, int]:
    """sha256 over the sorted, canonicalised rows of `relation` projected to
    `columns`. Returns (hash, row_count); ("absent", 0) when the relation is
    not there."""
    try:
        rows = con.execute(
            f"SELECT {', '.join(columns)} FROM {relation}").fetchall()
    except Exception:                                   # noqa: BLE001 - absent
        return "absent", 0
    lines = sorted("\x1f".join(_canon_value(v) for v in row) for row in rows)
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest(), len(rows)
