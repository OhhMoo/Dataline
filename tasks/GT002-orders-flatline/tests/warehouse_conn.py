#!/usr/bin/env python3
"""
The ONE way anything in this task opens the warehouse.

§9.2.1 deploys the warehouse at `/var/warehouse/warehouse.duckdb` and the schema
inside it is also called `warehouse`. DuckDB names the catalog after the file
stem, so opening that file directly makes catalog and schema collide and EVERY
two-part reference raises:

    Binder Error: Ambiguous reference to catalog or schema "warehouse"

Measured on duckdb 1.5.5 at the contract filename, 2026-09-13:

    direct connect   SELECT ... FROM warehouse.fct_orders  -> Binder Error
    direct connect   SELECT ... FROM raw.orders_topic      -> OK
    direct connect   the shipped pipelines/orders_sink.sql -> Binder Error
    :memory: + ATTACH AS wh + USE wh.warehouse             -> all OK, and the
                                                              sink raises the
                                                              Conversion Error
                                                              it is supposed to

Only `warehouse.*` collides, which is exactly what makes this invisible: a
harness that touches `raw.*` and opens a file named something other than
warehouse.duckdb passes every test while the deployed world is unusable. That is
how GT001 shipped TASK VALID with an oracle that aborted on step 1 of 4 - its
baselines ran against `tmp/world.duckdb`, a stem that does not collide.

So two rules, and both are load-bearing:

  1. Never `duckdb.connect(<the warehouse file>)`. Use `open_warehouse()`.
  2. Test against the CONTRACT FILENAME. `deployed_copy()` puts a copy at
     var/warehouse/warehouse.duckdb so the collision is reproduced rather than
     dodged. run_baselines.py uses it for every baseline.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import duckdb

CONTRACT_RELPATH = Path("var") / "warehouse" / "warehouse.duckdb"


def open_warehouse(path: str | Path, *, read_only: bool = False,
                   attach: dict[str, str | Path] | None = None):
    """Open the warehouse the way the gateway does (gateway/sql.py Warehouse).

    Returns an in-memory connection with the warehouse attached as `wh` and the
    search path set to `wh.warehouse`, so `warehouse.fct_orders`, `raw.*` and
    bare `fct_orders` all resolve. Extra read-only databases can be attached by
    alias - that is how the grading reference is mounted without ever putting it
    in the agent's catalog.
    """
    con = duckdb.connect(":memory:", config={"threads": 1})
    mode = " (READ_ONLY)" if read_only else ""
    con.execute(f"ATTACH '{Path(path).as_posix()}' AS wh{mode}")
    con.execute("USE wh.warehouse")
    for alias, p in (attach or {}).items():
        con.execute(f"ATTACH '{Path(p).as_posix()}' AS {alias} (READ_ONLY)")
    return con


def create_warehouse(path: str | Path):
    """Same idiom, for building a world from scratch. ATTACH creates the file."""
    p = Path(path)
    for f in (p, Path(str(p) + ".wal")):
        f.unlink(missing_ok=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{p.as_posix()}' AS wh")
    con.execute("USE wh")
    return con


def deployed_copy(src: str | Path, root: str | Path) -> Path:
    """Copy a world to the CONTRACT path under `root` and return it.

    Every test that runs the oracle, the grader or the pipeline must go through
    here. A harness that only ever exercises a differently-named file has not
    tested the world its task ships.
    """
    dst = Path(root) / CONTRACT_RELPATH
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    return dst
