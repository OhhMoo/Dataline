# Reconciliation SLA

The inventory nightly is held to the service levels in this document. The
reconciliations below prove those levels. The history below explains why the
rules take their current form. All times are UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| EDI partition for business date D landed | 00:00 on D+1 | `data/landing/inventory_edi/dt=D/` present |
| `edi_poll` confirms the partition | 00:40 | at least five CSV files scanned |
| `inventory_load` complete | 00:55 | job green in `sched_state` |
| `inventory_dbt` complete, tests green | 01:15 | marts carry D |
| `threepl_handoff` complete | 01:25 | extract table and CSV export present |
| `customs_close` complete | 01:35 | `warehouse.customs_cutoff` advanced |
| customs declaration freeze evaluated | 18:00 | declarations inside the freeze window frozen |

The chain finishes inside two hours of landing. That timing leaves the
working day for count investigations and vendor queries. A breach of the
00:00 landing SLA is for the partner to explain. A downstream breach with a
healthy partition is ours to explain.

## Nightly reconciliation duties

Before the DCs open, verify in this order:

1. Completeness: compare the poll file count against the partner nightly
   manifest. Compare `raw.asn_receipts` for D against the ASNs that the DCs
   expected.
2. Position parity: on-hand stock in `warehouse.fct_inventory_position` for
   D moves only through received ASNs and movements. Received ASNs convert
   to eaches through the UOM table. Any other jump is a defect, not a
   convention.
3. Counts: cycle count variances appear in
   `warehouse.cycle_count_reconciliation`. SHRINKAGE rows correct book
   stock. A hand edit to the position fact never corrects book stock.
4. 3PL billing: `total_charge_cents` on the settlement extract equals the
   contracted receipt, outbound and storage per-each rates applied to
   eaches, and the CSV under `data/exports/threepl/` matches the table.
5. Customs: declarations past the configured freeze window are frozen in
   `warehouse.customs_cutoff`. A reopened window appears in
   `warehouse.factory_corrections` with sign-off and a ticket. An
   unregistered change stays withheld from customs-compliance until
   registered.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`. Quote timestamps from those logs when raising or
closing an incident. Link the ticket in `fixtures/workflow/issues.yaml`.

## Partner onboarding history

- The ASN/EDI feed was onboarded in 2023 under a land-then-reload contract.
  The partner owns the partition until the load turns green. Resends are
  whole replacement files under the same `dt=` folder. They are never
  deltas.
- The five-file poll minimum dates from a 2023 incident. A partial envelope
  loaded cleanly in that incident. It quietly dropped a document stream.
  The poll makes a short partition fail loudly at 00:30 instead.
- Atlas 3PL billing was onboarded in 2024. The settlement extract then
  replaced the portal spreadsheets. The per-each rate card was standardised
  at that cutover. The case-pack conversion conventions were also
  standardised then.

## Legacy and provisional rules

Two retirement rules apply here, and they are not interchangeable.

(a) Closed-window restatements are governance. Once a business date is
closed, its published snapshot changes only when
`warehouse.factory_corrections` carries a controller sign-off naming that
date and the tracker ticket authorising the restatement. A draft or
unsigned entry never reopens a closed date.

(b) Pre-cutover feed adjustments are engineering. While the partner EDI
feed delivered gross, pre-split, or pre-fee amounts, the fold layer applied
temporary bridge adjustments. Those adjustments were pre-cutover split
multipliers and fee retention haircuts. They made the warehouse see net
figures. Those adjustments retire on log evidence, not on tickets. Retire
them once `var/logs/ingest_daemon.log` records the partner extract cutover
as active. At that point, the feed delivers net `amount_cents` directly.
Any leftover multiplier or haircut then double-adjusts every affected row.
Remove it so warehouse facts match the landed net cents. Before changing a
rate, check `var/logs/ingest_daemon.log` for the delivery. Also check
`var/logs/sftp_sync.log` for the delivery. Afterwards, reconcile the
affected day against the raw landed rows.

Treating a closed-window restatement as an engineering fix is a governance
breach. Treating a dead feed bridge as governance lets double-counted
charges survive. It also lets double-discounted charges survive in the
settlement extract.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to the partner when the EDI partition is late or short. Escalate
internally when our poll, load, build, export, or close broke on a healthy
partition. Post every SLA breach with the log timestamps that bracket it.
Include breaches that the next night's reload silently heals.
