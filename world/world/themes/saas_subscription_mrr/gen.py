"""Deterministic subscription and billing webhooks for the SaaS finance world."""
import csv
import random
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from world import scheduler, workflow
from world.scheduler import parse_vtime

EVENT_FIELDS = [
    "event_id", "subscription_id", "account_id", "account_name", "segment",
    "event_type", "occurred_at", "effective_date", "plan_code", "seats",
    "unit_price_cents", "revrec_days", "reason",
]
INVOICE_FIELDS = [
    "webhook_id", "invoice_id", "subscription_id", "account_id", "webhook_type",
    "invoice_type", "status", "invoice_date", "service_period_start",
    "service_period_end", "amount_cents", "currency",
]
PLAN_CODES = ("starter", "growth", "scale")
COMPANIES = (
    "Aster", "Bluefin", "Cedar", "Datachart", "Everwell", "Fathom",
    "Goldfinch", "Harbor", "Juniper", "Kestrel", "Lumina", "Meridian",
    "Northstar", "Oakline", "Pinnacle", "Quartz", "Riverstone", "Summit",
)
INDUSTRIES = ("Health", "Logistics", "Systems", "Analytics", "Industries", "Group")
SEGMENTS = ("SMB", "Mid-Market", "Enterprise")


def _accounts(seed: dict) -> list[dict]:
    accounts = []
    lo_seats, hi_seats = seed["base_seats"]
    for i in range(int(seed["account_count"])):
        rng = random.Random(f"{seed['seed']}:account:{i}")
        plan = PLAN_CODES[rng.randrange(len(PLAN_CODES))]
        accounts.append({
            "account_id": f"ACCT-{i + 1:04d}",
            "subscription_id": f"SUB-{i + 1:04d}",
            "account_name": f"{COMPANIES[i % len(COMPANIES)]} {INDUSTRIES[(i * 5) % len(INDUSTRIES)]}",
            "segment": SEGMENTS[rng.randrange(len(SEGMENTS))],
            "plan_code": plan,
            "seats": rng.randint(int(lo_seats), int(hi_seats)),
            "unit_price_cents": int(seed["plan_prices_cents"][plan]),
        })
    return accounts


def _initial_states(seed: dict) -> dict:
    return {
        a["account_id"]: {
            **a,
            "seats": 0,
            "base_seats": a["seats"],
            "active": False,
        }
        for a in _accounts(seed)
    }


