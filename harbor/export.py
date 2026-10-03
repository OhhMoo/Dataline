#!/usr/bin/env python3
"""Export the Alkera tasks as a Harbor dataset. Thin entry: see alkera_harbor.

    gateway/.venv/bin/python harbor/export.py --output-dir harbor/dist/alkera-de

`harbor/` is deliberately NOT a Python package (no __init__.py), so it can never
shadow the real `harbor` distribution; this script puts its own directory on
sys.path and runs `alkera_harbor.main`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alkera_harbor.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
