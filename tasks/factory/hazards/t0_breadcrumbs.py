"""Indirect t0 clues for sealed future-night hazards.

Future payloads stay under ``build/factory_future``. A breadcrumb is a
historical file, a contract page, or a rotated log the agent can read at t0.
Clues state the operational rule. They do not include repair code.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


HAZARD_IDS = tuple(f"H{i:02d}" for i in range(1, 15))

# Phrases that would name the repair. Breadcrumb text must not contain them.
SPOILER_PHRASES = (
    "ingest_feed",
    "fold_ledger",
    "publish_handoff",
    "factory_events.py",
    "POLICY",
    "ROW_NUMBER",
    "def load",
    "def fold",
    "def publish",
)


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if text not in existing:
        path.write_text(existing + text, encoding="utf-8")


def _prepend_once(path: Path, text: str, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if marker not in existing:
        path.write_text(text + existing, encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _historical_events(world: Path, historical_day: str) -> Path | None:
    matches = sorted((world / "workspace").glob(f"**/dt={historical_day}/events.csv"))
    return matches[0] if matches else None


def _routine_daemon_context(day: str) -> str:
    return (
        f"{day}T00:02:01Z daemon boot sequence=nightly-sync worker=ingest-03 build=7.18.2\n"
        f"{day}T00:02:03Z config loaded partner=primary timezone=UTC retry_limit=3\n"
        f"{day}T00:03:11Z partition scan discovered=24 ready=24 delayed=0\n"
        f"{day}T00:03:27Z schema registry source=partner-feed active=3.0\n"
        f"{day}T00:04:02Z queue poll received=18 acknowledged=18 pending=0\n"
        f"{day}T00:04:46Z batch opened partitions=24 expected_rows=18420\n"
        f"{day}T00:05:19Z normalization complete accepted=18420 rejected=0\n"
        f"{day}T00:06:08Z event ids checked unique=18420 duplicates=0\n"
        f"{day}T00:06:52Z watermark observed previous={day}T00:00:00Z\n"
        f"{day}T00:07:25Z warehouse staging rows=18420 elapsed_ms=941\n"
        f"{day}T00:08:13Z checkpoint persisted shard_count=24 checksum_status=ok\n"
        f"{day}T00:08:47Z batch closed status=complete retryable=false\n"
    )


def _routine_sftp_context(day: str) -> str:
    return (
        f"{day}T00:01:04Z transport session=nightly-partner peer=feed-gateway-2\n"
        f"{day}T00:01:06Z ssh handshake cipher=aes256-gcm host_key=verified\n"
        f"{day}T00:01:09Z remote directory listed objects=24 bytes=918204\n"
        f"{day}T00:01:15Z keepalive interval_seconds=30 status=active\n"
        f"{day}T00:01:32Z transfer started object_count=24 mode=binary\n"
        f"{day}T00:01:59Z transfer progress completed=8 total=24 bytes=306071\n"
        f"{day}T00:02:18Z transfer progress completed=16 total=24 bytes=612883\n"
        f"{day}T00:02:43Z transfer progress completed=24 total=24 bytes=918204\n"
        f"{day}T00:02:51Z remote size verification passed objects=24\n"
        f"{day}T00:03:02Z local rename operations=24 temporary_objects=0\n"
        f"{day}T00:03:08Z session summary transferred=24 failed=0 retries=0\n"
        f"{day}T00:03:11Z transport closed reason=normal\n"
    )


def stage_breadcrumbs(world_dir: Path, hazard_ids: list[str] | tuple[str, ...],
                      *, historical_day: str, seed: int,
                      stressor_ids: list[str] | tuple[str, ...] | None = None) -> list[dict]:
    """Stage one indirect clue per active hazard. Returns the files written.

    ``stressor_ids`` adds timestamped ingest audit entries for S03, S06,
    S07, and S08.
    """
    world = Path(world_dir)
    stressors = {str(item).split("_", 1)[0] for item in (stressor_ids or ())}
    unknown = [hid for hid in hazard_ids if hid not in HAZARD_IDS]
    if unknown:
        raise ValueError(f"unknown hazards for breadcrumbs: {unknown}")
    docs = world / "workspace/docs"
    logs = world / "var/logs"
    written: list[dict] = []
    contract_sections: list[str] = []
    runbook_sections: list[str] = []
    ingest_lines: list[str] = []
    sftp_lines: list[str] = []

    def record(hid: str, *paths: Path) -> None:
        written.append({"id": hid, "files": [str(path.relative_to(world)) for path in paths]})

    for hid in hazard_ids:
        if hid == "H01":
            archive = docs / "archive" / f"dt={historical_day}"
            events = _historical_events(world, historical_day)
            row_count = 0
            digest = "0" * 64
            if events and events.is_file():
                payload = events.read_bytes()
                digest = hashlib.sha256(payload).hexdigest()
                row_count = max(payload.count(b"\n") - 1, 0)
            manifest = archive / "_delivery_manifest.csv"
            checksum = archive / "_checksums.sha256"
            _write(manifest,
                   "filename,rows\n"
                   f"events.csv,{row_count}\n")
            _write(checksum, f"{digest}  events.csv\n")
            ingest_lines.append(
                f"{historical_day}T02:14:08Z partner gateway wrote _delivery_manifest.csv "
                f"and _checksums.sha256 next to events.csv in the processed "
                f"dt={historical_day} drop. The manifest is a transfer sidecar, "
                "not a business event file. An archived copy is kept with the day folder.\n"
            )
            record(hid, manifest, checksum)
        elif hid == "H02":
            ingest_lines.append(
                f"{historical_day}T03:02:41Z ingest warning: vendor retry sample "
                "listed the same fields in a different order from the integration "
                "guide. Header names still matched.\n"
            )
            record(hid, logs / "ingest_daemon.log")
        elif hid == "H03":
            ingest_lines.append(
                f"{historical_day}T03:06:12Z ingest warning: partner notice says v3.1 "
                "resync files append optional fee_bps and settlement_currency. "
                "Historical files do not carry those headers.\n"
            )
            record(hid, logs / "ingest_daemon.log")
        elif hid == "H04":
            sftp_lines.append(
                f"{historical_day}T01:44:19Z sftp retry left temporary marker files "
                "in the drop for 40s, then kept one completed events file. The "
                "markers were not batches.\n"
            )
            record(hid, logs / "sftp_sync.log")
        elif hid == "H05":
            sftp_lines.append(
                f"{historical_day}T05:11:02Z sftp note: northwind export arrived "
                "with a byte-order mark on the first header and CRLF line endings. "
                "A quoted merchant label contained a comma.\n"
            )
            record(hid, logs / "sftp_sync.log")
        elif hid == "H06":
            sftp_lines.append(
                f"{historical_day}T04:20:33Z sftp delivered events-heartbeat.csv with "
                "a header and zero rows next to events.csv. The file checks channel availability.\n"
            )
            record(hid, logs / "sftp_sync.log")
        elif hid == "H07":
            ingest_lines.append(
                f"{historical_day}T06:40:18Z ingest audit: partner backfill landed in "
                f"an already-processed dt={historical_day} folder after the watermark "
                "had moved past that day. The file name was events-backfill.csv.\n"
            )
            record(hid, logs / "ingest_daemon.log")
        elif hid == "H08":
            ingest_lines.append(
                f"{historical_day}T07:15:44Z contract reminder: a retry carried two "
                "rows for one business key. The later transmission was the older "
                "copy. Arrival order was not the version order.\n"
            )
            record(hid, logs / "ingest_daemon.log")
        elif hid == "H09":
            ingest_lines.append(
                f"{historical_day}T08:01:09Z partner repeated an event_id in "
                "events-retry.csv with the same bytes as the original batch. "
                "The identifier is still one event.\n"
            )
            record(hid, logs / "ingest_daemon.log")
        elif hid == "H10":
            ingest_lines.append(
                f"{historical_day}T09:12:55Z clearing note: a void for an open day "
                "removes the payment. A void aimed at a closed day waits for an "
                "approved correction and does not rewrite the snapshot by itself.\n"
            )
            record(hid, logs / "ingest_daemon.log")
        elif hid == "H11":
            ingest_lines.append(
                f"{historical_day}T10:05:27Z timestamp note: a partner file mixed "
                "offsets around the nightly roll. Two rows with a blank business "
                "day were questioned because the wall clock and the UTC instant "
                "did not fall on the same date.\n"
            )
            record(hid, logs / "ingest_daemon.log")
        elif hid == "H12":
            sftp_lines.append(
                f"{historical_day}T11:22:03Z scheduler audit: nightly loader ack was "
                "lost and the job was started again. Counts after the replay matched "
                "the first start.\n"
            )
            record(hid, logs / "sftp_sync.log")
        elif hid == "H13":
            ingest_lines.append(
                f"{historical_day}T12:18:36Z corrections desk: a mid-horizon "
                "restatement needs an approved correction for that business day. "
                "An open ticket without approval leaves the closed snapshot as it was.\n"
            )
            record(hid, logs / "ingest_daemon.log")
        elif hid == "H14":
            ingest_lines.append(
                f"{historical_day}T13:09:14Z historical backfill used a blank version "
                "on otherwise valid rows. Blank version means version 1.\n"
            )
            record(hid, logs / "ingest_daemon.log")

    if "S06" in stressors:
        ingest_lines.append(
            f"{historical_day}T06:15:44Z cutover audit schema=v3 "
            "amount_basis=net_post_split_cents split_adjustment=0 "
            "source=partner_extract reconciliation=matched.\n"
        )
    if "S08" in stressors:
        ingest_lines.append(
            f"{historical_day}T06:32:10Z cutover audit schema=v3 "
            "amount_basis=net_post_fee_cents fee_adjustment=0 "
            "source=partner_extract reconciliation=matched.\n"
        )
    if "S07" in stressors:
        ingest_lines.append(
            f"{historical_day}T06:48:19Z reconciliation audit: platform-level "
            "settlement rows arrived without an entity_id; the ledger total "
            "includes unattributed platform rows alongside entity-attributed rows.\n"
        )
    if "S03" in stressors:
        ingest_lines.append(
            f"{historical_day}T07:55:02Z ingest audit: retry drop "
            "events-retry.csv repeated an event_id from primary batch "
            "events.csv; the raw event store must not retain duplicate "
            "event_id rows.\n"
        )

    if any(hid in set(hazard_ids) for hid in ("H02", "H03", "H07", "H08", "H09", "H10", "H13")):
        contract_sections.append(
            "The header name is what identifies a field, so field names stay stable "
            "across deliveries. Optional values may be absent or blank where the "
            "contract allows this. Use event identity and version to interpret repeated "
            "deliveries for a business day. A correction to a closed period requires "
            "approval that can be traced to its source.\n"
        )
    if any(hid in set(hazard_ids) for hid in ("H04", "H05", "H06", "H12")):
        runbook_sections.append(
            "Check transport status and file completion before interpreting the "
            "business data. Read each completed transfer in the context of its contents "
            "and delivery history. When a transfer is retried, reconcile the result "
            "with the original run.\n"
        )
    if contract_sections:
        body = (
            "# Partner feed contract\n\n"
            "The nightly partner feed follows the rules below for interpreting "
            "records, repeated deliveries, and approvals.\n\n"
            + "\n".join(contract_sections)
        )
        _write(docs / "partner_feed_contract.md", body)
    if runbook_sections:
        body = (
            "# Ingest operations runbook\n\n"
            "Use these operating principles when checking transfers, retries, "
            "and batch completion.\n\n"
            + "\n".join(runbook_sections)
        )
        _write(docs / "ops_runbook.md", body)
    _prepend_once(logs / "ingest_daemon.log", _routine_daemon_context(historical_day),
                  "daemon boot sequence=nightly-sync")
    _prepend_once(logs / "sftp_sync.log", _routine_sftp_context(historical_day),
                  "transport session=nightly-partner")
    if ingest_lines:
        _append(logs / "ingest_daemon.log", "".join(ingest_lines))
    if sftp_lines:
        _append(logs / "sftp_sync.log", "".join(sftp_lines))
    for hid in hazard_ids:
        if hid in {"H04", "H05", "H06", "H12"} and (logs / "sftp_sync.log").is_file():
            for item in written:
                if item["id"] == hid and "var/logs/sftp_sync.log" not in item["files"]:
                    item["files"].append("var/logs/sftp_sync.log")
        if (logs / "ingest_daemon.log").is_file() and hid not in {"H04", "H05", "H06", "H12"}:
            for item in written:
                if item["id"] == hid and "var/logs/ingest_daemon.log" not in item["files"]:
                    item["files"].append("var/logs/ingest_daemon.log")
    if stressors & {"S03", "S06", "S07", "S08"} and (logs / "ingest_daemon.log").is_file():
        for item in written:
            if "var/logs/ingest_daemon.log" not in item["files"]:
                item["files"].append("var/logs/ingest_daemon.log")
    contract_rel = "workspace/docs/partner_feed_contract.md"
    extra = []
    if ((world / contract_rel).is_file()
            and not any(contract_rel in item["files"] for item in written)):
        extra.append({"id": "stressor-contract", "files": [contract_rel]})
    for sid in ("S03", "S06", "S07", "S08"):
        if sid in stressors:
            extra.append({"id": sid, "files": ["var/logs/ingest_daemon.log"]})
    _assert_clean(world, written + extra + [
        {"id": "routine-ops", "files": [
            "var/logs/ingest_daemon.log", "var/logs/sftp_sync.log"]},
    ])
    return written


def _assert_clean(world: Path, written: list[dict]) -> None:
    for item in written:
        for relative in item["files"]:
            text = (world / relative).read_text(encoding="utf-8")
            for phrase in SPOILER_PHRASES:
                if phrase in text:
                    raise ValueError(f"breadcrumb {item['id']} contains {phrase!r}")
            if "EV-FUT-" in text or "factory_future" in text:
                raise ValueError(f"breadcrumb {item['id']} copies a future payload")
