"""Re-rooting host paths, the leak detector, and git normalisation."""
from __future__ import annotations

import json
import os
import subprocess

import duckdb
import pytest

import test_export_support  # noqa: F401  (puts harbor/ on sys.path)
from alkera_harbor.reroot import (EXPORT_MARKER, LeakError, Rerooter, needles_for,
                                  scan)
from alkera_harbor.worldbuild import normalize_git

GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00"}


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=GIT_ENV).stdout


@pytest.fixture
def stage(tmp_path):
    """A rollout-shaped export dir whose name carries the export marker."""
    root = tmp_path / f"{EXPORT_MARKER}case-abc123" / "rollout"
    (root / "world" / "var" / "dbt").mkdir(parents=True)
    (root / "grading").mkdir()
    return root


def _rerooter(root):
    return Rerooter([(str(root / "world" / "workspace"), "/workspace"),
                     (str(root / "home"), "/home/engineer"),
                     (str(root / "world"), "/world"),
                     (str(root / "grading"), "/tests/episode/grading")])


# ------------------------------------------------------------------ rerooting
def test_longest_prefix_and_path_boundary(stage):
    r = _rerooter(stage)
    w = stage / "world"
    text = (f"path: {w}/var/warehouse/warehouse.duckdb\n"
            f"ws: {w}/workspace/dbt\n"
            f"root: {w}\n"
            f'json: "{stage}/grading"\n'
            f"not-a-boundary: {w}s/x\n")
    out = r.text(text)
    assert "path: /world/var/warehouse/warehouse.duckdb" in out
    assert "ws: /workspace/dbt" in out
    assert "root: /world\n" in out
    assert 'json: "/tests/episode/grading"' in out
    assert f"not-a-boundary: {w}s/x" in out          # /x/worlds is not /x/world


def test_resolved_spelling_is_rewritten_too(tmp_path):
    real = tmp_path / "real"
    (real / "world").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    r = Rerooter([(str(link / "world"), "/world")])
    assert r.text(f"{real}/world/a") == "/world/a"
    assert r.text(f"{link}/world/a") == "/world/a"


def test_obj_rewrites_nested_strings_only(stage):
    r = _rerooter(stage)
    world = {"world_root": str(stage / "world"), "seed": 7,
             "warehouses": [{"name": "nova", "path": str(stage / "world/var/w.duckdb")}],
             "keys": [str(stage / "grading"), None, True]}
    assert r.obj(world) == {"world_root": "/world", "seed": 7,
                            "warehouses": [{"name": "nova", "path": "/world/var/w.duckdb"}],
                            "keys": ["/tests/episode/grading", None, True]}


def test_tree_rewrites_text_keeps_mode_and_refuses_binary(stage):
    r = _rerooter(stage)
    profiles = stage / "world" / "var" / "dbt" / "profiles.yml"
    profiles.write_text(f'path: "{stage}/world/var/warehouse/warehouse.duckdb"\n')
    profiles.chmod(0o640)
    assert r.tree(stage / "world") == ["var/dbt/profiles.yml"]
    assert profiles.read_text() == 'path: "/world/var/warehouse/warehouse.duckdb"\n'
    assert profiles.stat().st_mode & 0o777 == 0o640
    blob = stage / "world" / "var" / "blob.bin"
    blob.write_bytes(b"\xff\xfe\x00" + str(stage / "world").encode() + b"\x00")
    with pytest.raises(LeakError, match="binary file contains a host path"):
        r.tree(stage / "world")


def test_refuses_to_reroot_the_filesystem_root():
    with pytest.raises(ValueError):
        Rerooter([("/", "/world")])


# ------------------------------------------------------------------ the detector
def _needles(stage):
    return needles_for(export_dirs=[stage.parent], repo=None, home=None)


def test_clean_tree_scans_clean(stage):
    (stage / "world" / "a.txt").write_text("/world/var/warehouse/warehouse.duckdb\n")
    assert scan(stage, _needles(stage)) == []


