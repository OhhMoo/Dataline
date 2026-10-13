"""databricks: the tier-1 workspace at https://databricks.internal (native-services, port
443, seeded from the world's DuckDB warehouse into the service's own state), through each
client the box ships: databricks-sdk, databricks-sql-connector over the Statement
Execution API (`use_sea=True`: the workspace serves no Thrift), REST with curl, Unity
Catalog, dbt-databricks through the box profile, and Jobs run-now over the world's
scheduler. One `databricks.statement` row per statement, `databricks.<op>` for the rest.

Everything is configured from the box alone: DATABRICKS_HOST / _SERVER_HOSTNAME /
_HTTP_PATH / _TOKEN, and the certificate trust REQUESTS_CA_BUNDLE (the SDK's) and
SSL_CERT_FILE (the connector's and curl's) name. The run-now probe is last: it is the one
that changes the world - it QUEUES a run in the ledger (nothing runs, and the clock does not
move, until the grader's advance) - and its truth is the ledger before it.
"""
from tool_tour import Probe

from tool_tour_probes import (
    COMPLETED_ORDERS_FEB_2026_SQL,
    NET_REVENUE_FEB_2026_SQL,
    NET_REVENUE_SQL,
)

PROFILE = "de/databricks"

PROBES = [
    Probe(
        id="databricks.sdk.net_revenue_feb_2026",
        profile=PROFILE,
        plugs={},
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
r = w.statement_execution.execute_statement(
    warehouse_id="alkera",
    statement="SELECT SUM(order_amount) FROM main.warehouse.fct_orders "
              "WHERE order_status = 'completed' AND order_date >= DATE'2026-02-01' "
              "AND order_date < DATE'2026-03-01'")
assert r.status.state.value == "SUCCEEDED", r.status
print(json.dumps({"value": r.result.data_array[0][0]}))
PY
""",
        expect_tools=("databricks.statement",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        instruction=(
            "Using the team's Databricks workspace and the Databricks SDK for Python, compute "
            "February 2026 Net Revenue: the sum of order_amount over completed orders in "
            "fct_orders placed in February 2026."),
    ),
    Probe(
        id="databricks.connector.completed_orders_feb_2026",
        profile=PROFILE,
        plugs={},
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json, os
from databricks import sql

env = os.environ
with sql.connect(server_hostname=env["DATABRICKS_SERVER_HOSTNAME"],
                 http_path=env["DATABRICKS_HTTP_PATH"],
                 access_token=env["DATABRICKS_TOKEN"], use_sea=True) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM `main`.`warehouse`.`fct_orders` "
                    "WHERE order_status = 'completed' AND order_date >= '2026-02-01' "
                    "AND order_date < '2026-03-01'")
        print(json.dumps({"value": cur.fetchone()[0]}))
PY
""",
        expect_tools=("databricks.statement",),
        truth_sql=COMPLETED_ORDERS_FEB_2026_SQL,
        instruction=(
            "Connect to the team's Databricks SQL warehouse with the Databricks SQL "
            "Connector for Python and count the completed orders placed in February 2026 "
            "(fct_orders)."),
    ),
    Probe(
        id="databricks.rest.net_revenue",
        profile=PROFILE,
        plugs={},
        command="""cd /workspace
set -euo pipefail
curl -sS --fail-with-body "$DATABRICKS_HOST/api/2.0/sql/statements" \\
  -H "Authorization: Bearer $DATABRICKS_TOKEN" -H "Content-Type: application/json" \\
  -d '{"warehouse_id": "alkera", "statement": "SELECT SUM(order_amount) FROM main.warehouse.fct_orders WHERE order_status = '"'"'completed'"'"'"}' \\
  > /tmp/dbx_rest.json
python3 -c 'import json; d = json.load(open("/tmp/dbx_rest.json")); print(json.dumps({"value": d["result"]["data_array"][0][0]}))'
""",
        expect_tools=("databricks.statement",),
        truth_sql=NET_REVENUE_SQL,
        instruction=(
            "Through the Databricks SQL Statement Execution REST API (no SDK), ask the "
            "team's SQL warehouse for total Net Revenue: the sum of order_amount over "
            "completed orders in fct_orders."),
    ),
    Probe(
        id="databricks.uc.fct_orders_columns",
        profile=PROFILE,
        plugs={},
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
table = w.tables.get("main.warehouse.fct_orders")
print(json.dumps({"value": len(table.columns)}))
PY
""",
        expect_tools=("databricks.table_get",),
        truth_sql=("SELECT count(*) FROM information_schema.columns WHERE table_catalog = "
                   "'warehouse' AND table_schema = 'warehouse' AND table_name = 'fct_orders'"),
        instruction=(
            "In the team's Databricks workspace, look up the fct_orders table in Unity "
            "Catalog and report how many columns it has."),
    ),
    Probe(
        id="databricks.dbt.net_revenue_feb_2026",
        profile=PROFILE,
        plugs={},
        command="""cd /workspace
set -euo pipefail
cd dbt_project
dbt --quiet show --inline "select sum(order_amount) as net_revenue from main.warehouse.fct_orders where order_status = 'completed' and order_date >= date'2026-02-01' and order_date < date'2026-03-01'" --output json > /tmp/dbx_dbt_show.json
python3 -c 'import json; print(json.dumps({"value": json.load(open("/tmp/dbx_dbt_show.json"))["show"][0]["net_revenue"]}))'
""",
        expect_tools=("shim.dbt", "databricks.statement"),
        expect_args=("show",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        instruction=(
            "With dbt in the team's dbt project, using the dbt profile this box provides "
            "(its default target is the Databricks warehouse), compute February 2026 Net "
            "Revenue: completed orders' order_amount summed over February 2026."),
    ),
    Probe(
        id="databricks.jobs.run_now_bi_refresh",
        profile=PROFILE,
        plugs={},
        command="""cd /workspace
set -euo pipefail
python3 - <<'PY'
import json
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
job = next(j for j in w.jobs.list() if j.settings.name == "bi_refresh")
run = w.jobs.run_now(job_id=job.job_id)
got = w.jobs.get_run(run_id=run.run_id)
# queued, not run: the world's scheduler runs it when the world's clock next moves
assert got.state.life_cycle_state.value == "QUEUED", got.state
assert got.state.result_state is None and got.end_time == 0, got
print(json.dumps({"value": got.start_time}))
PY
""",
        expect_tools=("databricks.sched_list", "databricks.sched_trigger",
                      "databricks.sched_state"),
        # A run-now is queued at the world's clock and moves nothing, so the run's
        # start_time is the ledger's vnow before the probe, in epoch milliseconds.
        truth_sql=("SELECT epoch_ms(CAST(json(content)->>'vnow' AS TIMESTAMPTZ)) "
                   "FROM read_text('var/sched/state.json')"),
        instruction=(
            "Through the team's Databricks Jobs, start the bi_refresh job now and report "
            "the run's start time as the Jobs API reports it, in epoch milliseconds."),
    ),
]
