"""revenue_live theme hooks: the pilot dynamic world DW-1 (tasks/DW001-live-revenue).

Derived from revenue_daily. Same arrival stream, same schedule, same keyed
PRNG per (seed, day_index); three differences, each one a LATENT DEFECT
ported from the stressor registry (tasks/stressors/registry.yaml). A defect
does nothing on its own; it turns a shock into damage. AUTHOR-SIDE: this file
is not copied into the world, and nothing in workspace/ names a defect.

1. exactly_once  (registry: retry_backfill_doubling, the GT004/GT001 family)
   workspace/dbt_project/models/warehouse/fct_revenue_lines.sql does not
   deduplicate `order_id`. Shock: `duplicate_arrival` - the day's CSV is
   delivered twice and the loader's `*.csv` glob reads the sibling copy, so
   every line of that day is in the fact twice.
2. liveness      (registry: checkpoint_past_an_unread_partition, GT009)
   workspace/loader/load_daily_revenue.py is an incremental loader that loads
   partitions newer than a checkpoint and advances the checkpoint to the
   newest partition it saw. Shock: `late_arrival` with a delay longer than a
   day - the next day's partition is loaded first, the checkpoint moves past
   the late one, and it is never read.
3. consumer_consistency (registry: null_unsafe_not_in_quality_gate, GT006)
   workspace/dbt_project/models/staging/stg_revenue.sql gates out test-harness
   traffic with `terminal_ref NOT IN (...)`, which also drops every line whose
   `terminal_ref` is NULL. Continuous: no shock needed (the control).

`terminal_ref` is the one new landing column: NULL for a seeded fraction of
rows (card-not-present orders from the legacy acquirer carry none), a harness
marker for a smaller fraction (rows the gate is right to drop), and a terminal
id otherwise.
"""
import csv
import importlib.util
import random
from datetime import timedelta
from pathlib import Path

from world.scheduler import parse_vtime

FIELDS = ["order_id", "order_date", "customer_id", "amount", "terminal_ref"]

# Per-row probabilities, drawn from the day's own PRNG. Constants, not seed
# keys: the live control plane carries only the seed keys theme.yaml lists,
# and these are the world's shape rather than a knob.
TERMINAL_NULL_P = 0.02           # legacy acquirer, card-not-present: no terminal
TERMINAL_HARNESS_P = 0.02        # payments test-harness traffic
HARNESS_REFS = ("TEST-HARNESS", "SMOKE-TEST")


def day_rows(seed: int, day_index: int, day, params: dict) -> list[dict]:
    rng = random.Random(f"{seed}:{day_index}")
    lo_n, hi_n = params["orders_per_day"]
    lo_c, hi_c = params["amount_cents"]
    rows = []
    for i in range(rng.randint(lo_n, hi_n)):
        row = {
            "order_id": day_index * 1000 + i,
            "order_date": day.isoformat(),
            "customer_id": f"C{rng.randrange(params['customers']):04d}",
            "amount": f"{rng.randint(lo_c, hi_c) / 100:.2f}",
        }
        u = rng.random()
        if u < TERMINAL_NULL_P:
            row["terminal_ref"] = ""                 # empty CSV field -> NULL
        elif u < TERMINAL_NULL_P + TERMINAL_HARNESS_P:
            row["terminal_ref"] = HARNESS_REFS[rng.randrange(len(HARNESS_REFS))]
        else:
            row["terminal_ref"] = f"T{rng.randrange(400):04d}"
        rows.append(row)
    return rows


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """One day's landing partition; a pure function of (seed, day_index)."""
    d = workspace / "data" / "landing" / "daily_revenue" / f"dt={day}"
    d.mkdir(parents=True, exist_ok=True)
    rows = day_rows(seed["seed"], day_index, day, seed)
    with (d / "revenue.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return {"partition": f"dt={day}", "rows": len(rows)}


def write_landing(workspace: Path, seed: dict) -> None:
    start = parse_vtime(seed["vtime_start"])
    for i in range(seed["days"]):
        write_day(workspace, seed, i, (start + timedelta(days=i)).date())


def load_raw(workspace: Path, db_path: Path) -> None:
    """The initial load runs the world's OWN loader - the one daily_load runs
    - so the checkpoint it leaves at build is the one the episode inherits."""
    path = Path(workspace) / "loader" / "load_daily_revenue.py"
    spec = importlib.util.spec_from_file_location("revenue_live_loader", path)
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    loader.load(Path(workspace) / "data" / "landing" / "daily_revenue", Path(db_path))
