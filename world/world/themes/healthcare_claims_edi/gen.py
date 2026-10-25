"""Synthetic 837/835 EDI arrivals and raw loads for a healthcare payer."""
import csv
import random
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import yaml

from world import scheduler, workflow
from world.scheduler import parse_vtime

DEFAULTS = {
    "providers": 14,
    "member_pool": 2000,
    "claims_per_day": [24, 36],
    "replacement_pct": 8,
    "void_pct": 3,
    "cob_pct": 20,
    "denial_pct": 6,
    "allowed_bps": [7000, 9200],
    "followup_lag_days": [1, 3],
    "capitated_lives_per_provider": [90, 220],
    "capitation_pmpm_cents": [2400, 5200],
    "capitation_withhold_bps": 500,
    "capitation_payment_day": 15,
    "cms_cutoff_day": 5,
    "cms_cutoff_hour": 18,
    "cms_acceptance_lag_days": 2,
    "reserve_window_days": 45,
    "reserve_bps": 1500,
}

CLAIM_FIELDS = [
    "claim_id", "claim_version", "frequency_code", "transaction_control_number",
    "provider_id", "member_id", "service_date", "received_date", "service_code",
    "diagnosis_code", "billed_amount", "other_payer_id", "other_payer_paid_amount",
]
REMIT_FIELDS = [
    "remit_id", "claim_id", "claim_version", "provider_id", "remit_date",
    "adjudication_status", "allowed_amount", "payer_paid_amount",
    "member_responsibility_amount", "contractual_adjustment_amount",
    "cob_adjustment_amount", "other_payer_paid_amount", "denial_reason_code",
]
PROVIDER_FIELDS = [
    "provider_id", "provider_name", "specialty", "network_tier",
    "capitated_members", "capitation_pmpm", "contract_start_date",
]

SERVICE_CODES = (
    ("99213", 9000, 18000), ("99214", 14000, 26000),
    ("99203", 12000, 23000), ("93000", 4500, 9500),
    ("71046", 7000, 16000), ("80053", 3500, 9000),
    ("85025", 2500, 6500), ("90471", 3000, 8000),
)
DIAGNOSIS_CODES = ("Z00.00", "I10", "E11.9", "J06.9", "M54.50", "R51.9")
SPECIALTIES = ("family_medicine", "internal_medicine", "pediatrics", "cardiology")
PRACTICE_PLACES = (
    "Alder Creek", "Blue Heron", "Cedar Hollow", "Copper Mesa", "Foxglove",
    "Harbor Stone", "Juniper Ridge", "Lakeshore", "Meadow Lark", "North Star",
    "Oak Meadow", "Prairie Sun", "River Birch", "Silver Pine", "Summit Trail",
    "Willow Bend",
)


def params_of(seed: dict) -> dict:
    out = {key: seed.get(key, value) for key, value in DEFAULTS.items()}
    out["seed"] = seed["seed"]
    out["vtime_start"] = seed["vtime_start"]
    for key in ("replacement_pct", "void_pct", "cob_pct", "denial_pct"):
        if not 0 <= int(out[key]) <= 100:
            raise ValueError(f"{key} must be between 0 and 100")
    if not 1 <= int(out["cms_cutoff_day"]) <= 28:
        raise ValueError("cms_cutoff_day must be between 1 and 28")
    if not 0 <= int(out["cms_cutoff_hour"]) <= 23:
        raise ValueError("cms_cutoff_hour must be between 0 and 23")
    if not 1 <= int(out["capitation_payment_day"]) <= 28:
        raise ValueError("capitation_payment_day must be between 1 and 28")
    if int(out["reserve_window_days"]) < 1:
        raise ValueError("reserve_window_days must be positive")
    return out


