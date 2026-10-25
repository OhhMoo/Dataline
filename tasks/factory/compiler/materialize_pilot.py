"""Materialize one governance triplet per theme from the 1080-candidate catalog."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
from datetime import datetime, timezone
from pathlib import Path

from tasks.factory.compiler.task_materializer import materialize

REPO = Path(__file__).resolve().parents[3]
CATALOG = REPO / "experiments/scale_1000/catalog_1080.jsonl"
DEFAULT_OUT = Path("/home/ubuntu/alkera_factory_1017/pilot_tasks")
DEFAULT_SCRATCH = Path("/home/ubuntu/alkera_factory_1017/pilot_materialize")
MODES = ("act-authorized", "withhold-blocked", "escalate-ambiguous")


def select_pilot(catalog: Path, themes: set[str] | None = None) -> list[dict]:
    selected: dict[tuple[str, str], dict] = {}
    for line in catalog.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        theme, mode = row["theme"], row["governance_mode"]
        if themes is not None and theme not in themes:
            continue
        if mode in MODES and (theme, mode) not in selected:
            selected[(theme, mode)] = row
    return [selected[key] for key in sorted(selected)]


def _one(row: dict, out: Path, scratch: Path, overwrite: bool) -> dict:
    task = out / row["task_id"]
    if (task / "tests/materializer_report.json").is_file() and not overwrite:
        return {"task_id": row["task_id"], "theme": row["theme"],
                "mode": row["governance_mode"], "status": "existing",
                "path": str(task)}
    try:
        path = materialize(row, out, scratch=scratch, overwrite=True)
        return {"task_id": row["task_id"], "theme": row["theme"],
                "mode": row["governance_mode"], "status": "materialized",
                "path": str(path)}
    except (Exception, SystemExit) as exc:
        return {"task_id": row["task_id"], "theme": row["theme"],
                "mode": row["governance_mode"], "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[-1800:]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--themes", nargs="*")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    rows = select_pilot(args.catalog, set(args.themes) if args.themes else None)
    if args.dry_run:
        print(json.dumps({"count": len(rows), "task_ids": [r["task_id"] for r in rows]}, indent=2))
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_one, row, args.out, args.scratch, args.overwrite)
                   for row in rows]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            print(json.dumps(result), flush=True)
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(),
              "catalog_count": len(rows), "materialized": sum(r["status"] == "materialized"
                                                      for r in results),
              "existing": sum(r["status"] == "existing" for r in results),
              "failed": sum(r["status"] == "failed" for r in results),
              "tasks": sorted(results, key=lambda r: r["task_id"])}
    output = args.report or args.out.parent / "pilot_materialization_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "materialized": report["materialized"],
                      "failed": report["failed"]}))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
