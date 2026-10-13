#!/usr/bin/env python3
"""Export the Alkera tasks as a Harbor dataset. Thin entry: see alkera_harbor.

    gateway/.venv/bin/python harbor/export.py --output-dir harbor/dist/alkera-de
    gateway/.venv/bin/python harbor/export.py --tooltour \\
        --output-dir harbor/dist/alkera-tooltour [--tools duckdb,sched]

`--tooltour` exports the tool tour instead of the task packages: one Harbor
task per tool-tour probe (alkera_harbor.tooltour; `--tooltour --help`).

`harbor/` is deliberately NOT a Python package (no __init__.py), so it can never
shadow the real `harbor` distribution; this script puts its own directory on
sys.path and runs `alkera_harbor.main` (or `alkera_harbor.tooltour.main`).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alkera_harbor.main import main  # noqa: E402

TOOLTOUR_FLAG = "--tooltour"

if __name__ == "__main__":
    argv = sys.argv[1:]
    if TOOLTOUR_FLAG in argv:
        from alkera_harbor.tooltour import main as tooltour_main
        sys.exit(tooltour_main([a for a in argv if a != TOOLTOUR_FLAG]))
    sys.exit(main())
