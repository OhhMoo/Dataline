#!/usr/bin/env python3
"""Self-test for the grading side of the publication ledger (ledger.py).

    python test_ledger_harness.py

Same species as selftest.py and deliberately a separate file: selftest.py is
the detector/classifier battery and its `main()` is a fixed list, so a new
surface goes in beside it rather than inside it.

Fast and synthetic: no world is built here. The receipts are written by hand in
the exact shape world/world/ledger.py emits, which is the point - this file is
the reader half of the seam described in world/README.md#publication-ledger, and a reader
tested only against its own writer proves that the two agree, not that either
is right. The writer side is proved in world/tests/test_ledger.py against a
real advanced world.

What is measured here:
  * stage() copies the world's ledger into the grading zone, byte for byte,
    and reports honestly when a world published nothing;
  * load_receipts() prefers the staged copy over the live world tree;
  * malformed evidence RAISES rather than being read around - a missing key, an
    unknown key, a hole in the seq sequence;
  * receipts_between() partitions: consecutive checkpoints never double-count
    and never drop a receipt;
  * last_receipt() follows seq, not vnow.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger                          # noqa: E402

OK, BAD = [], []


def check(label, cond, detail=""):
    (OK if cond else BAD).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))


def receipt(seq, vnow, target="warehouse.warehouse.fct_revenue", **over):
    row = {"seq": seq, "vnow": vnow, "day": vnow[:10], "job": "dbt_build",
           "target": target, "kind": "table", "canonical_hash": f"h{seq}",
           "row_count": seq, "sample": [[vnow]], "consumer": "finance_daily"}
    row.update(over)
    return row


def write(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(
        json.dumps(r, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for r in rows))
    return path


ROWS = [receipt(1, "2026-03-02T01:06:30Z"),
        receipt(2, "2026-03-02T01:06:30Z", target="reports/settlement.csv",
                kind="file", row_count=None, sample=None, consumer=None),
        receipt(3, "2026-03-03T01:06:30Z"),
        receipt(4, "2026-03-04T01:06:30Z")]


# --------------------------------------------------------------- staging
def test_staging(tmp: Path):
    world, grading = tmp / "world", tmp / "grading"
    check("stage: a world that published nothing stages nothing",
          ledger.stage(world, grading) is None)
    source = write(world / ledger.LEDGER_REL, ROWS)
    staged = ledger.stage(world, grading)
    check("stage: the copy lands in the grading zone",
          staged == grading / ledger.STAGED_REL and staged.is_file())
    check("stage: byte for byte",
          staged.read_bytes() == source.read_bytes())

    # The rule that matters: the grader reads the COPY. Diverge the two and the
    # staged answer must be the one that comes back.
    write(world / ledger.LEDGER_REL, ROWS + [receipt(5, "2026-03-05T01:06:30Z")])
    check("load: the staged copy wins over the live world tree",
          [r["seq"] for r in ledger.load_receipts(grading)] == [1, 2, 3, 4])
    check("load: resolve() names where it read from",
          ledger.resolve(grading)[1] == "staged"
          and ledger.resolve(world)[1] == "world")
    check("load: a world root still reads, for a task that asks for it",
          [r["seq"] for r in ledger.load_receipts(world)] == [1, 2, 3, 4, 5])
    check("load: an absent ledger is [] and not an error",
          ledger.load_receipts(tmp / "nowhere") == [])


# --------------------------------------------- malformed evidence raises
def test_refusals(tmp: Path):
    def raises(rows_or_text, label):
        root = tmp / f"bad-{len(OK) + len(BAD)}"
        path = root / ledger.LEDGER_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(rows_or_text, str):
            path.write_text(rows_or_text, encoding="utf-8")
        else:
            write(path, rows_or_text)
        try:
            ledger.load_receipts(root)
        except ledger.LedgerError:
            check(label, True)
            return
        check(label, False, "no LedgerError raised")

    missing = {k: v for k, v in ROWS[0].items() if k != "consumer"}
    raises([missing], "refuse: a receipt with a missing key")
    raises([{**ROWS[0], "extra": 1}], "refuse: a receipt with an unknown key")
    raises([ROWS[0], receipt(3, "2026-03-03T01:06:30Z")],
           "refuse: a hole in the seq sequence (append-only means contiguous)")
    raises([receipt(2, "2026-03-02T01:06:30Z")],
           "refuse: a ledger that does not start at 1")
    raises("{not json\n", "refuse: a line that is not JSON")


# ------------------------------------------------------------- queries
def test_queries():
    c1, c2, c3 = ("2026-03-02T00:00:00Z", "2026-03-03T00:00:00Z",
                  "2026-03-05T00:00:00Z")
    first = ledger.receipts_between(ROWS, c1, c2)
    second = ledger.receipts_between(ROWS, c2, c3)
    check("between: [a, b) selects the span",
          [r["seq"] for r in first] == [1, 2]
          and [r["seq"] for r in second] == [3, 4])
    check("between: consecutive checkpoints partition the ledger",
          first + second == ledger.receipts_between(ROWS, c1, c3))
    check("between: a receipt exactly on the boundary belongs to one span only",
          [r["seq"] for r in ledger.receipts_between(
              ROWS, "2026-03-02T01:06:30Z", "2026-03-03T01:06:30Z")] == [1, 2])
    check("between: an open end means open",
          [r["seq"] for r in ledger.receipts_between(ROWS, None, None)]
          == [1, 2, 3, 4])
    check("between: offsets and Z compare as the same instant",
          [r["seq"] for r in ledger.receipts_between(
              ROWS, "2026-03-03T01:00:00+00:00", None)] == [3, 4])

    check("last: the highest seq for that target, not the highest vnow",
          ledger.last_receipt(ROWS, "warehouse.warehouse.fct_revenue")["seq"] == 4)
    check("last: a target nobody published is None",
          ledger.last_receipt(ROWS, "warehouse.warehouse.nothing") is None)
    # The property the whole ledger exists for: a bad intermediate publication
    # is still in the record after a good final one.
    bad = ledger.receipts_between(ROWS, c1, c2)[0]
    check("the bad intermediate publication survives the good final state",
          bad["canonical_hash"] != ledger.last_receipt(
              ROWS, "warehouse.warehouse.fct_revenue")["canonical_hash"])


# ------------------------------- the seam: keys match what the world writes
def test_seam():
    """The two halves of the seam agree, checked without an import.

    agent.md 10.2 forbids a task importing world internals, and this file keeps
    that rule even in a test: it PARSES world/world/ledger.py instead. The
    check is worth the awkwardness - a key added on the writer side and not
    here would make every grader silently refuse every receipt.
    """
    import ast

    source = (Path(__file__).resolve().parents[2]
              / "world" / "world" / "ledger.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("KEYS", "KINDS"):
                found[name] = tuple(e.value for e in node.value.elts)
    check("seam: the harness KEYS are the world's KEYS",
          found.get("KEYS") == ledger.KEYS,
          f"{found.get('KEYS')} vs {ledger.KEYS}")
    check("seam: the world writes the path the harness stages from",
          'LEDGER = Path("var") / "ledger" / "receipts.jsonl"' in source
          and ledger.LEDGER_REL.as_posix() == "var/ledger/receipts.jsonl")


def main() -> int:
    print("\n  ledger harness self-test")
    tmp = Path(tempfile.mkdtemp(prefix="ledger_harness_"))
    try:
        test_staging(tmp)
        test_refusals(tmp)
        test_queries()
        test_seam()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n  {len(OK)} passed, {len(BAD)} failed")
    if BAD:
        print("  FAILED: " + ", ".join(BAD))
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
