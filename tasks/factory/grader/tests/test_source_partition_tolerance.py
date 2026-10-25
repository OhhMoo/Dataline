"""A retry/primary ``source_partition`` choice alone does not fail C0 or R1..R4.

Receipts here come from ``world.ledger.table_contents`` itself, so the night
tests also prove the grader rebuilds the ledger's hash exactly.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import duckdb
import pytest

from tasks.factory.grader import receipt_engine as re_
from tasks.factory.grader.replay_isolation import _capture

RAW = "raw.factory_events"
DAY = "dt=2026-03-02"
PRIMARY = f"data/factory_feed/{DAY}/events.csv"
RETRY = f"data/factory_feed/{DAY}/events-retry.csv"
OTHER_DAY = "data/factory_feed/dt=2026-03-01/events.csv"
COLUMNS = ("event_id VARCHAR, delivered_at TIMESTAMP, business_day DATE, business_id VARCHAR, "
           "version INTEGER, event_type VARCHAR, entity_id VARCHAR, amount_cents BIGINT, "
           "source_partition VARCHAR")
DUP = ["EV-NORMAL-120800", "2026-03-02 04:00:00", "2026-03-02", "B-NORMAL-120800", 1,
       "settle", "ORG-A", 2000]
OTHERS = [
    ["EV-A-1", "2026-03-01 03:00:00", "2026-03-01", "B-A-1", 1, "settle", "ORG-A", 500, OTHER_DAY],
    ["EV-B-2", "2026-03-02 05:00:00", "2026-03-02", "B-B-2", 1, "settle", None, 700, PRIMARY],
]
HEADER = "event_id,delivered_at,business_day,business_id,version,event_type,entity_id,amount_cents\n"
CSV_ROW = "EV-NORMAL-120800,2026-03-02T04:00:00+00:00,2026-03-02,B-NORMAL-120800,1,settle,ORG-A,2000\n"
T0_SPEC = {"tables": [{"table": RAW, "grain_key": ["event_id"]}]}
REPLAY = {"targets": [RAW], "nights": [{"advance_hours": 24}]}
S03_TASKS = ("FT0385-mainframe-batch-ft05-auth-a", "FT0047-settlement-clearing-ft08-hold-b")
PILOT = Path(os.environ.get("ALKERA_PILOT_DIR", "/home/ubuntu/alkera_factory_1017/phase2_pilot_51"))


def _world(root: Path, rows: list[list], *, landing: bool = True) -> Path:
    db = root / "var/warehouse/warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{db.as_posix()}' AS wh")
    con.execute("CREATE SCHEMA wh.raw")
    con.execute(f"CREATE TABLE wh.raw.factory_events ({COLUMNS})")
    con.executemany("INSERT INTO wh.raw.factory_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.close()
    if landing:
        folder = root / "workspace/data/factory_feed" / DAY
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("events.csv", "events-retry.csv"):
            (folder / name).write_text(HEADER + CSV_ROW, encoding="utf-8")
    return root


def _rows(*partitions: str) -> list[list]:
    return [*OTHERS, *[[*DUP, p] for p in partitions]]


def _oracle_t0(grading: Path, world: Path) -> None:
    grading.mkdir(parents=True, exist_ok=True)
    snap = re_.snapshot_t0(world, T0_SPEC["tables"])
    (grading / "oracle_t0.json").write_text(json.dumps(snap), encoding="utf-8")


def _receipt(world: Path) -> dict:
    from world.ledger import table_contents
    return {"target": RAW, "kind": "table", "consumer": None, **table_contents(world, RAW)}


def _night(world: Path, grading: Path) -> dict:
    """One replayed night as advance_nights reports it, with the grader's snapshots."""
    specs = re_.partition_snapshot_specs(world, grading, REPLAY)
    return {"night": 1, "receipts": [_receipt(world)], "vnow": "2026-03-04T00:00:45Z",
            "runs": [{"status": "success", "executed": True}],
            "snapshots": {name: _capture(world, decl) for name, decl in specs.items()}}


