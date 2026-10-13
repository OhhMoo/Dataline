"""postgres_dbt: dbt-postgres run live against the stack's Postgres (de/postgres), from
a project whose profile names only the box's PG* variables.

The model is a table over a `source()` of the seeded mart (`warehouse.fct_orders`,
copied from the world when the box is prepared), materialised in `staging`, where the
agent's role may create; `dbt show --inline` reads it back through `ref()`. Two kinds
of row prove it: the `dbt` PATH shim's (`shim.dbt`, as each command returns) and the
server's own (`psql.statement`, every statement dbt-postgres sent, folded in from the
server log at teardown) - so the probe is `rows_at="teardown"`.
"""
from tool_tour import Probe

from tool_tour_probes import NET_REVENUE_FEB_2026_SQL

_DIR = "/tmp/tour_pg_dbt"

PROBES = [
    Probe(
        id="postgres_dbt.run.net_revenue_feb_2026",
        profile="de/postgres",
        plugs={},
        command=(
            "cd /workspace\n"
            "set -euo pipefail\n"
            f"d={_DIR}\n"
            "rm -rf \"$d\" && mkdir -p \"$d/models\"\n"
            "cat > \"$d/dbt_project.yml\" <<'YML'\n"
            "name: tour_pg\n"
            "version: \"1.0\"\n"
            "profile: tour_pg\n"
            "config-version: 2\n"
            "model-paths: [models]\n"
            "YML\n"
            "cat > \"$d/profiles.yml\" <<'YML'\n"
            "tour_pg:\n"
            "  target: postgres\n"
            "  outputs:\n"
            "    postgres:\n"
            "      type: postgres\n"
            "      host: \"{{ env_var('PGHOST') }}\"\n"
            "      port: \"{{ env_var('PGPORT') | as_number }}\"\n"
            "      user: \"{{ env_var('PGUSER') }}\"\n"
            "      password: \"{{ env_var('PGPASSWORD') }}\"\n"
            "      dbname: \"{{ env_var('PGDATABASE') }}\"\n"
            "      schema: staging\n"
            "      threads: 1\n"
            "YML\n"
            "cat > \"$d/models/sources.yml\" <<'YML'\n"
            "version: 2\n"
            "sources:\n"
            "  - name: warehouse\n"
            "    schema: warehouse\n"
            "    tables:\n"
            "      - name: fct_orders\n"
            "YML\n"
            "cat > \"$d/models/tour_net_revenue_feb.sql\" <<'SQL'\n"
            "{{ config(materialized='table') }}\n"
            "select sum(order_amount) as net_revenue\n"
            "from {{ source('warehouse', 'fct_orders') }}\n"
            "where order_status = 'completed'\n"
            "  and order_date >= date '2026-02-01' and order_date < date '2026-03-01'\n"
            "SQL\n"
            "cd \"$d\"\n"
            "dbt --quiet debug --profiles-dir \"$d\" > /dev/null\n"
            "dbt --quiet run --profiles-dir \"$d\" -s tour_net_revenue_feb > /dev/null\n"
            "dbt --quiet show --profiles-dir \"$d\" --inline \"select net_revenue from "
            "{{ ref('tour_net_revenue_feb') }}\" --output json > \"$d/show.json\"\n"
            "python3 -c 'import json; print(json.dumps({\"value\": "
            "json.load(open(\"/tmp/tour_pg_dbt/show.json\"))[\"show\"][0][\"net_revenue\"]}))'\n"),
        expect_tools=("shim.dbt", "psql.statement"),
        expect_args=("run",),
        truth_sql=NET_REVENUE_FEB_2026_SQL,
        rows_at="teardown",
        instruction=(
            "Build February 2026 Net Revenue (order_amount summed over completed orders "
            "placed in February 2026, from warehouse.fct_orders) as a dbt model on the "
            "team's Postgres warehouse, using dbt with the Postgres connection settings "
            "this box provides, and report the value the model holds."),
    ),
]
