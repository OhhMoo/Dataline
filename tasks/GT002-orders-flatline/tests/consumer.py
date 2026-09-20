#!/usr/bin/env python3
"""
GT002 consumer reading - one scheduler night on a scratch copy of the graded
world, and a type-insensitive hash of what the board reads afterwards.

    from consumer import run_night, receipts, business_hash, KPI_COLUMNS

WHY A SCRATCH NIGHT. The consumer check of the decision arm (DECISION_TEMPLATE
section 4) reads the consumer-visible object as a whole: on this task the exec
revenue board reads `warehouse.kpi_orders_daily`, and on the decision arm the
`exec_revenue_board` job snapshots it into `warehouse.board_pack_orders` at
06:30 and PUBLISHES it, so the world's own ledger writes the receipt. The
grader copies the world the agent left behind, lets `world.scheduler.advance`
run one night over the copy, and reads the receipt and the snapshot. The
agent's world is never touched, and the agent cannot write the receipt.

On the control arm there is no live-world control plane (var/world/live.json
is absent), so `advance` moves the clock and executes nothing; the grader then
hashes the view directly. Same rule, one fewer moving part.

The hash is TYPE-INSENSITIVE: DECIMAL(12,2) 12.30 and DOUBLE 12.3 read the
same, a DATE and its ISO string read the same. A repair that rebuilt a table
with a differently-typed expression is still the same reading to the consumer.
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

NIGHT_HOURS = 24
KPI_COLUMNS = ["dt", "channel_group", "order_count", "order_value"]
KPI_VIEW = "warehouse.kpi_orders_daily"
BOARD_TABLE = "warehouse.board_pack_orders"


def is_advanceable(root: Path) -> bool:
    live_path = Path(root) / LIVE
    if not live_path.is_file():
        return False
    live = json.loads(live_path.read_text(encoding="utf-8"))
    if "vtime_start" not in live or "days_landed" not in live:
        return False
    return all({"time", "day_offset", "duration_s"} <= set(j)
               for j in (live.get("jobs") or {}).values())


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
def canon_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, decimal.Decimal)):
        d = decimal.Decimal(str(value)).quantize(decimal.Decimal("0.0001"))
        return format(d.normalize(), "f") if d != 0 else "0"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def rows_hash(rows) -> str:
    """sha256 over the sorted, canonicalised rows."""
    lines = sorted("\x1f".join(canon_value(v) for v in row) for row in rows)
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def business_hash(con, relation: str, columns: list[str]) -> tuple[str, int]:
    """(hash, row_count) of `relation` projected to `columns`; ("absent", 0)
    when the relation is not there or cannot be read."""
    try:
        rows = con.execute(
            f"SELECT {', '.join(columns)} FROM {relation}").fetchall()
    except Exception:                                   # noqa: BLE001 - absent
        return "absent", 0
    return rows_hash(rows), len(rows)


def kpi_rows(con, relation: str) -> dict[str, list[tuple]] | None:
    """The consumer object's rows keyed by day (ISO), canonicalised, or None
    when it cannot be read. Used for the per-day progress quantity."""
    try:
        rows = con.execute(
            f"SELECT {', '.join(KPI_COLUMNS)} FROM {relation}").fetchall()
    except Exception:                                   # noqa: BLE001 - absent
        return None
    by_day: dict[str, list[tuple]] = {}
    for row in rows:
        day = canon_value(row[0])
        # lists, not tuples: the decision arm's reference comes back from JSON
        by_day.setdefault(day, []).append([canon_value(v) for v in row])
    return {d: sorted(v) for d, v in by_day.items()}
