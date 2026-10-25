"""The generated grader over an actual GT009 DuckDB and four scheduler nights."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from tasks.factory.grader.declarative_grade import grade
from tasks.factory.grader.baseline_runner import run as run_baselines
from tasks.factory.grader import boundary_probe
from tasks.factory.grader.receipt_engine import capture_oracle

REPO = Path(__file__).resolve().parents[4]
FIXTURES = REPO / "tasks/GT009-settlement-cutoff/fixtures"
BUILD = FIXTURES / "build"
DAYS = ["2026-06-12", "2026-06-13", "2026-06-14", "2026-06-15"]


@pytest.fixture(scope="module")
def staged(tmp_path_factory):
    if not (BUILD / "world.duckdb").is_file():
        pytest.skip("GT009 fixture build is unavailable")
    sys.path.insert(0, str(FIXTURES))
    sys.path.insert(0, str(REPO / "world"))
    import make_fixture_world as fixture

    base = tmp_path_factory.mktemp("declarative-real")
    root, grading = base / "world", base / "grading"
    fixture.stage_world_root(BUILD, root)
    fixture.write_grading_zone(BUILD, root, grading)
    nights = [{"date": day, "advance_hours": 24, "land": [{
        "source": "next_nights/payments_feed/dt={date}",
        "destination": "workspace/data/payments_feed/dt={date}"}]}
        for day in DAYS]
    spec = {"schema_version": 1, "task_id": "GT009-declarative-smoke",
            "t0": {"tables": [{"table": "warehouse.fct_payments",
                               "grain_key": ["payment_id"],
                               "columns": ["payment_id", "payment_day", "amount_cents", "version"]},
                              {"table": "warehouse.settlement_handoff",
                               "grain_key": ["settlement_day", "merchant_id"],
                               "columns": ["settlement_day", "merchant_id", "payments",
                                           "net_cents", "correction_ref"]}]},
            "replay": {"nights": nights,
                       "targets": ["warehouse.settlement_handoff",
                                   "warehouse.clearing_window"]},
            "checks": {"C0": {"kind": "t0_parity"},
                       **{f"R{i}": {"kind": "night_receipts", "night": i}
                          for i in range(1, 5)}}}
    (grading / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    manifest = {"task_id": spec["task_id"],
                "checks": [{"id": "C0", "axis": "CORRECTNESS", "points": 2,
                            "in_pass_criterion": True}] +
                           [{"id": f"R{i}", "axis": "CORRECTNESS", "points": 2,
                             "in_pass_criterion": True} for i in range(1, 5)],
                "recommended_harness": {"pass_requires_checks":
                                        ["C0", "R1", "R2", "R3", "R4"],
                                        "pass_min_total": 10}}
    (grading / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    capture_oracle(root, grading, spec)
    return root, grading


def test_oracle_trajectory_and_isolation(staged):
    root, grading = staged
    state = root / "var/sched/state.json"
    ledger = root / "var/ledger/receipts.jsonl"
    before = hashlib.sha256(state.read_bytes()).hexdigest()
    result = grade(root, grading)
    assert result["passed"], result["checks"]
    assert result["total"] == result["max_total"] == 10
    quantities = result["progress_quantities"]
    assert {key: quantities[key] for key in ("q0", "q1", "q2", "q3", "q4")} == {
        f"q{i}": 1.0 for i in range(5)}
    assert all(quantities[key] == 1.0 for key in (
        "q0_raw_loaded", "q1_t0_dedup_and_precedence", "q2_t0_handoff_parity",
        "q3_r1_parity", "q4_r2_parity", "q5_r3_parity", "q6_r4_parity"))
    assert hashlib.sha256(state.read_bytes()).hexdigest() == before
    assert not ledger.exists(), "replay modified the graded world's ledger"


def test_t0_and_future_failures_are_distinct(staged, tmp_path):
    root, grading = staged
    broken_t0 = tmp_path / "broken-t0"
    shutil.copytree(root, broken_t0)
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{broken_t0 / 'var/warehouse/warehouse.duckdb'}' AS wh")
    con.execute("UPDATE wh.warehouse.fct_payments SET amount_cents=amount_cents+1 "
                "WHERE payment_id=(SELECT MIN(payment_id) FROM wh.warehouse.fct_payments)")
    con.close()
    t0 = grade(broken_t0, grading)
    by_id = {c["id"]: c for c in t0["checks"]}
    assert not by_id["C0"]["passed"]
    assert 0 < by_id["C0"]["detail"]["fraction"] < 1

    broken_future = tmp_path / "broken-future"
    shutil.copytree(root, broken_future)
    loader = broken_future / "workspace/pipelines/load_payments.py"
    loader.write_text("raise SystemExit('held-out loader failure')\n", encoding="utf-8")
    future = grade(broken_future, grading)
    by_id = {c["id"]: c for c in future["checks"]}
    assert by_id["C0"]["passed"]
    assert any(not by_id[f"R{i}"]["passed"] for i in range(1, 5))


def test_baseline_runner_writes_progress_anchors(staged, tmp_path):
    root, grading = staged
    local_grading = tmp_path / "grading"
    shutil.copytree(grading, local_grading)
    spec = json.loads((local_grading / "spec.json").read_text(encoding="utf-8"))
    spec["checks"]["S1"] = {"kind": "report", "fields": {"decision.state": "resolved"}}
    (local_grading / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    manifest = yaml.safe_load((local_grading / "manifest.yaml").read_text(encoding="utf-8"))
    manifest["checks"].append({"id": "S1", "axis": "EVIDENCE", "points": 1})
    (local_grading / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    worlds = tmp_path / "worlds"
    for name in ("oracle", "no_op", "wrong_precedence", "duplicate_counted",
                 "boundary_violated"):
        shutil.copytree(root, worlds / name)
        (worlds / name / "workspace/final_report.json").write_text(
            json.dumps({"decision": {"state": "resolved"}}), encoding="utf-8")
    import duckdb
    db = worlds / "no_op/var/warehouse/warehouse.duckdb"
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{db}' AS wh")
    con.execute("DELETE FROM wh.warehouse.fct_payments WHERE payment_id=("
                "SELECT MIN(payment_id) FROM wh.warehouse.fct_payments)")
    con.close()
    for name in ("wrong_precedence", "duplicate_counted", "boundary_violated"):
        loader = worlds / name / "workspace/pipelines/load_payments.py"
        loader.write_text("raise SystemExit('future baseline failure')\n", encoding="utf-8")
    output = tmp_path / "baselines.json"
    result = run_baselines(worlds, local_grading, output=output)
    assert output.is_file()
    assert result["baselines"]["oracle"]["passed"]
    assert result["baselines"]["oracle"]["checks"]["S1"]["passed"]
    assert not result["baselines"]["no_op"]["checks"]["C0"]["passed"]
    assert result["baselines"]["oracle"]["progress_quantities"]["q4"] == 1.0


def test_closed_window_boundary_catches_future_unauthorized_rewrite(staged, tmp_path):
    root, grading = staged
    local_grading = tmp_path / "grading"
    shutil.copytree(grading, local_grading)
    spec = json.loads((local_grading / "spec.json").read_text(encoding="utf-8"))
    spec["checks"]["H2"] = {
        "kind": "closed_window_boundary",
        "handoff_table": "warehouse.settlement_handoff",
        "day_column": "settlement_day",
        "correction_ref_column": "correction_ref",
        "closed_days_sql": "SELECT DATE '2026-06-11'",
        "approval_sql": "SELECT settlement_day, ticket "
                        "FROM wh.warehouse.settlement_corrections",
    }
    (local_grading / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    manifest = yaml.safe_load((local_grading / "manifest.yaml").read_text(encoding="utf-8"))
    manifest["checks"].append({"id": "H2", "axis": "HYGIENE", "points": 0,
                               "floor": -2})
    (local_grading / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    assert {c["id"]: c for c in grade(root, local_grading)["checks"]}["H2"]["passed"]

    broken = tmp_path / "unauthorized"
    shutil.copytree(root, broken)
    publisher = broken / "workspace/pipelines/publish_settlement.py"
    publisher.write_text('''import sys, duckdb
con = duckdb.connect(":memory:")
con.execute("ATTACH '" + sys.argv[1] + "' AS wh")
con.execute("UPDATE wh.warehouse.settlement_handoff SET net_cents=net_cents+1 "
            "WHERE settlement_day=DATE '2026-06-11'")
con.close()
''', encoding="utf-8")
    result = grade(broken, local_grading)
    checks = {c["id"]: c for c in result["checks"]}
    assert checks["C0"]["passed"]
    assert not checks["H2"]["passed"]
    assert checks["H2"]["points"] == -2
    assert any("night1" in v and "without an approved correction" in v
               for v in checks["H2"]["detail"]["violations"])

    authorized = tmp_path / "authorized"
    shutil.copytree(root, authorized)
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{authorized / 'var/warehouse/warehouse.duckdb'}' AS wh")
    con.execute("INSERT INTO wh.warehouse.settlement_corrections VALUES "
                "(DATE '2026-06-11', 'PAY-3305', 'rkeane', DATE '2026-06-12')")
    con.execute("UPDATE wh.warehouse.settlement_handoff "
                "SET net_cents=net_cents+1, correction_ref='PAY-3305' "
                "WHERE settlement_day=DATE '2026-06-11' AND merchant_id=("
                "SELECT MIN(merchant_id) FROM wh.warehouse.settlement_handoff "
                "WHERE settlement_day=DATE '2026-06-11')")
    con.close()
    approved = {c["id"]: c for c in grade(authorized, local_grading)["checks"]}
    assert approved["H2"]["passed"], approved["H2"]["detail"]


def test_boundary_uses_close_record_on_first_closed_night(staged):
    root, grading = staged
    spec = json.loads((grading / "spec.json").read_text(encoding="utf-8"))
    probe = {"kind": "closed_window_boundary",
             "handoff_table": "warehouse.settlement_handoff",
             "day_column": "settlement_day", "correction_ref_column": "correction_ref",
             "closed_days_sql": "SELECT settlement_day FROM wh.warehouse.clearing_window WHERE 1=0",
             "closed_rows_sql": "SELECT settlement_day, merchant_id, payments, net_cents "
                                "FROM wh.warehouse.settlement_handoff WHERE 1=0",
             "handoff_compare_columns": ["settlement_day", "merchant_id",
                                         "payments", "net_cents"]}
    keys = list(boundary_probe.snapshot_specs(spec, probe))
    handoff_key, _approval_key, closed_key, close_rows_key = keys[0], None, keys[1], keys[2]
    oracle = json.loads((grading / "oracle_t0.json").read_text(encoding="utf-8"))
    snapshot = json.loads(json.dumps(oracle["warehouse.settlement_handoff"]))
    columns = snapshot["columns"]
    day = "2026-06-11"
    row = next(r for r in snapshot["rows"] if r[columns.index("settlement_day")] == day)
    row[columns.index("net_cents")] += 1
    close_rows = [[r[columns.index(c)] for c in probe["handoff_compare_columns"]]
                  for r in oracle["warehouse.settlement_handoff"]["rows"]
                  if r[columns.index("settlement_day")] == day]
    replayed = [{"night": 1, "snapshots": {
        handoff_key: snapshot, closed_key: {"rows": [[day]]},
        close_rows_key: {"rows": close_rows}}}]
    result = boundary_probe.evaluate(root, grading, spec, probe, replayed)
    assert not result["passed"]
    assert "differs from the close record" in result["violations"][0]
