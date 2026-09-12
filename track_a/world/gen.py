"""Deterministic landing-zone data generation.

One RNG per day keyed on (seed, day_index), so output does not depend on
iteration order. Amounts are integer cents rendered with %.2f — no binary
floats anywhere in the pipeline.
"""
import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIELDS = ["order_id", "order_date", "customer_id", "amount"]


def parse_vtime(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_rows(seed: int, day_index: int, day, params: dict) -> list[dict]:
    rng = random.Random(f"{seed}:{day_index}")
    lo_n, hi_n = params["orders_per_day"]
    lo_c, hi_c = params["amount_cents"]
    rows = []
    for i in range(rng.randint(lo_n, hi_n)):
        rows.append({
            "order_id": day_index * 1000 + i,
            "order_date": day.isoformat(),
            "customer_id": f"C{rng.randrange(params['customers']):04d}",
            "amount": f"{rng.randint(lo_c, hi_c) / 100:.2f}",
        })
    return rows


def write_landing(workspace: Path, seed: dict) -> list[Path]:
    """Write data/landing/daily_revenue/dt=YYYY-MM-DD/revenue.csv for each day."""
    start = parse_vtime(seed["vtime_start"])
    out = []
    for i in range(seed["days"]):
        day = (start + timedelta(days=i)).date()
        d = workspace / "data" / "landing" / "daily_revenue" / f"dt={day}"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "revenue.csv"
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
            w.writeheader()
            w.writerows(day_rows(seed["seed"], i, day, seed))
        out.append(p)
    return out
