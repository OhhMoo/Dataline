"""Card-network settlement feed generation and world builder hooks.

Every partition is a pure function of (seed, day_index). Payment amounts stay
integer cents end to end; recovery replays exercise version precedence without
planting a delivery outage in the clean world.
"""
import csv
import random
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from world import scheduler, workflow
from world.scheduler import parse_vtime

FIELDS = ["event_id", "delivered_at", "payment_day", "payment_id", "version",
          "event_type", "merchant_id", "amount_cents"]
CORRECTION_FIELDS = ["settlement_day", "ticket", "approved_by", "approved_at"]
DEFAULTS = {
    "payments_per_day": [28, 40],
    "amount_cents": [1150, 48000],
    "merchants": 4,
    "late_gap_days": 2,
    "interchange_bps": 175,
    "window_days": 14,
    "cutoff_day": -1,
}
MERCHANTS = (
    ("MER-ARDEN", "Arden Market"),
    ("MER-BRAY", "Bray & Co."),
    ("MER-COLE", "Cole Mobility"),
    ("MER-DRAKE", "Drake Hospitality"),
    ("MER-ELM", "Elm Street Grocers"),
    ("MER-FINCH", "Finch Outdoor"),
    ("MER-GROVE", "Grove Pharmacies"),
    ("MER-HARBOR", "Harbor Books"),
)


def params_of(seed: dict) -> dict:
    return {key: seed.get(key, value) for key, value in DEFAULTS.items()}


def merchant_roster(seed: dict) -> list[tuple[str, str]]:
    configured = params_of(seed)["merchants"]
    if isinstance(configured, int):
        if not 1 <= configured <= 9999:
            raise ValueError("merchants must be a positive integer or a non-empty list")
        roster = list(MERCHANTS[:configured])
        roster.extend((f"MER-{index:04d}", f"Network Merchant {index:04d}")
                      for index in range(len(roster) + 1, configured + 1))
        return roster
    if not isinstance(configured, list) or not configured:
        raise ValueError("merchants must be a positive integer or a non-empty list")
    roster = []
    for value in configured:
        merchant_id = str(value)
        if not merchant_id or any(c.isspace() for c in merchant_id):
            raise ValueError("merchant ids must be non-empty and contain no whitespace")
        known = dict(MERCHANTS).get(merchant_id)
        name = known or merchant_id.removeprefix("MER-").replace("_", " ").title()
        roster.append((merchant_id, name))
    if len({merchant_id for merchant_id, _ in roster}) != len(roster):
        raise ValueError("merchants contains a duplicate merchant id")
    return roster