def business_date(params: dict, day_index: int):
    return parse_vtime(params["vtime_start"]).date() + timedelta(days=day_index)


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def provider_rows(params: dict) -> list[dict]:
    rows = []
    start = parse_vtime(params["vtime_start"]).date()
    lives_lo, lives_hi = (int(v) for v in params["capitated_lives_per_provider"])
    pmpm_lo, pmpm_hi = (int(v) for v in params["capitation_pmpm_cents"])
    for i in range(int(params["providers"])):
        rng = random.Random(f"{params['seed']}:provider:{i}")
        specialty = SPECIALTIES[i % len(SPECIALTIES)]
        rows.append({
            "provider_id": f"PRV{i:04d}",
            "provider_name": f"{PRACTICE_PLACES[i % len(PRACTICE_PLACES)]} Medical {i + 1:02d}",
            "specialty": specialty,
            "network_tier": ("preferred", "standard", "standard")[rng.randrange(3)],
            "capitated_members": rng.randint(lives_lo, lives_hi),
            "capitation_pmpm": money(rng.randint(pmpm_lo, pmpm_hi)),
            "contract_start_date": (start - timedelta(days=rng.randint(180, 1080))).isoformat(),
        })
    return rows


def _financials(rng: random.Random, params: dict, billed: int, denied: bool,
                cob: bool, voided: bool) -> dict:
    if denied or voided:
        return {
            "allowed": 0, "paid": 0, "member": 0, "contractual": billed,
            "cob_adjustment": 0, "other_payer_paid": 0,
        }
    lo, hi = (int(v) for v in params["allowed_bps"])
    allowed = billed * rng.randint(lo, hi) // 10000
    member = allowed * rng.choice((0, 500, 1000, 1500)) // 10000
    other_payer = 0
    if cob:
        other_payer = min(allowed - member, billed * rng.randint(15, 45) // 100)
    paid = allowed - member - other_payer
    return {
        "allowed": allowed, "paid": paid, "member": member,
        "contractual": billed - allowed, "cob_adjustment": other_payer,
        "other_payer_paid": other_payer,
    }


def _version(params: dict, base: dict, version: int, frequency: str,
             received_date, rng: random.Random) -> tuple[dict, dict]:
    billed = base["billed_cents"]
    if frequency == "7":
        billed = billed * rng.randint(95, 110) // 100
    voided = frequency == "8"
    denied = base["denied"] and frequency == "1"
    cob = base["other_payer_id"] != "NONE" and not voided
    amounts = _financials(rng, params, billed, denied, cob, voided)
    if voided:
        status, denial = "voided", ""
    elif denied:
        status, denial = "denied", "CO-97"
    elif frequency == "7":
        status, denial = "adjusted", ""
    else:
        status, denial = "paid", ""
    suffix = f"{version:02d}"
    claim = {
        "claim_id": base["claim_id"],
        "claim_version": version,
        "frequency_code": frequency,
        "transaction_control_number": f"TCN{base['claim_id'][3:]}{suffix}",
        "provider_id": base["provider_id"],
        "member_id": base["member_id"],
        "service_date": base["service_date"].isoformat(),
        "received_date": received_date.isoformat(),
        "service_code": base["service_code"],
        "diagnosis_code": base["diagnosis_code"],
        "billed_amount": money(billed),
        "other_payer_id": base["other_payer_id"] if cob else "NONE",
        "other_payer_paid_amount": money(amounts["other_payer_paid"]),
    }
    remit = {
        "remit_id": f"REM{base['claim_id'][3:]}{suffix}",
        "claim_id": base["claim_id"],
        "claim_version": version,
        "provider_id": base["provider_id"],
        "remit_date": received_date.isoformat(),
        "adjudication_status": status,
        "allowed_amount": money(amounts["allowed"]),
        "payer_paid_amount": money(amounts["paid"]),
        "member_responsibility_amount": money(amounts["member"]),
        "contractual_adjustment_amount": money(amounts["contractual"]),
        "cob_adjustment_amount": money(amounts["cob_adjustment"]),
        "other_payer_paid_amount": money(amounts["other_payer_paid"]),
        "denial_reason_code": denial,
    }
    return claim, remit


def _original(params: dict, day_index: int, claim_index: int):
    rng = random.Random(f"{params['seed']}:claim:{day_index}:{claim_index}")
    service_day = business_date(params, day_index)
    service_code, lo, hi = SERVICE_CODES[rng.randrange(len(SERVICE_CODES))]
    denied = rng.randrange(100) < int(params["denial_pct"])
    cob = not denied and rng.randrange(100) < int(params["cob_pct"])
    base = {
        "claim_id": f"CLM{day_index:05d}{claim_index:04d}",
        "claim_index": claim_index,
        "provider_id": f"PRV{rng.randrange(int(params['providers'])):04d}",
        "member_id": f"SYNM{rng.randrange(int(params['member_pool'])):08d}",
        "service_date": service_day - timedelta(days=rng.randrange(8)),
        "service_code": service_code,
        "diagnosis_code": DIAGNOSIS_CODES[rng.randrange(len(DIAGNOSIS_CODES))],
        "billed_cents": rng.randint(lo, hi),
        "other_payer_id": f"COB-{rng.randrange(1, 5):02d}" if cob else "NONE",
        "denied": denied,
    }
    claim, remit = _version(params, base, 1, "1", service_day, rng)
    return base, claim, remit


def _originals(params: dict, day_index: int):
    rng = random.Random(f"{params['seed']}:{day_index}")
    lo, hi = (int(v) for v in params["claims_per_day"])
    return [_original(params, day_index, i) for i in range(rng.randint(lo, hi))]


def day_rows(params: dict, day_index: int) -> list[tuple[dict, dict]]:
    originals = _originals(params, day_index)
    versions = [(claim, remit) for _base, claim, remit in originals]
    lag_lo, lag_hi = (int(v) for v in params["followup_lag_days"])
    for origin_index in range(max(0, day_index - lag_hi), day_index):
        service_day = business_date(params, day_index)
        for base, _claim, _remit in _originals(params, origin_index):
            rng = random.Random(
                f"{params['seed']}:followup:{origin_index}:{base['claim_index']}")
            outcome = rng.randrange(100)
            lag = rng.randint(lag_lo, lag_hi)
            if origin_index + lag != day_index:
                continue
            if outcome < int(params["void_pct"]):
                frequency = "8"
            elif outcome < int(params["void_pct"]) + int(params["replacement_pct"]):
                frequency = "7"
            else:
                continue
            versions.append(_version(params, base, 2, frequency, service_day, rng))
    return sorted(versions, key=lambda pair: (pair[0]["claim_id"], pair[0]["claim_version"]))


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """Land one deterministic business day of 837 claims and paired 835 remits."""
    params = params_of(seed)
    business_day = business_date(params, day_index)
    versions = day_rows(params, day_index)
    claims = [claim for claim, _remit in versions]
    remits = [remit for _claim, remit in versions]
    base = Path(workspace) / "data" / "landing"
    partition = f"dt={business_day.isoformat()}"
    _write_csv(base / "edi_837_claims" / partition / "edi_837_claims.csv",
               CLAIM_FIELDS, claims)
    _write_csv(base / "edi_835_remits" / partition / "edi_835_remits.csv",
               REMIT_FIELDS, remits)
    return {"partition": partition, "edi_837_claims": len(claims),
            "edi_835_remits": len(remits)}


def _write_dbt_config(workspace: Path, params: dict) -> None:
    path = Path(workspace) / "dbt_project" / "dbt_project.yml"
    project = yaml.safe_load(path.read_text(encoding="utf-8"))
    keys = (
        "capitation_withhold_bps", "capitation_payment_day", "cms_cutoff_day",
        "cms_cutoff_hour", "cms_acceptance_lag_days", "reserve_window_days",
        "reserve_bps",
    )
    project["vars"] = {key: int(params[key]) for key in keys}
    path.write_text(yaml.safe_dump(project, sort_keys=False, width=100), encoding="utf-8")


def write_ops_logs(workspace: Path, seed: dict) -> None:
    """Deterministic historical EDI ops logs under ``<world>/var/logs``.

    Every timestamp derives from ``seed['vtime_start']`` plus the day index and
    all jitter comes from one seeded RNG, so two builds of the same seed write
    byte-identical files. Batch sizes replay the same deterministic claim
    generators the landing writer uses, so the narrative matches landed data.
    """
    params = params_of(seed)
    start = parse_vtime(seed["vtime_start"]).date()
    days = int(seed["days"])
    rng = random.Random(f"{seed['seed']}:logs")
    ingest, sftp, audit = [], [], []
    for day_index in range(days):
        day = start + timedelta(days=day_index)
        stamp = day.strftime("%Y%m%d")
        versions = day_rows(params, day_index)

        def ts(hour: int, minute: int) -> str:
            return f"{day.isoformat()}T{hour:02d}:{minute:02d}:{rng.randint(0, 59):02d}Z"

        if day_index == 0:
            ingest.append(f"{ts(0, 20)} INFO provider_roster: snapshot"
                          f" dt={day.isoformat()} rows={int(params['providers'])}"
                          f" validated and staged")
        sftp.append(f"{ts(0, 5)} INFO connect sftp.clearinghouse.example:22"
                    f" user=edi_payer_ops")
        sftp.append(f"{ts(0, 7)} INFO get /outbound/837P/CLMB-{stamp}.edi"
                    f" -> inbound/CLMB-{stamp}.edi.part renamed, sha256 ok"
                    f" ({len(versions)} claim versions)")
        sftp.append(f"{ts(0, 9)} INFO get /outbound/835/REMB-{stamp}.edi"
                    f" -> inbound/REMB-{stamp}.edi.part renamed, sha256 ok")
        if rng.random() < 0.15:
            sftp.append(f"{ts(0, 11)} WARN transfer reset by peer for"
                        f" REMB-{stamp}.edi; retry 1/3 succeeded")
        ingest.append(f"{ts(0, 30)} INFO edi837_intake: batch CLMB-{stamp} received"
                      f" with {len(versions)} claim version(s); TA1 accepted")
        ingest.append(f"{ts(0, 32)} INFO edi837_intake: 999 functional ack sent for"
                      f" CLMB-{stamp}; 0 rejected transaction sets")
        ingest.append(f"{ts(0, 35)} INFO edi835_intake: remit batch REMB-{stamp}"
                      f" posted with {len(versions)} remit(s)")
        if versions and rng.random() < 0.15:
            claim_id = versions[rng.randrange(len(versions))][0]["claim_id"]
            ingest.append(f"{ts(0, 38)} WARN edi837_intake: claim {claim_id} COB"
                          f" other-payer id mismatch; routed to exceptions queue")
        for job, hour, minute, base in (("nightly_edi_load", 1, 10, 60),
                                        ("adjudication_build", 1, 21, 150),
                                        ("settlement_handoffs", 1, 37, 120),
                                        ("cms_window_close", 1, 48, 90)):
            audit.append(f"{ts(hour, minute)} INFO job={job}"
                         f" run_date={day.isoformat()} status=SUCCESS"
                         f" duration_s={base + rng.randint(-4, 8)}")
        if day_index % 7 == 6:
            for name, lines in (("ingest_daemon.log", ingest),
                                ("sftp_sync.log", sftp),
                                ("scheduler_audit.log", audit)):
                lines.append(f"{day.isoformat()}T23:59:{rng.randint(0, 59):02d}Z"
                             f" INFO rotate: archived {name}.1.gz after 7 day(s)")
    logs_dir = Path(workspace).parent / "var" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for name, lines in (("ingest_daemon.log", ingest),
                        ("sftp_sync.log", sftp),
                        ("scheduler_audit.log", audit)):
        (logs_dir / name).write_text(
            "".join(f"{line}\n" for line in lines), encoding="utf-8")


def write_landing(workspace: Path, seed: dict) -> None:
    params = params_of(seed)
    _write_dbt_config(workspace, params)
    start = business_date(params, 0)
    _write_csv(Path(workspace) / "data" / "landing" / "provider_roster"
               / f"dt={start.isoformat()}" / "provider_roster.csv",
               PROVIDER_FIELDS, provider_rows(params))
    for day_index in range(seed["days"]):
        write_day(workspace, seed, day_index, business_date(params, day_index))
    write_ops_logs(workspace, seed)


def load_raw(workspace: Path, db_path: Path) -> None:
    loader = Path(workspace) / "loader" / "load_edi.py"
    result = subprocess.run(
        [sys.executable, str(loader), "--landing", str(Path(workspace) / "data" / "landing"),
         "--db", str(db_path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"load_edi failed:\n{result.stdout}\n{result.stderr}")


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
