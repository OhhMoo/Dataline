"""Tool tour probes: one module per tool, each defining `PROBES` (see ../tool_tour.py).

The contract a module keeps:

  PROBES   a list of `tool_tour.Probe`. Every id starts with this module's name
           ("<module>.<client>.<what>"): the module name IS the tool, and
           `tool_tour.load_probes(["<module>"])` is how one tool is toured.
  KNOWN    optional {probe id: cause}: a FAIL whose cause is known and owned by
           another task. It is reported beside the verdict and never changes it.

A probe's `command` is bash run inside the episode from /workspace, as the agent
would run it: the real client, configured from the box's own discovery files
(the environment, `.envrc`, `docs/onboarding.md`, `~/.dbt/profiles.yml`,
`~/.config/alkera/services.toml`). It must be self-contained - Task 11 copies it
verbatim into a Harbor task's solve.sh - and print `{"value": ...}` as its last
stdout line (or `{"doc": "<why>"}` for a path that is refused by name).

`truth_sql` runs on the HOST against the world (tool_tour.truth): DuckDB on the
world's warehouse file, read-only, so the file's catalog is `warehouse` and a
table is `warehouse.warehouse.<table>`; `file_search_path` is the world root, so
`read_json('var/workflow/issues.json')` reads the world's own file.

`instruction` is the brief a live agent gets for the same probe (Task 11): what
to find out, never which command finds it.

The facts below are the ones the commerce_analytics world (seed 11, the
BI001/BI002 seed) pins; they are defined once so every tool reads the same one.
"""

#: Net Revenue: the sum of `order_amount` over completed orders (33733790.00 on seed 11).
NET_REVENUE_SQL = ("SELECT SUM(order_amount) FROM warehouse.warehouse.fct_orders "
                   "WHERE order_status = 'completed'")
#: February 2026 Net Revenue, by order date (2361224.00 on seed 11).
NET_REVENUE_FEB_2026_SQL = (NET_REVENUE_SQL + " AND order_date >= DATE '2026-02-01' "
                            "AND order_date < DATE '2026-03-01'")
#: Completed orders placed in February 2026.
COMPLETED_ORDERS_FEB_2026_SQL = (
    "SELECT COUNT(*) FROM warehouse.warehouse.fct_orders WHERE order_status = 'completed' "
    "AND order_date >= DATE '2026-02-01' AND order_date < DATE '2026-03-01'")

#: The profile BI001/BI002 run on: the analyst's laptop with the three BI vendors.
DA_LAPTOP_BI = ("da/laptop", {"bi": ("powerbi", "sigma", "tableau")})
