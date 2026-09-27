"""Judge tools, all off the score.

  clarity_gate   reject-only task-clarity gate (never accepts a brief)
  model_client   the model behind any of them: `claude --print`, k samples,
                 strict majority with `unknown`, provenance incl. model family
  grader_audit   audits the GRADER's verdicts check by check against the world;
                 a `grader_wrong` sets needs_fixer, never a point

Not a pass@1 signal. ``reward.py``, ``outcome.py``, and ``tests/grade.py``
must not import this package (test_judge_isolation.py).
"""
from __future__ import annotations

from .clarity_gate import consensus, judge_brief, scan_brief

__all__ = ["consensus", "judge_brief", "scan_brief"]
