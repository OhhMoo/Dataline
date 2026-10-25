"""Deterministic multi-warehouse inventory and EDI theme hooks."""
import csv
import random
import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

from world import scheduler, workflow
from world.scheduler import parse_vtime

DEFAULTS = {
    "distribution_centers": ["DC_EAST", "DC_CENTRAL", "DC_WEST"],
    "sku_count": 12,
    "case_pack_options": [6, 12, 24],
    "asns_per_day": [3, 5],
    "lines_per_asn": [1, 3],
    "movements_per_day": [8, 12],
    "cycle_counts_per_day": [2, 4],
    "transit_days": [2, 4],
    "customs_freeze_hours": 48,
    "customs_cutoff_hour": 18,
    "customs_declaration_modulo": 3,
    "threepl_receipt_cents_per_each": 2,
    "threepl_outbound_cents_per_each": 4,
    "threepl_storage_cents_per_each": 1,
}

FIELDS = {
    "asn_receipts": [
        "event_id", "asn_id", "asn_line_id", "edi_control_id", "warehouse_id",
        "sku_id", "supplier_id", "event_type", "event_date", "quantity", "uom",
        "expected_receipt_date", "customs_declaration_id",
    ],
    "dc_movements": [
        "movement_id", "movement_date", "warehouse_id", "sku_id", "movement_type",
        "quantity", "uom", "reference_id", "threepl_id",
    ],
    "cycle_counts": [
        "count_id", "count_date", "warehouse_id", "sku_id", "book_quantity_each",
        "counted_quantity", "uom", "count_method", "counter_id",
    ],
    "uom_conversions": [
        "effective_date", "warehouse_id", "sku_id", "product_family", "case_uom",
        "each_uom", "units_per_case", "receipt_rate_cents", "outbound_rate_cents",
        "storage_rate_cents",
    ],
    "customs_declarations": [
        "declaration_id", "asn_id", "warehouse_id", "entry_type", "declaration_status",
        "declared_at", "expected_arrival_date", "freeze_start_at", "freeze_end_at",
        "document_version",
    ],
}

SORT_FIELDS = {
    "asn_receipts": ("event_date", "event_id"),
    "dc_movements": ("movement_date", "movement_id"),
    "cycle_counts": ("count_date", "count_id"),
    "uom_conversions": ("effective_date", "warehouse_id", "sku_id"),
    "customs_declarations": ("declaration_id",),
}


def params_of(seed: dict) -> dict:
    return {key: seed.get(key, default) for key, default in DEFAULTS.items()}


def _sku(index: int) -> str:
    return f"SKU-{index + 1:04d}"


def _catalog(params: dict) -> dict:
    out = {}
    options = params["case_pack_options"]
    for warehouse_id in params["distribution_centers"]:
        for index in range(params["sku_count"]):
            sku_id = _sku(index)
            out[(warehouse_id, sku_id)] = {
                "warehouse_id": warehouse_id,
                "sku_id": sku_id,
                "product_family": ["AMBIENT", "CHILLED", "FRAGILE"][index % 3],
                "case_uom": "CASE",
                "each_uom": "EACH",
                "units_per_case": options[index % len(options)],
                "receipt_rate_cents": params["threepl_receipt_cents_per_each"],
                "outbound_rate_cents": params["threepl_outbound_cents_per_each"],
                "storage_rate_cents": params["threepl_storage_cents_per_each"],
            }
    return out


def _quantity_each(quantity: int, uom: str, conversion: dict) -> int:
    return quantity * (conversion["units_per_case"] if uom == "CASE" else 1)


