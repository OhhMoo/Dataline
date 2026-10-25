# Architecture overview

How a night's EDI traffic becomes the inventory position, the 3PL billing
extract and the customs cutoff, and which pieces are load-bearing when
something disagrees.

## System at a glance

The nightly ASN/EDI feed for business date D lands at 00:00 UTC on D+1. It
lands as CSV partitions under `data/landing/inventory_edi/dt=D/`. The
pipeline for business date D runs in the early hours of D+1, UTC:

1. `edi_poll` runs at 00:30 and scans the partition. It requires at least
   five CSV files before anything downstream starts. A short partition
   fails here, at the poll. It does not fail halfway through the load.
2. `inventory_load` starts 5 minutes after the poll. It rebuilds every
   `raw.*` table from the landed partitions. It is a full reload, not an
   upsert. Whatever is landed is what `raw` shows, no more and no less.
3. `inventory_dbt` starts 10 minutes after the load. It builds the position
   fact and the cycle count reconciliation. It also builds the 3PL
   settlement extract and the customs cutoff. It runs the tests, including
   position-key uniqueness. A failed test fails the job. The marts then
   keep the previous night's rows.
4. `threepl_handoff` starts 10 minutes after the build. It exports
   `warehouse.threepl_settlement_extract`. It writes
   `data/exports/threepl/threepl_settlement.csv` for the Atlas 3PL billing
   consumer.
5. `customs_close` starts 5 minutes after the handoff. It advances
   `warehouse.customs_cutoff`. Declarations past the configured freeze
   window are frozen against the document cutoff.

The deployed job order is recorded in `orchestration/schedule.yml`. Treat
that file, not memory, as the schedule of record.

## DuckDB schema overview

One database file, two schemas.

`raw` mirrors the EDI partitions, one table per document stream:

| table | grain | source partition |
|---|---|---|
| `raw.asn_receipts` | one row per ASN receipt line | `data/landing/inventory_edi/dt=D/` |
| `raw.dc_movements` | one row per DC movement | `data/landing/inventory_edi/dt=D/` |
| `raw.cycle_counts` | one row per physical count line | `data/landing/inventory_edi/dt=D/` |
| `raw.uom_conversions` | one row per SKU case-pack conversion | `data/landing/inventory_edi/dt=D/` |
| `raw.customs_declarations` | one row per declaration document version | `data/landing/inventory_edi/dt=D/` |

`warehouse` holds the marts and the partner-facing surfaces:

| table | grain |
|---|---|
| `warehouse.fct_inventory_position` | one row per `position_date` per `warehouse_id` per `sku_id` |
| `warehouse.cycle_count_reconciliation` | one row per counted position variance |
| `warehouse.threepl_settlement_extract` | one row per 3PL billing line |
| `warehouse.customs_cutoff` | one row per declaration freeze observation |

Only received ASNs add on-hand stock. Cases convert to eaches through the
UOM table. Movements reduce stock. The fact is quantity-native, recorded
as `on_hand_each`. Money lives only in the settlement extract as
`total_charge_cents`. Cycle counts are a separate control from book stock.
SHRINKAGE rows in the reconciliation correct the book.

## Change control

The marts and the export are reviewed artefacts. Before altering any
pipeline parameter, confirm the live state rather than the intention.
Parameters include per-each rates and case-pack fallbacks. They also
include freeze windows or cutoff hours encoded in job code:

- active tickets in `fixtures/workflow/issues.yaml` (a parked ticket is not
  an approved change).
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`.
- the corrections register `warehouse.factory_corrections`. It records
  which positions or billing windows were reopened, by whom, and under
  which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

The current marts replaced the 3PL portal extracts in the 2024 cutover. Two
conventions from that programme are still load-bearing:

- The pre-cutover extracts applied their own rate and case-pack conventions
  downstream of the warehouse. Anything of that sort still present in job
  code retires under one of two rules. A closed business date or freeze
  window waits for controller sign-off. The sign-off names the date in
  `warehouse.factory_corrections`. A pre-cutover feed split multiplier or
  retention haircut retires on log evidence. Retire it once
  `var/logs/ingest_daemon.log` records the cutover as active. At that point,
  the feed delivers net `amount_cents` directly. The bridge then
  double-adjusts every affected row and must be removed. Do not delete or
  "fix" such logic on the strength of a comment alone. Do not assume it is
  active business policy either. Check `fixtures/workflow/issues.yaml` and
  the audit lines in `var/logs/` first. Confirm against
  `warehouse.factory_corrections`.
- Vendor billing splits were standardised at cutover. They are computed
  from eaches in the marts at the contracted per-each rates. They are never
  multiplied into raw ASN rows. Any deviation between
  `warehouse.threepl_settlement_extract` and `raw.asn_receipts` is evidence
  to reconcile. It is not evidence to copy forward.

Historical cutover review notes live with the decision records under
`docs/`. When a discrepancy smells like a cutover leftover, read those
notes before touching the pipeline.
