"""A dbt/python shim row that could not see the warehouse may explain it, softly.

    python -m pytest -q tasks/harness/test_blindness_unscoped_shim.py

Run opus-decision-k8-20260926T080818Z quarantined 5 of 16 GT005/GT006 episodes
on `var/warehouse/warehouse.duckdb changed but no gateway call targets it`:
each had rewritten the warehouse through an audited `dbt build` or `python`,
whose shim then snapshotted only the workspace. These pin the rule that
readmits them and the limits that keep it from laundering anything else.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blindness  # noqa: E402


def layout(tmp: Path) -> dict:
    root = tmp / "world"
    (root / "var" / "warehouse").mkdir(parents=True, exist_ok=True)
    (root / "var" / "warehouse" / "warehouse.duckdb").write_bytes(b"x")
    (root / "workspace").mkdir(exist_ok=True)
    return {"world_root": str(root), "warehouse": str(root / "var/warehouse/warehouse.duckdb"),
            "scheduler": str(root / "var/sched/state.json"),
            "profiles_dir": str(root / "build/dbt"), "workspace": str(root / "workspace")}


BEFORE = {"var/warehouse/warehouse.duckdb": "a", "var/other.duckdb": "a"}
CONTAINER = {"/world": "", "/workspace": "workspace"}


def unattributed(tmp, rows, after):
    notes: list = []
    out = blindness._unattributed(BEFORE, after, rows, layout(tmp), CONTAINER, notes)
    return out, notes


def dbt_row(**result):
    row = {"tool": "shim.dbt", "op_class": "exec", "targets": [],
           "args": {"argv": ["dbt", "build"], "cwd": "/workspace/dbt_project"}}
    if result:
        row["result"] = result
    return row


def test_unscoped_dbt_row_explains_the_declared_warehouse(tmp_path):
    after = {**BEFORE, "var/warehouse/warehouse.duckdb": "b"}
    out, notes = unattributed(tmp_path, [dbt_row(returncode=0, changed=[])], after)
    assert out == [] and notes == ["var/warehouse/warehouse.duckdb"]


def test_unscoped_python_row_counts_too(tmp_path):
    row = {"tool": "shim.python", "op_class": "exec", "targets": [],
           "args": {"argv": ["python", "fix.py"]}, "result": {"returncode": 0}}
    after = {**BEFORE, "var/warehouse/warehouse.duckdb": "b"}
    assert unattributed(tmp_path, [row], after) == ([], ["var/warehouse/warehouse.duckdb"])


def test_no_shim_row_still_quarantines(tmp_path):
    after = {**BEFORE, "var/warehouse/warehouse.duckdb": "b"}
    out, notes = unattributed(tmp_path, [], after)
    assert out == ["var/warehouse/warehouse.duckdb"] and notes == []


def test_an_undeclared_file_is_never_laundered(tmp_path):
    after = {**BEFORE, "var/other.duckdb": "b"}
    out, _ = unattributed(tmp_path, [dbt_row(returncode=0)], after)
    assert out == ["var/other.duckdb"]


def test_a_scoped_row_must_actually_name_the_warehouse(tmp_path):
    watched = ["/world/var/warehouse/warehouse.duckdb"]
    after = {**BEFORE, "var/warehouse/warehouse.duckdb": "b"}
    silent = dbt_row(returncode=0, changed=[], watched=watched)
    out, notes = unattributed(tmp_path, [silent], after)
    assert out == ["var/warehouse/warehouse.duckdb"] and notes == []
    named = {**dbt_row(returncode=0, watched=watched), "targets": watched}
    out, notes = unattributed(tmp_path, [named], after)
    assert out == [] and notes == []                 # explained by its own target
