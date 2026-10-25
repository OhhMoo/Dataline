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

import yaml

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
    write_ops_logs(workspace, seed)


# --------------------------------------------------------------------------
# historical ops logs (var/logs)
# --------------------------------------------------------------------------

def _occurrence_runs(spec: dict, at, fault: dict | None, missed: str | None,
                     duration: int) -> list[tuple]:
    """The recorded runs of one job occurrence: (at, duration_s, status, error,
    try_number, max_tries) per attempt, mirroring scheduler.initial_state's
    per-occurrence fault logic so the audit log and the run history describe
    the same night."""
    kind = fault["kind"] if fault else None
    params = fault["params"] if fault else {}
    status = "failed" if missed else "success"
    if kind == "job_timeout":
        timeout_s = int(params.get("timeout_s", duration))
        return [(at, timeout_s, "failed",
                 params.get("error")
                 or (f"job exceeded its {timeout_s}s timeout and was killed "
                     f"(SIGKILL) before it produced any output"), None, None)]
    if kind == "job_retry":
        attempts = max(2, int(params.get("attempts", 2)))
        delay = int(params.get("retry_delay_minutes", 10))
        message = params.get("error") or "connection reset by peer"
        fail_after = int(params.get("fail_after_s", 5))
        runs = []
        for n in range(1, attempts):
            runs.append((at, fail_after, "failed", message, n, attempts))
            at = at + timedelta(minutes=delay)
        runs.append((at, duration, status, missed, attempts, attempts))
        return runs
    if kind == "job_overrun":
        secs = (int(duration * float(params.get("multiplier", 1.0)))
                + int(params.get("extra_minutes", 0)) * 60)
        return [(at, secs, status, missed, None, None)]
    return [(at, duration, status, missed, None, None)]


