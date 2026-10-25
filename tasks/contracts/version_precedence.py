#!/usr/bin/env python3
"""VERSION PRECEDENCE: the target holds each key at its highest delivered
version, and a replay of an older version never wins."""
from __future__ import annotations

import csv
import shutil
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import _world as W
from ._types import Clause, ContractNotEvaluable, Context, not_evaluable

NAME = "version_precedence"

DOC = """INVARIANT   the target holds each key at its highest delivered version; a
            delete at the highest version removes it; a replay of a superseded
            version changes nothing.
STRESSED BY arrival order that differs from version order: a v3 landing before
            its v2, a byte-identical replay, a delete then a stale redelivery.
SHORTCUT    the merge on arrival order (an older version over a newer one) and
            the full reload that resurrects a deleted key.  Both hold on shipped
            history, so runner "advance" re-judges a COPY after every night.
ORACLE      fold the ledger in pure Python - GT008's make_fixture_world.fold.
            EXECUTED on GT008 (= C1..C4) and GT009's recorded feeds.
"""


def _fold(events: list, key_f: str, ver_f: str, type_f: str, delete_value: str,
          identity_fields: list) -> dict:
    """Source contract: the current state of a key is its highest-version event;
    a delete at the highest version removes it; a re-delivered event is
    byte-identical to the original and changes nothing."""
    best: dict = {}
    for ev in events:
        k, version = str(ev[key_f]), _version(ev.get(ver_f))
        held = best.get(k)
        if held is None or version > _version(held.get(ver_f)):
            best[k] = ev
        elif version == _version(held.get(ver_f)):
            fields = identity_fields or sorted(set(ev) & set(held))
            if any(str(ev.get(f)) != str(held.get(f)) for f in fields):
                raise ContractNotEvaluable(
                    f"the ledger violates its own source contract: {k} "
                    f"v{version} was delivered twice with different content, so "
                    f"there is no single correct answer to compare against")
    return {k: ev for k, ev in best.items()
            if str(ev.get(type_f, "")) != delete_value}


def _version(value) -> int:
    """Legacy source rows without a version are version 1 by contract."""
    return 1 if value is None or str(value).strip() == "" else int(value)


def _read_feed(feed_root: Path, glob: str = "*.csv", required=()) -> list:
    """Every event under dt=*/<glob>, partitions in date order, rows in file
    order - the ARRIVAL order, which is deliberately not the version order.

    A file whose header lacks a `required` field is not an event file and is
    skipped: GT009's noisy-surface arm drops SFTP `_delivery_manifest.csv`
    sidecars into the partitions, and a contract that folded them as events
    crashed on the first real feed it read.  Columns are matched by NAME, so a
    recovery export with a permuted header folds the same.  `_*` / `._*` files
    (sidecars, macOS resource forks) are never events; a BOM is not a header."""
    events = []
    parts = sorted(p for p in Path(feed_root).glob("dt=*") if p.is_dir())
    if not parts:
        raise ContractNotEvaluable(f"no dt=* partitions under {feed_root}")
    for part in parts:
        for payload in sorted(part.glob(glob)):
            if payload.name.startswith(("_", "._")):
                continue
            with open(payload, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                if any(f not in (reader.fieldnames or []) for f in required):
                    continue
                events.extend(dict(row) for row in reader)
    return events


def _same(a, b) -> bool:
    """Equality that survives the trip through duckdb: numbers compared as
    Decimals, everything else as text."""
    if a is None or b is None:
        return a is None and b is None
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, ValueError, ArithmeticError):
        return str(a) == str(b)


def evaluate(ctx: Context) -> list:
    try:
        return _evaluate(ctx)
    except ContractNotEvaluable as exc:
        return not_evaluable(f"{NAME}.not_evaluable", str(exc))


def _evaluate(ctx: Context) -> list:
    spec = _spec(ctx)
    runner = ctx.param("runner", "end_state")
    if runner == "advance":
        if ctx.param("nights") is None:
            return _evaluate_advanced_once(ctx)
        return _evaluate_advanced(ctx, spec)
    if runner not in ("end_state", "snapshot", "pipeline"):
        raise ContractNotEvaluable(
            f"runner={runner!r}; version_precedence knows 'end_state' (alias "
            f"'snapshot': the world as graded) and 'advance' (the world's clock "
            f"moved on a copy - over params.nights, or once by "
            f"params.advance_hours)")
    events = spec["events"]
    if events is None:
        events = _read_feed(_feed_path(ctx.world_root, spec["feed"]),
                            spec["feed_glob"], (spec["key_f"], spec["ver_f"]))
    found = _judge(ctx, _db_path(ctx, ctx.world_root), events, spec)
    if found.get("absent"):
        return [Clause.make(f"{NAME}.highest_version", False,
                            f"{spec['table']} does not exist in the graded world",
                            weight=1.0, fatal=True)]
    return _clauses([(None, found)], spec)


