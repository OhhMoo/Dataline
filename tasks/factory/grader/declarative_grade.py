#!/usr/bin/env python3
"""Grade a task from manifest.yaml, spec.json and oracle snapshots only.

Thin task wrapper:

    from tasks.factory.grader.declarative_grade import main
    raise SystemExit(main())

Two dialects share this entry point, chosen by `spec.json` `dialect`:

  probe (default)  every manifest check needs a `probe` mapping, either on the
                   check row or under `spec.json:checks.<id>`. Missing probes
                   are authoring errors, never free marks.
  kind             `"dialect": "kind"`: every check names an evaluator `kind`
                   and is scored by kind_engine (specs/README.md).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import sys
import traceback
from pathlib import Path

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from tasks.factory.grader import communication_rubric as communication
    from tasks.factory.grader import boundary_probe as boundary
    from tasks.factory.grader import receipt_engine as receipts
    from tasks.factory.grader.replay_isolation import advance_nights
else:
    from . import communication_rubric as communication
    from . import boundary_probe as boundary
    from . import receipt_engine as receipts
    from .replay_isolation import advance_nights


class GradingError(ValueError):
    pass


def _within(base: Path, relative: str) -> Path:
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()):
        raise GradingError(f"path escapes declared plane: {relative}")
    return path


def _sql_assert(world_root: Path, probe: dict) -> dict:
    from tasks.contracts._types import Context
    ctx = Context(world_root=world_root)
    try:
        actual = ctx.query_one(str(probe["sql"]))
    finally:
        ctx.close()
    value = actual[0] if actual else None
    expected = probe.get("equals")
    if "equals" in probe:
        passed = value == expected
    elif "at_least" in probe:
        passed = value is not None and value >= probe["at_least"]
        expected = {"at_least": probe["at_least"]}
    elif "at_most" in probe:
        passed = value is not None and value <= probe["at_most"]
        expected = {"at_most": probe["at_most"]}
    else:
        raise GradingError("sql_assert needs equals, at_least or at_most")
    return {"passed": passed, "actual": value, "expected": expected}


def _contract(world_root: Path, replayed: list[dict], probe: dict) -> dict:
    from tasks.contracts import evaluate
    from tasks.contracts._types import Context
    name = str(probe["name"])
    ledger = [r for night in replayed for r in night["receipts"]] \
        if probe.get("receipts") == "replay" else Context.load_receipts(
            world_root / "var/ledger/receipts.jsonl")
    ctx = Context(world_root=world_root, receipts=ledger,
                  before=probe.get("before") or {}, params=probe.get("params") or {})
    try:
        result = evaluate(name, ctx)
    finally:
        ctx.close()
    return {"passed": result.passed, "points": result.points, "max": result.max,
            "clauses": [c.as_dict() for c in result.clauses], "reason": result.reason}


def _advance(world_root: Path, grading: Path, spec: dict, cached: dict) -> list[dict]:
    optional = {**(cached.get("progress_snapshot_specs") or {}),
                **receipts.partition_snapshot_specs(world_root, grading, spec["replay"])}
    extra = {"optional_snapshots": optional} if optional else {}
    return advance_nights(world_root, grading, spec["replay"]["nights"],
                          cached.get("snapshot_specs"), spec["replay"].get("prepare"), **extra)


def _t0_spec(spec: dict) -> dict:
    """The t0 block, plus feed_day/t0_day when the task declares them beside it.

    The checkpoint window is read from this block. Copying the dates here keeps
    ``compare_t0`` on the t0 spec and still honors a task-level declaration.
    """
    t0 = dict(spec.get("t0") or {})
    if "feed_day" in spec or "t0_day" in spec:
        for key in ("feed_day", "t0_day"):
            if key not in t0 and key in spec:
                t0[key] = spec[key]
    return t0


def _probe(probe: dict, *, world_root: Path, grading: Path, artifact: Path | None,
           spec: dict, cached: dict) -> dict:
    kind = probe.get("kind")
    if kind == "all":
        children = [_probe(p, world_root=world_root, grading=grading,
                           artifact=artifact, spec=spec, cached=cached)
                    for p in probe.get("probes") or []]
        if not children:
            raise GradingError("all probe has no children")
        return {"passed": all(c["passed"] for c in children), "children": children}
    if kind == "t0_parity":
        if "t0" not in cached:
            cached["t0"] = receipts.compare_t0(world_root, grading, _t0_spec(spec))
        return cached["t0"]
    if kind == "night_receipt":
        probe = {**probe, "kind": "night_receipts", "night": probe.get("night")}
        kind = "night_receipts"
    if kind == "night_receipts":
        night = int(probe["night"])
        if "nights" not in cached:
            cached["nights"] = _advance(world_root, grading, spec, cached)
            cached["night_results"] = receipts.compare_nights(
                cached["nights"], grading, spec["replay"])
        return cached["night_results"][night - 1]
    if kind == "contract":
        if probe.get("receipts") == "replay" and "nights" not in cached:
            cached["nights"] = _advance(world_root, grading, spec, cached)
        return _contract(world_root, cached.get("nights") or [], probe)
    if kind == "closed_window_boundary":
        if "nights" not in cached:
            cached["nights"] = _advance(world_root, grading, spec, cached)
        return boundary.evaluate(world_root, grading, spec, probe, cached["nights"])
    if kind == "report":
        return communication.verify_report(artifact, probe)
    if kind == "states_hold":
        return communication.states_hold(world_root, artifact, probe)
    if kind == "escalation_credited":
        return communication.states_hold(world_root, artifact, probe)
    if kind == "kb_record_forged":
        return communication.kb_record_forged(
            world_root, str(probe["actor"]), list(probe.get("forbidden_claims") or []))
    if kind == "sql_assert":
        return _sql_assert(world_root, probe)
    if kind == "file_hash":
        plane = grading if probe.get("plane") == "grading" else world_root
        path = _within(plane, str(probe["path"]))
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        return {"passed": actual == probe.get("sha256"), "actual": actual,
                "expected": probe.get("sha256")}
    raise GradingError(f"unknown probe kind {kind!r}")


def _boundary_snapshots(spec: dict, probes: list[dict]) -> dict:
    collected = {}

    def visit(probe):
        if probe.get("kind") == "all":
            for child in probe.get("probes") or []:
                visit(child)
        elif probe.get("kind") == "closed_window_boundary":
            collected.update(boundary.snapshot_specs(spec, probe))

    for probe in probes:
        visit(probe)
    return collected


DIALECTS = ("probe", "kind")
PROGRESS_MODULE = "tasks.factory.grader.progress_checkpoints"


def spec_dialect(spec_path: Path | None) -> str:
    """`dialect` of a spec.json; a missing or unreadable spec is the probe dialect."""
    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return "probe"
    dialect = spec.get("dialect", "probe") if isinstance(spec, dict) else "probe"
    if dialect not in DIALECTS:
        raise GradingError(f"unknown spec dialect {dialect!r}; known {list(DIALECTS)}")
    return dialect


def find_spec(grading: Path, spec: Path | None = None, task_dir: Path | None = None,
              manifest: Path | None = None) -> Path | None:
    """The kind dialect's search order: explicit, grading zone, task tests, manifest dir."""
    if spec:
        return Path(spec)
    for candidate in (Path(grading) / "spec.json",
                      Path(task_dir) / "tests" / "spec.json" if task_dir else None,
                      Path(manifest).parent / "spec.json" if manifest else None):
        if candidate is not None and candidate.is_file():
            return candidate
    return None


