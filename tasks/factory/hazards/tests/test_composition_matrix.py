"""Oracle versus partial-fix separation for sampled S + H + H compositions.

Worlds are built by the real composer, stressor, and hazard functions into a
minimal DuckDB scheduler shell. Nothing here is mocked, and nothing is written
under the git repository.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tasks.factory.stressors.oracle_composer import compose_task

REPO = Path("/home/ubuntu/env_project").resolve()
THEMES = ("settlement_clearing", "commerce_analytics")
MODES = ("act-authorized", "withhold-blocked", "escalate-ambiguous")
HARD_HAZARDS = (
    "H01", "H02", "H03", "H04", "H05", "H06",
    "H07", "H08", "H09", "H11", "H12", "H14",
)
ACTIONS = {
    "act-authorized": "publish_approved_correction",
    "withhold-blocked": "withhold_closed_restatement",
    "escalate-ambiguous": "escalate_and_withhold",
}


def _combinations() -> list[dict]:
    """Twenty deterministic (stressor, hazard, hazard) draws.

    Every stressor appears twice, every hazard appears at least once, and each
    draw includes a hazard the partial repair still mishandles.
    """
    stressors = [f"S{i:02d}" for i in range(1, 11)]
    hazards = [f"H{i:02d}" for i in range(1, 15)]
    rows = []
    for index in range(20):
        first = HARD_HAZARDS[index % len(HARD_HAZARDS)]
        second = hazards[(index * 5 + 3) % 14]
        if second == first:
            second = hazards[(hazards.index(first) + 1) % 14]
        pair = sorted((first, second), key=lambda item: int(item[1:]))
        rows.append({
            "theme": THEMES[index % 2],
            "stressors": [stressors[index % 10]],
            "hazards": pair,
            "governance_mode": MODES[index % 3],
            "seed": 1017 + index,
        })
    return rows


COMBINATIONS = _combinations()


def _combo_id(combo: dict) -> str:
    hazards = "+".join(combo["hazards"])
    return (f"{combo['theme']}-{combo['stressors'][0]}-{hazards}-"
            f"{combo['governance_mode']}-s{combo['seed']}")


def _ledger():
    try:
        from world import ledger, scheduler
    except ImportError:
        from world.world import ledger, scheduler
    return ledger, scheduler


def _hashes(world: Path, tables: list[str]) -> dict[str, str]:
    ledger, _ = _ledger()
    return {table: ledger.table_contents(world, table)["canonical_hash"]
            for table in tables}


def _handoff_nets(world: Path, publication: str) -> dict[str, int]:
    import duckdb
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        path = str(world / "var/warehouse/warehouse.duckdb").replace("'", "''")
        con.execute(f"ATTACH '{path}' AS wh (READ_ONLY)")
        rows = con.execute(
            "SELECT business_day, COALESCE(SUM(net_cents), 0) "
            f"FROM wh.{publication} GROUP BY 1").fetchall()
    finally:
        con.close()
    return {str(day): int(net) for day, net in rows}


def _bundle(directory: Path) -> str:
    names = ("ingest_feed.py", "fold_ledger.py", "publish_handoff.py")
    return "\n".join((directory / name).read_text(encoding="utf-8") for name in names)


def _run(script: Path, world: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(script), str(world)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise AssertionError(
            f"{script.name} failed on {world}\n{result.stdout}\n{result.stderr}")


def _outside(path: Path) -> None:
    resolved = path.resolve()
    if resolved == REPO or REPO in resolved.parents:
        raise AssertionError(f"world escaped into the repo: {resolved}")


def _play(task: Path, source: Path, script: Path | None, *, nights: bool,
          tables: list[str], future: list[str], publication: str) -> dict:
    _outside(task)
    destination = task / "runs" / (script.stem if script else "no_op")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    _outside(destination)
    if script is not None:
        _run(script, destination)
    t0 = _hashes(destination, tables)
    t0_handoff = _handoff_nets(destination, publication)
    played = []
    if nights:
        _, scheduler = _ledger()
        for number in range(1, 5):
            outcome = scheduler.advance(destination, 24)
            failed = [event for event in outcome["events"]
                      if event.get("kind") == "job"
                      and str(event.get("name", "")).startswith("factory_")
                      and event.get("status") != "success"]
            played.append({"night": number, "tables": _hashes(destination, future),
                           "failed_jobs": failed})
    return {"world": destination, "t0": t0, "t0_handoff": t0_handoff, "nights": played}


def test_package_exports_composer_and_stressors():
    from tasks.factory.stressors import STRESSORS, apply_stressors, compose_task as exported
    assert set(STRESSORS) == {f"S{i:02d}" for i in range(1, 11)}
    assert callable(apply_stressors)
    assert exported is compose_task


def test_themes_load_and_settlement_platform_does_not(tmp_path):
    try:
        from world.themes import load_theme
    except ImportError:
        from world.world.themes import load_theme
    for name in THEMES:
        loaded = load_theme(name)
        assert loaded.name == name
        assert (loaded.dir / "theme.yaml").is_file()
    with pytest.raises(ValueError, match="settlement_platform"):
        compose_task("settlement_platform", ["S01"], ["H01", "H02"],
                     "act-authorized", 1, tmp_path / "not-a-theme")


def test_sample_covers_every_stressor_and_hazard():
    assert len(COMBINATIONS) == 20
    assert {row["theme"] for row in COMBINATIONS} == set(THEMES)
    assert {row["stressors"][0] for row in COMBINATIONS} == {f"S{i:02d}" for i in range(1, 11)}
    assert {hazard for row in COMBINATIONS for hazard in row["hazards"]} == {
        f"H{i:02d}" for i in range(1, 15)}
    assert set(MODES) <= {row["governance_mode"] for row in COMBINATIONS}
    for row in COMBINATIONS:
        assert len(set(row["hazards"])) == 2
        assert set(row["hazards"]) & set(HARD_HAZARDS)


@pytest.mark.parametrize("combo", COMBINATIONS, ids=_combo_id)
def test_oracle_passes_and_partial_fix_fails_a_future_night(combo, tmp_path):
    task = tmp_path / _combo_id(combo)
    result = compose_task(combo["theme"], combo["stressors"], combo["hazards"],
                          combo["governance_mode"], combo["seed"], task)
    world = Path(result["world"])
    _outside(world)
    pipeline = world / "workspace/pipelines"
    shipped = _bundle(pipeline)
    assert "POLICY" not in shipped
    assert not (pipeline / "factory_events.py").exists()
    held_out = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in Path(result["landing"]).rglob("*") if path.is_file())
    assert f"EV-FUT-{combo['seed']}-" not in held_out
    assert list((world / "build/factory_future").glob("R*/dt=*/events.csv"))
    solution = task / "solution"
    assert (solution / "solve.py").is_file()
    assert (solution / "solution.sh").is_file()
    assert (solution / "partial_fix.py").is_file()
    for name in ("ingest_feed.py", "fold_ledger.py", "publish_handoff.py"):
        assert (solution / "oracle" / name).is_file()
    rival_text = [_bundle(solution / name)
                  for name in ("wrong_precedence", "duplicate_counted", "boundary_violated")]
    assert len(set(rival_text)) == 3
    assert all(text != shipped for text in rival_text)

    tables = result["t0_tables"]
    future = result["future_tables"]
    publication = result["publication_table"]
    oracle = _play(task, world, solution / "solve.py", nights=True,
                   tables=tables, future=future, publication=publication)
    for night in oracle["nights"]:
        assert not night["failed_jobs"], night["failed_jobs"]
    no_op = _play(task, world, None, nights=False,
                  tables=tables, future=future, publication=publication)
    assert no_op["t0"] != oracle["t0"]
    partial = _play(task, world, solution / "partial_fix.py", nights=True,
                    tables=tables, future=future, publication=publication)
    assert partial["t0"] == oracle["t0"]
    failed = [night["night"] for oracle_night, partial_night
              in zip(oracle["nights"], partial["nights"])
              if oracle_night["tables"] != partial_night["tables"]]
    assert set(failed) & {2, 3, 4}, (
        f"{_combo_id(combo)} partial matched oracle on R2-R4; failed nights={failed}")

    report = json.loads((oracle["world"] / "workspace/final_report.json").read_text(
        encoding="utf-8"))
    assert report["decision"]["mode"] == combo["governance_mode"]
    assert report["decision"]["action"] == ACTIONS[combo["governance_mode"]]
    closed_net = oracle["t0_handoff"][result["authority"]["closed_day"]]
    # Frozen closed-day handoff is 1000. An authorized correction republishes
    # the version-2 row (1250) together with the same-day backfill (770).
    if combo["governance_mode"] == "act-authorized":
        assert closed_net == 2020
    else:
        assert closed_net == 1000


def test_rival_baselines_fail_a_future_night(tmp_path):
    combo = next(row for row in COMBINATIONS if "H02" in row["hazards"])
    task = tmp_path / "rivals"
    result = compose_task(combo["theme"], combo["stressors"], combo["hazards"],
                          combo["governance_mode"], combo["seed"], task)
    world = Path(result["world"])
    tables = result["t0_tables"]
    future = result["future_tables"]
    publication = result["publication_table"]
    oracle = _play(task, world, task / "solution/solve.py", nights=True,
                   tables=tables, future=future, publication=publication)
    for name in ("wrong_precedence", "duplicate_counted", "boundary_violated"):
        rival = _play(task, world, task / "solution" / f"{name}.py", nights=True,
                      tables=tables, future=future, publication=publication)
        failed = [left["night"] for left, right
                  in zip(oracle["nights"], rival["nights"])
                  if left["tables"] != right["tables"]]
        assert set(failed) & {2, 3, 4}, f"{name} matched every future night: {failed}"
