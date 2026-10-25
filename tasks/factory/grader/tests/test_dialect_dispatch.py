"""declarative_grade dispatches on spec.json `dialect`, and the probe dialect's
progress_checkpoints hook can only add to a result."""
from __future__ import annotations

import json
import types
from pathlib import Path

import duckdb
import pytest
import yaml

from tasks.factory.grader import declarative_grade as dg

GRADER = Path(__file__).resolve().parents[1]


def _sig(digest: str) -> dict:
    return {"target": "warehouse.handoff", "kind": "table", "consumer": "bank",
            "canonical_hash": digest, "row_count": 1}


def _probe_task(tmp_path: Path) -> tuple[Path, Path]:
    root, grading = tmp_path / "world", tmp_path / "grading"
    db = root / "var/warehouse/warehouse.duckdb"
    db.parent.mkdir(parents=True)
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{db.as_posix()}' AS wh")
    con.execute("CREATE SCHEMA wh.warehouse")
    con.execute("CREATE TABLE wh.warehouse.fct (payment_id VARCHAR, amount INTEGER)")
    con.executemany("INSERT INTO wh.warehouse.fct VALUES (?, ?)", [("a", 1), ("b", 9)])
    con.close()
    (root / "var/sched").mkdir(parents=True)
    (root / "var/sched/state.json").write_text('{"vnow":"t0","runs":[]}', encoding="utf-8")
    grading.mkdir()
    (grading / "oracle_t0.json").write_text(json.dumps({
        "warehouse.fct": {"columns": ["payment_id", "amount"],
                          "rows": [["a", 1], ["b", 2]]}}), encoding="utf-8")
    (grading / "oracle_receipts.json").write_text(json.dumps(
        {"nights": [[_sig("n1")], [_sig("n2")]]}), encoding="utf-8")
    spec = {"schema_version": 1, "task_id": "dispatch-fixture",
            "t0": {"tables": [{"table": "warehouse.fct",
                               "columns": ["payment_id", "amount"]}]},
            "replay": {"nights": [{"advance_hours": 24}] * 2},
            "checks": {"C0": {"kind": "t0_parity"},
                       "R1": {"kind": "night_receipts", "night": 1},
                       "R2": {"kind": "night_receipts", "night": 2}}}
    (grading / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (grading / "manifest.yaml").write_text(yaml.safe_dump({"task_id": "dispatch-fixture",
        "checks": [{"id": c, "points": 1, "in_pass_criterion": True}
                   for c in ("C0", "R1", "R2")]}), encoding="utf-8")
    return root, grading


@pytest.fixture
def replay(monkeypatch):
    calls = []

    def fake_advance(world, grading_dir, nights, snapshots=None, prepare=None, **kwargs):
        calls.append({"snapshots": snapshots, **kwargs})
        return [{"night": i, "vnow": f"t{i}", "receipts": [_sig(d)], "snapshots": {},
                 "runs": [{"status": "success", "executed": True}]}
                for i, d in enumerate(["n1", "other"], 1)]

    monkeypatch.setattr(dg, "advance_nights", fake_advance)
    return calls


def _progress(monkeypatch, **attrs):
    module = types.SimpleNamespace(**attrs)
    monkeypatch.setattr(dg, "_progress_module", lambda: (module, None))


def test_spec_dialect(tmp_path):
    assert dg.spec_dialect(None) == "probe"
    assert dg.spec_dialect(tmp_path / "absent.json") == "probe"
    path = tmp_path / "spec.json"
    path.write_text('{"checks": {}}', encoding="utf-8")
    assert dg.spec_dialect(path) == "probe"
    path.write_text('{"dialect": "kind"}', encoding="utf-8")
    assert dg.spec_dialect(path) == "kind"
    path.write_text('{"dialect": "vibes"}', encoding="utf-8")
    with pytest.raises(dg.GradingError):
        dg.spec_dialect(path)
    assert dg.spec_dialect(GRADER / "specs" / "gt009.spec.json") == "kind"


def test_grade_dispatches_kind_specs(tmp_path, monkeypatch):
    from tasks.factory.grader import kind_engine
    seen = {}
    monkeypatch.setattr(kind_engine, "kind_grade", lambda **kw: seen.update(kw) or {"k": 1})
    grading = tmp_path / "grading"
    grading.mkdir()
    (grading / "spec.json").write_text('{"dialect": "kind"}', encoding="utf-8")
    assert dg.grade(tmp_path / "w", grading, tmp_path / "r.json", "arm") == {"k": 1}
    assert seen["warehouse"] == (tmp_path / "w/var/warehouse/warehouse.duckdb").resolve()
    assert seen["workspace"] == (tmp_path / "w/workspace").resolve()
    assert seen["spec_path"] == grading / "spec.json" and seen["variant"] == "arm"
    assert seen["artifact_path"] == tmp_path / "r.json" and seen["task_dir"] is None


def test_main_dispatches_both_clis(tmp_path, monkeypatch, capsys):
    from tasks.factory.grader import kind_engine
    seen = {}
    monkeypatch.setattr(kind_engine, "kind_main",
                        lambda argv, task_dir=None: seen.update(argv=argv, task=task_dir) or 7)
    argv = ["--world", "w.duckdb", "--grading", str(tmp_path), "--world-root", str(tmp_path),
            "--spec", str(GRADER / "specs" / "gt009.spec.json"), "--json"]
    assert dg.main(argv, task_dir=tmp_path) == 7
    assert seen == {"argv": argv, "task": tmp_path.resolve()}
    monkeypatch.setattr(dg, "grade", lambda *a, **kw: {
        "checks": [], "total": 0.0, "max": 0.0, "passed": False, "args": a, "kw": kw})
    assert dg.main(["--grading", str(tmp_path), "--world-root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kw"] == {"spec": None, "manifest": None}


def test_no_progress_module_leaves_probe_grading_alone(tmp_path, monkeypatch, replay):
    root, grading = _probe_task(tmp_path)
    monkeypatch.setattr(dg, "_progress_module", lambda: (None, None))
    result = dg.grade(root, grading, None, "default")
    assert "snapshots" in replay[0] and "optional_snapshots" not in replay[0]
    assert list(result["progress_quantities"]) == ["q0", "q1", "q2", *dg.receipts.PROGRESS_KEYS]
    assert "progress_error" not in result


def test_progress_hook_only_adds(tmp_path, monkeypatch, replay):
    root, grading = _probe_task(tmp_path)
    monkeypatch.setattr(dg, "_progress_module", lambda: (None, None))
    plain = dg.grade(root, grading, None, "default")
    seen = {}

    def attach(result, **context):
        seen.update(context)
        result["progress_quantities"].update(q0=0.99, q9_extra=0.25)
        result["checks"][0]["detail"].update(fraction=-1, q_added=0.5)
        result["checks"][0]["passed"] = True
        result["total"] = 99
        return result

    _progress(monkeypatch, snapshot_specs=lambda spec: {"p:fct": {"table": "warehouse.fct"}},
              attach=attach)
    result = dg.grade(root, grading, None, "default")
    assert replay[-1]["optional_snapshots"] == {"p:fct": {"table": "warehouse.fct"}}
    assert set(seen) == {"world_root", "grading", "spec", "nights", "t0"}
    assert len(seen["nights"]) == 2 and seen["t0"]["oracle_rows"] == 2
    assert result["progress_quantities"].pop("q9_extra") == 0.25
    assert result["checks"][0]["detail"].pop("q_added") == 0.5
    assert result == plain


@pytest.mark.parametrize("where", ["snapshot_specs", "attach"])
def test_raising_progress_module_is_reported_not_fatal(tmp_path, monkeypatch, replay, where):
    root, grading = _probe_task(tmp_path)
    monkeypatch.setattr(dg, "_progress_module", lambda: (None, None))
    plain = dg.grade(root, grading, None, "default")

    def boom(*_a, **_k):
        raise RuntimeError("progress bug")

    hooks = {"snapshot_specs": lambda spec: {}, "attach": lambda result, **_: result}
    hooks[where] = boom
    _progress(monkeypatch, **hooks)
    result = dg.grade(root, grading, None, "default")
    assert result.pop("progress_error").startswith(f"{where}: RuntimeError: progress bug")
    assert result == plain


def test_optional_snapshot_failure_is_recorded(tmp_path, monkeypatch):
    from tasks.factory.grader import replay_isolation as iso
    root, grading = _probe_task(tmp_path)
    monkeypatch.setattr(iso.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    nights = iso.advance_nights(root, grading, [{"advance_hours": 24}],
                                {"fct": {"table": "warehouse.fct"}}, None,
                                optional_snapshots={"fct": {"table": "warehouse.missing"},
                                                    "ok": {"sql": "SELECT 1"},
                                                    "bad": {"table": "warehouse.missing"}})
    snaps = nights[0]["snapshots"]
    assert snaps["fct"]["columns"] == ["payment_id", "amount"]
    assert snaps["ok"] == {"rows": [[1]]}
    assert snaps["bad"]["error"].startswith("ValueError: table warehouse.missing")
    with pytest.raises(ValueError):
        iso.advance_nights(root, grading, [{"advance_hours": 24}],
                           {"bad": {"table": "warehouse.missing"}}, None)


def test_capture_oracle_calls_progress_rows(tmp_path, monkeypatch):
    import sys
    from tasks.factory.grader import receipt_engine
    root, grading = _probe_task(tmp_path)
    spec = json.loads((grading / "spec.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(receipt_engine, "advance_nights",
                        lambda *a, **k: [{"receipts": [_sig("n")]}] * 2)
    calls = []
    fake = types.ModuleType("tasks.factory.grader.progress_checkpoints")
    fake.capture_oracle_rows = lambda *a: calls.append(a) or {"rows": 3}
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    import tasks.factory.grader as pkg
    monkeypatch.setattr(pkg, "progress_checkpoints", fake, raising=False)
    out = receipt_engine.capture_oracle(root, grading, spec)
    assert out["progress"] == {"rows": 3} and calls == [(root, grading, spec)]

    def boom(*_a):
        raise RuntimeError("bad oracle rows")

    fake.capture_oracle_rows = boom
    out = receipt_engine.capture_oracle(root, grading, spec)
    assert out["nights"] == 2 and "bad oracle rows" in out["progress_error"]