@pytest.mark.parametrize("payload, label", [
    (lambda s: f"leak {s.parent}/rollout/world\n".encode(), "export temp dir"),
    (lambda s: f"only the marker: {EXPORT_MARKER}zzz\n".encode(), "export temp-dir marker"),
    (lambda s: b"\x00\x01binary " + str(s.parent).encode() + b"\xff", "export temp dir"),
    (lambda s: b"spawned /tmp/tmpab12cd_9 for scratch\n", "python mkdtemp path"),
])
def test_planted_host_path_is_found(stage, payload, label):
    (stage / "world" / "planted.dat").write_bytes(payload(stage))
    findings = scan(stage, _needles(stage))
    assert any(f.startswith("world/planted.dat:") and label in f for f in findings), findings


def test_host_path_hidden_in_a_git_object_is_found(stage):
    repo = stage / "workspace"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "notes.txt").write_text(f"built in {stage.parent}\n")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", "c")
    (repo / "notes.txt").write_text("clean now\n")      # only the blob remembers
    plain = [f for f in scan(stage, _needles(stage), git=False)]
    assert plain == []                                 # zlib hides it from a byte scan
    findings = scan(stage, _needles(stage))
    assert any("git object" in f for f in findings), findings


def test_host_path_in_duckdb_data_or_view_is_found(stage):
    db = stage / "world" / "var" / "warehouse.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA warehouse")      # same name as the catalog
    con.execute("CREATE TABLE warehouse.warehouse.t "
                "(id INTEGER, note VARCHAR, tags VARCHAR[])")
    con.execute("INSERT INTO warehouse.warehouse.t VALUES (1, 'fine', ['a'])")
    con.close()
    assert scan(stage, _needles(stage)) == []
    con = duckdb.connect(str(db))
    con.execute("INSERT INTO warehouse.warehouse.t VALUES (2, 'ok', [?])",
                [f"{stage}/x"])
    con.execute(f"CREATE VIEW v AS SELECT '{stage.parent}' AS p")
    con.close()
    findings = scan(stage, _needles(stage))
    assert any("row(s) of warehouse.t" in f for f in findings), findings
    assert any("view main.v" in f for f in findings), findings


def test_symlinks_are_refused(stage):
    (stage / "world" / "ln").symlink_to("/etc/hostname")
    with pytest.raises(LeakError, match="symlinks"):
        scan(stage, _needles(stage))


def test_needles_skip_unspecific_home():
    from pathlib import Path
    assert "exporter home directory" not in {
        n.label for n in needles_for(home=Path("/root"))}
    specific = needles_for(home=Path("/home/someone"))
    assert ("exporter home directory", b"/home/someone") in {
        (n.label, n.value) for n in specific}
    assert EXPORT_MARKER.encode() in {n.value for n in specific}


# ------------------------------------------------------------------ git normalisation
def _built_repo(path):
    """A workspace like the builders make: commit, then an untracked file and a
    tracked edit (what stage_discovery does to .envrc)."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / ".envrc").write_text("export A=1\n")
    (path / "model.sql").write_text("select 1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    (path / ".envrc").write_text("export A=1\nexport B=2\n")
    (path / "docs").mkdir()
    (path / "docs" / "onboarding.md").write_text("hi\n")
    (path / ".git" / "hooks").mkdir(exist_ok=True)
    (path / ".git" / "hooks" / "pre-commit.sample").write_text("#!/bin/sh\n")
    return path


def test_normalize_git_is_byte_stable_and_keeps_status(tmp_path):
    a = _built_repo(tmp_path / "a")
    b = _built_repo(tmp_path / "b")
    os.utime(b / "model.sql", (1, 1))                # different stat data
    status = _git(a, "status", "--porcelain")
    assert (a / ".git/index").read_bytes() != (b / ".git/index").read_bytes()
    rec_a = normalize_git(a / ".git")
    normalize_git(b / ".git")
    assert (a / ".git/index").read_bytes() == (b / ".git/index").read_bytes()
    assert _git(a, "status", "--porcelain") == status
    assert not list((a / ".git" / "hooks").glob("*.sample"))
    assert rec_a["sample_hooks_removed"] >= 1        # git's own templates + ours


def test_normalize_git_keeps_staged_changes(tmp_path):
    a = _built_repo(tmp_path / "a")
    _git(a, "add", "docs/onboarding.md")
    before = _git(a, "status", "--porcelain")
    record = normalize_git(a / ".git")
    assert record["index"] == "read-tree index tree"
    assert _git(a, "status", "--porcelain") == before
    assert json.dumps(record)
