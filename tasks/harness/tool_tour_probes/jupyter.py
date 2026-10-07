"""jupyter: a notebook executed on the box's audited `alkera-python` kernel (nbclient,
`jupyter execute`), one `jupyter.cell` row per cell.

The notebook is written by hand as nbformat 4.4 JSON (no cell ids to invent), and the
cell leaves its answer in a file because `jupyter execute` does not print outputs.
"""
from tool_tour import Probe
from tool_tour_probes import COMPLETED_ORDERS_FEB_2026_SQL, DA_LAPTOP_BI

PROFILE, PLUGS = DA_LAPTOP_BI

_CELL = (
    "import duckdb, json\\n"
    "con = duckdb.connect('/world/var/warehouse/warehouse.duckdb', read_only=True)\\n"
    "n = con.execute(\\\"" + COMPLETED_ORDERS_FEB_2026_SQL + "\\\").fetchone()[0]\\n"
    "con.close()\\n"
    "json.dump({'value': n}, open('/tmp/tour_jupyter/answer.json', 'w'))\\n")

PROBES = [
    Probe(
        id="jupyter.kernel.completed_orders_feb_2026",
        profile=PROFILE,
        plugs=PLUGS,
        command=(
            "set -euo pipefail\n"
            "mkdir -p /tmp/tour_jupyter\n"
            "cat > /tmp/tour_jupyter/feb_orders.ipynb <<'NB'\n"
            '{"nbformat": 4, "nbformat_minor": 4,\n'
            ' "metadata": {"kernelspec": {"name": "alkera-python", '
            '"display_name": "Python 3 (alkera)", "language": "python"}},\n'
            ' "cells": [{"cell_type": "code", "execution_count": null, "metadata": {}, '
            '"outputs": [],\n'
            '   "source": "' + _CELL + '"}]}\n'
            "NB\n"
            "jupyter execute --kernel_name=alkera-python /tmp/tour_jupyter/feb_orders.ipynb\n"
            "cat /tmp/tour_jupyter/answer.json\n"
            "echo\n"),
        expect_tools=("jupyter.cell",),
        truth_sql=COMPLETED_ORDERS_FEB_2026_SQL,
        instruction=(
            "In a notebook on this box's Python kernel, count the completed orders placed in "
            "February 2026 (fct_orders in the team's DuckDB warehouse)."),
    ),
]
