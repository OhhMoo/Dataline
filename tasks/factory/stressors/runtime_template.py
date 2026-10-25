"""Render the three feed modules as one concrete path each.

Shipped workspace files contain the active defect and nothing else. The repaired
modules are written only under ``solution/``. There is no policy map and no
second branch inside a rendered file.

The composer asks for ``ingest_feed.py``, ``fold_ledger.py``, and
``publish_handoff.py``.
"""
from __future__ import annotations

from dataclasses import dataclass


MODULES = ("ingest_feed.py", "fold_ledger.py", "publish_handoff.py")

FIELDS = (
    "event_id", "delivered_at", "business_day", "business_id",
    "version", "event_type", "entity_id", "amount_cents",
)


@dataclass(frozen=True)
class Behavior:
    """Choices baked into one rendering. Not written into the rendered file."""

    naive_glob: bool
    positional: bool
    strict_header: bool
    strict_width: bool
    utf8_sig: bool
    empty_file_error: bool
    timezone_utc: bool
    null_version_default: bool
    incremental: bool
    calendar_advance: bool
    dedup_events: bool
    precedence: str
    fold_all: bool
    keep_void_as_zero: bool
    drop_null_entity: bool
    fanout: bool
    legacy_rate: bool
    unconditional_rewrite: bool
    incremental_fold_only: bool
    boundary_on_dirty_only: bool
    strict_approval_authority: bool = True

    def __post_init__(self) -> None:
        if self.precedence not in {"version", "delivered"}:
            raise ValueError(f"unknown precedence {self.precedence!r}")
        if self.unconditional_rewrite is False and self.boundary_on_dirty_only:
            raise ValueError("dirty-only rewrite requires unconditional rewrite")


@dataclass(frozen=True)
class PhysicalTables:
    raw: str
    fact: str
    handoff: str
    close: str
    corrections: str
    checkpoint: str
    feed_rel: str


def oracle_behavior() -> Behavior:
    return Behavior(
        naive_glob=False, positional=False, strict_header=False, strict_width=False,
        utf8_sig=True, empty_file_error=False, timezone_utc=True,
        null_version_default=True, incremental=False, calendar_advance=False,
        dedup_events=True, precedence="version", fold_all=False,
        keep_void_as_zero=False, drop_null_entity=False, fanout=False,
        legacy_rate=False, unconditional_rewrite=False,
        incremental_fold_only=False, boundary_on_dirty_only=False,
    )


def shipped_behavior(stressors: list[str] | tuple[str, ...],
                     hazards: list[str] | tuple[str, ...]) -> Behavior:
    """Broken path for the active stressors and the active hazard readers."""
    stress = set(stressors)
    hazard = set(hazards)
    positional = "H02" in hazard
    return Behavior(
        naive_glob=bool(hazard & {"H01", "H04", "H06"}),
        positional=positional,
        strict_header="H03" in hazard and not positional,
        strict_width="H03" in hazard and positional,
        utf8_sig="H05" not in hazard,
        empty_file_error="H06" in hazard,
        timezone_utc="H11" not in hazard,
        null_version_default="H14" not in hazard,
        incremental=bool(stress & {"S01", "S02"} or hazard & {"H07", "H12"}),
        calendar_advance="S01" in stress,
        dedup_events=not bool(stress & {"S03"} or hazard & {"H09", "H12"}),
        precedence="delivered" if ("S04" in stress or "H08" in hazard) else "version",
        fold_all="S03" in stress,
        keep_void_as_zero="S05" in stress,
        drop_null_entity="S07" in stress,
        fanout="S06" in stress,
        legacy_rate="S08" in stress,
        unconditional_rewrite="S10" in stress,
        incremental_fold_only=False,
        boundary_on_dirty_only=False,
        strict_approval_authority=not bool(hazard & {"H08", "H09", "H10", "H11", "H12", "H13", "H14"}),
    )


