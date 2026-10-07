"""Alkera -> Harbor adapter.

Turns every (task, arm) of every task package (`tasks/GT*`, `tasks/BI*`: any
`tasks/<dir>` with a task.toml) into one self-contained Harbor task
directory, plus a `dataset.toml`, following Harbor's adapter pattern
(`adapters/<name>`: an adapter class with `run()`, an argparse `main`).

    gateway/.venv/bin/python harbor/export.py --output-dir harbor/dist/alkera-de

The generated tree is a build artifact (gitignored); this package and the
templates under `harbor/template/` are the source of truth. The binding design
is `harbor/README.md`.
"""

__version__ = "0.1.0"
