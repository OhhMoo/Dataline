"""Failing R2, R3, and R4 must not share one progress scalar."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import yaml

from tasks.factory.grader.baseline_runner import BASELINES, run
from tasks.factory.grader.communication_rubric import verify_report
from tasks.factory.grader.declarative_grade import grade
from tasks.factory.grader.receipt_engine import PROGRESS_KEYS, capture_oracle, progress_scalar
from tasks.factory.grader.recorded.declarative_grade import EVALUATORS


def _db(root: Path) -> None:
    db = root / "var/warehouse/warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{db.as_posix()}' AS wh")
    con.execute("CREATE SCHEMA wh.warehouse")
    con.execute("CREATE TABLE wh.warehouse.fct (payment_id VARCHAR, amount INTEGER)")
    con.execute("INSERT INTO wh.warehouse.fct VALUES ('a', 1), ('b', 2)")
    con.close()


def _sig(digest: str) -> dict:
    return {"target": "warehouse.handoff", "kind": "table", "consumer": "bank",
            "canonical_hash": digest, "row_count": 1}


def _grading(path: Path) -> None:
    path.mkdir()
    (path / "oracle_t0.json").write_text(json.dumps({
        "warehouse.fct": {"columns": ["payment_id", "amount"],
                          "rows": [["a", 1], ["b", 2]]}}), encoding="utf-8")
    (path / "oracle_receipts.json").write_text(json.dumps({"nights": [
        [_sig(f"n{i}")] for i in range(1, 5)]}), encoding="utf-8")
    spec = {
        "schema_version": 1, "task_id": "night-scalar",
        "t0": {"tables": [{"table": "warehouse.fct", "grain_key": ["payment_id"],
                           "columns": ["payment_id", "amount"]}]},
        "replay": {"nights": [{"advance_hours": 24}] * 4},
        "checks": {"C0": {"kind": "t0_parity"},
                   **{f"R{i}": {"kind": "night_receipts", "night": i} for i in range(1, 5)}}}
    (path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (path / "manifest.yaml").write_text(yaml.safe_dump({
        "task_id": "night-scalar",
        "checks": [{"id": cid, "points": 1, "in_pass_criterion": True}
                   for cid in ("C0", "R1", "R2", "R3", "R4")]}), encoding="utf-8")


def test_public_names_and_recorded_kinds_stay_beside_each_other():
    assert callable(grade) and callable(capture_oracle) and callable(run)
    assert callable(verify_report)
    assert BASELINES == ("oracle", "no_op", "wrong_precedence", "duplicate_counted",
                         "boundary_violated")
    assert set(EVALUATORS) == {
        "partition_parity", "key_uniqueness", "whole_object", "object_parity",
        "escalate_not_take", "probe", "diagnosis", "claims", "t0_parity",
        "night_receipt", "consumer_book", "register_row", "decision", "liveness",
        "report", "hygiene", "contract", "comm"}


def test_failing_r2_r3_and_r4_produce_three_scalars(tmp_path, monkeypatch):
    root, grading = tmp_path / "world", tmp_path / "grading"
    _db(root)
    _grading(grading)
    scalars = []
    for failed in (2, 3, 4):
        def fake_advance(world, grading_dir, nights, snapshots=None, prepare=None,
                         bad=failed, **_kwargs):
            return [{"night": index, "vnow": f"t{index}",
                     "receipts": [_sig("bad" if index == bad else f"n{index}")],
                     "runs": [{"status": "success", "executed": True}]}
                    for index in range(1, 5)]

        monkeypatch.setattr(
            "tasks.factory.grader.declarative_grade.advance_nights", fake_advance)
        quantities = grade(root, grading)["progress_quantities"]
        assert [quantities[key] for key in PROGRESS_KEYS[:3]] == [1.0, 1.0, 1.0]
        for index, key in enumerate(PROGRESS_KEYS[3:], 1):
            assert quantities[key] == (0.0 if index == failed else 1.0)
        scalars.append(progress_scalar(quantities))
    assert len(set(scalars)) == 3
    assert all(0.0 <= value <= 1.0 for value in scalars)