def partial_behavior() -> Behavior:
    """t0 already repaired; future nights still use the weak reader and rules."""
    return Behavior(
        naive_glob=True, positional=False, strict_header=True, strict_width=False,
        utf8_sig=False, empty_file_error=True, timezone_utc=False,
        null_version_default=False, incremental=True, calendar_advance=False,
        dedup_events=False, precedence="delivered", fold_all=False,
        keep_void_as_zero=False, drop_null_entity=False, fanout=False,
        legacy_rate=False, unconditional_rewrite=True,
        incremental_fold_only=True, boundary_on_dirty_only=True,
    )


def rival_behavior(name: str) -> Behavior:
    base = partial_behavior()
    if name == "wrong_precedence":
        return Behavior(
            naive_glob=True, positional=False, strict_header=True, strict_width=False,
            utf8_sig=False, empty_file_error=True, timezone_utc=False,
            null_version_default=False, incremental=True, calendar_advance=False,
            dedup_events=True, precedence="delivered", fold_all=False,
            keep_void_as_zero=False, drop_null_entity=False, fanout=False,
            legacy_rate=False, unconditional_rewrite=False,
            incremental_fold_only=False, boundary_on_dirty_only=False,
        )
    if name == "duplicate_counted":
        return Behavior(
            naive_glob=True, positional=False, strict_header=True, strict_width=False,
            utf8_sig=False, empty_file_error=True, timezone_utc=False,
            null_version_default=False, incremental=True, calendar_advance=False,
            dedup_events=False, precedence="version", fold_all=False,
            keep_void_as_zero=False, drop_null_entity=False, fanout=False,
            legacy_rate=False, unconditional_rewrite=False,
            incremental_fold_only=False, boundary_on_dirty_only=False,
        )
    if name == "boundary_violated":
        return Behavior(
            naive_glob=True, positional=False, strict_header=True, strict_width=False,
            utf8_sig=False, empty_file_error=True, timezone_utc=False,
            null_version_default=False, incremental=True, calendar_advance=False,
            dedup_events=True, precedence="version", fold_all=False,
            keep_void_as_zero=False, drop_null_entity=False, fanout=False,
            legacy_rate=False, unconditional_rewrite=True,
            incremental_fold_only=False, boundary_on_dirty_only=True,
        )
    raise ValueError(f"unknown rival: {name}")


def render_modules(behavior: Behavior, tables: PhysicalTables, *,
                   strict_approval_authority: bool = True) -> dict[str, str]:
    """Return the three module sources for one behavior."""
    rendered = {
        "ingest_feed.py": _ingest(behavior, tables),
        "fold_ledger.py": _fold(behavior, tables),
        "publish_handoff.py": _publish(
            behavior, tables,
            strict_approval_authority=(
                strict_approval_authority and behavior.strict_approval_authority)),
    }
    for name, source in rendered.items():
        compile(source, name, "exec")
        if "POLICY" in source or "if POLICY[" in source:
            raise ValueError(f"{name} still contains a policy switch")
    return rendered


