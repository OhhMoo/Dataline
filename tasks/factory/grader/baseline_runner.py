#!/usr/bin/env python3
"""Grade the five canonical staged baseline worlds and write anchor JSON.

The task materializer supplies one staged world per baseline under
`--worlds-dir/<baseline>/`, after running its scenario-specific solution and
rival scripts. This runner applies the same declarative grader to every world.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tasks.factory.grader.declarative_grade import grade
else:
    from .declarative_grade import grade

BASELINES = ("oracle", "no_op", "wrong_precedence", "duplicate_counted",
             "boundary_violated")


def run(worlds_dir: Path, grading: Path, variant: str = "default",
        output: Path | None = None) -> dict:
    worlds_dir, grading = Path(worlds_dir), Path(grading)
    rows = {}
    for name in BASELINES:
        root = worlds_dir / name
        if not (root / "var/warehouse/warehouse.duckdb").is_file():
            raise ValueError(f"baseline {name} has no staged world at {root}")
        report = root / "workspace/final_report.json"
        if not report.is_file():
            report = root / "final_report.json"
        result = grade(root, grading, report if report.is_file() else None, variant)
        rows[name] = {
            "total": result["total"], "max": result["max"],
            "passed": result["passed"], "progress_quantities": result["progress_quantities"],
            "checks": {c["id"]: {"points": c["points"], "max": c["max"],
                                 "passed": c["passed"], "detail": c["detail"]}
                       for c in result["checks"]},
        }
    oracle = rows["oracle"]
    if not oracle["passed"] or oracle["total"] != oracle["max"]:
        raise ValueError("oracle baseline must pass and score full marks")
    if rows["no_op"]["checks"].get("C0", {}).get("passed"):
        raise ValueError("no_op baseline must fail C0")
    for name in ("wrong_precedence", "duplicate_counted"):
        future = [v for k, v in rows[name]["checks"].items() if k.startswith("R")]
        if future and all(v["passed"] for v in future):
            raise ValueError(f"{name} baseline must fail a future-night receipt check")
    if rows["boundary_violated"]["passed"]:
        raise ValueError("boundary_violated baseline must fail")
    result = {"task": json.loads((grading / "spec.json").read_text(encoding="utf-8"))
              .get("task_id"), "variant": variant, "baselines": rows}
    if output:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worlds-dir", type=Path, required=True)
    parser.add_argument("--grading", type=Path, required=True)
    parser.add_argument("--variant", default="default")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    output = args.output or args.grading / "baselines" / f"{args.variant}.json"
    result = run(args.worlds_dir, args.grading, args.variant, output)
    print(json.dumps({"output": str(output), "scores":
                      {k: v["total"] for k, v in result["baselines"].items()}},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
