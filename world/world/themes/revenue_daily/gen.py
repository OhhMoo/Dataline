"""revenue_daily theme hooks: landing-zone generation + raw load.

write_landing: one RNG per day keyed on (seed, day_index), so output does not
depend on iteration order. Amounts are integer cents rendered %.2f — no binary
floats anywhere in the pipeline.
load_raw: hive-partitioned CSVs -> raw.daily_revenue; PRAGMA threads=1 +
ORDER BY so duckdb page layout is deterministic.
"""
import csv
import hashlib
import random
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import duckdb

from world import scheduler, warehouse_seed, workflow
from world.scheduler import parse_vtime

FIELDS = ["order_id", "order_date", "customer_id", "amount"]


def day_rows(seed: int, day_index: int, day, params: dict) -> list[dict]:
    rng = random.Random(f"{seed}:{day_index}")
    lo_n, hi_n = params["orders_per_day"]
    lo_c, hi_c = params["amount_cents"]
    rows = []
    for i in range(rng.randint(lo_n, hi_n)):
        rows.append({
            "order_id": day_index * 1000 + i,
            "order_date": day.isoformat(),
            "customer_id": f"C{rng.randrange(params['customers']):04d}",
            "amount": f"{rng.randint(lo_c, hi_c) / 100:.2f}",
        })
    return rows


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """One day's landing partition. A pure function of (seed, day_index): the
    live-world arrival stream calls this for day N and gets the same bytes the
    initial build would have written, whatever order it got to N in."""
    d = workspace / "data" / "landing" / "daily_revenue" / f"dt={day}"
    d.mkdir(parents=True, exist_ok=True)
    rows = day_rows(seed["seed"], day_index, day, seed)
    with (d / "revenue.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return {"partition": f"dt={day}", "rows": len(rows)}


def write_landing(workspace: Path, seed: dict) -> None:
    """Write data/landing/daily_revenue/dt=YYYY-MM-DD/revenue.csv for each day."""
    start = parse_vtime(seed["vtime_start"])
    for i in range(seed["days"]):
        write_day(workspace, seed, i, (start + timedelta(days=i)).date())
    write_ops_logs(workspace, seed)


# --------------------------------------------------------------------------
# historical ops logs (var/logs)
# --------------------------------------------------------------------------

def write_ops_logs(workspace: Path, seed: dict) -> None:
    """The platform-side record of the seeded backstory: three rotated daemon
    logs under var/logs/, covering every seeded day on the virtual clock.

    - ingest_daemon.log   the ERP's nightly revenue batch sealing each
                          business date's export at 00:00 the next morning
    - sftp_sync.log       the sync that pushes revenue.csv to the landing zone
                          (size and checksum of what actually landed)
    - scheduler_audit.log the data platform's audit trail of the night's two
                          job runs (01:00 daily_load, 01:05 dbt_build) for the
                          partition just landed

    A pure function of the seed: jitter comes from one stream per day,
    `Random(f"{seed}:logs:{day_index}")`, and every timestamp is derived from
    seed['vtime_start'] - never a wall clock. Two builds of one seed write
    byte-identical logs. Called from write_landing (build time) only: the live
    arrival stream calls write_day directly and appends nothing here.
    """
    start = parse_vtime(seed["vtime_start"])
    folder = Path(workspace).parent / "var" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    ingest, sftp, audit = [], [], []
    for i in range(seed["days"]):
        rng = random.Random(f"{seed['seed']}:logs:{i}")
        day = (start + timedelta(days=i)).date()   # the partition's business date
        run_day = start + timedelta(days=i + 1)    # it lands at 00:00 the next day
        rows = day_rows(seed["seed"], i, day, seed)
        file = (Path(workspace) / "data" / "landing" / "daily_revenue"
                / f"dt={day.isoformat()}" / "revenue.csv")
        data = file.read_bytes()

        # the ERP batch: sealed at 00:00 (+jitter), after the day's close
        seal = run_day + timedelta(seconds=rng.randrange(2, 20))
        elapsed = rng.randrange(2, 9)
        if rng.randrange(100) < 4:
            ingest.append(
                f"{scheduler.iso(seal - timedelta(seconds=elapsed))} WARN "
                f"ingestd[412]: totals query on the POS replica ran long "
                f"({rng.randrange(20, 90)}s); caught up without loss")
        ingest.append(
            f"{scheduler.iso(seal)} INFO ingestd[412]: batch sealed "
            f"dt={day.isoformat()} orders={len(rows)} bytes={len(data)} "
            f"elapsed_s={elapsed}")

        # the sftp push of the bytes that actually landed
        push = seal + timedelta(seconds=rng.randrange(5, 40))
        if rng.randrange(100) < 3:
            ingest_warn_at = push
            push += timedelta(seconds=rng.randrange(4, 15))
            sftp.append(
                f"{scheduler.iso(ingest_warn_at)} WARN sftp-sync[17]: push "
                f"daily_revenue/dt={day.isoformat()}/revenue.csv stalled on "
                f"first ACK; re-sending")
        sftp.append(
            f"{scheduler.iso(push)} INFO sftp-sync[17]: push "
            f"daily_revenue/dt={day.isoformat()}/revenue.csv bytes={len(data)} "
            f"sha256={hashlib.sha256(data).hexdigest()[:16]} attempts=1 ok")

        # the scheduler's audit trail of the night's two runs
        for name, hhmm, nominal in (("daily_load", "01:00", 45),
                                    ("dbt_build", "01:05", 90)):
            hh, mm = (int(x) for x in hhmm.split(":"))
            begin = run_day + timedelta(hours=hh, minutes=mm,
                                        seconds=rng.randrange(0, 30))
            took = max(3, nominal + rng.randrange(-nominal // 6, nominal // 5))
            level, note = "INFO", ""
            if rng.randrange(100) < 4:
                took = int(nominal * 1.6) + rng.randrange(20)
                level, note = "WARN", f" above_p90={int(nominal * 1.5)}"
            audit.append(
                f"{scheduler.iso(begin)} INFO sched-audit[3]: job={name} "
                f"trigger=scheduled dt={day.isoformat()} started")
            audit.append(
                f"{scheduler.iso(begin + timedelta(seconds=took))} {level} "
                f"sched-audit[3]: job={name} status=success "
                f"duration_s={took}{note}")

    for name, lines in (("ingest_daemon.log", ingest),
                        ("sftp_sync.log", sftp),
                        ("scheduler_audit.log", audit)):
        # LF like the landing CSVs: var/logs sits beside the fingerprinted
        # planes, so the bytes are the same on every host.
        (folder / name).write_text("".join(f"{line}\n" for line in lines),
                                   encoding="utf-8")


def load_raw(workspace: Path, db_path: Path) -> None:
    glob = (workspace / "data" / "landing" / "daily_revenue").as_posix() + "/dt=*/*.csv"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.daily_revenue AS
            SELECT cast(order_id as integer) AS order_id,
                   cast(order_date as date) AS order_date,
                   customer_id, amount, dt
            FROM read_csv('{glob}', hive_partitioning=true, columns={{
                'order_id': 'VARCHAR', 'order_date': 'VARCHAR',
                'customer_id': 'VARCHAR', 'amount': 'VARCHAR'}})
            ORDER BY order_date, order_id""")
    finally:
        con.close()


LEGACY_RAW_TYPES = OrderedDict([
    ("order_id", "INTEGER"), ("order_date", "DATE"),
    ("customer_id", "VARCHAR"), ("amount", "DECIMAL(12,2)"),
])
LEGACY_FCT_TYPES = OrderedDict([
    ("order_date", "DATE"), ("orders", "INTEGER"), ("revenue", "DECIMAL(14,2)"),
])


def _legacy_tables(root: Path, seed: dict, name: str, with_fact: bool) -> dict:
    """The pre-dbt mart's tables, from the same landing rows.

    A pure function of the seed, like write_day: `day_rows` for every landed
    day, typed the way the old loader typed them (amount as a DECIMAL rather
    than the VARCHAR the current raw load keeps), and - when asked - the daily
    fact precomputed in exact decimal arithmetic. Returns the `tables` mapping
    `world.warehouse_seed.seed_duckdb` bulk-loads; the CSVs land under build/,
    which is neither fingerprinted nor snapshotted, so the seed material does
    not become world state twice.
    """
    start = parse_vtime(seed["vtime_start"])
    raw, fct = [], {}
    for i in range(seed["days"]):
        day = (start + timedelta(days=i)).date()
        for row in day_rows(seed["seed"], i, day, seed):
            raw.append((row["order_id"], row["order_date"], row["customer_id"],
                        row["amount"]))
            orders, revenue = fct.get(row["order_date"], (0, Decimal("0.00")))
            fct[row["order_date"]] = (orders + 1, revenue + Decimal(row["amount"]))
    out = root / "build" / "warehouse_seed" / name
    tables = {
        "raw.daily_revenue": (
            warehouse_seed.write_rows_csv(out / "daily_revenue.csv",
                                          list(LEGACY_RAW_TYPES), raw),
            dict(LEGACY_RAW_TYPES)),
    }
    if with_fact:
        tables["warehouse.fct_revenue"] = (
            warehouse_seed.write_rows_csv(
                out / "fct_revenue.csv", list(LEGACY_FCT_TYPES),
                [(day, n, f"{total:.2f}") for day, (n, total) in sorted(fct.items())]),
            dict(LEGACY_FCT_TYPES))
    return tables


def seed_legacy(root: Path, seed: dict, manifest: dict, name: str) -> dict:
    """The `legacy` warehouse: raw layer AND the precomputed daily fact - the
    mart the platform ran before dbt, complete, and decommissioned."""
    return _legacy_tables(root, seed, name, with_fact=True)


def seed_target(root: Path, seed: dict, manifest: dict, name: str) -> dict:
    """The `target` warehouse: the raw layer only. Where a cutover lands; the
    marts are the agent's to build there (`dbt_run(warehouse="target")`)."""
    return _legacy_tables(root, seed, name, with_fact=False)


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    """Seed var/workflow/{issues,chat,kb}.json per gateway/SURFACES.md#workflow.

    The corpus is authored as data in this theme's workflow/ directory and
    dated against the world clock, which is read out of the scheduler state
    `build` has already written rather than re-derived from the seed - one
    definition of `vnow`, so a ticket cannot be filed after today.
    """
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
