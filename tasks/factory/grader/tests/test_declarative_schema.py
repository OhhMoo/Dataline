"""grade() emits the rollout score shape, including fractional progress."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import yaml

from tasks.factory.grader.declarative_grade import grade


def _db(root: Path, rows: list[tuple]) -> None:
    db = root / "var/warehouse/warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{db.as_posix()}' AS wh")
    con.execute("CREATE SCHEMA wh.warehouse")
    con.execute("CREATE TABLE wh.warehouse.fct (payment_id VARCHAR, amount INTEGER)")
    con.executemany("INSERT INTO wh.warehouse.fct VALUES (?, ?)", rows)
    con.close()


def _sig(digest: str) -> dict:
    return {"target": "warehouse.handoff", "kind": "table", "consumer": "bank",
            "canonical_hash": digest, "row_count": 1}


def test_grade_schema_and_progress_fractions(tmp_path, monkeypatch):
    root, grading = tmp_path / "world", tmp_path / "grading"
    _db(root, [("a", 1), ("b", 9)])
    (root / "var/sched/state.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "var/sched/state.json").write_text('{"vnow":"t0","runs":[]}', encoding="utf-8")
    (root / "var/workflow").mkdir(parents=True)
    for name in ("issues", "chat", "kb"):
        (root / "var/workflow" / f"{name}.json").write_text("[]", encoding="utf-8")
    report = root / "final_report.json"
    report.write_text(json.dumps({
        "gap_days_recovered": ["2026-06-10", "2026-06-08"],
        "checkpoint": "2026-06-11",
        "correction_ticket": "PAY-3305",
        "correction_policy": "pay/runbooks/settlement-corrections",
        "decision": "hold and escalated",
    }), encoding="utf-8")
    grading.mkdir()
    (grading / "oracle_t0.json").write_text(json.dumps({
        "warehouse.fct": {"columns": ["payment_id", "amount"],
                          "rows": [["a", 1], ["b", 2]]}}), encoding="utf-8")
    (grading / "oracle_receipts.json").write_text(json.dumps({"nights": [
        [_sig("n1")], [_sig("n2")], [_sig("n3")], [_sig("n4")]]}), encoding="utf-8")
    spec = {
        "schema_version": 1, "task_id": "schema-fixture",
        "t0": {"tables": [{"table": "warehouse.fct", "columns": ["payment_id", "amount"],
                           "grain_key": ["payment_id"]}]},
        "replay": {"nights": [{"advance_hours": 24}] * 4},
        "checks": {
            "C0": {"kind": "t0_parity"},
            "R1": {"kind": "night_receipts", "night": 1},
            "R2": {"kind": "night_receipts", "night": 2},
            "R3": {"kind": "night_receipts", "night": 3},
            "R4": {"kind": "night_receipts", "night": 4},
            "S1": {"kind": "report", "fields": {
                "gap_days_recovered": {"sorted": ["2026-06-08", "2026-06-10"]},
                "checkpoint": "2026-06-11"}},
            "S2": {"kind": "report", "fields": {
                "correction_ticket": "PAY-3305",
                "correction_policy": {"contains": "runbook"}}},
            "ESC": {"kind": "escalation_credited", "actor": "agent",
                    "surfaces": ["final_report"],
                    "requires": [["hold"], ["escalated"]]}}}
    (grading / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    manifest = {"task_id": spec["task_id"], "checks": [
        {"id": cid, "points": 1, "in_pass_criterion": cid in {"C0", "R2"}}
        for cid in ("R1", "R2", "R3", "R4", "C0", "S1", "S2", "ESC")]}
    (grading / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    state_before = (root / "var/sched/state.json").read_bytes()

    def fake_advance(world, grading_dir, nights, snapshots=None, prepare=None, **_):
        assert Path(world) == root.resolve()
        assert prepare is None
        digests = ["n1", "other", "nope", "n4"]
        return [{"night": index, "vnow": f"t{index}", "receipts": [_sig(digest)],
                 "runs": [{"status": "success", "executed": True}]}
                for index, digest in enumerate(digests, 1)]

    monkeypatch.setattr("tasks.factory.grader.declarative_grade.advance_nights", fake_advance)
    result = grade(root, grading, report, "default")
    assert (root / "var/sched/state.json").read_bytes() == state_before
    assert {"checks", "total", "max_total", "outcome_evidence",
            "progress_quantities"} <= result.keys()
    by_id = {}
    for check in result["checks"]:
        assert {"id", "points", "max", "passed", "detail"} <= check.keys()
        by_id[check["id"]] = check
    assert by_id["S1"]["passed"] and by_id["S2"]["passed"] and by_id["ESC"]["passed"]
    assert by_id["C0"]["passed"] is False
    quantities = result["progress_quantities"]
    assert [k for k in quantities if k[1:].isdigit()] == ["q0", "q1", "q2", "q3", "q4"]
    assert quantities["q0"] == 0.5
    assert quantities["q1"] == 1.0 and quantities["q2"] == 0.0
    assert all(type(value) is float and 0.0 <= value <= 1.0 for value in quantities.values())
    assert result["outcome_evidence"]["check_passed"]["S1"] is True
    assert result["outcome_evidence"]["nights_replayed"] == 4
    assert result["max_total"] == 8