def _nights(tmp_path: Path, agent: list[list], oracle: list[list], **kw) -> dict:
    grading = tmp_path / "grading"
    grading.mkdir(parents=True, exist_ok=True)
    oracle_world = _world(tmp_path / "oracle", oracle, landing=False)
    (grading / "oracle_receipts.json").write_text(
        json.dumps({"nights": [[_receipt(oracle_world)]]}), encoding="utf-8")
    world = _world(tmp_path / "agent", agent, **kw)
    return re_.compare_nights([_night(world, grading)], grading, REPLAY)[0]


def _c0(tmp_path: Path, agent: list[list], oracle: list[list]) -> dict:
    grading = tmp_path / "grading"
    _oracle_t0(grading, _world(tmp_path / "oracle", oracle, landing=False))
    return re_.compare_t0(_world(tmp_path / "agent", agent), grading, T0_SPEC)


# ---------------------------------------------------------------- Test 1


@pytest.mark.parametrize("agent, oracle", [(PRIMARY, RETRY), (RETRY, PRIMARY)])
def test_retry_or_primary_choice_passes_c0(tmp_path, agent, oracle):
    result = _c0(tmp_path, _rows(agent), _rows(oracle))
    assert result["passed"], result
    assert result["fraction"] == 1.0


@pytest.mark.parametrize("agent, oracle", [(PRIMARY, RETRY), (RETRY, PRIMARY)])
def test_retry_or_primary_choice_passes_the_night(tmp_path, agent, oracle):
    result = _nights(tmp_path, _rows(agent), _rows(oracle))
    assert result["passed"], result
    assert result["source_partition"]["receipts_relabelled"] == 1


def test_identical_partition_needs_no_relabel(tmp_path):
    result = _nights(tmp_path, _rows(PRIMARY), _rows(PRIMARY))
    assert result["passed"] and "source_partition" not in result


def test_rebuilt_hash_is_the_ledger_hash(tmp_path):
    world = _world(tmp_path / "agent", _rows(RETRY))
    night = _night(world, tmp_path / "grading")
    stored, count = re_._partition_hashes(night)["stored"]
    assert (stored, count) == (night["receipts"][0]["canonical_hash"],
                               night["receipts"][0]["row_count"])


# ---------------------------------------------------------------- Test 2


def test_other_day_folder_still_fails(tmp_path):
    moved = [*OTHERS, [*DUP, OTHER_DAY]]
    assert not _c0(tmp_path / "c0", moved, _rows(RETRY))["passed"]
    assert not _nights(tmp_path / "night", moved, _rows(RETRY))["passed"]


def test_undeduplicated_retry_still_fails(tmp_path):
    both = _rows(PRIMARY, RETRY)
    c0 = _c0(tmp_path / "c0", both, _rows(RETRY))
    assert not c0["passed"]
    assert any("duplicate grain keys" in issue for issue in c0["issues"])
    night = _nights(tmp_path / "night", both, _rows(RETRY))
    assert not night["passed"]
    assert not (night.get("source_partition") or {}).get("relabelled")


def test_no_landed_retry_file_means_no_relabel(tmp_path):
    result = _nights(tmp_path, _rows(PRIMARY), _rows(RETRY), landing=False)
    assert not result["passed"]
    assert "source_partition" not in result


def test_wrong_amount_with_retry_choice_still_fails(tmp_path):
    agent = [*OTHERS, [*DUP[:7], 2001, PRIMARY]]
    assert not _c0(tmp_path / "c0", agent, _rows(RETRY))["passed"]
    assert not _nights(tmp_path / "night", agent, _rows(RETRY))["passed"]


# ---------------------------------------------------------------- Test 3


@pytest.mark.parametrize("name", S03_TASKS)
def test_s03_rivals_still_fail_stage0(tmp_path, name):
    task = PILOT / name
    if not (task / "solution/solve.py").is_file():
        pytest.skip(f"S03 pilot task absent: {task}")
    from tasks.factory.compiler.peer_materializer import run_baselines_for_task
    copy = tmp_path / name
    shutil.copytree(task, copy)
    result = run_baselines_for_task(copy, tmp_path / "scratch")
    arms = result["baselines"]
    assert arms["oracle"]["total"] == 20.0 and arms["oracle"]["passed"], arms["oracle"]
    rivals = {k: v for k, v in arms.items() if k != "oracle"}
    assert len(rivals) >= 5, sorted(rivals)
    for arm, row in rivals.items():
        assert row["total"] < 20.0 and not row["passed"], (arm, row)
