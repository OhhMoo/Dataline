#!/usr/bin/env python
"""The `quota_exhausted` exit reason and class. No model, no key.

    python test_quota_class.py

A spend-limit refusal used to read as `agent_error` -> `harness_error` ->
"needs human review" (GT004 `authorized-20260917-k5`). These checks pin the
two things that make it its own class: the words that count as a limit
message, and the rule that a subject which exited cleanly is never
relabelled on prose alone.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_under_test as aut   # noqa: E402
import classify as classifier    # noqa: E402
import report                    # noqa: E402

FAILS: list[str] = []


def check(cond: bool, name: str, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f": {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


LIMIT_MESSAGES = [
    "You've hit your monthly spend limit. Your weekly limit resets Sep 21, 5pm",
    "Error: usage limits exceeded for this account",
    "403 Forbidden: quota exceeded",
    "rate limit reached, retry after 60s",
    "HTTP 429 Too Many Requests",
    "insufficient_credits",
]
NOT_LIMIT_MESSAGES = [
    "stopped at the tool-call limit of 150",
    "the wall clock limit was reached before the report was written",
    "Traceback (most recent call last): KeyError: 'rows'",
    "",
]


def main() -> int:
    print("QUOTA_PATTERN")
    for m in LIMIT_MESSAGES:
        check(aut.looks_like_quota_exhaustion(m), f"limit message: {m[:48]!r}")
    for m in NOT_LIMIT_MESSAGES:
        check(not aut.looks_like_quota_exhaustion(m), f"not a limit message: {m[:48]!r}")

    print("exit_reason_for")
    check(aut.exit_reason_for(0, {}, "done") == "completed", "clean exit completes")
    check(aut.exit_reason_for(0, {}, "we hit the rate limit twice and retried") == "completed",
          "a clean exit is never relabelled on prose")
    check(aut.exit_reason_for(1, {}, LIMIT_MESSAGES[0]) == "quota_exhausted",
          "failed process + limit text in final_text")
    check(aut.exit_reason_for(1, {}, "", stderr_tail=LIMIT_MESSAGES[2]) == "quota_exhausted",
          "failed process + limit text in stderr")
    check(aut.exit_reason_for(0, {"is_error": True, "subtype": "error_quota_exhausted"}, "")
          == "quota_exhausted", "result subtype decides it")
    check(aut.exit_reason_for(1, {}, "KeyError: rows") == "agent_error",
          "a plain crash stays agent_error")

    print("classify")

    class _Spec:
        wall_clock_seconds, max_tool_calls = 600, 150

    def _classify(reason, text):
        return classifier.classify(
            spec=_Spec(), score={"total": 0, "max": 33}, score_stable=True,
            grader_crashed=False, harness_failure=None,
            agent_exit_reason=reason, blindness={}, action_rows=[],
            surfaces={}, artifact_present=False, agent_final_text=text)

    q = _classify("agent_error", LIMIT_MESSAGES[0])
    check(q["classification"] == "quota_exhausted",
          "agent_error + limit text classifies as quota_exhausted", q["classification"])
    check(not q["scored"] and not q["needs_review"],
          "quota_exhausted is unscored and not needs_review")
    h = _classify("agent_error", "KeyError: rows")
    check(h["classification"] == "harness_error" and h["needs_review"],
          "agent_error without limit text stays harness_error + needs_review")
    check("quota_exhausted" in report.CLASSES, "class exists in report")
    check(classifier.quota_exhaustion_evidence("quota_exhausted", "", "") is not None,
          "recorded reason is evidence")
    check(classifier.quota_exhaustion_evidence("agent_error", LIMIT_MESSAGES[0], "") is not None,
          "pre-class record: agent_error + limit text is evidence (--reclassify)")
    check(classifier.quota_exhaustion_evidence("agent_error", "KeyError", "") is None,
          "agent_error without limit text is not")
    check(classifier.quota_exhaustion_evidence("completed", LIMIT_MESSAGES[0], "") is None,
          "a completed subject is never relabelled")

    print(f"\n  {len(FAILS)} failure(s)" if FAILS else "\n  OK: 0 failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
