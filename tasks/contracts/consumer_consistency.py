#!/usr/bin/env python3
"""CONSUMER CONSISTENCY: what the consumer was handed at each point on the
trajectory is what the oracle would have published at that point."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from pathlib import Path

from ._types import Clause, ContractNotEvaluable, Context, not_evaluable

NAME = "consumer_consistency"

DOC = """INVARIANT   every receipt's canonical_hash equals the hash of the object the
            oracle would have published at that receipt's own vnow.
STRESSED BY a repair that runs while a consumer keeps publishing: a dashboard
            repointed mid-cutover, a file written from a half-loaded mart.
SHORTCUT    fix the end state and let the intermediate publications stand -
            invisible to any grader that reads only the final warehouse.  The
            coverage clause stops the opposite shortcut, publishing nothing.
ORACLE      params.expected, or params.oracle "<file.py>::<fn>", hashed by the
            gateway's canonical form.  DESIGNED: no task writes receipts yet.
"""

EMPTY_DETAIL = "no receipts: contract not evaluable"


# --------------------------------------------------------------- audit bytes
# Byte-for-byte gateway/src/alkera_gateway/records.py::canonical, reduced to the
# types that survive a JSON round trip (a receipt was already parsed from JSON,
# so nothing else can appear).  Copied, not imported: this package does not
# depend on the gateway.
def _json_value(value):
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {"type": "float", "value": str(value)}
    return value


def canonical(value) -> bytes:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _load_oracle(spec: str, world_root: Path):
    if "::" not in spec:
        raise ContractNotEvaluable(
            f"params.oracle must be '<file.py>::<function>', got {spec!r}")
    path_part, func = spec.split("::", 1)
    path = Path(path_part)
    if not path.is_absolute():
        path = Path(world_root) / path_part
    if not path.is_file():
        raise ContractNotEvaluable(f"params.oracle names {path}, which does not exist")
    module_spec = importlib.util.spec_from_file_location(
        "contract_oracle_" + path.stem, path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    fn = getattr(module, func, None)
    if fn is None:
        raise ContractNotEvaluable(f"{path} has no function {func!r}")
    return fn


def _keys(receipt: dict) -> list:
    """The spellings params.expected may be keyed by, most specific first."""
    seq = receipt.get("seq")
    target, day = receipt.get("target"), receipt.get("day")
    consumer = receipt.get("consumer")
    return [str(seq), f"{consumer}|{target}|{day}", f"{target}|{day}", str(day)]


def evaluate(ctx: Context) -> list:
    try:
        return _evaluate(ctx)
    except ContractNotEvaluable as exc:
        return not_evaluable(f"{NAME}.not_evaluable", str(exc))


def _evaluate(ctx: Context) -> list:
    receipts = list(ctx.receipts or [])
    if not receipts:
        return [Clause.make(f"{NAME}.evaluable", False, EMPTY_DETAIL, weight=1.0)]

    expected = {str(k): v for k, v in (ctx.param("expected") or {}).items()}
    oracle_spec = ctx.param("oracle")
    oracle = _load_oracle(oracle_spec, ctx.world_root) if oracle_spec else None
    if not expected and oracle is None:
        raise ContractNotEvaluable(
            "neither params.expected nor params.oracle is set, so there is "
            "nothing to compare the receipts against")
    kinds = ctx.param("kinds")
    rows = [r for r in sorted(receipts, key=lambda r: int(r.get("seq", 0)))
            if kinds is None or str(r.get("kind")) in set(kinds)]
    if not rows:
        return [Clause.make(f"{NAME}.evaluable", False,
                            f"{len(receipts)} receipt(s), none of kind "
                            f"{sorted(kinds or [])}", weight=1.0)]

    mismatched, uncovered, forged = [], [], []
    for r in rows:
        want = None
        for k in _keys(r):
            if k in expected:
                want = expected[k]
                break
        if want is None and oracle is not None:
            want = digest(oracle(r, ctx))
        if want is None:
            uncovered.append(r.get("seq"))
        elif r.get("canonical_hash") != want:
            mismatched.append(
                f"seq {r.get('seq')} @ {r.get('vnow')} {r.get('target')}/"
                f"{r.get('day')}: published {str(r.get('canonical_hash'))[:12]}, "
                f"oracle {str(want)[:12]}")
        if r.get("sample") is not None and digest(r["sample"]) != r.get("canonical_hash"):
            forged.append(r.get("seq"))

    checkpoint = str(rows[-1].get("vnow"))
    return [
        Clause.make(
            f"{NAME}.hashes_match", not mismatched,
            (f"all {len(rows)} receipt(s) carry the hash the oracle would have "
             f"published at their own vnow" if not mismatched else
             f"{len(mismatched)} receipt(s) do not match the oracle at their own "
             f"vnow: {mismatched[:3]}"),
            weight=0.55, fatal=True, checkpoint=checkpoint),
        Clause.make(
            f"{NAME}.every_receipt_covered", not uncovered,
            ("every receipt has an expectation" if not uncovered else
             f"{len(uncovered)} receipt(s) have no expected hash at all "
             f"({uncovered[:4]}): an unexplained publication is not evidence of "
             f"consistency"),
            weight=0.25, checkpoint=checkpoint),
        Clause.make(
            f"{NAME}.hash_is_canonical", not forged,
            ("every receipt that carries a sample hashes to its own recorded "
             "canonical_hash" if not forged else
             f"{len(forged)} receipt(s) record a hash that is not the hash of "
             f"their own payload ({forged[:4]}) - the ledger was edited after "
             f"the fact"),
            weight=0.20, checkpoint=checkpoint),
    ]
