"""airflow: Airflow 3's REST API v2 over the world ledger (airflow.internal:8100,
gateway native/services/airflow.py), reached with `curl` + `jq` and with airflowctl,
Airflow's REST-only CLI. A box that plugs it has no `./ops/sched` and no `airflow`
CLI (no metadata database), so this runs on de/laptop with the orchestrator slot
plugged to airflow.

Read-only: a trigger or a clear would QUEUE a run in the world's ledger (nothing
runs until the grader's advance, and nothing moves the clock), and a tour probe
must leave the world as it found it.
"""
from tool_tour import Probe

PROFILE = "de/laptop"
PLUGS = {"orchestrator": ("airflow",)}

PROBES = [
    Probe(
        id="airflow.rest.dbt_build_successes",
        profile=PROFILE,
        plugs=PLUGS,
        command="""cd /workspace
set -euo pipefail
token=$(curl -sf -X POST "${AIRFLOW_API_URL%/api/v2}/auth/token" \\
  -H 'Content-Type: application/json' \\
  -d "{\\"username\\": \\"$AIRFLOW_USER\\", \\"password\\": \\"$AIRFLOW_PASSWORD\\"}" \\
  | jq -r .access_token)
tis="$AIRFLOW_API_URL/dags/commerce_nightly/dagRuns/~/taskInstances"
n=$(curl -sf -H "Authorization: Bearer $token" \\
  "$tis?task_id=dbt_build&state=success&limit=1" | jq .total_entries)
printf '{"value": %s}\\n' "$n"
""",
        expect_tools=("airflow.auth_token", "airflow.task_list"),
        truth_sql=("SELECT count(*) FROM (SELECT unnest(runs) AS r FROM "
                   "read_json('var/sched/state.json')) "
                   "WHERE r.job = 'dbt_build' AND r.status = 'success'"),
        instruction=(
            "How many dbt_build task instances of the commerce_nightly DAG have succeeded? "
            "Ask the team's Airflow through its REST API, with the credentials this box "
            "provides."),
    ),
    Probe(
        id="airflow.airflowctl.commerce_nightly_schedule",
        profile=PROFILE,
        plugs=PLUGS,
        command="""cd /workspace
set -euo pipefail
export AIRFLOW_HOME=/tmp/airflowctl
base="${AIRFLOW_API_URL%/api/v2}"
token=$(airflowctl auth token --api-url "$base" --username "$AIRFLOW_USER" \\
  --password "$AIRFLOW_PASSWORD" | tail -n 1)
airflowctl auth login --api-url "$base" --api-token "$token" --skip-keyring >/dev/null
schedule=$(AIRFLOW_CLI_TOKEN="$token" airflowctl dags get commerce_nightly -o json \\
  | jq -r 'if type == "array" then .[0] else . end | .timetable_summary')
printf '{"value": "%s"}\\n' "$schedule"
""",
        expect_tools=("airflow.auth_token", "airflow.dag_get"),
        truth_sql=("SELECT regexp_extract(content, 'schedule=\"([^\"]+)\"', 1) "
                   "FROM read_text('workspace/dags/commerce_nightly.py')"),
        instruction=(
            "What schedule does Airflow report for the commerce_nightly DAG? Use "
            "airflowctl, Airflow's command-line client, with this box's credentials."),
    ),
]
