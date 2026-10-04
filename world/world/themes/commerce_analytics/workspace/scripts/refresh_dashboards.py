"""The 06:00 dashboard refresh: recompute every dashboard's tiles from the marts.

Each deployed dashboard is a YAML file of named SQL panels (`bi_get` shows
one). A refresh runs every panel against the warehouse as it is at that moment,
stores the values as the dashboard's cache, and logs the refresh - with the
newest business date the tiles could see - to raw.bi_refresh_history.

What a refresh does NOT do is check that the data is new. It runs on a clock
(dags/bi_refresh.py, 06:00 UTC) whether or not the nightly load and dbt build
have finished, and a refresh of yesterday's data is a successful refresh. The
log is how you find out afterwards which business date people were looking
at: `select * from raw.bi_refresh_history order by refresh_id desc`.

A panel that no longer binds (a renamed column, a dropped mart) fails the
whole refresh with the panel's name and DuckDB's error, and nothing is logged.

Usage:
    refresh_dashboards.py --dashboards <dir of *.yml> --db <warehouse.duckdb>
                          --out <cache dir>
Prints one JSON summary line on success.
"""
import argparse
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import yaml

LOG = "raw.bi_refresh_history"


def canon(value):
    """A cell as it is stored in the cache: exact, and the same on every host."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def attach(db: Path, read_only: bool):
    """ATTACH under an alias - never open warehouse.duckdb directly: the file
    stem is the same as the `warehouse` schema and two-part names go
    ambiguous."""
    con = duckdb.connect(":memory:", config={"threads": 1})
    mode = " (READ_ONLY)" if read_only else ""
    con.execute("ATTACH '" + str(db).replace("'", "''") + f"' AS wh{mode}")
    return con


def dump(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii") + b"\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="refresh_dashboards")
    p.add_argument("--dashboards", type=Path, required=True)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args(argv)

    specs = [yaml.safe_load(f.read_text(encoding="utf-8"))
             for f in sorted(a.dashboards.glob("*.yml"))]
    if not specs:
        print(f"no dashboards deployed under {a.dashboards}; nothing to refresh",
              file=sys.stderr)
        return 1

    # 1. Read: every panel, read-only, in the panels' own two-part dialect.
    con = attach(a.db, read_only=True)
    try:
        con.execute("USE wh.warehouse")
        rendered = {}
        for spec in specs:
            panels = {}
            for panel in spec["panels"]:
                try:
                    rows = con.execute(panel["sql"]).fetchall()
                except duckdb.Error as exc:
                    print(f"panel {spec['dashboard']}.{panel['name']} failed: "
                          f"{str(exc).splitlines()[0]}", file=sys.stderr)
                    return 1
                panels[panel["name"]] = [[canon(c) for c in row] for row in rows]
            rendered[spec["dashboard"]] = panels
        watermark = con.execute(
            "select max(order_date) from wh.warehouse.fct_orders").fetchone()[0]
    finally:
        con.close()

    # 2. Log: one row per dashboard, one refresh_id per run.
    con = attach(a.db, read_only=False)
    try:
        con.execute(f"""
            create table if not exists wh.{LOG} (
                refresh_id integer, dashboard varchar, data_through date,
                panels integer, panel_rows integer)""")
        refresh_id = con.execute(
            f"select coalesce(max(refresh_id), 0) + 1 from wh.{LOG}").fetchone()[0]
        for name in sorted(rendered):
            panels = rendered[name]
            con.execute(f"insert into wh.{LOG} values (?, ?, ?, ?, ?)",
                        [refresh_id, name, watermark, len(panels),
                         sum(len(rows) for rows in panels.values())])
    finally:
        con.close()

    # 3. Cache: what the dashboards now show.
    a.out.mkdir(parents=True, exist_ok=True)
    through = watermark.isoformat() if watermark else None
    for name in sorted(rendered):
        (a.out / f"{name}.json").write_bytes(dump({
            "dashboard": name, "refresh_id": refresh_id, "data_through": through,
            "panels": rendered[name]}))
    sys.stdout.write(dump({"dashboards": len(rendered),
                           "panels": sum(len(p) for p in rendered.values()),
                           "refresh_id": refresh_id, "data_through": through}).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
