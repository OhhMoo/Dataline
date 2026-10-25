"""Static discoverability and spoiler checks for generated tasks.

D1, D2, and S1 anchors (ticket, owner, closed day, authority pages, and the
mode-specific evidence) must appear on an agent-readable surface. When
``tests/governance.json`` or ``fixtures/blueprint.json`` lists hazard ids, each
hazard also needs its operational clue on that same surface: prompts, workflow
YAML (kb, chat, and issues), ``workspace/docs``, ``var/logs/ingest_daemon.log``,
or ``var/logs/sftp_sync.log``. ``grading/`` and ``solution/`` are not
agent-readable. When ``fixtures/blueprint.json`` lists S07 or S09, that
stressor needs its clue on kb, chat, or workspace/docs.
``factory_future``, solution lines, and a decoy ticket label fail the lint.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tasks.factory.narrative.corecraft_synthesizer import (
    HAZARD_CLUES, STRESSOR_CLUES, blueprint_lint_stressors, hazard_entries,
)

SPOILER_PATTERNS = (
    re.compile(r"\b(?:this|the) (?:bug|defect) is\b", re.I),
    re.compile(r"\b(?:decoy|red herring|correct ticket is|oracle solution)\b", re.I),
    re.compile(r"\bline\s+\d+\s+(?:of|in)\s+\S+\.(?:py|sql)\b", re.I),
    re.compile(r"\b(?:replace|edit|change)\s+[`\w./-]+\.(?:py|sql)\s+line\s+\d+", re.I),
    re.compile(r"\b(?:delete|remove)\s+the\s+(?:line|check)\s+in\s+\w+\.py\b", re.I),
    re.compile(r"\b(?:fold_ledger|ingest_feed|publish_handoff)\.py\b"),
)


def lint_narrative(task_dir: Path) -> "LintResult":
    """Alias used by the phase-4 bundle gate."""
    return lint_task(task_dir)
_FUTURE_PAYLOAD = re.compile(r"factory_future|(?<![A-Za-z0-9])_future\b", re.I)


@dataclass(frozen=True)
class LintResult:
    ok: bool
    spoilers: tuple[str, ...]
    missing: tuple[str, ...]
    coverage: float
    governance_hops: dict = field(default_factory=dict)
    hazard_clues: dict = field(default_factory=dict)
    stressor_clues: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "spoilers": list(self.spoilers),
                "missing": list(self.missing), "coverage": self.coverage,
                "governance_hops": self.governance_hops,
                "hazard_clues": self.hazard_clues,
                "stressor_clues": self.stressor_clues}


def _flatten(text: str) -> str:
    return " ".join(text.split())


def _strings(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        found = [str(key) for key in node if isinstance(key, str)]
        return found + [part for value in node.values() for part in _strings(value)]
    if isinstance(node, list):
        return [part for value in node for part in _strings(value)]
    return []


def _logical_text(path: Path) -> str:
    """Raw file text plus parsed YAML scalars, so quoted SQL stays searchable."""
    raw = path.read_text(encoding="utf-8")
    if path.suffix not in {".yaml", ".yml"}:
        return raw
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw
    return raw + "\n" + "\n".join(_strings(loaded))


# Hazard clues may live only in these rotated logs. grading/ and solution/ stay out.
_AGENT_LOGS = ("ingest_daemon.log", "sftp_sync.log")


def _agent_paths(task_dir: Path) -> list[Path]:
    paths = [task_dir / "prompt.md"]
    paths += sorted((task_dir / "prompts").glob("*.md"))
    paths += sorted((task_dir / "fixtures" / "workflow").glob("*.yaml"))
    variants = task_dir / "fixtures" / "variants"
    if variants.is_dir():
        paths += sorted(variants.glob("**/workflow/*.yaml"))
        paths += sorted(variants.glob("**/prompt.md"))
    paths += sorted((task_dir / "workspace" / "docs").rglob("*.md"))
    paths += sorted((task_dir / "docs").glob("*.md"))
    logs = task_dir / "var" / "logs"
    paths += [logs / name for name in _AGENT_LOGS]
    return paths


def _agent_text(task_dir: Path) -> str:
    return "\n".join(_logical_text(path) for path in _agent_paths(task_dir) if path.is_file())


def _surface_text(task_dir: Path, surface: str) -> str:
    workflow = task_dir / "fixtures" / "workflow"
    chunks: list[str] = []
    if surface == "sql_query":
        names = ("chat.yaml", "kb.yaml")
    elif surface == "issues":
        names = ("issues.yaml",)
    elif surface == "chat":
        names = ("chat.yaml",)
    elif surface == "kb":
        names = ("kb.yaml",)
    else:
        return ""
    for name in names:
        path = workflow / name
        if path.is_file():
            chunks.append(_logical_text(path))
        variants = task_dir / "fixtures" / "variants"
        if variants.is_dir():
            chunks.extend(_logical_text(candidate)
                          for candidate in sorted(variants.glob(f"**/workflow/{name}"))
                          if candidate.is_file())
    if surface in {"kb", "sql_query"}:
        chunks.extend(_logical_text(candidate)
                      for candidate in sorted((task_dir / "workspace" / "docs").rglob("*.md"))
                      if candidate.is_file())
        chunks.extend(_logical_text(candidate)
                      for candidate in sorted((task_dir / "docs").glob("*.md"))
                      if candidate.is_file())
    return "\n".join(chunks)


def _solution_lines(task_dir: Path) -> set[str]:
    lines = set()
    for path in (task_dir / "solution").rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".sql", ".sh"}:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if (len(stripped) >= 48 and not stripped.startswith(("#", "//", "--"))
                    and re.search(r"[()=]", stripped)):
                lines.add(stripped)
    return lines


def _decoy_labels(task_dir: Path) -> list[str]:
    hits = []
    candidates = list((task_dir / "fixtures" / "workflow").glob("issues.yaml"))
    candidates += list((task_dir / "fixtures" / "variants").glob("**/workflow/issues.yaml"))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        for issue in loaded.get("issues") or []:
            for label in issue.get("labels") or []:
                if re.search(r"decoy", str(label), re.I):
                    hits.append(f"label:{issue.get('id')}:{label}")
    return hits


def _listed_hazards(task_dir: Path, decision: dict) -> list[tuple[str, str]]:
    raws: list[object] = []
    for key in ("hazard_ids", "hazards"):
        listed = decision.get(key)
        if isinstance(listed, list):
            raws.extend(listed)
    blueprint = task_dir / "fixtures" / "blueprint.json"
    if blueprint.is_file():
        try:
            payload = json.loads(blueprint.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if isinstance(payload, dict):
            listed = payload.get("hazards") or payload.get("r_hazards") or payload.get("hazard_pair") or []
            if isinstance(listed, list):
                raws.extend(listed)
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for raw, code in hazard_entries(raws):
        if code not in grouped:
            grouped[code] = []
            order.append(code)
        if raw not in grouped[code]:
            grouped[code].append(raw)
    pairs = []
    for code in order:
        for raw in grouped[code]:
            pairs.append((raw, code))
    return pairs


def _governance_hops(task_dir: Path, decision: dict) -> tuple[dict, list[str]]:
    hops = decision.get("hops") if isinstance(decision.get("hops"), list) else []
    if not hops:
        report = {"ok": False, "count": 0, "surfaces": [], "missing": ["evidence_chain"]}
        return report, ["hop:evidence_chain"]
    by_surface: dict[str, list[str]] = {}
    for hop in hops:
        if not isinstance(hop, dict):
            continue
        by_surface.setdefault(str(hop.get("surface")), []).append(str(hop.get("token") or ""))
    found: list[str] = []
    missing: list[str] = []
    for surface, tokens in by_surface.items():
        blob = _flatten(_surface_text(task_dir, surface))
        surface_ok = True
        for token in tokens:
            if token and _flatten(token) in blob:
                continue
            surface_ok = False
            missing.append(f"hop:{surface}:{token[:48]}")
        if surface_ok and surface:
            found.append(surface)
    report = {"ok": not missing and len(found) >= 3, "count": len(found),
              "surfaces": found, "missing": missing}
    return report, missing


def _stressor_surface(task_dir: Path) -> str:
    """Kb, chat, and workspace docs. Prompts and tickets do not carry this clue."""
    return "\n".join((
        _surface_text(task_dir, "chat"),
        _surface_text(task_dir, "kb"),
    ))


def _stressor_clues(task_dir: Path) -> tuple[dict, list[str]]:
    codes = blueprint_lint_stressors(task_dir)
    if not codes:
        return {"ok": True, "covered": [], "missing": []}, []
    blob = _flatten(_stressor_surface(task_dir))
    covered: list[str] = []
    missing: list[str] = []
    for code in codes:
        marker = STRESSOR_CLUES.get(code, {}).get("marker", "")
        if marker and marker in blob:
            covered.append(code)
        else:
            missing.append(code)
    report = {"ok": not missing, "covered": covered, "missing": missing}
    return report, [f"stressor:{code}" for code in missing]


def _hazard_clues(task_dir: Path, decision: dict, flat_text: str) -> tuple[dict, list[str]]:
    entries = _listed_hazards(task_dir, decision)
    if not entries:
        return {"ok": True, "covered": [], "missing": []}, []
    covered: list[str] = []
    missing: list[str] = []
    seen_codes: set[str] = set()
    for raw, code in entries:
        if code in seen_codes:
            if raw not in covered and code in covered:
                covered.append(raw)
            continue
        seen_codes.add(code)
        marker = HAZARD_CLUES.get(code, {}).get("marker", "")
        if marker and marker in flat_text:
            covered.append(code)
            if raw != code:
                covered.append(raw)
        else:
            missing.append(code)
    report = {"ok": not missing, "covered": covered, "missing": missing}
    return report, [f"hazard:{code}" for code in missing]


def _is_preserved_certified_package(task_dir: Path) -> bool:
    manifest_path = task_dir.parent / "phase3_delta_manifest.json"
    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = (manifest.get("tasks") or {}).get(task_dir.name)
        return isinstance(entry, dict) and entry.get("status") == "preserved_certified"
    except (OSError, ValueError, TypeError):
        return False


_GAP220_FORBIDDEN = (
    ("do not treat the cent gap as settled", "gap220:freeze_phrase"),
    ("must be fixed in the forward pipeline", "gap220:repair_order"),
    ("already retired", "gap220:already_retired"),
    ("retire the provisional", "gap220:retire_provisional"),
)
_GAP220_REQUIRED = (
    ("raw feed schema version", "gap220:schema_version"),
    ("gross or net", "gap220:gross_or_net"),
)


def _gap220_checklist(task_dir: Path) -> list[str]:
    """GAP-220 is an open gross-or-net discrepancy, not a repair order."""
    path = task_dir / "fixtures" / "workflow" / "issues.yaml"
    if not path.is_file():
        return []
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return ["gap220:unreadable"]
    issues = loaded.get("issues") if isinstance(loaded, dict) else None
    if not isinstance(issues, list):
        return []
    ticket = next((item for item in issues if isinstance(item, dict) and item.get("id") == "GAP-220"), None)
    if ticket is None:
        return []
    blob = _flatten(" ".join(_strings(ticket)))
    lowered = blob.lower()
    missing: list[str] = []
    if "warehouse.factory_corrections" not in blob:
        missing.append("gap220:corrections")
    if "var/logs/ingest_daemon.log" not in blob:
        missing.append("gap220:ingest_log")
    if _is_preserved_certified_package(task_dir):
        return missing
    for phrase, code in _GAP220_FORBIDDEN:
        if phrase in lowered:
            missing.append(code)
    if ticket.get("status") == "in_progress":
        missing.append("gap220:in_progress_hold")
    for phrase, code in _GAP220_REQUIRED:
        if phrase not in lowered:
            missing.append(code)
    if "forward pipeline" not in lowered:
        missing.append("gap220:forward_pipeline")
    labels = set(ticket.get("labels") or [])
    if not {"reconciliation", "triage"} <= labels:
        missing.append("gap220:labels")
    return missing


def lint_task(task_dir: Path) -> LintResult:
    """Trace required authority anchors to surfaces and scan for literal spoilers."""
    task_dir = Path(task_dir)
    text = _agent_text(task_dir)
    decision_path = task_dir / "tests" / "governance.json"
    if not decision_path.is_file():
        raise FileNotFoundError(decision_path)
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    # D1 ticket/owner/day, D2 blocker evidence, S1 the same anchors on a readable surface.
    required = {
        "ticket": decision["ticket"],
        "owner": decision["controller"],
        "closed_day": decision["closed_days"][0],
    }
    for index, page in enumerate(decision["authority_pages"], 1):
        required[f"authority_page_{index}"] = page
    blocker = decision.get("blocker")
    if decision["mode"] == "act-authorized":
        if decision.get("superseded_hold"):
            required["superseded_hold"] = decision["superseded_hold"]
        fact = decision.get("warehouse_fact") or {}
        if fact.get("sql"):
            required["warehouse_sql"] = fact["sql"]
    elif decision["mode"] == "withhold-blocked":
        if blocker == "scope_date_mismatch" and len(decision.get("closed_days") or []) > 1:
            required["unapproved_day"] = decision["closed_days"][1]
        elif blocker == "conditional_prerequisite_unmet":
            required["condition"] = decision.get("prerequisite_ticket") or "warehouse.partner_recon"
            required["warehouse_check"] = "warehouse.partner_recon"
        elif blocker == "wrong_approver_role":
            required["wrong_role"] = "Engineering lead"
        elif blocker == "delegation_expired_or_revoked":
            required["delegation_page"] = decision.get("delegation_page", "governance-delegations")
            required["delegation_end"] = decision.get("delegation_end", "")
        elif blocker == "threshold_cap_exceeded":
            required["approval_cap"] = "2500000"
            required["delta_relation"] = "warehouse.restatement_delta"
    elif decision["mode"] == "escalate-ambiguous":
        for source in decision.get("conflict_sources") or []:
            required[f"source_{source.get('name', 'conflict')}"] = source.get("token", "")
        if "source_partner_addendum" not in required and not decision.get("conflict_sources"):
            required["signed_source_a"] = "signed-rate-a"
            required["signed_source_b"] = "signed-rate-b"
        if decision.get("exact_question"):
            required["exact_question"] = decision["exact_question"]
    flat_text = _flatten(text)
    token_missing = [key for key, token in required.items() if token and _flatten(str(token)) not in flat_text]
    token_missing.extend(key for key, token in required.items() if not token)
    governance_hops, hop_missing = _governance_hops(task_dir, decision)
    hazard_report, hazard_missing = _hazard_clues(task_dir, decision, flat_text)
    stressor_report, stressor_missing = _stressor_clues(task_dir)
    gap_missing = _gap220_checklist(task_dir)
    missing = tuple([*token_missing, *hop_missing, *hazard_missing, *stressor_missing, *gap_missing])
    spoilers = [f"pattern:{pattern.pattern}" for pattern in SPOILER_PATTERNS if pattern.search(text)]
    if _FUTURE_PAYLOAD.search(text):
        spoilers.append("payload:factory_future")
    spoilers.extend(_decoy_labels(task_dir))
    for line in _solution_lines(task_dir):
        if line in text:
            spoilers.append(f"solution_line:{line[:64]}")
    units = (len(required) + len(decision.get("hops") or [])
             + len({code for _raw, code in _listed_hazards(task_dir, decision)})
             + len(blueprint_lint_stressors(task_dir)))
    misses = (len(token_missing) + len(hop_missing) + len(hazard_missing)
              + len(stressor_missing) + len(gap_missing))
    coverage = 1.0 if units == 0 else (units - misses) / units
    ok = (not missing and not spoilers and governance_hops["ok"]
          and hazard_report["ok"] and stressor_report["ok"])
    return LintResult(ok, tuple(spoilers), missing, coverage, governance_hops,
                      hazard_report, stressor_report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_dir", type=Path)
    args = parser.parse_args()
    result = lint_task(args.task_dir)
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
