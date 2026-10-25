"""Mechanical prose checks for the operational breadcrumb surfaces."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tasks.factory.hazards.t0_breadcrumbs import HAZARD_IDS, stage_breadcrumbs


STRESSOR_IDS = tuple(f"S{number:02d}" for number in range(1, 11))
SURFACES = (
    "workspace/docs/partner_feed_contract.md",
    "workspace/docs/ops_runbook.md",
    "var/logs/ingest_daemon.log",
    "var/logs/sftp_sync.log",
)
BANNED_TERMS = (
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
MAX_PROSE_WORDS = 34


def _prose_lines(relative: str, text: str) -> list[str]:
    """Return prose lines, omitting Markdown headings, tables, and code blocks."""
    if not relative.endswith(".md"):
        return [line for line in text.splitlines() if line.strip()]

    lines: list[str] = []
    in_code = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            continue
        if in_code or not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("|"):
            continue
        lines.append(line)
    return lines


def _sentences(line: str) -> list[str]:
    # Split at sentence punctuation followed by a capitalized sentence. This
    # keeps periods inside filenames, timestamps, and abbreviations intact.
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line) if part.strip()]


def _word_count(sentence: str) -> int:
    return len(re.findall(r"\b[\w]+(?:['’][\w]+)*\b", sentence))


def _assert_humanized(relative: str, text: str) -> None:
    assert "—" not in text, f"{relative} contains an em dash"
    assert "–" not in text, f"{relative} contains an en dash"
    assert " - " not in text, f"{relative} contains a spaced hyphen dash"

    folded = text.casefold()
    for term in BANNED_TERMS:
        assert not re.search(rf"(?<!\w){re.escape(term)}(?!\w)", folded), (
            f"{relative} contains banned AI-tell term {term!r}"
        )

    for line in _prose_lines(relative, text):
        for sentence in _sentences(line):
            assert _word_count(sentence) <= MAX_PROSE_WORDS, (
                f"{relative} has a { _word_count(sentence) }-word prose sentence: {sentence!r}"
            )


def _stage_case(world: Path, hazards: tuple[str, ...], stressors: tuple[str, ...]) -> None:
    stage_breadcrumbs(
        world,
        hazards,
        historical_day="2026-06-01",
        seed=31,
        stressor_ids=stressors,
    )


@pytest.mark.parametrize(
    ("hazards", "stressors", "case_name"),
    [
        pytest.param(HAZARD_IDS, STRESSOR_IDS, "combined", id="all-hazards-and-stressors"),
        *[
            pytest.param((hazard,), (), f"{hazard.lower()}-only", id=f"{hazard}-only")
            for hazard in HAZARD_IDS
        ],
        *[
            pytest.param((), (stressor,), f"{stressor.lower()}-only", id=f"{stressor}-only")
            for stressor in STRESSOR_IDS
        ],
    ],
)
def test_generated_operational_surfaces_follow_humanizer_rules(
    tmp_path: Path,
    hazards: tuple[str, ...],
    stressors: tuple[str, ...],
    case_name: str,
) -> None:
    world = tmp_path / case_name
    _stage_case(world, hazards, stressors)

    for relative in SURFACES:
        path = world / relative
        if case_name == "combined" or relative.startswith("var/logs/"):
            assert path.is_file(), f"missing generated surface: {relative}"
        if path.is_file():
            _assert_humanized(relative, path.read_text(encoding="utf-8"))
