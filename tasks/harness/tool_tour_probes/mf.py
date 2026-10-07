"""mf: MetricFlow's CLI from the toolbelt (dbt-metricflow), audited by the `mf` PATH shim.

`mf` reads `target/semantic_manifest.json`, which a freshly provisioned repo does not
have, so the probe runs `dbt parse` first, exactly as bi_e2e_check's M0 does. Every
`mf` call also spawns `git --help` through dbt's dependency check, so a `shim.git` row
precedes the `shim.mf` one.
"""
from tool_tour import Probe
from tool_tour_probes import DA_LAPTOP_BI, NET_REVENUE_FEB_2026_SQL

PROFILE, PLUGS = DA_LAPTOP_BI

PROBES = [
    Probe(
        id="mf.query.net_revenue_feb_2026",
        profile=PROFILE,
        plugs=PLUGS,
        command=(
            "set -euo pipefail\n"
            "cd dbt_project\n"
            "dbt --quiet parse\n"
            "mf query --metrics net_revenue --group-by metric_time__month "
            "--start-time 2026-02-01 --end-time 2026-02-28 --csv /tmp/tour_mf.csv --quiet\n"
            "python3 -c 'import csv, json; rows = [r for r in csv.DictReader(open("
            "\"/tmp/tour_mf.csv\")) if r[\"metric_time__month\"].startswith(\"2026-02\")]; "
            "print(json.dumps({\"value\": rows[0][\"net_revenue\"]}))'\n"),
        expect_tools=("shim.mf",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        instruction=(
            "Use the semantic layer's own CLI (MetricFlow) on the repository's dbt project "
            "to get the net_revenue metric for February 2026, by month of metric time."),
    ),
]
