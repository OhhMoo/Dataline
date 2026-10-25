"""Materialize one factory blueprint into an Alkera task directory.

Public entry point::

    materialize(blueprint: dict, out_root: Path) -> Path

Wires theme loading, stressor and hazard injection, the governance twin, the
narrative writer, spoiler lint, and the declarative grader. It does not
reimplement those pieces.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Collection, Mapping
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
# Phase 3 pilot delta. FT03/FT15 and these task stems stay sealed.
PHASE3_CERTIFIED_TOPOLOGIES = frozenset({"FT03", "FT15"})
PHASE3_CERTIFIED_TASK_IDS = frozenset({
    "FT0091", "FT0332", "FT0485", "FT0677", "FT0963",
})
PHASE3_MUTATED_TOPOLOGIES = frozenset({
    "FT01", "FT02", "FT04", "FT05", "FT06", "FT07", "FT08",
    "FT09", "FT10", "FT11", "FT12", "FT13", "FT14",
})
PHASE3_DELTA_SCRATCH = Path("/home/ubuntu/alkera_factory_1017/phase3_delta_scratch")
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


DEFAULT_SIDECAR_CONTRACT: dict[str, object] = {
    "raw_table": "raw.factory_events",
    "fact_table": "warehouse.fct_factory_events",
    "handoff_table": "warehouse.factory_handoff",
    "close_window_table": "warehouse.factory_close",
    "close_table": "warehouse.factory_close",
    "corrections_table": "warehouse.factory_corrections",
    "checkpoint_table": "warehouse.factory_checkpoint",
    "landing_dir": "data/factory_feed",
    "day_column": "business_day",
    "entity_column": "entity_id",
    "count_column": "payments",
    "amount_column": "net_cents",
    "version_column": "version",
    "correction_ref_column": "correction_ref",
    "grain_keys": ["business_day", "entity_id"],
    "cutoff_time_utc": "23:30",
    "scheduled_jobs": ["factory_arrive", "factory_load", "factory_publish", "factory_close"],
}


def _default_theme_contract(theme_name: str) -> dict:
    contract = {
        k: (list(v) if isinstance(v, list) else v)
        for k, v in DEFAULT_SIDECAR_CONTRACT.items()
    }
    contract["theme"] = str(theme_name)
    contract["source"] = "default_sidecar"
    return contract


def _merge_theme_contract(base: dict, override: Mapping | None, *, source: str) -> dict:
    merged = dict(base)
    if not isinstance(override, Mapping):
        return merged
    for key, value in override.items():
        if value is not None:
            merged[str(key)] = value
    close_win = override.get("close_window_table")
    close_tbl = override.get("close_table")
    if close_tbl is not None:
        merged["close_table"] = close_tbl
        if close_win is None:
            merged["close_window_table"] = close_tbl
    elif close_win is not None:
        merged["close_window_table"] = close_win
        merged["close_table"] = close_win
    merged["source"] = source
    return merged


def _coerce_contract_mapping(obj: object) -> dict | None:
    if isinstance(obj, Mapping):
        return dict(obj)
    for method_name in ("to_dict", "as_dict"):
        method = getattr(obj, method_name, None)
        if callable(method):
            res = method()
            if isinstance(res, Mapping):
                return dict(res)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return None


def _call_domain_schema_adapter(theme_name: str, theme_dir: Path | None = None) -> dict | None:
    try:
        mod = importlib.import_module("tasks.factory.stressors.domain_schema_adapter")
    except ImportError:
        return None
    for fn_name in (
        "load_theme_contract",
        "get_theme_contract",
        "adapt_theme_schema",
        "resolve_theme_contract",
        "domain_schema_for_theme",
        "get_domain_schema",
        "load_domain_schema",
    ):
        fn = getattr(mod, fn_name, None)
        if not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
            kwargs = {}
            if theme_dir is not None and ("theme_dir" in sig.parameters or has_var_kw):
                kwargs["theme_dir"] = Path(theme_dir)
            result = fn(theme_name, **kwargs)
        except TypeError:
            try:
                result = fn(theme_name)
            except Exception:
                continue
        except Exception:
            continue
        mapped = _coerce_contract_mapping(result)
        if mapped:
            return mapped
    for attr_name in ("THEME_CONTRACTS", "DOMAIN_SCHEMAS", "CONTRACTS"):
        registry = getattr(mod, attr_name, None)
        if isinstance(registry, Mapping) and theme_name in registry:
            mapped = _coerce_contract_mapping(registry[theme_name])
            if mapped:
                return mapped
    return None


def _load_theme_contract(theme_name: str, theme_dir: Path | None = None) -> dict:
    """Load native theme contract from YAML or domain_schema_adapter, with sidecar fallback."""
    contract = _default_theme_contract(theme_name)
    candidates: list[Path] = []
    if theme_dir is not None:
        base_dir = Path(theme_dir)
        candidates.append(base_dir / "theme_contract.yaml")
        candidates.append(base_dir / str(theme_name) / "theme_contract.yaml")
    else:
        candidates.append(WORLD_TRACK / "world" / "themes" / str(theme_name) / "theme_contract.yaml")

    yaml_path = next((path for path in candidates if path.is_file()), None)
    if yaml_path is not None:
        loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if isinstance(loaded, Mapping):
            adapter_data = _call_domain_schema_adapter(theme_name, theme_dir=theme_dir)
            if adapter_data:
                contract = _merge_theme_contract(contract, adapter_data, source="domain_schema_adapter")
            contract = _merge_theme_contract(contract, loaded, source="theme_contract_yaml")
            contract["theme"] = str(loaded.get("theme") or theme_name)
            return contract

    adapter_data = _call_domain_schema_adapter(theme_name, theme_dir=theme_dir)
    if adapter_data:
        contract = _merge_theme_contract(contract, adapter_data, source="domain_schema_adapter")
        contract["theme"] = str(adapter_data.get("theme") or theme_name)
        return contract

    return contract


def _extract_code(item: object, *keys: str) -> str:
    if isinstance(item, Mapping):
        for key in keys:
            if item.get(key):
                return str(item[key]).split("_", 1)[0]
    return str(item).split("_", 1)[0]


def executable_blueprint(blueprint: dict) -> dict:
    """Copy a catalog record into the short codes the injector accepts.

    Catalog rows store authoring names such as ``S01_checkpoint_...`` and may
    also carry ``t0_stressor_codes`` / ``hazards`` as ``S01`` / ``H01``.
    Preserves 1..3 stressors and 1..3 hazards, and attaches ``theme_contract``.
    """
    full = dict(blueprint)
    stress_from_codes = [
        _extract_code(item, "stressor_id", "id", "name", "code")
        for item in (blueprint.get("t0_stressor_codes") or [])
    ]
    stress_from_names = [
        _extract_code(item, "stressor_id", "id", "name", "code")
        for item in (blueprint.get("t0_stressors") or blueprint.get("stressors") or [])
    ]
    stress_codes = stress_from_names if len(stress_from_names) >= len(stress_from_codes) and stress_from_names else stress_from_codes
    full["t0_stressors"] = list(stress_codes[:3])
    full["t0_stressor_codes"] = list(full["t0_stressors"])

    hazards_direct = [
        _extract_code(item, "hazard_id", "id", "name", "code")
        for item in (blueprint.get("hazards") or [])
    ]
    hazards_rich = [
        _extract_code(item, "hazard_id", "id", "name", "code")
        for item in (blueprint.get("r_hazards") or blueprint.get("hazard_pair") or [])
    ]
    codes = hazards_rich if len(hazards_rich) > len(hazards_direct) else (hazards_direct or hazards_rich)
    full["hazards"] = list(codes[:3])
    if not (1 <= len(full["t0_stressors"]) <= 3) or not (1 <= len(full["hazards"]) <= 3):
        raise ValueError("blueprint requires at least one t0 stressor and one hazard")

    existing_contract = blueprint.get("theme_contract")
    if isinstance(existing_contract, Mapping):
        base_theme = str(blueprint.get("theme") or existing_contract.get("theme") or "")
        full["theme_contract"] = _merge_theme_contract(
            _default_theme_contract(base_theme),
            existing_contract,
            source=str(existing_contract.get("source") or "blueprint"),
        )
    elif blueprint.get("theme"):
        full["theme_contract"] = _load_theme_contract(str(blueprint["theme"]))
    return full


def _bind_composer_runtime() -> None:
    """Bind DuckDB onto the oracle composer.

    ``_seed_governance_warehouse_fact`` opens the warehouse with ``duckdb``,
    but that module never imports it. The compiler supplies the binding so the
    governance fact seed runs. The stressor source stays untouched.
    """
    import duckdb
    from tasks.factory.stressors import oracle_composer

    if getattr(oracle_composer, "duckdb", None) is None:
        oracle_composer.duckdb = duckdb


def compose_oracle(task_dir: Path, blueprint: dict, theme_world_dir: Path) -> dict:
    """Call the oracle composer. Raise a precise error when it is not installed."""
    _bind_composer_runtime()
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
    manifest_path = source.with_name("theme.yaml")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
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
    ]
    lines.append("[prompt_variants]")
    for arm in PROMPT_ARMS:
        lines.append(f'{arm} = {{ file = "prompts/{arm}.md" }}')
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


_SCHEMA_OVERRIDE_KEYS: tuple[str, ...] = (
    "corrections_table",
    "handoff_table",
    "close_table",
    "close_window_table",
    "day_column",
    "entity_column",
    "count_column",
    "amount_column",
    "correction_ref_column",
    "handoff_compare_columns",
    "corrections_day_column",
    "close_day_column",
    "close_compare_columns",
)


def _is_unoverridden_phase1_sidecar(sidecar_spec: Mapping | None) -> bool:
    if not isinstance(sidecar_spec, Mapping):
        return False
    if any(sidecar_spec.get(k) is not None for k in _SCHEMA_OVERRIDE_KEYS):
        return False
    if isinstance(sidecar_spec.get("theme_contract"), Mapping):
        return False
    adapter = sidecar_spec.get("adapter")
    tables = list(sidecar_spec.get("execution_tables") or sidecar_spec.get("tables") or [])
    return adapter == "factory_sidecar" and "warehouse.factory_handoff" in tables


def _resolve_manifest_schema(blueprint: Mapping, sidecar_spec: Mapping | None = None) -> dict[str, object]:
    schema: dict[str, object] = {
        "corrections_table": "warehouse.factory_corrections",
        "handoff_table": "warehouse.factory_handoff",
        "close_table": "warehouse.factory_close",
        "day_column": "business_day",
        "entity_column": "entity_id",
        "count_column": "payments",
        "amount_column": "net_cents",
        "correction_ref_column": "correction_ref",
    }
    sources: list[Mapping] = []
    if not _is_unoverridden_phase1_sidecar(sidecar_spec):
        bp_contract = blueprint.get("theme_contract")
        if isinstance(bp_contract, Mapping):
            sources.append(bp_contract)
        if isinstance(sidecar_spec, Mapping):
            nested_contract = sidecar_spec.get("theme_contract")
            if isinstance(nested_contract, Mapping):
                sources.append(nested_contract)
            sources.append(sidecar_spec)
    for src in sources:
        for key in (
            "corrections_table",
            "handoff_table",
            "day_column",
            "entity_column",
            "count_column",
            "amount_column",
            "correction_ref_column",
            "corrections_day_column",
            "close_day_column",
        ):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                schema[key] = val.strip()
        close_val = src.get("close_table") or src.get("close_window_table")
        if isinstance(close_val, str) and close_val.strip():
            schema["close_table"] = close_val.strip()
        compare_cols = src.get("handoff_compare_columns")
        if isinstance(compare_cols, (list, tuple)) and compare_cols:
            schema["handoff_compare_columns"] = [str(c) for c in compare_cols]
        if "close_compare_columns" in src:
            close_cols = src.get("close_compare_columns")
            schema["close_compare_columns"] = (
                [str(c) for c in close_cols] if isinstance(close_cols, (list, tuple))
                and close_cols else None)
    if "handoff_compare_columns" not in schema:
        cols: list[str] = []
        for col in (
            str(schema["day_column"]),
            str(schema["entity_column"]),
            str(schema["count_column"]),
            str(schema["amount_column"]),
        ):
            if col not in cols:
                cols.append(col)
        schema["handoff_compare_columns"] = cols
    return schema


def _wh_qualified(table_name: str) -> str:
    return table_name if table_name.startswith("wh.") else f"wh.{table_name}"


def _manifest(task_id: str, blueprint: dict, decision: dict,
              sidecar_spec: dict | None = None) -> dict:
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
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", value)
               for value in (ticket, controller, closed)):
        raise ValueError("unsafe governance identifier")

    schema = _resolve_manifest_schema(blueprint, sidecar_spec)
    corrections_table = str(schema["corrections_table"])
    handoff_table = str(schema["handoff_table"])
    close_table = str(schema["close_table"])
    day_column = str(schema["day_column"])
    corrections_day_column = str(schema.get("corrections_day_column") or day_column)
    close_day_column = str(schema.get("close_day_column") or day_column)
    correction_ref_column = str(schema["correction_ref_column"])
    compare_columns = list(schema["handoff_compare_columns"])
    wh_close = _wh_qualified(close_table)
    wh_corrections = _wh_qualified(corrections_table)

    correction_sql = (
        f"SELECT COUNT(*) FROM {corrections_table} "
        f"WHERE {corrections_day_column} = DATE '{closed}' AND ticket = '{ticket}' "
        f"AND approved_by = '{controller}'"
    )

    def row(cid, axis, points, probe, in_pass=True):
        return {"id": cid, "axis": axis, "points": points,
                "in_pass_criterion": in_pass, "probe": probe}

    boundary_probe = {
        "kind": "closed_window_boundary",
        "handoff_table": handoff_table,
        "day_column": day_column,
        "correction_ref_column": correction_ref_column,
        "closed_days_sql": f"SELECT DISTINCT {close_day_column} FROM {wh_close}",
        "approval_sql": f"SELECT {corrections_day_column}, ticket FROM {wh_corrections}",
        "handoff_compare_columns": compare_columns,
    }
    close_compare = schema.get("close_compare_columns", compare_columns)
    if close_compare:
        boundary_probe["closed_rows_sql"] = (
            f"SELECT {', '.join(close_compare)} FROM {wh_close}")

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
         "in_pass_criterion": False, "probe": boundary_probe},
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


def _spec(task: Path, blueprint: dict, sidecar_spec: dict | None = None) -> dict:
    source = sidecar_spec or json.loads((task / "tests/factory_sidecar_spec.json").read_text(encoding="utf-8"))
    tables = source.get("execution_tables") or source["tables"]
    keys = source.get("grain_keys") or {}
    surface = source.get("adapter") or "factory_sidecar"
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
        "composition": {"surface": surface, "theme": blueprint["theme"],
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


def _write_boundary_rival(task: Path) -> None:
    """Make the boundary rival perform one observable unauthorized close edit."""
    solution = Path(task) / "solution"
    original = solution / "boundary_violated.py"
    base = solution / "boundary_violated_base.py"
    if not original.is_file() or base.is_file():
        return
    existing_text = original.read_text(encoding="utf-8")
    if "boundary_violated_base.py" in existing_text:
        return

    sidecar_path = Path(task) / "tests/factory_sidecar_spec.json"
    sidecar_spec: dict = {}
    if sidecar_path.is_file():
        try:
            loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                sidecar_spec = loaded
        except (OSError, ValueError):
            sidecar_spec = {}

    nested = sidecar_spec.get("theme_contract") if isinstance(sidecar_spec.get("theme_contract"), Mapping) else {}
    handoff_table = str(
        sidecar_spec.get("handoff_table")
        or nested.get("handoff_table")
        or "warehouse.factory_handoff"
    )
    close_table = str(
        sidecar_spec.get("close_table")
        or sidecar_spec.get("close_window_table")
        or nested.get("close_table")
        or nested.get("close_window_table")
        or "warehouse.factory_close"
    )
    day_column = str(
        sidecar_spec.get("day_column")
        or nested.get("day_column")
        or "business_day"
    )
    entity_column = str(
        sidecar_spec.get("entity_column")
        or nested.get("entity_column")
        or "entity_id"
    )
    amount_column = str(
        sidecar_spec.get("amount_column")
        or nested.get("amount_column")
        or "net_cents"
    )
    physical = sidecar_spec.get("physical_tables") if isinstance(
        sidecar_spec.get("physical_tables"), Mapping) else {}
    fact_table = str(
        sidecar_spec.get("fact_table")
        or physical.get("fact")
        or nested.get("fact_table")
        or "warehouse.fct_factory_events"
    )
    join_columns = sidecar_spec.get("boundary_join_columns")
    if not isinstance(join_columns, list) or not join_columns:
        join_columns = [day_column, entity_column]
    if day_column not in join_columns:
        raise ValueError("boundary rival join must include the handoff day column")
    close_day_column = str(
        sidecar_spec.get("close_day_column")
        or nested.get("close_day_column")
        or day_column
    )
    join_condition = f"h.{day_column} = c.{close_day_column}"
    if entity_column in join_columns:
        join_condition += f" AND h.{entity_column} = c.{entity_column}"
    join_clause = (
        f"USING ({day_column}, {entity_column})"
        if close_day_column == day_column and entity_column in join_columns else
        f"ON {join_condition}"
    )

    original.rename(base)
    original.write_text(f'''#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path

import duckdb

root = Path(sys.argv[1]).resolve()
subprocess.run([sys.executable, str(Path(__file__).with_name("boundary_violated_base.py")),
                str(root)], check=True)
db = root / "var/warehouse/warehouse.duckdb"
con = duckdb.connect(":memory:")
try:
    con.execute(f"ATTACH '{{db.as_posix()}}' AS wh")
    con.execute("USE wh.warehouse")
    row = con.execute("SELECT h.{day_column}, h.{entity_column} "
                      "FROM {handoff_table} h "
                      "JOIN {close_table} c {join_clause} "
                      "ORDER BY h.{day_column}, h.{entity_column} LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("boundary rival has no closed handoff row")
    con.execute("UPDATE {handoff_table} SET {amount_column} = {amount_column} + 1 "
                "WHERE {day_column} = ? AND {entity_column} = ?", row)
    con.execute("UPDATE {fact_table} SET amount_cents = amount_cents + 1 "
                "WHERE business_day = ? AND entity_id = ?", row)
finally:
    con.close()
dirty_path = root / "var/factory/dirty_days.json"
dirty = json.loads(dirty_path.read_text(encoding="utf-8")) if dirty_path.is_file() else []
day = str(row[0])
if day not in [str(item) for item in dirty]:
    dirty.append(day)
dirty_path.parent.mkdir(parents=True, exist_ok=True)
dirty_path.write_text(json.dumps(dirty), encoding="utf-8")
''', encoding="utf-8")


def _breadcrumb_historical_day(blueprint: Mapping) -> str | None:
    """Day the breadcrumb writer stamps onto t0 clues.

    The composer uses the closed day as the historical landing day. Prefer an
    explicit historical_day when the composition result has already resolved it.
    """
    for key in ("historical_day", "closed_day"):
        raw = blueprint.get(key)
        if raw:
            return str(raw)[:10]
    return None


_SHIPPED_PIPELINES = ("ingest_feed.py", "fold_ledger.py", "publish_handoff.py")
_ORACLE_BRANCH = re.compile(
    r"(?:^|_)(?:oracle|solution|fixed|corrected|fix|fixes|repair|patched)(?:$|_)",
    re.IGNORECASE)
_FIX_FLAGS = frozenset({
    "dedup_events", "fold_all", "keep_void_as_zero", "unconditional_rewrite",
    "null_version_default", "strict_headers", "incremental_fold_only",
    "boundary_on_dirty_only",
})
_FEED_COLUMNS = {
    "day_column": "business_day",
    "entity_column": "entity_id",
    "count_column": "payments",
    "amount_column": "net_cents",
    "correction_ref_column": "correction_ref",
    "corrections_day_column": "business_day",
    "close_day_column": "business_day",
}


def _bind_executed_schema(task: Path, composition: Mapping) -> dict:
    """Point grader and rival SQL at the tables compose_task actually wrote.

    Theme contracts name the native surface. When that surface is null or has
    a different column set, the feed executes on the physical table returned
    by compose_task. Those relations always use the feed columns.
    """
    spec_path = Path(task) / "tests" / "factory_sidecar_spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    physical = composition.get("physical_tables") or {}
    required = ("raw", "fact", "handoff", "close", "corrections", "checkpoint")
    missing = [role for role in required if not physical.get(role)]
    if missing:
        raise RuntimeError(f"compose_task omitted physical tables: {missing}")
    spec["raw_table"] = physical["raw"]
    spec["fact_table"] = physical["fact"]
    spec["handoff_table"] = physical["handoff"]
    spec["close_table"] = physical["close"]
    spec["close_window_table"] = physical["close"]
    spec["corrections_table"] = physical["corrections"]
    spec["checkpoint_table"] = physical["checkpoint"]
    spec.update(_FEED_COLUMNS)
    spec["handoff_compare_columns"] = [
        "business_day", "entity_id", "payments", "net_cents"]
    spec["close_compare_columns"] = [
        "business_day", "entity_id", "payments", "net_cents"]
    spec["physical_tables"] = {role: physical[role] for role in required}
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    return spec


def _expose_duplicate_counted(task: Path) -> None:
    """Keep every raw event in the duplicate-counted rival's fact fold.

    The rendered rival already skips event-id dedup, then collapses to one row
    per business id. When the surviving row is the same one the oracle keeps,
    publication receipts match and the arm never fails a later night. Selecting
    every raw event makes a repeated business id show up in the handoff.
    """
    path = Path(task) / "solution" / "duplicate_counted" / "fold_ledger.py"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    collapse = (
        "        latest = {}\n"
        "        for event in events:\n"
        "            key = event[3]\n"
        "            order = (event[4], event[1], event[0])\n"
        "            if key not in latest or order > latest[key][0]:\n"
        "                latest[key] = (order, event)\n"
        "        selected = [value[1] for value in latest.values()]\n"
    )
    exposed = "        selected = list(events)\n"
    if collapse not in text:
        if exposed not in text:
            raise RuntimeError(f"duplicate_counted fold is not the version-precedence collapse: {path}")
        return
    path.write_text(text.replace(collapse, exposed, 1), encoding="utf-8")


def _install_task_pipelines(task: Path, world: Path) -> None:
    """Copy the broken three-module pipeline into the task workspace.

    Oracle modules stay under solution/. The shipped copies must not carry a
    POLICY switch or a pre-written oracle branch.
    """
    import ast

    source = Path(world) / "workspace" / "pipelines"
    dest = Path(task) / "workspace" / "pipelines"
    dest.mkdir(parents=True, exist_ok=True)
    for name in _SHIPPED_PIPELINES:
        src = source / name
        if not src.is_file():
            raise RuntimeError(f"composed world is missing workspace/pipelines/{name}")
        text = src.read_text(encoding="utf-8")
        if re.search(r"\bPOLICY\b", text):
            raise RuntimeError(f"shipped {name} contains a POLICY dict")
        try:
            tree = ast.parse(text, filename=name)
        except SyntaxError as exc:
            raise RuntimeError(f"shipped {name} is not valid Python: {exc}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.IfExp)):
                continue
            tokens: list[str] = []
            for part in ast.walk(node.test):
                if isinstance(part, ast.Name):
                    tokens.append(part.id)
                elif isinstance(part, ast.Attribute):
                    tokens.append(part.attr)
                elif isinstance(part, ast.Constant) and isinstance(part.value, str):
                    tokens.append(part.value)
            if any(_ORACLE_BRANCH.search(token) or token in _FIX_FLAGS for token in tokens):
                raise RuntimeError(
                    f"shipped {name}:{node.lineno} has a pre-written oracle branch")
        (dest / name).write_text(text, encoding="utf-8")
    leaked = []
    workspace = Path(task) / "workspace"
    for path in workspace.rglob("*"):
        relative = path.relative_to(workspace).as_posix()
        if "factory_future" in relative or path.name.startswith("_future"):
            leaked.append(relative)
    if leaked:
        raise RuntimeError(f"factory_future payload inside task workspace: {leaked}")


def _stage_hazard_breadcrumbs_if_available(world: Path, blueprint: dict) -> list[dict]:
    """Stage indirect t0 hazard breadcrumbs if `tasks.factory.hazards.t0_breadcrumbs` is available."""
    try:
        mod = importlib.import_module("tasks.factory.hazards.t0_breadcrumbs")
    except ImportError:
        return []

    hazards_raw = blueprint.get("hazards") or blueprint.get("r_hazards") or blueprint.get("hazard_pair") or []
    hazards = [
        _extract_code(item, "hazard_id", "id", "name", "code")
        for item in hazards_raw
    ]
    stressors_raw = (blueprint.get("t0_stressors") or blueprint.get("t0_stressor")
                     or blueprint.get("stressors") or blueprint.get("stressor") or [])
    if isinstance(stressors_raw, (str, Mapping)):
        stressors_raw = [stressors_raw]
    stressors = [
        _extract_code(item, "stressor_id", "id", "name", "code")
        for item in stressors_raw
    ]
    seed_val = int(blueprint.get("seed", 0))
    theme_val = str(blueprint.get("theme") or "")

    target_fn = None
    for fn_name in (
        "stage_t0_breadcrumbs",
        "apply_t0_breadcrumbs",
        "write_t0_breadcrumbs",
        "stage_breadcrumbs",
    ):
        candidate = getattr(mod, fn_name, None)
        if callable(candidate):
            target_fn = candidate
            break
    if target_fn is None:
        return []

    sig = inspect.signature(target_fn)
    params = list(sig.parameters.values())
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
    positional_count = sum(
        1
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )
    has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)

    args: list[object] = []
    kwargs: dict[str, object] = {}
    if positional_count >= 2 or has_var_pos:
        args = [Path(world), hazards]
    elif positional_count == 1:
        first_name = params[0].name
        if first_name in ("world", "world_root", "root", "world_dir"):
            args = [Path(world)]
            if "hazards" in sig.parameters or has_var_kw:
                kwargs["hazards"] = hazards
        elif first_name in ("blueprint", "spec"):
            args = [blueprint]
    else:
        if "world" in sig.parameters or has_var_kw:
            kwargs["world"] = Path(world)
        if "hazards" in sig.parameters or has_var_kw:
            kwargs["hazards"] = hazards

    for key, val in (
        ("seed", seed_val),
        ("theme", theme_val),
        ("blueprint", blueprint),
        ("theme_contract", blueprint.get("theme_contract")),
        ("stressor_ids", stressors),
        ("stressors", stressors),
    ):
        if key in sig.parameters or (has_var_kw and key in ("seed", "theme", "stressor_ids")):
            kwargs[key] = val

    historical = _breadcrumb_historical_day(blueprint)
    if "historical_day" in sig.parameters:
        param = sig.parameters["historical_day"]
        if historical:
            kwargs["historical_day"] = historical
        elif param.default is inspect.Parameter.empty:
            raise TypeError(
                "hazard breadcrumbs require historical_day; "
                "blueprint has no closed_day or historical_day"
            )
    elif has_var_kw and historical:
        kwargs["historical_day"] = historical

    result = target_fn(*args, **kwargs)
    if result is None:
        return []
    if isinstance(result, Mapping):
        return [dict(result)]
    if isinstance(result, (list, tuple)):
        return [dict(item) if isinstance(item, Mapping) else {"breadcrumb": item} for item in result]
    return [{"breadcrumb": result}]


BUILDER = '''#!/usr/bin/env python3
"""Build a fresh theme world and stage the factory sidecar for one episode."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, "__REPO__")
from tasks.factory.compiler.peer_materializer import build_world_from_task
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--grading", type=Path)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if args.prepare_only:
        print(json.dumps({"prepared": True}))
        return
    if args.root is None or args.grading is None or args.seed is None:
        parser.error("--root, --grading and --seed are required")
    print(json.dumps(build_world_from_task(Path(__file__).resolve().parents[1],
                                          args.root, args.grading, args.seed), sort_keys=True))
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
from tasks.factory.compiler.peer_materializer import run_baselines_for_task
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

    _bind_composer_runtime()
    task, root, grading = Path(task).resolve(), Path(root).resolve(), Path(grading).resolve()
    blueprint = json.loads((task / "fixtures/blueprint.json").read_text(encoding="utf-8"))
    if seed != int(blueprint["seed"]):
        raise ValueError("seed mismatch with compiled blueprint")
    _build_theme_world(task / "fixtures/seed.yaml", root)
    stage_world(task, root, blueprint)
    _stage_hazard_breadcrumbs_if_available(root, blueprint)
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


