"""Advance a copied world, never the episode world being graded."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
LEDGER = Path("var/ledger/receipts.jsonl")
STATE = Path("var/sched/state.json")


class ReplayError(ValueError):
    pass


def _inside(base: Path, relative: str) -> Path:
    """Resolve a declared path without allowing it to escape its plane."""
    base = base.resolve()
    dest = (base / relative).resolve()
    if not dest.is_relative_to(base):
        raise ReplayError(f"path escapes declared plane: {relative!r}")
    return dest


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except (ValueError, TypeError) as exc:
        raise ReplayError(f"unreadable receipt ledger {path}: {exc}") from exc


def query_rows(world_root: Path, sql: str) -> list[list]:
    """Read a declared SQL query from a world through aliased, read-only ATTACH."""
    import duckdb

    db = Path(world_root) / "var/warehouse/warehouse.duckdb"
    if not db.is_file():
        raise ReplayError(f"missing warehouse {db}")
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute(f"ATTACH '{db.as_posix()}' AS wh (READ_ONLY)")
        out = []
        for row in con.execute(sql).fetchall():
            out.append([v.isoformat() if isinstance(v, (date, datetime)) else
                        str(v) if isinstance(v, Decimal) else v for v in row])
        return out
    finally:
        con.close()


def _land(grading: Path, root: Path, entries: list[dict], variables: dict) -> None:
    for entry in entries:
        source_name = str(entry["source"]).format(**variables)
        destination_name = str(entry["destination"]).format(**variables)
        source = _inside(grading, source_name)
        destination = _inside(root, destination_name)
        if not source.exists():
            raise ReplayError(f"missing planted arrival: {source}")
        if entry.get("replace") and destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _capture(root: Path, declaration: dict) -> dict:
    if "sql" in declaration:
        return {"rows": query_rows(root, declaration["sql"])}
    from .receipt_engine import table_rows
    return table_rows(root / "var/warehouse/warehouse.duckdb",
                      declaration["table"], declaration.get("columns"))


def advance_nights(world_root: Path, grading: Path, nights: list[dict],
                   snapshots: dict[str, dict] | None = None,
                   prepare: list[dict] | None = None, *,
                   optional_snapshots: dict[str, dict] | None = None) -> list[dict]:
    """Return per-night receipts, runs and state from an isolated copy.

    `nights` entries accept `advance_hours`, `date`, and `land` with paths
    relative to the grading zone and world root. A scheduler that merely moves
    its clock is reported as an error, so it cannot earn receipt credit.
    `optional_snapshots` are captured like `snapshots`, except that a failed
    capture is recorded as `{"error": ...}` instead of failing the replay.
    """
    world_root, grading = Path(world_root).resolve(), Path(grading).resolve()
    if not (world_root / STATE).is_file():
        raise ReplayError(f"missing scheduler state in {world_root}")
    result = []
    with tempfile.TemporaryDirectory(prefix="declarative-replay-") as tmp:
        root = Path(tmp) / "world"
        shutil.copytree(world_root, root, ignore=shutil.ignore_patterns(".git", "*.wal"))
        (root / "var/warehouse/warehouse.duckdb.wal").unlink(missing_ok=True)
        # Plant grading-zone files onto the copy before any night runs, so a
        # platform daemon restored from the zone cannot write through to t0.
        _land(grading, root, prepare or [], {"night": 0, "date": ""})
        for index, night in enumerate(nights, 1):
            variables = {"night": index, "date": night.get("date", "")}
            _land(grading, root, night.get("land") or [], variables)
            receipts_before = _jsonl(root / LEDGER)
            state_before = json.loads((root / STATE).read_text(encoding="utf-8"))
            hours = float(night.get("advance_hours", 24))
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO / "world") + os.pathsep + env.get("PYTHONPATH", "")
            process = subprocess.run(
                [sys.executable, "-m", "world.scheduler", str(root), "advance", str(hours)],
                env=env, capture_output=True, text=True, timeout=600)
            if process.returncode:
                tail = (process.stderr or process.stdout).strip().splitlines()
                raise ReplayError(f"night {index} scheduler failed: {tail[-1] if tail else process.returncode}")
            receipts_after = _jsonl(root / LEDGER)
            state_after = json.loads((root / STATE).read_text(encoding="utf-8"))
            if len(receipts_after) < len(receipts_before):
                raise ReplayError(f"night {index} truncated the publication ledger")
            runs = state_after.get("runs", [])[len(state_before.get("runs", [])):]
            captured = {}
            for name, declaration in (snapshots or {}).items():
                captured[name] = _capture(root, declaration)
            for name, declaration in (optional_snapshots or {}).items():
                if name in captured:
                    continue
                try:
                    captured[name] = _capture(root, declaration)
                except Exception as exc:                           # noqa: BLE001
                    captured[name] = {"error": f"{type(exc).__name__}: {exc}"}
            result.append({"night": index, "receipts": receipts_after[len(receipts_before):],
                           "runs": runs, "vnow": state_after.get("vnow"),
                           "snapshots": captured})
    return result