def _progress_module() -> tuple[object | None, str | None]:
    try:
        return importlib.import_module(PROGRESS_MODULE), None
    except ModuleNotFoundError as exc:
        if exc.name == PROGRESS_MODULE:
            return None, None
        return None, f"import: {type(exc).__name__}: {exc}"
    except Exception as exc:                                       # noqa: BLE001
        return None, f"import: {type(exc).__name__}: {exc}"


def _progress_error(where: str, exc: BaseException) -> str:
    return f"{where}: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"


def _only_additions(base, extended):
    """`extended` with every value already in `base` restored; new keys kept."""
    if isinstance(base, dict) and isinstance(extended, dict):
        out = dict(extended)
        for key, value in base.items():
            out[key] = _only_additions(value, extended[key]) if key in extended else value
        return out
    if isinstance(base, list) and isinstance(extended, list) and len(base) == len(extended):
        return [_only_additions(a, b) for a, b in zip(base, extended)]
    return base


def _attach_progress(module, result: dict, **context) -> dict:
    try:
        extended = module.attach(copy.deepcopy(result), **context)
        if not isinstance(extended, dict):
            raise TypeError(f"attach returned {type(extended).__name__}, not dict")
        return _only_additions(result, extended)
    except Exception as exc:                                       # noqa: BLE001
        return {**result, "progress_error": _progress_error("attach", exc)}


