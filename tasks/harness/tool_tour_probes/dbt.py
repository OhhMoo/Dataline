"""dbt: `dbt show --inline` through the box's own profile (`~/.dbt/profiles.yml`), audited
by the `dbt` PATH shim (`shim.dbt`).

The query names the mart three-part (`warehouse.warehouse.fct_orders`). `{{ ref('fct_orders') }}`
does NOT work in an inline query on this world, measured 2026-10-07: the project's
`macros/relations.sql` renders every ref() without its database except inside a test,
an inline query is a `sql_operation` node, and a two-part `warehouse.fct_orders` against
a DuckDB file whose catalog is also `warehouse` is a Binder Error ("Ambiguous reference
to catalog or schema"). That is the world theme's dbt project, not the tool; it is
reported as a finding of the tour, not worked around in the theme.

`dbt.show.ref_inline` is the same question asked the way an engineer would ask it,
through `{{ ref('fct_orders') }}`. It FAILs until the theme's macro also restores the
database for `sql_operation` nodes, a fix that moves the BI001/BI002 world bytes and
so waits on the user (KNOWN).
"""
from tool_tour import Probe

from tool_tour_probes import DA_LAPTOP_BI, NET_REVENUE_SQL

PROFILE, PLUGS = DA_LAPTOP_BI

PROBES = [
    Probe(
        id="dbt.show.net_revenue",
        profile=PROFILE,
        plugs=PLUGS,
        command=(
            "cd /workspace\n"
            "set -euo pipefail\n"
            "cd dbt_project\n"
            "dbt --quiet show --inline \"select sum(order_amount) as net_revenue from "
            "warehouse.warehouse.fct_orders where order_status = 'completed'\" "
            "--output json > /tmp/tour_dbt_show.json\n"
            "python3 -c 'import json; print(json.dumps({\"value\": "
            "json.load(open(\"/tmp/tour_dbt_show.json\"))[\"show\"][0][\"net_revenue\"]}))'\n"),
        expect_tools=("shim.dbt",),
        expect_args=("show",),
        truth_sql=NET_REVENUE_SQL,
        instruction=(
            "Using dbt from the repository's dbt project (with the profile a bare `dbt` "
            "picks up on this box), run an ad-hoc query that returns total Net Revenue: "
            "the sum of order_amount over completed orders in the fct_orders mart."),
    ),
    Probe(
        id="dbt.show.ref_inline",
        profile=PROFILE,
        plugs=PLUGS,
        command=(
            "cd /workspace\n"
            "set -euo pipefail\n"
            "cd dbt_project\n"
            "dbt --quiet show --inline \"select sum(order_amount) as net_revenue from "
            "{{ ref('fct_orders') }} where order_status = 'completed'\" "
            "--output json > /tmp/tour_dbt_show_ref.json "
            "|| { cat /tmp/tour_dbt_show_ref.json >&2; exit 1; }\n"
            "python3 -c 'import json; print(json.dumps({\"value\": "
            "json.load(open(\"/tmp/tour_dbt_show_ref.json\"))[\"show\"][0]"
            "[\"net_revenue\"]}))'\n"),
        expect_tools=("shim.dbt",),
        expect_args=("show",),
        truth_sql=NET_REVENUE_SQL,
        instruction=(
            "Using dbt from the repository's dbt project (with the profile a bare `dbt` "
            "picks up on this box), run an ad-hoc query over the fct_orders model - "
            "referenced the way the project's own models reference it - that returns "
            "total Net Revenue: order_amount summed over completed orders."),
    ),
]

KNOWN = {
    "dbt.show.ref_inline": (
        "awaiting user decision: theme macro fix moves BI001/BI002 world bytes "
        "(see relations.patch)"),
}
