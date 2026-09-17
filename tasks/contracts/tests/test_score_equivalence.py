#!/usr/bin/env python3
"""
The copy of the scoring fold must still agree with the original.

tasks/contracts/_score.py restates tasks/*/tests/probes/_common.py's
score_clauses because the contracts package may not import from a task
directory.  A copy that drifts is worse than an import, so this test loads the
REAL _common.py by path - from every task that ships one - and asserts the two
folds agree on points, max, passed and the reason string, across the branches
that matter: all-pass, a failed scored clause, a failed FATAL clause (which
gates and does not pay), and an all-gate contract.
"""
from __future__ import annotations

import importlib.util
import itertools
import sys
from pathlib import Path

import pytest

from tasks.contracts._score import score_clauses
from tasks.contracts._types import Clause

REPO = Path(__file__).resolve().parents[3]
COMMONS = sorted(REPO.glob("tasks/GT*/tests/probes/_common.py"))


def _load(path: Path):
    name = "gt_probe_common_" + path.parts[-4].replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # registered before exec: @dataclass resolves its own annotations through
    # sys.modules[cls.__module__], and a module that is not there raises
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _cases():
    """Every combination of (ok, fatal) over three clauses, plus an all-gate one."""
    for flags in itertools.product([True, False], repeat=3):
        for fatals in itertools.product([True, False], repeat=3):
            yield [
                ("a", flags[0], 0.45, fatals[0]),
                ("b", flags[1], 0.20, fatals[1]),
                ("c", flags[2], 0.35, fatals[2]),
            ]


@pytest.mark.parametrize("common_path", COMMONS, ids=lambda p: p.parts[-4])
def test_fold_agrees_with_the_task_probes(common_path):
    common = _load(common_path)
    assert COMMONS, "no task ships probes/_common.py any more; this test is stale"
    for spec in _cases():
        mine = [Clause(id=n, passed=ok, points=w if ok else 0.0, max=w,
                       detail=f"detail {n}", fatal=f) for n, ok, w, f in spec]
        theirs = [common.Clause(name=n, ok=ok, weight=w, detail=f"detail {n}",
                                fatal=f) for n, ok, w, f in spec]
        got = score_clauses("X", mine, 8.0)
        want = common.score_clauses("X", theirs, 8.0)
        assert got.points == want.points, (spec, got.reason, want.reason)
        assert got.max == want.max
        assert got.passed == want.passed
        assert got.reason == want.reason


def test_fatal_clause_gates_and_never_pays():
    """The rule GT004 paid 6.25 of 33 points to learn."""
    clauses = [Clause.make("gate", True, "held", weight=0.45, fatal=True),
               Clause.make("scored", False, "did not hold", weight=0.55)]
    assert score_clauses("X", clauses, 10.0).points == 0.0
    clauses[1] = Clause.make("scored", True, "held", weight=0.55)
    assert score_clauses("X", clauses, 10.0).points == 10.0
    clauses[0] = Clause.make("gate", False, "broken", weight=0.45, fatal=True)
    result = score_clauses("X", clauses, 10.0)
    assert result.points == 0.0
    assert result.reason.startswith("ZERO - the mandatory clause 'gate' failed")


def test_max_points_defaults_to_the_scored_weight_base():
    clauses = [Clause.make("gate", True, "held", weight=9.0, fatal=True),
               Clause.make("a", True, "held", weight=0.6),
               Clause.make("b", False, "no", weight=0.4)]
    result = score_clauses("X", clauses)
    assert result.max == 1.0          # the FATAL clause's weight is excluded
    assert result.points == 0.6


def test_all_gate_contract_is_worth_its_weights_when_it_holds():
    clauses = [Clause.make("g1", True, "held", weight=1.0, fatal=True),
               Clause.make("g2", True, "held", weight=1.0, fatal=True)]
    assert score_clauses("X", clauses).points == 2.0
    clauses[1] = Clause.make("g2", False, "broken", weight=1.0, fatal=True)
    assert score_clauses("X", clauses).points == 0.0
