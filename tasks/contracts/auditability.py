#!/usr/bin/env python3
"""AUDITABILITY: a declared assertion actually ran, its result was recorded, and
it still goes red on input it is supposed to catch."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from . import _world as W
from ._types import Clause, ContractNotEvaluable, Context, not_evaluable

NAME = "auditability"

DOC = """INVARIANT   every DECLARED assertion executed, its verdict is recorded as a
            definite state, and firing it at the input it exists to catch
            still turns the build red.
STRESSED BY anything that silently drops a test from the compiled set: a
            .dbtignore'd schema file, severity warn, enabled false.
SHORTCUT    reading the project instead of firing it.  "Is the test in the
            YAML?" awards full marks to the world as shipped - it HAS been
            there for six weeks and has never executed once.  So discrimination
            is FATAL and a contract with no declared phase fails it.
LIFTED FROM GT005's live-fire probe; GT007's DEFINITE_STATES rule.
"""

DEFINITE = ("pass", "fail", "error", "success")


def _load_results(ctx: Context) -> list:
    """Recorded assertion results.

    From params.results (a JSON file, dbt run_results shape or a bare list), or
    from the receipts whose kind is params.result_kind.
    """
    path = ctx.param("results")
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = ctx.world_root / path
        if not p.is_file():
            raise ContractNotEvaluable(
                f"params.results names {p}, which does not exist - so nothing "
                f"recorded that any assertion ran")
        doc = json.loads(p.read_text(encoding="utf-8"))
        rows = doc.get("results") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            raise ContractNotEvaluable(f"{p} carries no list of results")
        return rows
    kind = ctx.param("result_kind", "assertion")
    rows = [r for r in (ctx.receipts or []) if str(r.get("kind")) == kind]
    if not rows:
        raise ContractNotEvaluable(
            f"params.results is not set and no receipt carries kind {kind!r}: "
            f"there is no record that any assertion ever ran")
    return rows


def _name_of(row: dict) -> str:
    for k in ("name", "unique_id", "assertion", "test", "id"):
        if row.get(k):
            return str(row[k])
    return "?"


def _status_of(row: dict) -> str:
    for k in ("status", "state", "result"):
        if row.get(k) is not None:
            return str(row[k]).lower()
    return "none"


def _run_phase(ctx: Context, phase: dict) -> tuple:
    """Copy the world, plant the phase's rows, run the declared steps.

    Green == every step ran clean.  Red == some step failed.  A phase declares
    which of the two it requires; three out of four is zero, because each phase
    exists to kill a different way of faking the others.
    """
    runner = phase.get("runner") or ctx.param("runner", "pipeline")
    steps = W.normalise_steps(phase.get("steps") or ctx.param("pipeline.steps"))
    if runner not in ("pipeline", "advance"):
        raise ContractNotEvaluable(f"unknown runner {runner!r}")
    if runner == "pipeline" and not steps:
        raise ContractNotEvaluable(
            f"phase {phase.get('name')!r} declares no steps and "
            f"params.pipeline.steps is unset, so there is nothing to fire")
    expect = str(phase.get("expect", "green")).lower()
    if expect not in ("green", "red"):
        raise ContractNotEvaluable(
            f"phase {phase.get('name')!r}: expect must be 'green' or 'red'")
    tmp = Path(tempfile.mkdtemp(prefix="contract-auditability-"))
    try:
        root = W.copy_world(ctx.world_root, tmp / "world")
        planted = []
        for stmt in phase.get("plant_sql") or []:
            con = W.open_world(W.warehouse_path(root))
            try:
                con.execute(stmt)
                planted.append(stmt.strip().splitlines()[0][:60])
            except Exception as exc:                        # noqa: BLE001
                raise ContractNotEvaluable(
                    f"phase {phase.get('name')!r} could not plant its row: "
                    f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
            finally:
                con.close()
        if phase.get("plant"):
            planted.append(W.plant(root, phase["plant"]))
        if runner == "advance":
            runs = W.run_advance(root, float(phase.get("advance_hours") or
                                             ctx.param("advance_hours", 24)),
                                 require_success=False,
                                 advance=ctx.param("advance"))
            log = [{"step": r.get("job", "?"),
                    "error": r.get("error") or
                             (None if r.get("status") == "success" and r.get("executed")
                              else str(r.get("status") or "not executed"))}
                   for r in runs]
        else:
            log = W.run_pipeline(root, steps)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    failure = W.pipeline_failed(log)
    got = "red" if failure else "green"
    return got == expect, got, expect, failure, planted


def evaluate(ctx: Context) -> list:
    try:
        return _evaluate(ctx)
    except ContractNotEvaluable as exc:
        return not_evaluable(f"{NAME}.not_evaluable", str(exc))


def _evaluate(ctx: Context) -> list:
    declared = [str(x) for x in (ctx.param("declared") or [])]
    definite = tuple(s.lower() for s in (ctx.param("definite_states") or DEFINITE))
    clauses = []

    # ---- clauses 1-2: did the declared assertions run, with a real verdict?
    if declared:
        rows = _load_results(ctx)
        executed = {_name_of(r) for r in rows}
        never_ran = sorted(set(declared) - executed)
        clauses.append(Clause.make(
            f"{NAME}.declared_ran", not never_ran,
            (f"all {len(declared)} declared assertion(s) appear in the recorded "
             f"results ({len(rows)} row(s))" if not never_ran else
             f"{len(never_ran)} declared assertion(s) never executed: "
             f"{never_ran[:4]} - declared is not the same as parsed, and parsed "
             f"is not the same as run"),
            weight=0.30))
        # Only the declared rows are judged: a real dbt run_results.json also
        # records exposures and saved queries, as `no-op`, and those are not
        # assertions.
        indefinite = sorted(_name_of(r) for r in rows if _name_of(r) in set(declared)
                            and _status_of(r) not in definite)
        clauses.append(Clause.make(
            f"{NAME}.result_definite", not indefinite,
            (f"every declared assertion's result carries a definite verdict "
             f"{list(definite)}" if not indefinite else
             f"{len(indefinite)} recorded result(s) carry no definite verdict "
             f"({indefinite[:4]}): 'skipped' and 'insufficient_data' are the "
             f"states a blinded check leaves behind, not verdicts"),
            weight=0.20))

    # ---- clause 3 (FATAL): does the assertion still discriminate?
    phases = ctx.param("phases") or []
    if not phases:
        clauses.append(Clause.make(
            f"{NAME}.discriminates", False,
            "no live-fire phase is declared, so nothing here fired the assertion. "
            "An assertion that is only READ is the exact thing this contract "
            "exists to catch: declare phases with expect green/red, or do not "
            "claim auditability",
            weight=1.0, fatal=True))
        return clauses

    failed, notes = [], []
    for phase in phases:
        ok, got, expect, failure, planted = _run_phase(ctx, phase)
        name = str(phase.get("name", "?"))
        notes.append(f"{name}:{got}(want {expect})")
        if not ok:
            failed.append(f"{name}: expected {expect}, got {got}"
                          + (f" [{failure}]" if failure else ""))
    clauses.append(Clause.make(
        f"{NAME}.discriminates", not failed,
        (f"all {len(phases)} live-fire phase(s) landed on the colour they had to: "
         + ", ".join(notes) if not failed else
         f"{len(failed)} of {len(phases)} phase(s) wrong: {failed[:3]}. "
         f"Three out of four is zero - each phase kills a different way of "
         f"faking the others"),
        weight=1.0, fatal=True))
    return clauses
