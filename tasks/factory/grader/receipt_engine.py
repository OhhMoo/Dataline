"""Compare t0 warehouse rows and each future publication with an oracle."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .replay_isolation import advance_nights

IDENT = re.compile(r"^[A-Za-z0-9_]+$")
SIGNATURE = ("target", "kind", "consumer", "canonical_hash", "row_count")
DATA_TABLES = (
    "raw.factory_events",
    "warehouse.fct_factory_events",
    "warehouse.factory_handoff",
)
CHECKPOINT_TABLE = "warehouse.factory_checkpoint"


def _q_table(table: str) -> tuple[str, str, str]:
    parts = str(table).split(".")
    if len(parts) != 2 or not all(IDENT.fullmatch(x) for x in parts):
        raise ValueError(f"table must be schema.table with plain identifiers: {table!r}")
    return parts[0], parts[1], f'wh."{parts[0]}"."{parts[1]}"'


def _cell(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float):
        return repr(value)
    return value


def _row_key(row: list) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def row_hash(row: list) -> str:
    """Stable identity of one canonical row. Equality of hashes is row equality."""
    return hashlib.sha256(_row_key(row).encode("utf-8")).hexdigest()


def _fraction(matched: int, denominator: int, *, perfect_if_empty: bool) -> float:
    if denominator <= 0:
        return 1.0 if perfect_if_empty else 0.0
    return round(min(1.0, max(0.0, matched / denominator)), 6)


def table_rows(db: Path, table: str, columns: list[str] | None = None) -> dict:
    """Read a table as canonical rows; preserve duplicate physical rows."""
    import duckdb

    schema, name, quoted = _q_table(table)
    if not Path(db).is_file():
        raise ValueError(f"missing warehouse {db}")
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute(f"ATTACH '{Path(db).as_posix()}' AS wh (READ_ONLY)")
        layout = con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_catalog = 'wh' AND table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position", [schema, name]).fetchall()
        available = [r[0] for r in layout]
        if not available:
            raise ValueError(f"table {table} does not exist in {db}")
        selected = columns or available
        if any(not IDENT.fullmatch(c) or c not in available for c in selected):
            raise ValueError(f"table {table} lacks selected columns {selected!r}; has {available!r}")
        select = ", ".join(f'"{c}"' for c in selected)
        rows = [[_cell(v) for v in row] for row in con.execute(
            f"SELECT {select} FROM {quoted}").fetchall()]
        return {"columns": list(selected), "rows": sorted(rows, key=_row_key)}
    finally:
        con.close()


PARTITION_COLUMN = "source_partition"
PARTITION_TABLE = "raw.factory_events"
RETRY_FILE = re.compile(r"/events-retry\.csv$")
PARTITION_FILE = re.compile(r"(dt=[^/]+)/events(-retry)?\.csv$")
PARTITION_PREFIX = "__source_partition__"


def normalize_partition(value):
    """``.../dt=X/events-retry.csv`` and ``.../dt=X/events.csv`` name one delivery."""
    return RETRY_FILE.sub("/events.csv", value) if isinstance(value, str) else value


def _normalize_partitions(table: str, snap: dict) -> dict:
    """Rows of ``raw.factory_events`` with the retry file folded onto its primary.

    Only the file name changes, never the ``dt=`` folder, and no row is merged,
    so duplicate event_ids still fail the grain and multiset checks.
    """
    if table != PARTITION_TABLE or PARTITION_COLUMN not in (snap.get("columns") or []):
        return snap
    index = snap["columns"].index(PARTITION_COLUMN)
    rows = [[normalize_partition(c) if i == index else c for i, c in enumerate(row)]
            for row in snap["rows"]]
    return {**snap, "rows": sorted(rows, key=_row_key)}


def _csv_event_ids(path: Path) -> set[str]:
    import csv
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return {row["event_id"] for row in csv.DictReader(handle) if row.get("event_id")}
    except (OSError, UnicodeDecodeError, KeyError, csv.Error):
        return set()


def retry_duplicates(world_root: Path, grading: Path) -> set[tuple[str, str]]:
    """``(dt=X, event_id)`` pairs delivered in both ``events.csv`` and ``events-retry.csv``.

    Read from the t0 landing folders and from every planted future night.
    """
    folders = list((Path(world_root) / "workspace").rglob("dt=*"))
    folders += list((Path(grading) / "factory_future").glob("R*/dt=*"))
    pairs: set[tuple[str, str]] = set()
    for folder in folders:
        retry, primary = folder / "events-retry.csv", folder / "events.csv"
        if folder.is_dir() and retry.is_file() and primary.is_file():
            pairs |= {(folder.name, e) for e in _csv_event_ids(retry) & _csv_event_ids(primary)}
    return pairs


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def partition_snapshot_specs(world_root: Path, grading: Path, replay: dict) -> dict:
    """Replay snapshots that let a night receipt be re-hashed under either file name.

    The ledger hashes whole tables, so a receipt can only be matched under the
    retry/primary equivalence by rebuilding its hash. Each night captures the
    column types, the table as stored, and the table with every duplicated
    delivery relabelled to the primary file and to the retry file. SQL does the
    relabel and the ordering, so the order is the ledger's own.
    """
    if PARTITION_TABLE not in set(replay.get("targets") or []):
        return {}
    pairs = retry_duplicates(world_root, grading)
    if not pairs:
        return {}
    schema, name = PARTITION_TABLE.split(".")
    keys = ", ".join(_sql_text(f"{dt}|{event}") for dt, event in sorted(pairs))
    dup = (f"regexp_extract({PARTITION_COLUMN}, '(dt=[^/]+)/events(-retry)?\\.csv$', 1)"
           f" || '|' || event_id IN ({keys})")
    columns = ("SELECT column_name, data_type FROM information_schema.columns "
               f"WHERE table_catalog = 'wh' AND table_schema = '{schema}' "
               f"AND table_name = '{name}' ORDER BY ordinal_position")
    specs = {PARTITION_PREFIX + "columns": {"sql": columns}}
    for label, target in (("stored", None), ("primary", "events.csv"),
                          ("retry", "events-retry.csv")):
        specs[PARTITION_PREFIX + label] = {"sql": _relabel_sql(schema, name, dup, target)}
    return specs


def _relabel_sql(schema: str, name: str, dup: str, target: str | None) -> str:
    """Rows in ledger order, with duplicated deliveries renamed to `target`."""
    # The column list is resolved inside DuckDB so the projection keeps the
    # table's own column order whatever the agent's layout is.
    relabel = (f"* REPLACE (CASE WHEN {dup} THEN regexp_replace({PARTITION_COLUMN}, "
               f"'events(-retry)?\\.csv$', '{target}') ELSE {PARTITION_COLUMN} END "
               f"AS {PARTITION_COLUMN})") if target else "*"
    return (f'SELECT * FROM (SELECT {relabel} FROM wh."{schema}"."{name}") '
            "ORDER BY ALL")


def _ledger_hash(columns: list, rows: list) -> str:
    """``world.ledger.table_contents``'s hash over already-ordered canonical rows."""
    def canon(obj) -> bytes:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("utf-8")
    digest = hashlib.sha256(canon({"table": PARTITION_TABLE,
                                   "columns": [list(c) for c in columns]}))
    for row in rows:
        digest.update(canon([repr(v) if isinstance(v, float) else v for v in row]) + b"\n")
    return digest.hexdigest()


