"""postgres: `psql` with the box's PG* variables, which only a profile that plugs the
postgres warehouse exports (de/postgres).

A profile that plugs postgres gets the world's `warehouse.*` copied into the stack's
server when the box is prepared (`postgres_live.seed`), so a psql read answers the
world's facts: here February 2026 Net Revenue, in Postgres' own spelling (database
`alkera`, schema `warehouse`). Its statements are audited by the TAILER that folds the
server's role-scoped log into the action log in the stack's `down()`
(`postgres_live.fold_statement_log`), so the probe is `rows_at="teardown"`: judged
after teardown, on a stack of its own.
"""
from tool_tour import Probe

from tool_tour_probes import NET_REVENUE_FEB_2026_SQL

#: The same fact over the seeded copy: two-part names, the server's own database.
PG_NET_REVENUE_FEB_2026_SQL = NET_REVENUE_FEB_2026_SQL.replace(
    "warehouse.warehouse.fct_orders", "warehouse.fct_orders")

PROBES = [
    Probe(
        id="postgres.psql.net_revenue_feb_2026",
        profile="de/postgres",
        plugs={},
        command=(
            "cd /workspace\n"
            "set -euo pipefail\n"
            "value=$(psql -X -tA -v ON_ERROR_STOP=1 -c \""
            + PG_NET_REVENUE_FEB_2026_SQL + "\")\n"
            "printf '{\"value\": \"%s\"}\\n' \"$value\"\n"),
        expect_tools=("psql.statement",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        rows_at="teardown",
        instruction=(
            "Finance wants February 2026 Net Revenue from the team's Postgres warehouse. "
            "Using psql with the connection settings this box provides, sum order_amount "
            "over completed orders (warehouse.fct_orders) placed in February 2026."),
    ),
]
