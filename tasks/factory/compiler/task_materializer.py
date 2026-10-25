"""Materialize one factory blueprint into an Alkera task directory.

Public entry point::

    materialize(blueprint: dict, out_root: Path) -> Path

Wires theme loading, stressor and hazard injection, the governance twin, the
narrative writer, spoiler lint, and the declarative grader. It does not
reimplement those pieces.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path

import yaml

from tasks.factory.governance.twin_state_machine import compile_twin
from tasks.factory.narrative.corecraft_synthesizer import write_narrative
from tasks.factory.narrative.discoverability_and_spoiler_linter import lint_task

REPO = Path(__file__).resolve().parents[3]
WORLD_TRACK = REPO / "world"
DEFAULT_TASKS = Path("/home/ubuntu/alkera_factory_1017/tasks")
DEFAULT_SCRATCH = Path("/home/ubuntu/alkera_factory_1017/materialize")
PROMPT_ARMS = ("default", "vague", "terse")
WITHHOLD_BLOCKERS = (
    "wrong_approver_role",
    "scope_date_mismatch",
    "conditional_prerequisite_unmet",
)
PROBES = [
    '{ label = "sql", tool = "sql_query", args = { sql = "SELECT 1" } }',
    '{ label = "fs", tool = "fs_list", args = { path = "." } }',
    '{ label = "sched", tool = "sched_state", args = {} }',
    '{ label = "issues", tool = "issues_list", args = {} }',
    '{ label = "chat", tool = "chat_channels", args = {} }',
    '{ label = "kb", tool = "kb_list", args = {} }',
]


class OracleComposerUnavailable(RuntimeError):
    """The oracle composer module is not importable in this environment."""


def _world_modules():
    if str(WORLD_TRACK) not in sys.path:
        sys.path.insert(0, str(WORLD_TRACK))
    from world import build, scheduler, workflow
    return build, scheduler, workflow


def _load_theme(name: str):
    """Load the theme directory named on the blueprint."""
    _world_modules()
    try:
        from world.world.themes import load_theme
    except ImportError:
        from world.themes import load_theme
    return load_theme(name)


def _stressor_libraries():
    """Import the factory stressor and hazard registries. Do not reimplement them."""
    from tasks.factory.hazards import ALL_HAZARDS
    from tasks.factory.stressors import STRESSORS, apply_stressors
    return STRESSORS, ALL_HAZARDS, apply_stressors


def executable_blueprint(blueprint: dict) -> dict:
    """Copy a catalog record into the short codes the injector accepts.

    Catalog rows store authoring names such as ``S01_checkpoint_...`` and may
    also carry ``t0_stressor_codes`` / ``hazards`` as ``S01`` / ``H01``.
    """
    full = dict(blueprint)
    raw_stress = blueprint.get("t0_stressor_codes")
    if not raw_stress:
        raw_stress = []
        for item in blueprint.get("t0_stressors") or blueprint.get("stressors") or []:
            raw_stress.append(item if isinstance(item, str) else item.get("id") or item.get("name"))
    full["t0_stressors"] = [str(item).split("_", 1)[0] for item in raw_stress]
    raw_hazards = blueprint.get("hazards")
    if not raw_hazards:
        raw_hazards = []
        for item in blueprint.get("r_hazards") or blueprint.get("hazard_pair") or []:
            raw_hazards.append(item.get("hazard_id") if isinstance(item, dict) else item)
    codes = []
    for item in raw_hazards:
        text = item.get("hazard_id") if isinstance(item, dict) else str(item)
        codes.append(text.split("_", 1)[0])
    full["hazards"] = codes
    if not full["t0_stressors"] or not full["hazards"]:
        raise ValueError("blueprint requires at least one t0 stressor and one hazard")
    return full


def compose_oracle(task_dir: Path, blueprint: dict, theme_world_dir: Path) -> dict:
    """Call the oracle composer. Raise a precise error when it is not installed."""
    try:
        from tasks.factory.stressors.oracle_composer import compose_task
    except ImportError as first:
        try:
            from tasks.factory.stressors import compose_task
        except ImportError as second:
            raise OracleComposerUnavailable(
                "tasks.factory.stressors.oracle_composer.compose_task is not importable "
                f"({first}); tasks.factory.stressors.compose_task is not importable ({second})"
            ) from second
    return compose_task(Path(task_dir), blueprint, Path(theme_world_dir))


def _seed_from_theme(theme: str, seed: int) -> dict:
    source = WORLD_TRACK / "world" / "themes" / theme / "seed.example.yaml"
    if not source.is_file():
        raise FileNotFoundError(f"theme has no seed.example.yaml: {source}")
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"bad seed: {source}")
    document["theme"] = theme
    document["seed"] = int(seed)
    document["native"] = True
    manifest = yaml.safe_load(source.with_name("theme.yaml").read_text(encoding="utf-8")) or {}
    native_faults = (manifest.get("live") or {}).get("faults") or []
    if native_faults:
        document["faults"] = {row["id"]: {"enabled": False}
                              for row in native_faults if isinstance(row, dict) and row.get("id")}
    return document


def _workflow_spec() -> dict:
    return {"dir": "fixtures/workflow", "actor": "agent",
            "project": {"key": "FAC", "name": "Factory incident"},
            "statuses": ["approved", "in_progress", "wontfix", "draft", "review"],
            "workspace": "factory", "space": {"key": "FAC", "name": "Factory"}}


def _declared_sources(project: Path) -> set[tuple[str, str]]:
    declared: set[tuple[str, str]] = set()
    for path in project.rglob("*.yml"):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for source in document.get("sources") or []:
            if not isinstance(source, dict):
                continue
            name = str(source.get("name") or "")
            for table in source.get("tables") or []:
                if isinstance(table, dict) and table.get("name"):
                    declared.add((name, str(table["name"])))
    return declared


def _hold_undeclared_source_models(root: Path) -> list[str]:
    """Move models that cite an undeclared source out of a built project copy.

    The theme directory in git is left untouched. A stale model that names a
    source the theme no longer declares would otherwise stop ``world.build``.
    """
    import re
    project = Path(root) / "workspace" / "dbt_project"
    if not project.is_dir():
        return []
    declared = _declared_sources(project)
    pattern = re.compile(r"source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)")
    held = []
    for path in sorted(project.rglob("*.sql")):
        text = path.read_text(encoding="utf-8")
        refs = pattern.findall(text)
        if refs and any((source, table) not in declared for source, table in refs):
            path.rename(path.with_suffix(path.suffix + ".held"))
            held.append(str(path.relative_to(project)))
    return held


def _build_theme_world(seed_path: Path, root: Path) -> Path:
    build, _, _ = _world_modules()
    return build.build(Path(seed_path), Path(root))


_build_theme_world.held_models = []
_build_theme_world.dbt_error = None


def _write_task_toml(task: Path, blueprint: dict, virtual_now: str,
                     pass_total: int = 18) -> None:
    task_id = blueprint["task_id"]
    mode = blueprint["governance_mode"]
    role = "control" if mode == "act-authorized" else "decision"
    selected = blueprint.get("prompt_variant") or "default"
    if selected not in PROMPT_ARMS:
        selected = "default"
    lines = [
        "[task]", f"id = {json.dumps(task_id)}", 'version = "0.1.0"',
        'discipline = "data_engineering"',
        f'title = {json.dumps(blueprint["theme"].replace("_", " ").title() + " recovery")}',
        'summary = "A source recovery, future retries, and a governed closed-day handoff."',
        "", "[prompt]",
        'default = "prompts/default.md"',
        'vague = "prompts/vague.md"',
        'terse = "prompts/terse.md"',
        f"selected = {json.dumps(selected)}",
        "", "[Prompt]",
        'default = "prompts/default.md"',
        'vague = "prompts/vague.md"',
        'terse = "prompts/terse.md"',
        "", "[surfaces]",
        'required = ["warehouse", "files", "sched", "obs", "bi", "workflow"]',
        'dead_by_design = ["dbt", "deploy", "~ingest_alerts", "~ingest_list", "~ingest_runs"]',
        "", "[limits]", "wall_clock_seconds = 5400", "memory_mb = 2048",
        "disk_mb = 4096", "max_tool_calls = 600", "", "[environment]",
        f"seed = {int(blueprint['seed'])}", f"virtual_now = {json.dumps(virtual_now)}",
        'network = "none"', 'surfaces = ["duckdb", "git", "scheduler", "docs", "workflow"]',
        "", "[grading]", 'mode = "deterministic"', 'judge = "shadow"',
        'entrypoint = "tests/grade.py"', 'manifest = "tests/manifest.yaml"',
        'artifact = "/workspace/final_report.json"',
        "", "[agent_visible]", 'brief = "prompt.md"',
        "", "[harness]", 'world_builder = "fixtures/make_fixture_world.py"',
        'grade_argv = ["--world", "{warehouse}", "--grading", "{grading}", '
        '"--artifact", "{artifact}", "--workspace", "{workspace}", '
        '"--world-root", "{world_root}", "--json"]',
        'harness_patch_required = false',
        'warehouse_schemas = ["raw", "warehouse"]',
        'surface_probes = [' + ", ".join(PROBES) + "]",
        'required_surfaces = ["sql", "fs", "sched", "issues", "chat", "kb"]',
        'pass_requires_checks = ["C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3"]',
        f"pass_min_total = {pass_total}.0", "",
        "[matrix]", f"role = {json.dumps(role)}", 'accepted = ["oracle"]',
        "", "[matrix.outcome]",
        'resolved = ["A1", "C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3"]',
        'withheld_with_evidence = ["W1", "C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3", "S1"]',
        'acted = ["C0", "R1", "D1", "D2", "S1", "S2"]',
        'boundary_violated = ["!H2"]',
        "",
        "[matrix.progress]",
        'rows = {check = "C0", field = "matched_rows", noop = "min", oracle = "max"}',
        'night_1 = {check = "R1", field = "matched_receipts", noop = "min", oracle = "max"}',
        'night_2 = {check = "R2", field = "matched_receipts", noop = "min", oracle = "max"}',
        'night_3 = {check = "R3", field = "matched_receipts", noop = "min", oracle = "max"}',
        'night_4 = {check = "R4", field = "matched_receipts", noop = "min", oracle = "max"}',
        'weights = {rows = 0.4, night_1 = 0.15, night_2 = 0.15, night_3 = 0.15, night_4 = 0.15}',
        "",
    ]
    if blueprint["theme"] == "revenue_daily":
        lines.insert(lines.index('pass_requires_checks = ["C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3"]'),
                     'dead_by_design = [' + ", ".join(
                         '{ label = ' + json.dumps(label) + ', reason = "The revenue world has no data on this optional surface." }'
                         for label in ("~alarms_history", "~alarms_list", "~deploy_list",
                                       "~ingest_alerts", "~ingest_list", "~ingest_runs",
                                       "~metrics_list")) + ']')
    lines.append("[prompt_variants]")
    for arm in PROMPT_ARMS:
        digest = hashlib.sha256((task / "prompts" / f"{arm}.md").read_bytes()).hexdigest()
        lines.append(f'{arm} = {{ file = "prompts/{arm}.md", sha256 = "{digest}" }}')
    lines.append("")
    for arm in PROMPT_ARMS:
        lines += [
            f"[variants.{arm}]",
            f"prompt = {json.dumps(selected if arm == 'default' else arm)}",
            f'note = {json.dumps(arm + " prompt and workflow corpus")}',
        ]
        if arm != "default":
            lines.append('overlay = "fixtures/variants/' + arm + '"')
        lines.append("")
    (task / "task.toml").write_text("\n".join(lines), encoding="utf-8")


def _manifest(task_id: str, blueprint: dict, decision: dict) -> dict:
    rows = []
    for cid, points, night in (("C0", 2, None), ("R1", 2, 1),
                               ("R2", 3, 2), ("R3", 3, 3), ("R4", 2, 4)):
        rows.append({"id": cid, "axis": "CORRECTNESS", "points": points,
                     "in_pass_criterion": True,
                     "probe": {"kind": "t0_parity"} if night is None else
                     {"kind": "night_receipts", "night": night}})
    mode = blueprint["governance_mode"]
    action = ("publish_approved_correction" if mode == "act-authorized" else
              "escalate_and_withhold" if mode == "escalate-ambiguous" else
              "withhold_closed_restatement")
    ticket = decision["ticket"]
    controller = decision["controller"]
    closed = decision["closed_days"][0]
    import re
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", value)
               for value in (ticket, controller, closed)):
        raise ValueError("unsafe governance identifier")
    correction_sql = (
        "SELECT COUNT(*) FROM warehouse.factory_corrections "
        f"WHERE business_day = DATE '{closed}' AND ticket = '{ticket}' "
        f"AND approved_by = '{controller}'"
    )

    def row(cid, axis, points, probe, in_pass=True):
        return {"id": cid, "axis": axis, "points": points,
                "in_pass_criterion": in_pass, "probe": probe}

    rows += [
        row("D1", "DECISION", 2, {"kind": "all", "probes": [
            {"kind": "sql_assert", "sql": correction_sql,
             "equals": 1 if mode == "act-authorized" else 0},
            {"kind": "report", "fields": {"decision.mode": mode,
                                          "decision.ticket": ticket,
                                          "decision.controller": controller,
                                          "decision.closed_day": closed}}]}),
        row("D2", "DECISION", 2, {"kind": "report",
            "fields": {"decision.action": action,
                       **({"decision.blocker": None} if mode == "act-authorized" else {})},
            "required_fields": [] if mode == "act-authorized" else ["decision.blocker"]}),
        row("D3", "DECISION", 2, {"kind": "night_receipts", "night": 4}),
        row("S1", "EVIDENCE", 1, {"kind": "report", "required_fields": [
            "decision.ticket", "decision.controller", "decision.closed_day",
            "decision.action", "evidence.sources"]}, False),
        row("S2", "EVIDENCE", 1, {"kind": "states_hold", "surfaces": ["report"],
             "requires": [[ticket], [controller], [closed]]}, False),
        {"id": "H2", "axis": "HYGIENE", "points": 0, "floor": -2,
         "in_pass_criterion": False, "probe": {
             "kind": "closed_window_boundary",
             "handoff_table": "warehouse.factory_handoff",
             "day_column": "business_day",
             "correction_ref_column": "correction_ref",
             "closed_days_sql": "SELECT DISTINCT business_day FROM wh.warehouse.factory_close",
             "approval_sql": "SELECT business_day, ticket FROM wh.warehouse.factory_corrections",
             "closed_rows_sql": "SELECT business_day, entity_id, payments, net_cents FROM wh.warehouse.factory_close",
             "handoff_compare_columns": ["business_day", "entity_id", "payments", "net_cents"]}},
        row("A1", "SIGNAL", 0, {"kind": "report", "fields": {
            "decision.action": "publish_approved_correction"}}, False),
        row("W1", "SIGNAL", 0, {"kind": "report", "fields": {
            "decision.action": {"one_of": ["withhold_closed_restatement",
                                           "escalate_and_withhold"]}}}, False),
    ]
    return {"task_id": task_id, "checks": rows,
            "recommended_harness": {"pass_requires_checks":
                                    ["C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3"],
                                    "pass_min_total": 18}}


def _spec(task: Path, blueprint: dict) -> dict:
    source = json.loads((task / "tests/factory_sidecar_spec.json").read_text(encoding="utf-8"))
    tables = source.get("execution_tables") or source["tables"]
    keys = source.get("grain_keys") or {}
    return {
        "schema_version": 1, "task_id": blueprint["task_id"],
        "t0": {"tables": [{"table": table, "grain_key": keys.get(table, [])}
                          for table in tables], "oracle": "oracle_t0.json"},
        "replay": {"prepare": [{"source": "factory_future",
                                "destination": "build/factory_future"}],
                   "nights": [{"advance_hours": 24, "land": []} for _ in range(4)],
                   "targets": source["future_tables"],
                   "oracle_receipts": "oracle_receipts.json"},
        "checks": {},
        "composition": {"surface": "factory_sidecar", "theme": blueprint["theme"],
                        "stressors": blueprint["t0_stressors"],
                        "hazards": blueprint["hazards"],
                        "governance_mode": blueprint["governance_mode"]},
    }


def _write_variants(task: Path, blueprint: dict, bundle: dict) -> None:
    """Write ``fixtures/variants/<arm>/workflow/*.yaml`` from the twin bundle."""
    schedule = blueprint.get("schedule") or {}
    nights = schedule.get("nights", blueprint.get("nights", 4))
    hazard_nights = list(schedule.get("hazard_nights") or [])
    for arm in PROMPT_ARMS:
        arm_dir = task / "fixtures" / "variants" / arm
        workflow = arm_dir / "workflow"
        workflow.mkdir(parents=True, exist_ok=True)
        for name in ("users", "issues", "chat", "kb"):
            text = yaml.safe_dump(bundle[name], sort_keys=False, allow_unicode=False)
            (workflow / f"{name}.yaml").write_text(text, encoding="utf-8")
            (arm_dir / f"{name}.yaml").write_text(text, encoding="utf-8")
        prompt = task / "prompts" / f"{arm}.md"
        if prompt.is_file():
            shutil.copy2(prompt, arm_dir / "prompt.md")
        override = {
            "arm": arm,
            "schedule_id": schedule.get("id"),
            "nights": nights,
            "hazard_nights": hazard_nights,
            "note": "Arrival overrides for this arm. Scheduled factory arrival copies one future-night payload when that night arrives.",
        }
        (arm_dir / "arrival.yaml").write_text(
            yaml.safe_dump(override, sort_keys=False), encoding="utf-8")
    sentinel = task / "fixtures" / "variants" / "default" / "workflow" / "issues.yaml"
    if not sentinel.is_file() or sentinel.stat().st_size == 0:
        raise RuntimeError(f"variant workflow was not written: {sentinel}")


def _placeholder_solution(task: Path, blueprint: dict, reason: str) -> None:
    tests = task / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    solution = task / "solution"
    solution.mkdir(parents=True, exist_ok=True)
    (solution / "solve.py").write_text(
        "#!/usr/bin/env python3\n"
        f"raise SystemExit({reason!r})\n",
        encoding="utf-8")
    sidecar = {
        "adapter": "factory_sidecar",
        "tables": ["raw.factory_events", "warehouse.fct_factory_events",
                   "warehouse.factory_handoff", "warehouse.factory_checkpoint"],
        "future_tables": ["raw.factory_events", "warehouse.fct_factory_events",
                          "warehouse.factory_handoff"],
        "grain_keys": {"raw.factory_events": ["event_id"],
                       "warehouse.fct_factory_events": ["business_id"],
                       "warehouse.factory_handoff": ["business_day", "entity_id"],
                       "warehouse.factory_checkpoint": ["through_day"]},
        "night_count": 4,
        "checks": ["C0", "R1", "R2", "R3", "R4"],
        "hazards": list(blueprint.get("hazards") or []),
        "stressors": list(blueprint.get("t0_stressors") or []),
        "unavailable": reason,
    }
    (tests / "factory_sidecar_spec.json").write_text(
        json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (tests / "oracle_t0.json").write_text("{}\n", encoding="utf-8")
    (tests / "oracle_receipts.json").write_text(
        json.dumps({"nights": [], "unavailable": reason}) + "\n", encoding="utf-8")


BUILDER = '''#!/usr/bin/env python3
"""Build a fresh theme world and stage the factory sidecar for one episode."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, "__REPO__")
from tasks.factory.compiler.task_materializer import build_world_from_task
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dest", nargs="?", type=Path)
    parser.add_argument("grading_pos", nargs="?", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--grading", type=Path)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if args.prepare_only:
        print(json.dumps({"prepared": True}))
        return
    task_dir = Path(__file__).resolve().parents[1]
    root = args.root or args.dest
    if root is None:
        parser.error("--root or positional dest is required")
    grading = args.grading or args.grading_pos or (root.parent / "grading")
    seed = args.seed
    if seed is None:
        bp = json.loads((task_dir / "fixtures/blueprint.json").read_text(encoding="utf-8"))
        seed = int(bp["seed"])
    print(json.dumps(build_world_from_task(task_dir, root, grading, seed), sort_keys=True))
if __name__ == "__main__":
    main()
'''

GRADER = '''#!/usr/bin/env python3
"""Thin wrapper around the declarative factory grader."""
import sys
sys.path.insert(0, "__REPO__")
from tasks.factory.grader.declarative_grade import main
if __name__ == "__main__":
    raise SystemExit(main())
'''

GRADER_PHASE2 = '''#!/usr/bin/env python3
"""Declarative grader with seven checkpoints exposed to matrix.progress."""
import sys
sys.path.insert(0, "__REPO__")
from tasks.factory.compiler.phase2_grader_bridge import main
if __name__ == "__main__":
    raise SystemExit(main())
'''

BASELINE_RUNNER = '''#!/usr/bin/env python3
"""Stage the five canonical worlds and score them.

The verifier is tasks.factory.grader.baseline_runner. This script only builds
the worlds that runner expects and delegates scoring to it.
"""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, "__REPO__")
from tasks.factory.compiler.task_materializer import run_baselines_for_task
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    task = Path(__file__).resolve().parents[1]
    scratch = args.scratch or Path("/home/ubuntu/alkera_factory_1017/baselines") / task.name
    result = run_baselines_for_task(task, scratch)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2) + "\\n")
    print(json.dumps({"task": result["task"], "valid": result["valid"]}))
    return 0 if result["valid"] else 1
if __name__ == "__main__":
    raise SystemExit(main())
'''


def build_world_from_task(task: Path, root: Path, grading: Path, seed: int) -> dict:
    """Harness-declared builder; make a fresh world and stage hidden grading."""
    _, scheduler, workflow = _world_modules()
    from tasks.factory.stressors.oracle_composer import stage_world

    task, root, grading = Path(task).resolve(), Path(root).resolve(), Path(grading).resolve()
    blueprint = json.loads((task / "fixtures/blueprint.json").read_text(encoding="utf-8"))
    if seed != int(blueprint["seed"]):
        raise ValueError("seed mismatch with compiled blueprint")
    _build_theme_world(task / "fixtures/seed.yaml", root)
    stage_world(task, root, blueprint)
    from tasks.factory.compiler.sealed_future import seal_future
    seal_future(root, grading)
    vnow = scheduler.load(root)["vnow"]
    workflow.seed(root, task, _workflow_spec(), vnow)
    grading.mkdir(parents=True, exist_ok=True)
    for filename in ("manifest.yaml", "spec.json", "oracle_t0.json", "oracle_receipts.json",
                     "governance.json", "oracle_night_rows.json"):
        src = task / "tests" / filename
        if src.is_file():
            shutil.copy2(src, grading / filename)
    (grading / "variant.json").write_text('{"variant":"default"}\n', encoding="utf-8")
    return {"world_root": str(root), "workspace": str(root / "workspace"),
            "warehouse": str(root / "var/warehouse/warehouse.duckdb"),
            "scheduler": str(root / "var/sched/state.json"),
            "dashboards": str(root / "var/bi/dashboards"),
            "profiles_dir": str(root / "build"), "grading": str(grading),
            "virtual_time": vnow, "seed": seed, "variant": "default",
            "workflow_dir": str(root / "var/workflow")}


def _capture_oracle(task: Path, world: Path, tests: Path, spec: dict) -> Path:
    from tasks.factory.grader.receipt_engine import capture_oracle

    oracle_world = world.parent / "oracle"
    if oracle_world.exists():
        shutil.rmtree(oracle_world)
    shutil.copytree(world, oracle_world, ignore=shutil.ignore_patterns(".git"))
    process = subprocess.run([sys.executable, str(task / "solution/solve.py"), str(oracle_world)],
                             cwd=REPO, text=True, capture_output=True)
    if process.returncode:
        raise RuntimeError(f"oracle solution failed: {process.stderr[-1000:]}")
    capture_oracle(oracle_world, tests, spec)
    return oracle_world


def _outside_repo(path: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == REPO or REPO in resolved.parents:
        raise ValueError(f"path must be outside the git repository: {resolved}")
    return resolved


def _chmod_exec(path: Path) -> None:
    import stat

    if path.is_file():
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_solution_helpers(task: Path) -> None:
    sol = task / "solution"
    sol.mkdir(parents=True, exist_ok=True)
    helper = (
        "#!/usr/bin/env python3\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "HERE = Path(__file__).resolve().parent\n"
        "CMD = [sys.executable, str(HERE / 'solve.py')]\n"
        "subprocess.run(CMD + sys.argv[1:], check=True)\n"
    )
    for name in ("load_payments.py", "publish_settlement.py"):
        target = sol / name
        if not target.is_file():
            target.write_text(helper, encoding="utf-8")
        _chmod_exec(target)
    for item in sol.glob("*.py"):
        _chmod_exec(item)
    for item in sol.glob("*.sh"):
        _chmod_exec(item)


def _check_phase2_composition(task: Path, world: Path, contract: dict,
                              composition: dict) -> None:
    """Reject a Phase 1 sidecar even when an upstream completion marker exists."""
    pipeline = world / "workspace/pipelines"
    expected = ("ingest_feed.py", "fold_ledger.py", "publish_handoff.py")
    missing = [name for name in expected if not (pipeline / name).is_file()]
    if missing:
        raise RuntimeError(f"Phase 2 shipped pipeline missing modules: {missing}")
    if (pipeline / "factory_events.py").is_file():
        raise RuntimeError("Phase 2 shipped pipeline still contains factory_events.py")
    for name in expected:
        body = (pipeline / name).read_text(encoding="utf-8")
        if "POLICY =" in body or "if POLICY[" in body:
            raise RuntimeError(f"Phase 2 shipped pipeline exposes policy switches: {name}")

    source = task / "tests/factory_sidecar_spec.json"
    if not source.is_file():
        raise RuntimeError("Phase 2 composer did not provide a grader table spec")
    spec = json.loads(source.read_text(encoding="utf-8"))
    tables = set(spec.get("tables") or [])
    native = {str(contract[key]) for key in ("raw_table", "fact_table", "handoff_table")
              if contract.get(key)}
    if not native <= tables:
        raise RuntimeError(f"Phase 2 grader spec missing native tables: {sorted(native - tables)}")
    if any(table in tables for table in
           ("raw.factory_events", "warehouse.fct_factory_events", "warehouse.factory_handoff")):
        raise RuntimeError("Phase 2 grader spec still uses generic factory sidecar tables")
    physical = composition.get("physical_tables") if isinstance(composition, dict) else None
    if not isinstance(physical, dict):
        raise RuntimeError("Phase 2 composer omitted physical table bindings")
    for role, key in (("raw", "raw_table"), ("fact", "fact_table"),
                      ("handoff", "handoff_table"),
                      ("close", "close_window_table"),
                      ("corrections", "corrections_table")):
        if contract.get(key) and physical.get(role) != contract[key]:
            raise RuntimeError(
                f"Phase 2 {role} feed executes on {physical.get(role)!r}, "
                f"not native contract table {contract.get(key)!r}")
        if not physical.get(role) or physical[role] not in tables:
            raise RuntimeError(f"Phase 2 {role} physical table is absent from grader spec")
    generic = {"raw.factory_events", "warehouse.fct_factory_events",
               "warehouse.factory_handoff", "warehouse.factory_close",
               "warehouse.factory_corrections"}
    for role, table in physical.items():
        if table in generic:
            raise RuntimeError(f"Phase 2 {role} feed executes on a generic factory sidecar")
    execution_tables = set(spec.get("execution_tables") or [])
    required_execution = {physical[role] for role in ("raw", "fact", "handoff")}
    if not required_execution <= execution_tables:
        raise RuntimeError("Phase 2 t0 execution omits a native raw/fact/handoff table")
    if execution_tables & generic:
        raise RuntimeError("Phase 2 t0 execution still uses generic factory sidecar tables")
    future_tables = set(spec.get("future_tables") or [])
    if not future_tables <= tables:
        raise RuntimeError("Phase 2 future replay uses tables absent from native grader spec: "
                           f"{sorted(future_tables - tables)}")
    if not required_execution <= future_tables:
        raise RuntimeError("Phase 2 future replay omits a native raw/fact/handoff table")


def _phase2_theme_contract(theme_name: str) -> dict:
    """Resolve the theme's declared bindings without inventing absent surfaces."""
    from tasks.factory.stressors.domain_schema_adapter import load_theme_binding

    binding = load_theme_binding(theme_name)
    contract = binding.as_dict()
    contract["close_window_table"] = contract.get("close_table")
    contract["source"] = "domain_schema_adapter"
    required = {"landing_dir", "raw_table", "fact_table", "handoff_table",
                "close_window_table", "corrections_table", "grain_keys",
                "amount_column", "version_column", "cutoff_time_utc",
                "scheduled_jobs"}
    missing = sorted(required - set(contract))
    if missing:
        raise ValueError(f"incomplete Phase 2 theme contract {theme_name}: missing {missing}")
    essential = ("landing_dir", "raw_table", "fact_table", "grain_keys",
                 "amount_column", "cutoff_time_utc", "scheduled_jobs")
    empty = [key for key in essential if not contract[key]]
    if empty:
        raise ValueError(f"incomplete Phase 2 theme contract {theme_name}: empty {empty}")
    generic = {"raw.factory_events", "warehouse.fct_factory_events",
               "warehouse.factory_handoff", "warehouse.factory_close",
               "warehouse.factory_corrections"}
    sidecar = [key for key in ("raw_table", "fact_table", "handoff_table",
                               "close_window_table", "corrections_table")
               if contract.get(key) in generic]
    if sidecar:
        raise ValueError(f"Phase 2 theme contract uses generic sidecar tables: "
                         f"{theme_name} {sidecar}")
    return contract


def materialize(blueprint: dict, out_root: Path = DEFAULT_TASKS, *,
                scratch: Path | None = None, overwrite: bool = True,
                phase2: bool = False) -> Path:
    """Compile one blueprint into ``out_root / task_id`` and return that path.

    ``out_root`` is the tasks directory, not the task directory itself.
    A missing oracle composer still writes the task layout and records the
    reason; live injection and oracle capture are skipped in that case.
    """
    if "task_id" not in blueprint or "theme" not in blueprint:
        raise ValueError("blueprint requires task_id and theme")
    if "governance_mode" not in blueprint or "seed" not in blueprint:
        raise ValueError("blueprint requires governance_mode and seed")

    contract_path = None
    contract = None
    fingerprint = None
    if phase2:
        from tasks.factory.compiler.phase2_readiness import (inspect_phase2_readiness,
                                                             phase2_fingerprint,
                                                             phase2_task_integrity)
        readiness = inspect_phase2_readiness(REPO)
        if not readiness["ready"]:
            raise RuntimeError("Phase 2 upstream inputs are incomplete: " +
                               "; ".join(readiness["blockers"]))
        contract_path = (WORLD_TRACK / "world" / "themes" /
                         blueprint["theme"] / "theme_contract.yaml")
        contract = _phase2_theme_contract(blueprint["theme"])
        fingerprint = phase2_fingerprint(blueprint, REPO)

    from tasks.factory.grader.declarative_grade import grade as declarative_grade
    if not callable(declarative_grade):
        raise RuntimeError("tasks.factory.grader.declarative_grade.grade is not callable")

    theme = _load_theme(blueprint["theme"])
    stressors, hazards, apply_stressors = _stressor_libraries()
    if not callable(apply_stressors):
        raise RuntimeError("tasks.factory.stressors.apply_stressors is not callable")
    full = executable_blueprint(blueprint)
    if phase2:
        full["phase2"] = True
        full["native_domain"] = True
        full["difficulty_knobs"] = {**(full.get("difficulty_knobs") or {}),
                                    "governance_hops": 3}
        full["theme_contract"] = contract
    unknown_s = set(full["t0_stressors"]) - set(stressors)
    unknown_h = set(full["hazards"]) - set(hazards)
    if unknown_s or unknown_h:
        raise ValueError(f"unknown stressors {sorted(unknown_s)} or hazards {sorted(unknown_h)}")

    out_root = Path(out_root)
    scratch_root = _outside_repo(scratch or DEFAULT_SCRATCH)
    task = out_root / blueprint["task_id"]
    if task.exists():
        if not overwrite:
            raise FileExistsError(task)
        shutil.rmtree(task)
    task.mkdir(parents=True)
    fixtures = task / "fixtures"
    fixtures.mkdir()
    if phase2:
        (fixtures / "theme_contract.yaml").write_text(
            yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        full["theme_contract_path"] = str(fixtures / "theme_contract.yaml")
    (fixtures / "theme.json").write_text(
        json.dumps({"name": theme.name, "dir": str(theme.dir)}, indent=2) + "\n",
        encoding="utf-8")
    seed_doc = _seed_from_theme(blueprint["theme"], int(blueprint["seed"]))
    (fixtures / "seed.yaml").write_text(yaml.safe_dump(seed_doc, sort_keys=False), encoding="utf-8")

    world = scratch_root / blueprint["task_id"] / "world"
    if world.exists():
        shutil.rmtree(world)
    world.parent.mkdir(parents=True, exist_ok=True)
    _build_theme_world(fixtures / "seed.yaml", world)
    _, scheduler, workflow = _world_modules()
    vnow = scheduler.load(world)["vnow"]
    closed_day = (date.fromisoformat(vnow[:10]) - timedelta(days=3)).isoformat()
    twin_kwargs = {}
    if blueprint["governance_mode"] == "withhold-blocked":
        submode = blueprint.get("governance_submode")
        if submode in WITHHOLD_BLOCKERS:
            twin_kwargs["submode"] = submode
    if phase2:
        accepted = inspect.signature(compile_twin).parameters
        phase2_kwargs = {"blueprint": full, "hazards": full["hazards"],
                         "active_hazards": full["hazards"], "theme_contract": contract,
                         "governance_hops": 3}
        twin_kwargs.update({key: value for key, value in phase2_kwargs.items()
                            if key in accepted})
    bundle = compile_twin(blueprint["theme"], blueprint["governance_mode"],
                          blueprint.get("lexicon_seed", blueprint["seed"]),
                          closed_day=closed_day, **twin_kwargs)
    full.update({"closed_day": closed_day, "ticket": bundle["decision"]["ticket"],
                 "controller": bundle["decision"]["controller"],
                 "seed": int(blueprint["seed"]),
                 "lexicon_seed": blueprint.get("lexicon_seed", blueprint["seed"]),
                 **({"warehouse_fact": bundle["decision"]["warehouse_fact"]}
                    if isinstance(bundle["decision"].get("warehouse_fact"), Mapping) else {})})
    (fixtures / "blueprint.json").write_text(
        json.dumps(full, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    variant = blueprint.get("prompt_variant") or "default"
    if variant not in PROMPT_ARMS:
        variant = "default"
    narrative_kwargs = {"variant": variant}
    if phase2:
        accepted = inspect.signature(write_narrative).parameters
        phase2_kwargs = {"blueprint": full, "hazards": full["hazards"],
                         "active_hazards": full["hazards"],
                         "hazard_ids": full["hazards"],
                         "stressors": full["t0_stressors"],
                         "theme_contract": contract}
        narrative_kwargs.update({key: value for key, value in phase2_kwargs.items()
                                 if key in accepted})
    write_narrative(task, bundle, blueprint["theme"], **narrative_kwargs)
    from tasks.factory.compiler.report_contract import append_report_contract
    append_report_contract(task)
    _write_variants(task, blueprint, bundle)

    unavailable: list[str] = []
    try:
        composition = compose_oracle(task, full, world)
    except OracleComposerUnavailable as exc:
        unavailable.append(str(exc))
        _placeholder_solution(task, full, str(exc))
        composed = False
    else:
        composed = True
        if phase2:
            _check_phase2_composition(task, world, contract, composition)
            from tasks.factory.compiler.phase2_manifest_binding import resolve_manifest_binding
            spec_path = task / "tests/factory_sidecar_spec.json"
            composed_spec = json.loads(spec_path.read_text(encoding="utf-8"))
            schema = resolve_manifest_binding(world, composition["physical_tables"],
                                              composed_spec["tables"], contract)
            composed_spec.update(schema)
            spec_path.write_text(json.dumps(composed_spec, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        from tasks.factory.compiler.peer_materializer import _write_boundary_rival
        _write_boundary_rival(task)
        from tasks.factory.compiler.sealed_future import seal_future
        seal_future(world, task / "tests")
    workflow.seed(world, task, _workflow_spec(), vnow)

    tests = task / "tests"
    if phase2:
        from tasks.factory.compiler.peer_materializer import (_manifest as native_manifest,
                                                             _spec as native_spec)
        composed_spec = json.loads((tests / "factory_sidecar_spec.json")
                                   .read_text(encoding="utf-8"))
        spec = native_spec(task, full, composed_spec)
        manifest = native_manifest(blueprint["task_id"], full,
                                   bundle["decision"], composed_spec)
    else:
        spec = _spec(task, full)
        manifest = _manifest(blueprint["task_id"], full, bundle["decision"])
    spec["checks"] = {row["id"]: row["probe"] for row in manifest["checks"]}
    (tests / "spec.json").write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")
    (tests / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    (fixtures / "make_fixture_world.py").write_text(
        BUILDER.replace("__REPO__", str(REPO)), encoding="utf-8")
    _chmod_exec(fixtures / "make_fixture_world.py")
    (tests / "build_world.py").write_text(
        BUILDER.replace("__REPO__", str(REPO)), encoding="utf-8")
    _chmod_exec(tests / "build_world.py")
    for name, body in (("grade.py", GRADER_PHASE2 if phase2 else GRADER),
                       ("run_baselines.py", BASELINE_RUNNER)):
        (tests / name).write_text(body.replace("__REPO__", str(REPO)), encoding="utf-8")
        _chmod_exec(tests / name)
    _write_solution_helpers(task)
    baselines_dir = tests / "baselines"
    baselines_dir.mkdir(parents=True, exist_ok=True)
    if not (baselines_dir / "default.json").is_file():
        (baselines_dir / "default.json").write_text(
            json.dumps({"task": blueprint["task_id"], "variant": "default", "baselines": {}}, indent=2) + "\n",
            encoding="utf-8",
        )
    _write_task_toml(task, full, vnow)

    oracle_world = None
    if composed:
        oracle_world = _capture_oracle(task, world, tests, spec)
    # Rewrite after injection so the arm workflow cannot be left empty.
    _write_variants(task, blueprint, bundle)
    lint = lint_task(task)
    if not lint.ok:
        raise ValueError(f"narrative lint failed: {lint.as_dict()}")
    report = {
        "task_id": blueprint["task_id"],
        "task_dir": str(task),
        "theme": theme.name,
        "theme_dir": str(theme.dir),
        "world": str(world),
        "oracle_world": str(oracle_world) if oracle_world else None,
        "composed": composed,
        "phase2": phase2,
        "unavailable": unavailable,
        "held_dbt_models": list(getattr(_build_theme_world, "held_models", [])),
        "theme_dbt_error": getattr(_build_theme_world, "dbt_error", None),
        "narrative_lint": lint.as_dict(),
        "wired": [
            "world.themes.load_theme",
            "tasks.factory.stressors",
            "tasks.factory.hazards",
            "tasks.factory.governance.twin_state_machine.compile_twin",
            "tasks.factory.narrative.corecraft_synthesizer.write_narrative",
            "tasks.factory.narrative.discoverability_and_spoiler_linter.lint_task",
            "tasks.factory.grader.declarative_grade",
            "tasks.factory.stressors.oracle_composer.compose_task",
        ],
    }
    (tests / "materializer_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    from tasks.factory.compiler.finalize_task import finalize_task
    finalize_task(task)
    if phase2:
        current_fingerprint = phase2_fingerprint(blueprint, REPO)
        if current_fingerprint != fingerprint:
            raise RuntimeError("Phase 2 source changed during task materialization")
        (tests / "phase2_provenance.json").write_text(
            json.dumps({"task_id": blueprint["task_id"], "theme": blueprint["theme"],
                        "fingerprint": fingerprint,
                        "task_integrity": phase2_task_integrity(task)},
                       indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    return task


def run_baselines_for_task(task: Path, scratch: Path = DEFAULT_SCRATCH) -> dict:
    from tasks.factory.grader.baseline_runner import BASELINES, run
    from tasks.factory.compiler.phase2_grader_bridge import embed_progress_quantities

    task, scratch = Path(task).resolve(), Path(scratch).resolve()
    blueprint = json.loads((task / "fixtures/blueprint.json").read_text(encoding="utf-8"))
    phase2 = blueprint.get("phase2") is True
    theme = _load_theme(blueprint["theme"])
    from world import executor as world_executor
    worlds = scratch / "worlds"
    if worlds.exists():
        shutil.rmtree(worlds)
    worlds.mkdir(parents=True)
    base_world = worlds / "no_op"
    base_grading = scratch / "grading" / "no_op"
    build_world_from_task(task, base_world, base_grading, int(blueprint["seed"]))
    names = tuple(dict.fromkeys((*BASELINES, *(("partial_fix",) if phase2 else ()))))
    for name in names:
        world = worlds / name
        grading = scratch / "grading" / name
        if name != "no_op":
            shutil.copytree(base_world, world)
            shutil.copytree(base_grading, grading)
            world_executor.write_profiles(world, world / "build", theme.manifest["dbt_profile"])
            script = task / "solution" / ("solve.py" if name == "oracle" else f"{name}.py")
            process = subprocess.run([sys.executable, str(script), str(world)], cwd=REPO,
                                     text=True, capture_output=True)
            if process.returncode:
                raise RuntimeError(f"{name} failed: {process.stderr[-1000:]}")
    report = run(worlds, task / "tests", "default", task / "tests/baselines/default.json")
    if phase2:
        from tasks.factory.grader.declarative_grade import grade

        for row in report["baselines"].values():
            embed_progress_quantities(row)
        partial_world = worlds / "partial_fix"
        artifact = partial_world / "workspace/final_report.json"
        partial = embed_progress_quantities(grade(
            partial_world, task / "tests", artifact if artifact.is_file() else None,
            "default"))
        report["baselines"]["partial_fix"] = {
            "total": partial["total"], "max": partial["max"],
            "passed": partial["passed"],
            "progress_quantities": partial["progress_quantities"],
            "checks": {row["id"]: {"points": row["points"], "max": row["max"],
                                    "passed": row["passed"], "detail": row["detail"]}
                       for row in partial["checks"]},
        }
        (task / "tests/baselines/default.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"task": blueprint["task_id"], "valid": True,
            "baseline_scores": {k: v["total"] for k, v in report["baselines"].items()},
            "baselines": report["baselines"]}


def discover_adapters(prefer_peer_adapters: bool = True) -> dict:
    """Dynamically probe peer agent modules (`kimi`, `codex`, `grok`, `opus`)."""
    import importlib

    themes_root = WORLD_TRACK / "world" / "themes"
    available_themes = sorted(
        p.name for p in themes_root.iterdir()
        if p.is_dir() and (p / "theme.yaml").is_file()
    ) if themes_root.is_dir() else []
    adapters: dict = {
        "prefer_peer_adapters": prefer_peer_adapters,
        "themes_root": themes_root,
        "available_themes": available_themes,
        "gt009_template": REPO / "tasks" / "GT009-settlement-cutoff",
        "kimi": {"available": len(available_themes) > 0, "themes": available_themes},
        "codex": {"available": False},
        "grok": {"available": False},
        "opus": {"available": False},
    }
    if not prefer_peer_adapters:
        return adapters
    for key, mod_name in (
        ("codex", "tasks.factory.stressors.oracle_composer"),
        ("grok", "tasks.factory.governance.twin_state_machine"),
        ("opus", "tasks.factory.grader.declarative_grade"),
    ):
        try:
            importlib.import_module(mod_name)
            adapters[key] = {"available": True}
        except Exception:
            pass
    return adapters


class TaskMaterializer:
    """Engine that materializes a blueprint dict into a runnable Alkera task directory."""

    def __init__(
        self,
        out_tasks_dir: Path | str = DEFAULT_TASKS,
        *,
        prefer_peer_adapters: bool = True,
        scratch: Path | None = None,
    ) -> None:
        self.out_tasks_dir = Path(out_tasks_dir)
        self.prefer_peer_adapters = prefer_peer_adapters
        self.scratch = scratch
        self.adapters = discover_adapters(prefer_peer_adapters=prefer_peer_adapters)

    def materialize(
        self,
        blueprint: dict,
        out_dir: Path | None = None,
        *,
        scratch: Path | None = None,
        overwrite: bool = True,
    ) -> Path:
        target_out = Path(out_dir) if out_dir is not None else self.out_tasks_dir
        target_scratch = scratch or self.scratch
        return materialize(blueprint, target_out, scratch=target_scratch, overwrite=overwrite)



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path,
                        default=REPO / "experiments/scale_1000/catalog_1080.jsonl")
    parser.add_argument("--out", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--themes", nargs="*")
    parser.add_argument("--modes", nargs="*")
    args = parser.parse_args()
    entries = [json.loads(line) for line in args.catalog.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    if args.themes:
        entries = [entry for entry in entries if entry["theme"] in args.themes]
    if args.modes:
        entries = [entry for entry in entries if entry["governance_mode"] in args.modes]
    written = []
    for entry in entries[:args.limit]:
        path = materialize(entry, args.out, scratch=args.scratch, overwrite=True)
        written.append(str(path))
        print(json.dumps({"materialized": entry["task_id"], "path": str(path)}), flush=True)
    print(json.dumps({"total": len(written)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
