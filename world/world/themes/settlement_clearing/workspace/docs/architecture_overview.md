# Architecture overview

How a day's card network traffic becomes the settlement handoff the clearing
bank acknowledges, and which pieces are load-bearing when something disagrees.

## System at a glance

The card network delivers one partition per calendar day. The partition
lands under `data/payments_feed/dt=<settlement day>/` at 04:00 UTC. A
partition holds `events.csv` and may hold recovery files. A recovery file
can arrive in a later partition. It can repeat an event id or an older
payment version. The control files land once under `data/control/`. They
are merchant terms, settlement config, and the corrections register. The
pipeline for settlement day D runs entirely on calendar day D, UTC:

1. `payments_nightly` (20:00) rebuilds every `raw.*` table from every feed
   partition and control file on disk. It is a full reload with no append
   mode and no date flags. Whatever is landed is what `raw` shows, no more
   and no less.
2. `dbt_payments` starts 10 minutes after the load. It materialises staging
   and the warehouse models. Those models are current payments, the merchant
   dimension, and the checkpoint. They also include the correction register
   and the interchange ledger. It runs the tests. A failed test fails the
   job. The warehouse then keeps the previous run's rows.
3. `settlement_publish` starts 10 minutes after the build. It updates
   `warehouse.settlement_handoff` for open days. It also republishes
   `warehouse.interchange_ledger` to finance reconciliation.
4. `clearing_window_close` runs at 23:30. It appends the bank's cutoff
   record to `warehouse.clearing_window`. It never updates a prior row. A
   `SETTLED` row closes the day. A day observed in the feed with nothing in
   the handoff receives an `SLA_BREACH` row. Later publication cannot erase
   that row.

The deployed job order is recorded in `orchestration/schedule.yml`. Treat
that file, not memory, as the schedule of record.

## DuckDB schema overview

One database file, two schemas.

`raw` mirrors the network feed and the control files:

| table | grain | source |
|---|---|---|
| `raw.payments_events` | one row per delivered event version, with its source partition | `data/payments_feed/dt=D/` |
| `raw.merchant_terms` | one row per merchant terms record | `data/control/` |
| `raw.settlement_config` | one row per settlement config record | `data/control/` |
| `raw.settlement_corrections` | one row per controller-approved correction | `data/control/settlement_corrections.csv` |

`warehouse` holds the current-state models and the bank-facing surfaces:

| table | grain |
|---|---|
| `warehouse.fct_payments` | one row per `payment_id` at its highest non-void version |
| `warehouse.dim_merchant` | one row per `merchant_id` |
| `warehouse.settlement_corrections` | one row per registered correction |
| `warehouse.load_checkpoint` | one row; `through_day` is the newest partition actually read |
| `warehouse.interchange_ledger` | one row per merchant per settlement day |
| `warehouse.settlement_handoff` | one row per settlement day per merchant, frozen layout |
| `warehouse.clearing_window` | append-only; one row per cutoff observation per day |

Staging models (`stg_*`) are views over `raw`. The warehouse models are
tables. Version precedence uses the highest `version`. Ties use
`delivered_at`, then `event_id`, both descending. Precedence applies in the
marts. It never applies by deleting landed rows. A highest-version `void`
withdraws the payment. It therefore has no row in `warehouse.fct_payments`.
Amounts are integer cents end to end.

## Change control

The marts, the publisher, and the cutoff script are reviewed artefacts.
`docs/correction_protocol.md` governs every change to a closed day. Before
altering any pipeline parameter, confirm the live state rather than the
intention. Parameters include window lengths and interchange basis points.
They also include cutoff or tie-break conventions encoded in job code:

- active tickets in `fixtures/workflow/issues.yaml` (a parked ticket is not
  an approved change).
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`.
- the corrections register `warehouse.factory_corrections`, which records
  which settlement days were reopened, by whom, and under which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

The current pipeline replaced the pre-cutover finance extracts in the 2024
cutover. Two conventions from that programme are still load-bearing:

- The pre-cutover extracts applied their own fee and split conventions
  downstream of the warehouse. Those conventions were reviewed at cutover.
  Anything of that sort still present in job code retires under one of two
  rules. A closed settlement day waits for controller sign-off naming the
  day in `warehouse.factory_corrections`. A pre-cutover feed split
  multiplier or fee retention haircut retires on log evidence. Retire it
  once `var/logs/ingest_daemon.log` records the cutover as active. At that
  point, the feed delivers net `amount_cents` directly. The bridge then
  double-adjusts every affected row and must be removed. Do not delete or
  "fix" such logic on the strength of a comment alone. Do not assume it is
  active business policy either. Check `fixtures/workflow/issues.yaml` and
  the audit lines in `var/logs/` first. Confirm against
  `warehouse.factory_corrections`.
- Merchant and network fee splits were standardised at cutover. They are
  computed in `warehouse.interchange_ledger` as integer division. The
  division uses `gross_cents * interchange_bps` divided by 10,000. They are
  never multiplied into raw payment rows. They are never floating-point and
  never rounded up. Any deviation between `warehouse.fct_payments` and
  `raw.payments_events` is evidence to reconcile. It is not evidence to
  copy forward.

Historical cutover review notes live with the decision records under
`docs/`. When a discrepancy smells like a cutover leftover, read those
notes before touching the pipeline.
