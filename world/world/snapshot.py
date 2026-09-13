"""Deterministic tar snapshots.

save(): sorted entries, zeroed mtime/uid/gid, fixed modes -> identical tar
bytes for identical world contents. restore(): byte-exact extraction.
Gates (agent.md §9.3 Track A): snapshot restore must reproduce the identical
raw byte hash; building from scratch twice must reproduce the identical
semantic fingerprint — the two are deliberately separate.
"""
import argparse
import io
import tarfile
from pathlib import Path

from .hashing import file_hash, tree_items

DB = Path("var/warehouse/warehouse.duckdb")
SCOPE = ("workspace", "var")  # episode state; build/ is host-specific metadata


def _state_items(root: Path):
    items = []
    for sub in SCOPE:
        items += [(f"{sub}/{rel}", data)
                  for rel, data in tree_items(Path(root) / sub)]
    # .git/index is a local stat cache (inode/device/mtime): never portable
    # across hosts or builds, and git rebuilds it on first use.
    items = [it for it in items if it[0] != "workspace/.git/index"]
    return sorted(items, key=lambda t: t[0])


def _state_hash(root: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    for rel, data in _state_items(root):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()


def save(root: Path, out: Path) -> Path:
    with tarfile.open(out, "w") as tf:
        for rel, data in _state_items(root):
            ti = tarfile.TarInfo(rel)
            ti.size = len(data)
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mode = 0o755 if rel.endswith(".sh") else 0o644
            tf.addfile(ti, io.BytesIO(data))
    return Path(out)


def restore(tar_path: Path, dest: Path) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as tf:
        tf.extractall(dest)
    return dest


def verify(root_a: Path, root_b: Path) -> bool:
    """Restore gate: episode-state tree and warehouse bytes must match exactly."""
    return (_state_hash(Path(root_a)) == _state_hash(Path(root_b))
            and file_hash(Path(root_a) / DB) == file_hash(Path(root_b) / DB))


def main(argv=None):
    p = argparse.ArgumentParser(prog="world.snapshot")
    p.add_argument("cmd", choices=["save", "restore", "verify"])
    p.add_argument("a", type=Path)
    p.add_argument("b", type=Path)
    a = p.parse_args(argv)
    if a.cmd == "save":
        print(save(a.a, a.b))
    elif a.cmd == "restore":
        print(restore(a.a, a.b))
    else:
        ok = verify(a.a, a.b)
        print("snapshot verify: PASS" if ok else "snapshot verify: FAIL")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
