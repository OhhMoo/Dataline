"""revenue_daily theme hooks: landing-zone generation + raw load.

write_landing: one RNG per day keyed on (seed, day_index), so output does not
depend on iteration order. Amounts are integer cents rendered %.2f — no binary
floats anywhere in the pipeline.
load_raw: hive-partitioned CSVs -> raw.daily_revenue; PRAGMA threads=1 +
ORDER BY so duckdb page layout is deterministic.
"""
import csv
import random
from datetime import timedelta
from pathlib import Path

import duckdb

from world import scheduler, workflow
from world.scheduler import parse_vtime

FIELDS = ["order_id", "order_date", "customer_id", "amount"]


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


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """One day's landing partition. A pure function of (seed, day_index): the
    live-world arrival stream calls this for day N and gets the same bytes the
    initial build would have written, whatever order it got to N in."""
    d = workspace / "data" / "landing" / "daily_revenue" / f"dt={day}"
    d.mkdir(parents=True, exist_ok=True)
    rows = day_rows(seed["seed"], day_index, day, seed)
    with (d / "revenue.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return {"partition": f"dt={day}", "rows": len(rows)}


def write_landing(workspace: Path, seed: dict) -> None:
    """Write data/landing/daily_revenue/dt=YYYY-MM-DD/revenue.csv for each day."""
    start = parse_vtime(seed["vtime_start"])
    for i in range(seed["days"]):
        write_day(workspace, seed, i, (start + timedelta(days=i)).date())


def load_raw(workspace: Path, db_path: Path) -> None:
    glob = (workspace / "data" / "landing" / "daily_revenue").as_posix() + "/dt=*/*.csv"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.daily_revenue AS
            SELECT cast(order_id as integer) AS order_id,
                   cast(order_date as date) AS order_date,
                   customer_id, amount, dt
            FROM read_csv('{glob}', hive_partitioning=true, columns={{
                'order_id': 'VARCHAR', 'order_date': 'VARCHAR',
                'customer_id': 'VARCHAR', 'amount': 'VARCHAR'}})
            ORDER BY order_date, order_id""")
    finally:
        con.close()


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    """Seed var/workflow/{issues,chat,kb}.json per gateway/WORKFLOW_CONTRACT.md.

    The corpus is authored as data in this theme's workflow/ directory and
    dated against the world clock, which is read out of the scheduler state
    `build` has already written rather than re-derived from the seed - one
    definition of `vnow`, so a ticket cannot be filed after today.
    """
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
