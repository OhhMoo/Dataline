"""Deterministic 51-task Stratified Hardness Proof Selector & Materializer CLI.

Selects 51 unique blueprints from ``catalog_1080.jsonl``:
  - Part A (36 tasks): 12 themes x 3 governance modes on canonical ``FT01``
    (``schedule_seed == "a"``), cycling ``governance_submode`` across the 3
    withhold blocker submodes (4 each across the 12 ``withhold-blocked`` tasks).
  - Part B (15 tasks): 15 topology representatives covering ``FT01`` (with
    ``schedule_seed == "b"``) and ``FT02..FT15``, distributed evenly across the
    12 themes and 3 governance modes (5 ``act-authorized``, 5 ``withhold-blocked``,
    5 ``escalate-ambiguous``).

Together, the 51 selected blueprints cover all 12 themes, all 3 governance modes,
all 15 fault topologies ``FT01..FT15``, all 10 t0 stressors ``S01..S10``, and
all 14 future-night hazards ``H01..H14``.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tasks.factory.compiler.blueprint_matrix import (
    DEFAULT_CATALOG_PATH,
    FAULT_TOPOLOGIES,
    GOVERNANCE_MODES,
    R_HAZARDS,
    T0_STRESSORS,
    THEMES,
    WITHHOLD_SUBMODES,
)
from tasks.factory.compiler.phase2_readiness import inspect_phase2_readiness
from tasks.factory.compiler.peer_materializer import materialize

REPO = Path(__file__).resolve().parents[3]
DEFAULT_OUT = Path("/home/ubuntu/alkera_factory_1017/phase2_pilot_51")
DEFAULT_SCRATCH = Path("/home/ubuntu/alkera_factory_1017/phase2_pilot_scratch")
DEFAULT_REPORT = Path("/home/ubuntu/alkera_factory_1017/phase2_pilot_51_report.json")

MODES: tuple[str, ...] = (
    "act-authorized",
    "withhold-blocked",
    "escalate-ambiguous",
)
EXPECTED_STRESSOR_CODES: tuple[str, ...] = tuple(
    s_id.split("_", 1)[0] for s_id in T0_STRESSORS
)
EXPECTED_HAZARD_CODES: tuple[str, ...] = tuple(
    h_id.split("_", 1)[0] for h_id in R_HAZARDS
)


def _schedule_seed_short(row: dict[str, Any]) -> str:
    if row.get("schedule_seed") in ("a", "b"):
        return str(row["schedule_seed"])
    seed_id = str(row.get("schedule_seed_id", ""))
    if seed_id == "H4_seedA" or str(row.get("task_id", "")).endswith("-a"):
        return "a"
    if seed_id == "H4_seedB" or str(row.get("task_id", "")).endswith("-b"):
        return "b"
    raise ValueError(f"Cannot determine schedule_seed for task {row.get('task_id')!r}")


def _extract_stressor_codes(row: dict[str, Any]) -> list[str]:
    codes = row.get("t0_stressor_codes")
    if codes:
        return [str(c).split("_", 1)[0] for c in codes]
    return [str(s).split("_", 1)[0] for s in row.get("t0_stressors", [])]


def _extract_hazard_codes(row: dict[str, Any]) -> list[str]:
    codes = row.get("hazards")
    if codes:
        return [str(c).split("_", 1)[0] for c in codes]
    result: list[str] = []
    for item in row.get("r_hazards", []):
        h_id = item.get("hazard_id") if isinstance(item, dict) else str(item)
        result.append(str(h_id).split("_", 1)[0])
    return result


def _load_catalog_rows(catalog_path: Path) -> list[dict[str, Any]]:
    path = Path(catalog_path)
    if not path.is_file():
        raise FileNotFoundError(f"Catalog file not found: {path}")
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"Catalog line {line_no} is not a JSON object")
        rows.append(row)
    return rows


def select_stratified_51(catalog_path: Path = DEFAULT_CATALOG_PATH) -> list[dict[str, Any]]:
    """Deterministically select 51 stratified blueprints (36 Part A + 15 Part B)."""
    catalog_rows = _load_catalog_rows(Path(catalog_path))

    by_cell: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in catalog_rows:
        theme = row["theme"]
        topology_id = row.get("topology_id") or row.get("fault_topology_id")
        mode = row["governance_mode"]
        seed_short = _schedule_seed_short(row)
        cell_key = (theme, str(topology_id), mode, seed_short)
        if cell_key not in by_cell:
            by_cell[cell_key] = row

    themes = list(THEMES.keys())
    topologies = list(FAULT_TOPOLOGIES.keys())
    selected: list[dict[str, Any]] = []
    seen_task_ids: set[str] = set()

    # Part A: 36 Theme x Governance Twins (12 themes x 3 governance modes on FT01, seed "a")
    for theme_idx, theme in enumerate(themes):
        expected_submode = WITHHOLD_SUBMODES[theme_idx % len(WITHHOLD_SUBMODES)]
        for mode in MODES:
            cell_key = (theme, "FT01", mode, "a")
            if cell_key not in by_cell:
                raise ValueError(f"Missing Part A cell in catalog: {cell_key}")
            entry = dict(by_cell[cell_key])
            entry["topology_id"] = entry.get("topology_id") or entry["fault_topology_id"]
            entry["schedule_seed"] = "a"
            if mode == "withhold-blocked":
                entry["governance_submode"] = expected_submode
            entry["selection_reason"] = "theme_governance_grid"
            seen_task_ids.add(entry["task_id"])
            selected.append(entry)

    # Part B: 15 Fault/Hazard Topology Representatives (FT01..FT15)
    # Distributed evenly across the 12 themes and 3 governance modes (5 each).
    # FT01 uses schedule_seed == "b" so it is distinct from Part A's schedule_seed == "a".
    for ft_idx, ft_id in enumerate(topologies):
        theme = themes[ft_idx % len(themes)]
        mode = MODES[ft_idx % len(MODES)]
        seed_short = "b" if ft_id == "FT01" else "a"
        cell_key = (theme, ft_id, mode, seed_short)
        if cell_key not in by_cell or by_cell[cell_key]["task_id"] in seen_task_ids:
            alt_seed = "b" if seed_short == "a" else "a"
            cell_key = (theme, ft_id, mode, alt_seed)
        if cell_key not in by_cell:
            raise ValueError(f"Missing Part B cell in catalog: {cell_key}")
        entry = dict(by_cell[cell_key])
        if entry["task_id"] in seen_task_ids:
            raise ValueError(f"Duplicate task_id selected in Part B: {entry['task_id']}")
        entry["topology_id"] = entry.get("topology_id") or entry["fault_topology_id"]
        entry["schedule_seed"] = _schedule_seed_short(entry)
        entry["selection_reason"] = f"topology_coverage_{ft_id}"
        seen_task_ids.add(entry["task_id"])
        selected.append(entry)

    _verify_selection(selected)
    return selected


def _verify_selection(rows: list[dict[str, Any]]) -> None:
    """Validate all structural and coverage invariants on the 51-task selection."""
    if len(rows) != 51:
        raise ValueError(f"Expected 51 selected blueprints, got {len(rows)}")
    task_ids = [r["task_id"] for r in rows]
    if len(set(task_ids)) != 51:
        raise ValueError(f"Expected 51 unique task_ids, got {len(set(task_ids))}")

    part_a = [r for r in rows if r.get("selection_reason") == "theme_governance_grid"]
    part_b = [
        r for r in rows
        if str(r.get("selection_reason", "")).startswith("topology_coverage_")
    ]
    if len(part_a) != 36:
        raise ValueError(f"Expected 36 Part A blueprints, got {len(part_a)}")
    if len(part_b) != 15:
        raise ValueError(f"Expected 15 Part B blueprints, got {len(part_b)}")

    # Part A checks
    part_a_pairs = {(r["theme"], r["governance_mode"]) for r in part_a}
    if len(part_a_pairs) != 36:
        raise ValueError(f"Expected 36 unique (theme, governance_mode) pairs in Part A, got {len(part_a_pairs)}")
    if not all((r.get("topology_id") or r.get("fault_topology_id")) == "FT01" for r in part_a):
        raise ValueError("All Part A tasks must use canonical topology FT01")
    if not all(_schedule_seed_short(r) == "a" for r in part_a):
        raise ValueError("All Part A tasks must use schedule_seed == 'a'")

    part_a_withhold = [r for r in part_a if r["governance_mode"] == "withhold-blocked"]
    submode_counts = Counter(r["governance_submode"] for r in part_a_withhold)
    if set(submode_counts.keys()) != set(WITHHOLD_SUBMODES) or set(submode_counts.values()) != {4}:
        raise ValueError(
            f"Part A withhold-blocked submodes must have 4 of each {WITHHOLD_SUBMODES}, got {dict(submode_counts)}"
        )

    # Part B checks
    part_b_topologies = [r.get("topology_id") or r.get("fault_topology_id") for r in part_b]
    if set(part_b_topologies) != set(FAULT_TOPOLOGIES.keys()) or len(part_b_topologies) != 15:
        raise ValueError(f"Part B must cover FT01..FT15 once each, got {part_b_topologies}")
    ft01_part_b = [r for r in part_b if (r.get("topology_id") or r.get("fault_topology_id")) == "FT01"]
    if len(ft01_part_b) != 1 or _schedule_seed_short(ft01_part_b[0]) != "b":
        raise ValueError("Part B FT01 representative must have schedule_seed == 'b'")
    part_b_modes = Counter(r["governance_mode"] for r in part_b)
    if part_b_modes != {m: 5 for m in MODES}:
        raise ValueError(f"Part B must have 5 of each governance mode, got {dict(part_b_modes)}")
    if len({r["theme"] for r in part_b}) != 12:
        raise ValueError("Part B must distribute across all 12 themes")

    # Global coverage checks across all 51 blueprints
    if len({r["theme"] for r in rows}) != 12:
        raise ValueError("Selection does not cover all 12 themes")
    if len({r["governance_mode"] for r in rows}) != 3:
        raise ValueError("Selection does not cover all 3 governance modes")
    if len({r.get("topology_id") or r.get("fault_topology_id") for r in rows}) != 15:
        raise ValueError("Selection does not cover all 15 fault topologies FT01..FT15")

    stressor_codes = {code for r in rows for code in _extract_stressor_codes(r)}
    missing_stressors = set(EXPECTED_STRESSOR_CODES) - stressor_codes
    if missing_stressors:
        raise ValueError(f"Selection missing t0 stressors: {sorted(missing_stressors)}")

    hazard_codes = {code for r in rows for code in _extract_hazard_codes(r)}
    missing_hazards = set(EXPECTED_HAZARD_CODES) - hazard_codes
    if missing_hazards:
        raise ValueError(f"Selection missing r_hazards: {sorted(missing_hazards)}")


def summarize_selection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return selection counts by theme, governance_mode, topology_id, stressor, and hazard."""
    theme_counts: Counter[str] = Counter()
    mode_counts: Counter[str] = Counter()
    submode_counts: Counter[str] = Counter()
    topology_counts: Counter[str] = Counter()
    stressor_counts: Counter[str] = Counter()
    hazard_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()

    part_a_withhold_submodes: Counter[str] = Counter()
    part_b_modes: Counter[str] = Counter()

    for row in rows:
        theme = row["theme"]
        mode = row["governance_mode"]
        submode = row.get("governance_submode", "")
        topology_id = str(row.get("topology_id") or row.get("fault_topology_id"))
        reason = str(row.get("selection_reason", ""))

        theme_counts[theme] += 1
        mode_counts[mode] += 1
        if submode:
            submode_counts[submode] += 1
        topology_counts[topology_id] += 1
        if reason:
            reason_counts[reason] += 1

        if reason == "theme_governance_grid" and mode == "withhold-blocked" and submode:
            part_a_withhold_submodes[submode] += 1
        elif reason.startswith("topology_coverage_"):
            part_b_modes[mode] += 1

        for s_code in _extract_stressor_codes(row):
            stressor_counts[s_code] += 1
        for h_code in _extract_hazard_codes(row):
            hazard_counts[h_code] += 1

    by_theme = {t: theme_counts[t] for t in THEMES if t in theme_counts}
    for t, c in sorted(theme_counts.items()):
        by_theme.setdefault(t, c)

    by_mode = {m: mode_counts[m] for m in MODES if m in mode_counts}
    for m, c in sorted(mode_counts.items()):
        by_mode.setdefault(m, c)

    by_topology = {ft: topology_counts[ft] for ft in FAULT_TOPOLOGIES if ft in topology_counts}
    for ft, c in sorted(topology_counts.items()):
        by_topology.setdefault(ft, c)

    by_stressor = {s: stressor_counts[s] for s in EXPECTED_STRESSOR_CODES if s in stressor_counts}
    for s, c in sorted(stressor_counts.items()):
        by_stressor.setdefault(s, c)

    by_hazard = {h: hazard_counts[h] for h in EXPECTED_HAZARD_CODES if h in hazard_counts}
    for h, c in sorted(hazard_counts.items()):
        by_hazard.setdefault(h, c)

    by_submode = dict(sorted(submode_counts.items()))
    by_reason = dict(sorted(reason_counts.items()))

    return {
        "total": len(rows),
        "count": len(rows),
        "unique_task_ids": len({r["task_id"] for r in rows}),
        "part_a_count": sum(1 for r in rows if r.get("selection_reason") == "theme_governance_grid"),
        "part_b_count": sum(
            1 for r in rows if str(r.get("selection_reason", "")).startswith("topology_coverage_")
        ),
        "theme": by_theme,
        "by_theme": by_theme,
        "theme_counts": by_theme,
        "governance_mode": by_mode,
        "by_governance_mode": by_mode,
        "governance_mode_counts": by_mode,
        "governance_submode": by_submode,
        "by_governance_submode": by_submode,
        "governance_submode_counts": by_submode,
        "part_a_withhold_submodes": {
            sm: part_a_withhold_submodes[sm] for sm in WITHHOLD_SUBMODES if sm in part_a_withhold_submodes
        },
        "part_b_governance_modes": {
            m: part_b_modes[m] for m in MODES if m in part_b_modes
        },
        "topology_id": by_topology,
        "by_topology_id": by_topology,
        "topology_counts": by_topology,
        "stressor": by_stressor,
        "by_stressor": by_stressor,
        "stressor_counts": by_stressor,
        "hazard": by_hazard,
        "by_hazard": by_hazard,
        "hazard_counts": by_hazard,
        "selection_reason": by_reason,
        "by_selection_reason": by_reason,
    }


