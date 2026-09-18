"""The native world strip (STATUS 4.3): author-side material comes out of the
staged world before a native subject is handed a shell.

    python -m pytest test_native_sanitize.py -q

A native subject reads FILES, not tool responses, so the response-side strip
(gateway workflow.py's AUTHOR_SIDE) does not protect it: GT004's first native
run read DATA-4402's `authoring` note - which names the second defect -
straight out of var/workflow/issues.json, and var/obs/definitions.yaml carried
the designer's commentary. native_stack.sanitize_world_files is the staged-copy
strip; these tests run it over a fixture tree, no world build required.
"""
from __future__ import annotations

import json
from pathlib import Path

import native_stack


def _world(tmp_path: Path) -> dict:
    root = tmp_path / "world"
    (root / "var" / "workflow").mkdir(parents=True)
    (root / "var" / "obs").mkdir(parents=True)
    (root / "workspace").mkdir()
    (root / "var" / "workflow" / "issues.json").write_text(json.dumps({
        "issues": [{
            "id": "DATA-4402", "status": "open", "reporter": "rsalas",
            "authoring": {"note": "the second defect lives in the layout switch"},
            "comments": [{
                "author": "kliu", "at": "2026-01-23T09:26:00Z",
                "body": "Expected.",
                "authoring": {"note": "this comment is the decoy"},
            }],
        }],
    }), encoding="utf-8")
    (root / "var" / "obs" / "definitions.yaml").write_text(
        "# ARCHETYPE A027 - EVERY SURFACE IS GREEN\n"
        "# A correctness monitor would delete the task.\n"
        "version: 1\n"
        "metrics:\n"
        "  row_count:\n"
        "    sql_scalar: SELECT 1  # inline comments stay (they parse away)\n",
        encoding="utf-8")
    return {"world_root": str(root), "workspace": str(root / "workspace")}


def test_authoring_blocks_come_out_recursively(tmp_path):
    out = native_stack.sanitize_world_files(_world(tmp_path))
    assert out["workflow"] == {"issues.json": 2}
    doc = json.loads((tmp_path / "world/var/workflow/issues.json")
                     .read_text(encoding="utf-8"))
    assert "authoring" not in json.dumps(doc)
    # the content the agent is supposed to read survives
    assert doc["issues"][0]["comments"][0]["body"] == "Expected."


def test_obs_comment_header_comes_out_and_the_document_still_parses(tmp_path):
    import yaml                                                # noqa: PLC0415
    out = native_stack.sanitize_world_files(_world(tmp_path))
    assert out["obs_comment_lines_removed"] == 2
    text = (tmp_path / "world/var/obs/definitions.yaml").read_text(
        encoding="utf-8")
    assert "A027" not in text and "delete the task" not in text
    parsed = yaml.safe_load(text)
    assert parsed["metrics"]["row_count"]["sql_scalar"] == "SELECT 1"


def test_a_clean_world_is_a_noop_not_an_error(tmp_path):
    root = tmp_path / "world"
    (root / "workspace").mkdir(parents=True)
    before = native_stack.sanitize_world_files(
        {"world_root": str(root), "workspace": str(root / "workspace")})
    assert before == {"workflow": {}, "obs_comment_lines_removed": 0}
