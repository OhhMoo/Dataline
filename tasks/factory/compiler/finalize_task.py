"""Finish prompt locks and measured reward declarations on materialized tasks."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from pathlib import Path

from tasks.factory.compiler.report_contract import append_report_contract

REPO = Path(__file__).resolve().parents[3]
PROMPT_ARMS = ("default", "vague", "terse")
PHASE2_PROGRESS_CHECKPOINTS: tuple[str, ...] = (
    "q0_raw_loaded",
    "q1_t0_dedup_and_precedence",
    "q2_t0_handoff_parity",
    "q3_r1_parity",
    "q4_r2_parity",
    "q5_r3_parity",
    "q6_r4_parity",
)
PROGRESS = """[matrix.progress]
rows = {check = "C0", field = "matched_rows", noop = "min", oracle = "max"}
night_1 = {check = "R1", field = "matched_receipts", noop = "min", oracle = "max"}
night_2 = {check = "R2", field = "matched_receipts", noop = "min", oracle = "max"}
night_3 = {check = "R3", field = "matched_receipts", noop = "min", oracle = "max"}
night_4 = {check = "R4", field = "matched_receipts", noop = "min", oracle = "max"}
weights = {rows = 0.4, night_1 = 0.15, night_2 = 0.15, night_3 = 0.15, night_4 = 0.15}

"""
PROGRESS_PHASE2 = """[matrix.progress]
q0_raw_loaded = {check = "C0", field = "q0_raw_loaded", noop = "min", oracle = "max"}
q1_t0_dedup_and_precedence = {check = "C0", field = "q1_t0_dedup_and_precedence", noop = "min", oracle = "max"}
q2_t0_handoff_parity = {check = "C0", field = "q2_t0_handoff_parity", noop = "min", oracle = "max"}
q3_r1_parity = {check = "R1", field = "q3_r1_parity", noop = "min", oracle = "max"}
q4_r2_parity = {check = "R2", field = "q4_r2_parity", noop = "min", oracle = "max"}
q5_r3_parity = {check = "R3", field = "q5_r3_parity", noop = "min", oracle = "max"}
q6_r4_parity = {check = "R4", field = "q6_r4_parity", noop = "min", oracle = "max"}
weights = {q0_raw_loaded = 0.1, q1_t0_dedup_and_precedence = 0.15, q2_t0_handoff_parity = 0.15, q3_r1_parity = 0.15, q4_r2_parity = 0.15, q5_r3_parity = 0.15, q6_r4_parity = 0.15}

