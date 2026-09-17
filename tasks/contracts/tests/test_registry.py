#!/usr/bin/env python3
"""The package's own shape: the seven contracts, the interface, the dependency rule."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tasks.contracts import REGISTRY, Context, docs, evaluate, evaluate_all

CONTRACTS = {"exactly_once", "liveness", "version_precedence", "cutoff_integrity",
       "consumer_consistency", "auditability", "authorization"}
PKG = Path(__file__).resolve().parents[1]


def test_the_registry_holds_exactly_the_declared_contracts():
    assert set(REGISTRY) == CONTRACTS


@pytest.mark.parametrize("name", sorted(CONTRACTS))
def test_every_contract_exposes_the_interface(name):
    module = REGISTRY[name]
    assert module.NAME == name
    assert callable(module.evaluate)
    lines = module.DOC.strip().splitlines()
    assert 0 < len(lines) <= 10, f"{name}: DOC is {len(lines)} lines, max 10"
    text = module.DOC.lower()
    assert "invariant" in text and "stressed by" in text and "shortcut" in text


def test_docs_are_reachable_without_evaluating_anything():
    assert set(docs()) == CONTRACTS


@pytest.mark.parametrize("path", sorted(PKG.glob("*.py")), ids=lambda p: p.name)
def test_nothing_in_the_package_imports_from_a_task_or_from_world_or_gateway(path):
    """The dependency rule.  A library that imports its own callers is not one."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    banned = ("probes", "fixture_contract", "make_fixture_world", "world",
              "alkera_gateway", "live_fire", "schedule_model")
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        for n in names:
            head = n.split(".")[0]
            assert head not in banned, f"{path.name} imports {n}"


def test_evaluate_all_hands_each_contract_its_own_block(tmp_path):
    ctx = Context(world_root=tmp_path, receipts=[], params={
        "cutoff_integrity": {"cutoff_rule": {"offset_hours": 30}},
        "consumer_consistency": {"expected": {"1": "x"}},
        "not_a_contract": {"ignored": True},
    })
    out = evaluate_all(ctx)
    assert set(out) == {"cutoff_integrity", "consumer_consistency"}
    for result in out.values():
        assert result.points == 0.0
        assert "no receipts: contract not evaluable" in result.reason


def test_a_contract_that_raises_is_scored_never_propagated(tmp_path, monkeypatch):
    """A crashed contract that took the grader down would be indistinguishable
    from a hard task."""
    def boom(ctx):
        raise RuntimeError("detonated")
    monkeypatch.setattr(REGISTRY["liveness"], "evaluate", boom)
    result = evaluate("liveness", Context(world_root=tmp_path))
    assert result.points == 0.0
    assert "liveness.crashed" in result.reason and "detonated" in result.reason


def test_an_unknown_contract_is_a_packaging_error(tmp_path):
    with pytest.raises(KeyError):
        evaluate("sla_freshness", Context(world_root=tmp_path))
