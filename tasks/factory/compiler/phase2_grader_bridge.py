"""Expose Opus's seven progress checkpoints through the harness check details."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tasks.factory.grader.declarative_grade import grade

CHECKPOINT_CHECKS = {
    "q0_raw_loaded": "C0",
    "q1_t0_dedup_and_precedence": "C0",
    "q2_t0_handoff_parity": "C0",
    "q3_r1_parity": "R1",
    "q4_r2_parity": "R2",
    "q5_r3_parity": "R3",
    "q6_r4_parity": "R4",
}


def embed_progress_quantities(result: dict) -> dict:
    """Keep top-level progress and check details consistent for matrix.progress."""
    progress = result.get("progress_quantities") or {}
    checks = result.get("checks") or []
    if isinstance(checks, dict):
        by_id = checks
    else:
        by_id = {row.get("id"): row for row in checks if isinstance(row, dict)}
    for name, check_id in CHECKPOINT_CHECKS.items():
        if name not in progress or check_id not in by_id:
            continue
        row = by_id[check_id]
        detail = row.get("detail")
        if not isinstance(detail, dict):
            detail = {"original_detail": detail} if detail is not None else {}
            row["detail"] = detail
        detail[name] = progress[name]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world")
    parser.add_argument("--grading", type=Path, required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--world-root", type=Path)
    parser.add_argument("--variant")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = args.world_root or (args.workspace.parent if args.workspace else None)
    if root is None:
        parser.error("--world-root or --workspace is required")
    root = root.resolve()
    if args.world and Path(args.world).resolve() != (root / "var/warehouse/warehouse.duckdb").resolve():
        parser.error("--world must name the warehouse under --world-root")
    result = embed_progress_quantities(grade(root, args.grading, args.artifact, args.variant))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        for row in result["checks"]:
            print(f"{row['id']:<5} {row['points']:>5g}/{row['max']:<5g} {row['passed']}")
        print(f"total {result['total']:g}/{result['max']:g} "
              f"{'PASS' if result['passed'] else 'fail'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
