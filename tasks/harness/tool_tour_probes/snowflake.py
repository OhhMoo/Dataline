"""snowflake: snowflake-connector-python against the Snowflake plug (fakesnow behind the
audited TLS proxy at snowflake.internal:8443, seeded from the world's DuckDB warehouse),
trusted through `REQUESTS_CA_BUNDLE`. One `snowflake.statement` row per statement.

The seeded database is the warehouse file's stem upper-cased (`WAREHOUSE`); once the
discovery registry exports `SNOWFLAKE_DATABASE` (Task 2) the probe reads it from there.
"""
from tool_tour import Probe

from tool_tour_probes import NET_REVENUE_FEB_2026_SQL

PROBES = [
    Probe(
        id="snowflake.connector.net_revenue_feb_2026",
        profile="da/snowflake",
        plugs={},
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json, os
import snowflake.connector

env = os.environ
database = env.get("SNOWFLAKE_DATABASE", "WAREHOUSE")
conn = snowflake.connector.connect(
    account=env["SNOWFLAKE_ACCOUNT"], user=env["SNOWFLAKE_USER"],
    password=env["SNOWFLAKE_PASSWORD"], host=env["SNOWFLAKE_HOST"], port=8443,
    protocol="https", database=database, login_timeout=20, network_timeout=30)
cur = conn.cursor()
cur.execute(f"SELECT SUM(order_amount) FROM {database}.WAREHOUSE.FCT_ORDERS "
            "WHERE order_status = 'completed' AND order_date >= '2026-02-01' "
            "AND order_date < '2026-03-01'")
value = cur.fetchone()[0]
cur.close()
conn.close()
print(json.dumps({"value": str(value)}))
PY
""",
        expect_tools=("snowflake.statement",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        instruction=(
            "Connect to the team's Snowflake warehouse with the Python connector and the "
            "settings this box provides, and compute February 2026 Net Revenue: the sum of "
            "order_amount over completed orders (FCT_ORDERS) placed in February 2026."),
    ),
]
