#!/usr/bin/env python3
"""stage_discovery writes discovery files onto an already-built world.

    gateway/.venv/Scripts/python.exe tasks/harness/test_native_discovery.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import native_stack  # noqa: E402


class StageDiscoveryTest(unittest.TestCase):
    def test_stage_discovery_writes_envrc_under_workspace(self):
        tmp = Path(tempfile.mkdtemp(prefix="native-stage-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        workspace = tmp / "world" / "workspace"
        workspace.mkdir(parents=True)
        world = {"world_root": str(tmp / "world"), "workspace": str(workspace)}
        result = native_stack.stage_discovery(world)
        envrc = workspace / ".envrc"
        self.assertTrue(envrc.is_file(), envrc)
        self.assertIn("JIRA_URL", envrc.read_text(encoding="utf-8"))
        self.assertEqual(result["root"], str(tmp / "world"))
        self.assertTrue(result["files"])


if __name__ == "__main__":
    unittest.main()
