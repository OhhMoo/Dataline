"""Future-night advances run on a copy and leave the graded world at t0."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tasks.factory.grader.replay_isolation import advance_nights


def _world(root: Path) -> None:
    state = root / "var/sched/state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"vnow": "2026-06-12T09:00:00Z", "runs": []}),
                     encoding="utf-8")
    (root / "canary.txt").write_text("t0", encoding="utf-8")
    platform = root / "workspace/.platform"
    platform.mkdir(parents=True)
    (platform / "daemon.py").write_text("agent-edit", encoding="utf-8")


def test_r1_through_r4_advance_a_copy(tmp_path, monkeypatch):
    root = tmp_path / "world"
    _world(root)
    before = {path.relative_to(root).as_posix(): path.read_bytes()
              for path in root.rglob("*") if path.is_file()}
    grading = tmp_path / "grading"
    (grading / "plant.txt").parent.mkdir(parents=True)
    (grading / "plant.txt").write_text("arrived", encoding="utf-8")
    shipped = grading / "platform"
    shipped.mkdir()
    (shipped / "daemon.py").write_text("shipped", encoding="utf-8")
    seen = []

    def fake_run(cmd, **kwargs):
        copy = Path(cmd[3])
        assert copy.resolve() != root.resolve()
        if not seen:
            assert (copy / "canary.txt").read_text(encoding="utf-8") == "t0"
        assert (copy / "plant.txt").read_text(encoding="utf-8") == "arrived"
        assert (copy / "workspace/.platform/daemon.py").read_text(encoding="utf-8") == "shipped"
        (copy / "canary.txt").write_text("moved", encoding="utf-8")
        state = copy / "var/sched/state.json"
        payload = json.loads(state.read_text(encoding="utf-8"))
        payload["vnow"] = "later"
        payload["runs"] = list(payload.get("runs") or []) + [
            {"status": "success", "executed": True, "job": "nightly"}]
        state.write_text(json.dumps(payload), encoding="utf-8")
        ledger = copy / "var/ledger/receipts.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write('{"target":"warehouse.t"}\n')
        seen.append(copy)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("tasks.factory.grader.replay_isolation.subprocess.run", fake_run)
    nights = [{"date": day, "advance_hours": 24, "land": [
        {"source": "plant.txt", "destination": "plant.txt"}]}
        for day in ("2026-06-12", "2026-06-13", "2026-06-14", "2026-06-15")]
    result = advance_nights(root, grading, nights, None, [
        {"source": "platform", "destination": "workspace/.platform", "replace": True}])
    assert [row["night"] for row in result] == [1, 2, 3, 4]
    assert len(seen) == 4
    after = {path.relative_to(root).as_posix(): path.read_bytes()
             for path in root.rglob("*") if path.is_file()}
    assert after == before