def _spec(ctx: Context) -> dict:
    spec = {
        "table": ctx.param("target", required=True),
        "key_col": ctx.param("key_column", required=True),
        "ver_col": ctx.param("version_column", required=True),
        "type_f": ctx.param("type_field", "event_type"),
        "delete_value": ctx.param("delete_value", "delete"),
        "identity": ctx.param("identity_fields") or [],
        "compare": ctx.param("compare") or {},      # {target_column: event_field}
        "events": ctx.param("events"),
        "feed": ctx.param("feed_dir"),
        "feed_glob": ctx.param("feed_glob", "*.csv"),
    }
    spec["key_f"] = ctx.param("key_field", spec["key_col"])
    spec["ver_f"] = ctx.param("version_field", spec["ver_col"])
    if spec["events"] is None and not spec["feed"]:
        raise ContractNotEvaluable(
            "neither params.events nor params.feed_dir is set, so there is no "
            "ledger to fold and no oracle to compare the target against")
    return spec


def _feed_path(root: Path, feed) -> Path:
    feed_path = Path(feed)
    return feed_path if feed_path.is_absolute() else Path(root) / feed


def _db_path(ctx: Context, root: Path) -> Path:
    """params.database (world-relative) names a warehouse other than the
    primary - GT008's legacy.duckdb, whose loader is the shortcut this contract
    names.  Absent, the Context's own warehouse, re-rooted onto `root`."""
    db = ctx.param("database")
    if db:
        return Path(db) if Path(db).is_absolute() else Path(root) / db
    if Path(root) == Path(ctx.world_root):
        return Path(ctx.warehouse)
    try:
        return Path(root) / Path(ctx.warehouse).relative_to(ctx.world_root)
    except ValueError:
        return W.warehouse_path(root)


def _judge(ctx: Context, db: Path, events: list, spec: dict) -> dict:
    """Fold the ledger, read the target, and say what differs.  One call per
    checkpoint: the end state, or each advanced night."""
    if not events:
        raise ContractNotEvaluable("the ledger is empty")
    key_f, ver_f, key_col, ver_col = (spec["key_f"], spec["ver_f"],
                                      spec["key_col"], spec["ver_col"])
    for f in (key_f, ver_f):
        if f not in events[0]:
            raise ContractNotEvaluable(f"ledger events carry no field {f!r}; "
                                       f"they carry {sorted(events[0])}")
    current = _fold(events, key_f, ver_f, spec["type_f"], spec["delete_value"],
                    spec["identity"])
    deleted = {str(ev[key_f]) for ev in events} - set(current)
    compare = spec["compare"]
    table = spec["table"]

    names = list(dict.fromkeys([key_col, ver_col] + sorted(compare)))
    select = ", ".join(f'"{c}"' for c in names)
    con = ctx.connect(db, read_only=True)
    try:
        if not W.table_exists(con, table):
            return {"absent": True, "events": len(events)}
        rows = con.execute(f"SELECT {select} FROM {W.q(table)}").fetchall()
    finally:
        con.close()

    held: dict = {}
    dup = []
    for row in rows:
        r = dict(zip(names, row))
        k = str(r[key_col])
        if k in held:
            dup.append(k)
        held[k] = r

    older, missing, wrong = [], [], []
    for k, ev in current.items():
        want = _version(ev.get(ver_f))
        if k not in held:
            missing.append(k)
            continue
        got = _version(held[k][ver_col])
        if got < want:
            older.append(f"{k}: target v{got}, ledger v{want}")
        elif got > want:
            wrong.append(f"{k}: target v{got} was never delivered (ledger v{want})")
    content = []
    for k, ev in current.items():
        if k not in held:
            continue
        for tcol, efield in sorted(compare.items()):
            if not _same(held[k].get(tcol), ev.get(efield)):
                content.append(f"{k}.{tcol}: {held[k].get(tcol)!r} != "
                               f"{ev.get(efield)!r}")
    return {"absent": False, "events": len(events), "current": len(current),
            "deleted": len(deleted), "held": len(held), "older": older,
            "wrong": wrong, "missing": missing,
            "resurrected": sorted(deleted & set(held)), "dup": dup,
            "content": content}


def _evaluate_advanced_once(ctx: Context) -> list:
    """`runner: "advance"` with no params.nights: plant params.plant into a
    copy, advance the world's scheduler once by params.advance_hours (a real
    job must execute), and judge the copy's end state."""
    with tempfile.TemporaryDirectory(prefix="contract-version-") as tmp:
        root = W.copy_world(ctx.world_root, Path(tmp) / "world")
        plants = ctx.param("plant") or []
        for item in plants if isinstance(plants, list) else [plants]:
            W.plant(root, item)
        W.run_advance(root, float(ctx.param("advance_hours", 24)),
                      advance=ctx.param("advance"))
        params = dict(ctx.params)
        params["runner"] = "end_state"
        copied = Context(world_root=root, params=params, before=ctx.before,
                         receipts=ctx.receipts)
        try:
            return _evaluate(copied)
        finally:
            copied.close()