def _timestamp(day, hour: int, sequence: int) -> str:
    value = datetime.combine(day, time(hour=hour), tzinfo=timezone.utc)
    return (value + timedelta(minutes=sequence)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(account: dict, state: dict, event_type: str, occurred_at: str,
           effective_date, seats: int, event_id: str, revrec_days: int,
           reason: str) -> dict:
    return {
        "event_id": event_id,
        "subscription_id": account["subscription_id"],
        "account_id": account["account_id"],
        "account_name": account["account_name"],
        "segment": account["segment"],
        "event_type": event_type,
        "occurred_at": occurred_at,
        "effective_date": effective_date.isoformat(),
        "plan_code": state["plan_code"],
        "seats": int(seats),
        "unit_price_cents": int(state["unit_price_cents"]),
        "revrec_days": int(revrec_days),
        "reason": reason,
    }


def _apply_event(states: dict, event: dict) -> None:
    state = states[event["account_id"]]
    state.update({
        "plan_code": event["plan_code"],
        "seats": int(event["seats"]),
        "unit_price_cents": int(event["unit_price_cents"]),
        "active": event["event_type"] != "subscription_churned",
    })


def _day_rows(seed: dict, day_index: int, day, states: dict) -> tuple[list, list]:
    """Build one source day from an already folded prior state, without mutating it."""
    rng = random.Random(f"{seed['seed']}:{day_index}")
    accounts = _accounts(seed)
    by_id = {a["account_id"]: a for a in accounts}
    start_day = parse_vtime(seed["vtime_start"]).date()
    revrec_days = int(seed["revrec_window_days"])
    events, invoices = [], []
    invoice_sequence = 0

    def add_invoice(account: dict, invoice_type: str, amount_cents: int,
                    service_start, service_end=None) -> None:
        nonlocal invoice_sequence
        invoice_sequence += 1
        if service_end is None:
            service_end = service_start + timedelta(days=revrec_days)
        credit = invoice_type == "proration_credit" or amount_cents < 0
        suffix = f"{day_index:04d}-{invoice_sequence:04d}"
        invoices.append({
            "webhook_id": f"WH-{suffix}",
            "invoice_id": f"{'CN' if credit else 'INV'}-{suffix}",
            "subscription_id": account["subscription_id"],
            "account_id": account["account_id"],
            "webhook_type": "credit_note.finalized" if credit else "invoice.finalized",
            "invoice_type": "proration_credit" if credit else invoice_type,
            "status": "finalized",
            "invoice_date": day.isoformat(),
            "service_period_start": service_start.isoformat(),
            "service_period_end": service_end.isoformat(),
            "amount_cents": int(amount_cents),
            "currency": "USD",
        })

    if day_index > 0 and (day - start_day).days % revrec_days == 0:
        for account in accounts:
            state = states[account["account_id"]]
            if state["active"]:
                add_invoice(account, "recurring", state["seats"] * state["unit_price_cents"], day)

    event_account_ids = []
    if day_index == 0:
        event_account_ids = [a["account_id"] for a in accounts]
    else:
        active = [a["account_id"] for a in accounts if states[a["account_id"]]["active"]]
        lo_events, hi_events = seed["events_per_day"]
        requested = rng.randint(int(lo_events), int(hi_events))
        event_account_ids = rng.sample(active, min(requested, len(active)))

    for sequence, account_id in enumerate(event_account_ids, 1):
        account = by_id[account_id]
        old = states[account_id]
        if day_index == 0:
            event_type = "subscription_started"
            seats = old["base_seats"]
            reason = "initial contract"
        else:
            event_type = "seat_upgraded" if rng.randrange(100) < 58 else "seat_downgraded"
            lo_delta, hi_delta = seed["seat_delta"]
            delta = rng.randint(int(lo_delta), int(hi_delta))
            if event_type == "seat_downgraded" and old["seats"] > 1:
                seats = max(1, old["seats"] - delta)
                reason = "customer seat reduction"
            else:
                event_type = "seat_upgraded"
                seats = old["seats"] + delta
                reason = "customer seat expansion"
        new_state = dict(old)
        event_id = f"SE-{day_index:04d}-{sequence:04d}"
        occurred_at = _timestamp(day, 12, sequence)
        event = _event(account, new_state, event_type, occurred_at, day, seats,
                       event_id, revrec_days, reason)
        events.append(event)
        if day_index == 0:
            add_invoice(account, "recurring", seats * old["unit_price_cents"], day)
        else:
            old_mrr = old["seats"] * old["unit_price_cents"]
            new_mrr = seats * old["unit_price_cents"]
            elapsed = (day - start_day).days % revrec_days
            remaining = revrec_days - elapsed
            prorated = (new_mrr - old_mrr) * remaining // revrec_days
            invoice_type = "proration_charge" if prorated >= 0 else "proration_credit"
            add_invoice(account, invoice_type, prorated, day,
                        day + timedelta(days=remaining))

    cadence = int(seed["churn_cadence_days"])
    if day_index >= 5 and cadence > 0 and day_index % cadence == cadence - 1:
        changed = set(event_account_ids)
        candidates = [
            a for a in accounts
            if states[a["account_id"]]["active"] and a["account_id"] not in changed
        ]
        if candidates:
            account = candidates[rng.randrange(len(candidates))]
            old = states[account["account_id"]]
            lo_lag, hi_lag = seed["backdated_churn_lag_days"]
            lag = rng.randint(int(lo_lag), int(hi_lag))
            effective = max(day - timedelta(days=lag), day.replace(day=1), start_day)
            sequence = len(events) + 1
            event = _event(
                account, old, "subscription_churned", _timestamp(day, 18, 0),
                effective, 0, f"SE-{day_index:04d}-{sequence:04d}", revrec_days,
                "backdated customer cancellation",
            )
            events.append(event)
            elapsed = (effective - start_day).days
            remaining = (-elapsed) % revrec_days
            credit = -(old["seats"] * old["unit_price_cents"] * remaining // revrec_days)
            add_invoice(account, "proration_credit", credit, effective,
                        effective + timedelta(days=remaining))

    return (sorted(events, key=lambda r: r["event_id"]),
            sorted(invoices, key=lambda r: r["webhook_id"]))


def _states_before(seed: dict, day_index: int) -> dict:
    states = _initial_states(seed)
    start = parse_vtime(seed["vtime_start"])
    for i in range(day_index):
        day = (start + timedelta(days=i)).date()
        events, _ = _day_rows(seed, i, day, states)
        for event in sorted(events, key=lambda r: (r["occurred_at"], r["event_id"])):
            _apply_event(states, event)
    return states


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """Land one day using only its isolated RNG and reproducible prior fold."""
    states = _states_before(seed, day_index)
    events, invoices = _day_rows(seed, day_index, day, states)
    base = workspace / "data" / "landing"
    partition = f"dt={day.isoformat()}"
    _write_csv(base / "subscription_events" / partition / "events.csv",
               EVENT_FIELDS, events)
    _write_csv(base / "invoice_webhooks" / partition / "webhooks.csv",
               INVOICE_FIELDS, invoices)
    return {"partition": partition, "subscription_events": len(events),
            "invoice_webhooks": len(invoices)}


def _at(day, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=timezone.utc)


def write_ops_logs(workspace: Path, seed: dict, days: list[dict]) -> None:
    """Historical ops logs for the seeded backstory, under var/logs.

    Content is a pure function of the seed: timestamps walk the seeded days
    derived from vtime_start (source day D lands at D+1 00:00 UTC and the
    ingest/fold/close chain runs at D+1 01:00/01:10/01:20, exactly as the
    scheduler history records it) and all jitter comes from one dedicated
    PRNG. No wall-clock reads, so two builds of one seed are byte-identical.
    """
    rng = random.Random(f"{seed['seed']}:logs")
    logs_dir = Path(workspace).parent / "var" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ingest, sftp, audit = [], [], []
    accounts = int(seed["account_count"])
    total_events = total_invoices = 0
    for entry in days:
        i, day = entry["index"], entry["day"]
        events_n = entry["subscription_events"]
        invoices_n = entry["invoice_webhooks"]
        total_events += events_n
        total_invoices += invoices_n
        run_day = day + timedelta(days=1)
        dt = day.isoformat()
        period = day.strftime("%Y-%m")

        # Daytime billing webhook traffic on the source day.
        ingest.append(
            f"{scheduler.iso(_at(day, 12, 5 + rng.randrange(40), rng.randrange(60)))} "
            f"INFO ingest_daemon[webhook_receiver]: received subscription batch "
            f"effective_date={dt} events={events_n} invoices={invoices_n}")
        # The feed seals at 00:00 UTC the following day.
        seal = _at(run_day, 0, 0, rng.randrange(3, 18))
        ingest.append(
            f"{scheduler.iso(seal)} INFO ingest_daemon[landing]: partition dt={dt} "
            f"sealed subscription_events={events_n} invoice_webhooks={invoices_n} "
            f"bytes={events_n * 214 + invoices_n * 268 + rng.randrange(100, 999)}")
        # Nightly ingest run: a full rebuild of both raw webhook tables.
        start = _at(run_day, 1, 0, rng.randrange(0, 3))
        ingest.append(
            f"{scheduler.iso(start)} INFO ingest_daemon[ingest_webhooks]: run start "
            f"billing_dt={dt} mode=full_rebuild partitions={i + 1}")
        ingest.append(
            f"{scheduler.iso(start + timedelta(seconds=11 + rng.randrange(9)))} "
            f"INFO ingest_daemon[ingest_webhooks]: raw.subscription_events "
            f"rebuilt rows={total_events}")
        ingest.append(
            f"{scheduler.iso(start + timedelta(seconds=24 + rng.randrange(9)))} "
            f"INFO ingest_daemon[ingest_webhooks]: raw.invoice_webhooks "
            f"rebuilt rows={total_invoices}")
        if rng.randrange(100) < 15:
            ingest.append(
                f"{scheduler.iso(start + timedelta(seconds=31 + rng.randrange(9)))} "
                f"WARN ingest_daemon[ingest_webhooks]: duplicate webhook delivery "
                f"ignored webhook_id=WH-{i:04d}-{invoices_n:04d} billing_dt={dt}")

        # The close delta extract pushed to the finance SFTP drop after close.
        push = _at(run_day, 1, 25 + rng.randrange(8), rng.randrange(60))
        sftp.append(
            f"{scheduler.iso(push)} INFO sftp_sync[finance_drop]: pushed "
            f"revrec_close_delta period={period} rows={accounts} "
            f"dest=sftp://finance-drop.example/inbox "
            f"bytes={accounts * 187 + rng.randrange(100, 999)}")
        if rng.randrange(100) < 8:
            sftp.append(
                f"{scheduler.iso(push + timedelta(seconds=20 + rng.randrange(30)))} "
                f"WARN sftp_sync[finance_drop]: push retried attempt=2 "
                f"reason=ssh_timeout period={period}")

        # Scheduler audit trail, mirroring the nightly chain one-for-one.
        for job, offset_min, duration_s, detail_key in (
                ("ingest_webhooks", 0, 45, "billing_dt"),
                ("fold_subscriptions", 10, 90, "subscription_dt"),
                ("revrec_window_close", 20, 60, "revrec_dt")):
            at = _at(run_day, 1, 0) + timedelta(minutes=offset_min)
            run_id = f"{job}-{run_day.strftime('%Y%m%d')}"
            audit.append(
                f"{scheduler.iso(at)} INFO scheduler_audit: job={job} "
                f"run_id={run_id} {detail_key}={dt} status=started")
            audit.append(
                f"{scheduler.iso(at + timedelta(seconds=duration_s))} "
                f"INFO scheduler_audit: job={job} run_id={run_id} "
                f"{detail_key}={dt} status=success duration_s={duration_s}")

    for name, lines in (("ingest_daemon.log", ingest),
                        ("sftp_sync.log", sftp),
                        ("scheduler_audit.log", audit)):
        (logs_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_landing(workspace: Path, seed: dict) -> None:
    start = parse_vtime(seed["vtime_start"])
    days = []
    for i in range(int(seed["days"])):
        day = (start + timedelta(days=i)).date()
        result = write_day(workspace, seed, i, day)
        days.append({"index": i, "day": day, **result})
    write_ops_logs(workspace, seed, days)


def load_raw(workspace: Path, db_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(workspace / "loader" / "ingest_webhooks.py"),
         "--landing", str(workspace / "data" / "landing"),
         "--db", str(db_path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"ingest_webhooks failed:\n{result.stdout}\n{result.stderr}")


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
