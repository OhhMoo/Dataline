"""Judge tools cannot move the score.

    python3 tasks/harness/test_judge_isolation.py

Reads reward.py and outcome.py as text. Imports episode_reward and checks
the false_claim scalar. Imports clarity_gate only when that module is present.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parent
_FORBIDDEN = ("harness.judge", "clarity_gate", "exploit_agent", "exploit_loop")


def _ensure_harness_on_path() -> None:
    harness = str(_HARNESS)
    if harness not in sys.path:
        sys.path.insert(0, harness)


def _source(name: str) -> str:
    return (_HARNESS / name).read_text(encoding="utf-8")


def test_reward_and_outcome_do_not_name_judge_tools():
    for name in ("reward.py", "outcome.py"):
        text = _source(name)
        for needle in _FORBIDDEN:
            assert needle not in text, f"{name} contains {needle!r}"


def test_false_claim_episode_reward_is_minus_one():
    _ensure_harness_on_path()
    from reward import episode_reward

    scored = episode_reward("false_claim", "control")
    assert scored["reward"] == -1.0


def test_clarity_gate_consensus_never_accepts():
    _ensure_harness_on_path()
    try:
        from judge.clarity_gate import consensus
    except ImportError as exc:
        pytest.skip(
            "clarity_gate did not import "
            f"({type(exc).__name__}: {exc}). "
            "Skipped only the consensus check. "
            "reward.py and outcome.py source text is still checked "
            "for harness.judge, clarity_gate, exploit_agent, and exploit_loop."
        )
    assert consensus(["hold", "reject"]) == "reject"
    assert consensus(["hold"]) != "accept"


if __name__ == "__main__":
    own = (
        test_reward_and_outcome_do_not_name_judge_tools,
        test_false_claim_episode_reward_is_minus_one,
        test_clarity_gate_consensus_never_accepts,
    )
    failed = 0
    for test in own:
        try:
            test()
        except BaseException as exc:
            if type(exc).__name__ == "Skipped":
                print(f"SKIP {test.__name__}: {exc}")
                continue
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
            continue
        print(f"PASS {test.__name__}")
    raise SystemExit(failed)
