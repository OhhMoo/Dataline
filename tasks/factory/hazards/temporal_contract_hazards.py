"""H08-H14: clocked event semantics and scheduler retry hazards."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from .arrival_file_hazards import _csv_files, _read, _write


TEMPORAL_HAZARDS = {
    "H08": "intra_file_out_of_order_versions",
    "H09": "byte_identical_event_retry",
    "H10": "late_void_on_open_vs_closed_window",
    "H11": "cross_day_timezone_boundary",
    "H12": "idempotent_rerun_double_trigger",
    "H13": "superseding_correction_ticket_mid_horizon",
    "H14": "null_version_legacy_fallback",
}


def _new(base: dict, hid: str, seed: int, *, business_id: str | None = None,
         version: str = "1", kind: str = "settle") -> dict:
    row = dict(base)
    row.update(event_id=f"EV-{hid}-{seed}-{version}-{kind}",
               business_id=business_id or f"B-{hid}-{seed}",
               version=version, event_type=kind)
    return row


def _stage_draft_hold(root: Path, historical_day: str) -> Path:
    """Add both R3 system holds once, preserving controller approvals."""
    approvals = root / "R3" / "_approvals.csv"
    fields = ["business_day", "ticket", "approved_by"]
    rows: list[dict] = []
    if approvals.is_file():
        fields, rows = _read(approvals)
    changed = False
    for approver, ticket in (("ops_auto_draft", "DRAFT-HOLD"),
                             ("ops_ingestor", "INGEST-HOLD")):
        if not any(str(row.get("approved_by") or "").strip() == approver
                   and row.get("business_day") == historical_day for row in rows):
            rows.append({"business_day": historical_day, "ticket": ticket,
                         "approved_by": approver})
            changed = True
    if changed:
        _write(approvals, fields, rows)

    log = root.parent.parent / "var" / "logs" / "ingest_daemon.log"
    line = (
        f"{historical_day}T11:42:07Z corrections audit: automated system holds "
        "(ops_auto_draft, ops_ingestor) are written to "
        "warehouse.factory_corrections during nightly syncs; system holds "
        "are not controller sign-off.\n"
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    existing = log.read_text(encoding="utf-8") if log.is_file() else ""
    if line not in existing:
        log.write_text(existing + line, encoding="utf-8")
    return approvals


def apply_temporal_hazards(future_root: Path,
                           hazard_ids: list[str] | tuple[str, ...],
                           *, seed: int, historical_day: str,
                           ticket: str | None = None,
                           controller: str | None = None) -> list[dict]:
    """Apply H08-H14 in place and return night/row effects.

    H12 is recorded as a retry trigger for the composer to wire as a second
    scheduler script job. Every temporal hazard stages unauthorized system
    holds in an approval sidecar consumed only at R3.
    ``controller`` is the same identifier t0 wrote on
    ``warehouse.factory_corrections.approved_by``. Night 3's approval uses
    that identifier. The literal ``controller`` is written only when that
    string is itself the authority name.
    """
    root = Path(future_root)
    results: list[dict] = []
    for hid in hazard_ids:
        if hid not in TEMPORAL_HAZARDS:
            raise ValueError(f"unknown temporal hazard: {hid}")
        night = {"H09": 4, "H11": 3, "H13": 3}.get(hid, 2)
        paths = _csv_files(root, night)
        if not paths:
            raise ValueError(f"{hid} requires R{night}/dt=*/events.csv")
        path = paths[0]
        fields, rows = _read(path)
        base = rows[0]
        added: list[str] = []
        if hid == "H08":
            key = f"B-H08-{seed}"
            high = _new(base, hid, seed, business_id=key, version="2")
            high["amount_cents"] = str(int(base["amount_cents"]) + 700)
            low = _new(base, hid + "-STALE", seed, business_id=key)
            low["delivered_at"] = base["delivered_at"][:10] + "T23:12:00+00:00"
            rows.extend([high, low])
            added = [high["event_id"], low["event_id"]]
        elif hid == "H09":
            retry = path.parent / "events-retry.csv"
            _write(retry, fields, [dict(base)])
            added = [str(retry.relative_to(root))]
        elif hid == "H10":
            open_void = _new(base, hid + "-OPEN", seed, version="2", kind="void")
            open_void["business_id"] = base["business_id"]
            rows.append(open_void)
            # A closed historical day's correction arrives one night later.
            r3 = _csv_files(root, 3)[0]
            f3, rows3 = _read(r3)
            closed_void = _new(base, hid + "-CLOSED", seed, version="2", kind="void")
            closed_void["business_id"] = f"B-CLOSED-{seed}"
            closed_void["business_day"] = historical_day
            rows3.append(closed_void)
            _write(r3, f3, rows3)
            added = [open_void["event_id"], closed_void["event_id"]]
        elif hid == "H11":
            day = date.fromisoformat(path.parent.name.split("=", 1)[1])
            before = _new(base, hid + "-BEFORE", seed)
            before["business_day"] = ""
            before["delivered_at"] = f"{day}T23:20:00+00:00"
            after = _new(base, hid + "-AFTER", seed)
            after["business_day"] = ""
            after["delivered_at"] = f"{day}T19:40:00-04:00"
            rows.extend([before, after])
            added = [before["event_id"], after["event_id"]]
        elif hid == "H12":
            added = ["factory_load_retry@R2"]
        elif hid == "H13":
            prior = date.fromisoformat(historical_day)
            approved_day = (prior + timedelta(days=1)).isoformat()
            approved = _new(base, hid + "-APPROVED", seed, version="2")
            approved["business_id"] = f"B-APPROVED-{seed}"
            approved["business_day"] = approved_day
            blocked = _new(base, hid + "-BLOCKED", seed, version="2")
            blocked["business_id"] = f"B-CLOSED-{seed}"
            blocked["business_day"] = historical_day
            rows.extend([approved, blocked])
            approvals = root / "R3" / "_approvals.csv"
            approver = controller if controller else "controller"
            _write(approvals, ["business_day", "ticket", "approved_by"],
                   [{"business_day": approved_day,
                     "ticket": (ticket or f"FACT-{seed}") + "-R3",
                     "approved_by": approver},
                    {"business_day": historical_day,
                     "ticket": (ticket or f"FACT-{seed}") + "-DRAFT",
                     "approved_by": "ops_auto_draft"}])
            added = [approved["event_id"], blocked["event_id"],
                     str(approvals.relative_to(root))]
        elif hid == "H14":
            legacy = _new(base, hid, seed)
            legacy["version"] = ""
            rows.append(legacy)
            added = [legacy["event_id"]]
        if hid not in {"H09", "H12"}:
            _write(path, fields, rows)
        results.append({"id": hid, "night": night, "effects": added})
    if hazard_ids:
        approvals = _stage_draft_hold(root, historical_day)
        relative = str(approvals.relative_to(root))
        for result in results:
            if relative not in result["effects"]:
                result["effects"].append(relative)
    return results