"""
BASE_DEAD_SURFACES: tuple[str, ...] = (
    "~alarms_history",
    "~alarms_list",
    "~deploy_list",
    "~ingest_alerts",
    "~ingest_list",
    "~ingest_runs",
    "~metrics_list",
)
REVENUE_DEAD = BASE_DEAD_SURFACES
NO_BI_THEMES: frozenset[str] = frozenset({
    "sftp_vendor",
    "mainframe_batch",
    "supply_chain_inventory",
    "healthcare_claims_edi",
    "iot_telemetry_billing",
})
ALL_THEMES: tuple[str, ...] = (
    "settlement_clearing",
    "commerce_analytics",
    "revenue_daily",
    "sftp_vendor",
    "mainframe_batch",
    "excel_close",
    "saas_subscription_mrr",
    "ad_attribution_events",
    "supply_chain_inventory",
    "healthcare_claims_edi",
    "fintech_kyc_risk_limits",
    "iot_telemetry_billing",
)
DEAD_BY_DESIGN_BY_THEME: dict[str, tuple[str, ...]] = {
    theme: (BASE_DEAD_SURFACES + ("~bi_list",) if theme in NO_BI_THEMES else BASE_DEAD_SURFACES)
    for theme in ALL_THEMES
}


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _detect_theme(task: Path, raw: dict) -> str:
    spec = _read_json(task / "tests/spec.json")
    comp_theme = (spec.get("composition") or {}).get("theme") or spec.get("theme")
    if isinstance(comp_theme, str) and comp_theme:
        return comp_theme

    blueprint = _read_json(task / "fixtures/blueprint.json")
    bp_theme = blueprint.get("theme")
    if isinstance(bp_theme, str) and bp_theme:
        return bp_theme

    theme_json = _read_json(task / "fixtures/theme.json")
    tj_name = theme_json.get("name")
    if isinstance(tj_name, str) and tj_name:
        return tj_name

    mat_report = _read_json(task / "tests/materializer_report.json")
    mr_theme = mat_report.get("theme")
    if isinstance(mr_theme, str) and mr_theme:
        return mr_theme

    task_id = str((raw.get("task") or {}).get("id") or task.name)
    for theme in ALL_THEMES:
        slug = theme.replace("_", "-")
        if f"-{slug}-" in task_id or task_id.endswith(f"-{slug}"):
            return theme
    return ""


def dead_by_design_for_theme(theme: str, *, supports_bi: bool | None = None) -> tuple[str, ...]:
    """Return the `[harness].dead_by_design` surface labels for a factory theme."""
    if supports_bi is False:
        return BASE_DEAD_SURFACES + ("~bi_list",)
    if supports_bi is True:
        return BASE_DEAD_SURFACES
    if theme in DEAD_BY_DESIGN_BY_THEME:
        return DEAD_BY_DESIGN_BY_THEME[theme]
    return BASE_DEAD_SURFACES


def _exposes_phase2_progress(task: Path, receipt_engine_path: Path | None = None) -> bool:
    """Check whether `tests/spec.json` or `receipt_engine.py` exposes `q0..q6`."""
    spec_path = task / "tests/spec.json"
    if spec_path.is_file():
        spec_text = spec_path.read_text(encoding="utf-8")
        if all(cp in spec_text for cp in PHASE2_PROGRESS_CHECKPOINTS):
            return True

    engine_file = receipt_engine_path or (REPO / "tasks/factory/grader/receipt_engine.py")
    if engine_file.is_file():
        engine_text = engine_file.read_text(encoding="utf-8")
        if all(cp in engine_text for cp in PHASE2_PROGRESS_CHECKPOINTS):
            return True
    return False


def _progress_block_for_task(task: Path, receipt_engine_path: Path | None = None) -> str:
    if not _exposes_phase2_progress(task, receipt_engine_path=receipt_engine_path):
        return PROGRESS

    # Verify check detail fields against baselines/default.json if already populated
    baselines = _read_json(task / "tests/baselines/default.json").get("baselines") or {}
    oracle_checks = ((baselines.get("oracle") or {}).get("checks") or {}) if isinstance(baselines, dict) else {}
    c0_detail = (oracle_checks.get("C0") or {}).get("detail") if isinstance(oracle_checks, dict) else None
    r1_detail = (oracle_checks.get("R1") or {}).get("detail") if isinstance(oracle_checks, dict) else None

    if isinstance(c0_detail, dict) and c0_detail and "q0_raw_loaded" not in c0_detail:
        return PROGRESS
    if isinstance(r1_detail, dict) and r1_detail and "q3_r1_parity" not in r1_detail:
        if "matched_receipts" in r1_detail:
            return (
                "[matrix.progress]\n"
                'q0_raw_loaded = {check = "C0", field = "q0_raw_loaded", noop = "min", oracle = "max"}\n'
                'q1_t0_dedup_and_precedence = {check = "C0", field = "q1_t0_dedup_and_precedence", noop = "min", oracle = "max"}\n'
                'q2_t0_handoff_parity = {check = "C0", field = "q2_t0_handoff_parity", noop = "min", oracle = "max"}\n'
                'q3_r1_parity = {check = "R1", field = "matched_receipts", noop = "min", oracle = "max"}\n'
                'q4_r2_parity = {check = "R2", field = "matched_receipts", noop = "min", oracle = "max"}\n'
                'q5_r3_parity = {check = "R3", field = "matched_receipts", noop = "min", oracle = "max"}\n'
                'q6_r4_parity = {check = "R4", field = "matched_receipts", noop = "min", oracle = "max"}\n'
                "weights = {q0_raw_loaded = 0.1, q1_t0_dedup_and_precedence = 0.15, q2_t0_handoff_parity = 0.15, "
                "q3_r1_parity = 0.15, q4_r2_parity = 0.15, q5_r3_parity = 0.15, q6_r4_parity = 0.15}\n\n"
            )
    return PROGRESS_PHASE2


def finalize_task(task: Path, *, receipt_engine_path: Path | None = None) -> bool:
    """Update one completed factory task without rebuilding its world or baselines."""
    task = Path(task)
    if not (task / "tests/materializer_report.json").is_file():
        raise ValueError(f"task is not fully materialized: {task}")
    append_report_contract(task)
    path = task / "task.toml"
    original = path.read_text(encoding="utf-8")
    raw = tomllib.loads(original)
    text = original

    for arm in PROMPT_ARMS:
        digest = hashlib.sha256((task / "prompts" / f"{arm}.md").read_bytes()).hexdigest()
        replacement = f'{arm} = {{ file = "prompts/{arm}.md", sha256 = "{digest}" }}'
        pattern = rf'(?m)^{arm} = \{{ file = "prompts/{arm}\.md"(?:, sha256 = "[a-f0-9]{{64}}")? \}}$'
        text, count = re.subn(pattern, replacement, text)
        if count != 1:
            raise ValueError(f"expected exactly one prompt lock for {arm} in {path}")

    desired_progress = _progress_block_for_task(task, receipt_engine_path=receipt_engine_path)
    if "[matrix.progress]" not in text:
        marker = "[prompt_variants]"
        if marker not in text:
            raise ValueError(f"prompt variants absent from {path}")
        text = text.replace(marker, desired_progress + marker, 1)
    elif desired_progress != PROGRESS and "q0_raw_loaded" not in text:
        text = re.sub(
            r"(?ms)^\[matrix\.progress\]\n.*?(?=^\[)",
            desired_progress,
            text,
            count=1,
        )

    theme = _detect_theme(task, raw)
    labels = dead_by_design_for_theme(theme)
    reason_prefix = "revenue" if theme == "revenue_daily" else (theme.replace("_", " ") if theme else "factory")
    declaration_line = (
        "dead_by_design = ["
        + ", ".join(
            f'{{ label = "{label}", reason = "The {reason_prefix} world has no data on this optional surface." }}'
            for label in labels
        )
        + "]"
    )
    harness_section = text.split("[harness]", 1)[-1].split("[matrix]", 1)[0]
    existing_labels = [
        item.get("label")
        for item in ((raw.get("harness") or {}).get("dead_by_design") or [])
        if isinstance(item, dict)
    ]
    if "dead_by_design = [" not in harness_section:
        marker = 'pass_requires_checks = ["C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3"]'
        if marker not in text:
            raise ValueError(f"pass checks absent from {path}")
        text = text.replace(marker, declaration_line + "\n" + marker, 1)
    elif list(existing_labels) != list(labels):
        before_harness, after_harness = text.split("[harness]", 1)
        harness_body, after_matrix = after_harness.split("[matrix]", 1)
        harness_body = re.sub(
            r"(?m)^dead_by_design = \[.*\]$",
            declaration_line,
            harness_body,
            count=1,
        )
        text = before_harness + "[harness]" + harness_body + "[matrix]" + after_matrix

    tomllib.loads(text)
    if text != original:
        path.write_text(text, encoding="utf-8")
    return text != original


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tasks", type=Path, nargs="+")
    args = parser.parse_args()
    changed = 0
    for task in args.tasks:
        changed += finalize_task(task)
    print(f"finalized {len(args.tasks)} tasks ({changed} metadata updates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
