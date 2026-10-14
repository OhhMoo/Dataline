"""jupyter: a notebook executed with nbclient (`jupyter execute`), one `jupyter.cell` row
per cell, on two kernels:

  jupyter.kernel...           the box's audited `alkera-python` kernel, named outright;
  jupyter.default_kernel...   what an agent does unprompted (the 10-10 live Claude run:
                              `NotebookClient(kernel_name="python3")`, and no
                              `jupyter.cell` row): a notebook whose metadata names
                              `python3`, run with no --kernel_name. It passes only where the
                              default `python3` kernelspec is the audited one (plan Task 1).

The notebook is written by hand as nbformat 4.4 JSON (no cell ids to invent), and the
cell leaves its answer in a file because `jupyter execute` does not print outputs.
"""
from tool_tour import Probe

from tool_tour_probes import COMPLETED_ORDERS_FEB_2026_SQL, DA_LAPTOP_BI

PROFILE, PLUGS = DA_LAPTOP_BI


def _command(work: str, kernel: str, display_name: str, execute: str) -> str:
    """Write the one-cell notebook into `work`, run `execute` on it, print its answer."""
    cell = (
        "import duckdb, json\\n"
        "con = duckdb.connect('/world/var/warehouse/warehouse.duckdb', read_only=True)\\n"
        "n = con.execute(\\\"" + COMPLETED_ORDERS_FEB_2026_SQL + "\\\").fetchone()[0]\\n"
        "con.close()\\n"
        f"json.dump({{'value': n}}, open('{work}/answer.json', 'w'))\\n")
    return (
        "cd /workspace\n"
        "set -euo pipefail\n"
        f"mkdir -p {work}\n"
        f"cat > {work}/feb_orders.ipynb <<'NB'\n"
        '{"nbformat": 4, "nbformat_minor": 4,\n'
        f' "metadata": {{"kernelspec": {{"name": "{kernel}", '
        f'"display_name": "{display_name}", "language": "python"}}}},\n'
        ' "cells": [{"cell_type": "code", "execution_count": null, "metadata": {}, '
        '"outputs": [],\n'
        '   "source": "' + cell + '"}]}\n'
        "NB\n"
        f"{execute} {work}/feb_orders.ipynb\n"
        f"cat {work}/answer.json\n"
        "echo\n")


PROBES = [
    Probe(
        id="jupyter.kernel.completed_orders_feb_2026",
        profile=PROFILE,
        plugs=PLUGS,
        command=_command("/tmp/tour_jupyter", "alkera-python", "Python 3 (alkera)",
                         "jupyter execute --kernel_name=alkera-python"),
        expect_tools=("jupyter.cell",),
        truth_sql=COMPLETED_ORDERS_FEB_2026_SQL,
        instruction=(
            "In a notebook on this box's Python kernel, count the completed orders placed in "
            "February 2026 (fct_orders in the team's DuckDB warehouse)."),
    ),
    Probe(
        id="jupyter.default_kernel.completed_orders_feb_2026",
        profile=PROFILE,
        plugs=PLUGS,
        command=_command("/tmp/tour_jupyter_default", "python3", "Python 3 (ipykernel)",
                         "jupyter execute"),
        expect_tools=("jupyter.cell",),
        truth_sql=COMPLETED_ORDERS_FEB_2026_SQL,
        instruction=(
            "Execute a Jupyter notebook on this box's default Python 3 kernel that counts "
            "the completed orders placed in February 2026 (fct_orders in the team's DuckDB "
            "warehouse), and report the count."),
    ),
]
