"""bigquery: the BigQuery plug (goccy's emulator behind the audited proxy at
bigquery.internal:9050, seeded from the world's DuckDB warehouse as project
`alkera-local`, one dataset per schema), configured from the box's own variables
(BIGQUERY_API_URL, BIGQUERY_PROJECT):

  client  google-cloud-bigquery, AnonymousCredentials + client_options api_endpoint:
          a query, and a catalog read (tables.list);
  rest    curl against the REST API v2 (`jobs.query`).

Every query is one `bigquery.statement` row and every catalog read one
`bigquery.<op>` row, written by the proxy before it answers. No dbt probe: dbt-bigquery
is not in the image (gateway/docker/bigquery-clients.in).
"""
from tool_tour import Probe

from tool_tour_probes import COMPLETED_ORDERS_FEB_2026_SQL, NET_REVENUE_FEB_2026_SQL

PROFILE = "de/gcp"
PLUGS: dict = {}

PROBES = [
    Probe(
        id="bigquery.client.net_revenue_feb_2026",
        profile=PROFILE,
        plugs=PLUGS,
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json, os
from google.api_core.client_options import ClientOptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

project = os.environ["BIGQUERY_PROJECT"]
client = bigquery.Client(project=project, credentials=AnonymousCredentials(),
                         client_options=ClientOptions(api_endpoint=os.environ["BIGQUERY_API_URL"]))
rows = list(client.query(
    f"SELECT SUM(order_amount) AS v FROM `{project}.warehouse.fct_orders` "
    "WHERE order_status = 'completed' AND order_date >= '2026-02-01' "
    "AND order_date < '2026-03-01'").result())
client.close()
print(json.dumps({"value": str(rows[0]["v"])}))
PY
""",
        expect_tools=("bigquery.statement",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        instruction=(
            "Query the team's BigQuery warehouse from this box with the Python client and "
            "the settings the box provides, and compute February 2026 Net Revenue: the sum "
            "of order_amount over completed orders (fct_orders) placed in February 2026."),
    ),
    Probe(
        id="bigquery.rest.completed_orders_feb_2026",
        profile=PROFILE,
        plugs=PLUGS,
        command="""cd /workspace
set -euo pipefail
python3 -c 'import json, os; print(json.dumps({"useLegacySql": False, "query": "SELECT COUNT(*) FROM `" + os.environ["BIGQUERY_PROJECT"] + ".warehouse.fct_orders` WHERE order_status = \\"completed\\" AND order_date >= \\"2026-02-01\\" AND order_date < \\"2026-03-01\\""}))' > /tmp/tour_bq_query.json
curl -sS --fail-with-body -X POST -H 'Content-Type: application/json' \\
  --data @/tmp/tour_bq_query.json \\
  "$BIGQUERY_API_URL/bigquery/v2/projects/$BIGQUERY_PROJECT/queries" > /tmp/tour_bq_rest.json
python3 -c 'import json; print(json.dumps({"value": json.load(open("/tmp/tour_bq_rest.json"))["rows"][0]["f"][0]["v"]}))'
""",
        expect_tools=("bigquery.statement",),
        truth_sql=COMPLETED_ORDERS_FEB_2026_SQL,
        instruction=(
            "Using the BigQuery REST API directly (no client library), count the completed "
            "orders placed in February 2026 in the warehouse's fct_orders table."),
    ),
    Probe(
        id="bigquery.client.warehouse_tables",
        profile=PROFILE,
        plugs=PLUGS,
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json, os
from google.api_core.client_options import ClientOptions
from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery

client = bigquery.Client(project=os.environ["BIGQUERY_PROJECT"],
                         credentials=AnonymousCredentials(),
                         client_options=ClientOptions(api_endpoint=os.environ["BIGQUERY_API_URL"]))
tables = [t.table_id for t in client.list_tables("warehouse")]
client.close()
print(json.dumps({"value": len(tables)}))
PY
""",
        expect_tools=("bigquery.table_list",),
        truth_sql=("SELECT COUNT(*) FROM information_schema.tables "
                   "WHERE table_catalog = 'warehouse' AND table_schema = 'warehouse'"),
        instruction=(
            "Using the team's BigQuery warehouse from this box, find out how many tables "
            "the `warehouse` dataset holds."),
    ),
]
