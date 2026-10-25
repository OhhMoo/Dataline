# Architecture overview

How a night's 837/835 EDI delivery becomes an adjudicated claim fact is the
subject of this document. It also covers a capitation settlement and a CMS
encounter window. It notes which pieces are load-bearing when something
disagrees.

## System at a glance

The clearinghouse drops claim and remittance extracts as CSV partitions.
The partitions sit under `data/landing/<entity>/dt=<service partition>/`.
The arrival `edi_claims_remit_delivery` is due at 00:30 UTC for business date
D. The pipeline for D runs entirely on calendar day D+1, UTC:

1. `nightly_edi_load` (01:10) rebuilds `raw.edi_837_claims`,
   `raw.edi_835_remits`, and `raw.provider_roster` from every partition on
   disk. It is a full reload, not an upsert: every transmitted claim version
   (originals, replacements, and voids) is preserved exactly as landed.
2. `adjudication_build` (starts 10 minutes after the load) materialises
   staging and `warehouse.fct_adjudicated_claims` with `warehouse.dim_provider`,
   and runs the data tests (claim grain uniqueness, COB and financial-balance
   identities). A failed test fails the job and leaves the marts holding the
   previous night's rows. Staging selects the latest claim version by received
   date, version number, and control number.
3. `settlement_handoffs` (15 minutes after adjudication) builds
   `warehouse.actuarial_reserve_handoff` for the actuarial reserving consumer
   and `warehouse.provider_capitation_settlement` for provider settlement.
4. `cms_window_close` (10 minutes after the handoffs) builds
   `warehouse.cms_submission_window` for the encounter submission consumer.

The deployed job order is recorded in `orchestration/schedule.yml`. Treat
that file, not memory, as the schedule of record.

## DuckDB schema overview

One database file, two schemas.

`raw` mirrors the EDI extract, one table per feed, partitioned by `dt`:

| table | grain | source partition |
|---|---|---|
| `raw.edi_837_claims` | one row per claim version per `dt` | `data/landing/edi_837_claims/dt=D/` |
| `raw.edi_835_remits` | one row per remit per claim version | `data/landing/edi_835_remits/dt=D/` |
| `raw.provider_roster` | one row per provider contract snapshot | `data/landing/provider_roster/dt=D/` |

`warehouse` is what the payer operations, actuarial, settlement, and CMS
consumers read:

| table | grain |
|---|---|
| `warehouse.fct_adjudicated_claims` | one current row per `claim_id` |
| `warehouse.dim_provider` | one row per provider contract |
| `warehouse.actuarial_reserve_handoff` | one row per service date |
| `warehouse.provider_capitation_settlement` | one row per service month and provider |
| `warehouse.cms_submission_window` | one row per service month |

Staging models (`stg_*`) are views over `raw`. The facts and handoffs are
tables. Amounts in raw remain text and are cast in staging. Never pre-cast
or pre-aggregate them in a report extract.

## Change control

The marts and the handoffs are reviewed artefacts. A model change ships
through the pull request and CI before it reaches a consumer. Confirm the
live state rather than the intention before altering any pipeline parameter.
Such parameters include load windows, frequency-code precedence, and reserve
basis points. They also include withhold or cutoff conventions encoded in
job code:

- active tickets in `fixtures/workflow/issues.yaml` (a parked ticket is not
  an approved change),
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`,
- the corrections register `warehouse.factory_corrections`, which records
  which service partitions were reopened, by whom, and under which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

The adjudicated-claim star replaced the old clearinghouse status extracts in
the 2024 cutover. At that time, the 835 remit pairing moved from a
spreadsheet reconciliation into the nightly build. Two conventions from
that programme are still load-bearing:

- The pre-cutover extracts applied their own allowed-amount and COB
  conventions downstream of the warehouse. Those conventions were reviewed at
  cutover. Anything of that sort still present in job code is a legacy
  placeholder. Do not delete or "fix" such logic on the strength of a comment
  alone. A ticket and the logs must confirm retirement first. Do not assume
  the logic is active business policy either. Check
  `fixtures/workflow/issues.yaml` and the audit lines in `var/logs/` first.
- Trading-partner and vendor splits were standardised at cutover to be
  computed in the marts from adjudicated amounts, never multiplied into raw
  837 rows. Any deviation visible in `warehouse.fct_adjudicated_claims`
  versus `raw.edi_837_claims` is evidence to reconcile, not to copy forward.

Retirement evidence depends on the kind of legacy logic. Restating a closed
day is governance. It waits for a controller sign-off in
`warehouse.factory_corrections` naming that day. Pre-cutover feed split
multipliers in the fold layer are engineering. Fee retention haircuts in the
fold layer are also engineering. They retire when
`var/logs/ingest_daemon.log` records the extract cutover active. At that
point, the feed delivers net amount_cents directly. A leftover multiplier or
haircut double-adjusts every affected row. Remove it on that log evidence.

Historical cutover review notes live with the architecture decision records
under `docs/`. When a discrepancy smells like a cutover leftover, read those
notes before touching the pipeline.
