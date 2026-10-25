"""Compare declarative checks with grade.py scores already stored on built worlds.

The legacy scores are the `score` objects tests/grade.py wrote into rollout.json.
This module does not re-execute those graders. Receipt nights are graded here,
through declarative_grade, on the same world directory.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import duckdb
import yaml

from tasks.factory.grader.declarative_grade import grade
from tasks.factory.grader.receipt_engine import table_rows

REPO = Path(__file__).resolve().parents[4]
FIXTURES = REPO / "tasks/GT009-settlement-cutoff/fixtures"
OUT = Path("/home/ubuntu/alkera_factory_1017/grader_equiv")
GAP_DAYS = ["2026-06-08", "2026-06-09", "2026-06-10"]
PLANT_DAYS = ["2026-06-12", "2026-06-13", "2026-06-14", "2026-06-15"]
FCT = "warehouse.fct_payments"
HANDOFF = "warehouse.settlement_handoff"
WINDOW = "warehouse.clearing_window"
TARGETS = (HANDOFF, WINDOW)

GT009 = {
    "default": Path("/home/ubuntu/alkera_rollouts/GT009-settlement-cutoff/"
                    "default-20261017-k5-opus55h/r0"),
    "e1-corecraft-implicit": Path(
        "/home/ubuntu/desaturation_experiment_20261017/runs/GT009-settlement-cutoff/"
        "e1-corecraft-implicit-20261017-k5-opus55h/r0"),
    "e2-noisy-surface": Path(
        "/home/ubuntu/desaturation_experiment_20261017/runs/GT009-settlement-cutoff/"
        "e2-noisy-surface-20261017-k5-opus55h/r0"),
    "e3-compositional": Path(
        "/home/ubuntu/desaturation_experiment_20261017/runs/GT009-settlement-cutoff/"
        "e3-compositional-20261017-k5-opus55h/r0"),
}
GT004 = Path("/home/ubuntu/alkera_rollouts/GT004-doubling-window/"
             "default-20261017-k5-opus55h/r0")
COMPARED_GT009 = ("C0", "R1", "R2", "R3", "R4", "S1", "S2")
NOT_COMPARED_GT009 = ("D1", "D2", "D2a", "D2b", "D3", "L1", "H1", "H2")


def _legacy(episode: Path) -> dict:
    score = json.loads((episode / "rollout.json").read_text(encoding="utf-8"))["score"]
    return {row["id"]: {"passed": bool(row["passed"]), "points": row["points"],
                        "max": row["max"]}
            for row in score["checks"]}


def _materialise(rows_by_table: dict, dest_db_root: Path) -> Path:
    """Build a warehouse file whose attached catalog is not named warehouse.

    Opening a file called warehouse.duckdb makes catalog == schema and the
    fixture DDL (`CREATE SCHEMA warehouse`) fails. Materialise into another
    file, then copy those bytes to the path the ledger reader attaches.
    """
    sys.path.insert(0, str(FIXTURES))
    import make_fixture_world as fixture

    folder = dest_db_root / "var/warehouse"
    folder.mkdir(parents=True, exist_ok=True)
    scratch = folder / "ref.duckdb"
    db = folder / "warehouse.duckdb"
    scratch.unlink(missing_ok=True)
    db.unlink(missing_ok=True)
    con = duckdb.connect(str(scratch))
    try:
        for table, rows in rows_by_table.items():
            fixture.materialise(con, table, rows)
    finally:
        con.close()
    shutil.copy(scratch, db)
    scratch.unlink()
    return db


def _snapshot_from_reference(expected_t0: dict, dest_db_root: Path) -> dict:
    db = _materialise({FCT: expected_t0["fct"], HANDOFF: expected_t0[HANDOFF],
                       WINDOW: expected_t0[WINDOW]}, dest_db_root)
    return {table: table_rows(db, table) for table in (FCT, HANDOFF, WINDOW)}


def _oracle_receipts(nights: list[dict], dest_db_root: Path) -> list[list[dict]]:
    sys.path.insert(0, str(REPO / "world"))
    from world.ledger import table_contents

    signature_nights = []
    for entry in nights:
        _materialise({table: entry[table] for table in TARGETS}, dest_db_root)
        night = []
        for table in TARGETS:
            contents = table_contents(dest_db_root, table)
            night.append({"target": table, "kind": "table", "consumer": "clearing_bank",
                          "canonical_hash": contents["canonical_hash"],
                          "row_count": contents["row_count"]})
        signature_nights.append(night)
    return signature_nights


def _report_expectations(world: Path) -> dict:
    db = world / "var/warehouse/warehouse.duckdb"
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute(f"ATTACH '{db.as_posix()}' AS wh (READ_ONLY)")
        handed = {str(row[0])[:10] for row in con.execute(
            "SELECT DISTINCT settlement_day FROM wh.warehouse.settlement_handoff").fetchall()}
        checkpoint_rows = con.execute(
            "SELECT CAST(through_day AS VARCHAR) FROM wh.warehouse.load_checkpoint").fetchall()
    finally:
        con.close()
    checkpoint = sorted(str(row[0])[:10] for row in checkpoint_rows)[0]
    return {"gap_days_recovered": {"sorted": [day for day in GAP_DAYS if day in handed]},
            "checkpoint": checkpoint}


def _stage_gt009(episode: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    source = episode / "grading"
    shutil.copytree(source / "next_nights", dest / "next_nights")
    if (source / "platform").is_dir():
        shutil.copytree(source / "platform", dest / "platform")
    reference = json.loads((source / "expected/t0.json").read_text(encoding="utf-8"))
    trajectory = json.loads((source / "expected/variant_oracle.json").read_text(encoding="utf-8"))
    snapshot = _snapshot_from_reference(reference["oracle_t0"], dest / "_oracle_rows")
    shutil.rmtree(dest / "_oracle_rows")
    receipts = _oracle_receipts(trajectory["nights"], dest / "_oracle_hash")
    shutil.rmtree(dest / "_oracle_hash")
    (dest / "oracle_t0.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (dest / "oracle_receipts.json").write_text(
        json.dumps({"nights": receipts}), encoding="utf-8")
    legacy = _legacy(episode)
    expectations = _report_expectations(episode / "world")
    spec = {
        "schema_version": 1, "task_id": "GT009-settlement-cutoff",
        "t0": {"tables": [
            {"table": FCT, "grain_key": ["payment_id"]},
            {"table": HANDOFF, "grain_key": ["settlement_day", "merchant_id"]},
            {"table": WINDOW, "grain_key": ["closed_on", "settlement_day", "status"]}]},
        "replay": {
            "nights": [{"date": day, "advance_hours": 24, "land": [{
                "source": "next_nights/payments_feed/dt={date}",
                "destination": "workspace/data/payments_feed/dt={date}"}]}
                for day in PLANT_DAYS],
            "targets": list(TARGETS),
            "match": "last_per_target",
            "prepare": [{"source": "platform",
                         "destination": "workspace/.platform",
                         "replace": True}] if (dest / "platform").is_dir() else []},
        "checks": {
            "C0": {"kind": "t0_parity"},
            **{f"R{index}": {"kind": "night_receipts", "night": index}
               for index in range(1, 5)},
            "S1": {"kind": "report", "fields": expectations},
            "S2": {"kind": "report", "fields": {
                "correction_ticket": "PAY-3305",
                "correction_policy": {"contains": "runbook"}}}}}
    (dest / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    manifest = {"task_id": spec["task_id"], "checks": [
        {"id": cid, "axis": "CORRECTNESS", "points": legacy[cid]["max"],
         "in_pass_criterion": cid.startswith(("C", "R"))}
        for cid in COMPARED_GT009]}
    (dest / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


def _view(result: dict) -> dict:
    return {row["id"]: {"passed": row["passed"], "points": row["points"], "max": row["max"]}
            for row in result["checks"]}


def _agree(legacy: dict, got: dict, ids: tuple[str, ...]) -> dict:
    rows = {}
    for cid in ids:
        rows[cid] = {"legacy_passed": legacy[cid]["passed"],
                     "declarative_passed": got[cid]["passed"],
                     "legacy_points": legacy[cid]["points"],
                     "declarative_points": got[cid]["points"],
                     "agreed": legacy[cid]["passed"] == got[cid]["passed"]}
    return rows


def test_gt009_and_gt004_equivalence_against_stored_grade_py_scores():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"legacy_source": "rollout.json score written by tests/grade.py; "
                               "grade.py was not re-executed",
              "gt009": {}, "gt004": {}}
    for variant, episode in GT009.items():
        if not (episode / "world/var/warehouse/warehouse.duckdb").is_file():
            report["gt009"][variant] = {"status": "skipped",
                                        "reason": f"no built world at {episode}"}
            continue
        grading = OUT / "GT009" / variant
        _stage_gt009(episode, grading)
        artifact = episode / "world/workspace/final_report.json"
        result = grade(episode / "world", grading,
                       artifact if artifact.is_file() else None, variant)
        legacy = _legacy(episode)
        compared = _agree(legacy, _view(result), COMPARED_GT009)
        report["gt009"][variant] = {
            "status": "graded",
            "episode": str(episode),
            "compared": compared,
            "agreed_ids": [cid for cid, row in compared.items() if row["agreed"]],
            "disagreed_ids": [cid for cid, row in compared.items() if not row["agreed"]],
            "not_compared": list(NOT_COMPARED_GT009),
            "not_compared_reason": "decision, liveness and hygiene checks encode "
                                   "GT009's trajectory classifier and were not "
                                   "reimplemented as declarative probes",
            "progress_quantities": result["progress_quantities"]}
    if (GT004 / "world/var/warehouse/warehouse.duckdb").is_file():
        grading = OUT / "GT004" / "default"
        if grading.exists():
            shutil.rmtree(grading)
        grading.mkdir(parents=True)
        legacy = _legacy(GT004)
        spec = {"schema_version": 1, "task_id": "GT004-doubling-window", "checks": {
            "G1b": {"kind": "sql_assert", "equals": 0, "sql":
                    "SELECT COUNT(*) FROM (SELECT order_id, line_nbr FROM fct_orders "
                    "GROUP BY 1, 2 HAVING COUNT(*) > 1)"}}}
        (grading / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        (grading / "manifest.yaml").write_text(yaml.safe_dump({
            "task_id": spec["task_id"],
            "checks": [{"id": "G1b", "points": legacy["G1b"]["max"],
                        "in_pass_criterion": True}]}), encoding="utf-8")
        result = grade(GT004 / "world", grading, None, "default")
        compared = _agree(legacy, _view(result), ("G1b",))
        present = sorted(legacy)
        report["gt004"] = {
            "status": "graded",
            "episode": str(GT004),
            "compared": compared,
            "agreed_ids": [cid for cid, row in compared.items() if row["agreed"]],
            "disagreed_ids": [cid for cid, row in compared.items() if not row["agreed"]],
            "not_compared": [cid for cid in present if cid != "G1b"],
            "not_compared_reason": "G1a/G1c and the lifecycle probes re-execute "
                                   "pipelines against expected.duckdb; only the "
                                   "duplicate-key bit of G1b is a single SQL assert"}
    else:
        report["gt004"] = {"status": "skipped", "reason": f"no built world at {GT004}"}
    (OUT / "agreement.json").write_text(json.dumps(report, indent=2) + "\n",
                                        encoding="utf-8")
    graded = [item for item in report["gt009"].values() if item.get("status") == "graded"]
    assert graded, "no GT009 world was available to grade"
    disagreements = {variant: item["disagreed_ids"]
                     for variant, item in report["gt009"].items()
                     if item.get("disagreed_ids")}
    assert not disagreements, disagreements
    assert report["gt004"].get("status") == "graded"
    assert report["gt004"]["disagreed_ids"] == []