def _partition_hashes(night: dict) -> dict | None:
    snaps = night.get("snapshots") or {}
    taken = {k[len(PARTITION_PREFIX):]: v for k, v in snaps.items()
             if k.startswith(PARTITION_PREFIX)}
    if not taken or any("rows" not in v for v in taken.values()):
        return None
    columns = taken.pop("columns")["rows"]
    if PARTITION_COLUMN not in [c[0] for c in columns]:
        return None
    try:
        return {label: (_ledger_hash(columns, v["rows"]), len(v["rows"]))
                for label, v in taken.items()}
    except (TypeError, ValueError):
        return None


def _relabel_receipts(night: dict, want: list) -> tuple[list, dict | None]:
    """The night's receipts with a retry/primary-only raw difference forgiven.

    A raw receipt is rewritten only when the captured table re-hashes to that
    receipt exactly and a relabelled copy re-hashes to an oracle receipt with
    the same row count. Anything else, including a duplicate row, is left as is.
    """
    hashes = _partition_hashes(night)
    receipts = list(night["receipts"])
    if not hashes or "stored" not in hashes:
        return receipts, None
    stored, count = hashes["stored"]
    oracle = {(r.get("canonical_hash"), r.get("row_count")) for r in want
              if r.get("target") == PARTITION_TABLE}
    if (stored, count) in oracle:
        return receipts, None
    match = next((h for label, (h, n) in hashes.items()
                  if label != "stored" and (h, n) in oracle), None)
    if match is None:
        return receipts, {"stored_hash": stored, "relabelled": False}
    out, rewritten = [], 0
    for receipt in receipts:
        if receipt.get("target") == PARTITION_TABLE and \
                receipt.get("canonical_hash") == stored and receipt.get("row_count") == count:
            receipt = {**receipt, "canonical_hash": match}
            rewritten += 1
        out.append(receipt)
    return out, {"stored_hash": stored, "relabelled": bool(rewritten),
                 "receipts_relabelled": rewritten}


