"""Our digest == Harbor's Packager.compute_content_hash, byte for byte."""
from __future__ import annotations

import hashlib

import test_export_support as support
from alkera_harbor import digest

PACKAGER = r'''
import json, sys
from pathlib import Path
from harbor.publisher.packager import Packager
out = {}
for d in json.loads(sys.stdin.read()):
    h, files = Packager.compute_content_hash(Path(d))
    out[d] = {"digest": "sha256:" + h,
              "files": [f.relative_to(Path(d).resolve()).as_posix() for f in files]}
print(json.dumps(out))
'''


def _tricky_task(root):
    files = {
        "task.toml": "[task]\nname = \"a/b\"\n",
        "instruction.md": "do it\n",
        "README.md": "readme\n",
        "trajectory.json": "{}\n",
        "notes.txt": "not packaged: outside the hashed set\n",
        "environment/Dockerfile": "FROM x\n",
        "environment/episode/world/var/a b.json": "{}\n",
        "environment/episode/workspace/.git/HEAD": "ref: refs/heads/main\n",
        "environment/episode/workspace/.envrc": "export A=1\n",
        "environment/ünïcode/ß.txt": "unicode\n",
        "environment/x/__pycache__/m.cpython-312.pyc": "junk",
        "environment/x/__pycache__/keep.txt": "dropped with its directory",
        "environment/x/a.pyc": "junk",
        "environment/x/editor~": "junk",
        "environment/x/.DS_Store": "junk",
        "environment/x/swap.swp": "junk",
        "environment/x/dir~/inner.txt": "dropped: parent matches *~",
        "environment/x/__pycache__": None,
        "tests/test.sh": "#!/bin/bash\n",
        "tests/episode/grading/expected.duckdb": "binary-ish \x00\x01",
        "solution/solve.sh": "#!/bin/bash\n",
        "steps/one/instruction.md": "step\n",
        "Z-uppercase/ignored-dir.txt": "not packaged\n",
    }
    for rel, text in files.items():
        if text is None:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


def test_algorithm_by_hand(tmp_path):
    task = tmp_path / "t"
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text("x")
    (task / "environment" / "Dockerfile").write_text("y")
    expected = hashlib.sha256()
    for rel, data in (("environment/Dockerfile", b"y"), ("task.toml", b"x")):
        expected.update(f"{rel}\0{hashlib.sha256(data).hexdigest()}\n".encode())
    assert digest.task_digest(task) == "sha256:" + expected.hexdigest()


def test_default_ignores_and_unpublishable(tmp_path):
    task = _tricky_task(tmp_path / "t")
    rels = [rel for rel, _ in digest.collect_files(task)]
    assert "environment/x/a.pyc" not in rels
    assert "environment/x/__pycache__/keep.txt" not in rels
    assert "environment/x/dir~/inner.txt" not in rels
    assert "environment/episode/world/var/a b.json" in rels
    assert rels == sorted(rels)
    assert set(digest.unpublishable(task)) == {
        "notes.txt", "Z-uppercase/ignored-dir.txt", "environment/x/a.pyc",
        "environment/x/editor~", "environment/x/.DS_Store", "environment/x/swap.swp",
        "environment/x/dir~/inner.txt", "environment/x/__pycache__/m.cpython-312.pyc",
        "environment/x/__pycache__/keep.txt"}


def test_equal_to_harbor_packager(tmp_path):
    plain = _tricky_task(tmp_path / "plain")
    ignored = _tricky_task(tmp_path / "with-gitignore")
    (ignored / ".gitignore").write_text("*.json\n!trajectory.json\nsteps/\n")
    result = support.run_harbor(PACKAGER, [str(plain), str(ignored)])
    for task in (plain, ignored):
        theirs = result[str(task)]
        assert [rel for rel, _ in digest.collect_files(task)] == theirs["files"]
        assert digest.task_digest(task) == theirs["digest"]
    assert result[str(plain)]["digest"] != result[str(ignored)]["digest"]
