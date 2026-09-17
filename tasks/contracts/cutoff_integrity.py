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
PURPOSE     comes from the TASK, never from a receipt's `kind` - see PURPOSES.
"""

EMPTY_DETAIL = "no receipts: contract not evaluable"

# WHAT A RECEIPT IS FOR, and why the task has to say.
#
# This module used to split receipts by `kind`, reading it as the publication's
# PURPOSE.  `world/README.md#publication-ledger` freezes `kind` to "table" | "file" - the
# STORAGE SHAPE.  GT009, the first task in the repository to write real
# receipts, is where the two met: both of its publications are `kind: table`,
# one is the settlement the clearing bank reads and the other the platform's
# own window record, and no reading of `kind` can tell them apart.
#
# So purpose is DECLARED, by `params.purpose_of` (a callable, for a task whose
# purposes share a target and are told apart by what the receipt carries) or by
# the three target lists the default classifier reads.  A receipt this contract
# is given no purpose for is NOT GOVERNED and is skipped - never guessed at.
#
#   publish     an ordinary publication of the period's contents.
#   correction  the declared correction path: the only way a closed period may
#               change.
#   breach      a publication recording that the consumer was handed NOTHING.
#               It never establishes a held value and never counts as a change,
#               so a period whose window closed on a breach can still be
#               published late without the correction path: nothing that was
#               handed over is being restated.
PURPOSES = ("publish", "correction", "breach")


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


def _classifier(ctx: Context):
    """`receipt -> "publish" | "correction" | "breach" | None`, from the task.

    Either `params.purpose_of` (a callable) or the default classifier over
    `params.{publish,correction,breach}_targets`.  A task that declares neither
    gets ContractNotEvaluable rather than a guess: the alternative is reading
    purpose off `kind`, which is the storage shape and means nothing here.
    """
    given = ctx.param("purpose_of")
    if callable(given):
        return given
    if given is not None:
        raise ContractNotEvaluable(
            f"params.purpose_of is a {type(given).__name__}, not a callable "
            f"receipt -> purpose")
    by_target: dict[str, str] = {}
    for purpose in PURPOSES:
        for target in (ctx.param(f"{purpose}_targets") or []):
            by_target[str(target)] = purpose
    if not by_target:
        raise ContractNotEvaluable(
            "the task declares neither params.purpose_of nor "
            "params.publish_targets / correction_targets / breach_targets, so "
            "no receipt can be given a purpose; a receipt's `kind` is the "
            "storage shape ('table' | 'file') and this contract will not read "
            "a publication's purpose out of it")
    return lambda receipt: by_target.get(str(receipt.get("target")))


def _purpose(classify, receipt: dict):
    """One receipt's purpose, with the task's answer checked rather than trusted."""
    try:
        value = classify(receipt)
    except Exception as exc:                                # noqa: BLE001
        raise ContractNotEvaluable(
            f"params.purpose_of raised {type(exc).__name__} on receipt "
            f"{receipt.get('seq')}: {exc}") from exc
    if value is None or value in PURPOSES:
        return value
    raise ContractNotEvaluable(
        f"the task's purpose_of returned {value!r} for receipt "
        f"{receipt.get('seq')}; the purposes are {list(PURPOSES)} or None")


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
    corrections_allowed = bool(ctx.param("corrections_allowed", True))
    consumer_filter = ctx.param("consumer")
    classify = _classifier(ctx)

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

    considered = [(r, p) for r, p in ((r, _purpose(classify, r)) for r in rows)
                  if p is not None
                  and (consumer_filter is None
                       or str(r.get("consumer")) == str(consumer_filter))]
    if not considered:
        clauses.append(Clause.make(
            f"{NAME}.evaluable", False,
            f"{len(rows)} receipt(s), none of them given a purpose by the task"
            + (f" for consumer {consumer_filter!r}" if consumer_filter else "")
            + ": nothing this contract governs was ever published",
            weight=1.0))
        return clauses

    groups = defaultdict(list)
    for r, purpose in considered:
        groups[(str(r.get("consumer")), str(r.get("target")),
                str(r.get("day")))].append((r, purpose))

    per_day, corrections = [], []
    weight = 1.0 / len(groups)
    for (consumer, target, day), items in sorted(groups.items()):
        cutoff = _cutoff_for(day, cutoffs, rule)
        cid = f"{NAME}.day:{consumer}:{target}:{day}"
        last_vnow = str(items[-1][0].get("vnow"))
        if cutoff is None:
            per_day.append(Clause.make(
                cid, False,
                f"no cutoff is declared for {day} (params.cutoffs or "
                f"params.cutoff_rule.offset_hours), so whether this publication "
                f"was closed cannot be decided",
                weight=weight, checkpoint=last_vnow))
            continue
        published = [(r, p) for r, p in items if p != "breach"]
        if not published:
            # Every receipt for this period says the consumer was handed
            # nothing.  A cutoff that closes over an empty period closes over
            # nothing, and there is no restatement to commit.
            per_day.append(Clause.make(
                cid, True,
                f"{len(items)} receipt(s) for {day}, every one a breach: the "
                f"consumer was handed nothing before the {cutoff.isoformat()} "
                f"cutoff, so nothing it holds can have changed since",
                weight=weight, checkpoint=last_vnow))
            continue
        offence, decided_at, held = None, last_vnow, None
        for r, purpose in published:
            if held is None:
                # Nothing was held for this period yet - because this is its
                # first publication, or because everything before it was a
                # breach.  A first publication cannot restate anything, however
                # late it is.
                held = r.get("canonical_hash")
                continue
            vnow = _parse(r["vnow"])
            closed = vnow >= cutoff
            changed = r.get("canonical_hash") != held
            if changed and closed:
                if purpose != "correction":
                    offence = (f"receipt {r.get('seq')} at {r.get('vnow')} "
                               f"republished {day} with a different hash after "
                               f"its {cutoff.isoformat()} cutoff, as a "
                               f"{purpose!r} - not the declared correction path")
                elif not corrections_allowed:
                    offence = (f"receipt {r.get('seq')} corrected {day} after its "
                               f"cutoff, and params.corrections_allowed is false")
                else:
                    corrections.append((r, day, True))
            elif purpose == "correction":
                # A correction that corrects nothing, or corrects a period that
                # was still open, is the correction path used for ordinary
                # publishing.
                corrections.append((r, day, False))
            if offence:
                decided_at = str(r.get("vnow"))
                break
            held = r.get("canonical_hash")
        per_day.append(Clause.make(
            cid, offence is None,
            (f"{len(items)} receipt(s) for {day}; nothing changed after the "
             f"{cutoff.isoformat()} cutoff outside the declared correction path"
             if offence is None else offence),
            weight=weight, checkpoint=decided_at))
    clauses.extend(per_day)

    mislabelled = [r for r, day, ok in corrections if not ok]
    n_corr = len(corrections)
    clauses.append(Clause.make(
        f"{NAME}.corrections_declared", not mislabelled,
        (f"{n_corr} correction receipt(s), each against a closed period it "
         f"actually changed" if not mislabelled else
         f"{len(mislabelled)} receipt(s) the task calls corrections correct "
         f"nothing, or correct a period that was still open: "
         f"{[r.get('seq') for r in mislabelled][:4]} - a correction path that is "
         f"used for ordinary publishing stops being a control"),
        weight=1.0))
    return clauses
