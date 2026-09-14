"""Import the monthly close workbooks into the warehouse raw layer.

finance/workbooks/CLOSE_YYYYMM.xlsx is the source of record for the close;
this script is the only path from the workbooks to raw.gl_lines and
raw.manual_adj. Parses the xlsx with stdlib zipfile + ElementTree (no Excel,
no openpyxl) so the close laptop needs nothing but python and duckdb.

Usage: import_close.py --workbooks finance/workbooks --db warehouse.duckdb
"""
import argparse
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import duckdb

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def sheet_parts(z: zipfile.ZipFile) -> dict:
    """Map sheet name -> part path via workbook.xml + its rels."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target")
               for r in rels.findall(f"{PKG}Relationship")}
    return {s.get("name"): "xl/" + targets[s.get(RID)]
            for s in wb.find(f"{NS}sheets").findall(f"{NS}sheet")}


def sheet_rows(z: zipfile.ZipFile, part: str) -> list:
    root = ET.fromstring(z.read(part))
    rows = []
    for row in root.find(f"{NS}sheetData").findall(f"{NS}row"):
        vals = []
        for c in row.findall(f"{NS}c"):
            t = c.find(f"{NS}is/{NS}t")
            vals.append(t.text if t is not None and t.text else "")
        rows.append(vals)
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(prog="import_close")
    p.add_argument("--workbooks", type=Path, required=True,
                   help="directory of CLOSE_YYYYMM.xlsx files")
    p.add_argument("--db", type=Path, required=True,
                   help="warehouse duckdb path")
    a = p.parse_args(argv)
    gl, adj = [], []
    for f in sorted(a.workbooks.glob("CLOSE_*.xlsx")):
        with zipfile.ZipFile(f) as z:
            parts = sheet_parts(z)
            gl += sheet_rows(z, parts["GL_LINES"])[1:]
            adj += sheet_rows(z, parts["MANUAL_ADJ"])[1:]
    con = duckdb.connect(str(a.db))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute("CREATE TEMP TABLE _gl (line_id VARCHAR, account VARCHAR,"
                    " post_date VARCHAR, period VARCHAR, amount VARCHAR)")
        con.execute("CREATE TEMP TABLE _adj (adj_id VARCHAR, account VARCHAR,"
                    " period VARCHAR, amount VARCHAR, owner VARCHAR,"
                    " note VARCHAR)")
        if gl:
            con.executemany("INSERT INTO _gl VALUES (?, ?, ?, ?, ?)", gl)
        if adj:
            con.executemany("INSERT INTO _adj VALUES (?, ?, ?, ?, ?, ?)", adj)
        con.execute("CREATE OR REPLACE TABLE raw.gl_lines AS"
                    " SELECT * FROM _gl ORDER BY period, account, line_id")
        con.execute("CREATE OR REPLACE TABLE raw.manual_adj AS"
                    " SELECT * FROM _adj ORDER BY period, adj_id")
    finally:
        con.close()
    print(f"imported {len(gl)} gl lines, {len(adj)} manual adjustments")


if __name__ == "__main__":
    sys.exit(main())
