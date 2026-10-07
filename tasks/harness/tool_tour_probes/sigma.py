"""sigma: Sigma REST API v2 (client-credentials token, workbook contents, element export
and download) at sigma.internal:8097."""
from tool_tour import Probe
from tool_tour_probes import DA_LAPTOP_BI, NET_REVENUE_FEB_2026_SQL

PROFILE, PLUGS = DA_LAPTOP_BI

PROBES = [
    Probe(
        id="sigma.rest.net_revenue_feb_2026",
        profile=PROFILE,
        plugs=PLUGS,
        command="""set -euo pipefail
python3 - <<'PY'
import csv, io, json, os
import requests

env = os.environ
base = env["SIGMA_BASE_URL"].rstrip("/") + "/v2"
token = requests.post(f"{base}/auth/token", data={"grant_type": "client_credentials"},
                      auth=(env["SIGMA_CLIENT_ID"], env["SIGMA_CLIENT_SECRET"]), timeout=60)
token.raise_for_status()
s = requests.Session()
s.headers["Authorization"] = "Bearer " + token.json()["access_token"]
workbook = next(w for w in s.get(f"{base}/workbooks", timeout=60).json()["entries"]
                if w["name"] == "Executive Overview")
doc = s.get(f"{base}/workbooks/{workbook['workbookId']}", params={"includeContents": "true"},
            timeout=60).json()
element = next(e for e in doc["contents"]["elements"] if e["name"] == "Revenue by Month")
job = s.post(f"{base}/workbooks/{workbook['workbookId']}/export",
             json={"elementId": element["id"], "format": {"type": "csv"}}, timeout=120)
job.raise_for_status()
got = s.get(f"{base}/query/{job.json()['queryId']}/download", timeout=60)
got.raise_for_status()
feb = next(r for r in csv.DictReader(io.StringIO(got.text))
           if r["year"] == "2026" and r["month"] == "2")
print(json.dumps({"value": feb["Net Revenue"]}))
PY
""",
        expect_tools=("sigma.auth_token", "sigma.workbook_export", "sigma.query_download"),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        instruction=(
            "Through the Sigma REST API, export the Revenue by Month element of the "
            "Executive Overview workbook and report February 2026 Net Revenue from it."),
    ),
]