def _base_events(seed: dict, day_index: int, day) -> tuple[list[dict], random.Random]:
    """The day's original events and same-evening corrections.

    The returned RNG is the same isolated per-day stream, so a caller adding
    delivery-side traffic does not introduce a second random source.
    """
    params = params_of(seed)
    rng = random.Random(f"{seed['seed']}:{day_index}")
    lo_n, hi_n = (int(value) for value in params["payments_per_day"])
    lo_c, hi_c = (int(value) for value in params["amount_cents"])
    if lo_n < 1 or hi_n < lo_n or lo_c < 1 or hi_c < lo_c:
        raise ValueError("invalid payments_per_day or amount_cents range")
    merchants = [merchant_id for merchant_id, _ in merchant_roster(seed)]
    rows = []
    for sequence in range(rng.randint(lo_n, hi_n)):
        payment_id = f"PMT-{day_index:05d}-{sequence:04d}"
        amount = rng.randint(lo_c, hi_c)
        row = {
            "event_id": f"EVT-{day_index:05d}-{sequence:04d}-V1",
            "delivered_at": f"{day.isoformat()}T04:00:00",
            "payment_day": day.isoformat(),
            "payment_id": payment_id,
            "version": 1,
            "event_type": "settle",
            "merchant_id": merchants[rng.randrange(len(merchants))],
            "amount_cents": amount,
        }
        rows.append(row)
        fate = rng.randrange(100)
        if fate < 12:
            correction = dict(row)
            correction["event_id"] = f"EVT-{day_index:05d}-{sequence:04d}-V2"
            correction["delivered_at"] = f"{day.isoformat()}T04:05:00"
            correction["version"] = 2
            if fate < 4:
                correction["event_type"] = "void"
                correction["amount_cents"] = 0
            else:
                correction["amount_cents"] = max(500, amount + rng.randint(-4000, 4000))
            rows.append(correction)
    return rows, rng


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """Land one network partition and, when due, a stale recovery replay.

    `late_gap_days` selects the earlier partition whose version-1 records are
    re-sent. Replays keep their original event and payment ids but arrive after
    any same-day version 2, so highest-version precedence remains observable.
    """
    rows, _rng = _base_events(seed, day_index, day)
    folder = Path(workspace) / "data" / "payments_feed" / f"dt={day.isoformat()}"
    current = sorted(rows, key=lambda row: (row["event_id"], row["delivered_at"]))
    _write_csv(folder / "events.csv", FIELDS, current)

    lag = int(params_of(seed)["late_gap_days"])
    replay = []
    if lag > 0 and day_index >= lag:
        earlier_day = day - timedelta(days=lag)
        earlier, _ = _base_events(seed, day_index - lag, earlier_day)
        highest = {}
        for row in earlier:
            highest[row["payment_id"]] = max(highest.get(row["payment_id"], 0),
                                             int(row["version"]))
        candidates = [row for row in earlier if int(row["version"]) == 1
                      and highest[row["payment_id"]] > 1]
        if not candidates:
            candidates = [row for row in earlier if int(row["version"]) == 1]
        for original in sorted(candidates, key=lambda row: row["event_id"])[:2]:
            replay.append({**original, "delivered_at": f"{day.isoformat()}T04:20:00"})
    if replay:
        _write_csv(folder / "recovery.csv", FIELDS,
                   sorted(replay, key=lambda row: row["event_id"]))
    return {"partition": f"dt={day.isoformat()}",
            "rows": len(current) + len(replay), "recovery_rows": len(replay)}


def _cutoff_date(seed: dict) -> str:
    days = int(seed["days"])
    index = int(params_of(seed)["cutoff_day"])
    index = index if index >= 0 else days + index
    if not 0 <= index < days:
        raise ValueError("cutoff_day must identify a seeded feed day (negative indexes count from the end)")
    start = parse_vtime(seed["vtime_start"])
    return (start + timedelta(days=index)).date().isoformat()


def write_landing(workspace: Path, seed: dict) -> None:
    """Write feed history plus merchant terms, correction register and config."""
    start = parse_vtime(seed["vtime_start"])
    params = params_of(seed)
    window_days = int(params["window_days"])
    interchange_bps = int(params["interchange_bps"])
    if window_days < 1 or not 0 <= interchange_bps <= 10000:
        raise ValueError("window_days must be positive and interchange_bps must be in [0, 10000]")
    if int(params["late_gap_days"]) < 0:
        raise ValueError("late_gap_days must be non-negative")

    control = Path(workspace) / "data" / "control"
    _write_csv(control / "merchant_terms.csv", ["merchant_id", "merchant_name", "interchange_bps"],
               [{"merchant_id": merchant_id, "merchant_name": name,
                 "interchange_bps": interchange_bps}
                for merchant_id, name in merchant_roster(seed)])
    _write_csv(control / "settlement_config.csv",
               ["cutoff_day", "window_days", "interchange_bps"],
               [{"cutoff_day": _cutoff_date(seed), "window_days": window_days,
                 "interchange_bps": interchange_bps}])
    _write_csv(control / "settlement_corrections.csv", CORRECTION_FIELDS, [])
    summaries = []
    for index in range(int(seed["days"])):
        summaries.append(
            write_day(workspace, seed, index, (start + timedelta(days=index)).date()))
    write_ops_logs(workspace, seed, summaries)


# --------------------------------------------------------------------------
# Historical ops logs (var/logs). The factory's breadcrumb generator appends
# to these three files, so they must be a pure function of the seed: every
# timestamp derives from seed["vtime_start"], and every jitter comes from one
# RNG keyed on the seed. No wall clock is read anywhere below.
# --------------------------------------------------------------------------

