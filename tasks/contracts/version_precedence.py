#!/usr/bin/env python3
"""VERSION PRECEDENCE: the target holds each key at its highest delivered
version, and a replay of an older version never wins."""
from __future__ import annotations

import csv
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
SHORTCUT    the conventional merge on arrival order, which writes an older
            version over a newer one, and the full reload that resurrects a
            deleted key.  Both look correct on every night of shipped history.
ORACLE      fold the ledger in pure Python - GT008's make_fixture_world.fold.
"""


def _fold(events: list, key_f: str, ver_f: str, type_f: str, delete_value: str,
          identity_fields: list) -> dict:
    """Source contract: the current state of a key is its highest-version event;
    a delete at the highest version removes it; a re-delivered event is
    byte-identical to the original and changes nothing."""
    best: dict = {}
    for ev in events:
        k, version = str(ev[key_f]), int(ev[ver_f])
        held = best.get(k)
        if held is None or version > int(held[ver_f]):
            best[k] = ev
        elif version == int(held[ver_f]):
            fields = identity_fields or sorted(set(ev) & set(held))
            if any(str(ev.get(f)) != str(held.get(f)) for f in fields):
                raise ContractNotEvaluable(
                    f"the ledger violates its own source contract: {k} "
                    f"v{version} was delivered twice with different content, so "
                    f"there is no single correct answer to compare against")
    return {k: ev for k, ev in best.items()
            if str(ev.get(type_f, "")) != delete_value}


def _read_feed(feed_root: Path) -> list:
    """Every event under dt=*/*.csv, partitions in date order, rows in file
    order - the ARRIVAL order, which is deliberately not the version order."""
    events = []
    parts = sorted(p for p in Path(feed_root).glob("dt=*") if p.is_dir())
    if not parts:
        raise ContractNotEvaluable(f"no dt=* partitions under {feed_root}")
    for part in parts:
        for payload in sorted(part.glob("*.csv")):
            with open(payload, newline="", encoding="utf-8") as fh:
                events.extend(dict(row) for row in csv.DictReader(fh))
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
    table = ctx.param("target", required=True)
    key_col = ctx.param("key_column", required=True)
    ver_col = ctx.param("version_column", required=True)
    key_f = ctx.param("key_field", key_col)
    ver_f = ctx.param("version_field", ver_col)
    type_f = ctx.param("type_field", "event_type")
    delete_value = ctx.param("delete_value", "delete")
    identity = ctx.param("identity_fields") or []
    compare = ctx.param("compare") or {}       # {target_column: event_field}

    events = ctx.param("events")
    if events is None:
        feed = ctx.param("feed_dir")
        if not feed:
            raise ContractNotEvaluable(
                "neither params.events nor params.feed_dir is set, so there is no "
                "ledger to fold and no oracle to compare the target against")
        feed_path = Path(feed)
        if not feed_path.is_absolute():
            feed_path = ctx.world_root / feed
        events = _read_feed(feed_path)
    if not events:
        raise ContractNotEvaluable("the ledger is empty")
    for f in (key_f, ver_f):
        if f not in events[0]:
            raise ContractNotEvaluable(f"ledger events carry no field {f!r}; "
                                       f"they carry {sorted(events[0])}")

    current = _fold(events, key_f, ver_f, type_f, delete_value, identity)
    deleted = {str(ev[key_f]) for ev in events} - set(current)

    cols = [key_col, ver_col] + sorted(compare)
    select = ", ".join(f'"{c}"' for c in dict.fromkeys(cols))
    con = ctx.connect(read_only=True)
    try:
        if not W.table_exists(con, table):
            return [Clause.make(f"{NAME}.highest_version", False,
                                f"{table} does not exist in the graded world",
                                weight=1.0, fatal=True)]
        rows = con.execute(f"SELECT {select} FROM {W.q(table)}").fetchall()
        names = [c for c in dict.fromkeys(cols)]
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
        want = int(ev[ver_f])
        if k not in held:
            missing.append(k)
            continue
        got = int(held[k][ver_col])
        if got < want:
            older.append(f"{k}: target v{got}, ledger v{want}")
        elif got > want:
            wrong.append(f"{k}: target v{got} was never delivered (ledger v{want})")
    resurrected = sorted(deleted & set(held))

    content = []
    if compare:
        for k, ev in current.items():
            if k not in held:
                continue
            for tcol, efield in sorted(compare.items()):
                if not _same(held[k].get(tcol), ev.get(efield)):
                    content.append(f"{k}.{tcol}: {held[k].get(tcol)!r} != "
                                   f"{ev.get(efield)!r}")

    clauses = [
        Clause.make(
            f"{NAME}.highest_version", not older and not wrong,
            (f"every one of {len(current)} current key(s) is held at the highest "
             f"version the ledger delivered" if not older and not wrong else
             (f"{len(older)} key(s) held at an OLDER version than the ledger's - "
              f"a replay overwrote a correction: {older[:3]}. " if older else "")
             + (f"{len(wrong)} key(s) at a version never delivered: {wrong[:3]}"
                if wrong else "")),
            weight=0.40, fatal=True),
        Clause.make(
            f"{NAME}.no_resurrection", not resurrected,
            (f"none of the {len(deleted)} deleted key(s) is present in {table}"
             if not resurrected else
             f"{len(resurrected)} deleted key(s) are back in {table}: "
             f"{resurrected[:3]}"),
            weight=0.25),
        Clause.make(
            f"{NAME}.one_row_per_key", not dup,
            (f"{table} holds one row per key ({len(held)} key(s))" if not dup else
             f"{len(dup)} key(s) appear more than once in {table}: {dup[:3]}"),
            weight=0.15),
        Clause.make(
            f"{NAME}.no_missing_keys", not missing,
            ("every current key is present" if not missing else
             f"{len(missing)} current key(s) are missing from {table}: "
             f"{missing[:3]}"),
            weight=0.20),
    ]
    if compare:
        clauses.append(Clause.make(
            f"{NAME}.content_matches", not content,
            (f"{len(current)} key(s) match the fold on "
             f"{sorted(compare)}" if not content else
             f"{len(content)} column value(s) differ from the fold: "
             f"{content[:3]}"),
            weight=0.20))
    return clauses
