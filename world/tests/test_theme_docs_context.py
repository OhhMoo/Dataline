"""Theme docs context gate (overnight Phase 4 desaturation, plan_10_20 §4.3).

Asserts three things:

1. Every theme ships enriched ``workspace/docs`` context: an
   ``architecture_overview.md`` and a ``reconciliation_sla.md`` of real
   operational prose (SLA windows, onboarding history, legacy migration
   notes, DuckDB schema overview) that point operators at the live-state
   evidence (``warehouse.factory_corrections``, ``var/logs/``,
   ``fixtures/workflow/issues.yaml``) instead of one-line fix instructions.
2. No theme doc contains a spoiler phrase or a per-task generated doc name
   (``partner_feed_contract.md`` / ``ops_runbook.md`` belong to the task
   generator, not the theme).
3. The schedule-sync suites pass 100%: ``world/world/scheduler.py`` stays
   compatible with comment-free ``workspace/orchestration/schedule.yml``
   files.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORLD_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = WORLD_ROOT.parent
THEMES_DIR = WORLD_ROOT / "world" / "themes"

THEMES = sorted(
    p.name for p in THEMES_DIR.iterdir() if (p / "theme.yaml").is_file()
)

REQUIRED_DOCS = ("architecture_overview.md", "reconciliation_sla.md")
MIN_DOC_CHARS = 2000

# Sections/phrases the enriched docs must carry (case-insensitive substring).
REQUIRED_SNIPPETS = {
    "architecture_overview.md": (
        "schema overview",
        "legacy migration",
        "warehouse.factory_corrections",
        "var/logs/ingest_daemon.log",
        "var/logs/sftp_sync.log",
        "fixtures/workflow/issues.yaml",
    ),
    "reconciliation_sla.md": (
        "sla",
        "onboarding",
        "legacy",
        "warehouse.factory_corrections",
        "var/logs/ingest_daemon.log",
        "var/logs/sftp_sync.log",
        "fixtures/workflow/issues.yaml",
    ),
}

SPOILER_RE = re.compile(
    r"\b(?:R[1-4]|H(?:0[1-9]|1[0-4])|S(?:0[1-9]|10))\b"
    r"|EV-FUT-|factory_future|oracle_|test_spec|build_manifest"
    r"|ingest_feed|fold_ledger|publish_handoff|B-FANOUT|B-CUTOVER",
    re.IGNORECASE,
)

# Phase 5 (plan_10_22): the trap sentence that told agents provisional
# cutover logic could only be retired by a signed ticket, which froze the
# pre-cutover feed adjustments in place on 13 floor tasks.
TRAP_PHRASES = (
    "provisional logic stays until a signed ticket retires it",
    "signed ticket retires",
    "stays until a signed ticket",
)

# What the rewritten Legacy section must establish: closed-window
# restatements are governed by warehouse.factory_corrections, while
# pre-cutover feed adjustments retire on the cutover audit evidence in
# var/logs/ingest_daemon.log (feed delivers net amount_cents directly).
LEGACY_SECTION_REQUIRED = (
    "warehouse.factory_corrections",
    "var/logs/ingest_daemon.log",
    "cutover",
    "net",
)

GENERATED_DOC_NAMES = ("partner_feed_contract.md", "ops_runbook.md")

# Humanizer gate (plan_10_24): no spaced parenthetical hyphens, no
# em/en dashes, and no AI-tell vocabulary in non-table prose.
HUMANIZER_BANNED_TERMS = (
    "delve",
    "underscore",
    "intricate",
    "tapestry",
    "testament",
    "pivotal",
    "showcase",
    "foster",
    "realm",
    "seamless",
    "groundbreaking",
    "paramount",
    "notably",
    "importantly",
    "it is worth noting",
    "moreover",
    "furthermore",
    "additionally",
)
HUMANIZER_BANNED_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(term) for term in HUMANIZER_BANNED_TERMS)
    + r")\b",
    re.IGNORECASE,
)


def _docs_dir(theme: str) -> Path:
    return THEMES_DIR / theme / "workspace" / "docs"


def test_twelve_themes_discovered():
    assert len(THEMES) == 12, f"expected 12 themes, found {THEMES}"


@pytest.mark.parametrize("theme", THEMES)
def test_theme_has_enriched_docs(theme: str):
    docs = _docs_dir(theme)
    assert docs.is_dir(), f"{theme}: no workspace/docs directory"
    for name in REQUIRED_DOCS:
        path = docs / name
        assert path.is_file(), f"{theme}: missing docs/{name}"
        text = path.read_text(encoding="utf-8")
        assert text.isascii(), f"{theme}: docs/{name} must be ASCII-only"
        assert len(text) >= MIN_DOC_CHARS, (
            f"{theme}: docs/{name} is only {len(text)} chars "
            f"(min {MIN_DOC_CHARS}) — not an enriched context doc"
        )
        lowered = text.lower()
        for snippet in REQUIRED_SNIPPETS[name]:
            assert snippet in lowered, (
                f"{theme}: docs/{name} lacks required content {snippet!r}"
            )


@pytest.mark.parametrize("theme", THEMES)
def test_docs_free_of_spoiler_phrases(theme: str):
    docs = _docs_dir(theme)
    offenders = []
    for path in sorted(docs.rglob("*.md")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = SPOILER_RE.search(line)
            if match:
                offenders.append(
                    f"{path.relative_to(THEMES_DIR)}:{lineno}: {match.group(0)!r}"
                )
    assert not offenders, "spoiler phrases found:\n" + "\n".join(offenders)


def test_theme_docs_humanized_no_spaced_dashes_or_ai_tells():
    offenders = []
    for theme in THEMES:
        docs = _docs_dir(theme)
        for name in REQUIRED_DOCS:
            path = docs / name
            assert path.is_file(), f"{theme}: missing docs/{name}"
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if line.lstrip().startswith("|"):
                    continue
                if " - " in line:
                    offenders.append(
                        f"{theme}/docs/{name}:{lineno}: spaced hyphen "
                        f"{line.strip()!r}"
                    )
                if "\u2014" in line or "\u2013" in line:
                    offenders.append(
                        f"{theme}/docs/{name}:{lineno}: em/en dash "
                        f"{line.strip()!r}"
                    )
                match = HUMANIZER_BANNED_RE.search(line)
                if match:
                    offenders.append(
                        f"{theme}/docs/{name}:{lineno}: AI-tell "
                        f"{match.group(0)!r}"
                    )
    assert not offenders, "unhumanized theme docs:\n" + "\n".join(offenders)


def _legacy_section(text: str) -> str:
    """The `## Legacy and provisional rules` section body (up to the next H2)."""
    match = re.search(
        r"^## Legacy and provisional rules\s*$(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1) if match else ""


@pytest.mark.parametrize("theme", THEMES)
def test_reconciliation_sla_has_no_retirement_trap(theme: str):
    path = _docs_dir(theme) / "reconciliation_sla.md"
    assert path.is_file(), f"{theme}: missing docs/reconciliation_sla.md"
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    for phrase in TRAP_PHRASES:
        assert phrase not in lowered, (
            f"{theme}: reconciliation_sla.md still carries the retirement "
            f"trap {phrase!r} (agents refuse to remove obsolete pre-cutover "
            "feed adjustments without a ticket)"
        )
    section = _legacy_section(text).lower()
    assert section, f"{theme}: no '## Legacy and provisional rules' section"
    for token in LEGACY_SECTION_REQUIRED:
        assert token in section, (
            f"{theme}: Legacy section lacks {token!r} — it must name "
            "var/logs/ingest_daemon.log as the cutover verification source "
            "and warehouse.factory_corrections as the closed-window authority"
        )


@pytest.mark.parametrize("theme", THEMES)
def test_no_generated_doc_names_in_theme(theme: str):
    docs = _docs_dir(theme)
    for name in GENERATED_DOC_NAMES:
        assert not (docs / name).exists(), (
            f"{theme}: docs/{name} is generated per task by the factory and "
            "must not live in the theme workspace"
        )


def test_all_theme_bi_dashboard_ids_match_filenames() -> None:
    """Every bi/*.yml's dashboard id equals its filename stem.

    The `~bi_list` surface probe keys dashboards by id; a mismatch (the
    Phase-5 `clearing_settlement` defect) fails the probe with
    `invalid_dashboard` and costs live-provenance on every rollout of the
    theme's tasks.
    """
    themes_root = Path(__file__).resolve().parents[1] / "world" / "themes"
    yml_paths = sorted(themes_root.glob("*/bi/*.yml"))
    assert yml_paths, "no theme BI definitions found"
    for yml_path in yml_paths:
        doc = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
        assert isinstance(doc, dict), f"{yml_path} is not a mapping"
        assert doc.get("dashboard") == yml_path.stem, (
            f"{yml_path} has dashboard={doc.get('dashboard')!r} "
            f"!= stem {yml_path.stem!r}"
        )


def test_schedule_sync_suites_pass():
    """The two schedule-sync suites must pass 100% (no comment spoiler needed)."""
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO_ROOT), str(WORLD_ROOT), pythonpath) if part
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "world/tests/test_schedule_sync.py",
            "world/tests/test_workspace_schedule_sync.py",
            "-q",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-15:])
    assert proc.returncode == 0, f"schedule-sync suites not green:\n{tail}"
    assert " failed" not in proc.stdout, f"failures in schedule-sync suites:\n{tail}"
