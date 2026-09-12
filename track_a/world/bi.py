"""BI-values surface: dashboards are YAML of named SQL panels; rendering
returns computed values (not images) so 'dashboard disagrees with warehouse'
is a checkable condition (split_of_work.md §4).
"""
import argparse
import json
import sys
from pathlib import Path

import duckdb
import yaml

from .hashing import canon_value, json_canon

DB = Path("var/warehouse/warehouse.duckdb")
DASHBOARDS = Path("var/bi/dashboards")


def render(root: Path) -> dict:
    root = Path(root)
    out = {}
    con = duckdb.connect(str(root / DB), read_only=True)
    try:
        for f in sorted((root / DASHBOARDS).glob("*.yml")):
            spec = yaml.safe_load(f.read_text(encoding="utf-8"))
            panels = {}
            for panel in spec["panels"]:
                rows = con.execute(panel["sql"]).fetchall()
                panels[panel["name"]] = [[canon_value(c) for c in r] for r in rows]
            out[spec["dashboard"]] = panels
    finally:
        con.close()
    return out


def main(argv=None):
    a = argparse.ArgumentParser(prog="world.bi")
    a.add_argument("root", type=Path)
    ns = a.parse_args(argv)
    sys.stdout.buffer.write(json_canon(render(ns.root)) + b"\n")


if __name__ == "__main__":
    main()