def _shipments(seed_value: int, day_index: int, day, params: dict) -> tuple[list, list]:
    rng = random.Random(f"{seed_value}:{day_index}:asn")
    rows, declarations = [], []
    warehouses = params["distribution_centers"]
    transit_lo, transit_hi = params["transit_days"]
    modulo = params["customs_declaration_modulo"]
    for asn_index in range(rng.randint(*params["asns_per_day"])):
        stamp = day.strftime("%Y%m%d")
        asn_id = f"ASN-{stamp}-{asn_index + 1:03d}"
        warehouse_id = warehouses[(day_index + asn_index) % len(warehouses)]
        expected = day + timedelta(days=rng.randint(transit_lo, transit_hi))
        has_customs = modulo > 0 and (day_index * 5 + asn_index) % modulo == 0
        customs_id = f"CUS-{asn_id}" if has_customs else ""
        control_id = f"856-{stamp}-{asn_index + 1:03d}"
        for line_index in range(rng.randint(*params["lines_per_asn"])):
            line_id = f"{asn_id}-{line_index + 1:02d}"
            uom = "CASE" if rng.randrange(4) else "EACH"
            quantity = rng.randint(2, 8) if uom == "CASE" else rng.randint(12, 96)
            rows.append({
                "event_id": f"{line_id}-SHIPPED",
                "asn_id": asn_id,
                "asn_line_id": line_id,
                "edi_control_id": control_id,
                "warehouse_id": warehouse_id,
                "sku_id": _sku(rng.randrange(params["sku_count"])),
                "supplier_id": f"SUP-{(day_index + asn_index) % 7 + 1:03d}",
                "event_type": "SHIPPED",
                "event_date": day.isoformat(),
                "quantity": quantity,
                "uom": uom,
                "expected_receipt_date": expected.isoformat(),
                "customs_declaration_id": customs_id,
            })
        if has_customs:
            declared = datetime.combine(day - timedelta(days=1), time(8, 0))
            declared += timedelta(minutes=asn_index)
            freeze_end = datetime.combine(
                expected - timedelta(days=1), time(params["customs_cutoff_hour"], 0))
            declarations.append({
                "declaration_id": customs_id,
                "asn_id": asn_id,
                "warehouse_id": warehouse_id,
                "entry_type": "CONSUMPTION",
                "declaration_status": (
                    "SUBMITTED" if (day_index + asn_index) % 4 == 0 else "CLEARED"),
                "declared_at": declared.isoformat(sep=" "),
                "expected_arrival_date": expected.isoformat(),
                "freeze_start_at": (
                    freeze_end - timedelta(hours=params["customs_freeze_hours"])
                ).isoformat(sep=" "),
                "freeze_end_at": freeze_end.isoformat(sep=" "),
                "document_version": 1,
            })
    return rows, declarations


def _receipts_for_day(seed_value: int, day_index: int, day, params: dict) -> list:
    start = parse_vtime(params["vtime_start"]).date() if "vtime_start" in params else None
    del start  # dates are always supplied by the caller; this helper has no clock reads.
    rows = []
    lookback = params["transit_days"][1]
    for prior_index in range(max(0, day_index - lookback), day_index):
        prior_day = day - timedelta(days=day_index - prior_index)
        shipments, _ = _shipments(seed_value, prior_index, prior_day, params)
        for shipped in shipments:
            if shipped["expected_receipt_date"] != day.isoformat():
                continue
            received = dict(shipped)
            received["event_id"] = shipped["event_id"].replace("-SHIPPED", "-RECEIVED")
            received["event_type"] = "RECEIVED"
            received["event_date"] = day.isoformat()
            rows.append(received)
    return sorted(rows, key=lambda row: row["event_id"])