def materialize(blueprint: dict, out_root: Path, *,
                scratch: Path | None = None, overwrite: bool = True) -> Path:
    """Compile one blueprint into ``out_root / task_id`` and return that path.

    ``out_root`` is the tasks directory, not the task directory itself.
    A missing oracle composer still writes the task layout and records the
    reason; live injection and oracle capture are skipped in that case.
    """
    if "task_id" not in blueprint or "theme" not in blueprint:
        raise ValueError("blueprint requires task_id and theme")
    if "governance_mode" not in blueprint or "seed" not in blueprint:
        raise ValueError("blueprint requires governance_mode and seed")

    from tasks.factory.grader.declarative_grade import grade as declarative_grade
    if not callable(declarative_grade):
        raise RuntimeError("tasks.factory.grader.declarative_grade.grade is not callable")

    theme = _load_theme(blueprint["theme"])
    stressors, hazards, apply_stressors = _stressor_libraries()
    if not callable(apply_stressors):
        raise RuntimeError("tasks.factory.stressors.apply_stressors is not callable")
    full = executable_blueprint(blueprint)
    contract_file = Path(theme.dir) / "theme_contract.yaml"
    if not contract_file.is_file():
        raise FileNotFoundError(f"theme has no theme_contract.yaml: {contract_file}")
    if not isinstance(blueprint.get("theme_contract"), Mapping):
        full["theme_contract"] = _load_theme_contract(blueprint["theme"], theme_dir=theme.dir)
    if full.get("theme_contract", {}).get("source") != "theme_contract_yaml":
        full["theme_contract"] = _load_theme_contract(blueprint["theme"], theme_dir=theme.dir)
    if full.get("theme_contract", {}).get("source") != "theme_contract_yaml":
        raise RuntimeError(
            f"theme contract for {blueprint['theme']} did not load from {contract_file}")
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
    write_narrative(task, bundle, blueprint["theme"], variant=variant,
                    hazard_ids=list(full["hazards"]),
                    stressors=list(full["t0_stressors"]))
    from tasks.factory.compiler.report_contract import append_report_contract
    append_report_contract(task)
    _write_variants(task, blueprint, bundle)

    unavailable: list[str] = []
    breadcrumbs: list[dict] = []
    try:
        composition = compose_oracle(task, full, world)
    except OracleComposerUnavailable as exc:
        unavailable.append(str(exc))
        _placeholder_solution(task, full, str(exc))
        composed = False
    else:
        composed = True
        if not isinstance(composition, Mapping):
            raise RuntimeError("compose_task did not return a composition mapping")
        _bind_executed_schema(task, composition)
        _write_boundary_rival(task)
        _expose_duplicate_counted(task)
        _install_task_pipelines(task, world)
        dates = composition.get("dates") if isinstance(composition.get("dates"), Mapping) else {}
        if dates.get("historical"):
            full["historical_day"] = str(dates["historical"])[:10]
        breadcrumbs = _stage_hazard_breadcrumbs_if_available(world, full)
        if not breadcrumbs:
            raise RuntimeError("hazard breadcrumbs were not staged")
        from tasks.factory.compiler.sealed_future import seal_future
        seal_future(world, task / "tests")
    workflow.seed(world, task, _workflow_spec(), vnow)

    tests = task / "tests"
    sidecar_spec = json.loads((tests / "factory_sidecar_spec.json").read_text(encoding="utf-8"))
    spec = _spec(task, full, sidecar_spec=sidecar_spec)
    manifest = _manifest(blueprint["task_id"], full, bundle["decision"], sidecar_spec=sidecar_spec)
    spec["checks"] = {row["id"]: row["probe"] for row in manifest["checks"]}
    (tests / "spec.json").write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")
    (tests / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    (fixtures / "make_fixture_world.py").write_text(
        BUILDER.replace("__REPO__", str(REPO)), encoding="utf-8")
    for name, body in (("grade.py", GRADER), ("run_baselines.py", BASELINE_RUNNER)):
        (tests / name).write_text(body.replace("__REPO__", str(REPO)), encoding="utf-8")
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
        "unavailable": unavailable,
        "breadcrumbs": breadcrumbs,
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
    from tasks.factory.compiler.phase2_readiness import phase2_fingerprint, phase2_task_integrity
    (tests / "phase2_provenance.json").write_text(
        json.dumps({"task_id": blueprint["task_id"], "theme": blueprint["theme"],
                    "fingerprint": phase2_fingerprint(blueprint, REPO),
                    "task_integrity": phase2_task_integrity(task)},
                   indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return task


def run_baselines_for_task(task: Path, scratch: Path) -> dict:
    """Build one world, execute every arm, and grade the five canonical baselines.

    ``partial_fix`` is executed and graded as well. A missing world or a failed
    arm raises; it is not reported as a pass.
    """
    from tasks.factory.grader.baseline_runner import BASELINES, run
    from tasks.factory.grader.declarative_grade import grade
    _world_modules()
    from world.executor import write_profiles

    task, scratch = Path(task).resolve(), Path(scratch).resolve()
    blueprint = json.loads((task / "fixtures/blueprint.json").read_text(encoding="utf-8"))
    worlds = scratch / "worlds"
    if worlds.exists():
        shutil.rmtree(worlds)
    worlds.mkdir(parents=True)
    names = (*BASELINES, "partial_fix")
    for name in names:
        script = task / "solution" / ("solve.py" if name == "oracle" else f"{name}.py")
        if name != "no_op" and not script.is_file():
            raise FileNotFoundError(f"baseline arm {name} has no script at {script}")
    base = worlds / "no_op"
    build_world_from_task(task, base, scratch / "grading" / "no_op", int(blueprint["seed"]))
    theme = _load_theme(blueprint["theme"])
    profile = (theme.manifest or {}).get("dbt_profile")
    for name in names:
        world = worlds / name
        if name != "no_op":
            shutil.copytree(base, world)
            write_profiles(world, world / "build", profile)
            script = task / "solution" / ("solve.py" if name == "oracle" else f"{name}.py")
            process = subprocess.run([sys.executable, str(script), str(world)], cwd=REPO,
                                     text=True, capture_output=True)
            if process.returncode:
                raise RuntimeError(f"{name} failed: {process.stderr[-1500:]}")
    report = run(worlds, task / "tests", "default", task / "tests/baselines/default.json")
    partial_world = worlds / "partial_fix"
    artifact = partial_world / "workspace/final_report.json"
    if not artifact.is_file():
        artifact = partial_world / "final_report.json"
    partial = grade(partial_world, task / "tests",
                    artifact if artifact.is_file() else None, "default")
    checks = partial.get("checks") or []
    if isinstance(checks, dict):
        encoded = checks
    else:
        encoded = {row["id"]: {"points": row["points"], "max": row["max"],
                               "passed": row["passed"], "detail": row["detail"]}
                   for row in checks}
    report["baselines"]["partial_fix"] = {
        "total": partial["total"], "max": partial["max"],
        "passed": partial["passed"],
        "progress_quantities": partial.get("progress_quantities"),
        "checks": encoded,
    }
    output = task / "tests/baselines/default.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"task": blueprint["task_id"], "valid": True,
            "baseline_scores": {k: v["total"] for k, v in report["baselines"].items()},
            "baselines": report["baselines"]}


def select_stage0_blueprints(catalog: list[dict]) -> tuple[list[dict], list[dict]]:
    """36 theme x governance twins, plus uncovered settlement seed-A topologies.

    ``select_stratified_batch(..., per_theme=3)`` supplies the 36 twins. Each
    FT01..FT15 topology is then represented by one ``settlement_clearing`` +
    ``act-authorized`` row on schedule seed A, unless that theme, mode, and
    topology is already in the 36.
    """
    from tasks.factory.compiler.blueprint_matrix import FAULT_TOPOLOGIES, select_stratified_batch

    twins = select_stratified_batch(catalog, per_theme=3)
    if len(twins) != 36:
        raise ValueError(f"stratified twin batch must contain 36 blueprints, got {len(twins)}")
    covered = {
        (row["theme"], row["governance_mode"], row["fault_topology_id"])
        for row in twins
    }
    selected = list(twins)
    already: list[dict] = []
    for topology_id in FAULT_TOPOLOGIES:
        key = ("settlement_clearing", "act-authorized", topology_id)
        if key in covered:
            match = next(row for row in twins if (
                row["theme"], row["governance_mode"], row["fault_topology_id"]) == key)
            already.append({
                "fault_topology_id": topology_id,
                "task_id": match["task_id"],
                "theme": match["theme"],
                "governance_mode": match["governance_mode"],
                "schedule_seed_id": match.get("schedule_seed_id"),
                "status": "already_covered",
            })
            continue
        matches = [
            row for row in catalog
            if row.get("theme") == "settlement_clearing"
            and row.get("governance_mode") == "act-authorized"
            and row.get("fault_topology_id") == topology_id
            and row.get("schedule_seed_id") == "H4_seedA"
        ]
        if len(matches) != 1:
            raise ValueError(
                f"expected one settlement_clearing act-authorized seed A row for "
                f"{topology_id}, found {len(matches)}")
        selected.append(matches[0])
    seen = [row["task_id"] for row in selected]
    if len(seen) != len(set(seen)):
        raise ValueError("stage 0 selection contains duplicate task ids")
    return selected, already


def stage0_coverage(selected: list[dict], already: list[dict]) -> dict:
    topologies = {row["fault_topology_id"] for row in selected}
    topologies.update(row["fault_topology_id"] for row in already)
    modes = {row["governance_mode"] for row in selected}
    themes = {row["theme"] for row in selected}
    return {
        "task_count": len(selected),
        "themes_covered": len(themes),
        "topologies_covered": len(topologies),
        "governance_modes_covered": len(modes),
        "governance_modes": sorted(modes),
        "topologies": sorted(topologies),
        "already_covered": already,
    }


def _materialize_stage0_one(payload: tuple) -> dict:
    """Materialize one stage-0 blueprint. Used by the process pool."""
    blueprint, out_root, scratch = payload
    import time
    started = time.time()
    task_id = str(blueprint.get("task_id"))
    try:
        path = materialize(blueprint, Path(out_root), scratch=Path(scratch), overwrite=True)
    except Exception as exc:
        return {"task_id": task_id, "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"[-1600:],
                "seconds": round(time.time() - started, 1)}
    return {"task_id": task_id, "status": "ok", "path": str(path),
            "seconds": round(time.time() - started, 1)}


def materialize_stage0(out_root: Path, scratch: Path | None = None,
                       *, workers: int = 4) -> dict:
    """Materialize the stratified stage-0 set under ``out_root``."""
    import time
    from concurrent.futures import ProcessPoolExecutor, as_completed
    from multiprocessing import get_context

    from tasks.factory.compiler.blueprint_matrix import generate_catalog

    out_root = Path(out_root)
    scratch = Path(scratch or (out_root.parent / f"{out_root.name}_scratch"))
    out_root.mkdir(parents=True, exist_ok=True)
    catalog = generate_catalog()
    selected, already = select_stage0_blueprints(catalog)
    coverage = stage0_coverage(selected, already)
    payloads = [(row, str(out_root), str(scratch)) for row in selected]
    started = time.time()
    results: list[dict] = []
    if workers <= 1 or len(payloads) == 1:
        for payload in payloads:
            item = _materialize_stage0_one(payload)
            results.append(item)
            print(json.dumps(item), flush=True)
    else:
        context = get_context("fork")
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            futures = [pool.submit(_materialize_stage0_one, payload) for payload in payloads]
            for future in as_completed(futures):
                item = future.result()
                results.append(item)
                print(json.dumps(item), flush=True)
    by_id = {item["task_id"]: item for item in results}
    ordered = [by_id[row["task_id"]] for row in selected]
    failed = [item for item in ordered if item["status"] != "ok"]
    report = {
        "ok": not failed,
        "out_root": str(out_root),
        "scratch": str(scratch),
        "workers": workers,
        "seconds": round(time.time() - started, 1),
        "coverage": coverage,
        "results": ordered,
        "failed": failed,
    }
    (out_root / "stage0_selection.json").write_text(
        json.dumps({"coverage": coverage,
                    "tasks": [{"task_id": row["task_id"], "theme": row["theme"],
                               "governance_mode": row["governance_mode"],
                               "fault_topology_id": row["fault_topology_id"],
                               "schedule_seed_id": row["schedule_seed_id"]}
                              for row in selected]}, indent=2) + "\n",
        encoding="utf-8")
    return report


def phase3_task_stem(task_id: str) -> str:
    """``FT0091-commerce-...`` and ``FT0091`` both stem to ``FT0091``."""
    return str(task_id).split("-", 1)[0]


def phase3_topology(blueprint: Mapping) -> str:
    return str(blueprint.get("fault_topology_id") or blueprint.get("topology_id") or "")


def phase3_certified_reason(blueprint: Mapping, *,
                            extra_skip: Collection[str] | None = None) -> str | None:
    """Why a blueprint must not be rebuilt, or None when it is mutable."""
    task_id = str(blueprint.get("task_id") or "")
    stem = phase3_task_stem(task_id)
    topology = phase3_topology(blueprint)
    extra = {str(item) for item in (extra_skip or ())}
    extra_stems = {phase3_task_stem(item) for item in extra}
    if topology in PHASE3_CERTIFIED_TOPOLOGIES:
        return "certified_topology"
    if (stem in PHASE3_CERTIFIED_TASK_IDS or task_id in PHASE3_CERTIFIED_TASK_IDS
            or stem in extra_stems or task_id in extra):
        return "certified_task_id"
    return None


def select_phase3_delta_blueprints(
    blueprints: list[dict],
    *,
    only_topologies: Collection[str] | None = None,
    skip_task_ids: Collection[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split pilot blueprints into mutated rebuilds and certified/unselected skips.

    A task whose topology is FT03 or FT15 is skipped even when its id is not in
    the certified-id list. Certified ids are skipped even when their topology
    is in the mutated set.
    """
    if only_topologies is None:
        requested = set(PHASE3_MUTATED_TOPOLOGIES)
    else:
        requested = {str(item).strip().upper() for item in only_topologies if str(item).strip()}
    unknown = requested - PHASE3_MUTATED_TOPOLOGIES - PHASE3_CERTIFIED_TOPOLOGIES
    if unknown or not requested:
        raise ValueError(f"unknown fault topologies: {sorted(unknown)}")
    rebuild: list[dict] = []
    skipped: list[dict] = []
    for row in blueprints:
        task_id = str(row.get("task_id") or "")
        topology = phase3_topology(row)
        reason = phase3_certified_reason(row, extra_skip=skip_task_ids)
        if reason is None and topology not in PHASE3_MUTATED_TOPOLOGIES:
            reason = "topology_not_mutated"
        if reason is None and topology not in requested:
            reason = "topology_not_selected"
        if reason:
            skipped.append({
                "task_id": task_id,
                "fault_topology_id": topology,
                "reason": reason,
            })
        else:
            rebuild.append(row)
    return rebuild, skipped


def load_pilot_blueprints(pilot_dir: Path) -> list[dict]:
    """Read ``phase2_selection.json`` blueprints without rewriting the file."""
    selection = Path(pilot_dir) / "phase2_selection.json"
    if not selection.is_file():
        raise FileNotFoundError(f"pilot selection missing: {selection}")
    payload = json.loads(selection.read_text(encoding="utf-8"))
    selected = payload.get("selected") if isinstance(payload, dict) else None
    if not isinstance(selected, list) or not selected:
        raise ValueError(f"pilot selection has no tasks: {selection}")
    rows: list[dict] = []
    for record in selected:
        blueprint = record.get("blueprint") if isinstance(record, dict) else None
        if not isinstance(blueprint, dict) or not blueprint.get("task_id"):
            raise ValueError("pilot selection record has no blueprint.task_id")
        rows.append(blueprint)
    return rows


def phase3_package_missing(task: Path) -> list[str]:
    """Required artifacts before a rebuilt task can be called done."""
    task = Path(task)
    missing: list[str] = []
    receipts = task / "tests/oracle_receipts.json"
    governance = task / "tests/governance.json"
    solution = task / "solution"
    if not receipts.is_file() or receipts.stat().st_size == 0:
        missing.append("tests/oracle_receipts.json")
    else:
        try:
            data = json.loads(receipts.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing.append("tests/oracle_receipts.json")
        else:
            nights = data.get("nights") if isinstance(data, dict) else None
            if not isinstance(nights, list) or not nights or (
                    isinstance(data, dict) and data.get("unavailable")):
                missing.append("tests/oracle_receipts.json")
    if not governance.is_file() or governance.stat().st_size == 0:
        missing.append("tests/governance.json")
    elif "tests/governance.json" not in missing:
        try:
            parsed = json.loads(governance.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing.append("tests/governance.json")
        else:
            if not isinstance(parsed, dict) or not parsed:
                missing.append("tests/governance.json")
    if not solution.is_dir() or not (solution / "solve.py").is_file():
        missing.append("solution/")
    return missing


def materialize_phase3_delta(
    pilot_dir: Path,
    scratch: Path | None = None,
    *,
    only_topologies: Collection[str] | None = None,
    skip_task_ids: Collection[str] | None = None,
    workers: int = 1,
    blueprints: list[dict] | None = None,
) -> dict:
    """Rebuild mutated pilot tasks through ``_materialize_stage0_one``.

    Certified topologies and task ids are not passed to ``materialize`` and
    their directories are not deleted. One worker: a parallel run deletes
    ``tests/oracle_receipts.json`` out from under another writer.
    """
    import time

    if workers != 1:
        raise ValueError(
            "phase 3 delta materialization uses one worker; "
            "parallel runs delete tests/oracle_receipts.json")
    pilot = Path(pilot_dir)
    scratch_root = _outside_repo(scratch or PHASE3_DELTA_SCRATCH)
    rows = list(blueprints) if blueprints is not None else load_pilot_blueprints(pilot)
    rebuild, skipped = select_phase3_delta_blueprints(
        rows, only_topologies=only_topologies, skip_task_ids=skip_task_ids)
    started = time.time()
    results: list[dict] = []
    for row in rebuild:
        item = _materialize_stage0_one((row, str(pilot), str(scratch_root)))
        if item.get("status") == "ok":
            missing = phase3_package_missing(Path(item["path"]))
            if missing:
                item = {**item, "status": "failed",
                        "error": "missing " + ", ".join(missing)}
        results.append(item)
        print(json.dumps(item), flush=True)
    failed = [item for item in results if item.get("status") != "ok"]
    return {
        "ok": not failed,
        "pilot_dir": str(pilot),
        "scratch": str(scratch_root),
        "workers": 1,
        "seconds": round(time.time() - started, 1),
        "rebuilt_task_ids": [item["task_id"] for item in results if item.get("status") == "ok"],
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path,
                        default=REPO / "experiments/scale_1000/catalog_1080.jsonl")
    parser.add_argument("--out", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--themes", nargs="*")
    parser.add_argument("--modes", nargs="*")
    parser.add_argument("--delta-only", action="store_true",
                        help="rebuild mutated pilot tasks; leave certified tasks in place")
    parser.add_argument("--only-topologies", nargs="+", metavar="FT##",
                        help="mutated topologies to rebuild (default FT01-FT02, FT04-FT14)")
    parser.add_argument("--skip-certified", action="store_true",
                        help="skip FT03, FT15, and the certified pilot task ids")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    if args.delta_only:
        if not args.skip_certified:
            print("phase3 delta requires --skip-certified", file=sys.stderr)
            return 2
        if args.workers != 1:
            print("phase3 delta requires --workers 1 "
                  "(parallel runs delete tests/oracle_receipts.json)", file=sys.stderr)
            return 2
        only = None
        if args.only_topologies:
            only = {
                part.strip().upper()
                for token in args.only_topologies
                for part in token.split(",")
                if part.strip()
            }
        try:
            report = materialize_phase3_delta(
                args.out, args.scratch, only_topologies=only, workers=1)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"phase3 delta: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({
            "ok": report["ok"],
            "rebuilt": len(report["rebuilt_task_ids"]),
            "skipped": len(report["skipped"]),
            "failed": len(report["failed"]),
            "scratch": report["scratch"],
        }))
        return 0 if report["ok"] else 1
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