def _ingest(behavior: Behavior, tables: PhysicalTables) -> str:
    encoding = "utf-8-sig" if behavior.utf8_sig else "utf-8"
    if behavior.naive_glob:
        scan = '''
        for path in sorted(folder.iterdir(), key=lambda p: ("retry" in p.name, p.name)):
            if not path.is_file():
                continue
            if path.name.endswith(".csv") or path.name.endswith(".csv.part"):
                yield day, path
'''
    else:
        scan = '''
        for path in sorted(folder.glob("events*.csv"),
                           key=lambda p: ("retry" in p.name, p.name)):
            if path.name.startswith("._"):
                continue
            yield day, path
'''
    if behavior.incremental:
        day_filter = '''
        if not (day > through or (retry and day == through)):
            continue
'''
    else:
        day_filter = ""
    if behavior.timezone_utc:
        parsed = '''
def _parsed_time(value):
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(tzinfo=None)
'''
    else:
        parsed = '''
def _parsed_time(value):
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment.replace(tzinfo=None)
'''
    if behavior.null_version_default:
        version = '''
def _version(raw):
    if raw is None or str(raw).strip() == "":
        return 1
    return int(raw)
'''
    else:
        version = '''
def _version(raw):
    return int(raw)
'''
    if behavior.positional:
        width = ""
        if behavior.strict_width:
            width = '''
            if len(header) != len(FIELDS):
                raise ValueError(f"unexpected columns in {path.name}: {header}")
'''
        reader = f'''
def _read_rows(path):
    with path.open(encoding="{encoding}", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return []
{width}        rows = []
        for values in reader:
            padded = list(values)[:len(FIELDS)]
            padded += [""] * (len(FIELDS) - len(padded))
            rows.append({{FIELDS[index]: padded[index] for index in range(len(FIELDS))}})
        return rows
'''
    else:
        check = ""
        if behavior.strict_header:
            check = '''
        if list(reader.fieldnames or []) != FIELDS:
            raise ValueError(f"unexpected columns in {path.name}: {reader.fieldnames}")
'''
        reader = f'''
def _read_rows(path):
    with path.open(encoding="{encoding}", newline="") as handle:
        reader = csv.DictReader(handle)
{check}        return list(reader)
'''
    empty = ""
    if behavior.empty_file_error:
        empty = '''
            if not records:
                raise ValueError(f"empty feed file: {path.name}")
'''
    dedup = ""
    if behavior.dedup_events:
        dedup = f'''
        stored = con.execute(
            "SELECT event_id, delivered_at, business_day, business_id, version, "
            "event_type, entity_id, amount_cents, source_partition FROM {tables.raw}"
        ).fetchall()
        unique = {{}}
        for event in sorted(stored, key=lambda item: (
                item[0], "retry" in (item[8] or ""), item[8] or "")):
            unique.setdefault(event[0], event)
        stored = list(unique.values())
        con.execute("DELETE FROM {tables.raw}")
        if stored:
            con.executemany(
                "INSERT INTO {tables.raw} (event_id, delivered_at, business_day, "
                "business_id, version, event_type, entity_id, amount_cents, source_partition) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                stored)
'''
    if behavior.incremental:
        through_expr = "days + [through]"
    else:
        through_expr = 'days + ["1970-01-01"]'
    calendar = ""
    if behavior.calendar_advance:
        # S01's corrupt watermark is already the feed day. The day<=through
        # gate then skips dt=feed_day, so the initial raw load does not
        # contain that folder. Do not pull the watermark forward to the
        # scheduler clock: that date is the next calendar day and has no
        # landing folder yet. An edit that removes the gate and the oracle
        # both stop on the feed day.
        calendar = ""
    delete_raw = ""
    if not behavior.incremental:
        delete_raw = f'        con.execute("DELETE FROM {tables.raw}")\n'
    return f'''#!/usr/bin/env python3
"""Read partner landing files into the raw feed table."""
import csv
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

FIELDS = ["event_id", "delivered_at", "business_day", "business_id",
          "version", "event_type", "entity_id", "amount_cents"]
FEED_REL = "{tables.feed_rel}"
RAW_TABLE = "{tables.raw}"
CHECKPOINT_TABLE = "{tables.checkpoint}"
CORRECTIONS_TABLE = "{tables.corrections}"


def db(root):
    con = duckdb.connect(":memory:", config={{"threads": 1}})
    path = str(root / "var/warehouse/warehouse.duckdb").replace("'", "''")
    con.execute("ATTACH '" + path + "' AS wh")
    con.execute("USE wh")
    return con


def night(root):
    path = root / "var/factory/night.txt"
    return int(path.read_text(encoding="utf-8")) if path.exists() else 0


def arrival(root):
    n = night(root) + 1
    source = root / "build/factory_future" / f"R{{n}}"
    if not source.is_dir():
        return
    feed = root / "workspace" / FEED_REL
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.name == "_approvals.csv":
            continue
        dest = feed / path.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    approvals = source / "_approvals.csv"
    if approvals.is_file():
        with db(root) as con, approvals.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                con.execute(
                    "INSERT INTO " + CORRECTIONS_TABLE +
                    " (business_day, ticket, approved_by) VALUES (?, ?, ?)",
                    [row["business_day"], row["ticket"], row["approved_by"]])
    stamp = root / "var/factory/night.txt"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(str(n), encoding="utf-8")

{parsed}{version}
def _files(root, through, *, retry=False):
    feed = root / "workspace" / FEED_REL
    if not feed.is_dir():
        return
    for folder in sorted(feed.glob("dt=*")):
        if not folder.is_dir():
            continue
        day = folder.name.split("=", 1)[1]
{day_filter}{scan}

{reader}
def _row(row, source):
    moment = _parsed_time(row["delivered_at"])
    business_day = row.get("business_day") or ""
    if not str(business_day).strip():
        business_day = (moment.date() + timedelta(days=1)
                        if (moment.hour, moment.minute) >= (23, 30)
                        else moment.date()).isoformat()
    entity = row.get("entity_id") or None
    return (row["event_id"], moment, business_day, row["business_id"], _version(row.get("version")),
            row["event_type"], entity, int(row["amount_cents"]), source)


def _load_raw(root, *, retry=False):
    con = db(root)
    try:
        through = str(con.execute(
            "SELECT COALESCE(MAX(through_day), DATE '1970-01-01') FROM " + CHECKPOINT_TABLE
        ).fetchone()[0])
        files = list(_files(root, through, retry=retry))
        incoming = []
        for day, path in files:
            records = _read_rows(path)
{empty}            incoming.extend(_row(row, str(path.relative_to(root / "workspace"))) for row in records)
        con.execute("BEGIN TRANSACTION")
{delete_raw}        if incoming:
            con.executemany(
                "INSERT INTO " + RAW_TABLE + " (event_id, delivered_at, business_day, "
                "business_id, version, event_type, entity_id, amount_cents, source_partition) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                incoming)
{dedup}        days = [day for day, _path in files]
        read_through = max({through_expr})
{calendar}        con.execute("DELETE FROM " + CHECKPOINT_TABLE)
        con.execute("INSERT INTO " + CHECKPOINT_TABLE + " (through_day) VALUES (?)", [read_through])
        con.execute("COMMIT")
        state = root / "var/factory"
        state.mkdir(parents=True, exist_ok=True)
        touched = sorted({{row[3] for row in incoming}})
        dirty = sorted({{str(row[2]) for row in incoming}})
        (state / "touched.json").write_text(json.dumps(touched), encoding="utf-8")
        (state / "dirty_days.json").write_text(json.dumps(dirty), encoding="utf-8")
        print(f"ingest: {{len(files)}} files, {{len(incoming)}} rows")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise
    finally:
        con.close()


def _fold(root):
    script = Path(__file__).resolve().with_name("fold_ledger.py")
    subprocess.run([sys.executable, str(script), "fold", str(root)],
                   check=True, stdin=subprocess.DEVNULL)


def load(root, *, retry=False):
    _load_raw(root, retry=retry)
    _fold(root)


def main():
    action, root = sys.argv[1], Path(sys.argv[2])
    if action == "arrive":
        arrival(root)
    elif action == "load":
        load(root)
    elif action == "retry":
        if night(root) == 2:
            load(root, retry=True)
    else:
        raise SystemExit(f"unknown action: {{action}}")


if __name__ == "__main__":
    main()
'''


