"""C0 row-hash / grain-key parity and R1..R4 receipt fractions."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from tasks.factory.grader.receipt_engine import compare_nights, compare_t0


def _warehouse(root: Path, rows: list[tuple]) -> None:
    db = root / "var/warehouse/warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{db.as_posix()}' AS wh")
    con.execute("CREATE SCHEMA wh.warehouse")
    con.execute("CREATE TABLE wh.warehouse.fct (payment_id VARCHAR, amount INTEGER)")
    con.executemany("INSERT INTO wh.warehouse.fct VALUES (?, ?)", rows)
    con.close()


def _oracle(grading: Path, rows: list[list]) -> dict:
    grading.mkdir(parents=True, exist_ok=True)
    (grading / "oracle_t0.json").write_text(json.dumps({
        "warehouse.fct": {"columns": ["payment_id", "amount"], "rows": rows}}),
        encoding="utf-8")
    return {"tables": [{"table": "warehouse.fct", "grain_key": ["payment_id"],
                        "columns": ["payment_id", "amount"]}]}


def test_t0_row_hash_match_is_a_unit_fraction(tmp_path):
    root, grading = tmp_path / "world", tmp_path / "grading"
    _warehouse(root, [("a", 1), ("b", 2)])
    result = compare_t0(root, grading, _oracle(grading, [["b", 2], ["a", 1]]))
    assert result["passed"] is True
    assert result["fraction"] == 1.0
    assert type(result["fraction"]) is float


def test_t0_partial_repair_is_a_fraction_not_a_boolean(tmp_path):
    root, grading = tmp_path / "world", tmp_path / "grading"
    _warehouse(root, [("a", 1), ("b", 9)])
    result = compare_t0(root, grading, _oracle(grading, [["a", 1], ["b", 2]]))
    assert result["passed"] is False
    assert result["fraction"] == 0.5
    assert 0.0 < result["fraction"] < 1.0


def test_duplicate_grain_fails_even_when_row_hashes_match(tmp_path):
    root, grading = tmp_path / "world", tmp_path / "grading"
    rows = [("a", 1), ("a", 2)]
    _warehouse(root, rows)
    result = compare_t0(root, grading, _oracle(grading, [["a", 1], ["a", 2]]))
    assert result["passed"] is False
    assert result["fraction"] == 1.0
    assert any("duplicate grain" in issue for issue in result["issues"])


def _night(receipts, runs=None):
    return {"night": 1, "vnow": "2026-06-12T23:30:00Z", "receipts": receipts,
            "runs": runs if runs is not None else [{"status": "success", "executed": True}]}


def _sig(target, digest, count=1):
    return {"target": target, "kind": "table", "consumer": "clearing_bank",
            "canonical_hash": digest, "row_count": count}


def test_night_receipts_report_a_fraction_and_honor_last_per_target(tmp_path):
    grading = tmp_path / "grading"
    grading.mkdir()
    handoff, window = "warehouse.settlement_handoff", "warehouse.clearing_window"
    (grading / "oracle_receipts.json").write_text(json.dumps({"nights": [
        [_sig(handoff, "good"), _sig(window, "good")],
        [_sig(handoff, "n2"), _sig(window, "n2")],
        [_sig(handoff, "n3"), _sig(window, "n3")],
        [_sig(handoff, "final"), _sig(window, "final")],
    ]}), encoding="utf-8")
    replayed = [
        _night([_sig(handoff, "good"), _sig(window, "stale")]),
        _night([_sig(handoff, "n2"), _sig(window, "n2")]),
        _night([_sig(handoff, "other"), _sig(window, "other")]),
        _night([_sig(handoff, "wrong"), _sig(handoff, "final"),
                _sig(window, "final")]),
    ]
    partial = compare_nights(replayed, grading, {"targets": [handoff, window]})
    assert [row["fraction"] for row in partial] == [0.5, 1.0, 0.0, round(2 / 3, 6)]
    assert all(type(row["fraction"]) is float and 0.0 <= row["fraction"] <= 1.0
               for row in partial)
    assert partial[0]["passed"] is False and partial[1]["passed"] is True
    assert partial[3]["passed"] is False

    folded = compare_nights(replayed, grading, {"targets": [handoff, window],
                                                "match": "last_per_target"})
    assert folded[3]["passed"] is True
    assert folded[3]["fraction"] == 1.0
