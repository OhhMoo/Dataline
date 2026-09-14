# Commercial data platform

- `pipelines/` - the orders stream pipeline, executed by the SQL runner.
- `dbt_project/` - the reporting models built on top of `warehouse.fct_orders`.
- `docs/` - runbooks and the data dictionary. Freshness varies.
- `asset_platform/` - read-only mirror of the *other* orchestrator's run state.
  That platform belongs to the marketing data team; it is mirrored here only so
  lineage tooling can see across the two.

The warehouse is reachable through the SQL tools, not through the filesystem.
