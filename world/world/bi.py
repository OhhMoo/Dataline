"""BI-values surface: dashboards are YAML of named SQL panels; rendering
returns computed values (not images) so 'dashboard disagrees with warehouse'
is a checkable condition (agent.md §9.3 Track B).

A legacy panel is the degenerate case of the semantic core
(docs/environments/BI_BUILD_PLAN.md, phase 1 "Migration"): a Visual whose only
field is `sql`, whose rows are the query's rows, unchanged. Each panel goes
through `world.semantic.render`, so there is one evaluator for every
dashboard - SQL panels here, semantic-model visuals for the vendor front-ends.
The output is byte-identical to what this module printed before the semantic
core existed (world/tests/test_semantic_engine.py pins every theme's bytes):
same connection discipline (in-memory, the file ATTACHed READ_ONLY as `wh`,
`USE wh.warehouse`, one thread), same canonical values, same last-panel-wins
when two panels share a name.
"""
import argparse
import sys
from pathlib import Path

import yaml

from .hashing import json_canon
from .semantic.model import Model, Visual
from .semantic.render import Session

DB = Path("var/warehouse/warehouse.duckdb")
DASHBOARDS = Path("var/bi/dashboards")

#: SQL panels read no semantic model; the engine still wants one to hold.
LEGACY = Model("legacy dashboards", ())


def render(root: Path) -> dict:
    root = Path(root)
    out = {}
    # One engine session (one read-only connection) for every dashboard: the
    # Session ATTACHes the file under the neutral alias `wh` rather than opening
    # it directly. DuckDB names the catalog after the file stem, so opening
    # warehouse.duckdb directly makes catalog == schema == "warehouse" and every
    # reference to warehouse.<table> raises "Ambiguous reference to catalog or
    # schema". A SQL visual runs under `USE wh.warehouse`, which keeps dashboard
    # SQL writable in the natural two-part (or bare) form.
    with Session(root / DB) as engine:
        for f in sorted((root / DASHBOARDS).glob("*.yml")):
            spec = yaml.safe_load(f.read_text(encoding="utf-8"))
            panels = {}
            for panel in spec["panels"]:
                visual = Visual(panel["name"], sql=panel["sql"])
                panels[panel["name"]] = engine.visual(LEGACY, visual)["rows"]
            out[spec["dashboard"]] = panels
    return out


def main(argv=None):
    a = argparse.ArgumentParser(prog="world.bi")
    a.add_argument("root", type=Path)
    ns = a.parse_args(argv)
    sys.stdout.buffer.write(json_canon(render(ns.root)) + b"\n")


if __name__ == "__main__":
    main()