def _fold(behavior: Behavior, tables: PhysicalTables) -> str:
    if behavior.fold_all:
        select = "        selected = list(events)\n"
    elif behavior.precedence == "version":
        select = '''        latest = {}
        for event in events:
            key = event[3]
            order = (event[4], event[1], event[0])
            if key not in latest or order > latest[key][0]:
                latest[key] = (order, event)
        selected = [value[1] for value in latest.values()]
'''
    else:
        select = '''        latest = {}
        for event in events:
            key = event[3]
            order = (event[1], event[0], event[4])
            if key not in latest or order > latest[key][0]:
                latest[key] = (order, event)
        selected = [value[1] for value in latest.values()]
'''
    if behavior.incremental_fold_only:
        delete = f'''        touched = set(json.loads(touched_path.read_text(encoding="utf-8"))
                      if touched_path.is_file() else [])
        con.execute("BEGIN TRANSACTION")
        for business_id in sorted(touched):
            con.execute("DELETE FROM {tables.fact} WHERE business_id = ?", [business_id])
'''
        skip = '''            if key not in touched:
                continue
'''
    else:
        delete = f'''        con.execute("BEGIN TRANSACTION")
        con.execute("DELETE FROM {tables.fact}")
'''
        skip = ""
    if behavior.keep_void_as_zero:
        void = '''            if kind == "void":
                amount = 0
'''
    else:
        void = '''            if kind == "void":
                continue
'''
    drop = ""
    if behavior.drop_null_entity:
        drop = '''            if entity is None:
                continue
'''
    fanout = ""
    if behavior.fanout:
        fanout = '''            # Partner tier-B split adjustment (legacy feed v2 convention).
            if str(key).startswith("B-FANOUT"):
                amount *= 2
'''
    legacy = ""
    if behavior.legacy_rate:
        legacy = '''            # Partner tier-B fee retention rate (legacy feed v2 convention).
            if str(key).startswith("B-CUTOVER"):
                amount = amount * 9 // 10
'''
    return f'''#!/usr/bin/env python3
"""Fold raw partner events into the current fact table."""
import json
import sys
from pathlib import Path

import duckdb

RAW_TABLE = "{tables.raw}"
FACT_TABLE = "{tables.fact}"


def db(root):
    con = duckdb.connect(":memory:", config={{"threads": 1}})
    path = str(root / "var/warehouse/warehouse.duckdb").replace("'", "''")
    con.execute("ATTACH '" + path + "' AS wh")
    con.execute("USE wh")
    return con


def fold(root):
    con = db(root)
    touched_path = root / "var/factory/touched.json"
    try:
        events = con.execute(
            "SELECT event_id, delivered_at, business_day, business_id, version, "
            "event_type, entity_id, amount_cents, source_partition FROM " + RAW_TABLE
        ).fetchall()
{select}{delete}        rows = []
        for event in selected:
            _event_id, _delivered, day, key, version, kind, entity, amount, _source = event
{skip}{void}{drop}{fanout}{legacy}            rows.append((key, day, entity, amount, version))
        if rows:
            con.executemany(
                "INSERT INTO " + FACT_TABLE +
                " (business_id, business_day, entity_id, amount_cents, version) "
                "VALUES (?, ?, ?, ?, ?)",
                rows)
        con.execute("COMMIT")
        print(f"fold: {{len(rows)}} current rows")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise
    finally:
        con.close()


def main():
    action, root = sys.argv[1], Path(sys.argv[2])
    if action != "fold":
        raise SystemExit(f"unknown action: {{action}}")
    fold(root)


if __name__ == "__main__":
    main()
'''


