"""ad_attribution_events theme hooks.

Each daily export contains ad impressions, clicks, and server-to-server
conversion postbacks. A per-day PRNG drives every generated value; conversions
may regenerate earlier click days to choose an attribution candidate, but never
depend on generation order or wall time. Money remains integer cents.
"""
import csv
import random
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from world import scheduler, workflow
from world.scheduler import parse_vtime

ENTITIES = ("impressions", "clicks", "conversions")
FIELDS = {
    "impressions": ["impression_id", "event_time", "user_id", "partner_id",
                    "campaign_id", "placement", "ip_prefix", "user_agent"],
    "clicks": ["click_id", "impression_id", "click_time", "user_id", "partner_id",
               "campaign_id", "ip_prefix", "user_agent", "referrer"],
    "conversions": ["postback_id", "conversion_id", "event_time", "user_id",
                    "order_id", "revenue_cents", "currency", "received_at"],
}
DEFAULTS = {
    "impressions_per_day": [90, 120],
    "clicks_per_day": [24, 36],
    "conversions_per_day": [8, 14],
    "users": 120,
    "conversion_amount_cents": [1500, 25000],
    "bot_traffic_pct": 8,
    "unattributed_pct": 10,
    "duplicate_postback_pct": 35,
    "attribution_lookback_days": 7,
    "max_conversion_lag_days": 10,
    "billing_lag_days": 1,
}
PARTNERS = (
    ("P100", "Northstar Media", 1400),
    ("P200", "Orbit Affiliates", 1250),
    ("P300", "Cobalt Reach", 1600),
    ("P400", "Juniper Social", 1100),
)
CAMPAIGNS = (
    ("CMP-SEARCH-01", "P100", "search", "search.example"),
    ("CMP-SEARCH-02", "P100", "search", "search.example"),
    ("CMP-AFF-01", "P200", "affiliate", "deals.example"),
    ("CMP-AFF-02", "P200", "affiliate", "reviews.example"),
    ("CMP-DISPLAY-01", "P300", "display", "publisher.example"),
    ("CMP-DISPLAY-02", "P300", "display", "news.example"),
    ("CMP-SOCIAL-01", "P400", "social", "social.example"),
    ("CMP-SOCIAL-02", "P400", "social", "social.example"),
)
BOT_EXCLUSIONS = (
    ("BOT-IP-01", "ip_prefix", "45.148.10", "Known hosting-range automation"),
    ("BOT-IP-02", "ip_prefix", "185.220.101", "Exit-node monitoring traffic"),
    ("BOT-UA-01", "user_agent", "python-requests", "Non-browser HTTP client"),
    ("BOT-UA-02", "user_agent", "headlesschrome", "Automated browser traffic"),
)
NORMAL_IPS = ("10.24.8", "10.24.19", "172.18.4", "192.168.14", "198.51.100")
NORMAL_UAS = (
    "Mozilla/5.0 (X11; Linux x86_64) Chrome/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X) Safari/17.6",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1) Mobile/15E148",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/132.0",
)
BOT_IPS = tuple(row[2] for row in BOT_EXCLUSIONS if row[1] == "ip_prefix")
BOT_UAS = ("python-requests/2.32.3", "HeadlessChrome/131.0")


def params_of(seed: dict) -> dict:
    params = {key: seed.get(key, default) for key, default in DEFAULTS.items()}
    for key in ("impressions_per_day", "clicks_per_day", "conversions_per_day",
                "conversion_amount_cents"):
        lo, hi = (int(value) for value in params[key])
        if lo < 0 or hi < lo:
            raise ValueError(f"{key} must be an ordered non-negative range")
    for key in ("bot_traffic_pct", "unattributed_pct", "duplicate_postback_pct"):
        if not 0 <= int(params[key]) <= 100:
            raise ValueError(f"{key} must be between 0 and 100")
    for key in ("users", "attribution_lookback_days", "max_conversion_lag_days",
                "billing_lag_days"):
        if int(params[key]) < 0:
            raise ValueError(f"{key} must be non-negative")
    if int(params["users"]) == 0:
        raise ValueError("users must be positive")
    return params


def money_range(params: dict) -> tuple[int, int]:
    return tuple(int(value) for value in params["conversion_amount_cents"])


