"""The grading side of the publication ledger (world/LEDGER_CONTRACT.md).

The world writes `<world_root>/var/ledger/receipts.jsonl` while it advances; a
grader reads it afterwards to ask what a consumer could see, and when. This
module is the only reader the harness and the task graders use.

Two rules, and both are about where the file is read FROM.

1. **The grader reads a copy in the grading zone, not the live world.** The
   world root is the directory the episode ran in; on the host topology it is
   an ordinary directory on the same filesystem as everything else, and a
   grader that reads its evidence out of the subject's own tree is one path
   mistake away from reading evidence the subject could have edited. So the
   harness calls `stage()` after teardown and before grading, and the graders
   read `<grading>/ledger/receipts.jsonl`. `load_receipts` prefers the staged
   copy and says so when it falls back.

2. **It imports nothing from `world/`.** agent.md 10.2: a task knows the
   world's *layout* and never imports its internals. The layout constants are
   restated here on purpose - this file is the reader half of a seam, and a
   seam that is one import is not a seam.

Reading a receipt is reading evidence, so this module refuses malformed
evidence rather than working around it: unknown or missing keys, a non-integer
seq, or a seq sequence that is not 1..n contiguous all raise. An append-only
ledger with a hole in it is not an append-only ledger, and silently grading
against one would be the exact class of defect (mechanical verification that is
not quality) that this project keeps paying for.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

# The world's layout (world/world/ledger.py LEDGER), restated, not imported.
LEDGER_REL = Path("var") / "ledger" / "receipts.jsonl"
# Where stage() puts the copy inside the grading zone.
STAGED_REL = Path("ledger") / "receipts.jsonl"

KEYS = ("seq", "vnow", "day", "job", "target", "kind", "canonical_hash",
        "row_count", "sample", "consumer")


class LedgerError(Exception):
    """The ledger could not be read as evidence."""


# --------------------------------------------------------------------- stage

def stage(world_root, grading_dir) -> Path | None:
    """Copy the world's ledger into the grading zone. Returns the copy, or None.

    Called by the harness AFTER teardown and BEFORE the grader runs. None means
    the world published nothing - a world whose jobs declare no `publishes`
    never creates the file, and that is not an error.
    """
    source = Path(world_root) / LEDGER_REL
    if not source.is_file():
        return None
    target = Path(grading_dir) / STAGED_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


# ---------------------------------------------------------------------- read

def resolve(root) -> tuple[Path | None, str]:
    """(path, where) for a ledger under `root`. `where` is "staged" | "world"."""
    root = Path(root)
    if root.is_file():
        return root, "file"
    staged = root / STAGED_REL
    if staged.is_file():
        return staged, "staged"
    live = root / LEDGER_REL
    if live.is_file():
        return live, "world"
    return None, "absent"


def load_receipts(root) -> list[dict]:
    """Every receipt under `root`, in file order.

    `root` may be the grading zone (preferred: the staged copy), a world root,
    or the receipts file itself. A world that published nothing returns [].
    """
    path, _where = resolve(root)
    if path is None:
        return []
    receipts = []
    for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            receipt = json.loads(line)
        except ValueError as exc:
            raise LedgerError(f"{path}:{number}: not JSON: {exc}") from None
        if not isinstance(receipt, dict) or set(receipt) != set(KEYS):
            raise LedgerError(
                f"{path}:{number}: receipt keys {sorted(receipt)} != {sorted(KEYS)}")
        receipts.append(receipt)
    for index, receipt in enumerate(receipts, start=1):
        if receipt["seq"] != index:
            raise LedgerError(
                f"{path}: receipt {index} carries seq {receipt['seq']!r}; an "
                f"append-only ledger is contiguous from 1")
    return receipts


# ------------------------------------------------------------------- queries

def parse_vtime(value: str) -> datetime:
    return datetime.fromisoformat(
        str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def receipts_between(receipts, vnow_a, vnow_b) -> list[dict]:
    """Receipts published in the half-open span [vnow_a, vnow_b).

    Half-open so that consecutive checkpoints partition the ledger: a receipt
    belongs to exactly one span, and `receipts_between(r, c1, c2)` +
    `receipts_between(r, c2, c3)` is `receipts_between(r, c1, c3)`. Either bound
    may be None for an open end. Order is file order, which is publication
    order.
    """
    low = parse_vtime(vnow_a) if vnow_a is not None else None
    high = parse_vtime(vnow_b) if vnow_b is not None else None
    out = []
    for receipt in receipts:
        at = parse_vtime(receipt["vnow"])
        if (low is None or at >= low) and (high is None or at < high):
            out.append(receipt)
    return out


def last_receipt(receipts, target) -> dict | None:
    """The most recent receipt for `target`, or None.

    "Most recent" is the highest seq, not the highest vnow: seq is the order the
    world actually published in, and two publications can share a vnow when one
    job publishes several targets at once.
    """
    found = None
    for receipt in receipts:
        if receipt["target"] == target and (
                found is None or receipt["seq"] > found["seq"]):
            found = receipt
    return found


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(
        prog="tasks.harness.ledger",
        description="read a publication ledger (grading zone or world root)")
    p.add_argument("root", type=Path)
    a = p.parse_args(argv)
    path, where = resolve(a.root)
    print(json.dumps({"path": str(path) if path else None, "source": where,
                      "receipts": load_receipts(a.root)},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