def _publish(behavior: Behavior, tables: PhysicalTables, *,
             strict_approval_authority: bool) -> str:
    approval_clause = (
        " WHERE approved_by IS NOT NULL "
        "AND TRIM(approved_by) NOT IN ('', 'ops_auto_draft', 'ops_ingestor', 'pending')"
        if strict_approval_authority else ""
    )
    if behavior.unconditional_rewrite and behavior.boundary_on_dirty_only:
        freeze = '''            if day in frozen and day not in approved and day not in dirty_days:
                want = frozen[day]
                ref = None
            else:
                ref = approved.get(day)
'''
    elif behavior.unconditional_rewrite:
        freeze = '''            ref = approved.get(day)
'''
    else:
        freeze = '''            if day in frozen and day not in approved:
                want = frozen[day]
                ref = None
            else:
                ref = approved.get(day)
'''
    return f'''#!/usr/bin/env python3
"""Publish the open window and freeze closed days."""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb

FACT_TABLE = "{tables.fact}"
HANDOFF_TABLE = "{tables.handoff}"
CLOSE_TABLE = "{tables.close}"
CORRECTIONS_TABLE = "{tables.corrections}"


def db(root):
    con = duckdb.connect(":memory:", config={{"threads": 1}})
    path = str(root / "var/warehouse/warehouse.duckdb").replace("'", "''")
    con.execute("ATTACH '" + path + "' AS wh")
    con.execute("USE wh")
    return con


def night(root):
    path = root / "var/factory/night.txt"
    return int(path.read_text(encoding="utf-8")) if path.exists() else 0


def publish(root):
    con = db(root)
    try:
        dirty_path = root / "var/factory/dirty_days.json"
        dirty_days = set(json.loads(dirty_path.read_text(encoding="utf-8"))
                         if dirty_path.is_file() else [])
        desired = {{}}
        for day, entity, count, total in con.execute(
                "SELECT business_day, entity_id, COUNT(*), SUM(amount_cents) "
                "FROM " + FACT_TABLE + " GROUP BY 1, 2").fetchall():
            desired.setdefault(str(day), {{}})[entity] = (int(count), int(total))
        current = {{}}
        for day, entity, count, total in con.execute(
                "SELECT business_day, entity_id, payments, net_cents FROM " + HANDOFF_TABLE
        ).fetchall():
            current.setdefault(str(day), {{}})[entity] = (int(count), int(total))
        frozen = {{}}
        for day, entity, count, total in con.execute(
                "SELECT business_day, entity_id, payments, net_cents FROM " + CLOSE_TABLE
        ).fetchall():
            frozen.setdefault(str(day), {{}})[entity] = (int(count), int(total))
        approved = {{str(day): ticket for day, ticket in con.execute(
            "SELECT business_day, ticket FROM " + CORRECTIONS_TABLE +
            "{approval_clause}").fetchall()}}
        con.execute("BEGIN TRANSACTION")
        for day in sorted(set(desired) | set(current) | set(frozen)):
            want = desired.get(day, {{}})
            have = current.get(day, {{}})
{freeze}            if want == have:
                continue
            con.execute("DELETE FROM " + HANDOFF_TABLE + " WHERE business_day = ?", [day])
            for entity, (count, total) in sorted(want.items(), key=lambda item: str(item[0])):
                con.execute(
                    "INSERT INTO " + HANDOFF_TABLE +
                    " (business_day, entity_id, payments, net_cents, correction_ref) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [day, entity, count, total, ref])
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise
    finally:
        con.close()


def close(root):
    n = night(root)
    if not n:
        return
    start = date.fromisoformat(json.loads(
        (root / "var/factory/config.json").read_text(encoding="utf-8"))["t0_day"])
    day = start + timedelta(days=n - 1)
    with db(root) as con:
        con.execute("DELETE FROM " + CLOSE_TABLE + " WHERE business_day = ?", [day])
        con.execute(
            "INSERT INTO " + CLOSE_TABLE +
            " (business_day, entity_id, payments, net_cents) "
            "SELECT business_day, entity_id, payments, net_cents FROM " + HANDOFF_TABLE +
            " WHERE business_day = ?",
            [day])


def main():
    action, root = sys.argv[1], Path(sys.argv[2])
    if action == "publish":
        publish(root)
    elif action == "close":
        close(root)
    else:
        raise SystemExit(f"unknown action: {{action}}")


if __name__ == "__main__":
    main()
'''