def stamp(day, seconds: int) -> str:
    moment = datetime(day.year, day.month, day.day) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def seconds_of(timestamp: str) -> int:
    hour, minute, second = (int(value) for value in timestamp[11:19].split(":"))
    return hour * 3600 + minute * 60 + second


def source_for(rng: random.Random, bot: bool) -> tuple[str, str]:
    if bot:
        return rng.choice(BOT_IPS), rng.choice(BOT_UAS)
    return rng.choice(NORMAL_IPS), rng.choice(NORMAL_UAS)


def weighted_index(rng: random.Random, weights: list[int]) -> int:
    pick = rng.randrange(sum(weights))
    for index, weight in enumerate(weights):
        if pick < weight:
            return index
        pick -= weight
    raise AssertionError("unreachable")


def traffic_rows(seed_value: int, day_index: int, day, params: dict):
    """Generate one event date's impressions and clicks in isolation."""
    rng = random.Random(f"{seed_value}:{day_index}")
    impressions = []
    impression_seconds = {}
    lo, hi = (int(value) for value in params["impressions_per_day"])
    bot_pct = int(params["bot_traffic_pct"])
    for index in range(rng.randint(lo, hi)):
        impression_id = f"IMP-{day_index:06d}-{index:06d}"
        seconds = rng.randrange(84000)
        campaign_id, partner_id, placement, _referrer = rng.choice(CAMPAIGNS)
        ip_prefix, user_agent = source_for(rng, rng.randrange(100) < bot_pct)
        impressions.append({
            "impression_id": impression_id,
            "event_time": stamp(day, seconds),
            "user_id": f"U{rng.randrange(int(params['users'])):06d}",
            "partner_id": partner_id,
            "campaign_id": campaign_id,
            "placement": placement,
            "ip_prefix": ip_prefix,
            "user_agent": user_agent,
        })
        impression_seconds[impression_id] = seconds
    impressions.sort(key=lambda row: (row["event_time"], row["impression_id"]))

    clicks = []
    lo, hi = (int(value) for value in params["clicks_per_day"])
    campaigns = {row[0]: row for row in CAMPAIGNS}
    for index in range(rng.randint(lo, hi)):
        impression = rng.choice(impressions)
        click_id = f"CLK-{day_index:06d}-{index:06d}"
        seconds = min(86399, impression_seconds[impression["impression_id"]]
                      + 30 + rng.randrange(1800))
        ip_prefix, user_agent = source_for(rng, rng.randrange(100) < bot_pct)
        clicks.append({
            "click_id": click_id,
            "impression_id": impression["impression_id"],
            "click_time": stamp(day, seconds),
            "user_id": impression["user_id"],
            "partner_id": impression["partner_id"],
            "campaign_id": impression["campaign_id"],
            "ip_prefix": ip_prefix,
            "user_agent": user_agent,
            "referrer": campaigns[impression["campaign_id"]][3],
        })
    clicks.sort(key=lambda row: (row["click_time"], row["click_id"]))
    return impressions, clicks, rng


