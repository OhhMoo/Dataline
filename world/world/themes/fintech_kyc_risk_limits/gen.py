"""Merchant risk feeds, deterministic landing generation, and world hooks.

An arrival on calendar day D contains business date D-1. Each source day has
its own PRNG; representment and reserve events are regenerated from their
originating day or case, never from mutable generator state. Money is kept in
integer cents until it is rendered as a decimal string.
"""
import csv
import json
import random
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from world import scheduler, workflow
from world.scheduler import iso, parse_vtime

DEFAULTS = {
    "merchants": 10,
    "transactions_per_day": [45, 85],
    "average_ticket_cents": [3000, 11000],
    "disputes_per_day": [0, 2],
    "dispute_amount_cents": [2500, 24000],
    "representment_lag_days": [3, 9],
    "representment_win_pct": 55,
    "kyc_review_interval_days": 21,
    "hold_cycle_days": 18,
    "hold_volume_pct": 40,
    "release_request_lag_days": 4,
    "signoff_lag_days": 2,
    "rolling_window_days": 30,
    "chargeback_warning_bps": 100,
    "chargeback_critical_bps": 150,
    "reserve_coverage_days": 7,
    "base_reserve_bps": 250,
    "elevated_reserve_bps": 500,
}

POLICY_KEYS = (
    "rolling_window_days", "chargeback_warning_bps", "chargeback_critical_bps",
    "reserve_coverage_days", "base_reserve_bps", "elevated_reserve_bps",
)
RANGE_KEYS = (
    "transactions_per_day", "average_ticket_cents", "disputes_per_day",
    "dispute_amount_cents", "representment_lag_days",
)
ENTITIES = ("dispute_events", "kyc_reviews", "merchant_volume", "payout_holds")
FIELDS = {
    "dispute_events": [
        "event_id", "dispute_id", "merchant_id", "event_type", "event_at",
        "opened_at", "dispute_amount", "reason_code", "representment_case_id",
    ],
    "kyc_reviews": [
        "review_id", "merchant_id", "legal_name", "merchant_category", "country",
        "reviewed_at", "review_type", "kyc_status", "risk_tier", "reviewer_id",
        "next_review_date",
    ],
    "merchant_volume": [
        "merchant_id", "business_date", "settled_transactions", "settled_volume",
        "currency",
    ],
    "payout_holds": [
        "event_id", "hold_id", "request_id", "merchant_id", "event_type", "event_at",
        "reserve_amount", "release_amount", "reason_code", "risk_officer_id",
    ],
}
SORT_KEYS = {
    "dispute_events": ("event_id",),
    "kyc_reviews": ("review_id",),
    "merchant_volume": ("merchant_id",),
    "payout_holds": ("event_id",),
}
WEEKDAY_PCT = (105, 103, 100, 102, 108, 82, 72)
PREFIXES = (
    "Alder", "Beacon", "Cedar", "Drift", "Ember", "Fjord", "Garnet", "Harbor",
    "Indigo", "Juniper", "Kepler", "Lumen",
)
TRADES = (
    "Cycles", "Coffee", "Outdoor", "Books", "Mobility", "Market", "Audio", "Travel",
    "Supply", "Wellness", "Digital", "Home",
)
CATEGORIES = ("retail", "travel", "digital_goods", "services", "marketplace")
COUNTRIES = ("US", "CA", "GB", "NL", "AU")
REASONS = ("fraud_card_absent", "product_not_received", "not_as_described", "duplicate")


def params_of(seed: dict) -> dict:
    out = {key: seed.get(key, value) for key, value in DEFAULTS.items()}
    out["seed"] = seed["seed"]
    out["vtime_start"] = seed["vtime_start"]
    out["merchants"] = int(out["merchants"])
    if out["merchants"] < 1:
        raise ValueError("merchants must be positive")
    for key in RANGE_KEYS:
        lo, hi = (int(value) for value in out[key])
        if lo < 0 or hi < lo:
            raise ValueError(f"{key} must be an ordered non-negative range")
        out[key] = (lo, hi)
    for key in set(DEFAULTS) - set(RANGE_KEYS) - {"merchants"}:
        out[key] = int(out[key])
    if out["rolling_window_days"] < 1 or out["reserve_coverage_days"] < 1:
        raise ValueError("window and reserve coverage must be positive")
    if not 0 <= out["chargeback_warning_bps"] <= out["chargeback_critical_bps"]:
        raise ValueError("chargeback thresholds must be ordered non-negative basis points")
    return out


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def business_date(params: dict, day_index: int) -> date:
    return parse_vtime(params["vtime_start"]).date() + timedelta(days=day_index - 1)


