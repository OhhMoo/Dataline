"""Measurement defaults are --driver native --topology container.

    python -m pytest test_native_defaults.py -q

The three CLI entry points build argparse only inside main(). --help is
the public surface: after the flip it names native and container as the
defaults. mcp and host stay as choices.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_VENV = Path(__file__).resolve().parents[2] / "gateway" / ".venv"
PY = next((p for p in (_VENV / "bin" / "python", _VENV / "Scripts" / "python.exe")
           if p.exists()), Path(sys.executable))
SCRIPTS = ("run_rollouts.py", "rollout.py", "run_queue.py")


def help_text(script: str) -> str:
    r = subprocess.run(
        [str(PY), str(HERE / script), "--help"],
        capture_output=True, text=True, cwd=str(HERE),
    )
    assert r.returncode == 0, f"{script} --help exited {r.returncode}: {r.stderr}"
    return r.stdout


def test_help_defaults_are_native_and_container():
    for script in SCRIPTS:
        text = help_text(script)
        assert "default: native" in text, script
        assert "default: container" in text, script


def test_mcp_and_host_remain_choices():
    for script in SCRIPTS:
        text = help_text(script)
        assert "{mcp," in text or "mcp" in text, script
        assert "{host," in text or "host" in text, script
