"""sftp_vendor theme hooks: drop-folder generation, raw load, ingest seeding.

write_landing: one RNG per day keyed on (seed, day_index), so output does not
depend on iteration order. Each day lands a CSV plus a `<name>.done` sentinel
per feed. Amounts are integer cents rendered %.2f — no binary floats anywhere.
load_raw: runs the workspace's own loader/vendor_load.py via subprocess, so
the script the agent reads is the code that actually loads the drop folders.
write_ingest: seeds the connector control-plane state (var/ingest/state.json)
from the same per-day RNG, so sync history reconciles with raw exactly.
"""
import csv
import random
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from world.hashing import json_canon
from world import scheduler, workflow
from world.scheduler import iso, parse_vtime

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
    # newline="" or Windows text mode writes 53\r\n where Linux writes 53\n, and
    # the sentinel is fingerprinted workspace state. The .csv above already passes
    # newline=""; this line was the one that did not, and it broke cross-host
    # determinism for all 60 sentinels while the paired .csv stayed identical.
    (folder / f"{name}.done").write_text(
        f"{len(rows)}\n", encoding="utf-8", newline="")


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """One day's vendor delivery. Pure in (seed, day_index) - the live-world
    arrival stream and the initial build write identical bytes for day N."""
    drop = workspace / "data" / "drop"
    stamp = day.strftime("%Y%m%d")
    orders, returns = day_rows(seed["seed"], day_index, day, seed)
    _write_feed(drop / "acme", f"ORDERS_{stamp}", ORDER_FIELDS, orders)
    _write_feed(drop / "globex", f"RETURNS_{stamp}", RETURN_FIELDS, returns)
    return {"orders": len(orders), "returns": len(returns)}


def write_landing(workspace: Path, seed: dict) -> None:
    """Write data/drop/<vendor>/<FEED>_<YYYYMMDD>.csv (+ .done) per day."""
    start = parse_vtime(seed["vtime_start"])
    for i in range(seed["days"]):
        write_day(workspace, seed, i, (start + timedelta(days=i)).date())


def load_raw(workspace: Path, db_path: Path) -> None:
    loader = workspace / "loader" / "vendor_load.py"
    r = subprocess.run(
        [sys.executable, str(loader),
         "--drop", str(workspace / "data" / "drop"),
         "--db", str(db_path)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"vendor_load failed:\n{r.stdout}\n{r.stderr}")


def write_ingest(root: Path, seed: dict, manifest: dict) -> None:
    """Seed var/ingest/state.json per gateway/INGEST_CONTRACT.md.

    One green sync per connector per day, timed after sftp_poll and before
    vendor_load, with row counts from the same per-day RNG that wrote the
    drop folders — so the sync history agrees with raw exactly. A clean
    baseline: planted drift defects are Track C's job.
    """
    start = parse_vtime(seed["vtime_start"])
    specs = manifest["ingest"]["connectors"]
    connectors, schemas, runs = {}, [], []
    for cid, spec in specs.items():
        full = f"{spec['destination']['schema']}.{spec['destination']['tables'][0]}"
        connectors[cid] = {
            "name": spec["name"],
            "source": spec["source"],
            "destination": spec["destination"],
            "status": spec["status"],
            "schedule": spec["schedule"],
            "sync": {"duration_s": spec["duration_s"],
                     "tables": {full: {"rows_inserted": spec["rows_per_sync"],
                                       "rows_updated": 0}}},
        }
        schemas.append({
            "connector_id": cid, "table": full, "columns": spec["columns"],
            "column_added": spec["column_added"],
            "column_removed": spec["column_removed"],
        })
    for i in range(seed["days"]):
        day = (start + timedelta(days=i)).date()
        orders, returns = day_rows(seed["seed"], i, day, seed)
        daily = {"orders": orders, "returns": returns}
        for cid, spec in specs.items():
            hh, mm = (int(x) for x in spec["time"].split(":"))
            started = start + timedelta(days=i + 1, hours=hh, minutes=mm)
            finished = started + timedelta(seconds=spec["duration_s"])
            full = f"{spec['destination']['schema']}.{spec['destination']['tables'][0]}"
            runs.append({
                "id": f"sync-{len(runs) + 1:06d}",
                "connector_id": cid,
                "started_at": iso(started),
                "finished_at": iso(finished),
                "status": "success",
                "tables": {full: {"rows_inserted": len(daily[spec["feed"]]),
                                  "rows_updated": 0}},
                "timeline": [{"at": iso(started), "event": "started"},
                             {"at": iso(finished), "event": "succeeded"}],
            })
    state = {"connectors": connectors, "runs": runs, "schemas": schemas, "alerts": []}
    path = root / "var" / "ingest" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_canon(state) + b"\n")


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    """Seed var/workflow/{issues,chat,kb}.json per gateway/WORKFLOW_CONTRACT.md.

    The corpus is authored as data in this theme's workflow/ directory and
    dated against the world clock, which is read out of the scheduler state
    `build` has already written rather than re-derived from the seed - one
    definition of `vnow`, so a ticket cannot be filed after today.
    """
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
