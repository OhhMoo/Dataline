#!/usr/bin/env python3
"""
The scoring fold, COPIED from tasks/*/tests/probes/_common.py::score_clauses.

WHY COPIED AND NOT IMPORTED.  `tasks/contracts/` must not import from a task
directory: the contracts were lifted out of the tasks, and a library that
imports its own callers cannot be reused by a task that does not exist yet.
So the rule is restated here, verbatim in behaviour, and
tests/test_score_equivalence.py loads the real _common.py BY PATH and asserts
the two folds agree - points, max, passed and the reason string - on the same
inputs, including the branches that only a fatal clause reaches.

THE RULE, and what it cost to learn it (GT004, measured 2026-09-14):

  A FATAL CLAUSE GATES; IT DOES NOT PAY.  The shipped world already satisfies
  some mandatory clauses - the stock loader IS live, and an agent that does
  nothing breaks nothing - so paying for them handed a no-op 5.2 of G2's 8
  points for behaviour it never produced.  Fatal clauses are therefore EXCLUDED
  from the weight base: they can take the contract to zero, never add to it.

  Corollary, and the reason liveness is fatal everywhere in this package: a load
  "fixed" into a permanent skip is idempotent by construction and repairs
  nothing.  It must score ZERO rather than collect the partial credit its other
  clauses would earn.

The only difference from _common.py is the field mapping: a Clause here carries
(id, passed, max) where _common carries (name, ok, weight).  `max` IS the
weight.  Nothing else about the arithmetic differs.
"""
from __future__ import annotations

from dataclasses import dataclass, field



@dataclass
class ContractResult:
    """What one contract is worth, and why.

    `as_check()` emits the dict shape agent.md 9.2.4 freezes for
    score.json["checks"], so a composed grader can append it directly.
    """
    id: str
    points: float
    max: float
    reason: str
    clauses: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.points >= self.max

    @property
    def fatal_failures(self) -> list:
        return [c for c in self.clauses if c.fatal and not c.passed]

    def as_check(self) -> dict:
        return {"id": self.id, "points": round(float(self.points), 2),
                "max": round(float(self.max), 2), "passed": self.passed,
                "detail": self.reason}

    def as_dict(self) -> dict:
        d = self.as_check()
        d["clauses"] = [c.as_dict() for c in self.clauses]
        d["extra"] = self.detail
        return d


def score_clauses(cid: str, clauses: list, max_points: float | None = None,
                  extra: dict | None = None) -> ContractResult:
    """Fold clauses into a result.  See the module header for the rule.

    max_points  the contract's weight in the composed rubric.  When omitted it
                is the sum of the NON-FATAL clause maxima - which is what a
                contract that assigned its own clause weights already means -
                falling back to the sum of every clause max when every clause is
                a gate (so an all-gate contract is worth something when it holds
                and zero when it does not).
    """
    gating = [c for c in clauses if c.fatal]
    scored = [c for c in clauses if not c.fatal]
    if max_points is None:
        base = sum(float(c.max) for c in scored)
        if not scored:
            base = sum(float(c.max) for c in clauses)
        max_points = base
    max_points = float(max_points)

    fatal_fail = [c for c in gating if not c.passed]
    if fatal_fail:
        points = 0.0
    elif not scored:
        # every clause is a gate: all of them passing IS the whole result
        points = max_points
    else:
        total_weight = sum(float(c.max) for c in scored) or 1.0
        points = max_points * sum(float(c.max) for c in scored if c.passed) / total_weight

    head = "; ".join(f"{'PASS' if c.passed else 'FAIL'} {c.id}: {c.detail}"
                     for c in clauses)
    if fatal_fail:
        head = (f"ZERO - the mandatory clause '{fatal_fail[0].id}' failed, so no "
                f"partial credit is available. " + head)
    detail = dict(extra or {})
    return ContractResult(id=cid, points=round(points, 2), max=round(max_points, 2),
                          reason=head, clauses=list(clauses), detail=detail)