def write_ops_logs(workspace: Path, seed: dict) -> None:
    """The platform-side record of the seeded backstory: three rotated daemon
    logs under var/logs/, covering every seeded day on the virtual clock.

    - sftp_sync.log       the transfer daemon pulling each vendor's nightly
                          file into data/drop (05:00Z; on a vendor_late_delivery
                          night the 05:00 cycle finds nothing and the pull lands
                          at the deferred time)
    - ingest_daemon.log   the connector syncs at 05:31/05:32Z, on the same
                          owed/found decision write_ingest seeds into
                          var/ingest/state.json - a late night is a failed
                          zero-row sync and the next morning carries both
    - scheduler_audit.log the platform's audit trail of the night's three job
                          runs (sftp_poll, vendor_load, dbt_build), mirroring
                          initial_state's draws: a connection-reset retry, or
                          the day-scoped poll counting 0 files on a late night

    A pure function of the seed: jitter comes from one stream per day,
    `Random(f"{seed}:logs:{day_index}")`, timestamps derive from
    seed['vtime_start'], and the fault nights come from the SAME
    history_catalogue / history_arrivals / job_decision draws the seeded run
    history and the connector state read - one decision, four surfaces. Never
    a wall clock. Two builds of one seed write byte-identical logs. Called
    from write_landing (build time) only: the live arrival stream calls
    write_day directly and appends nothing here.
    """
    start = parse_vtime(seed["vtime_start"])
    folder = Path(workspace).parent / "var" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    manifest = yaml.safe_load(
        (Path(__file__).resolve().parent / "theme.yaml")
        .read_text(encoding="utf-8"))
    jobs = manifest["jobs"]
    connectors = manifest["ingest"]["connectors"]
    catalogue = faults.history_catalogue(manifest, seed)
    ctx = {"faults": catalogue, "fault_seed": seed["seed"]}
    late = faults.history_arrivals(catalogue, seed["seed"], seed["days"])
    rows_per_day = []
    for i in range(seed["days"]):
        day = (start + timedelta(days=i)).date()
        orders, returns = day_rows(seed["seed"], i, day, seed)
        rows_per_day.append({"orders": len(orders), "returns": len(returns)})
    sync, ingest, audit = [], [], []
    owed = {"orders": [], "returns": []}   # late data days not yet picked up
    loaded_days = 0                        # order dates present in raw so far
    for i in range(seed["days"]):
        rng = random.Random(f"{seed['seed']}:logs:{i}")
        day = (start + timedelta(days=i)).date()
        compact = day.strftime("%Y%m%d")
        tokens = {"day": day.isoformat(), "day_compact": compact}
        # --- sftp_sync.log: the 05:00Z pull from each vendor's outbox. A late
        # night defers BOTH feeds - the arrival event is one decision.
        pull = start + timedelta(days=i + 1, hours=5)
        if i in late:
            delay = int(late[i]["params"].get("delay_minutes", 60))
            for vendor in ("acme", "globex"):
                sync.append(
                    f"{iso(pull + timedelta(seconds=rng.randrange(20)))} WARN "
                    f"sftp-sync[611]: no new file in /outbound on "
                    f"{vendor}.sftp.example.com for {compact} (vendor window "
                    f"05:00Z); next cycle in {delay // 60}h{delay % 60:02d}m")
            pull = pull + timedelta(minutes=delay)
        for vendor, prefix, key in (("acme", "ORDERS", "orders"),
                                    ("globex", "RETURNS", "returns")):
            n = rows_per_day[i][key]
            size = (workspace / "data" / "drop" / vendor
                    / f"{prefix}_{compact}.csv").stat().st_size
            at = pull + timedelta(seconds=rng.randrange(5, 90))
            sync.append(
                f"{iso(at)} INFO sftp-sync[611]: connected "
                f"{vendor}.sftp.example.com:22 (host key verified)")
            sync.append(
                f"{iso(at + timedelta(seconds=1))} INFO sftp-sync[611]: get "
                f"/outbound/{prefix}_{compact}.csv -> data/drop/{vendor}/"
                f"{prefix}_{compact}.csv records={n} bytes={size} ok")
            sync.append(
                f"{iso(at + timedelta(seconds=2))} INFO sftp-sync[611]: "
                f"sentinel data/drop/{vendor}/{prefix}_{compact}.done "
                f"written ({n})")
        # --- ingest_daemon.log: the connector syncs at their cron slots, on
        # the same owed/found decision write_ingest makes - times and row
        # counts match var/ingest/state.json to the second and the row.
        found = {}
        for key in ("orders", "returns"):
            found[key] = owed[key] + ([] if i in late else [i])
            owed[key] = [i] if i in late else []
        for cid, spec in connectors.items():
            key = spec["feed"]
            hh, mm = (int(x) for x in spec["time"].split(":"))
            table = (f"{spec['destination']['schema']}."
                     f"{spec['destination']['tables'][0]}")
            t0 = start + timedelta(days=i + 1, hours=hh, minutes=mm)
            t1 = t0 + timedelta(seconds=spec["duration_s"])
            ingest.append(
                f"{iso(t0)} INFO ingest-daemon[872]: sync {cid} started "
                f"(cron {spec['schedule']})")
            if found[key]:
                n = sum(rows_per_day[j][key] for j in found[key])
                ingest.append(
                    f"{iso(t1)} INFO ingest-daemon[872]: sync {cid} ok: "
                    f"{table} +{n} row(s) from {len(found[key])} file(s)")
            else:
                ingest.append(
                    f"{iso(t1)} WARN ingest-daemon[872]: sync {cid} failed: "
                    f"no new file in {spec['source']['location']} for "
                    f"{day.isoformat()}; 0 rows synced")
                ingest.append(
                    f"{iso(t1 + timedelta(seconds=1))} WARN "
                    f"ingest-daemon[872]: alert sync_failed raised for "
                    f"connector {cid}")
        loaded_days += 1 if found["orders"] else 0
        # --- scheduler_audit.log: the night's job runs, attempt by attempt,
        # exactly as initial_state recorded them.
        starts = scheduler.job_starts(jobs, start + timedelta(days=i))
        arrival = faults.arrival_decision(ctx, i)
        notes = {
            "sftp_poll": f"matched=2 pattern=*/*_{compact}.done",
            "vendor_load": (
                f"raw.vendor_orders=+"
                f"{sum(rows_per_day[j]['orders'] for j in found['orders'])} "
                f"raw.vendor_returns=+"
                f"{sum(rows_per_day[j]['returns'] for j in found['returns'])}"),
            "dbt_build": f"warehouse.fct_orders_daily_rows={loaded_days}",
        }
        for name, spec in jobs.items():
            at = starts[name]
            detail = (spec["detail"].format(day=day.isoformat())
                      if spec.get("detail") else None)
            fault = faults.job_decision(ctx, name, iso(at))
            missed = faults.missed_by(arrival, name, tokens)
            duration = int(spec["duration_s"])
            tag = f" {detail}" if detail else ""
            for begin, took, status, error, try_n, max_n in _occurrence_runs(
                    spec, at, fault, missed, duration):
                attempt = f" attempt={try_n}/{max_n}" if try_n else ""
                audit.append(
                    f"{iso(begin)} INFO sched-audit[220]: job={name}{tag} "
                    f"started{attempt}")
                end = begin + timedelta(seconds=took)
                if status == "success":
                    level = "WARN" if took > duration else "INFO"
                    note = f" {notes.get(name, '')}".rstrip()
                    extra = (f" overran_window=true nominal_s={duration}"
                             if took > duration else "")
                    audit.append(
                        f"{iso(end)} {level} sched-audit[220]: job={name} "
                        f"status=success duration_s={took}{attempt}"
                        f"{extra}{note}")
                else:
                    level = "WARN" if try_n and try_n < max_n else "ERROR"
                    audit.append(
                        f"{iso(end)} {level} sched-audit[220]: job={name} "
                        f"status=failed duration_s={took}{attempt} "
                        f"error={error}")
    for name, lines in (("ingest_daemon.log", ingest),
                        ("sftp_sync.log", sync),
                        ("scheduler_audit.log", audit)):
        # LF like the landing CSVs: var/logs sits beside the fingerprinted
        # planes, so the bytes are the same on every host.
        (folder / name).write_text("".join(f"{line}\n" for line in lines),
                                   encoding="utf-8")


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
