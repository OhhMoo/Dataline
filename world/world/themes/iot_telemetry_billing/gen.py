"""Deterministic cloud/IoT telemetry and enterprise billing theme."""
import csv
import random
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from world import scheduler, workflow
from world.scheduler import iso, parse_vtime

DEFAULTS = {
    "customers": 6,
    "devices_per_customer": 4,
    "heartbeats_per_day": 4,
    "usage_per_heartbeat": [400, 900],
    "usage_scale_pct": 100,
    "counter_reset_days": 6,
    "gateway_cycle_days": 12,
    "gateway_offline_days": 3,
    "committed_units": [240000, 360000],
    "monthly_minimum_cents": [240000, 420000],
    "overage_cents_per_1000": [160, 280],
    "lock_grace_days": 2,
}

CUSTOMER_NAMES = (
    "Aster Cold Chain", "Borealis Water", "Cinder Gridworks", "Driftline Logistics",
    "Ember Agricultural", "Fjord Manufacturing", "Harbor Medical Systems",
    "Meridian Smart Buildings",
)
WEEKDAY_PCT = (105, 100, 100, 100, 105, 85, 80)
FIELDS = {
    "meter_heartbeats": [
        "heartbeat_id", "device_id", "customer_id", "gateway_id", "event_kind",
        "boot_id", "counter_value", "delta_units", "observed_at", "collected_at",
    ],
    "usage_rollups": [
        "rollup_id", "device_id", "customer_id", "gateway_id", "window_start",
        "window_end", "units", "source_event_count", "collected_at",
    ],
    "contract_commits": [
        "commit_id", "contract_id", "customer_id", "customer_name", "billing_month",
        "metric", "committed_units", "monthly_minimum_cents",
        "overage_cents_per_1000", "lock_grace_days", "currency", "effective_at",
        "collected_at",
    ],
}


def params_of(seed: dict) -> dict:
    out = {key: seed.get(key, value) for key, value in DEFAULTS.items()}
    out["seed"] = seed["seed"]
    out["vtime_start"] = seed["vtime_start"]
    if int(out["customers"]) < 1:
        raise ValueError("customers must be positive")
    if int(out["devices_per_customer"]) < 3:
        raise ValueError("devices_per_customer must be at least 3")
    if int(out["heartbeats_per_day"]) not in (1, 2, 3, 4, 6, 8, 12, 24):
        raise ValueError("heartbeats_per_day must divide 24")
    for key in ("usage_per_heartbeat", "committed_units", "monthly_minimum_cents",
                "overage_cents_per_1000"):
        lo, hi = out[key]
        if int(lo) < 0 or int(lo) > int(hi):
            raise ValueError(f"{key} must be an ordered non-negative range")
    if int(out["usage_scale_pct"]) < 1:
        raise ValueError("usage_scale_pct must be positive")
    if int(out["counter_reset_days"]) < 1:
        raise ValueError("counter_reset_days must be positive")
    if int(out["gateway_offline_days"]) < 1:
        raise ValueError("gateway_offline_days must be positive")
    if int(out["gateway_cycle_days"]) <= int(out["gateway_offline_days"]) + 2:
        raise ValueError("gateway_cycle_days must leave connected days around the outage")
    if int(out["lock_grace_days"]) < 0:
        raise ValueError("lock_grace_days must be non-negative")
    return out


def business_date(params: dict, usage_index: int):
    return parse_vtime(params["vtime_start"]).date() + timedelta(days=usage_index - 1)


def devices(params: dict) -> list[dict]:
    out = []
    for customer_index in range(1, int(params["customers"]) + 1):
        customer_id = f"CUST-{customer_index:03d}"
        if customer_index <= len(CUSTOMER_NAMES):
            customer_name = CUSTOMER_NAMES[customer_index - 1]
        else:
            customer_name = f"Enterprise Customer {customer_index:03d}"
        for device_index in range(1, int(params["devices_per_customer"]) + 1):
            source_kind = ("edge_rollup" if device_index == 1 else
                           "counter" if device_index % 2 else "delta")
            out.append({
                "customer_index": customer_index,
                "customer_id": customer_id,
                "customer_name": customer_name,
                "device_index": device_index,
                "device_id": f"DEV-{customer_index:03d}-{device_index:02d}",
                "gateway_id": f"GW-{customer_index:03d}-{1 if device_index == 1 else 0:02d}",
                "source_kind": source_kind,
            })
    return out


