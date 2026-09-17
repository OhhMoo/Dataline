#!/usr/bin/env python3
"""
tasks/contracts - layer 1 of the composition framework.

A CONTRACT is an invariant a data platform must keep while the world moves.
Seven of them, fixed, lifted out of the checks GT004-GT008 each implement
one-off.  A composed task DECLARES which contracts hold:

    [composition.contracts.exactly_once]
    table = "warehouse.fct_orders"
    business_key = ["order_id", "line_nbr"]
    ...

and the engine evaluates them, instead of the task re-writing grade.py.

    from tasks.contracts import REGISTRY, evaluate_all
    from tasks.contracts._types import Context

    ctx = Context(world_root=root, before=facts, receipts=ledger,
                  params=toml["composition"]["contracts"])
    results = evaluate_all(ctx)          # {name: ContractResult}

WHAT A CONTRACT IS NOT.  It is not a task, and it is not a defect.  Difficulty
lives in the CONJUNCTION of 2-4 contracts that must hold simultaneously while a
deterministic event schedule moves the world (agent.md 8.10; TASK_DESIGN
2026-09-15, "Framework proposal"), not in the count of planted defects.  Each
contract names, in its DOC, the shortcut it exists to kill - that list is what
the shortcut-baseline battery is generated from.

DEPENDENCY RULE.  Nothing under tasks/contracts/ imports from a task directory,
from world/ or from gateway/.  duckdb is the only third-party import.  Rules
learned inside the tasks (the scoring fold, the aliased ATTACH, the
newline="" discipline) are COPIED here with their reasons attached, and
tests/test_score_equivalence.py proves the copy of the fold still agrees with
the original.
"""
from __future__ import annotations

from . import (auditability, authorization, consumer_consistency,
               cutoff_integrity, exactly_once, liveness, version_precedence)
from ._score import ContractResult, score_clauses
from ._types import Clause, ContractNotEvaluable, Context

REGISTRY = {
    exactly_once.NAME: exactly_once,
    liveness.NAME: liveness,
    version_precedence.NAME: version_precedence,
    cutoff_integrity.NAME: cutoff_integrity,
    consumer_consistency.NAME: consumer_consistency,
    auditability.NAME: auditability,
    authorization.NAME: authorization,
}

__all__ = ["REGISTRY", "Clause", "Context", "ContractNotEvaluable",
           "ContractResult", "score_clauses", "evaluate", "evaluate_all", "docs"]


def evaluate(name: str, ctx: Context, max_points: float | None = None) -> ContractResult:
    """Evaluate ONE contract against a context whose `params` are its own block.

    A contract that raises is scored, never propagated: a crashed contract that
    took the grader down would be indistinguishable from a hard task.
    """
    if name not in REGISTRY:
        raise KeyError(f"no contract named {name!r}; have {sorted(REGISTRY)}")
    module = REGISTRY[name]
    try:
        clauses = module.evaluate(ctx)
    except Exception as exc:                                # noqa: BLE001
        clauses = [Clause.make(f"{name}.crashed", False,
                               f"the contract raised {type(exc).__name__}: "
                               f"{str(exc).splitlines()[0][:200]}", weight=1.0)]
    return score_clauses(name, clauses, max_points)


def evaluate_all(ctx: Context, weights: dict | None = None) -> dict:
    """Evaluate every contract the context declares a block for.

    `ctx.params` is the whole [composition.contracts] table; each contract is
    handed its own block through Context.for_contract.
    """
    out = {}
    for name in (ctx.params or {}):
        if name not in REGISTRY:
            continue
        out[name] = evaluate(name, ctx.for_contract(name),
                             (weights or {}).get(name))
    return out


def docs() -> dict:
    """{name -> DOC} - what invariant, what stresses it, what the shortcut is."""
    return {name: module.DOC for name, module in REGISTRY.items()}
