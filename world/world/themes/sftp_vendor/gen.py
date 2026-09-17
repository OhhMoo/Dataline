"""sftp_vendor theme hooks: drop-folder generation, raw load, ingest seeding.

write_landing: one RNG per day keyed on (seed, day_index), so output does not
depend on iteration order. Each day lands a CSV plus a `<name>.done` sentinel
per feed. Amounts are integer cents rendered %.2f — no binary floats anywhere.
load_raw: runs the workspace's own loader/vendor_load.py via subprocess, so
the script the agent reads is the code that actually loads the drop folders.
write_ingest: seeds the connector control-plane state (var/ingest/state.json)
from the same per-day RNG, so sync history reconciles with raw exactly - and
from the same seeded-fault decision the scheduler's backstory reads, so a night
whose 05:30 poll failed is a night whose 05:31 sync failed, on one story.
"""
import csv
import random
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from world.hashing import json_canon
from world import faults, scheduler, workflow
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
        stdin=subprocess.DEVNULL,  # never inherit the MCP JSON-RPC pipe; see executor.py
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"vendor_load failed:\n{r.stdout}\n{r.stderr}")


def write_ingest(root: Path, seed: dict, manifest: dict) -> None:
    """Seed var/ingest/state.json per gateway/SURFACES.md#ingest.

    One sync per connector per day at its cron slot, with row counts from the
    same per-day RNG that wrote the drop folders — so the sync history agrees
    with raw exactly.

    **And with the scheduler.** This plane and `var/sched/state.json` describe
    the same nights, so they must not contradict each other about them. The
    nights the vendor delivered late are decided ONCE, by
    `faults.history_arrivals` over `faults.history_catalogue` — the same draw
    `initial_state` reads through `missed_by` — and this hook consumes that
    decision rather than making its own.

    What a late night looks like here, and why:

    - The 05:31 sync runs, finds no new file on the drop folder, and is
      recorded `failed` with zero rows and a `sync_failed` alert. MEASURED
      before this: it was recorded `success` with the night's row count, while
      `sched_logs` for the same night carried
      `data/drop holds 0 file(s) matching */*_20260201.done` from the 05:30
      poll. The connector was claiming rows out of files that were not on disk
      for another two hours.
    - `failed`, not `success` with zero rows, because zero rows already MEANS
      something on this feed: `returns_per_day: [0, 5]`, so globex legitimately
      syncs nothing on a quiet night. MEASURED in the shipped world:
      `sync-000008` is green with `rows_inserted: 0` for 2026-02-04, a night
      that arrived on time and simply had no returns in it. A green zero-row
      run on a withheld night would make "the vendor sent no returns" and "the
      file was not there" the same row; the contract's own vocabulary for the
      second one is `failed` plus a `sync_failed` alert.
    - The rows are not lost, they are LATE: the delivery lands 150 minutes
      after the slot, hours before the connector comes round again, so the next
      morning's sync finds two nights' files and inserts both. That is why the
      per-table totals still reconcile with raw exactly — the invariant this
      hook has always claimed — and why a late night reads, on this surface, as
      a failed sync followed by a double-sized one.

    A day carrying an arrival fault the backstory does NOT carry (`torn_write`,
    `duplicate_arrival`) is a normal night here, because the builder wrote that
    night's delivery whole; `history_arrivals` filters those out, so the two
    planes cannot disagree about which nights are which.

    One edge is left honest rather than papered over: if the LAST seeded night
    were late there would be no later sync inside the history to pick it up,
    and that night's rows would stay outstanding, so the totals would come in
    below raw by exactly that night. That would be the truth - the file landed
    after the last sync the history records - and it is what the surface should
    say. The shipped seed does not hit it (day 29 arrives on time; the late
    nights are day_index 0, 5, 13, 17, 19, 24), so the totals reconcile exactly
    and `test_ingest_state_seeded_and_consistent` asserts both halves.

    No alert or error text names the fault that caused it (faults.py rule): the
    agent meets a connector that found no file, exactly as it meets a poll that
    counted none.
    """
    start = parse_vtime(seed["vtime_start"])
    specs = manifest["ingest"]["connectors"]
    late = faults.history_arrivals(faults.history_catalogue(manifest, seed),
                                   seed["seed"], seed["days"])
    connectors, schemas, runs, alerts = {}, [], [], []
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
    # Row counts per data day, from the same per-day RNG as the drop folders.
    rows_per_day = {}
    for i in range(seed["days"]):
        day = (start + timedelta(days=i)).date()
        orders, returns = day_rows(seed["seed"], i, day, seed)
        rows_per_day[i] = {"orders": len(orders), "returns": len(returns)}
    # Data days delivered late and not yet picked up, per connector.
    owed = {cid: [] for cid in specs}
    for i in range(seed["days"]):
        day = (start + timedelta(days=i)).date()
        for cid, spec in specs.items():
            hh, mm = (int(x) for x in spec["time"].split(":"))
            started = start + timedelta(days=i + 1, hours=hh, minutes=mm)
            finished = started + timedelta(seconds=spec["duration_s"])
            full = f"{spec['destination']['schema']}.{spec['destination']['tables'][0]}"
            # Anything still owed from an earlier night IS on the folder by
            # now - a late delivery landed later that same morning, a full day
            # before this slot comes round again. Tonight's own file is there
            # unless tonight is the one that was withheld.
            found = owed[cid] + ([] if i in late else [i])
            owed[cid] = [i] if i in late else []
            run_id = f"sync-{len(runs) + 1:06d}"
            ok = bool(found)
            runs.append({
                "id": run_id,
                "connector_id": cid,
                "started_at": iso(started),
                "finished_at": iso(finished),
                "status": "success" if ok else "failed",
                "tables": {full: {
                    "rows_inserted": sum(rows_per_day[j][spec["feed"]]
                                         for j in found),
                    "rows_updated": 0}},
                "timeline": [{"at": iso(started), "event": "started"},
                             {"at": iso(finished),
                              "event": "succeeded" if ok else "failed"}],
            })
            if not ok:
                alerts.append({
                    "id": f"alert-{len(alerts) + 1:06d}",
                    "connector_id": cid,
                    "run_id": run_id,
                    "kind": "sync_failed",
                    "fired_at": iso(finished),
                    "message": (f"no new file in {spec['source']['location']} "
                                f"for {day.isoformat()}; 0 rows synced"),
                    "acknowledged": False,
                })
    state = {"connectors": connectors, "runs": runs, "schemas": schemas,
             "alerts": alerts}
    path = root / "var" / "ingest" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_canon(state) + b"\n")


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    """Seed var/workflow/{issues,chat,kb}.json per gateway/SURFACES.md#workflow.

    The corpus is authored as data in this theme's workflow/ directory and
    dated against the world clock, which is read out of the scheduler state
    `build` has already written rather than re-derived from the seed - one
    definition of `vnow`, so a ticket cannot be filed after today.
    """
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
