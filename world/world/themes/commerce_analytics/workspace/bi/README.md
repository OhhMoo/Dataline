# bi/ - the dashboards as code

Every dashboard is a project in this directory, changed by pull request and
promoted by tooling (wiki: ae/runbooks/bi-release-process). Nobody publishes to
production from a desktop client.

| path | tool | what is in it |
|---|---|---|
| `bi/powerbi/` | Power BI | a PBIP project: `Executive Overview.pbip`, the `Commerce` semantic model as TMDL (`Commerce.SemanticModel/definition/`) and the report as PBIR (`Executive Overview.Report/definition/`) |
| `bi/tableau/` | Tableau | the published data source `datasources/Commerce.tds` and the workbook `workbooks/Executive Overview.twb`, as XML |
| `bi/sigma/` | Sigma | what the Sigma API returns for each document, as JSON: the data model `data-models/commerce.json`, the workbook `workbooks/executive-overview.json`, and the connection with its synced tables `connections/snowflake.json` |

Not every deployment runs all three tools. A directory that is not here is a
tool this environment does not use.

Each project is one team's build of the Executive Overview over the same
`Commerce` model: leadership's in Power BI, the one Sales rebuilt in Tableau,
and the one Finance rebuilt in Sigma (`dbt_project/models/exposures.yml`).
Where a tool has no exact way to say a measure, its project leaves the measure
out rather than approximating it: Revenue LY and YoY Growth (time
intelligence) are Power BI only, and Sigma has no Segment Share.

How each one is published - and why a change that is not committed does not
survive - is the wiki's ae/runbooks/bi-release-process.

## The semantic layer

The definition of record for every metric is the dbt Semantic Layer in
`dbt_project/models/semantic/`: semantic models over the star schema
(`semantic_models.yml`) and the published metrics (`metrics.yml`), in the
MetricFlow spec. `./run.sh sl` lists them.

A BI measure called "Net Revenue" - a DAX measure in the TMDL, a Tableau
calculated field, a Sigma metric - must agree with the dbt metric of the same
label. When they disagree, the BI tool is the one that is wrong. Two measures
exist only in the BI tools because MetricFlow cannot express them: Segment
Share and List Price Revenue (`docs/metrics.md`).

## What the tools read

All three read the five tables in the `warehouse` schema and nothing else:

    fct_orders  fct_order_lines  dim_customer  dim_product  dim_date

Column names and types are a contract (`dbt_project/models/marts/_marts.yml`):
add columns at the end, never rename one in place. `dbt_project/models/
exposures.yml` records which dashboard reads what, so
`dbt ls --select +exposure:executive_overview` is the blast radius of a
change.

## Refreshes

Tableau and Sigma read the warehouse live. The Power BI model is an import
model: it shows what its last refresh loaded, and a deploy refreshes it.

The certified SQL behind the tiles (the panel definitions the BI plane serves)
is refreshed by one job: `bi_refresh`, 06:00 UTC (`dags/bi_refresh.py` ->
`./run.sh refresh` -> `scripts/refresh_dashboards.py`). It recomputes each
dashboard's tiles from the marts, stores them as the dashboards' cache, and
logs the refresh, with the newest business date the tiles could see, to
`raw.bi_refresh_history`. It does not wait for the nightly dbt build.