def _kind_grade(world_root: Path, grading: Path, artifact: Path | None, variant: str | None,
                *, spec: Path, manifest: Path | None = None, task_dir: Path | None = None,
                workspace: Path | None = None, warehouse: Path | None = None) -> dict:
    from tasks.factory.grader import kind_engine
    world_root = Path(world_root).resolve()
    return kind_engine.kind_grade(
        warehouse=Path(warehouse or world_root / "var/warehouse/warehouse.duckdb").resolve(),
        grading=Path(grading).resolve(), world_root=world_root,
        workspace=Path(workspace).resolve() if workspace else world_root / "workspace",
        artifact_path=Path(artifact) if artifact else None, spec_path=Path(spec),
        manifest_path=Path(manifest) if manifest else None,
        task_dir=Path(task_dir).resolve() if task_dir else None, variant=variant)


def grade(world_root: Path, grading_dir: Path, artifact: Path | None = None,
          variant: str | None = None, *, spec: Path | None = None,
          manifest: Path | None = None, task_dir: Path | None = None,
          workspace: Path | None = None, warehouse: Path | None = None) -> dict:
    kind_spec = find_spec(grading_dir, spec, task_dir, manifest)
    if spec_dialect(kind_spec) == "kind":
        return _kind_grade(world_root, grading_dir, artifact, variant, spec=kind_spec,
                           manifest=manifest, task_dir=task_dir, workspace=workspace,
                           warehouse=warehouse)
    world_root, grading = Path(world_root).resolve(), Path(grading_dir).resolve()
    manifest_path = Path(manifest) if manifest else grading / "manifest.yaml"
    spec_path = Path(spec) if spec else grading / "spec.json"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(spec, dict):
        raise GradingError("manifest.yaml and spec.json must be mappings")
    if int(spec.get("schema_version", 1)) != 1:
        raise GradingError(f"unsupported spec schema_version {spec.get('schema_version')}")
    if variant is None:
        variant_path = grading / "variant.json"
        variant = (json.loads(variant_path.read_text(encoding="utf-8")).get("variant")
                   if variant_path.is_file() else "default")
    variant = variant or "default"
    rows = [r for r in manifest.get("checks") or []
            if not r.get("only_variants") or variant in r["only_variants"]]
    if not rows or len({r.get("id") for r in rows}) != len(rows):
        raise GradingError("manifest check IDs must be nonempty and unique")
    active_probes = [r.get("probe") or (spec.get("checks") or {}).get(r["id"])
                     for r in rows]
    cached: dict = {"snapshot_specs": _boundary_snapshots(
        spec, [p for p in active_probes if isinstance(p, dict)])}
    progress_module, progress_error = _progress_module()
    if progress_module is not None and hasattr(progress_module, "snapshot_specs"):
        try:
            declared = progress_module.snapshot_specs(spec) or {}
            cached["progress_snapshot_specs"] = {
                name: dict(decl) for name, decl in dict(declared).items()
                if name not in cached["snapshot_specs"]}
        except Exception as exc:                                   # noqa: BLE001
            progress_error = _progress_error("snapshot_specs", exc)
    checks = []
    for row in rows:
        cid = row["id"]
        probe = row.get("probe") or (spec.get("checks") or {}).get(cid)
        if not isinstance(probe, dict):
            raise GradingError(f"check {cid} has no declarative probe")
        detail = _probe(probe, world_root=world_root, grading=grading,
                        artifact=Path(artifact) if artifact else None,
                        spec=spec, cached=cached)
        passed = bool(detail["passed"])
        maximum = float(row.get("points", 0))
        floor = float(row.get("floor", 0))
        points = maximum if passed else floor if maximum == 0 and floor < 0 else 0.0
        checks.append({"id": cid, "axis": row.get("axis", "CORRECTNESS"),
                       "passed": passed, "points": points, "max": maximum,
                       "detail": detail})
    total = round(sum(r["points"] for r in checks), 4)
    maximum = round(sum(r["max"] for r in checks), 4)
    harness = manifest.get("recommended_harness") or {}
    arm = (harness.get("variants") or {}).get(variant) or {}
    required = arm.get("pass_requires_checks") or harness.get("pass_requires_checks") \
        or [r["id"] for r in rows if r.get("in_pass_criterion")]
    floor = float(arm.get("pass_min_total", harness.get("pass_min_total", 0)))
    check_map = {r["id"]: r for r in checks}
    unknown = set(required) - set(check_map)
    if unknown:
        raise GradingError(f"pass criterion names inactive checks: {sorted(unknown)}")
    passed = all(check_map[cid]["passed"] for cid in required) and total >= floor
    by_axis = {}
    for row in checks:
        axis = by_axis.setdefault(row["axis"], {"points": 0.0, "max": 0.0})
        axis["points"] += row["points"]
        axis["max"] += row["max"]
    def _unit(value) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        if number != number:
            return 0.0
        return round(min(1.0, max(0.0, number)), 6)

    progress = {"q0": _unit(cached.get("t0", {}).get("fraction", 0.0))}
    for index, row in enumerate(cached.get("night_results") or [], 1):
        progress[f"q{index}"] = _unit(row.get("fraction"))
    # q0..q4 stay for the compiler. progress_checkpoints supplies the seven
    # named fractions; the receipt-level ones fill any it did not produce.
    seven = receipts.seven_step_progress(
        world_root, grading, spec.get("t0") or {}, cached.get("night_results"))
    result = {"task": spec.get("task_id") or manifest.get("task_id"), "variant": variant,
              "total": total, "max": maximum, "max_total": maximum,
              "passed": passed, "checks": checks, "by_axis": by_axis,
              "pass_requires_checks": required, "pass_min_total": floor,
              "progress_quantities": progress,
              "outcome_evidence": {"check_passed": {r["id"]: r["passed"] for r in checks},
                                   "nights_replayed": len(cached.get("nights") or [])}}
    if progress_module is not None and hasattr(progress_module, "attach"):
        result = _attach_progress(progress_module, result, world_root=world_root,
                                  grading=grading, spec=copy.deepcopy(spec),
                                  nights=copy.deepcopy(cached.get("nights")),
                                  t0=copy.deepcopy(cached.get("t0")),
                                  task_dir=task_dir)
    check_owners = {
        "q0_raw_loaded": "C0",
        "q1_t0_dedup_and_precedence": "C0",
        "q2_t0_handoff_parity": "C0",
        "q3_r1_parity": "R1",
        "q4_r2_parity": "R2",
        "q5_r3_parity": "R3",
        "q6_r4_parity": "R4",
    }
    for key, value in seven.items():
        result["progress_quantities"].setdefault(key, value)
        owner = check_owners.get(key)
        if owner and value is not None:
            for check_row in result.get("checks") or []:
                if check_row.get("id") == owner and isinstance(check_row.get("detail"), dict):
                    check_row["detail"].setdefault(key, value)
    if progress_error:
        result["progress_error"] = "\n".join(
            e for e in (progress_error, result.get("progress_error")) if e)
    return result


def main(argv: list[str] | None = None, *, task_dir: Path | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", help="warehouse DuckDB file, checked for CLI compatibility")
    parser.add_argument("--grading", type=Path, required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--world-root", type=Path)
    parser.add_argument("--variant")
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--task-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    task_dir = args.task_dir or task_dir
    if spec_dialect(find_spec(args.grading, args.spec, task_dir, args.manifest)) == "kind":
        from tasks.factory.grader import kind_engine
        return kind_engine.kind_main(argv, task_dir=Path(task_dir).resolve() if task_dir else None)
    root = args.world_root or (args.workspace.parent if args.workspace else None)
    if root is None:
        parser.error("--world-root or --workspace is required")
    root = root.resolve()
    if args.world and Path(args.world).resolve() != (root / "var/warehouse/warehouse.duckdb").resolve():
        parser.error("--world must name the warehouse under --world-root")
    result = grade(root, args.grading, args.artifact, args.variant,
                   spec=args.spec, manifest=args.manifest, task_dir=task_dir)
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
