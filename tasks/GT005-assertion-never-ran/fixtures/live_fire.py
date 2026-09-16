#!/usr/bin/env python3
"""
GT005 live-fire probe - the one discriminating gate, and the only place it is
implemented.

THE RULE THIS FILE EXISTS FOR. "Every idempotency- or check-shaped assertion
needs a LIVENESS clause." This archetype IS a check that does not run, so a
rubric that asked only "is there a relationships test in the YAML?" would award
full marks to the world as shipped - the test is already in the YAML, and has
been for six weeks, and has never executed once.

So nothing here reads the project. It plants rows and runs the build.

    PHASE clean   the agent's project, the agent's warehouse, untouched.
                  `dbt build` MUST exit 0.
                  Kills: any "fix" that makes the suite fail unconditionally.

    PHASE recur   one NEW invoice line stamped with the same unconformed vendor
                  code the incident is about, dated after the last loaded day -
                  i.e. exactly what tomorrow's extract lands.
                  `dbt build` MUST exit 0.
                  Kills: repairing the DATA instead of the TRANSFORMATION. An
                  agent that rewrote raw rows, or back-filled the mart by hand,
                  leaves a model that still emits the vendor code; with the
                  assertion restored, tomorrow's row makes the build red, and a
                  pipeline that goes red on correct input is not repaired.
                  Also kills row-count-pinning tests.

    PHASE novel   one NEW invoice line stamped with a code in neither the
                  registry nor the crosswalk - a source system nobody has
                  conformed.
                  `dbt build` MUST exit NON-ZERO.
                  Kills: the assertion that never ran. Also kills restoring it
                  at `severity: warn` (warn does not change the exit code) and
                  restoring it with `enabled: false`.

    PHASE novel_sibling
                  the same never-conformed code, on the CREDIT MEMO chain.
                  `dbt build` MUST exit NON-ZERO.
                  Kills: patching the symptom. The swallowed schema file
                  declares the conformance control for BOTH live billing
                  staging models. An agent that repairs the invoice-line chain
                  and writes itself a fresh test for that one model has covered
                  the model it happened to look at; the identical exposure is
                  still open on the sibling, whose fact table joins the same
                  dimension the same inner way. Restoring the FILE - or auditing
                  what the project declares against what it parses - covers both
                  with one edit, which is the difference this phase measures.
                  Note what it does NOT require: the sibling chain is correct
                  today and needs no data repair, so this phase adds discovery,
                  not work.

All four must hold. Three out of four is zero, because each phase exists to kill
a different way of faking the others.

Each phase runs against its own COPY of the warehouse and the project, so the
probe cannot disturb the tree the data checks grade, and the phases cannot
disturb each other.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import make_fixture_world as mfw          # noqa: E402

PHASES = ("clean", "recur", "novel", "novel_sibling")
EXPECT_ZERO = {"clean": True, "recur": True, "novel": False, "novel_sibling": False}


def _plant_invoice_line(warehouse: Path, code: str, line_id: int, day: str,
                        amount: float) -> None:
    """One new posted invoice line, exactly as the nightly extract would land it.

    Aliased ATTACH (F622): `duckdb.connect('.../warehouse.duckdb')` names the
    catalog `warehouse`, which collides with the `warehouse` schema and makes
    every two-part reference ambiguous.
    """
    import duckdb

    con = duckdb.connect()
    con.execute(f"ATTACH '{Path(warehouse).as_posix()}' AS wh")
    try:
        con.execute(
            "INSERT INTO wh.raw.billing_invoice_line VALUES "
            "(?, ?, ?, ?, ?, ?, ?, 'posted')",
            [line_id, f"INV-LF-{line_id}", code, 900001, day, amount, "EUR"])
        con.execute("CHECKPOINT")
    finally:
        con.close()


def _plant_credit_memo(warehouse: Path, code: str, memo_id: int, day: str,
                       amount: float) -> None:
    """The same event on the SIBLING chain, which is correct today and has no
    parsed assertion either."""
    import duckdb

    con = duckdb.connect()
    con.execute(f"ATTACH '{Path(warehouse).as_posix()}' AS wh")
    try:
        con.execute(
            "INSERT INTO wh.raw.billing_credit_memo VALUES "
            "(?, ?, ?, ?, ?, ?, ?, 'issued')",
            [memo_id, f"INV-CM-LF-{memo_id}", code, 900001, day, amount, "EUR"])
        con.execute("CHECKPOINT")
    finally:
        con.close()


def run_phase(phase: str, project: Path, warehouse: Path, contract: dict,
              scratch: Path, *, timeout: int = 900) -> dict:
    """Copy, plant, build. Returns the phase's verdict; never raises on a red build."""
    lf = contract["live_fire"]
    work = Path(scratch) / phase
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    wh = work / "warehouse.duckdb"
    shutil.copy(warehouse, wh)
    Path(str(wh) + ".wal").unlink(missing_ok=True)
    proj = work / "dbt_project"
    shutil.copytree(project, proj, ignore=shutil.ignore_patterns(
        "target", "logs", "dbt_packages", "*.duckdb", "*.duckdb.wal"))

    # A world in which the row cannot be planted is a world that FAILED this
    # phase, not a grader crash. An agent that dropped raw.billing_credit_memo
    # has made the pipeline permanently incapable of catching an unconformed key
    # on that chain, which is exactly what this gate measures; raising here
    # instead would turn a scoreable episode into a harness_error and delete the
    # measurement. (Found by running the grader against a stale world whose
    # warehouse predated the sibling chain.)
    try:
        if phase == "recur":
            _plant_invoice_line(wh, lf["recur_vendor_code"], lf["plant_line_id"],
                                lf["plant_date"], lf["plant_amount"])
        elif phase == "novel":
            _plant_invoice_line(wh, lf["novel_vendor_code"], lf["plant_line_id"] + 1,
                                lf["plant_date"], lf["plant_amount"])
        elif phase == "novel_sibling":
            _plant_credit_memo(wh, lf["novel_vendor_code"], lf["plant_memo_id"],
                               lf["plant_date"], lf["plant_amount"])
    except Exception as exc:                                  # noqa: BLE001
        return {"phase": phase, "returncode": None,
                "expected": "exit 0" if EXPECT_ZERO[phase] else "exit non-zero",
                "ok": False, "timed_out": False, "done": None, "found": None,
                "tail": f"could not plant the {phase} row: {exc}",
                "plant_failed": True}

    result = mfw.run_dbt(proj, wh, command="build",
                         profiles_dir=work / "_profiles",
                         target=work / "_target", timeout=timeout)
    rc = int(result["returncode"])
    ok = (rc == 0) if EXPECT_ZERO[phase] else (rc != 0)
    counts = mfw.parse_counts(result["output"])
    return {
        "phase": phase,
        "returncode": rc,
        "expected": "exit 0" if EXPECT_ZERO[phase] else "exit non-zero",
        "ok": bool(ok),
        "timed_out": bool(result["timed_out"]),
        "done": counts.get("done"),
        "found": counts.get("found"),
        "tail": "\n".join(result["output"].splitlines()[-25:]),
        "plant_failed": False,
    }


