"""Preflight for the Section 7 Phase 2 pilot and scale gates.

The compiler cannot produce a meaningful hardness proof from Phase 1 sources.
This preflight records the concrete upstream artifacts needed before the 51-task
pilot is materialized. It does not infer readiness from a completion marker alone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

from tasks.factory.compiler.blueprint_matrix import THEMES

REPO = Path(__file__).resolve().parents[3]
STATUS_DIR = Path("experiments/scale_1000/status")


def phase2_fingerprint(blueprint: dict, repo: str | Path = REPO) -> str:
    """Hash the blueprint and the source inputs used to compile it.

    A changed upstream implementation invalidates a previously materialized
    task even when the catalog ID is unchanged.
    """
    root = Path(repo).resolve()
    paths: list[Path] = [
        root / STATUS_DIR / f"DONE_{agent}_phase2.json"
        for agent in ("codex", "opus", "kimi", "grok")
    ]
    theme_root = root / "world/world/themes" / blueprint["theme"]
    paths.extend(path for path in theme_root.rglob("*")
                 if path.is_file() and "__pycache__" not in path.parts
                 and path.suffix in {".py", ".sql", ".yaml", ".yml", ".json", ".csv", ".md"})
    for relative in ("tasks/factory/stressors", "tasks/factory/hazards",
                     "tasks/factory/governance", "tasks/factory/narrative",
                     "tasks/factory/grader", "tasks/contracts"):
        paths.extend(sorted((root / relative).rglob("*.py")))
    paths.extend(root / "tasks/factory/compiler" / name for name in
                 ("task_materializer.py", "peer_materializer.py", "sealed_future.py",
                  "finalize_task.py", "report_contract.py",
                  "phase2_manifest_binding.py", "phase2_grader_bridge.py"))
    paths.extend(root / "world/world" / name for name in
                 ("build.py", "scheduler.py", "workflow.py", "executor.py"))
    paths.extend(root / "tasks/harness" / name for name in
                 ("rollout.py", "run_queue.py", "cursor_subject.py", "container_shim.py",
                  "native_stack.py", "taskspec.py", "outcome.py"))
    paths.append(root / "tasks/factory/verify_all.py")
    digest = hashlib.sha256(json.dumps(blueprint, sort_keys=True,
                                      separators=(",", ":")).encode("utf-8"))
    for path in sorted(set(paths)):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"MISSING")
    return digest.hexdigest()


def phase2_task_integrity(task: str | Path) -> str:
    """Hash the task package, excluding generated baseline results and this seal."""
    root = Path(task)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if relative in {Path("tests/phase2_provenance.json"),
                        Path("tests/phase2_pilot_provenance.json")} or "baselines" in relative.parts:
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _status_check(root: Path, agent: str, phase: int = 2) -> dict:
    relative = STATUS_DIR / f"DONE_{agent}_phase{phase}.json"
    path = root / relative
    if not path.is_file():
        return {"ok": False, "detail": f"missing Phase {phase} completion marker", "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "detail": f"invalid completion marker: {exc}", "path": str(path)}
    if not isinstance(payload, dict):
        return {"ok": False, "detail": "completion marker is not an object", "path": str(path)}
    state = str(payload.get("status", "")).lower()
    if state not in {"complete", "completed", "passed", "success", "done", "ready"}:
        return {"ok": False, "detail": f"upstream status is {state or 'unspecified'}", "path": str(path)}
    return {"ok": True, "detail": f"upstream reports {state}", "path": str(path)}


def _codex_native_execution_check(root: Path) -> dict:
    """Reject an explicitly documented sidecar fallback in the shipped marker."""
    path = root / STATUS_DIR / "DONE_codex_phase2.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "detail": "Codex native execution evidence unavailable",
                "path": str(path)}
    contract = payload.get("shipped_module_contract") if isinstance(payload, dict) else None
    contract = contract if isinstance(contract, dict) else {}
    fallback = str(contract.get("fallback") or "")
    null_handoff = str(contract.get("null_handoff") or "")
    sidecar = ("factory sidecar" in fallback.lower()
               or "raw.factory_events" in fallback
               or "warehouse.factory_handoff" in null_handoff)
    return {"ok": not sidecar,
            "detail": ("composer declares physical factory sidecar fallback for "
                       "built native theme worlds" if sidecar else
                       "composer does not declare a native execution fallback"),
            "path": str(path)}


def inspect_phase2_readiness(repo: str | Path = REPO) -> dict:
    """Return checked evidence and blockers for the Phase 2 materializer.

    This is intentionally a preflight, not a claim that the upstream test suites
    passed. Each team still owns its completion marker and regression evidence.
    """
    root = Path(repo).resolve()
    checks: dict[str, dict[str, dict]] = {}

    runtime = root / "tasks/factory/stressors/runtime_template.py"
    runtime_body = runtime.read_text(encoding="utf-8") if runtime.is_file() else ""
    checks["codex"] = {
        "completion": _status_check(root, "codex"),
        "native_execution": _codex_native_execution_check(root),
        "three_modules": {
            "ok": all(name in runtime_body for name in
                      ("ingest_feed.py", "fold_ledger.py", "publish_handoff.py")),
            "detail": "3-module runtime renderer",
            "path": str(runtime),
        },
        "no_policy_oracle_branch": {
            "ok": bool(runtime_body) and
                  re.search(r"^\s*POLICY\s*=", runtime_body, re.MULTILINE) is None and
                  re.search(r"^\s*if\s+POLICY\[", runtime_body, re.MULTILINE) is None,
            "detail": "Phase 1 policy switches removed from shipped template",
            "path": str(runtime),
        },
        "hazard_breadcrumbs": {
            "ok": (root / "tasks/factory/hazards/t0_breadcrumbs.py").is_file(),
            "detail": "indirect t0 hazard breadcrumbs",
            "path": str(root / "tasks/factory/hazards/t0_breadcrumbs.py"),
        },
        "domain_schema_adapter": {
            "ok": (root / "tasks/factory/stressors/domain_schema_adapter.py").is_file(),
            "detail": "native theme schema adapter",
            "path": str(root / "tasks/factory/stressors/domain_schema_adapter.py"),
        },
    }

    grader_sources = [root / "tasks/factory/grader/receipt_engine.py",
                      root / "tasks/factory/grader/declarative_grade.py"]
    checkpoints = ("q0_raw_loaded", "q1_t0_dedup_and_precedence",
                   "q2_t0_handoff_parity", "q3_r1_parity", "q4_r2_parity",
                   "q5_r3_parity", "q6_r4_parity")
    grader_body = "\n".join(path.read_text(encoding="utf-8") for path in grader_sources
                            if path.is_file())
    checks["opus"] = {
        "completion": _status_check(root, "opus"),
        "seven_progress_checkpoints": {
            "ok": all(key in grader_body for key in checkpoints),
            "detail": "q0..q6 grader progress checkpoints",
            "paths": [str(path) for path in grader_sources],
        },
    }

    contracts = [root / "world/world/themes" / theme / "theme_contract.yaml"
                 for theme in THEMES]
    missing_contracts = [str(path) for path in contracts if not path.is_file()]
    required_bindings = ("landing_dir", "raw_table", "fact_table", "handoff_table",
                         "close_window_table", "corrections_table", "grain_keys",
                         "amount_column", "version_column", "cutoff_time_utc",
                         "scheduled_jobs")
    essential_bindings = ("landing_dir", "raw_table", "fact_table", "grain_keys",
                          "amount_column", "cutoff_time_utc", "scheduled_jobs")
    incomplete_bindings: dict[str, list[str]] = {}
    for theme, path in zip(THEMES, contracts):
        if not path.is_file():
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            document = None
        if not isinstance(document, dict):
            incomplete_bindings[theme] = ["invalid YAML mapping"]
            continue
        missing = [key for key in required_bindings if key not in document]
        empty = [f"empty:{key}" for key in essential_bindings if not document.get(key)]
        generic = [key for key in ("raw_table", "fact_table", "handoff_table",
                                    "close_window_table", "corrections_table")
                   if str(document.get(key) or "").startswith(
                       ("raw.factory_", "warehouse.factory_", "warehouse.fct_factory_"))]
        if missing or empty or generic:
            incomplete_bindings[theme] = [*missing, *empty,
                                           *(f"generic:{key}" for key in generic)]
    checks["kimi"] = {
        "completion": _status_check(root, "kimi"),
        "theme_contracts": {
            "ok": not missing_contracts,
            "detail": f"{len(contracts) - len(missing_contracts)}/{len(contracts)} theme contracts present",
            "missing": missing_contracts,
        },
        "complete_bindings": {
            "ok": not missing_contracts and not incomplete_bindings,
            "detail": f"{len(contracts) - len(missing_contracts) - len(incomplete_bindings)}/"
                      f"{len(contracts)} complete native theme contracts",
            "incomplete": incomplete_bindings,
        },
    }

    linter = root / "tasks/factory/narrative/discoverability_and_spoiler_linter.py"
    linter_body = linter.read_text(encoding="utf-8") if linter.is_file() else ""
    governance = root / "tasks/factory/governance/twin_state_machine.py"
    governance_body = governance.read_text(encoding="utf-8") if governance.is_file() else ""
    checks["grok"] = {
        "completion": _status_check(root, "grok"),
        "hazard_clue_lint": {
            "ok": "hazard" in linter_body.lower() and "clue" in linter_body.lower(),
            "detail": "hazard clue discoverability lint",
            "path": str(linter),
        },
        "three_hop_governance": {
            "ok": "sql_query" in governance_body and
                  any(word in governance_body.lower() for word in ("three_hop", "3-hop", "3_hop")),
            "detail": "multi-surface governance chain",
            "path": str(governance),
        },
    }

    catalog = root / "experiments/scale_1000/catalog_1080.jsonl"
    if catalog.is_file():
        try:
            entries = [json.loads(line) for line in catalog.read_text(encoding="utf-8").splitlines()
                       if line.strip()]
            catalog_ok = len(entries) == 1080 and len({e.get("task_id") for e in entries}) == 1080
            detail = f"{len(entries)} rows, {len({e.get('task_id') for e in entries})} IDs"
        except (OSError, ValueError, TypeError) as exc:
            catalog_ok, detail = False, f"invalid catalog: {exc}"
    else:
        catalog_ok, detail = False, "catalog missing"
    checks["catalog"] = {"catalog_1080": {"ok": catalog_ok, "detail": detail,
                                            "path": str(catalog)}}

    blockers = [f"{owner}.{name}: {check['detail']}"
                for owner, group in checks.items() for name, check in group.items()
                if not check["ok"]]
    return {"ready": not blockers, "checks": checks, "blockers": blockers}


def inspect_phase3_readiness(repo: str | Path = REPO) -> dict:
    """Return checked evidence and blockers for Phase 3 delta materialization."""
    root = Path(repo).resolve()
    checks: dict[str, dict[str, dict]] = {}

    temporal = root / "tasks/factory/hazards/temporal_contract_hazards.py"
    temporal_body = temporal.read_text(encoding="utf-8") if temporal.is_file() else ""
    has_controller_param = bool(re.search(r"\bcontroller\s*[:=]", temporal_body))
    hardcodes_controller = bool(
        re.search(r"""["']approved_by["']\s*:\s*["']controller["']""", temporal_body)
    )
    composer = root / "tasks/factory/stressors/oracle_composer.py"
    composer_body = composer.read_text(encoding="utf-8") if composer.is_file() else ""
    breadcrumbs = root / "tasks/factory/hazards/t0_breadcrumbs.py"
    breadcrumbs_body = breadcrumbs.read_text(encoding="utf-8") if breadcrumbs.is_file() else ""
    checks["codex"] = {
        "completion": _status_check(root, "codex", phase=3),
        "h13_controller_parameter": {
            "ok": bool(temporal_body) and has_controller_param and not hardcodes_controller,
            "detail": "H13 uses controller parameter instead of hardcoded 'controller'",
            "path": str(temporal),
        },
        "oracle_composer_schedule_yml": {
            "ok": "schedule.yml" in composer_body,
            "detail": "oracle_composer references schedule.yml",
            "path": str(composer),
        },
        "stressor_breadcrumbs": {
            "ok": "S07" in breadcrumbs_body and "S03" in breadcrumbs_body,
            "detail": "t0_breadcrumbs covers S07 and S03",
            "path": str(breadcrumbs),
        },
    }

    blindness = root / "tasks/harness/blindness.py"
    blindness_body = blindness.read_text(encoding="utf-8") if blindness.is_file() else ""
    legacy_solution_regex = '[\\\\/]solution(?=[\\\\/\'\\"\\s]|$)'
    unfiltered_marker_loop = (
        "hit = pat.search(text)\n"
        "            if hit:\n"
        "                kind = (\"attempted_forbidden_access\""
    )
    blindness_updated = bool(blindness_body) and not (
        legacy_solution_regex in blindness_body
        and (unfiltered_marker_loop in blindness_body or len(blindness_body.splitlines()) <= 10)
    )
    checks["opus"] = {
        "completion": _status_check(root, "opus", phase=3),
        "blindness_updated": {
            "ok": blindness_updated,
            "detail": "blindness.py updated for natural-language solution prose",
            "path": str(blindness),
        },
    }

    scheduler = root / "world/world/scheduler.py"
    scheduler_body = scheduler.read_text(encoding="utf-8") if scheduler.is_file() else ""
    checks["kimi"] = {
        "completion": _status_check(root, "kimi", phase=3),
        "scheduler_schedule_yml": {
            "ok": "schedule.yml" in scheduler_body,
            "detail": "world/world/scheduler.py references schedule.yml",
            "path": str(scheduler),
        },
    }

    synthesizer = root / "tasks/factory/narrative/corecraft_synthesizer.py"
    synthesizer_body = synthesizer.read_text(encoding="utf-8") if synthesizer.is_file() else ""
    linter = root / "tasks/factory/narrative/discoverability_and_spoiler_linter.py"
    linter_body = linter.read_text(encoding="utf-8") if linter.is_file() else ""
    checks["grok"] = {
        "completion": _status_check(root, "grok", phase=3),
        "synthesizer_stressor_clues": {
            "ok": "S07" in synthesizer_body and "S09" in synthesizer_body,
            "detail": "corecraft_synthesizer covers S07 and S09",
            "path": str(synthesizer),
        },
        "linter_stressor_clues": {
            "ok": "S07" in linter_body and "S09" in linter_body,
            "detail": "discoverability_and_spoiler_linter covers S07 and S09",
            "path": str(linter),
        },
    }

    blockers = [f"{owner}.{name}: {check['detail']}"
                for owner, group in checks.items() for name, check in group.items()
                if not check["ok"]]
    return {"ready": not blockers, "checks": checks, "blockers": blockers}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect Phase 2/3 integration readiness")
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--phase3", action="store_true",
                        help="inspect Phase 3 delta readiness instead of Phase 2")
    parser.add_argument("--json", type=Path, dest="json_out")
    args = parser.parse_args(argv)
    report = (inspect_phase3_readiness(args.repo) if args.phase3
              else inspect_phase2_readiness(args.repo))
    body = json.dumps(report, indent=2) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(body, encoding="utf-8")
    print(body, end="")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