def usage_delta(params: dict, device_id: str, event_index: int) -> int:
    """Units in one non-overlapping metering window, keyed without fleet state."""
    rng = random.Random(f"{params['seed']}:units:{device_id}:{event_index}")
    lo, hi = (int(value) for value in params["usage_per_heartbeat"])
    usage_index = event_index // int(params["heartbeats_per_day"])
    weekday = business_date(params, usage_index).weekday()
    units = rng.randint(lo, hi) * int(params["usage_scale_pct"]) * WEEKDAY_PCT[weekday]
    return max(1, units // 10000)


def counter_value(params: dict, device: dict, usage_index: int, heartbeat: int) -> tuple[str, int]:
    per_day = int(params["heartbeats_per_day"])
    reset_days = int(params["counter_reset_days"])
    boot_start = (usage_index // reset_days) * reset_days
    event_index = usage_index * per_day + heartbeat
    value = sum(
        usage_delta(params, device["device_id"], index)
        for index in range(boot_start * per_day, event_index + 1)
    )
    return f"BOOT-{device['device_id']}-{boot_start // reset_days:04d}", value


def delivery_day(params: dict, device: dict, usage_index: int) -> int:
    """Arrival index on which an edge device's usage-day rollup is flushed."""
    cycle = int(params["gateway_cycle_days"])
    offline = int(params["gateway_offline_days"])
    outage_start = cycle // 2
    position = (usage_index + device["customer_index"] * 2) % cycle
    if outage_start <= position < outage_start + offline:
        return usage_index + outage_start + offline - position + 1
    return usage_index + 1


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def write_csv(path: Path, entity: str, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS[entity], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def heartbeat_rows(params: dict, day_index: int, usage_day, arrival_day, jitter: int) -> list[dict]:
    rows = []
    per_day = int(params["heartbeats_per_day"])
    window_seconds = 86400 // per_day
    midnight = datetime.combine(usage_day, time())
    for device in devices(params):
        if device["source_kind"] == "edge_rollup":
            continue
        for heartbeat in range(per_day):
            event_index = day_index * per_day + heartbeat
            units = usage_delta(params, device["device_id"], event_index)
            observed = midnight + timedelta(seconds=(heartbeat + 1) * window_seconds - 60)
            collected = observed + timedelta(
                seconds=90 + jitter + device["customer_index"] + device["device_index"])
            row = {
                "heartbeat_id": f"HB-{device['device_id']}-{usage_day:%Y%m%d}-{heartbeat:02d}",
                "device_id": device["device_id"],
                "customer_id": device["customer_id"],
                "gateway_id": device["gateway_id"],
                "event_kind": device["source_kind"],
                "boot_id": "",
                "counter_value": "",
                "delta_units": "",
                "observed_at": stamp(observed),
                "collected_at": stamp(collected),
            }
            if device["source_kind"] == "counter":
                boot_id, value = counter_value(params, device, day_index, heartbeat)
                row["boot_id"] = boot_id
                row["counter_value"] = value
            else:
                row["delta_units"] = units
            rows.append(row)
    rows.sort(key=lambda row: (row["observed_at"], row["device_id"], row["heartbeat_id"]))
    return rows


def rollup_rows(params: dict, day_index: int, arrival_day, jitter: int) -> dict:
    grouped = defaultdict(list)
    per_day = int(params["heartbeats_per_day"])
    lookback = int(params["gateway_cycle_days"]) + int(params["gateway_offline_days"]) + 2
    for device in devices(params):
        if device["source_kind"] != "edge_rollup":
            continue
        for usage_index in range(max(0, day_index - lookback), day_index):
            if delivery_day(params, device, usage_index) != day_index:
                continue
            usage_day = business_date(params, usage_index)
            units = sum(
                usage_delta(params, device["device_id"], event_index)
                for event_index in range(usage_index * per_day, (usage_index + 1) * per_day)
            )
            collected = datetime.combine(arrival_day, time()) + timedelta(
                minutes=10, seconds=jitter + device["customer_index"])
            grouped[usage_day].append({
                "rollup_id": f"UR-{device['device_id']}-{usage_day:%Y%m%d}",
                "device_id": device["device_id"],
                "customer_id": device["customer_id"],
                "gateway_id": device["gateway_id"],
                "window_start": stamp(datetime.combine(usage_day, time())),
                "window_end": stamp(datetime.combine(usage_day + timedelta(days=1), time())),
                "units": units,
                "source_event_count": per_day,
                "collected_at": stamp(collected),
            })
    for rows in grouped.values():
        rows.sort(key=lambda row: (row["device_id"], row["rollup_id"]))
    return grouped


def contract_rows(params: dict, day_index: int, usage_day, arrival_day, jitter: int) -> list[dict]:
    if day_index != 0 and usage_day.day != 1:
        return []
    month = usage_day.replace(day=1)
    rows = []
    seen = set()
    for device in devices(params):
        customer_id = device["customer_id"]
        if customer_id in seen:
            continue
        seen.add(customer_id)
        rng = random.Random(f"{params['seed']}:contract:{customer_id}:{month:%Y-%m}")
        committed = rng.randint(*(int(value) for value in params["committed_units"]))
        minimum = rng.randint(*(int(value) for value in params["monthly_minimum_cents"]))
        overage = rng.randint(*(int(value) for value in params["overage_cents_per_1000"]))
        rows.append({
            "commit_id": f"COM-{month:%Y%m}-{customer_id}",
            "contract_id": f"CTR-{customer_id}",
            "customer_id": customer_id,
            "customer_name": device["customer_name"],
            "billing_month": month.isoformat(),
            "metric": "telemetry_units",
            "committed_units": committed,
            "monthly_minimum_cents": minimum,
            "overage_cents_per_1000": overage,
            "lock_grace_days": int(params["lock_grace_days"]),
            "currency": "USD",
            "effective_at": stamp(datetime.combine(month, time())),
            "collected_at": stamp(datetime.combine(arrival_day, time())
                                   + timedelta(minutes=5, seconds=jitter)),
        })
    rows.sort(key=lambda row: row["customer_id"])
    return rows


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """Land one gateway flush; pure in the seed and arrival-day index."""
    params = params_of(seed)
    rng = random.Random(f"{params['seed']}:{day_index}")
    jitter = rng.randrange(30)
    usage_day = day - timedelta(days=1)
    base = Path(workspace) / "data" / "landing"

    heartbeats = heartbeat_rows(params, day_index, usage_day, day, jitter)
    write_csv(base / "meter_heartbeats" / f"dt={usage_day}" /
              f"heartbeats-{day}.csv", "meter_heartbeats", heartbeats)

    grouped = rollup_rows(params, day_index, day, jitter)
    grouped.setdefault(usage_day, [])
    rollup_count = 0
    for rollup_day in sorted(grouped):
        rows = grouped[rollup_day]
        rollup_count += len(rows)
        write_csv(base / "usage_rollups" / f"dt={rollup_day}" /
                  f"rollups-{day}.csv", "usage_rollups", rows)

    contracts = contract_rows(params, day_index, usage_day, day, jitter)
    write_csv(base / "contract_commits" / f"dt={usage_day}" /
              f"contracts-{day}.csv", "contract_commits", contracts)
    return {"partition": f"dt={usage_day}", "heartbeats": len(heartbeats),
            "rollups": rollup_count, "contracts": len(contracts)}


def write_landing(workspace: Path, seed: dict) -> None:
    start = parse_vtime(seed["vtime_start"])
    summaries = []
    for day_index in range(int(seed["days"])):
        summaries.append(
            write_day(workspace, seed, day_index,
                      (start + timedelta(days=day_index)).date()))
    write_ops_logs(workspace, seed, summaries)


# --------------------------------------------------------------------------
# Historical ops logs (var/logs). The factory's breadcrumb generator appends
# to these three files, so they must be a pure function of the seed: every
# timestamp derives from seed["vtime_start"], and every jitter from a per-day
# PRNG keyed on the seed. No wall clock is read anywhere below.
# --------------------------------------------------------------------------

# Mirrors theme.yaml: load_usage at 01:00 for 60s, finalize_billing 10 minutes
# after that start for 180s (scheduler.job_starts `after` semantics).
AUDIT_JOBS = (
    ("load_usage", 60 * 60, 60,
     "raw.meter_heartbeats,raw.usage_rollups,raw.contract_commits"),
    ("finalize_billing", 70 * 60, 180,
     "warehouse.fct_billable_usage,warehouse.invoice_run_handoff,"
     "warehouse.billing_period_lock"),
)
AUDIT_PUBLISHES = (
    ("warehouse.fct_billable_usage", "usage_billing_dashboard"),
    ("warehouse.invoice_run_handoff", "enterprise_erp"),
    ("warehouse.billing_period_lock", "billing_close_control"),
)


def _at(day: date, seconds: int) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(
        seconds=seconds)


def _flushed_windows(params: dict, day_index: int) -> list[int]:
    """Usage-day indexes whose edge rollups flushed on arrival `day_index`."""
    lookback = int(params["gateway_cycle_days"]) + int(params["gateway_offline_days"]) + 2
    flushed = set()
    for device in devices(params):
        if device["source_kind"] != "edge_rollup":
            continue
        for usage_index in range(max(0, day_index - lookback), day_index):
            if delivery_day(params, device, usage_index) == day_index:
                flushed.add(usage_index)
    return sorted(flushed)


def _offline_gateways(params: dict, usage_index: int) -> list[str]:
    """Edge gateways inside their store-and-forward outage on this usage day."""
    cycle = int(params["gateway_cycle_days"])
    offline = int(params["gateway_offline_days"])
    outage_start = cycle // 2
    gateways = []
    for device in devices(params):
        if device["source_kind"] != "edge_rollup":
            continue
        position = (usage_index + device["customer_index"] * 2) % cycle
        if outage_start <= position < outage_start + offline:
            gateways.append(device["gateway_id"])
    return gateways


def write_ops_logs(workspace: Path, seed: dict, summaries: list[dict]) -> None:
    """Write ingest_daemon.log, sftp_sync.log, scheduler_audit.log to var/logs.

    Backstory-only record: one nightly cycle per seeded arrival day. Content
    is byte-identical across two builds of the same seed.
    """
    params = params_of(seed)
    start = parse_vtime(seed["vtime_start"])
    logs_dir = Path(workspace).parent / "var" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    sftp, ingest, audit = [], [], []
    for day_index, produced in enumerate(summaries):
        arrival = (start + timedelta(days=day_index)).date()
        usage_day = arrival - timedelta(days=1)
        partition = produced["partition"].removeprefix("dt=")
        flushed = _flushed_windows(params, day_index)
        late = [u for u in flushed if u < day_index - 1]
        rng = random.Random(f"{params['seed']}:logs:{day_index}")

        # sftp_sync.log - the 00:30 gateway flush for usage day D-1.
        at = 30 * 60 + rng.randrange(45)
        batch = f"GW-{arrival:%Y%m%d}-{211 + day_index}"
        sftp.append(
            f"{iso(_at(arrival, at))} INFO sftp_sync: connected to "
            f"sftp.northstar-metering.example batch={batch} files=3")
        for gateway in _offline_gateways(params, day_index):
            at += 1
            sftp.append(
                f"{iso(_at(arrival, at))} WARN sftp_sync: gateway {gateway} "
                f"unreachable (store-and-forward outage); usage day "
                f"{usage_day.isoformat()} rollups buffered for later flush")
        at += 1 + rng.randrange(3)
        sftp.append(
            f"{iso(_at(arrival, at))} INFO sftp_sync: fetched "
            f"meter_heartbeats/dt={partition} rows={produced['heartbeats']} seq=1")
        at += 1 + rng.randrange(3)
        sftp.append(
            f"{iso(_at(arrival, at))} INFO sftp_sync: fetched usage_rollups "
            f"rows={produced['rollups']} windows={len(flushed)} "
            f"late_windows={len(late)} seq=2")
        at += 1 + rng.randrange(3)
        sftp.append(
            f"{iso(_at(arrival, at))} INFO sftp_sync: fetched contract_commits "
            f"dt={partition} rows={produced['contracts']} seq=3")
        at += 1 + rng.randrange(3)
        sftp.append(
            f"{iso(_at(arrival, at))} INFO sftp_sync: batch={batch} complete "
            f"files=3 rows={produced['heartbeats'] + produced['rollups'] + produced['contracts']}")

        # ingest_daemon.log - landing-zone validation a minute later.
        at = 31 * 60 + rng.randrange(30)
        ingest.append(
            f"{iso(_at(arrival, at))} INFO ingest_daemon: arrival "
            f"iot_gateway_flush day={arrival.isoformat()} partition=dt={partition}")
        at += 2 + rng.randrange(5)
        ingest.append(
            f"{iso(_at(arrival, at))} INFO ingest_daemon: validated "
            f"meter_heartbeats dt={partition} rows={produced['heartbeats']} schema=v2")
        at += 2 + rng.randrange(5)
        ingest.append(
            f"{iso(_at(arrival, at))} INFO ingest_daemon: applied usage_rollups "
            f"windows={len(flushed)} rows={produced['rollups']} "
            f"late_windows={len(late)}")
        if produced["contracts"]:
            at += 2 + rng.randrange(4)
            ingest.append(
                f"{iso(_at(arrival, at))} INFO ingest_daemon: loaded "
                f"contract_commits rows={produced['contracts']} "
                f"billing_month={usage_day.replace(day=1).isoformat()}")
        if day_index and day_index % int(params["counter_reset_days"]) == 0:
            for device in devices(params):
                if device["source_kind"] != "counter":
                    continue
                boot = f"BOOT-{device['device_id']}-{day_index // int(params['counter_reset_days']):04d}"
                at += 1
                ingest.append(
                    f"{iso(_at(arrival, at))} WARN ingest_daemon: counter reset "
                    f"detected device={device['device_id']} boot_id={boot}; "
                    f"monotonicity check deferred to boot boundary")
        at += 2 + rng.randrange(4)
        ingest.append(
            f"{iso(_at(arrival, at))} INFO ingest_daemon: landing complete "
            f"dt={partition} feeds=3")

        # scheduler_audit.log - the nightly batch at 01:00 and the dbt build.
        for name, start_s, duration_s, tables in AUDIT_JOBS:
            offset = rng.randrange(3)
            audit.append(
                f"{iso(_at(arrival, start_s + offset))} INFO scheduler_audit: "
                f"job={name} occurrence={arrival.isoformat()}T01:00:00Z status=started")
            audit.append(
                f"{iso(_at(arrival, start_s + offset + duration_s))} INFO "
                f"scheduler_audit: job={name} status=success "
                f"duration_s={duration_s} tables={tables}")
            if name == "finalize_billing":
                at = start_s + offset + duration_s + 1
                for target, consumer in AUDIT_PUBLISHES:
                    audit.append(
                        f"{iso(_at(arrival, at))} INFO scheduler_audit: "
                        f"job=finalize_billing published target={target} "
                        f"kind=table consumer={consumer}")
                    at += 1
        if usage_day.day == 1:
            audit.append(
                f"{iso(_at(arrival, 76 * 60))} INFO scheduler_audit: billing "
                f"month {usage_day.replace(day=1).isoformat()} opened; period "
                f"lock evaluates {int(params['lock_grace_days'])} days after month end")
        audit.append(
            f"{iso(_at(arrival, 77 * 60 + rng.randrange(90)))} INFO "
            f"scheduler_audit: nightly batch complete jobs=2 failures=0")

    for name, lines in (("ingest_daemon.log", ingest), ("sftp_sync.log", sftp),
                        ("scheduler_audit.log", audit)):
        (logs_dir / name).write_text("".join(f"{line}\n" for line in lines),
                                     encoding="utf-8")


def load_raw(workspace: Path, db_path: Path) -> None:
    loader = Path(workspace) / "loader" / "load_usage.py"
    result = subprocess.run(
        [sys.executable, str(loader), "--landing", str(Path(workspace) / "data" / "landing"),
         "--db", str(db_path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"load_usage failed:\n{result.stdout}\n{result.stderr}")


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
