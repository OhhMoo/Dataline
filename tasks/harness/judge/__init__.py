"""Reject-only task-clarity gate.

Not a pass@1 signal. ``reward.py``, ``outcome.py``, and ``tests/grade.py``
must not import this package.
"""
from __future__ import annotations

from .clarity_gate import consensus, judge_brief, scan_brief

__all__ = ["consensus", "judge_brief", "scan_brief"]