def _evaluate_advanced(ctx: Context, spec: dict) -> list:
    """`runner: "advance"` - GT008's shape.  The fold is re-taken over the
    feed AS IT STANDS after each night and compared with the target after
    that night, on a copy of the world the world's own scheduler advanced.  A
    good final night cannot launder a bad intermediate one: every clause fails
    if it failed on ANY night, and its checkpoint is the first night it did.
    """
    if not spec["feed"]:
        raise ContractNotEvaluable(
            "runner=advance reads the ledger from params.feed_dir after every "
            "night; inline params.events cannot grow as the world moves")
    tmp = Path(tempfile.mkdtemp(prefix="contract-version-precedence-"))
    try:
        root = W.copy_world(ctx.world_root, tmp / "world")
        checkpoints = []

        def on_night(k, label, root_now):
            found = _judge(ctx, _db_path(ctx, root_now),
                           _read_feed(_feed_path(root_now, spec["feed"]),
                                      spec["feed_glob"],
                                      (spec["key_f"], spec["ver_f"])), spec)
            checkpoints.append((label, found))
            return None

        log = W.run_nights(ctx, root, on_night)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not checkpoints:
        return [Clause.make(f"{NAME}.every_night_reached", False,
                            f"no night was reached: {log['error']}",
                            weight=1.0, fatal=True, checkpoint="night 1")]
    clauses = _clauses(checkpoints, spec)
    clauses.append(Clause.make(
        f"{NAME}.every_night_reached", log["reached"] == log["declared"],
        (f"all {log['declared']} planted night(s) advanced"
         if log["reached"] == log["declared"] else
         f"{log['reached']} of {log['declared']} night(s) advanced: {log['error']}"),
        weight=1.0, fatal=True,
        checkpoint=None if log["reached"] == log["declared"]
        else f"night {log['reached'] + 1}"))
    return clauses


def _clauses(checkpoints: list, spec: dict) -> list:
    """Fold per-checkpoint findings into the contract's clauses.

    End state is the one-checkpoint case (label None) and produces exactly the
    clause list and details this module always produced."""
    table, compare = spec["table"], spec["compare"]

    def first(pred):
        for label, found in checkpoints:
            if pred(found):
                return label, found
        return None, None

    def at(label):
        return f"{label}: " if label else ""

    last = checkpoints[-1][1]
    many = len(checkpoints) > 1
    nights = f" on all {len(checkpoints)} night(s)" if many else ""

    lab, f = first(lambda x: x["absent"] or x["older"] or x["wrong"])
    if f is None:
        hv = Clause.make(
            f"{NAME}.highest_version", True,
            f"every one of {last['current']} current key(s) is held at the highest "
            f"version the ledger delivered{nights}", weight=0.40, fatal=True)
    elif f["absent"]:
        hv = Clause.make(f"{NAME}.highest_version", False,
                         f"{at(lab)}{table} does not exist", weight=0.40,
                         fatal=True, checkpoint=lab)
    else:
        hv = Clause.make(
            f"{NAME}.highest_version", False,
            at(lab)
            + (f"{len(f['older'])} key(s) held at an OLDER version than the ledger's - "
               f"a replay overwrote a correction: {f['older'][:3]}. " if f["older"] else "")
            + (f"{len(f['wrong'])} key(s) at a version never delivered: {f['wrong'][:3]}"
               if f["wrong"] else ""),
            weight=0.40, fatal=True, checkpoint=lab)
    clauses = [hv]

    lab, f = first(lambda x: not x["absent"] and x["resurrected"])
    clauses.append(Clause.make(
        f"{NAME}.no_resurrection", f is None,
        (f"none of the {last.get('deleted', 0)} deleted key(s) is present in "
         f"{table}{nights}" if f is None else
         f"{at(lab)}{len(f['resurrected'])} deleted key(s) are back in {table}: "
         f"{f['resurrected'][:3]}"),
        weight=0.25, checkpoint=lab))
    lab, f = first(lambda x: not x["absent"] and x["dup"])
    clauses.append(Clause.make(
        f"{NAME}.one_row_per_key", f is None,
        (f"{table} holds one row per key ({last.get('held', 0)} key(s)){nights}"
         if f is None else
         f"{at(lab)}{len(f['dup'])} key(s) appear more than once in {table}: "
         f"{f['dup'][:3]}"),
        weight=0.15, checkpoint=lab))
    lab, f = first(lambda x: not x["absent"] and x["missing"])
    clauses.append(Clause.make(
        f"{NAME}.no_missing_keys", f is None,
        ("every current key is present" + nights if f is None else
         f"{at(lab)}{len(f['missing'])} current key(s) are missing from {table}: "
         f"{f['missing'][:3]}"),
        weight=0.20, checkpoint=lab))
    if compare:
        lab, f = first(lambda x: not x["absent"] and x["content"])
        clauses.append(Clause.make(
            f"{NAME}.content_matches", f is None,
            (f"{last.get('current', 0)} key(s) match the fold on "
             f"{sorted(compare)}{nights}" if f is None else
             f"{at(lab)}{len(f['content'])} column value(s) differ from the fold: "
             f"{f['content'][:3]}"),
            weight=0.20, checkpoint=lab))
    return clauses
