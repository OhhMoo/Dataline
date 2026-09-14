"""sftp_vendor theme hooks: drop-folder generation + raw load.

write_landing: one RNG per day keyed on (seed, day_index), so output does not
depend on iteration order. Each day lands a CSV plus a `<name>.done` sentinel
per feed. Amounts are integer cents rendered %.2f — no binary floats anywhere.
load_raw: runs the workspace's own loader/vendor_load.py via subprocess, so
the script the agent reads is the code that actually loads the drop folders.
"""
import csv
import random
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from world.scheduler import parse_vtime

ORDER_FIELDS = ["order_id", "order_date", "customer_id", "amount"]
RETURN_FIELDS = ["return_id", "order_id", "return_date", "amount"]


def day_rows(seed: int, day_index: int, day, params: dict) -> tuple[list, list]:
    rng = random.Random(f"{seed}:{day_index}")
    lo_n, hi_n = params["orders_per_day"]
    lo_r, hi_r = params["returns_per_day"]
    lo_c, hi_c = params["amount_cents"]
    orders = []
    for i in range(rng.randint(lo_n, hi_n)):
        orders.append({
            "order_id": day_index * 1000 + i,
            "order_date": day.isoformat(),
            "customer_id": f"C{rng.randrange(params['customers']):04d}",
            "amount": f"{rng.randint(lo_c, hi_c) / 100:.2f}",
        })
    returns = []
    for j in range(rng.randint(lo_r, hi_r)):
        returns.append({
            "return_id": day_index * 1000 + j,
            "order_id": orders[rng.randrange(len(orders))]["order_id"],
            "return_date": day.isoformat(),
            "amount": f"{rng.randint(lo_c, hi_c) / 100:.2f}",
        })
    return orders, returns


def _write_feed(folder: Path, name: str, fields: list, rows: list) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    (folder / f"{name}.done").write_text(f"{len(rows)}\n", encoding="utf-8")


def write_landing(workspace: Path, seed: dict) -> None:
    """Write data/drop/<vendor>/<FEED>_<YYYYMMDD>.csv (+ .done) per day."""
    start = parse_vtime(seed["vtime_start"])
    drop = workspace / "data" / "drop"
    for i in range(seed["days"]):
        day = (start + timedelta(days=i)).date()
        stamp = day.strftime("%Y%m%d")
        orders, returns = day_rows(seed["seed"], i, day, seed)
        _write_feed(drop / "acme", f"ORDERS_{stamp}", ORDER_FIELDS, orders)
        _write_feed(drop / "globex", f"RETURNS_{stamp}", RETURN_FIELDS, returns)


def load_raw(workspace: Path, db_path: Path) -> None:
    loader = workspace / "loader" / "vendor_load.py"
    r = subprocess.run(
        [sys.executable, str(loader),
         "--drop", str(workspace / "data" / "drop"),
         "--db", str(db_path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"vendor_load failed:\n{r.stdout}\n{r.stderr}")
