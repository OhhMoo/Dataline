"""Tool tour probes: one module per tool, each defining `PROBES` (see ../tool_tour.py).

The contract a module keeps:

  PROBES   a list of `tool_tour.Probe`. Every id starts with this module's name
           ("<module>.<client>.<what>"): the module name IS the tool, and
           `tool_tour.load_probes(["<module>"])` is how one tool is toured.
  KNOWN    optional {probe id: cause}: a FAIL whose cause is known and owned by
           another task. It is reported beside the verdict and never changes it.

A probe's `command` is bash run inside the episode, as the agent would run it: the
real client, configured from the box's own discovery files (the environment,
`.envrc`, `docs/onboarding.md`, `~/.dbt/profiles.yml`,
`~/.config/alkera/services.toml`). It must be self-contained - Task 11 copies it
verbatim into a Harbor task's solve.sh - so it starts with `cd /workspace`, and it
prints `{"value": ...}` as its last stdout line (or `{"doc": "<why>"}` for a path
that is refused by name; never both).

The Probe's fields (tool_tour_core.Probe): `plugs` is written as a mapping
(`{"bi": ("sigma",)}`, a bare string is one plug) and stored as sorted pairs, so a
Probe is hashable; `expect_tools` is all-of groups of any-of audit `tool` values (a
bare tool is a one-tool group: `("shim.dbt",)`, or
`(("databricks.table_get", "databricks.tables_list"),)`), at least one group and no
empty one, and a row meets a group only when its `tool` is in it AND its `exit` is
"ok" (a failed call proves nothing); `expect_args` are substrings that must appear in
the argv of at least one of the rows that meet a group (`("show",)` for `dbt show`);
`expect_targets` are fnmatch globs, all-of, each matching a `targets` entry of one of
those rows (`("*.xlsx",)`); `rows_at="teardown"` is for a tool whose rows a
tailer writes in the stack's `down()` - the probe then gets a stack of its own and
is judged after teardown.

`truth_sql` runs on the HOST against the world as it was built, before the stack
comes up (tool_tour_core.truth): DuckDB on the world's warehouse file, read-only, so
the file's catalog is `warehouse` and a table is `warehouse.warehouse.<table>`;
`file_search_path` is the world root, so `read_json('var/workflow/issues.json')`
reads the world's own file. NULL is not a truth: it fails the probe.

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
