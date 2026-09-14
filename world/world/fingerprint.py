"""World fingerprint: the two determinism gates plus a diagnostic.

- semantic_hash  — THE GATE. Canonical logical contents: warehouse schemas +
  canonically-ordered rows, scheduler state (incl. v-clock), BI definitions,
  workspace files (dbt project, landing CSVs, docs), and — only when the theme
  seeds one — the ingestion control-plane state (var/ingest/state.json).
  Equal across hosts <=> same world. Comparison excludes `git_head`
  (informational).
- provenance     — attestation: seed hash, generator + dependency versions,
  requirements.lock hash, image digest.
- raw_byte_hash  — diagnostic only (duckdb page layout may differ across
  hosts/versions); used as THE GATE for snapshot restore, where bytes must
  match exactly.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import duckdb

from .hashing import canon_value, file_hash, json_canon, json_hash, tree_hash

DB = Path("var/warehouse/warehouse.duckdb")
SCHEMAS = ("raw", "staging", "warehouse")


def _warehouse_hash(db_path: Path) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    catalog = db_path.stem  # duckdb names the catalog after the file stem
    tables = {}
    try:
        rows = con.execute(
            "select table_schema, table_name from information_schema.tables "
            "where table_schema in ('raw','staging','warehouse') "
            # BASE TABLES only. A view is a deterministic function of the tables
            # it reads, so hashing it is redundant - and a view whose body uses a
            # 2-part name breaks when the catalog shares the schema's name.
            "and table_type = 'BASE TABLE' order by 1, 2"
        ).fetchall()
        for schema, table in rows:
            cols = con.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_schema = ? and table_name = ? "
                "order by ordinal_position", [schema, table]).fetchall()
            h = hashlib.sha256()
            h.update(json_canon({"table": f"{schema}.{table}", "columns": cols}))
            order = ", ".join(f'"{c}"' for c, _ in cols)
            # three-part name: schema `warehouse` collides with the catalog name
            cur = con.execute(
                f'select * from "{catalog}"."{schema}"."{table}" order by {order}')
            for row in cur.fetchall():
                h.update(json_canon([canon_value(c) for c in row]) + b"\n")
            tables[f"{schema}.{table}"] = h.hexdigest()
    finally:
        con.close()
    return {"tables": tables, "hash": json_hash(tables)}


def semantic(root: Path) -> dict:
    root = Path(root)
    sections = {
        "warehouse": _warehouse_hash(root / DB),
        "scheduler": json_hash(json.loads(
            (root / "var" / "sched" / "state.json").read_text(encoding="utf-8"))),
        "bi": tree_hash(root / "var" / "bi"),
        "workspace": tree_hash(root / "workspace", exclude=(".git",)),
    }
    # Conditional: only themes that seed a connector control plane (an
    # optional write_ingest gen hook) get the section, so worlds without one
    # keep their recorded semantic hash.
    ingest = root / "var" / "ingest" / "state.json"
    if ingest.is_file():
        sections["ingest"] = json_hash(json.loads(ingest.read_text(encoding="utf-8")))
    return sections


def semantic_hash(root: Path) -> str:
    return json_hash(semantic(root))


def git_head(root: Path) -> str | None:
    r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                       cwd=Path(root) / "workspace")
    return r.stdout.decode().strip() if r.returncode == 0 else None


def report(root: Path) -> dict:
    root = Path(root)
    sem = semantic(root)
    prov_path = root / "build" / "provenance.json"
    return {
        "semantic": sem,
        "semantic_hash": json_hash(sem),
        "git_head": git_head(root),
        "raw_byte_hash": file_hash(root / DB),
        "provenance": json.loads(prov_path.read_text(encoding="utf-8"))
        if prov_path.exists() else None,
    }


def main(argv=None):
    p = argparse.ArgumentParser(prog="world.fingerprint")
    p.add_argument("root", type=Path)
    p.add_argument("--full", action="store_true", help="print the full report")
    a = p.parse_args(argv)
    if a.full:
        print(json.dumps(report(a.root), indent=2, sort_keys=True))
    else:
        print(semantic_hash(a.root))


if __name__ == "__main__":
    sys.exit(main())