def day_rows(seed: dict, day_index: int, arrival_day) -> dict:
    """Return all three entities for the export arriving on ``arrival_day``."""
    params = params_of(seed)
    seed_value = seed["seed"]
    event_day = arrival_day - timedelta(days=1)
    impressions, clicks, rng = traffic_rows(seed_value, day_index, event_day, params)

    max_lag = min(int(params["max_conversion_lag_days"]), day_index)
    click_history = {0: clicks}
    for lag in range(1, max_lag + 1):
        _old_impressions, old_clicks, _old_rng = traffic_rows(
            seed_value, day_index - lag, event_day - timedelta(days=lag), params)
        click_history[lag] = old_clicks

    conversions = []
    lo, hi = (int(value) for value in params["conversions_per_day"])
    amount_lo, amount_hi = money_range(params)
    lag_weights = [40, 20, 12, 8, 6, 4, 3, 2, 1, 1, 1]
    for index in range(rng.randint(lo, hi)):
        conversion_id = f"CNV-{day_index:06d}-{index:06d}"
        direct = rng.randrange(100) < int(params["unattributed_pct"])
        selected = None
        if not direct:
            lag = weighted_index(rng, lag_weights[:max_lag + 1])
            candidates = click_history[lag]
            if candidates:
                selected = rng.choice(candidates)
                if lag == 0:
                    seconds = min(86399, seconds_of(selected["click_time"])
                                  + 60 + rng.randrange(7200))
                else:
                    seconds = rng.randrange(86400)
                user_id = selected["user_id"]
        if selected is None:
            seconds = rng.randrange(86400)
            user_id = f"ORG-{day_index:06d}-{index:06d}"
        received = min(86399, seconds + 5 + rng.randrange(300))
        row = {
            "postback_id": f"PB-{day_index:06d}-{index:06d}-A",
            "conversion_id": conversion_id,
            "event_time": stamp(event_day, seconds),
            "user_id": user_id,
            "order_id": f"ORD-{day_index:06d}-{index:06d}",
            "revenue_cents": rng.randint(amount_lo, amount_hi),
            "currency": "USD",
            "received_at": stamp(event_day, received),
        }
        conversions.append(row)
        if rng.randrange(100) < int(params["duplicate_postback_pct"]):
            retry_received = min(86399, received + 30 + rng.randrange(600))
            conversions.append({**row,
                                "postback_id": f"PB-{day_index:06d}-{index:06d}-B",
                                "received_at": stamp(event_day, retry_received)})
    conversions.sort(key=lambda row: (row["event_time"], row["conversion_id"],
                                      row["postback_id"]))
    return {"impressions": impressions, "clicks": clicks, "conversions": conversions}


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_config(workspace: Path, seed: dict) -> None:
    params = params_of(seed)
    config = Path(workspace) / "data" / "config"
    write_csv(config / "partners.csv",
              ["partner_id", "partner_name", "revshare_bps", "currency"],
              [{"partner_id": partner, "partner_name": name,
                "revshare_bps": bps, "currency": "USD"}
               for partner, name, bps in PARTNERS])
    write_csv(config / "bot_exclusions.csv",
              ["exclusion_id", "exclusion_type", "exclusion_value", "reason", "active"],
              [{"exclusion_id": exclusion_id, "exclusion_type": kind,
                "exclusion_value": value, "reason": reason, "active": "true"}
               for exclusion_id, kind, value, reason in BOT_EXCLUSIONS])
    write_csv(config / "attribution_rules.csv",
              ["rule_id", "lookback_days", "billing_lag_days"],
              [{"rule_id": "network_default",
                "lookback_days": int(params["attribution_lookback_days"]),
                "billing_lag_days": int(params["billing_lag_days"])}])


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """Write the export arriving on ``day`` for event date ``day - 1``."""
    rows = day_rows(seed, day_index, day)
    partition = f"dt={day - timedelta(days=1)}"
    result = {"partition": partition}
    for entity in ENTITIES:
        folder = Path(workspace) / "data" / "landing" / entity / partition
        write_csv(folder / f"{entity}.csv", FIELDS[entity], rows[entity])
        result[entity] = len(rows[entity])
    return result


def _at(day, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute, second), tzinfo=timezone.utc)