def run(project: Path, warehouse: Path, contract: dict, *, scratch: Path | None = None,
        timeout: int = 900, phases=PHASES) -> dict:
    """All three phases. `passed` is the conjunction - see the module docstring."""
    own = scratch is None
    base = Path(scratch or tempfile.mkdtemp(prefix="gt005-lf-"))
    try:
        results = [run_phase(p, Path(project), Path(warehouse), contract, base,
                             timeout=timeout) for p in phases]
    finally:
        if own:
            shutil.rmtree(base, ignore_errors=True)
    failed = [r["phase"] for r in results if not r["ok"]]
    return {"passed": not failed, "failed_phases": failed,
            "phases": {r["phase"]: r for r in results}}


def _cli(argv=None) -> int:
    import argparse

    from fixture_contract import CONTRACT

    ap = argparse.ArgumentParser(prog="live_fire")
    ap.add_argument("--project", required=True)
    ap.add_argument("--warehouse", required=True)
    ap.add_argument("--scratch")
    a = ap.parse_args(argv)
    out = run(Path(a.project), Path(a.warehouse), CONTRACT,
              scratch=Path(a.scratch) if a.scratch else None)
    for phase in PHASES:
        r = out["phases"][phase]
        print(f"{phase:6s} rc={r['returncode']:<4d} {r['expected']:<15s} "
              f"{'OK' if r['ok'] else 'FAIL'}   {r['done'] or ''}")
    print("live_fire:", "PASS" if out["passed"] else f"FAIL {out['failed_phases']}")
    return 0 if out["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
