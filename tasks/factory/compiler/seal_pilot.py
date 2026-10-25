"""Upgrade a materialized pilot to private future replay and semantic blockers."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import yaml

PREPARE = [{"source": "factory_future", "destination": "build/factory_future"}]


def _archive_legacy_worlds(task: Path, backup: Path) -> list[str]:
    """Remove pre-seal world snapshots from the task's verifier search path."""
    archived = []
    for name, backup_name in (("worlds", "legacy_worlds"), ("world", "legacy_world")):
        staged = task / name
        if not staged.is_dir():
            continue
        destination = backup / backup_name
        if destination.exists():
            raise FileExistsError(f"legacy world backup already exists: {destination}")
        backup.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(destination))
        archived.append(name)
    return archived


def upgrade_task(task: Path, backup_root: Path) -> dict:
    task = Path(task)
    tests = task / "tests"
    source = task / "fixtures/next_nights"
    if not source.is_dir():
        raise FileNotFoundError(f"future fixture absent: {source}")
    spec_path, manifest_path = tests / "spec.json", tests / "manifest.yaml"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    mode = json.loads((tests / "governance.json").read_text(encoding="utf-8"))["mode"]
    backup = Path(backup_root) / task.name
    if spec.get("replay", {}).get("prepare") == PREPARE:
        archived = _archive_legacy_worlds(task, backup)
        return {"task": task.name, "status": "already_upgraded",
                "legacy_worlds_archived": archived}
    if backup.exists():
        raise FileExistsError(f"backup already exists: {backup}")
    backup.mkdir(parents=True)
    for path in (spec_path, manifest_path, tests / "baselines/default.json"):
        if path.is_file():
            dest = backup / path.relative_to(tests)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)

    future = tests / "factory_future"
    if future.exists():
        shutil.rmtree(future)
    shutil.copytree(source, future)
    spec["replay"]["prepare"] = PREPARE
    if mode != "act-authorized":
        manifest_d2 = next(row for row in manifest["checks"] if row["id"] == "D2")["probe"]
        spec_d2 = spec["checks"]["D2"]
        for probe in (manifest_d2, spec_d2):
            probe["fields"].pop("decision.blocker", None)
            probe["required_fields"] = ["decision.blocker"]
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    (tests / "baselines/default.json").unlink(missing_ok=True)
    archived = _archive_legacy_worlds(task, backup)
    return {"task": task.name, "status": "upgraded", "mode": mode,
            "future_files": sum(path.is_file() for path in future.rglob("*")),
            "legacy_worlds_archived": archived}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    tasks = sorted(path for path in args.tasks_dir.iterdir() if path.is_dir())
    rows = [upgrade_task(task, args.backup_root) for task in tasks]
    report = {"total": len(rows), "upgraded": sum(r["status"] == "upgraded" for r in rows),
              "legacy_worlds_archived": sum(len(r.get("legacy_worlds_archived") or []) for r in rows),
              "tasks": rows, "backup_root": str(args.backup_root)}
    body = json.dumps(report, indent=2) + "\n"
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(body, encoding="utf-8")
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