def _one(row: dict[str, Any], out: Path, scratch: Path, overwrite: bool) -> dict[str, Any]:
    task = out / row["task_id"]
    topology_id = row.get("topology_id") or row.get("fault_topology_id")
    if (task / "tests/materializer_report.json").is_file() and not overwrite:
        return {
            "task_id": row["task_id"],
            "theme": row["theme"],
            "mode": row["governance_mode"],
            "topology_id": topology_id,
            "selection_reason": row.get("selection_reason"),
            "status": "existing",
            "path": str(task),
        }
    try:
        path = materialize(row, out, scratch=scratch, overwrite=True)
        return {
            "task_id": row["task_id"],
            "theme": row["theme"],
            "mode": row["governance_mode"],
            "topology_id": topology_id,
            "selection_reason": row.get("selection_reason"),
            "status": "materialized",
            "path": str(path),
        }
    except (Exception, SystemExit) as exc:
        return {
            "task_id": row["task_id"],
            "theme": row["theme"],
            "mode": row["governance_mode"],
            "topology_id": topology_id,
            "selection_reason": row.get("selection_reason"),
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}"[-1800:],
        }


def _write_manifest(
    manifest_path: Path,
    *,
    catalog_path: Path,
    all_rows: list[dict[str, Any]],
    active_rows: list[dict[str, Any]],
    summary: dict[str, Any],
    only_topologies: bool,
) -> None:
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "catalog_path": str(Path(catalog_path).resolve()),
        "total_selected": len(all_rows),
        "active_count": len(active_rows),
        "only_topologies": only_topologies,
        "summary": summary,
        "task_ids": [r["task_id"] for r in active_rows],
        "blueprints": active_rows,
    }
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--manifest-out", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-phase2-ready", action="store_true")
    parser.add_argument("--only-topologies", action="store_true")
    args = parser.parse_args(argv)

    if args.workers < 1:
        parser.error("--workers must be positive")

    all_rows = select_stratified_51(args.catalog)
    active_rows = (
        [r for r in all_rows if str(r.get("selection_reason", "")).startswith("topology_coverage_")]
        if args.only_topologies
        else all_rows
    )
    summary = summarize_selection(active_rows)

    if args.manifest_out is not None:
        _write_manifest(
            args.manifest_out,
            catalog_path=args.catalog,
            all_rows=all_rows,
            active_rows=active_rows,
            summary=summary,
            only_topologies=args.only_topologies,
        )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "count": len(active_rows),
                    "total_selected": len(all_rows),
                    "only_topologies": args.only_topologies,
                    "manifest_out": str(args.manifest_out) if args.manifest_out else None,
                    "summary": summary,
                    "task_ids": [r["task_id"] for r in active_rows],
                },
                indent=2,
            )
        )
        return 0

    if args.require_phase2_ready:
        readiness = inspect_phase2_readiness(REPO)
        if not readiness.get("ready"):
            print(
                json.dumps(
                    {
                        "status": "blocked_phase2_not_ready",
                        "ready": False,
                        "blockers": readiness.get("blockers", []),
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 1

    args.out.mkdir(parents=True, exist_ok=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    if args.workers == 1:
        for row in active_rows:
            result = _one(row, args.out, args.scratch, args.overwrite)
            results.append(result)
            print(json.dumps(result), flush=True)
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = [
                pool.submit(_one, row, args.out, args.scratch, args.overwrite)
                for row in active_rows
            ]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results.append(result)
                print(json.dumps(result), flush=True)

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "catalog_count": len(active_rows),
        "only_topologies": args.only_topologies,
        "materialized": sum(r["status"] == "materialized" for r in results),
        "existing": sum(r["status"] == "existing" for r in results),
        "failed": sum(r["status"] == "failed" for r in results),
        "summary": summary,
        "tasks": sorted(results, key=lambda r: r["task_id"]),
    }
    output = args.report or (args.out.parent / "phase2_pilot_51_report.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(output),
                "materialized": report["materialized"],
                "existing": report["existing"],
                "failed": report["failed"],
            }
        )
    )
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
