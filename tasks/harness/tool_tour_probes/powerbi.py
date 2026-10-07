"""powerbi: Power BI REST with an Entra client-credentials token, `executeQueries` with DAX
against the published Commerce semantic model (powerbi.internal:8095)."""
from tool_tour import Probe

from tool_tour_probes import DA_LAPTOP_BI, NET_REVENUE_SQL

PROFILE, PLUGS = DA_LAPTOP_BI

PROBES = [
    Probe(
        id="powerbi.rest.net_revenue",
        profile=PROFILE,
        plugs=PLUGS,
        command="""set -euo pipefail
python3 - <<'PY'
import json, os
import requests

env = os.environ
token = requests.post(
    f"{env['AZURE_AUTHORITY_HOST']}/{env['AZURE_TENANT_ID']}/oauth2/v2.0/token",
    data={"grant_type": "client_credentials", "client_id": env["AZURE_CLIENT_ID"],
          "client_secret": env["AZURE_CLIENT_SECRET"],
          "scope": "https://analysis.windows.net/powerbi/api/.default"}, timeout=60)
token.raise_for_status()
s = requests.Session()
s.headers["Authorization"] = "Bearer " + token.json()["access_token"]
api = env["POWERBI_API_URL"] + "/v1.0/myorg"
for group in s.get(f"{api}/groups", timeout=60).json()["value"]:
    for dataset in s.get(f"{api}/groups/{group['id']}/datasets", timeout=60).json()["value"]:
        if dataset["name"] == "Commerce":
            dax = 'EVALUATE SUMMARIZECOLUMNS("Net Revenue", [Net Revenue])'
            r = s.post(f"{api}/groups/{group['id']}/datasets/{dataset['id']}/executeQueries",
                       json={"queries": [{"query": dax}]}, timeout=120)
            r.raise_for_status()
            row = r.json()["results"][0]["tables"][0]["rows"][0]
            print(json.dumps({"value": row["[Net Revenue]"]}))
PY
""",
        expect_tools=("powerbi.token", "powerbi.execute_queries"),
        truth_sql=NET_REVENUE_SQL,
        instruction=(
            "Ask the published Power BI semantic model (Commerce) for its Net Revenue "
            "measure, through the Power BI REST API with this box's service-principal "
            "credentials."),
    ),
]