def snapshot_t0(world_root: Path, specs: list[dict]) -> dict:
    db = Path(world_root) / "var/warehouse/warehouse.duckdb"
    return {s["table"]: table_rows(db, s["table"], s.get("columns")) for s in specs}


def _parse_day(value) -> date | None:
    """A calendar day, or None when the cell is empty or not a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip() if value is not None else ""
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _scheduler_day(world_root: Path) -> date | None:
    """The world's scheduler date. An unreadable clock is not a window."""
    path = Path(world_root) / "var/sched/state.json"
    try:
        raw = str(json.loads(path.read_text(encoding="utf-8"))["vnow"])
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _watermark_window(world_root: Path, spec: dict) -> tuple[date, date, str] | None:
    """``[feed_day, t0_day]`` from the spec, else the scheduler day and the day before.

    A spec that names one bound and not the other does not fall through to the
    clock. An invented half-window would accept a watermark the task did not
    declare.
    """
    named = "feed_day" in spec or "t0_day" in spec
    feed, t0 = _parse_day(spec.get("feed_day")), _parse_day(spec.get("t0_day"))
    if named:
        if feed is not None and t0 is not None and feed <= t0:
            return feed, t0, "spec"
        return None
    t0 = _scheduler_day(world_root)
    if t0 is None:
        return None
    return t0 - timedelta(days=1), t0, "scheduler"


def _through_day_in_window(got: dict, want: dict, window) -> date | None:
    """The agent watermark when the single checkpoint row differs only there."""
    if window is None or got["columns"] != want.get("columns"):
        return None
    if "through_day" not in got["columns"]:
        return None
    if len(got["rows"]) != 1 or len(want.get("rows") or []) != 1:
        return None
    index = got["columns"].index("through_day")
    agent_rest = [cell for i, cell in enumerate(got["rows"][0]) if i != index]
    oracle_rest = [cell for i, cell in enumerate(want["rows"][0]) if i != index]
    if agent_rest != oracle_rest:
        return None
    actual = _parse_day(got["rows"][0][index])
    if actual is None:
        return None
    feed, t0, _source = window
    if feed <= actual <= t0:
        return actual
    return None


PROBE_DAYS_AHEAD = 5