def _movements_for_day(seed_value: int, day_index: int, day, params: dict,
                       catalog: dict, state: dict) -> list:
    rng = random.Random(f"{seed_value}:{day_index}:movements")
    rows = []
    warehouses = params["distribution_centers"]
    for index in range(rng.randint(*params["movements_per_day"])):
        warehouse_id = rng.choice(warehouses)
        sku_id = _sku(rng.randrange(params["sku_count"]))
        key = (warehouse_id, sku_id)
        conversion = catalog[key]
        uom = "CASE" if rng.randrange(4) else "EACH"
        outbound = day_index >= 2 and rng.randrange(5) != 0
        available = state[key]
        if outbound and available <= 0:
            outbound = False
        if outbound:
            if uom == "CASE" and available >= conversion["units_per_case"]:
                quantity = rng.randint(1, min(4, available // conversion["units_per_case"]))
            else:
                uom = "EACH"
                quantity = rng.randint(1, min(48, available))
        else:
            quantity = (rng.randint(1, 3) if uom == "CASE" else rng.randint(12, 48))
        movement_type = "OUTBOUND_SHIP" if outbound else "RETURN_RECEIPT"
        prefix = "SO" if outbound else "RTN"
        quantity_each = _quantity_each(quantity, uom, conversion)
        state[key] += -quantity_each if outbound else quantity_each
        rows.append({
            "movement_id": f"MOV-{day.strftime('%Y%m%d')}-{index + 1:04d}",
            "movement_date": day.isoformat(),
            "warehouse_id": warehouse_id,
            "sku_id": sku_id,
            "movement_type": movement_type,
            "quantity": quantity,
            "uom": uom,
            "reference_id": f"{prefix}-{day.strftime('%Y%m%d')}-{index + 1:04d}",
            "threepl_id": "3PL-ATLAS",
        })
    return rows


def _counts_for_day(seed_value: int, day_index: int, day, params: dict,
                    catalog: dict, state: dict) -> list:
    rng = random.Random(f"{seed_value}:{day_index}:counts")
    rows = []
    pairs = sorted(catalog)
    requested = rng.randint(*params["cycle_counts_per_day"])
    for index, key in enumerate(rng.sample(pairs, min(requested, len(pairs)))):
        warehouse_id, sku_id = key
        conversion = catalog[key]
        book = state[key]
        uom = "CASE" if book >= conversion["units_per_case"] and rng.randrange(2) == 0 else "EACH"
        shrink = rng.randint(0, min(12, book))
        if uom == "CASE":
            counted = max(0, (book - shrink) // conversion["units_per_case"])
            counted_each = counted * conversion["units_per_case"]
        else:
            counted_each = book - shrink
            counted = counted_each
        state[key] = counted_each
        rows.append({
            "count_id": f"CC-{day.strftime('%Y%m%d')}-{index + 1:04d}",
            "count_date": day.isoformat(),
            "warehouse_id": warehouse_id,
            "sku_id": sku_id,
            "book_quantity_each": book,
            "counted_quantity": counted,
            "uom": uom,
            "count_method": "RF_SCAN",
            "counter_id": f"EMP-{(day_index + index) % 9 + 1:03d}",
        })
    return rows


def _operational_rows(seed_value: int, day_index: int, day, params: dict) -> tuple[list, list]:
    """Replay the deterministic inventory ledger to produce today's movements/counts.

    The replay is what lets an outbound shipment never exceed available stock and a
    cycle count carry the WMS book quantity. It depends only on seed and day_index.
    """
    catalog = _catalog(params)
    state = {key: 0 for key in catalog}
    first_day = day - timedelta(days=day_index)
    movements = counts = []
    for index in range(day_index + 1):
        current_day = first_day + timedelta(days=index)
        for receipt in _receipts_for_day(seed_value, index, current_day, params):
            key = (receipt["warehouse_id"], receipt["sku_id"])
            state[key] += _quantity_each(
                int(receipt["quantity"]), receipt["uom"], catalog[key])
        movements = _movements_for_day(
            seed_value, index, current_day, params, catalog, state)
        counts = _counts_for_day(seed_value, index, current_day, params, catalog, state)
    return movements, counts


def _write_csv(path: Path, table: str, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS[table], lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: tuple(row[k] for k in SORT_FIELDS[table])))


def write_day(workspace: Path, seed: dict, day_index: int, day) -> dict:
    """Land one complete EDI partition, pure in (seed, day_index)."""
    params = params_of(seed)
    params["vtime_start"] = seed["vtime_start"]
    shipments, declarations = _shipments(seed["seed"], day_index, day, params)
    receipts = _receipts_for_day(seed["seed"], day_index, day, params)
    movements, counts = _operational_rows(seed["seed"], day_index, day, params)
    conversions = []
    for conversion in _catalog(params).values():
        conversions.append({"effective_date": day.isoformat(), **conversion})
    payloads = {
        "asn_receipts": shipments + receipts,
        "dc_movements": movements,
        "cycle_counts": counts,
        "uom_conversions": conversions,
        "customs_declarations": declarations,
    }
    folder = Path(workspace) / "data" / "landing" / "inventory_edi" / f"dt={day}"
    for table in sorted(payloads):
        _write_csv(folder / f"{table}.csv", table, payloads[table])
    return {table: len(rows) for table, rows in sorted(payloads.items())}


def write_ops_logs(workspace: Path, seed: dict) -> None:
    """Deterministic historical ops logs under ``<world>/var/logs``.

    Every timestamp derives from ``seed['vtime_start']`` plus the day index and
    all jitter comes from one seeded RNG, so two builds of the same seed write
    byte-identical files. Counts replay the same deterministic generators the
    landing writer uses, so the log narrative matches the landed data.
    """
    params = params_of(seed)
    start = parse_vtime(seed["vtime_start"]).date()
    days = int(seed["days"])
    rng = random.Random(f"{seed['seed']}:logs")
    ingest, sftp, audit = [], [], []
    for day_index in range(days):
        day = start + timedelta(days=day_index)
        shipments, declarations = _shipments(seed["seed"], day_index, day, params)
        asn_ids = sorted({row["asn_id"] for row in shipments})

        def ts(hour: int, minute: int) -> str:
            return f"{day.isoformat()}T{hour:02d}:{minute:02d}:{rng.randint(0, 59):02d}Z"

        sftp.append(f"{ts(0, 5)} INFO connect sftp.northstar-logistics.example:22"
                    f" user=edi_inbound")
        for position, asn_id in enumerate(asn_ids):
            sftp.append(f"{ts(0, 6 + position)} INFO get /outbound/856/{asn_id}.edi"
                        f" -> inbound/{asn_id}.edi.part renamed, sha256 ok")
        if asn_ids and rng.random() < 0.18:
            sftp.append(f"{ts(0, 12)} WARN transfer reset by peer for"
                        f" {rng.choice(asn_ids)}.edi; retry 1/3 succeeded")
        ingest.append(f"{ts(0, 30)} INFO edi856_poll: {len(asn_ids)} ASN file(s),"
                      f" {len(shipments)} line(s), {len(declarations)} customs"
                      f" declaration(s) landed under dt={day.isoformat()}")
        if asn_ids and rng.random() < 0.15:
            ingest.append(f"{ts(0, 33)} WARN asn_translate: {rng.choice(asn_ids)}"
                          f" duplicate EDI control id; file quarantined for review")
        ingest.append(f"{ts(16, 10)} INFO cycle_count_import: posted"
                      f" {rng.randint(*params['cycle_counts_per_day'])} RF count"
                      f" batch(es) from wms-rf-01")
        for job, hour, minute, base in (("edi_poll", 0, 30, 20),
                                        ("inventory_load", 0, 36, 75),
                                        ("inventory_dbt", 0, 47, 180),
                                        ("threepl_handoff", 0, 58, 45),
                                        ("customs_close", 1, 4, 30)):
            audit.append(f"{ts(hour, minute)} INFO job={job}"
                         f" run_date={day.isoformat()} status=SUCCESS"
                         f" duration_s={base + rng.randint(-3, 6)}")
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
    start = parse_vtime(seed["vtime_start"])
    for index in range(seed["days"]):
        write_day(workspace, seed, index, (start + timedelta(days=index)).date())
    write_ops_logs(workspace, seed)


def load_raw(workspace: Path, db_path: Path) -> None:
    loader = Path(workspace) / "loader" / "load_inventory.py"
    result = subprocess.run(
        [sys.executable, str(loader),
         "--landing", str(Path(workspace) / "data" / "landing" / "inventory_edi"),
         "--db", str(db_path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"inventory_load failed:\n{result.stdout}\n{result.stderr}")


def write_workflow(root: Path, seed: dict, manifest: dict) -> None:
    workflow.seed(root, Path(__file__).resolve().parent, manifest["workflow"],
                  scheduler.load(root)["vnow"])
