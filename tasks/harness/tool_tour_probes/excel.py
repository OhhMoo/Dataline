"""excel: an `.xlsx` workbook written with openpyxl and read back two ways - with
`pandas.read_excel` in the box's OWN Python and with csvkit's `in2csv` from
/opt/toolbelt - on the analyst's box (da/laptop).

The da/ds images carry openpyxl in the system interpreter (the `ds` extra,
gateway/docker/TOOLBELT.md "Roles"); before that only /opt/toolbelt had it, so
`pandas.read_excel` failed where the analyst's python runs (measured 2026-10-06).
The value written is February 2026 Net Revenue read from the warehouse file; the
answer is what `in2csv` reads back, after the command has checked that pandas read
the same number. The python steps are audited by the PATH shim (`shim.python`); `in2csv`
is not shimmed.

What proves the tool is the workbook on the record: a `shim.python` row that exited ok
and names an `.xlsx` among its targets (the shim's workspace diff, so the workbook is
written under /workspace). Not the argv: the 10-10 live Claude run wrote and read its
workbook from a `python3 -` heredoc, whose argv is `['python3', '-']` and holds no
`read_excel` - a right answer the old `expect_args=("read_excel",)` failed - while its
row's targets named `output/february_2026_close.xlsx`.
"""
from tool_tour import Probe

from tool_tour_probes import DA_LAPTOP_BI, NET_REVENUE_FEB_2026_SQL

PROFILE, PLUGS = DA_LAPTOP_BI
_DIR = "/workspace/tour_excel"

PROBES = [
    Probe(
        id="excel.openpyxl.net_revenue_feb_2026_roundtrip",
        profile=PROFILE,
        plugs=PLUGS,
        command=(
            "cd /workspace\n"
            "set -euo pipefail\n"
            f"d={_DIR}\n"
            "rm -rf \"$d\" && mkdir -p \"$d\"\n"
            "cat > \"$d/write_close.py\" <<'PY'\n"
            "import duckdb, openpyxl\n"
            "con = duckdb.connect('/world/var/warehouse/warehouse.duckdb', read_only=True)\n"
            "value = con.execute(\"" + NET_REVENUE_FEB_2026_SQL + "\").fetchone()[0]\n"
            "con.close()\n"
            "wb = openpyxl.Workbook()\n"
            "ws = wb.active\n"
            "ws.title = 'close'\n"
            "ws.append(['metric', 'value'])\n"
            "ws.append(['net_revenue_feb_2026', float(value)])\n"
            f"wb.save('{_DIR}/close.xlsx')\n"
            "PY\n"
            "python3 \"$d/write_close.py\"\n"
            "from_pandas=$(python3 -c \"import pandas; df = pandas.read_excel("
            f"'{_DIR}/close.xlsx', sheet_name='close', engine='openpyxl'); "
            "print(df.loc[df.metric == 'net_revenue_feb_2026', 'value'].iloc[0])\")\n"
            "from_in2csv=$(in2csv --sheet close \"$d/close.xlsx\" "
            "| awk -F, '$1 == \"net_revenue_feb_2026\" {print $2}')\n"
            "python3 -c \"import sys; a, b = map(float, sys.argv[1:]); "
            "sys.exit(0 if a == b else 'pandas and in2csv disagree')\" "
            "\"$from_pandas\" \"$from_in2csv\"\n"
            "printf '{\"value\": \"%s\"}\\n' \"$from_in2csv\"\n"),
        expect_tools=("shim.python",),
        expect_targets=("*.xlsx",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        instruction=(
            "Finance keeps the February close in a spreadsheet. Put February 2026 Net "
            "Revenue (order_amount over completed orders in fct_orders placed in February "
            "2026, from the team's DuckDB warehouse) into an .xlsx workbook, then open the "
            "workbook again with pandas in this box's Python and report the value it "
            "holds."),
    ),
]
