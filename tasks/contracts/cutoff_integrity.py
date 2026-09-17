#!/usr/bin/env python3
"""CUTOFF INTEGRITY: once a period is published and its cutoff has passed, what
the consumer holds for that period changes only through the declared correction
path."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from ._types import Clause, ContractNotEvaluable, Context, not_evaluable

NAME = "cutoff_integrity"

DOC = """INVARIANT   nothing published for a period changes after that period's cutoff,
            except through the declared correction path.  Evaluated per receipt.
STRESSED BY a backfill running across a signed report; a late correction
            arriving after a cutoff; a recovery that republishes history.
SHORTCUT    "recompute everything so every consumer agrees with today's best
            number" - invisible in the end state, which is why this contract
            reads the LEDGER.  With no receipts it reports that it cannot be
            evaluated rather than passing.
DESIGNED    not lifted: no task in the repo writes receipts yet.
"""

EMPTY_DETAIL = "no receipts: contract not evaluable"


def _parse(ts: str):
    t = str(ts).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        raise ContractNotEvaluable(f"unparseable timestamp {ts!r} in the ledger")


def _cutoff_for(day: str, cutoffs: dict, rule: dict):
    if day in cutoffs:
        return _parse(cutoffs[day])
    hours = (rule or {}).get("offset_hours")
    if hours is None:
        return None
    return _parse(f"{day}T00:00:00+00:00") + timedelta(hours=float(hours))


def evaluate(ctx: Context) -> list:
    try:
        return _evaluate(ctx)
    except ContractNotEvaluable as exc:
        return not_evaluable(f"{NAME}.not_evaluable", str(exc))


def _evaluate(ctx: Context) -> list:
    receipts = list(ctx.receipts or [])
    if not receipts:
        # A MISSING ledger must be visible, not silent.  Not fatal: it zeroes
        # this contract and leaves the rest of the composition standing.
        return [Clause.make(f"{NAME}.evaluable", False, EMPTY_DETAIL, weight=1.0)]

    cutoffs = ctx.param("cutoffs") or {}
    rule = ctx.param("cutoff_rule") or {}
    publish_kinds = set(ctx.param("publish_kinds") or ["publish"])
    correction_kinds = set(ctx.param("correction_kinds") or ["correction"])
    corrections_allowed = bool(ctx.param("corrections_allowed", True))
    consumer_filter = ctx.param("consumer")

    rows = sorted(receipts, key=lambda r: int(r.get("seq", 0)))

    # ---- gate: a ledger that can be rewritten is not evidence
    seqs = [int(r.get("seq", 0)) for r in rows]
    mono_seq = all(b > a for a, b in zip(seqs, seqs[1:]))
    times = [_parse(r["vnow"]) for r in rows if r.get("vnow")]
    mono_time = all(b >= a for a, b in zip(times, times[1:]))
    clauses = [Clause.make(
        f"{NAME}.ledger_monotonic", mono_seq and mono_time,
        (f"{len(rows)} receipt(s), seq strictly increasing and vnow "
         f"non-decreasing" if mono_seq and mono_time else
         ("seq is not strictly increasing" if not mono_seq else "")
         + ("; " if not mono_seq and not mono_time else "")
         + ("vnow goes backwards across the ledger" if not mono_time else "")
         + " - an append-only ledger is the only kind that is evidence"),
        weight=1.0, fatal=True)]

    considered = [r for r in rows
                  if str(r.get("kind")) in (publish_kinds | correction_kinds)
                  and (consumer_filter is None
                       or str(r.get("consumer")) == str(consumer_filter))]
    if not considered:
        clauses.append(Clause.make(
            f"{NAME}.evaluable", False,
            f"{len(rows)} receipt(s), none of kind {sorted(publish_kinds | correction_kinds)}"
            + (f" for consumer {consumer_filter!r}" if consumer_filter else "")
            + ": nothing this contract governs was ever published",
            weight=1.0))
        return clauses

    groups = defaultdict(list)
    for r in considered:
        groups[(str(r.get("consumer")), str(r.get("target")), str(r.get("day")))].append(r)

    per_day, corrections = [], []
    weight = 1.0 / len(groups)
    for (consumer, target, day), items in sorted(groups.items()):
        cutoff = _cutoff_for(day, cutoffs, rule)
        cid = f"{NAME}.day:{consumer}:{target}:{day}"
        if cutoff is None:
            per_day.append(Clause.make(
                cid, False,
                f"no cutoff is declared for {day} (params.cutoffs or "
                f"params.cutoff_rule.offset_hours), so whether this publication "
                f"was closed cannot be decided",
                weight=weight, checkpoint=str(items[-1].get("vnow"))))
            continue
        offence, decided_at = None, str(items[-1].get("vnow"))
        held = items[0].get("canonical_hash")
        for r in items[1:]:
            vnow = _parse(r["vnow"])
            closed = vnow >= cutoff
            changed = r.get("canonical_hash") != held
            kind = str(r.get("kind"))
            if changed and closed:
                if kind not in correction_kinds:
                    offence = (f"receipt {r.get('seq')} at {r.get('vnow')} "
                               f"republished {day} with a different hash after "
                               f"its {cutoff.isoformat()} cutoff, as kind "
                               f"{kind!r} - not the declared correction path")
                elif not corrections_allowed:
                    offence = (f"receipt {r.get('seq')} corrected {day} after its "
                               f"cutoff, and params.corrections_allowed is false")
                else:
                    corrections.append((r, day, True))
            elif changed and not closed:
                pass                      # the period was still open: legal
            if kind in correction_kinds and not (changed and closed):
                corrections.append((r, day, False))
            if offence:
                decided_at = str(r.get("vnow"))
                break
            held = r.get("canonical_hash")
        per_day.append(Clause.make(
            cid, offence is None,
            (f"{len(items)} receipt(s) for {day}; nothing changed after the "
             f"{cutoff.isoformat()} cutoff outside {sorted(correction_kinds)}"
             if offence is None else offence),
            weight=weight, checkpoint=decided_at))
    clauses.extend(per_day)

    mislabelled = [r for r, day, ok in corrections if not ok]
    n_corr = len(corrections)
    clauses.append(Clause.make(
        f"{NAME}.corrections_declared", not mislabelled,
        (f"{n_corr} correction receipt(s), each against a closed period it "
         f"actually changed" if not mislabelled else
         f"{len(mislabelled)} receipt(s) of kind {sorted(correction_kinds)} "
         f"correct nothing, or correct a period that was still open: "
         f"{[r.get('seq') for r in mislabelled][:4]} - a correction path that is "
         f"used for ordinary publishing stops being a control"),
        weight=1.0))
    return clauses