def _load_job(world_root: Path) -> dict | None:
    """The deployed ``factory_load`` script job, or None if the world has none."""
    try:
        live = json.loads((Path(world_root) / "var/world/live.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for name, job in (live.get("jobs") or {}).items():
        spec = job.get("exec") or {}
        if (name == "factory_load" or job.get("command") == "factory_load") \
                and spec.get("kind") == "script" and spec.get("path"):
            return spec
    return None


def _checkpoint_probe(world_root: Path, accepted: date) -> dict:
    """Run the deployed loader with the clock moved ahead and no new files.

    An in-range watermark is only an artefact of an old run when the loader no
    longer advances it on the calendar. S01's defect is exactly that advance,
    and no replayed night exposes it, so the loader is asked directly.
    """
    job = _load_job(world_root)
    if job is None:
        return {"ran": False, "advances_on_calendar": False,
                "why": "no factory_load script job in var/world/live.json"}
    with tempfile.TemporaryDirectory(prefix="c0_probe_") as tmp:
        root = Path(tmp) / "world"
        shutil.copytree(world_root, root, symlinks=True)
        state_path = root / "var/sched/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        ahead = datetime.combine(accepted + timedelta(days=PROBE_DAYS_AHEAD),
                                 datetime.min.time(), tzinfo=timezone.utc)
        state["vnow"] = ahead.strftime("%Y-%m-%dT%H:%M:%SZ")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        script = (root / "workspace" / job["path"]).resolve()
        argv = [sys.executable, str(script)] + [
            str(a).format(root=root, workspace=root / "workspace",
                          db=root / "var/warehouse/warehouse.duckdb")
            for a in job.get("args") or []]
        run = subprocess.run(argv, env={**os.environ, "PYTHONUTF8": "1"}, cwd=root,
                             stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=120)
        if run.returncode:
            return {"ran": True, "advances_on_calendar": None,
                    "why": f"loader exited {run.returncode}: {(run.stderr or '')[-300:]}"}
        snap = table_rows(root / "var/warehouse/warehouse.duckdb", CHECKPOINT_TABLE,
                          ["through_day"])
        days = [d for d in (_parse_day(row[0]) for row in snap["rows"]) if d]
        after = max(days) if days else None
    landed = [d for d in (_parse_day(p.name[3:]) for p in
                          (Path(world_root) / "workspace").rglob("dt=*") if p.is_dir()) if d]
    ceiling = max([accepted, *landed])
    advanced = after is not None and after > ceiling
    return {"ran": True, "advances_on_calendar": advanced,
            "probe_vnow": state["vnow"], "newest_landed_day": ceiling.isoformat(),
            "through_day_after": after.isoformat() if after else None}


def _checkpoint_tolerance(world_root: Path, spec: dict, tables: list[dict],
                          per_issues: dict[str, list[str]], snaps: dict[str, dict]) -> dict | None:
    """Accept an in-range checkpoint date only when every other table matches.

    ``no_op`` and a partial repair leave raw, fact, or handoff different, so
    they still fail. A close watermark does not excuse a wrong fact table.
    """
    names = {desc["table"] for desc in tables}
    if CHECKPOINT_TABLE not in snaps or not set(DATA_TABLES).issubset(names):
        return None
    if any(issues for table, issues in per_issues.items() if table != CHECKPOINT_TABLE):
        return None
    snap = snaps[CHECKPOINT_TABLE]
    if not snap["row_mismatch"] or snap["got"] is None:
        return None
    window = _watermark_window(world_root, spec)
    actual = _through_day_in_window(snap["got"], snap["want"], window)
    if actual is None or window is None:
        return None
    probe = _checkpoint_probe(world_root, actual)
    if probe["advances_on_calendar"] is not False:
        why = ("the deployed loader advances it without data"
               if probe["advances_on_calendar"] else "the loader probe did not finish")
        per_issues.setdefault(CHECKPOINT_TABLE, []).append(
            f"warehouse.factory_checkpoint watermark not excused: {why} ({probe})")
        return None
    feed, t0, source = window
    return {"through_day": actual.isoformat(), "feed_day": feed.isoformat(),
            "t0_day": t0.isoformat(), "source": source, "probe": probe}


def compare_t0(world_root: Path, grading: Path, spec: dict) -> dict:
    """Row multiset parity, layout parity and unique grain-key parity.

    When the only miss is ``through_day`` on the single-row
    ``warehouse.factory_checkpoint`` and raw, fact, and handoff match, a date
    in ``[feed_day, t0_day]`` passes with those data tables.
    """
    tables = spec.get("tables") or []
    if not tables:
        raise ValueError("spec.t0.tables is empty")
    oracle_file = (Path(grading) / spec.get("oracle", "oracle_t0.json")).resolve()
    if not oracle_file.is_relative_to(Path(grading).resolve()):
        raise ValueError("spec.t0.oracle escapes grading zone")
    expected = json.loads(oracle_file.read_text(encoding="utf-8"))
    matched, denominator = 0, 0
    per_issues: dict[str, list[str]] = {}
    snaps: dict[str, dict] = {}
    for desc in tables:
        table = desc["table"]
        want = expected.get(table)
        if want is None:
            raise ValueError(f"oracle t0 snapshot lacks {table}")
        table_issues: list[str] = []
        try:
            got = snapshot_t0(world_root, [desc])[table]
        except ValueError as exc:
            denominator += max(1, len(want.get("rows") or []))
            table_issues.append(str(exc))
            per_issues[table] = table_issues
            snaps[table] = {"got": None, "want": want, "row_mismatch": True}
            continue
        denominator += max(1, len(want.get("rows") or []), len(got["rows"]))
        if got["columns"] != want["columns"]:
            table_issues.append(
                f"{table} layout differs: {got['columns']} vs {want['columns']}")
            per_issues[table] = table_issues
            snaps[table] = {"got": got, "want": want, "row_mismatch": True}
            continue
        got, want = _normalize_partitions(table, got), _normalize_partitions(table, want)
        actual_rows = Counter(row_hash(r) for r in got["rows"])
        oracle_rows = Counter(row_hash(r) for r in want["rows"])
        matched += sum((actual_rows & oracle_rows).values())
        if not (want.get("rows") or []) and not got["rows"]:
            matched += 1
        grain = desc.get("grain_key") or []
        if grain:
            missing_cols = [c for c in grain if c not in got["columns"]]
            if missing_cols:
                table_issues.append(
                    f"{table} grain columns {missing_cols} are not in {got['columns']}")
            else:
                indices = [got["columns"].index(c) for c in grain]
                actual_keys = [row_hash([row[i] for i in indices]) for row in got["rows"]]
                oracle_keys = [row_hash([row[i] for i in indices]) for row in want["rows"]]
                if len(set(actual_keys)) != len(actual_keys):
                    table_issues.append(f"{table} has duplicate grain keys {grain}")
                if Counter(actual_keys) != Counter(oracle_keys):
                    table_issues.append(f"{table} grain-key parity failed for {grain}")
        if actual_rows != oracle_rows:
            table_issues.append(
                f"{table}: {sum((actual_rows - oracle_rows).values())} extra, "
                f"{sum((oracle_rows - actual_rows).values())} missing rows")
        per_issues[table] = table_issues
        snaps[table] = {"got": got, "want": want,
                        "row_mismatch": actual_rows != oracle_rows}
    tolerance = _checkpoint_tolerance(world_root, spec, tables, per_issues, snaps)
    issues: list[str] = []
    for table, table_issues in per_issues.items():
        if tolerance and table == CHECKPOINT_TABLE:
            if snaps[table]["row_mismatch"]:
                matched += 1
            continue
        issues.extend(table_issues)
    fraction = 0.0 if denominator <= 0 else round(
        min(1.0, max(0.0, matched / denominator)), 6)
    result = {"passed": not issues, "fraction": fraction,
              "matched_rows": matched, "oracle_rows": denominator,
              "issues": issues}
    if tolerance:
        result["checkpoint_watermark"] = tolerance
    return result


def receipt_signature(receipt: dict) -> dict:
    return {k: receipt.get(k) for k in SIGNATURE}


def _receipt_key(receipt: dict) -> str:
    return row_hash(list(receipt_signature(receipt).values()))


def _scoped_receipts(receipts: list, targets: set, match: str | None) -> list:
    chosen = [r for r in receipts if not targets or r.get("target") in targets]
    if match == "last_per_target":
        last: dict = {}
        for receipt in chosen:
            last[receipt.get("target")] = receipt
        chosen = list(last.values())
    return chosen


def compare_nights(replayed: list[dict], grading: Path, spec: dict) -> list[dict]:
    path = (Path(grading) / spec.get("oracle_receipts", "oracle_receipts.json")).resolve()
    if not path.is_relative_to(Path(grading).resolve()):
        raise ValueError("spec.replay.oracle_receipts escapes grading zone")
    data = json.loads(path.read_text(encoding="utf-8"))
    expected_nights = data["nights"] if isinstance(data, dict) else data
    if len(expected_nights) != len(replayed):
        raise ValueError(f"oracle has {len(expected_nights)} nights, replay has {len(replayed)}")
    targets = set(spec.get("targets") or [])
    match = spec.get("match")
    results = []
    for index, (night, expected) in enumerate(zip(replayed, expected_nights), 1):
        want = expected.get("receipts", expected) if isinstance(expected, dict) else expected
        if not want:
            raise ValueError(f"oracle night {index} has no publication receipts")
        want = _scoped_receipts(want, targets, match)
        if not want:
            raise ValueError(f"oracle night {index} has no receipts for targets {sorted(targets)}")
        relabelled, partition = _relabel_receipts(night, want)
        actual = _scoped_receipts(relabelled, targets, match)
        ca, cw = Counter(map(_receipt_key, actual)), Counter(map(_receipt_key, want))
        matches = sum((ca & cw).values())
        jobs_ok = bool(night["runs"]) and any(
            r.get("status") == "success" and r.get("executed") for r in night["runs"])
        denom = max(sum(cw.values()), sum(ca.values()))
        results.append({"night": index, "passed": jobs_ok and ca == cw,
                        "fraction": _fraction(matches, denom, perfect_if_empty=False),
                        "matched_receipts": matches, "oracle_receipts": sum(cw.values()),
                        "observed_receipts": sum(ca.values()), "jobs_ok": jobs_ok,
                        "missing": sum((cw-ca).values()), "extra": sum((ca-cw).values()),
                        "vnow": night.get("vnow")})
        if partition is not None:
            results[-1]["source_partition"] = partition
    return results


# Seven checkpoints. Night weights are distinct powers of two so a miss on
# R2, R3, or R4 cannot collapse to the same scalar when the other nights match.
PROGRESS_KEYS = (
    "q0_raw_loaded",
    "q1_t0_dedup_and_precedence",
    "q2_t0_handoff_parity",
    "q3_r1_parity",
    "q4_r2_parity",
    "q5_r3_parity",
    "q6_r4_parity",
)
NIGHT_PROGRESS_KEYS = PROGRESS_KEYS[3:]
SCALAR_WEIGHTS = (
    ("q0_raw_loaded", 1),
    ("q1_t0_dedup_and_precedence", 1),
    ("q2_t0_handoff_parity", 1),
    ("q3_r1_parity", 1),
    ("q4_r2_parity", 2),
    ("q5_r3_parity", 4),
    ("q6_r4_parity", 8),
)


def _clamp_fraction(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:
        return 0.0
    return round(min(1.0, max(0.0, number)), 6)


def _table_role(desc: dict) -> str:
    """raw load, grain-key dedup/precedence, or published handoff."""
    role = str(desc.get("role") or desc.get("stage") or "").lower()
    if role in {"raw", "dedup", "fact", "handoff"}:
        return "dedup" if role == "fact" else role
    name = str(desc.get("table") or "").lower()
    leaf = name.split(".")[-1]
    if name.startswith("raw.") or leaf.startswith("raw_"):
        return "raw"
    if "handoff" in leaf or leaf.endswith("_extract") or "publish" in leaf:
        return "handoff"
    return "dedup"


def _row_parity(got_rows: list, want_rows: list) -> tuple[int, int]:
    if not want_rows and not got_rows:
        return 1, 1
    actual = Counter(row_hash(row) for row in got_rows)
    oracle = Counter(row_hash(row) for row in want_rows)
    matched = sum((actual & oracle).values())
    return matched, max(sum(oracle.values()), sum(actual.values()), 1)


def _raw_loaded(got_rows: list, want_rows: list) -> tuple[int, int]:
    """Expected raw rows present. A missing table is a crash and scores 0."""
    if not want_rows and not got_rows:
        return 1, 1
    actual = Counter(row_hash(row) for row in got_rows)
    oracle = Counter(row_hash(row) for row in want_rows)
    return sum((actual & oracle).values()), max(sum(oracle.values()), 1)


def _grain_agreement(got: dict, want: dict, grain: list[str]) -> tuple[int, int]:
    columns = got["columns"]
    if any(column not in columns for column in grain):
        return 0, 1
    index = [columns.index(column) for column in grain]

    def groups(rows: list) -> dict:
        buckets: dict = {}
        for row in rows:
            key = tuple(row[i] for i in index)
            buckets.setdefault(key, []).append(row_hash(row))
        return {key: Counter(hashes) for key, hashes in buckets.items()}

    oracle_groups = groups(want.get("rows") or [])
    actual_groups = groups(got.get("rows") or [])
    if not oracle_groups:
        return (1, 1) if not actual_groups else (0, max(len(actual_groups), 1))
    matched = sum(1 for key, counts in oracle_groups.items()
                  if actual_groups.get(key) == counts)
    return matched, len(oracle_groups)


def _table_contribution(world_root: Path, desc: dict, want: dict | None) -> tuple[str, int, int]:
    role = _table_role(desc)
    if want is None:
        raise ValueError(f"oracle t0 snapshot lacks {desc['table']}")
    oracle_rows = want.get("rows") or []
    try:
        got = snapshot_t0(world_root, [desc])[desc["table"]]
    except ValueError:
        return role, 0, max(len(oracle_rows), 1)
    if got["columns"] != want.get("columns"):
        return role, 0, max(len(oracle_rows), 1)
    grain = list(desc.get("grain_key") or [])
    if role == "dedup" and grain:
        return role, *_grain_agreement(got, want, grain)
    if role == "raw":
        return role, *_raw_loaded(got["rows"], oracle_rows)
    return role, *_row_parity(got["rows"], oracle_rows)


def _mean_parts(parts: list[tuple[int, int]]) -> float:
    if not parts:
        return 1.0
    matched = sum(item[0] for item in parts)
    denom = sum(item[1] for item in parts)
    if denom <= 0:
        return 1.0
    return _clamp_fraction(matched / denom)


def stage_progress(world_root: Path, grading: Path, t0_spec: dict) -> dict:
    """q0 raw load, q1 grain-key dedup/precedence, q2 handoff row parity."""
    tables = list((t0_spec or {}).get("tables") or [])
    buckets: dict[str, list[tuple[int, int]]] = {"raw": [], "dedup": [], "handoff": []}
    if not tables:
        return {"q0_raw_loaded": 0.0, "q1_t0_dedup_and_precedence": 0.0,
                "q2_t0_handoff_parity": 0.0}
    oracle_file = (Path(grading) / (t0_spec.get("oracle") or "oracle_t0.json")).resolve()
    if not oracle_file.is_relative_to(Path(grading).resolve()):
        raise ValueError("spec.t0.oracle escapes grading zone")
    expected = json.loads(oracle_file.read_text(encoding="utf-8"))
    for desc in tables:
        want = expected.get(desc["table"])
        role, matched, denom = _table_contribution(world_root, desc, want)
        buckets[role].append((matched, denom))
    return {
        "q0_raw_loaded": _mean_parts(buckets["raw"]),
        "q1_t0_dedup_and_precedence": _mean_parts(buckets["dedup"]),
        "q2_t0_handoff_parity": _mean_parts(buckets["handoff"]),
    }


def night_parity(night_results: list | None) -> dict:
    """Per-night receipt agreement for R1..R4. A night that was not replayed is 0."""
    rows = list(night_results or [])
    out = {}
    for index, key in enumerate(NIGHT_PROGRESS_KEYS):
        if index < len(rows):
            out[key] = _clamp_fraction((rows[index] or {}).get("fraction"))
        else:
            out[key] = 0.0
    return out


def seven_step_progress(world_root: Path, grading: Path, t0_spec: dict,
                        night_results: list | None) -> dict:
    """The seven fractions declarative_grade emits beside q0..q4."""
    progress = stage_progress(world_root, grading, t0_spec or {})
    progress.update(night_parity(night_results))
    return {key: progress[key] for key in PROGRESS_KEYS}


def progress_scalar(quantities: dict) -> float:
    """Weighted mean in [0, 1]. Night weights 1,2,4,8 keep R2/R3/R4 misses apart."""
    total = 0.0
    weight_sum = 0.0
    for key, weight in SCALAR_WEIGHTS:
        total += weight * _clamp_fraction((quantities or {}).get(key))
        weight_sum += weight
    return round(total / weight_sum, 6) if weight_sum else 0.0


def capture_oracle(world_root: Path, grading: Path, spec: dict) -> dict:
    """Authoring helper: cache t0 and trajectory from an oracle-repaired world."""
    grading = Path(grading)
    grading.mkdir(parents=True, exist_ok=True)
    t0 = snapshot_t0(world_root, spec["t0"]["tables"])
    t0_path = grading / spec["t0"].get("oracle", "oracle_t0.json")
    t0_path.write_text(json.dumps(t0, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    replayed = advance_nights(world_root, grading, spec["replay"]["nights"],
                              prepare=spec["replay"].get("prepare"))
    trajectory = {"nights": [[receipt_signature(r) for r in row["receipts"]]
                             for row in replayed]}
    receipts_path = grading / spec["replay"].get("oracle_receipts", "oracle_receipts.json")
    receipts_path.write_text(json.dumps(trajectory, sort_keys=True, indent=2) + "\n",
                             encoding="utf-8")
    out = {"t0": str(t0_path), "receipts": str(receipts_path), "nights": len(replayed)}
    try:
        from . import progress_checkpoints
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.progress_checkpoints":
            out["progress_error"] = f"import: {type(exc).__name__}: {exc}"
        return out
    except Exception as exc:                                       # noqa: BLE001
        out["progress_error"] = f"import: {type(exc).__name__}: {exc}"
        return out
    if hasattr(progress_checkpoints, "capture_oracle_rows"):
        try:
            rows = progress_checkpoints.capture_oracle_rows(world_root, grading, spec)
            out["progress"] = str(rows) if isinstance(rows, Path) else rows
        except Exception as exc:                                   # noqa: BLE001
            out["progress_error"] = f"capture_oracle_rows: {type(exc).__name__}: {exc}"
    return out
