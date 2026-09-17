#!/usr/bin/env python3
"""
GT004 gate G7 - the publication ledger. LAYER 4, and PENALTY ONLY.

    from probes import ledger
    result = ledger.run(world_root, contract=CONTRACT, grading=grading,
                        max_points=0.0)

WHAT IT IS FOR. Every other gate in this package reads the world's FINAL state.
A grader that reads only the final state cannot tell a pipeline that was right
all along from one that published a wrong number on Tuesday and corrected it on
Thursday - `var/sched/state.json` says a job succeeded, not what it made
visible. `world/LEDGER_CONTRACT.md` closes that: a job may declare
`publishes: [{target, kind, consumer}]`, and the world appends one receipt per
publication, at the virtual moment a consumer could first see it, carrying the
canonical contents of what was published.

THE ORACLE, and it is the whole check. GT004's advanced nights land NO new
data: the arrival stream stops at the end of history, so a correct nightly chain
recomputes the same mart and republishes the same extract every night. Two
rules follow, and the first needs no reference at all:

  1. STABILITY. Every receipt for one target, across the advanced nights,
     carries the same `canonical_hash`. This is where GT004's own defect
     reappears: `load_orders_delta.sql` appends the newest landing day with no
     merge key, so an agent that repaired the DATA and left the LOADER alone
     re-doubles that day on every advance and the published extract moves every
     night. GT008's C5 records "`canonical_hash` ... is not checked at all" as
     its weakest finding; here it is the primary comparison.
  2. AGREEMENT. `row_count` and every `sample` row agree with the harness-side
     reference (F121: expected.duckdb, in the grading zone, never in the agent's
     catalog). A target the reference does not carry - `bi_revenue_extract` is
     built by the pipeline and is not a reference table - is reported as
     compared on STABILITY ONLY rather than silently passed.

THE BOUNDARY IS THE CONTRACT'S, not taste. `trigger()` writes no receipts: a
manual trigger runs inside the episode container, where `var/ledger` is an empty
read-only tmpfs, so the receipt would vanish at teardown. The contract's stated
consequence is *grade the publications the clock advanced over*, so receipts at
or before the episode's `virtual_now` are dropped and a publication an agent
caused by hand is not in the ledger to begin with.

NOT EVALUABLE IS THE NORMAL ANSWER TODAY, and it is 0 points either way. GT004's
shipped world declares no `publishes:` (make_fixture_world.PUBLISHES is staged
only by tests/ledger_check.py, because the declaration lives in two fingerprint
sections and moving them would move every recorded world hash), and nothing in
the grading path calls `world.scheduler.advance`. So a rollout reaches this gate
with an empty ledger, it says so, and no existing score moves by a hundredth.
`tests/ledger_check.py` is where the gate is actually exercised, against real
receipts written by a real advance.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import duckdb

from ._common import ProbeResult, crashed

PROBE_ID = "G7"
TASK_ROOT = Path(__file__).resolve().parents[2]
HARNESS = TASK_ROOT.parent / "harness"


def _reader():
    """`tasks/harness/ledger.py`, the one reader the graders use.

    Loaded by path under a private module name: `ledger` is also the name of the
    world's WRITER (world/world/ledger.py), and this package puts the world
    package on sys.path, so importing it by bare name could bind either.
    """
    name = "gt004_harness_ledger"
    if name in sys.modules:
        return sys.modules[name]
    path = HARNESS / "ledger.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def receipts_for(grading, world_root) -> tuple:
    """(receipts, why). `receipts is None` means NOT EVALUABLE, never `wrong`.

    The grading zone is preferred over the world tree, per tasks/harness/
    ledger.py rule 1: a grader that reads its evidence out of the subject's own
    directory is one path mistake away from reading evidence the subject could
    have edited.
    """
    reader = _reader()
    if reader is None:
        return None, f"no ledger reader at {HARNESS / 'ledger.py'}"
    for root in [p for p in (grading, world_root) if p is not None]:
        path, where = reader.resolve(Path(root))
        if path is None:
            continue
        try:
            return list(reader.load_receipts(Path(root))), \
                f"read the {where} ledger at {path}"
        except Exception as exc:                        # noqa: BLE001
            return None, (f"the ledger at {path} is not readable as evidence: "
                          f"{type(exc).__name__}: {exc}")
    return None, ("the world published no receipts (no var/ledger/receipts.jsonl). "
                  "GT004's shipped world declares no `publishes:` and nothing in "
                  "the grading path advances the clock through "
                  "world.scheduler.advance, so this is the expected state and "
                  "not a finding")


def _reference_rows(reference: Path, schema: str, table: str):
    """(row_count, {row tuple as strings}) for one reference table, or None.

    Aliased ATTACH, READ_ONLY, three-part references (F622). Rows are compared
    as strings because a receipt's `sample` has been through canonical JSON and
    a DECIMAL has come back as a string on the way.
    """
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute("ATTACH '" + str(reference).replace("'", "''")
                    + "' AS e (READ_ONLY)")
        got = con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() WHERE database_name = 'e' "
            "AND schema_name = ? AND table_name = ?", [schema, table]).fetchone()
        if not got or not got[0]:
            return None
        rows = con.execute(f'SELECT * FROM e."{schema}"."{table}"').fetchall()
    except duckdb.Error:
        return None
    finally:
        con.close()
    return len(rows), {tuple(_text(v) for v in row) for row in rows}


def _text(value) -> str:
    return "" if value is None else str(value)


def audit(receipts: list, virtual_now: str, reference: Path | None,
          ref_schema: str) -> dict:
    """{audited, offences, targets, dropped} for the receipts after `virtual_now`.

    Pure: it reads the receipts and the harness-side reference, and nothing
    else. `ledger_check.py` calls it directly against a world it advanced
    itself, which is the only place the gate has ever seen a real receipt.
    """
    reader = _reader()
    after = [r for r in receipts
             if reader.parse_vtime(r["vnow"]) > reader.parse_vtime(virtual_now)]
    offences, targets = [], {}
    by_target = {}
    for receipt in after:
        by_target.setdefault(str(receipt["target"]), []).append(receipt)

    for target, rows in sorted(by_target.items()):
        note = {"receipts": len(rows), "compared": []}
        hashes = {r["canonical_hash"] for r in rows}
        note["compared"].append("stability")
        if len(hashes) > 1:
            moved = [f"seq {r['seq']} @ {r['vnow']} rows={r['row_count']}"
                     for r in rows]
            offences.append(
                f"{target}: published {len(hashes)} different objects across "
                f"{len(rows)} advanced night(s) while no new data arrived - "
                f"{moved}")
        ref = None
        if reference is not None and target.count(".") == 1:
            # the reference keeps every table under ONE schema per checkpoint
            # (CONTRACT layout.reference_schemas), so only the table name
            # carries over from the receipt's target
            ref = _reference_rows(reference, ref_schema, target.split(".")[-1])
        if ref is None:
            note["reference"] = "absent - compared on stability only"
            targets[target] = note
            continue
        want_rows, want_set = ref
        note["reference"] = f"{ref_schema}.{target.split('.')[-1]}"
        note["compared"] += ["row_count", "sample"]
        for receipt in rows:
            count = receipt.get("row_count")
            if count is not None and int(count) != want_rows:
                offences.append(
                    f"{target}: receipt {receipt['seq']} @ {receipt['vnow']} "
                    f"published {count:,} rows where the reference holds "
                    f"{want_rows:,}")
            for row in (receipt.get("sample") or []):
                if not isinstance(row, list):
                    note["sample_shape"] = (
                        "the writer's sample is not a list of rows; not compared")
                    break
                if tuple(_text(v) for v in row) not in want_set:
                    offences.append(
                        f"{target}: receipt {receipt['seq']} @ "
                        f"{receipt['vnow']} published a row the reference never "
                        f"held: {row}")
                    break
        targets[target] = note
    return {"audited": len(after), "offences": offences, "targets": targets,
            "dropped": len(receipts) - len(after)}


def run(world_root, *, contract: dict | None = None, config: dict | None = None,
        max_points: float = 0.0, grading: Path | None = None) -> "object":
    """Gate G7. Penalty-only: `max_points` is 0 and the points are never used.

    grade.py re-weights this the way it re-weights G5 - it reads `offences` out
    of the detail and charges the manifest's penalty - so the ProbeResult's own
    points carry no meaning and are always zero.
    """
    del config
    try:
        receipts, why = receipts_for(grading, world_root)
        if receipts is None or not receipts:
            return ProbeResult(PROBE_ID, 0.0, max_points,
                               f"not_evaluable: {why}", {"evaluable": False})
        virtual_now = str((contract or {}).get("virtual_now") or "")
        if not virtual_now:
            return ProbeResult(
                PROBE_ID, 0.0, max_points,
                "not_evaluable: CONTRACT publishes no virtual_now, so the "
                "episode's t0 - and therefore which receipts the clock advanced "
                "over - cannot be established", {"evaluable": False})
        layout = (contract or {}).get("layout") or {}
        ref_schema = str((layout.get("reference_schemas") or {}).get("t0")
                         or "warehouse")
        reference = None
        if grading is not None:
            candidate = Path(grading) / str(layout.get("reference")
                                            or "expected.duckdb")
            reference = candidate if candidate.is_file() else None
        result = audit(receipts, virtual_now, reference, ref_schema)
        if not result["audited"]:
            return ProbeResult(
                PROBE_ID, 0.0, max_points,
                f"not_evaluable: {len(receipts)} receipt(s) exist and none is "
                f"after the episode clock {virtual_now} - a publication the "
                f"agent caused by hand is not in the ledger, by contract",
                {"evaluable": False, **result})
        head = "; ".join(f"{t} x{n['receipts']} ({', '.join(n['compared'])})"
                         for t, n in sorted(result["targets"].items()))
        if result["offences"]:
            reason = (f"{len(result['offences'])} of {result['audited']} audited "
                      f"receipt(s) published something the correct pipeline "
                      f"would not have: {result['offences'][:4]}")
        else:
            reason = (f"{result['audited']} receipt(s) across the advanced "
                      f"nights all match what the correct pipeline would have "
                      f"published: {head}. {why}")
        return ProbeResult(PROBE_ID, 0.0, max_points, reason,
                           {"evaluable": True, **result})
    except Exception as exc:                            # noqa: BLE001 - scored
        return crashed(PROBE_ID, max_points, exc)