def write_ops_logs(workspace: Path, seed: dict, summaries: list[dict]) -> None:
    """Deterministic historical ops logs under ``<world>/var/logs``.

    One nightly settlement cycle per seeded feed day: the card network's SFTP
    drop delivers the partition, the ingest daemon validates it, and the
    scheduler runs payments_nightly -> dbt_payments -> settlement_publish ->
    clearing_window_close against it. Row counts come from the write_day
    summaries the landing writer just produced, so the log narrative matches
    the landed partitions, and two builds of one seed are byte-identical.
    """
    start = parse_vtime(seed["vtime_start"])
    rng = random.Random(f"{seed['seed']}:logs")
    sftp, ingest, audit = [], [], []
    for day_index, produced in enumerate(summaries):
        day = (start + timedelta(days=day_index)).date()
        partition = produced["partition"].removeprefix("dt=")
        replay = int(produced["recovery_rows"])
        events = int(produced["rows"]) - replay
        files = 1 + (1 if replay else 0)

        def ts(hour: int, minute: int) -> str:
            return (f"{day.isoformat()}T{hour:02d}:{minute:02d}:"
                    f"{rng.randint(0, 59):02d}Z")

        batch = f"CN-{day:%Y%m%d}-{400 + day_index}"
        sftp.append(f"{ts(3, 56)} INFO sftp_sync: connected to"
                    f" sftp.cardnetwork.example:22 user=settlement_inbound"
                    f" batch={batch} files={files}")
        sftp.append(f"{ts(3, 57)} INFO sftp_sync: fetched"
                    f" outbound/dt={partition}/events.csv rows={events} seq=1")
        if replay:
            sftp.append(f"{ts(3, 58)} INFO sftp_sync: fetched"
                        f" outbound/dt={partition}/recovery.csv rows={replay}"
                        f" seq=2 note=stale_v1_replay")
        if rng.random() < 0.10:
            sftp.append(f"{ts(3, 59)} WARN sftp_sync: transfer reset by peer on"
                        f" dt={partition}; retry 1/3 succeeded")
        sftp.append(f"{ts(4, 0)} INFO sftp_sync: batch={batch} complete"
                    f" files={files} rows={produced['rows']}")
        ingest.append(f"{ts(4, 1)} INFO ingest_daemon: arrival payments_feed"
                      f" day={partition} partition=dt={partition}")
        ingest.append(f"{ts(4, 2)} INFO ingest_daemon: validated"
                      f" payments_events dt={partition} rows={events} schema=v1")
        if replay:
            ingest.append(f"{ts(4, 3)} INFO ingest_daemon: accepted ordinary"
                          f" stale v1 recovery replay dt={partition}"
                          f" rows={replay}")
        ingest.append(f"{ts(4, 4)} INFO ingest_daemon: landing complete"
                      f" dt={partition} rows={produced['rows']}")
        for job, hour, minute, base in (("payments_nightly", 20, 0, 74),
                                        ("dbt_payments", 20, 10, 120),
                                        ("settlement_publish", 20, 20, 31),
                                        ("clearing_window_close", 20, 40, 12)):
            audit.append(f"{ts(hour, minute)} INFO scheduler_audit: job={job}"
                         f" run_date={day.isoformat()} status=SUCCESS"
                         f" duration_s={base + rng.randint(-3, 6)}")
        audit.append(f"{ts(20, 21)} INFO scheduler_audit:"
                     f" job=settlement_publish published"
                     f" target=warehouse.settlement_handoff kind=table"
                     f" consumer=clearing_bank")
        audit.append(f"{ts(20, 21)} INFO scheduler_audit:"
                     f" job=settlement_publish published"
                     f" target=warehouse.interchange_ledger kind=table"
                     f" consumer=finance_reconciliation")
        audit.append(f"{ts(20, 41)} INFO scheduler_audit:"
                     f" job=clearing_window_close published"
                     f" target=warehouse.clearing_window kind=table"
                     f" consumer=clearing_bank")
        audit.append(f"{ts(20, 42)} INFO scheduler_audit: nightly settlement"
                     f" batch complete dt={partition} jobs=4 failures=0")
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


def load_raw(workspace: Path, db_path: Path) -> None:
    """Run the repository loader against every landed partition."""
    script = Path(workspace) / "pipelines" / "load_payments.py"
    result = subprocess.run(
        [sys.executable, str(script), "--workspace", str(workspace), "--db", str(db_path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"load_payments failed:\n{result.stdout}\n{result.stderr}")


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