def stamp(day: date, seconds: int) -> str:
    return (datetime(day.year, day.month, day.day) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S")


def merchant_id(number: int) -> str:
    return f"M{number + 1:04d}"


def merchant_profile(seed_value: int, number: int) -> dict:
    mid = merchant_id(number)
    rng = random.Random(f"{seed_value}:merchant:{mid}")
    cycle, slot = divmod(number, len(PREFIXES))
    suffix = ("LLC", "Ltd", "Inc.")[number % 3]
    qualifier = "" if cycle == 0 else f" Group {cycle + 1}"
    return {
        "merchant_id": mid,
        "legal_name": f"{PREFIXES[slot]} {TRADES[slot]}{qualifier} {suffix}",
        "merchant_category": CATEGORIES[number % len(CATEGORIES)],
        "country": COUNTRIES[rng.randrange(len(COUNTRIES))],
        "risk_tier": ("low", "standard", "standard", "high")[number % 4],
        "activity_pct": 80 + rng.randrange(41),
        "ticket_pct": 85 + rng.randrange(31),
    }


def day_plan(params: dict, day_index: int) -> dict:
    """New volume and disputes for one business date, from one isolated PRNG."""
    rng = random.Random(f"{params['seed']}:{day_index}")
    day = business_date(params, day_index)
    volume, disputes, outcomes, volume_cents = [], [], [], []
    lo_tx, hi_tx = params["transactions_per_day"]
    lo_ticket, hi_ticket = params["average_ticket_cents"]
    lo_disputes, hi_disputes = params["disputes_per_day"]
    lo_amount, hi_amount = params["dispute_amount_cents"]
    lo_lag, hi_lag = params["representment_lag_days"]
    for number in range(params["merchants"]):
        profile = merchant_profile(params["seed"], number)
        mid = profile["merchant_id"]
        transactions = max(
            1,
            rng.randint(lo_tx, hi_tx) * profile["activity_pct"]
            * WEEKDAY_PCT[day.weekday()] // 10000,
        )
        ticket = rng.randint(lo_ticket, hi_ticket) * profile["ticket_pct"] // 100
        settled_cents = transactions * ticket
        volume_cents.append(settled_cents)
        volume.append({
            "merchant_id": mid,
            "business_date": day.isoformat(),
            "settled_transactions": str(transactions),
            "settled_volume": money(settled_cents),
            "currency": "USD",
        })
        dispute_count = rng.randint(lo_disputes, hi_disputes)
        if profile["risk_tier"] == "high" and rng.randrange(100) < 35:
            dispute_count += 1
        for sequence in range(dispute_count):
            dispute_id = f"DSP-{day_index:05d}-{number + 1:03d}-{sequence + 1:02d}"
            amount = rng.randint(lo_amount, hi_amount)
            opened_at = stamp(day, 6 * 3600 + rng.randrange(6 * 3600))
            reason = REASONS[rng.randrange(len(REASONS))]
            disputes.append({
                "event_id": f"DSE-{dispute_id}-OPEN",
                "dispute_id": dispute_id,
                "merchant_id": mid,
                "event_type": "dispute_opened",
                "event_at": opened_at,
                "opened_at": opened_at,
                "dispute_amount": money(amount),
                "reason_code": reason,
                "representment_case_id": "",
            })
            lag = rng.randint(lo_lag, hi_lag)
            won = rng.randrange(100) < params["representment_win_pct"]
            outcome = "won" if won else "lost"
            outcomes.append({
                "due_index": day_index + lag,
                "row": {
                    "event_id": f"DSE-{dispute_id}-{outcome.upper()}",
                    "dispute_id": dispute_id,
                    "merchant_id": mid,
                    "event_type": f"representment_{outcome}",
                    "event_at": stamp(day + timedelta(days=lag),
                                      13 * 3600 + rng.randrange(5 * 3600)),
                    "opened_at": opened_at,
                    "dispute_amount": money(amount),
                    "reason_code": reason,
                    "representment_case_id": f"REP-{dispute_id}",
                },
            })
    return {"volume": volume, "disputes": disputes, "outcomes": outcomes,
            "volume_cents": volume_cents}


def kyc_rows(params: dict, day_index: int) -> list[dict]:
    day = business_date(params, day_index)
    interval = params["kyc_review_interval_days"]
    rows = []
    for number in range(params["merchants"]):
        profile = merchant_profile(params["seed"], number)
        initial = day_index == 0
        periodic = day_index > 0 and (day_index + number * 3) % interval == 0
        if not (initial or periodic):
            continue
        review_number = day_index // interval
        if initial:
            status = "pending" if number % 5 == 0 else "approved"
        else:
            status = "pending" if (review_number + number) % 9 == 0 else "approved"
        rows.append({
            "review_id": f"KYC-{day_index:05d}-{number + 1:03d}",
            "merchant_id": profile["merchant_id"],
            "legal_name": profile["legal_name"],
            "merchant_category": profile["merchant_category"],
            "country": profile["country"],
            "reviewed_at": stamp(day, 8 * 3600 + number * 90),
            "review_type": "onboarding" if initial else "periodic",
            "kyc_status": status,
            "risk_tier": profile["risk_tier"],
            "reviewer_id": "lcho" if number % 2 == 0 else "nshah",
            "next_review_date": (day + timedelta(days=interval)).isoformat(),
        })
    return rows


def payout_rows(params: dict, day_index: int) -> list[dict]:
    """Reserve placements, release requests, and officer decisions due today."""
    rows = []
    cycle_days = params["hold_cycle_days"]
    request_lag = params["release_request_lag_days"]
    decision_lag = params["signoff_lag_days"]
    for number in range(params["merchants"]):
        mid = merchant_id(number)
        first = 6 + number % 5
        cycle = 0
        while True:
            start = first + cycle * cycle_days
            if start > day_index:
                break
            hold_id = f"HOLD-{start:05d}-{number + 1:03d}"
            request_id = f"REL-{hold_id}"
            hold_cents = (day_plan(params, start)["volume_cents"][number]
                          * params["hold_volume_pct"] // 100)
            release_cents = hold_cents * (35 + (number + cycle) % 4 * 5) // 100
            if start == day_index:
                day = business_date(params, start)
                rows.append({
                    "event_id": f"PHE-{hold_id}-PLACED", "hold_id": hold_id,
                    "request_id": "", "merchant_id": mid, "event_type": "hold_placed",
                    "event_at": stamp(day, 7 * 3600 + number * 60),
                    "reserve_amount": money(hold_cents), "release_amount": money(0),
                    "reason_code": "rolling_reserve", "risk_officer_id": "",
                })
            request_day = start + request_lag
            if request_day == day_index:
                day = business_date(params, request_day)
                rows.append({
                    "event_id": f"PHE-{request_id}-REQUEST", "hold_id": hold_id,
                    "request_id": request_id, "merchant_id": mid,
                    "event_type": "release_requested",
                    "event_at": stamp(day, 10 * 3600 + number * 60),
                    "reserve_amount": money(0), "release_amount": money(release_cents),
                    "reason_code": "excess_reserve_review", "risk_officer_id": "",
                })
            decision_day = request_day + decision_lag
            decision_code = (number + cycle) % 6
            if decision_day == day_index and decision_code != 0:
                approved = decision_code != 1
                decision = "approved" if approved else "rejected"
                day = business_date(params, decision_day)
                rows.append({
                    "event_id": f"PHE-{request_id}-{decision.upper()}",
                    "hold_id": hold_id, "request_id": request_id, "merchant_id": mid,
                    "event_type": f"release_{decision}",
                    "event_at": stamp(day, 15 * 3600 + number * 60),
                    "reserve_amount": money(0), "release_amount": money(release_cents),
                    "reason_code": "risk_officer_decision",
                    "risk_officer_id": "nshah" if number % 2 == 0 else "okamoto",
                })
            cycle += 1
    return rows


def write_policy(workspace: Path, params: dict) -> None:
    path = Path(workspace) / "dbt_project" / "seeds" / "risk_policy.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["policy_id", *POLICY_KEYS],
                                lineterminator="\n")
        writer.writeheader()
        writer.writerow({"policy_id": "merchant_risk_policy",
                         **{key: params[key] for key in POLICY_KEYS}})


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """Write one arrival; its bytes depend only on the seed and day index."""
    params = params_of(seed)
    expected_day = parse_vtime(params["vtime_start"]).date() + timedelta(days=day_index)
    if day != expected_day:
        raise ValueError(f"arrival day {day} does not match day_index {day_index}")
    current = day_plan(params, day_index)
    disputes = list(current["disputes"])
    max_lag = params["representment_lag_days"][1]
    for earlier in range(max(0, day_index - max_lag), day_index + 1):
        for outcome in day_plan(params, earlier)["outcomes"]:
            if outcome["due_index"] == day_index:
                disputes.append(outcome["row"])
    rows = {
        "dispute_events": disputes,
        "kyc_reviews": kyc_rows(params, day_index),
        "merchant_volume": current["volume"],
        "payout_holds": payout_rows(params, day_index),
    }
    partition = business_date(params, day_index).isoformat()
    produced = {"partition": f"dt={partition}"}
    for entity in ENTITIES:
        folder = Path(workspace) / "data" / "landing" / entity / f"dt={partition}"
        folder.mkdir(parents=True, exist_ok=True)
        ordered = sorted(rows[entity],
                         key=lambda row: tuple(row[key] for key in SORT_KEYS[entity]))
        with (folder / f"{entity}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS[entity], lineterminator="\n")
            writer.writeheader()
            writer.writerows(ordered)
        produced[entity] = len(ordered)
    return produced


def write_landing(workspace: Path, seed: dict) -> None:
    params = params_of(seed)
    write_policy(workspace, params)
    start = parse_vtime(seed["vtime_start"])
    summaries = []
    for index in range(seed["days"]):
        summaries.append(
            write_day(workspace, seed, index, (start + timedelta(days=index)).date()))
    write_ops_logs(workspace, seed, summaries)


# --------------------------------------------------------------------------
# Historical ops logs (var/logs). The factory's breadcrumb generator appends
# to these three files, so they must be a pure function of the seed: every
# timestamp derives from seed["vtime_start"], and every jitter from a per-day
# PRNG keyed on the seed. No wall clock is read anywhere below.
# --------------------------------------------------------------------------

# Mirrors theme.yaml: risk_feed_load at 01:00 for 60s, merchant_exposure_build
# 10 minutes after that start for 150s, reserve_handoff 5 minutes after the
# exposure build's start for 120s (scheduler.job_starts `after` semantics).
AUDIT_JOBS = (
    ("risk_feed_load", 60 * 60, 60,
     "raw.dispute_events,raw.kyc_reviews,raw.merchant_volume,raw.payout_holds"),
    ("merchant_exposure_build", 70 * 60, 150, "warehouse.fct_merchant_exposure"),
    ("reserve_handoff", 75 * 60, 120,
     "warehouse.risk_committee_signoff,warehouse.treasury_reserve_handoff"),
)
AUDIT_PUBLISHES = (
    ("warehouse.risk_committee_signoff", "risk_committee"),
    ("warehouse.treasury_reserve_handoff", "treasury_ops"),
)


def _at(day: date, seconds: int) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(
        seconds=seconds)


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
        partition = produced["partition"].removeprefix("dt=")
        counts = {entity: produced[entity] for entity in ENTITIES}
        total = sum(counts.values())
        rng = random.Random(f"{params['seed']}:logs:{day_index}")

        # sftp_sync.log - the 00:30 processor drop for business date D-1.
        at = 30 * 60 + rng.randrange(45)
        batch = f"MB-{arrival:%Y%m%d}-{101 + day_index}"
        sftp.append(
            f"{iso(_at(arrival, at))} INFO sftp_sync: connected to "
            f"sftp.meridian-processor.example batch={batch} files=4")
        if rng.randrange(29) == 0:
            at += 2 + rng.randrange(4)
            sftp.append(
                f"{iso(_at(arrival, at))} WARN sftp_sync: crc mismatch on first "
                f"fetch of kyc_reviews dt={partition}; retransmit requested")
        for sequence, entity in enumerate(ENTITIES, 1):
            at += 1 + rng.randrange(4)
            sftp.append(
                f"{iso(_at(arrival, at))} INFO sftp_sync: fetched "
                f"outbound/{entity}/dt={partition}/{entity}.csv "
                f"rows={counts[entity]} seq={sequence}")
        at += 1 + rng.randrange(3)
        if counts["dispute_events"] == 0:
            sftp.append(
                f"{iso(_at(arrival, at))} WARN sftp_sync: empty dispute_events "
                f"feed for dt={partition}; processor confirmed zero-dispute day")
            at += 1
        sftp.append(
            f"{iso(_at(arrival, at))} INFO sftp_sync: batch={batch} complete "
            f"files=4 rows={total}")

        # ingest_daemon.log - landing-zone validation a minute later.
        at = 31 * 60 + rng.randrange(30)
        ingest.append(
            f"{iso(_at(arrival, at))} INFO ingest_daemon: arrival "
            f"merchant_risk_feeds day={arrival.isoformat()} partition=dt={partition}")
        for entity in ENTITIES:
            at += 2 + rng.randrange(5)
            if counts[entity] == 0:
                ingest.append(
                    f"{iso(_at(arrival, at))} WARN ingest_daemon: empty feed "
                    f"accepted: {entity} dt={partition} rows=0 schema=v1")
            else:
                ingest.append(
                    f"{iso(_at(arrival, at))} INFO ingest_daemon: validated "
                    f"{entity} dt={partition} rows={counts[entity]} schema=v1")
        at += 2 + rng.randrange(4)
        ingest.append(
            f"{iso(_at(arrival, at))} INFO ingest_daemon: landing complete "
            f"dt={partition} feeds=4 rows={total}")

        # scheduler_audit.log - the nightly batch at 01:00 and its dbt DAG.
        for name, start_s, duration_s, tables in AUDIT_JOBS:
            offset = rng.randrange(3)
            audit.append(
                f"{iso(_at(arrival, start_s + offset))} INFO scheduler_audit: "
                f"job={name} occurrence={arrival.isoformat()}T01:00:00Z status=started")
            audit.append(
                f"{iso(_at(arrival, start_s + offset + duration_s))} INFO "
                f"scheduler_audit: job={name} status=success "
                f"duration_s={duration_s} tables={tables}")
            if name == "reserve_handoff":
                at = start_s + offset + duration_s + 1
                for target, consumer in AUDIT_PUBLISHES:
                    audit.append(
                        f"{iso(_at(arrival, at))} INFO scheduler_audit: "
                        f"job=reserve_handoff published target={target} "
                        f"kind=table consumer={consumer}")
                    at += 1
        if counts["dispute_events"] >= 2 * params["merchants"]:
            audit.append(
                f"{iso(_at(arrival, 81 * 60))} WARN scheduler_audit: dispute "
                f"intake {counts['dispute_events']} events on dt={partition} "
                f"exceeds 2x merchant count; chargeback ratio review queued")
        audit.append(
            f"{iso(_at(arrival, 82 * 60 + rng.randrange(90)))} INFO "
            f"scheduler_audit: nightly batch complete jobs=3 failures=0")

    for name, lines in (("ingest_daemon.log", ingest), ("sftp_sync.log", sftp),
                        ("scheduler_audit.log", audit)):
        (logs_dir / name).write_text("".join(f"{line}\n" for line in lines),
                                     encoding="utf-8")


def load_raw(workspace: Path, db_path: Path) -> None:
    """Run the same agent-visible loader used by the nightly scheduler job."""
    loader = Path(workspace) / "loader" / "load_risk_feeds.py"
    result = subprocess.run(
        [sys.executable, str(loader), "--landing", str(Path(workspace) / "data" / "landing"),
         "--db", str(db_path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"load_risk_feeds failed:\n{result.stdout}{result.stderr}")


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
