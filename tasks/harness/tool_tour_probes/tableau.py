"""tableau: tabcmd 2 (PAT sign-in, `export --csv`) against Tableau Server at
tableau.internal:8096, audited by the server and by the `tabcmd` PATH shim.

The export is the KPIs sheet of Executive Overview, formatted the way Tableau formats
it (`$#,0.00`); the probe strips the currency formatting, nothing else. tabcmd writes
`tabcmd.log` in the current directory, which the shim's row names.
"""
from tool_tour import Probe

from tool_tour_probes import DA_LAPTOP_BI, NET_REVENUE_SQL

PROFILE, PLUGS = DA_LAPTOP_BI

PROBES = [
    Probe(
        id="tableau.tabcmd.net_revenue",
        profile=PROFILE,
        plugs=PLUGS,
        command=(
            "set -euo pipefail\n"
            "tabcmd login -s \"$TABLEAU_SERVER_URL\" --token-name \"$TABLEAU_PAT_NAME\" "
            "--token-value \"$TABLEAU_PAT_SECRET\"\n"
            "tabcmd export /ExecutiveOverview/KPIs --csv -f /tmp/tour_kpis.csv\n"
            "python3 -c 'import csv, json; "
            "row = next(csv.DictReader(open(\"/tmp/tour_kpis.csv\"))); "
            "print(json.dumps({\"value\": row[\"Net Revenue\"].replace(\"$\", \"\")"
            ".replace(\",\", \"\")}))'\n"),
        expect_tools=("tableau.auth_signin", "tableau.views_data", "shim.tabcmd"),
        truth_sql=NET_REVENUE_SQL,
        instruction=(
            "With the Tableau command-line client, sign in to the team's Tableau Server "
            "with this box's personal access token and export the KPIs sheet of the "
            "Executive Overview workbook; report the Net Revenue it shows, as a number."),
    ),
]
