# bi/ - the dashboards as code

Every dashboard is a project in this directory, changed by pull request and
promoted by tooling (wiki: ae/runbooks/bi-release-process). Nobody publishes to
production from a desktop client.

| path | tool | what is in it | dashboards |
|---|---|---|---|
| `bi/powerbi/` | Power BI | a PBIP project: the semantic model as TMDL (`*.SemanticModel/definition/`), the report as PBIR (`*.Report/definition/`) | Executive Overview |
| `bi/tableau/` | Tableau | the published data source (`.tds`) and workbook (`.twb`), as XML | Sales Overview |
| `bi/sigma/` | Sigma | workbooks exported as JSON through the Sigma API | Customer Health, Finance close pack |

Not every deployment runs all three tools. A directory that is not here is a
tool this environment does not use.

## The semantic layer

The definition of record for every metric is the dbt Semantic Layer in
`dbt_project/models/semantic/`: semantic models over the star schema
(`semantic_models.yml`) and the published metrics (`metrics.yml`), in the
MetricFlow spec. `./run.sh sl` lists them.

A BI measure called "Net Revenue" - a DAX measure in the TMDL, a Tableau
calculated field, a Sigma metric - must agree with the dbt metric of the same
label. When they disagree, the BI tool is the one that is wrong. Two measures
exist only in the Power BI model because MetricFlow cannot express them:
Segment Share and List Price Revenue (`docs/metrics.md`).

## What the tools read

All three read the five tables in the `warehouse` schema and nothing else:

    fct_orders  fct_order_lines  dim_customer  dim_product  dim_date

Column names and types are a contract (`dbt_project/models/marts/_marts.yml`):
add columns at the end, never rename one in place. `dbt_project/models/
exposures.yml` records which dashboard reads what, so
`dbt ls --select +exposure:executive_overview` is the blast radius of a
change.

## Refreshes

One job refreshes every dashboard: `bi_refresh`, 06:00 UTC
(`dags/bi_refresh.py` -> `./run.sh refresh` ->
`scripts/refresh_dashboards.py`). It recomputes each dashboard's tiles from
the marts, stores them as the dashboards' cache, and logs the refresh, with the
newest business date the tiles could see, to `raw.bi_refresh_history`. It does
not wait for the nightly dbt build.
