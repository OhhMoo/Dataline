# Architecture overview

How a night's shop activity becomes a dashboard number, and which pieces are
load-bearing when something disagrees.

## System at a glance

The shop platform exports a change-data-capture extract every night and drops
it as CSV partitions under `data/landing/<entity>/dt=<business date>/`. The
pipeline for business date D runs entirely on calendar day D+1, UTC:

1. `daily_load` (02:30) rebuilds every `raw.*` table from every partition on
   disk. It is a full reload, not an upsert. Whatever is landed is what `raw`
   shows, no more and no less.
2. `dbt_build` (starts 10 minutes after the load) materialises staging and the
   marts. It runs the data tests (grain uniqueness, order/line
   reconciliation). It also validates the semantic layer. A failed test
   fails the job. It leaves the marts holding the previous night's rows.
3. `bi_refresh` (06:00) re-renders every deployed dashboard against the marts
   as they stand at that moment. It is its own DAG, with no dependency on the
   build. On a late night, it happily refreshes yesterday's data and says
   nothing.

The deployed job order is recorded in `orchestration/schedule.yml`. Treat
that file, not memory, as the schedule of record.

## DuckDB schema overview

One database file, two schemas.

`raw` mirrors the extract, one table per entity, partitioned by `dt`:

| table | grain | source partition |
|---|---|---|
| `raw.orders` | one row per `order_id` per `dt` | `data/landing/orders/dt=D/` |
| `raw.order_lines` | one row per `order_line_id` per `dt` | `data/landing/order_lines/dt=D/` |
| `raw.customers` | one row per `customer_id` per `dt` | `data/landing/customers/dt=D/` |
| `raw.products` | one row per `product_id` per `dt` | `data/landing/products/dt=D/` |
| `raw.bi_refresh_history` | one row per dashboard per refresh | written by the refresh job |

`warehouse` is the star schema the semantic layer and every BI tool read:

| table | grain |
|---|---|
| `warehouse.dim_customer` | one row per `customer_id` |
| `warehouse.dim_product` | one row per `product_id` |
| `warehouse.dim_date` | one row per calendar date |
| `warehouse.fct_orders` | one row per `order_id` |
| `warehouse.fct_order_lines` | one row per `order_line_id` |

Staging models (`stg_*`) are views over `raw`. The facts and dimensions are
tables. Amounts on the facts are order- or line-grain measures. The semantic
layer aggregates them. Never pre-aggregate them in a report extract.

## Change control

The marts and the semantic layer are reviewed artefacts. A model change ships
through the PBIP pull request and CI before it reaches a dashboard. During
the month-end freeze, no metric definition or published dashboard changes at
all. Before altering any pipeline parameter (load windows, rates, currency
or split conventions encoded in job code), confirm the live state. Do not
rely on intention:

- active tickets in `fixtures/workflow/issues.yaml` (a parked ticket is not
  an approved change),
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`,
- the corrections register `warehouse.factory_corrections`, which records
  which business days were reopened, by whom, and under which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

The star schema replaced the old reporting extracts in the 2024 cutover. Two
conventions from that programme are still load-bearing:

- The pre-cutover finance extracts applied their own fee and split
  conventions downstream of the warehouse. Those conventions were reviewed
  at cutover. Anything of that sort still present in job code is a legacy
  placeholder. It stays so until a ticket and the logs confirm it was
  retired. Do not delete or "fix" such logic on the strength of a comment
  alone. Do not assume it is active business policy either. Check
  `fixtures/workflow/issues.yaml` and the audit lines in `var/logs/` first.
- Partner and reseller splits were standardised at cutover to be computed
  from net order amounts in the marts, never multiplied into raw rows. Any
  deviation visible in `warehouse.fct_order_lines` versus `raw.order_lines`
  is evidence to reconcile, not to copy forward.

Retirement evidence depends on the kind of legacy logic. Restating a closed
business day is governance. It waits for a controller sign-off in
`warehouse.factory_corrections` naming that day. Pre-cutover feed split
multipliers or fee retention haircuts in the fold layer are engineering.
They retire when `var/logs/ingest_daemon.log` records the extract cutover
active. The feed then delivers net amount_cents directly. A leftover one
double-adjusts every affected row. Remove it on that log evidence.

Historical cutover review notes live with the architecture decision records
under `docs/`. When a discrepancy smells like a cutover leftover, read those
before touching the pipeline.
