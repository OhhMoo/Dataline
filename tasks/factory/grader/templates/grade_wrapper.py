#!/usr/bin/env python3
"""
The grader of a generated task: tests/manifest.yaml + tests/spec.json, scored by
tasks/factory/grader/declarative_grade.py. This file holds no grading logic;
it locates the repo and the task and delegates. Same argv as task.toml
`grade_argv` (--world --grading --artifact --workspace --world-root --json).
"""
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent
REPO = TASK.parent.parent
sys.path.append(str(REPO))   # APPENDED: the repo root's `world/` must not shadow world/world

from tasks.factory.grader import declarative_grade                # noqa: E402

if __name__ == "__main__":
    raise SystemExit(declarative_grade.main(task_dir=TASK))
