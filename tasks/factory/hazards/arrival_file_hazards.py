"""H01-H07: mutate *future* landing payloads, never the t0 workspace.

The input tree is ``R1/dt=YYYY-MM-DD/events.csv`` through ``R4/...``.  The
scheduled factory-arrival job copies one R directory into workspace/data on
each night.  Every mutator is deterministic for a seed and leaves the original
business events intact unless its contract specifically introduces an event.
"""
from __future__ import annotations

import csv
import hashlib
import io
import random
from pathlib import Path


ARRIVAL_HAZARDS = {
    "H01": "delivery_manifest_sidecar",
    "H02": "permuted_column_order",
    "H03": "extra_nullable_columns",
    "H04": "part_and_temp_files",
    "H05": "utf8_bom_and_crlf_quotes",
    "H06": "empty_heartbeat_partition",
    "H07": "backdated_redelivery_in_old_dt",
}


def _csv_files(root: Path, night: int) -> list[Path]:
    return sorted((root / f"R{night}").glob("dt=*/events.csv"))


def _read(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _write(path: Path, fields: list[str], rows: list[dict], *, bom=False,
           lineterminator="\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8",
                   newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore",
                                lineterminator=lineterminator)
        writer.writeheader()
        writer.writerows(rows)


def apply_arrival_hazards(future_root: Path, hazard_ids: list[str] | tuple[str, ...],
                          *, seed: int, historical_day: str) -> list[dict]:
    """Apply selected H01-H07 in place; return exact staged-file evidence."""
    root = Path(future_root)
    results: list[dict] = []
    for hid in hazard_ids:
        if hid not in ARRIVAL_HAZARDS:
            raise ValueError(f"unknown arrival hazard: {hid}")
        night = {"H05": 3, "H07": 3}.get(hid, 2)
        paths = _csv_files(root, night)
        if not paths:
            raise ValueError(f"{hid} requires R{night}/dt=*/events.csv")
        path = paths[0]
        fields, rows = _read(path)
        added: list[Path] = []
        if hid == "H01":
            sidecar = path.parent / "_delivery_manifest.csv"
            _write(sidecar, ["filename", "rows"],
                   [{"filename": path.name, "rows": len(rows)}])
            checksum = path.parent / "_checksums.sha256"
            checksum.write_text(hashlib.sha256(path.read_bytes()).hexdigest()
                                + "  events.csv\n", encoding="utf-8")
            added = [sidecar, checksum]
        elif hid == "H02":
            shuffled = fields[:]
            random.Random(f"{seed}:H02").shuffle(shuffled)
            if shuffled == fields:
                shuffled.reverse()
            _write(path, shuffled, rows)
            added = [path]
        elif hid == "H03":
            extra = ["fee_bps", "settlement_currency"]
            for row in rows:
                row.update({"fee_bps": "", "settlement_currency": ""})
            _write(path, fields + extra, rows)
            added = [path]
        elif hid == "H04":
            part = path.parent / "events.csv.part"
            part.write_bytes(path.read_bytes()[:31])
            temp = path.parent / "._events.csv"
            temp.write_text("transfer in progress\n", encoding="utf-8")
            added = [part, temp]
        elif hid == "H05":
            # A quoted entity label exercises comma/newline handling; this is
            # still a legitimate business key, so the oracle must preserve it.
            if rows:
                rows[0]["entity_id"] = rows[0]["entity_id"] + ",\nWest"
            _write(path, fields, rows, bom=True, lineterminator="\r\n")
            added = [path]
        elif hid == "H06":
            heartbeat = path.parent / "events-heartbeat.csv"
            _write(heartbeat, fields, [])
            added = [heartbeat]
        elif hid == "H07":
            backfill = root / "R3" / f"dt={historical_day}" / "events-backfill.csv"
            row = dict(rows[0])
            row["event_id"] = f"EV-H07-{seed}"
            row["business_id"] = f"B-H07-{seed}"
            row["business_day"] = historical_day
            _write(backfill, fields, [row])
            added = [backfill]
        results.append({"id": hid, "night": night,
                        "files": [str(p.relative_to(root)) for p in added]})
    return results
