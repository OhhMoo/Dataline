#!/usr/bin/env python3
"""Reject-only clarity gate for a task brief.

A brief is "reject" or "hold". There is no accept verdict, and this module
is not a pass@1 signal. ``reward.py``, ``outcome.py``, and any
``tests/grade.py`` must not import it.

The deterministic scan follows the Terminal-Bench checks that behaviour
described in the instruction is tested, and that a solution or an expected
answer table is not sitting in the text the agent reads. The optional model
hook can add a reject. It cannot accept. With no client it returns "hold"
and does not touch the network.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Stable order. Each code is emitted at most once.
_REASON_ORDER = (
    "empty_brief",
    "leaked_solution",
    "self_referential",
    "behaviour_untested",
)

# "the solution isn't" does not match: "isn't" is not the word "is".
_LEAKED_PHRASE = re.compile(r"\bthe solution is\b", re.IGNORECASE)
_FENCE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)
_EXPECTED_WORD = re.compile(r"\bexpected\b", re.IGNORECASE)

# The brief talking about the grader, the tests, or itself — not the job.
_SELF_REF = re.compile(
    r"(?i)(?:"
    r"\bthis\s+(?:prompt|brief|instruction)\b"
    r"|\b(?:the\s+)?(?:unit\s+|hidden\s+)?tests?\s+"
    r"(?:will|shall|must|should)\s+(?:check|verify|assert|grade)\b"
    r"|\b(?:the\s+)?grader\b"
    r"|\bgrading\s+(?:script|harness|zone)\b"
    r"|\b(?:as|per)\s+(?:stated|described|written)\s+in\s+this\b"
    r")"
)

# Only an explicit must/shall plus one of these counts as a described
# outcome. Coworker prose ("have a look", "sort it out") does not.
_OUTCOME_VERBS = (
    "backfill",
    "deduplicate",
    "delete",
    "insert",
    "overwrite",
    "publish",
    "recompute",
    "recover",
    "republish",
    "restate",
    "restore",
    "update",
    "withhold",
)
_VERB_ALT = "|".join(sorted(_OUTCOME_VERBS, key=len, reverse=True))
_DESCRIBED_VERB = re.compile(
    rf"\b(?:must|shall)\s+(?:not\s+)?(?:be\s+)?({_VERB_ALT})(?:ed|ing|es|d|s)?\b",
    re.IGNORECASE,
)
_VERDICT_LINE = re.compile(
    r"(?:verdict\s*[:=]\s*)?(reject|hold)\Z",
    re.IGNORECASE,
)


def scan_brief(text: str, tests_text: str = "") -> list[str]:
    """Reason codes for a brief. Empty means the scanner does not reject.

    ``behaviour_untested`` is emitted only when ``tests_text`` is non-blank
    and a verb the brief states with "must" or "shall" is absent from those
    tests. Other prose is left alone.
    """
    brief = text if isinstance(text, str) else ""
    if not brief.strip():
        return ["empty_brief"]

    found: set[str] = set()
    if _leaked(brief):
        found.add("leaked_solution")
    if _SELF_REF.search(brief):
        found.add("self_referential")
    if _behaviour_untested(brief, tests_text):
        found.add("behaviour_untested")
    return [code for code in _REASON_ORDER if code in found]


def consensus(verdicts: list[str]) -> str:
    """Combine reject/hold votes. Any reject wins. All holds hold.

    An empty list rejects. A token other than "reject" or "hold" rejects.
    This function does not return "accept".
    """
    tokens = [str(verdict).strip().lower() for verdict in verdicts]
    if not tokens:
        return "reject"
    if any(token == "reject" for token in tokens):
        return "reject"
    if all(token == "hold" for token in tokens):
        return "hold"
    return "reject"


def judge_brief(text: str, *, client: object | None = None) -> str:
    """Model hook. No client means "hold", with no network and no API key.

    A client is an object with ``judge(text)`` or a callable. Only a reply
    whose first line is ``reject`` or ``verdict: reject`` (or a dict
    ``{"verdict": "reject"}``) becomes "reject". ``accept`` and anything
    else become "hold".
    """
    if client is None:
        return "hold"
    if callable(getattr(client, "judge", None)):
        raw = client.judge(text)  # type: ignore[attr-defined]
    elif callable(client):
        raw = client(text)
    else:
        raise TypeError("client must be callable or provide judge(text)")
    return _coerce_model(raw)


def _leaked(brief: str) -> bool:
    if _LEAKED_PHRASE.search(brief):
        return True
    for match in _FENCE.finditer(brief):
        info = match.group(1)
        body = match.group(2)
        if not _pipe_table(body):
            continue
        if _EXPECTED_WORD.search(info):
            return True
        preceding = _preceding_line(brief, match.start())
        if _EXPECTED_WORD.search(preceding):
            return True
    return False


def _pipe_table(body: str) -> bool:
    rows = 0
    for line in body.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith("|")
            and stripped.endswith("|")
            and stripped.count("|") >= 3
        ):
            rows += 1
    return rows >= 2


def _preceding_line(brief: str, start: int) -> str:
    head = brief[:start].rstrip()
    if not head:
        return ""
    return head.splitlines()[-1]


def _behaviour_untested(brief: str, tests_text: str) -> bool:
    tests = tests_text if isinstance(tests_text, str) else ""
    if not tests.strip():
        return False
    missing = False
    seen = False
    for match in _DESCRIBED_VERB.finditer(brief):
        seen = True
        stem = match.group(1)
        if not re.search(
            rf"\b{re.escape(stem)}(?:ed|ing|es|d|s)?\b",
            tests,
            re.IGNORECASE,
        ):
            missing = True
    return seen and missing


def _coerce_model(raw: object) -> str:
    if isinstance(raw, dict):
        raw = raw.get("verdict", "")
    lines = str(raw).strip().splitlines()
    first = lines[0].strip() if lines else ""
    if _VERDICT_LINE.fullmatch(first) and first.lower().endswith("reject"):
        return "reject"
    return "hold"


def _decide(
    text: str,
    tests_text: str = "",
    client: object | None = None,
) -> dict:
    reasons = scan_brief(text, tests_text)
    scanner_vote = "reject" if reasons else "hold"
    model_vote = judge_brief(text, client=client)
    return {
        "reasons": reasons,
        "consensus": consensus([scanner_vote, model_vote]),
    }


class _Exit1Parser(argparse.ArgumentParser):
    """Usage errors are bad input (exit 1), not a clarity reject (exit 2)."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Exit1Parser(
        prog="tasks.harness.judge.clarity_gate",
        description=(
            "Reject-only clarity gate. Prints {reasons, consensus}. "
            "Exit 0 on hold, 2 on reject. Does not call a model."
        ),
    )
    parser.add_argument("--brief", required=True, type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 0 if code in (None, 0) else int(code) if isinstance(code, int) else 1
    try:
        text = args.brief.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"clarity_gate: {exc}\n")
        return 1
    report = _decide(text)
    sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    return 0 if report["consensus"] == "hold" else 2


if __name__ == "__main__":
    raise SystemExit(main())
