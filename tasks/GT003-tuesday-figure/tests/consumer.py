#!/usr/bin/env python3
"""
GT003 consumer reading - one scheduler night on a scratch copy of the world,
and whole-object hashes of what a reader would see.

    from consumer import run_night, receipts, business_hash, pack_sql, ...

WHY A SCRATCH NIGHT (decision arm). The board pack is not the mart: it is
`finance.board_pack_daily`, which `board_pack_publish` republishes from the
mart at 06:00 for the OPEN reporting month. An agent that repaired the mart is
right for the pack only after the next pack run, and an agent that patched the
pack table by hand and left the mart is right until 06:00 and wrong after. So
the grader copies the world the agent left, lets `world.scheduler.advance` run
one night over the copy (23:07 `mart_hourly_rebuild` with the agent's OWN
rebuild script, 06:00 `board_pack_publish` with the agent's OWN pack script),
then hashes the pack table as a whole object. The night writes the consumer's
`publishes:` receipt into var/ledger/receipts.jsonl; the agent cannot.

Nothing here is decision-specific except that the control arm has no live plan
(no var/world/live.json), so a night over a control world moves the clock and
runs nothing - `advance` says so in its result rather than pretending.
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
WORKFLOW = Path("var") / "workflow"
WAREHOUSE = Path("var") / "warehouse" / "warehouse.duckdb"

NIGHT_HOURS = 24

# The pack table's business projection. `period_month` is derived, so it is
# excluded from the hash: two rows that agree on the day, the region and the
# figures are the same reading whatever month key a rewrite gave them.
PACK_TABLE = "finance.board_pack_daily"
PERIODS_TABLE = "finance.reporting_periods"
PACK_COLS = ["order_dt", "region", "revenue_usd", "order_count"]


def _canon(doc) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def pack_sql(catalog: str, where: str = "TRUE") -> str:
    """The pack job's own aggregation over `catalog`'s mart: one row per
    region per day. `where` filters on f.order_dt."""
    return (f"SELECT f.order_dt AS order_dt, d.region AS region, "
            f"ROUND(SUM(f.revenue_usd), 2)::DECIMAL(16,2) AS revenue_usd, "
            f"SUM(f.order_count)::BIGINT AS order_count "
            f"FROM {catalog}.warehouse.mart_revenue_fact f "
            f"JOIN {catalog}.warehouse.mart_revenue_dim d ON d.customer_id = f.customer_id "
            f"WHERE {where} GROUP BY 1, 2")


def staging_pack_sql(catalog: str, where: str = "TRUE") -> str:
    """The same reading taken from staging rather than the mart - what the
    pack said on the day a month closed, before any later mart rebuild."""
    return (f"SELECT l.order_dt AS order_dt, c.region AS region, "
            f"ROUND(SUM(l.amount_usd), 2)::DECIMAL(16,2) AS revenue_usd, "
            f"COUNT(*)::BIGINT AS order_count "
            f"FROM {catalog}.staging.stg_revenue_lines l "
            f"JOIN {catalog}.staging.stg_customer c USING (customer_id) "
            f"WHERE {where} GROUP BY 1, 2")


# ------------------------------------------------------------------ night
def live_plan(root: Path) -> dict | None:
    p = Path(root) / LIVE
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def is_advanceable(root: Path) -> bool:
    live = live_plan(root)
    if not live or "vtime_start" not in live or "days_landed" not in live:
        return False
    return all({"time", "day_offset", "duration_s"} <= set(j)
               for j in (live.get("jobs") or {}).values())


def declared_publishes(root: Path, job: str) -> list[dict]:
    live = live_plan(root) or {}
    return list(((live.get("jobs") or {}).get(job) or {}).get("publishes") or [])


def run_night(root: Path, hours: float = NIGHT_HOURS) -> dict:
    """`world.scheduler.advance` on `root`, which MUST be a scratch copy."""
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
        if line.strip():
            out.append(json.loads(line))
    return out


# ---------------------------------------------------------------- hashing
def _canon_value(value):
    """Type-insensitive: DECIMAL(16,2) 12.30 and DOUBLE 12.3 hash the same, a
    DATE and its ISO string hash the same. A table rebuilt with CREATE TABLE
    AS from a differently-typed expression is still the same reading."""
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


def business_hash(con, relation: str, columns: list[str]) -> tuple[str, int]:
    """sha256 over the sorted, canonicalised rows of `relation` projected to
    `columns`. ("absent", 0) when the relation cannot be read."""
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


def table_columns(con, catalog: str, schema: str, table: str) -> list[str]:
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_catalog = ? "
        "AND table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [catalog, schema, table]).fetchall()
    return [r[0] for r in rows]


def whole_table_hash(con, catalog: str, qualified: str) -> tuple[str, int]:
    """business_hash over EVERY column of `schema.table` in `catalog`."""
    schema, table = qualified.split(".", 1)
    cols = table_columns(con, catalog, schema, table)
    if not cols:
        return "absent", 0
    return business_hash(con, f"{catalog}.{schema}.{table}", cols)


def file_hash(path: Path) -> str:
    p = Path(path)
    if not p.is_file():
        return "absent"
    return hashlib.sha256(p.read_bytes()).hexdigest()
