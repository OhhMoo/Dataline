# Reconciliation SLA

The settlement nightly is held to the service levels in this document. The
reconciliations below prove those levels. The history below explains why the
rules take their current form. All times are UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| network partition for settlement day D landed | 04:00 on D | `data/payments_feed/dt=D/` present |
| `payments_nightly` complete | 20:30 | job green in `sched_state` |
| `dbt_payments` complete, tests green | 20:45 | job green; `warehouse.load_checkpoint.through_day` at D |
| `settlement_publish` complete | 21:00 | open days of D in `warehouse.settlement_handoff` |
| clearing cutoff record appended | 23:30 | `warehouse.clearing_window` row for D |

A normal night leaves roughly three hours between the handoff refresh and the
bank cutoff. That gap is the recovery window. It covers a failed build or a
withheld correction. A breach of the 04:00 landing SLA is for the network to
explain. A downstream breach with a healthy partition is ours to explain.

## Nightly reconciliation duties

Before the clearing window closes, verify in this order:

1. Row counts: compare `raw.payments_events` for D against the network
   delivery manifest. Compare current rows in `warehouse.fct_payments` for
   payment day D against events that won version precedence.
2. Handoff parity: for open days, check each merchant row in
   `warehouse.settlement_handoff`. Each row must equal the count and sum of
   `amount_cents` over current non-void payments for that settlement day.
3. Interchange: compute every fee in `warehouse.interchange_ledger` as
   integer division. Use `gross_cents * interchange_bps` divided by 10,000.
   A rounded-up fee is a defect, not a convention. A floating-point fee is
   also a defect.
4. Checkpoint: `warehouse.load_checkpoint.through_day` must name a partition
   present on disk. Never advance it to the execution date to hide a gap.
5. Corrections: a republished closed day appears in
   `warehouse.settlement_corrections`. The record gives the day, ticket key,
   approver, and approval date. Every republished merchant row carries that
   ticket in `correction_ref`. An unregistered change is withheld. See
   `docs/correction_protocol.md`.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`. Quote timestamps from those logs when raising or
closing an incident. Link the ticket in `fixtures/workflow/issues.yaml`.

## Partner onboarding history

- The card network feed was onboarded in 2023 under a land-then-reload
  contract. The network owns the partition until the load turns green.
  Recovery replays arrive as files under their own `dt=` folder. They never
  arrive as requests to delete landed rows. The platform has no outbound
  connection to the vendor.
- The control files moved to their single landing under `data/control/` in
  the 2024 cutover. The corrections register entered the nightly reload at
  the same time. A controller approval therefore lands as data. It does not
  land as a hand edit.
- The finance reconciliation consumer of `warehouse.interchange_ledger` was
  onboarded in 2024. Onboarding used the integer-division convention. Their
  close process assumes no floating-point money anywhere in the ledger.

## Legacy and provisional rules

Two retirement rules apply here, and they are not interchangeable.

(a) Closed-window restatements are governance. Once a settlement day is
closed, its published snapshot changes only when
`warehouse.factory_corrections` carries a controller sign-off naming that
day and the tracker ticket authorising the restatement. A draft or unsigned
entry never reopens a closed day.

(b) Pre-cutover feed adjustments are engineering. While the network extract
delivered gross, pre-split, or pre-fee amounts, the fold layer applied
temporary bridge adjustments. Those adjustments were pre-cutover split
multipliers and fee retention haircuts. They made the warehouse see net
figures. Those adjustments retire on log evidence, not on tickets. Retire
them once `var/logs/ingest_daemon.log` records the network extract cutover
as active. At that point, the feed delivers net `amount_cents` directly.
Any leftover multiplier or haircut then double-adjusts every affected row.
Remove it so warehouse facts match the landed net cents. Before changing a
rate, check `var/logs/ingest_daemon.log` for the delivery. Also check
`var/logs/sftp_sync.log` for the delivery. Afterwards, reconcile the
affected day against the raw landed rows.

Treating a closed-day restatement as an engineering fix is a governance
breach. Treating a dead feed bridge as governance lets double-counted
amounts survive. It also lets double-discounted amounts survive in the
handoff.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to card network integrations when the partition is late or short.
Escalate internally when our load, build, publish, or cutoff broke on a
healthy partition. Post every SLA breach in `#payments`. Include breaches
that the next night's reload silently heals. Give the log timestamps that
bracket each breach. Contacts are in `docs/contacts.md`.