def write_ops_logs(workspace: Path, seed: dict, days: list[dict]) -> None:
    """Historical ops logs for the seeded backstory, under var/logs.

    Content is a pure function of the seed: timestamps walk the seeded
    arrival days derived from vtime_start (the export for event date D lands
    at D+1 00:30 UTC and the ingest/attribute/lock chain runs on that arrival
    day at 01:00/01:10/01:20, exactly as the scheduler history records it)
    and all jitter comes from one dedicated PRNG. No wall-clock reads, so two
    builds of one seed are byte-identical.
    """
    params = params_of(seed)
    rng = random.Random(f"{seed['seed']}:logs")
    logs_dir = Path(workspace).parent / "var" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ingest, sftp, audit = [], [], []
    totals = {name: 0 for name in ENTITIES}
    for entry in days:
        i, arrival = entry["index"], entry["arrival_day"]
        event_day = arrival - timedelta(days=1)
        edt = event_day.isoformat()
        counts = {name: entry[name] for name in ENTITIES}
        for name in ENTITIES:
            totals[name] += counts[name]

        # The nightly export for the event date lands at 00:30 UTC.
        landed = _at(arrival, 0, 30, rng.randrange(3, 25))
        ingest.append(
            f"{scheduler.iso(landed)} INFO ingest_daemon[export_receiver]: "
            f"nightly export received event_dt={edt} "
            f"impressions={counts['impressions']} clicks={counts['clicks']} "
            f"conversions={counts['conversions']} "
            f"bytes={sum(counts.values()) * 173 + rng.randrange(100, 999)}")
        # Loader run at 01:00: a full reload of every landed partition.
        start = _at(arrival, 1, 0, rng.randrange(0, 3))
        ingest.append(
            f"{scheduler.iso(start)} INFO ingest_daemon[load_events]: run start "
            f"event_dt={edt} mode=full_reload partitions={i + 1}")
        for offset, name in ((9, "impressions"), (21, "clicks"), (33, "conversions")):
            ingest.append(
                f"{scheduler.iso(start + timedelta(seconds=offset + rng.randrange(7)))} "
                f"INFO ingest_daemon[load_events]: raw.{name} rebuilt "
                f"rows={totals[name]}")
        if rng.randrange(100) < int(params["duplicate_postback_pct"]) // 2:
            ingest.append(
                f"{scheduler.iso(start + timedelta(seconds=41 + rng.randrange(9)))} "
                f"WARN ingest_daemon[postback_dedup]: duplicate S2S postback "
                f"quarantined conversion_id=CNV-{i:06d}-000000 "
                f"postback_id=PB-{i:06d}-000000-B event_dt={edt}")
        if rng.randrange(100) < 20:
            ingest.append(
                f"{scheduler.iso(start + timedelta(seconds=52 + rng.randrange(9)))} "
                f"WARN ingest_daemon[traffic_quality]: bot-signature clicks "
                f"excluded count={1 + rng.randrange(4)} event_dt={edt}")

        # Partner payout handoff delivered to Partner Finance over SFTP once
        # the billing lock advances through event_dt - billing_lag_days.
        push = _at(arrival, 1, 26 + rng.randrange(8), rng.randrange(60))
        sftp.append(
            f"{scheduler.iso(push)} INFO sftp_sync[partner_finance]: delivered "
            f"partner_payout_handoff event_dt={edt} partners={len(PARTNERS)} "
            f"dest=sftp://partner-finance.example/inbox "
            f"bytes={len(PARTNERS) * 211 + rng.randrange(100, 999)}")
        lock_through = event_day - timedelta(days=int(params["billing_lag_days"]))
        sftp.append(
            f"{scheduler.iso(push + timedelta(seconds=8 + rng.randrange(20)))} "
            f"INFO sftp_sync[partner_finance]: billing lock advanced through "
            f"event_dt={lock_through.isoformat()} status=locked")
        if rng.randrange(100) < 8:
            sftp.append(
                f"{scheduler.iso(push + timedelta(seconds=35 + rng.randrange(40)))} "
                f"WARN sftp_sync[partner_finance]: delivery retried attempt=2 "
                f"reason=ssh_timeout event_dt={edt}")

        # Scheduler audit trail, mirroring the nightly chain one-for-one.
        for job, offset_min, duration_s in (
                ("ingest_events", 0, 60),
                ("attribute_conversions", 10, 120),
                ("lock_partner_billing", 20, 90)):
            at = _at(arrival, 1, 0) + timedelta(minutes=offset_min)
            run_id = f"{job}-{arrival.strftime('%Y%m%d')}"
            audit.append(
                f"{scheduler.iso(at)} INFO scheduler_audit: job={job} "
                f"run_id={run_id} event_dt={edt} status=started")
            audit.append(
                f"{scheduler.iso(at + timedelta(seconds=duration_s))} "
                f"INFO scheduler_audit: job={job} run_id={run_id} "
                f"event_dt={edt} status=success duration_s={duration_s}")

    for name, lines in (("ingest_daemon.log", ingest),
                        ("sftp_sync.log", sftp),
                        ("scheduler_audit.log", audit)):
        (logs_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_landing(workspace: Path, seed: dict) -> None:
    write_config(workspace, seed)
    start = parse_vtime(seed["vtime_start"])
    days = []
    for index in range(seed["days"]):
        day = (start + timedelta(days=index)).date()
        result = write_day(workspace, seed, index, day)
        days.append({"index": index, "arrival_day": day, **result})
    write_ops_logs(workspace, seed, days)


def load_raw(workspace: Path, db_path: Path) -> None:
    """Run the workspace loader used by both initial build and nightly ingest."""
    script = Path(workspace) / "loader" / "load_events.py"
    result = subprocess.run(
        [sys.executable, str(script),
         "--landing", str(Path(workspace) / "data" / "landing"),
         "--config", str(Path(workspace) / "data" / "config"),
         "--db", str(db_path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"load_events failed:\n{result.stdout}\n{result.stderr}")


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
