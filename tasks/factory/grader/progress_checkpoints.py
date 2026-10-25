"""Seven continuous progress checkpoints for factory tasks (q0..q6).

Receipt hashes are whole-table, so one divergent night zeroes every later
night. These checkpoints read ROWS instead, so a partially corrupted night
earns partial credit and each later night is judged on its own rows.

Roles (raw / fact / handoff) and grain keys come from the spec, never from
table names: ``spec["progress"]`` may name them explicitly
(``raw_table``, ``fact_table``, ``handoff_table``, ``day_columns``);
otherwise raw is the replay target in schema ``raw`` (else the first target),
handoff is the ``closed_window_boundary`` probe's ``handoff_table`` (else the
last target) and fact is the remaining target.

Definitions (all in [0, 1], 1.0 for the oracle world):

* ``q0_raw_loaded``: |oracle raw grain keys present in the agent raw table| /
  |oracle raw grain keys| at t0. A missing table or grain column is 0.
* ``q1_t0_dedup_and_precedence``: keyed parity of the fact table at t0.
* ``q2_t0_handoff_parity``: keyed parity of the handoff table at t0.
  Keyed parity = keys whose agent row is unique and equal to the oracle row
  (projected onto the oracle's columns) / |oracle keys U agent keys|, so
  duplicate or extra keys cost credit.
* ``q3_r1_parity``..``q6_r4_parity``: mean over replay targets of a windowed
  multiset Jaccard |A n O| / |A u O|. For night n and table T the window is
  the set of day-column values on which either the oracle's or the agent's
  rows changed between night n-1 and night n (night 0 = t0). Rows outside the
  window are ignored, so an error made on night 2 is not charged again on
  night 3 unless night 3 touches those days. A table without a day column, or
  a night where nothing changed on either side, is compared whole.

Oracle per-night rows live in ``grading/oracle_night_rows.json`` (written at
materialization by :func:`capture_oracle_rows`). When absent, q3..q6 fall
back, in order, to:

1. a cached capture under ``$ALKERA_PROGRESS_CACHE`` (default
   ``$TMPDIR/alkera_progress_cache``), keyed by task id and spec hash;
2. a lazy capture that rebuilds the task's t0 world, applies its
   ``solution/solve.py`` and replays it against the episode's grading zone.
   It needs a task directory: the ``task_dir`` argument,
   ``$ALKERA_PROGRESS_TASK_DIR``, ``grading/..`` when grading is a task's
   ``tests/``, or ``$ALKERA_PROGRESS_TASKS_ROOT/<task_id>``. It is accepted
   only if its receipts reproduce ``oracle_receipts.json``;
3. a receipt approximation: per target, 1.0 on hash match, else
   0.5 * min/max row-count ratio, averaged over targets.

``basis.night_rows`` records which one was used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tasks.factory.grader import receipt_engine as receipts
    from tasks.factory.grader.replay_isolation import advance_nights
else:
    from . import receipt_engine as receipts
    from .replay_isolation import advance_nights

REPO = Path(__file__).resolve().parents[3]
ROWS_FILE = "oracle_night_rows.json"
PREFIX = "progress:"
NIGHT_KEYS = ("q3_r1_parity", "q4_r2_parity", "q5_r3_parity", "q6_r4_parity")
T0_KEYS = ("q0_raw_loaded", "q1_t0_dedup_and_precedence", "q2_t0_handoff_parity")
KEYS = T0_KEYS + NIGHT_KEYS
WEIGHTS = {"q0_raw_loaded": 0.10, "q1_t0_dedup_and_precedence": 0.15,
           "q2_t0_handoff_parity": 0.15, "q3_r1_parity": 0.15, "q4_r2_parity": 0.15,
           "q5_r3_parity": 0.15, "q6_r4_parity": 0.15}
DAY_NAME = re.compile(r"(^|_)(day|date)$")
CACHE_ENV, TASK_DIR_ENV, TASKS_ROOT_ENV = (
    "ALKERA_PROGRESS_CACHE", "ALKERA_PROGRESS_TASK_DIR", "ALKERA_PROGRESS_TASKS_ROOT")


def _key(row) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _unit(value: float) -> float:
    return round(min(1.0, max(0.0, float(value))), 6)


def _spec_sha(spec: dict) -> str:
    core = {k: spec.get(k) for k in ("t0", "replay", "task_id")}
    return hashlib.sha256(_key(core).encode()).hexdigest()[:16]


def _targets(spec: dict) -> list[str]:
    targets = list((spec.get("replay") or {}).get("targets") or [])
    if targets:
        return targets
    return [t["table"] for t in (spec.get("t0") or {}).get("tables") or []]


def _boundary_probes(spec: dict) -> list[dict]:
    found = []

    def visit(probe):
        if isinstance(probe, dict):
            if probe.get("kind") == "closed_window_boundary":
                found.append(probe)
            for child in probe.get("probes") or []:
                visit(child)

    for probe in (spec.get("checks") or {}).values():
        visit(probe)
    return found


def roles(spec: dict) -> dict:
    """{"raw": table, "fact": table, "handoff": table, "grain": {table: [...]}}."""
    declared = spec.get("progress") or {}
    t0_tables = (spec.get("t0") or {}).get("tables") or []
    grain = {t["table"]: list(t.get("grain_key") or []) for t in t0_tables}
    targets = _targets(spec)
    raw = declared.get("raw_table") or next(
        (t for t in targets if t.split(".")[0] == "raw"), targets[0] if targets else None)
    handoff = declared.get("handoff_table") or next(
        (p["handoff_table"] for p in _boundary_probes(spec)
         if p.get("handoff_table") in grain or p.get("handoff_table") in targets), None)
    if handoff is None:
        handoff = next((t for t in reversed(targets) if t != raw), None)
    fact = declared.get("fact_table") or next(
        (t for t in targets if t not in (raw, handoff)), None)
    return {"raw": raw, "fact": fact, "handoff": handoff, "grain": grain}


def _day_column(spec: dict, table: str, columns: list[str]) -> str | None:
    declared = ((spec.get("progress") or {}).get("day_columns") or {}).get(table)
    if declared in columns:
        return declared
    for probe in _boundary_probes(spec):
        if probe.get("day_column") in columns:
            return probe["day_column"]
    return next((c for c in columns if DAY_NAME.search(c)), None)


def snapshot_specs(spec: dict) -> dict:
    """Declarations merged into ``advance_nights(snapshots=...)``.

    Whole tables (no column list) so an agent's extra columns never break
    capture; rows are projected onto the oracle's columns when scored.
    """
    return {PREFIX + table: {"table": table} for table in _targets(spec)}


# ---------------------------------------------------------------- reading


def _read_table(world_root: Path, table: str) -> dict | None:
    try:
        return receipts.table_rows(Path(world_root) / "var/warehouse/warehouse.duckdb", table)
    except Exception:  # missing table / warehouse: scored as absent, never a crash
        return None


def _project(snap: dict | None, columns: list[str]) -> list[list] | None:
    """Rows of `snap` on `columns`; None when the agent lacks any of them."""
    if not snap or "rows" not in snap:
        return None
    have = snap.get("columns") or columns
    if any(c not in have for c in columns):
        return None
    index = [have.index(c) for c in columns]
    return [[row[i] for i in index] for row in snap["rows"]]


def _keyed_parity(oracle_rows: list[list], agent_rows: list[list] | None,
                  columns: list[str], grain: list[str]) -> dict:
    if agent_rows is None:
        return {"score": 0.0, "matched": 0, "keys": len(oracle_rows), "absent": True}
    if not grain or any(c not in columns for c in grain):
        return _jaccard(oracle_rows, agent_rows)
    index = [columns.index(c) for c in grain]
    want: dict[str, list] = {}
    for row in oracle_rows:
        want.setdefault(_key([row[i] for i in index]), []).append(_key(row))
    got: dict[str, list] = {}
    for row in agent_rows:
        got.setdefault(_key([row[i] for i in index]), []).append(_key(row))
    universe = set(want) | set(got)
    matched = sum(1 for k, rows in want.items()
                  if len(rows) == 1 and got.get(k) == rows)
    score = 1.0 if not universe else matched / len(universe)
    return {"score": _unit(score), "matched": matched, "keys": len(universe),
            "duplicate_keys": sum(1 for v in got.values() if len(v) > 1)}


def _jaccard(oracle_rows: list[list], agent_rows: list[list] | None) -> dict:
    if agent_rows is None:
        return {"score": 0.0, "inter": 0, "union": len(oracle_rows), "absent": True}
    o, a = Counter(map(_key, oracle_rows)), Counter(map(_key, agent_rows))
    inter, union = sum((o & a).values()), sum((o | a).values())
    return {"score": 1.0 if union == 0 else _unit(inter / union),
            "inter": inter, "union": union}


# ------------------------------------------------------------ oracle rows


def _oracle_t0(grading: Path, spec: dict) -> dict:
    path = Path(grading) / (spec.get("t0") or {}).get("oracle", "oracle_t0.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def write_oracle_rows(grading: Path, spec: dict, t0_world: Path, replayed: list[dict],
                      out: Path | None = None) -> Path:
    """Persist per-night oracle rows from an `advance_nights` result that was
    captured with :func:`snapshot_specs` (lets capture_oracle reuse its replay)."""
    tables = _targets(spec)
    t0 = {t: _read_table(t0_world, t) for t in tables}
    nights = []
    for night in replayed:
        snaps = night.get("snapshots") or {}
        missing = [t for t in tables if PREFIX + t not in snaps]
        if missing:
            raise ValueError(f"night {night.get('night')} lacks progress snapshots {missing}")
        nights.append({"night": night.get("night"),
                       "tables": {t: snaps[PREFIX + t] for t in tables}})
    doc = {"schema_version": 1, "task_id": spec.get("task_id"), "spec_sha": _spec_sha(spec),
           "targets": tables, "t0": t0, "nights": nights}
    path = Path(out) if out else Path(grading) / ROWS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")
    return path


def capture_oracle_rows(world_root: Path, grading: Path, spec: dict,
                        out: Path | None = None) -> Path:
    """Replay the ORACLE world on a copy and write ``grading/oracle_night_rows.json``."""
    replay = spec["replay"]
    replayed = advance_nights(world_root, grading, replay["nights"],
                              snapshot_specs(spec), replay.get("prepare"))
    return write_oracle_rows(grading, spec, world_root, replayed, out)


def _builder(task_dir: Path):
    text = (task_dir / "fixtures/make_fixture_world.py").read_text(encoding="utf-8")
    name = "peer_materializer" if "peer_materializer" in text else "task_materializer"
    module = __import__(f"tasks.factory.compiler.{name}", fromlist=["build_world_from_task"])
    return module.build_world_from_task


def capture_from_task(task_dir: Path, grading: Path, spec: dict, out: Path) -> dict:
    """Lazy capture for tasks materialized before oracle_night_rows.json existed.

    Rebuilds the task's t0 world, applies ``solution/solve.py`` and replays it
    against `grading` (whose planted future is what the episode saw). The
    rows are written only if the replay reproduces every oracle receipt.
    """
    task_dir, grading = Path(task_dir).resolve(), Path(grading).resolve()
    blueprint = json.loads((task_dir / "fixtures/blueprint.json").read_text(encoding="utf-8"))
    for path in (str(REPO), str(REPO / "world")):
        if path not in sys.path:
            sys.path.insert(0, path)
    with tempfile.TemporaryDirectory(prefix="progress-oracle-") as tmp:
        world, built = Path(tmp) / "world", Path(tmp) / "grading"
        _builder(task_dir)(task_dir, world, built, int(blueprint["seed"]))
        sealed = built / "factory_future"
        if not spec["replay"].get("prepare") and sealed.is_dir() \
                and not (world / "build/factory_future").exists():
            # pre-sealing tasks kept their arrivals inside the world
            shutil.copytree(sealed, world / "build/factory_future")
        process = subprocess.run([sys.executable, str(task_dir / "solution/solve.py"),
                                  str(world)], cwd=REPO, text=True, capture_output=True)
        if process.returncode:
            raise RuntimeError(f"oracle solution failed: {process.stderr[-500:]}")
        replay = spec["replay"]
        replayed = advance_nights(world, grading, replay["nights"], snapshot_specs(spec),
                                  replay.get("prepare"))
        verdicts = receipts.compare_nights(replayed, grading, replay)
        if not all(v["passed"] for v in verdicts):
            raise RuntimeError("lazy oracle replay does not reproduce oracle_receipts.json: "
                               + json.dumps([v["fraction"] for v in verdicts]))
        path = write_oracle_rows(grading, spec, world, replayed, out)
    return {"path": str(path), "verified_nights": len(verdicts)}


def _cache_path(spec: dict, cache_dir: Path | None) -> Path:
    root = cache_dir or os.environ.get(CACHE_ENV) or \
        Path(tempfile.gettempdir()) / "alkera_progress_cache"
    return Path(root) / f"{spec.get('task_id') or 'task'}-{_spec_sha(spec)}" / ROWS_FILE


def _find_task_dir(grading: Path, spec: dict, task_dir: Path | None) -> Path | None:
    candidates = [task_dir, os.environ.get(TASK_DIR_ENV)]
    if Path(grading).name == "tests":
        candidates.append(Path(grading).parent)
    if os.environ.get(TASKS_ROOT_ENV) and spec.get("task_id"):
        candidates.append(Path(os.environ[TASKS_ROOT_ENV]) / spec["task_id"])
    for candidate in candidates:
        if candidate and (Path(candidate) / "solution/solve.py").is_file() \
                and (Path(candidate) / "fixtures/blueprint.json").is_file():
            return Path(candidate)
    return None


def load_oracle_rows(grading: Path, spec: dict, *, task_dir: Path | None = None,
                     cache_dir: Path | None = None, lazy: bool = True) -> tuple[dict | None, str]:
    """(doc, basis) following the documented fallback chain."""
    direct = Path(grading) / ROWS_FILE
    if direct.is_file():
        return json.loads(direct.read_text(encoding="utf-8")), ROWS_FILE
    cached = _cache_path(spec, cache_dir)
    if cached.is_file():
        return json.loads(cached.read_text(encoding="utf-8")), f"cache:{cached}"
    source = _find_task_dir(grading, spec, task_dir) if lazy else None
    if source is not None:
        try:
            capture_from_task(source, grading, spec, cached)
            return json.loads(cached.read_text(encoding="utf-8")), f"lazy:{cached}"
        except Exception as exc:  # fall through to the receipt approximation
            return None, f"receipts_approx (lazy capture failed: {str(exc)[:200]})"
    return None, "receipts_approx"


# -------------------------------------------------------------- scoring


def _night_parity(spec: dict, oracle_doc: dict, oracle_t0: dict, agent_t0: dict,
                  nights: list[dict]) -> tuple[list[float], list[dict]]:
    tables = [t for t in oracle_doc.get("targets") or _targets(spec)]
    values, detail = [], []
    prev_o = {t: (oracle_doc.get("t0") or {}).get(t) or oracle_t0.get(t) for t in tables}
    prev_a = dict(agent_t0)
    for index, want in enumerate(oracle_doc.get("nights") or [], 1):
        night = nights[index - 1] if index - 1 < len(nights) else {}
        snaps = night.get("snapshots") or {}
        per_table = {}
        for table in tables:
            o_snap = (want.get("tables") or {}).get(table) or {"columns": [], "rows": []}
            columns = o_snap["columns"]
            o_rows = o_snap["rows"]
            a_snap = snaps.get(PREFIX + table)
            a_rows = _project(a_snap, columns)
            o_prev = _project(prev_o.get(table), columns) or []
            a_prev = _project(prev_a.get(table), columns) or []
            day = _day_column(spec, table, columns)
            window = None
            if day is not None:
                d = columns.index(day)
                window = {row[d] for row in _sym_diff(o_rows, o_prev)}
                if a_rows is not None:
                    window |= {row[d] for row in _sym_diff(a_rows, a_prev)}
                if window:
                    o_rows = [r for r in o_rows if r[d] in window]
                    a_rows = None if a_rows is None else [r for r in a_rows if r[d] in window]
                else:
                    window = None
            score = _jaccard(o_rows, a_rows)
            score["window"] = sorted(map(str, window)) if window else "whole_table"
            per_table[table] = score
            prev_o[table] = o_snap
            prev_a[table] = a_snap
        value = sum(s["score"] for s in per_table.values()) / max(1, len(per_table))
        values.append(_unit(value))
        detail.append({"night": index, "tables": per_table})
    return values, detail


def _sym_diff(rows_a: list[list], rows_b: list[list]) -> list[list]:
    a, b = Counter(map(_key, rows_a)), Counter(map(_key, rows_b))
    keys = set((a - b) | (b - a))
    seen = {}
    for row in list(rows_a) + list(rows_b):
        seen.setdefault(_key(row), row)
    return [seen[k] for k in keys]


def _receipt_approx(grading: Path, spec: dict, nights: list[dict]) -> tuple[list[float], list]:
    replay = spec.get("replay") or {}
    path = Path(grading) / replay.get("oracle_receipts", "oracle_receipts.json")
    if not path.is_file() or not nights:
        return [0.0] * len(nights or []), []
    data = json.loads(path.read_text(encoding="utf-8"))
    expected = data["nights"] if isinstance(data, dict) else data
    targets = _targets(spec)
    values, detail = [], []
    for index, night in enumerate(nights, 1):
        want = expected[index - 1] if index - 1 < len(expected) else []
        want = want.get("receipts", want) if isinstance(want, dict) else want
        last_o = {r.get("target"): r for r in want}
        last_a = {r.get("target"): r for r in night.get("receipts") or []}
        per = {}
        for table in targets:
            o, a = last_o.get(table), last_a.get(table)
            if o is None:
                continue
            if a is None:
                per[table] = 0.0
            elif a.get("canonical_hash") == o.get("canonical_hash"):
                per[table] = 1.0
            else:
                ro, ra = int(o.get("row_count") or 0), int(a.get("row_count") or 0)
                per[table] = 0.5 * (min(ro, ra) / max(ro, ra) if max(ro, ra) else 0.0)
        values.append(_unit(sum(per.values()) / len(per)) if per else 0.0)
        detail.append({"night": index, "tables": per})
    return values, detail


def checkpoints(*, world_root, grading, spec, nights: list[dict] | None,
                t0: dict | None = None, task_dir: Path | None = None,
                cache_dir: Path | None = None, lazy: bool = True) -> dict:
    """The seven checkpoints plus a ``basis`` record of how each was measured."""
    world_root, grading = Path(world_root), Path(grading)
    role = roles(spec)
    oracle_t0 = _oracle_t0(grading, spec)
    agent_t0 = {t: _read_table(world_root, t)
                for t in set(_targets(spec)) | {role["raw"], role["fact"], role["handoff"]}
                if t}
    out: dict = {}
    basis: dict = {"roles": {k: role[k] for k in ("raw", "fact", "handoff")},
                   "t0_c0_passed": (t0 or {}).get("passed")}

    raw = role["raw"]
    want = oracle_t0.get(raw) or {"columns": [], "rows": []}
    grain = role["grain"].get(raw) or []
    got = _project(agent_t0.get(raw), grain) if grain else None
    if grain and all(c in want["columns"] for c in grain):
        idx = [want["columns"].index(c) for c in grain]
        expected = {_key([r[i] for i in idx]) for r in want["rows"]}
        present = {_key(r) for r in got} if got is not None else set()
        out["q0_raw_loaded"] = 1.0 if not expected else _unit(len(expected & present) / len(expected))
        basis["q0"] = {"table": raw, "grain": grain, "expected": len(expected),
                       "present": len(expected & present), "absent": got is None}
    else:
        rows = _project(agent_t0.get(raw), want["columns"])
        score = _jaccard(want["rows"], rows)
        out["q0_raw_loaded"] = score["score"]
        basis["q0"] = dict(score, table=raw)

    for key, name in (("q1_t0_dedup_and_precedence", "fact"), ("q2_t0_handoff_parity", "handoff")):
        table = role[name]
        want = oracle_t0.get(table) or {"columns": [], "rows": []}
        rows = _project(agent_t0.get(table), want["columns"])
        score = _keyed_parity(want["rows"], rows, want["columns"], role["grain"].get(table) or [])
        out[key] = score["score"]
        basis[key[:2]] = dict(score, table=table)

    nights = list(nights or [])
    have_snaps = bool(nights) and all(
        PREFIX + t in (n.get("snapshots") or {}) for n in nights for t in _targets(spec))
    doc, source = (None, "no_replay") if not nights else (
        load_oracle_rows(grading, spec, task_dir=task_dir, cache_dir=cache_dir, lazy=lazy)
        if have_snaps else (None, "receipts_approx (nights lack progress snapshots)"))
    if doc is not None and len(doc.get("nights") or []) == len(nights):
        values, detail = _night_parity(spec, doc, oracle_t0, agent_t0, nights)
    else:
        if doc is not None:
            source = f"receipts_approx (oracle rows have {len(doc.get('nights') or [])} nights)"
        values, detail = _receipt_approx(grading, spec, nights)
    span = len((spec.get("replay") or {}).get("nights") or [])
    for i, key in enumerate(NIGHT_KEYS):
        out[key] = values[i] if i < len(values) else 0.0 if i < span else None
    # A role the spec does not resolve, or one the oracle never captured, is
    # unmeasured rather than zero; so is a night without row-level evidence.
    # attach() skips None, which leaves the caller's receipt-level value.
    unmeasured = {"q0_raw_loaded": raw,
                  "q1_t0_dedup_and_precedence": role["fact"],
                  "q2_t0_handoff_parity": role["handoff"]}
    for key, table in unmeasured.items():
        if not table or table not in oracle_t0:
            out[key] = None
    if str(source).startswith("receipts_approx"):
        for key in NIGHT_KEYS:
            out[key] = None
    basis["night_rows"] = source
    basis["nights"] = detail
    out["basis"] = basis
    return out


def attach(result: dict, *, world_root, grading, spec, nights, t0, **kwargs) -> dict:
    """Add the 7 keys to progress_quantities and copy them into check details."""
    values = checkpoints(world_root=world_root, grading=grading, spec=spec,
                         nights=nights, t0=t0, **kwargs)
    progress = dict(result.get("progress_quantities") or {})
    progress.update({k: values[k] for k in KEYS if values[k] is not None})
    result["progress_quantities"] = progress
    result["progress_basis"] = values["basis"]
    owners = {k: "C0" for k in T0_KEYS}
    owners.update({k: f"R{i}" for i, k in enumerate(NIGHT_KEYS, 1)})
    for check in result.get("checks") or []:
        for key, owner in owners.items():
            if values[key] is None:
                continue
            if check.get("id") == owner and isinstance(check.get("detail"), dict):
                check["detail"][key] = values[key]
    return result


def recommended_progress_block() -> dict:
    """``[matrix.progress]`` for a materializer: 7 quantities, weights sum to 1."""
    owners = {k: "C0" for k in T0_KEYS}
    owners.update({k: f"R{i}" for i, k in enumerate(NIGHT_KEYS, 1)})
    block = {k: {"check": owners[k], "field": k, "noop": "min", "oracle": "max"} for k in KEYS}
    block["weights"] = dict(WEIGHTS)
    return block


# ------------------------------------------------------------------- CLI


def _scalar(block: dict, checks: dict, baselines: dict | None) -> tuple[float | None, str]:
    """progress.py scalar; anchors from baselines when they carry the fields,
    else the definitional anchors noop=0 / oracle=1."""
    from tasks.harness.progress import (anchors_from_baselines, episode_progress,
                                        parse_progress_spec)
    spec = parse_progress_spec(block, None)
    try:
        anchors, source = anchors_from_baselines(spec, baselines or {}), "baselines"
    except ValueError:
        anchors = {name: {"noop": 0.0, "oracle": 1.0} for name in spec.quantities}
        source = "unit"
    return episode_progress(spec, checks, anchors), source


def regrade(rollout_dir: Path, *, task_dir: Path | None = None,
            cache_dir: Path | None = None) -> dict:
    rollout_dir = Path(rollout_dir).resolve()
    record = json.loads((rollout_dir / "rollout.json").read_text(encoding="utf-8"))
    grading, world = rollout_dir / "grading", rollout_dir / "world"
    spec = json.loads((grading / "spec.json").read_text(encoding="utf-8"))
    task_id = record.get("task") or spec.get("task_id")
    if task_dir is None and record.get("tasks_dir"):
        task_dir = Path(record["tasks_dir"]) / task_id
    replay = spec["replay"]
    present = {k: v for k, v in snapshot_specs(spec).items()
               if _read_table(world, v["table"]) is not None}
    t0 = receipts.compare_t0(world, grading, spec["t0"])
    try:
        nights = advance_nights(world, grading, replay["nights"], present, replay.get("prepare"))
        error = None
    except Exception as exc:
        nights, error = [], str(exc)[:300]
    checks = {c["id"]: dict(c) for c in (record.get("score") or {}).get("checks") or []}
    if not checks:
        checks = {k: dict(v, id=k) for k, v in (record.get("checks") or {}).items()}
    result = {"checks": [dict(c, detail=dict(c.get("detail") or {})) for c in checks.values()],
              "progress_quantities": {}}
    attach(result, world_root=world, grading=grading, spec=spec, nights=nights, t0=t0,
           task_dir=task_dir, cache_dir=cache_dir)
    by_id = {c["id"]: c for c in result["checks"]}
    baselines = None
    if task_dir and (Path(task_dir) / "tests/baselines/default.json").is_file():
        baselines = json.loads((Path(task_dir) / "tests/baselines/default.json").read_text())
    scalar, anchors = _scalar(recommended_progress_block(), by_id, baselines)
    old = None
    toml_path = Path(task_dir) / "task.toml" if task_dir else None
    if toml_path and toml_path.is_file() and baselines:
        block = (tomllib.loads(toml_path.read_text()).get("matrix") or {}).get("progress")
        if block:
            from tasks.harness.progress import (anchors_from_baselines, episode_progress,
                                                parse_progress_spec)
            parsed = parse_progress_spec(block, None)
            old = episode_progress(parsed, {k: v for k, v in checks.items()},
                                   anchors_from_baselines(parsed, baselines))
    return {"rollout": str(rollout_dir), "task": task_id,
            "passed": record.get("passed"), "classification": record.get("classification"),
            "checkpoints": {k: result["progress_quantities"][k] for k in KEYS},
            "scalar": scalar, "anchors": anchors, "old_scalar": old,
            "basis": result["progress_basis"]["night_rows"], "replay_error": error}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rg = sub.add_parser("regrade", help="print the 7 checkpoints for a recorded rollout")
    rg.add_argument("rollout_dir", type=Path)
    rg.add_argument("--task-dir", type=Path)
    rg.add_argument("--cache-dir", type=Path)
    rg.add_argument("--json", action="store_true")
    cap = sub.add_parser("capture", help="write oracle_night_rows.json for a task dir")
    cap.add_argument("task_dir", type=Path)
    cap.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.command == "capture":
        grading = args.task_dir / "tests"
        spec = json.loads((grading / "spec.json").read_text(encoding="utf-8"))
        print(json.dumps(capture_from_task(args.task_dir, grading, spec,
                                           args.out or grading / ROWS_FILE)))
        return 0
    row = regrade(args.rollout_dir, task_dir=args.task_dir, cache_dir=args.cache_dir)
    if args.json:
        print(json.dumps(row, indent=2, sort_keys=True))
    else:
        print(" ".join(f"{k}={v:.4f}" for k, v in row["checkpoints"].items())
              + f" scalar={row['scalar']} basis={row['basis']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
