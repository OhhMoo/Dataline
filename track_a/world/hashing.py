"""Canonical hashing helpers shared by fingerprint and snapshot.

Canonicalization rules (the semantic-fingerprint spec):
- JSON: sort_keys, tight separators, ensure_ascii.
- Files: hashed as (relative POSIX path, raw bytes) pairs, sorted by path.
- SQL values: None->null, int/bool/str as-is, float->repr, Decimal->str,
  date/datetime->ISO 8601, bytes->hex. Rows serialized as JSON arrays.
"""
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def file_hash(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def json_canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def json_hash(obj) -> str:
    return sha256_bytes(json_canon(obj))


def canon_value(v):
    if v is None or isinstance(v, (int, str, bool)):
        return v
    if isinstance(v, float):
        return repr(v)  # shortest round-trip repr: exact and stable
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    raise TypeError(f"uncanonicalizable value: {type(v)!r}")


def tree_items(root: Path, exclude=()):
    """Sorted (relpath, bytes) for every file under root, skipping exclude dirs."""
    root = Path(root)
    out = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if any(part in exclude for part in rel.parts):
            continue
        if p.is_file():
            out.append((rel.as_posix(), p.read_bytes()))
    out.sort(key=lambda t: t[0])
    return out


def tree_hash(root: Path, exclude=()) -> str:
    h = hashlib.sha256()
    for rel, data in tree_items(root, exclude):
        h.update(rel.encode())
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()
