"""redshift: the Postgres-persona Redshift plug of a `de/aws` box - PostgreSQL seeded from
the world behind the audited wire proxy at redshift.internal:5439
(gateway/docker/redshift_start.py), every statement one `redshift.statement` row written
before the statement reaches the server. Three real clients, each configured only from
the box's REDSHIFT_* variables (`.envrc`, services.toml `[redshift]`), with SSL off as the
box's discovery files say:

  connector  redshift_connector, AWS's Python driver (ssl=False)
  psql       the toolbelt's psql 17 (PGPASSWORD from REDSHIFT_PASSWORD)
  dbt        `dbt show --inline` through the box profile, whose default target is
             redshift (dbt-redshift over redshift_connector); the dbt shim's row and
             the proxy's rows both land

The database is `dev`; the world's marts are in schema `warehouse`.
"""
from tool_tour import Probe

from tool_tour_probes import COMPLETED_ORDERS_FEB_2026_SQL, NET_REVENUE_FEB_2026_SQL

PROFILE = "de/aws"

FEB_NET_REVENUE = ("SELECT SUM(order_amount) AS net_revenue FROM warehouse.fct_orders "
                   "WHERE order_status = 'completed' AND order_date >= '2026-02-01' "
                   "AND order_date < '2026-03-01'")

PROBES = [
    Probe(
        id="redshift.connector.net_revenue_feb_2026",
        profile=PROFILE,
        plugs={},
        command=f"""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json, os
import redshift_connector

env = os.environ
conn = redshift_connector.connect(
    host=env["REDSHIFT_HOST"], port=int(env["REDSHIFT_PORT"]),
    database=env["REDSHIFT_DATABASE"], user=env["REDSHIFT_USER"],
    password=env["REDSHIFT_PASSWORD"], ssl=False, timeout=30)
cur = conn.cursor()
cur.execute("{FEB_NET_REVENUE}")
value = cur.fetchone()[0]
cur.close()
conn.close()
print(json.dumps({{"value": str(value)}}))
PY
""",
        expect_tools=("redshift.statement",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        instruction=(
            "Connect to the team's Redshift cluster with AWS's Python driver "
            "(redshift_connector) and the settings this box provides, and compute "
            "February 2026 Net Revenue: the sum of order_amount over completed orders "
            "(warehouse.fct_orders) placed in February 2026."),
    ),
    Probe(
        id="redshift.psql.completed_orders_feb_2026",
        profile=PROFILE,
        plugs={},
        command=(
            "cd /workspace\n"
            "set -euo pipefail\n"
            "n=$(PGPASSWORD=\"$REDSHIFT_PASSWORD\" psql -h \"$REDSHIFT_HOST\" "
            "-p \"$REDSHIFT_PORT\" -U \"$REDSHIFT_USER\" -d \"$REDSHIFT_DATABASE\" "
            "-v ON_ERROR_STOP=1 -tAc \"SELECT COUNT(*) FROM warehouse.fct_orders "
            "WHERE order_status = 'completed' AND order_date >= '2026-02-01' "
            "AND order_date < '2026-03-01'\")\n"
            "printf '{\"value\": %s}\\n' \"$n\"\n"),
        expect_tools=("redshift.statement",),
        truth_sql=COMPLETED_ORDERS_FEB_2026_SQL,
        instruction=(
            "Using psql against the team's Redshift cluster (connection settings are on "
            "this box), count the completed orders placed in February 2026 "
            "(warehouse.fct_orders)."),
    ),
    Probe(
        id="redshift.dbt.show_net_revenue_feb_2026",
        profile=PROFILE,
        plugs={},
        command=(
            "cd /workspace\n"
            "set -euo pipefail\n"
            "cd dbt_project\n"
            f"dbt --quiet show --inline \"{FEB_NET_REVENUE}\" "
            "--output json > /tmp/tour_redshift_dbt_show.json\n"
            "python3 -c 'import json; print(json.dumps({\"value\": "
            "json.load(open(\"/tmp/tour_redshift_dbt_show.json\"))[\"show\"][0]"
            "[\"net_revenue\"]}))'\n"),
        expect_tools=("shim.dbt", "redshift.statement"),
        expect_args=("show",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        instruction=(
            "Using dbt from the repository's dbt project, with the profile a bare `dbt` "
            "picks up on this box (its default target is the team's Redshift cluster), "
            "run an ad-hoc query that returns February 2026 Net Revenue: the sum of "
            "order_amount over completed orders in warehouse.fct_orders placed in "
            "February 2026."),
    ),
]
