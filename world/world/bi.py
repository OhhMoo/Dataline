"""BI-values surface: dashboards are YAML of named SQL panels; rendering
returns computed values (not images) so 'dashboard disagrees with warehouse'
is a checkable condition (agent.md §9.3 Track B).
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
    # Connect in-memory and ATTACH under a neutral alias rather than opening the
    # file directly. DuckDB names the catalog after the file stem, so opening
    # warehouse.duckdb directly makes catalog == schema == "warehouse" and every
    # reference to warehouse.<table> raises "Ambiguous reference to catalog or
    # schema". An alias removes the collision, and `USE wh.warehouse` keeps
    # dashboard SQL writable in the natural two-part form.
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(root / DB).replace("'", "''") + "' AS wh (READ_ONLY)")
    con.execute("USE wh.warehouse")
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
