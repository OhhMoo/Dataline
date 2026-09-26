#!/usr/bin/env python3
"""Leakage scanner checks. Stdlib only besides PyYAML via leakage_check."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import yaml

from leakage_check import scan

HERE = Path(__file__).resolve().parent


class ScanTests(unittest.TestCase):
    def test_enforce_false_keeps_ok_when_hit(self) -> None:
        result = scan("hello nbrennan", ["nbrennan"], enforce=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["hits"], ["nbrennan"])

    def test_enforce_true_fails_when_hit(self) -> None:
        result = scan("hello nbrennan", ["nbrennan"], enforce=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["hits"], ["nbrennan"])

    def test_enforce_true_passes_when_clean(self) -> None:
        result = scan("hello", ["nbrennan"], enforce=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["hits"], [])

    def test_script_on_real_task_toml_exits_zero_while_disabled(self) -> None:
        flags = yaml.safe_load((HERE / "flags.yaml").read_text(encoding="utf-8"))
        self.assertFalse(flags["hidden_blocker"]["enabled"])
        proc = subprocess.run(
            [sys.executable, str(HERE / "leakage_check.py")],
            cwd=HERE,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("prompt_hits: []", proc.stdout)
        self.assertIn("nbrennan", proc.stdout)
        self.assertIn("metadata_hits:", proc.stdout)


if __name__ == "__main__":
    unittest.main()
