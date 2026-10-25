# Reconciliation SLA

The service levels the attribution nightly is held to, the reconciliations
that prove them, and the history that explains why the rules are the way
they are. All times UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| export for event date D landed | 00:30 on D+1 | all three `dt=D` partitions present |
| `ingest_events` complete | 01:15 | job green in `sched_state` |
| `attribute_conversions` complete, tests green | 01:30 | `warehouse.fct_attributed_conversions` carries D |
| `lock_partner_billing` complete, reconciliation green | 01:45 | handoff and lock rebuilt together |
| Partner Finance handoff consumable | 02:00 | handoff visible over SFTP; locked rows stable |

The whole chain is due inside two hours of the export. A breach of the
00:30 landing SLA is the network's to explain. A downstream breach with
healthy partitions is ours.

## Nightly reconciliation duties

Before Partner Finance opens the handoff, verify in this order:

1. Row counts: raw postbacks versus unique conversions. A postback count
   above the conversion count is normal S2S retry traffic. The reverse is
   not. A conversion missing from the fact also needs a reason.
2. Exclusions: traffic matching an active bot rule carries the exclusion,
   no excluded click wins attribution, and the lookback boundary test
   covers the seven-day edge.
3. Handoff parity: `warehouse.partner_payout_handoff` equals the fact
   summed at event-date/partner grain, with each conversion's payout as
   `floor(revenue_cents * revshare_bps / 10000)`.
4. Lock coverage: on a night whose newest attributed event date is D,
   `warehouse.billing_lock` covers dates through D minus the configured
   billing lag, and no locked row differs from its handoff amount.
5. Adjustments: any change to a locked partner/date arrives as a separate
   adjustment. It needs Partner Finance approval and a ticket. It never
   arrives as an edit to the lock. Register it in
   `warehouse.factory_corrections` before downstream consumption.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`. Quote timestamps from those logs when raising or
closing an incident. Link the ticket in `fixtures/workflow/issues.yaml`.

## Partner onboarding history

- The S2S postback endpoint was onboarded in 2023 with a land-then-reload
  contract. Duplicate deliveries during partner deploy windows are
  expected. Confirm the warehouse grain absorbed them. The grain is one
  row per `conversion_id`, with earliest `received_at`, then lowest
  `postback_id`. Do not delete landed postbacks.
- The `network_default` attribution rule (the seven-day lookback) shipped
  with that 2023 onboarding. It has been the reference for every boundary
  test since.
- The partner payout handoff moved to SFTP in the 2024 cutover, when the
  billing lock and its one-day lag were introduced. Open dates stay in the
  handoff for partner visibility. They carry no lock row.

## Legacy and provisional rules

Two retirement rules apply here, and they are not interchangeable.

(a) Closed-window restatements are governance. Once a partner billing date
is locked, its published snapshot changes only when
`warehouse.factory_corrections` carries a controller sign-off naming that
date and the tracker ticket authorising the restatement. A draft or
unsigned entry never reopens a locked date.

(b) Pre-cutover feed adjustments are engineering. While the network export
delivered gross, pre-split or pre-fee amounts, the fold layer applied
temporary bridge adjustments. Those adjustments included pre-cutover split
multipliers and fee retention haircuts. They let the warehouse see net
figures. Those adjustments retire on log evidence, not on tickets. The
record is in `var/logs/ingest_daemon.log`. It must show the partner export
cutover active. The feed must now deliver net `amount_cents` directly. Any
leftover multiplier or haircut in the fold script then double-adjusts
every affected row. It must be removed so warehouse facts match the landed
net cents. Before changing a rate, check `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log` for the delivery. Afterwards, reconcile the
affected day against the raw landed rows.

Treating a locked-date restatement as an engineering fix is a governance
breach. Treating a dead feed bridge as governance lets double-counted and
double-discounted payouts survive in the handoff.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to the network when the export is late or short. Escalate
internally when our ingest, attribution or lock broke on a healthy export.
Post every SLA breach, including ones the next night's reload silently
heals. Include the log timestamps that bracket it.
