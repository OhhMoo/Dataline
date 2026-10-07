"""duckdb: the `duckdb` CLI on PATH (the audited shim) against the world's warehouse file.

The shim writes one `duckdb.statement` row per `-c` command, with the tables it read
as targets (gateway/src/alkera_gateway/native/shims/duckdb_cli.py). The file's path is
the one the box's own dbt profile names (`~/.dbt/profiles.yml`).
"""
from tool_tour import Probe

from tool_tour_probes import DA_LAPTOP_BI, NET_REVENUE_FEB_2026_SQL

PROFILE, PLUGS = DA_LAPTOP_BI

PROBES = [
    Probe(
        id="duckdb.cli.net_revenue_feb_2026",
        profile=PROFILE,
        plugs=PLUGS,
        command=(
            "set -euo pipefail\n"
            "value=$(duckdb -readonly /world/var/warehouse/warehouse.duckdb -c \""
            + NET_REVENUE_FEB_2026_SQL + "\" | tail -n 1)\n"
            "printf '{\"value\": \"%s\"}\\n' \"$value\"\n"),
        expect_tools=("duckdb.statement",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        instruction=(
            "Finance wants February 2026 Net Revenue straight from the warehouse. Using "
            "the DuckDB command-line client on the warehouse file this box's dbt profile "
            "points at, sum order_amount over completed orders (fct_orders) placed in "
            "February 2026."),
    ),
]
