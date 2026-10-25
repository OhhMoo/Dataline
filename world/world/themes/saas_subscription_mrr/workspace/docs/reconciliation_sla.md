# Reconciliation SLA

The service levels the billing nightly is held to, the reconciliations that
prove them, and the history that explains why the rules are the way they
are. All times UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| billing day D sealed and landed | 00:00 on D+1 | both `dt=D` landing partitions present |
| `ingest_webhooks` complete | 01:15 | job green in `sched_state` |
| `fold_subscriptions` complete, tests green | 01:30 | `warehouse.fct_mrr_daily` carries D |
| `revrec_window_close` complete | 01:45 | close ledger published for the period |
| Finance ARR dashboard and close ledger current | 02:00 | spot check against `warehouse.fct_mrr_daily` |

The chain is short and strictly sequential; there is no parallel branch to
hide a late ingest behind. A breach of the 00:00 sealing SLA is the billing
platform's to explain; a breach of anything downstream of it with healthy
partitions is ours.

## Nightly reconciliation duties

Before Finance opens the period, verify in this order:

1. Run identity: all three scheduler runs succeeded for the same execution
   date, and the maximum raw `source_date` matches that execution date.
2. Grain: `warehouse.fct_mrr_daily` holds exactly one row per account and
   metric date through D.
3. Credits: signed credit notes in `raw.invoice_webhooks` reconcile to
   `proration_credit_cents` in the open close window.
4. Deferred revenue: `deferred_revenue_cents` moves as cumulative net
   billed less cumulative recognized; a jump without matching billing is a
   defect, not a convention.
5. Close status: periods before the latest materialized period read
   `closed` and the latest reads `open`. Any reopened period appears in
   `warehouse.factory_corrections` with controller sign-off and a ticket;
   an unregistered change to a closed period is withheld from downstream
   consumers until it is registered.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`; quote timestamps from those logs when raising or
closing an incident, and link the ticket in `fixtures/workflow/issues.yaml`.

## Partner onboarding history

- The billing webhook feed was onboarded in 2023.
  It uses a land-then-rebuild contract. The platform owns the partitions
  until the ingest turns green. Replays are whole source days restored
  under their own `dt=` folders, never row-level patches.
- Signed amounts (charges positive, credits negative) were part of the
  feed contract from onboarding; service-period end dates are exclusive,
  and the posting date can legitimately lag a backdated service-period
  start.
- The ASC-606 close ledger was onboarded in 2024 when Finance moved the
  close off the legacy extract; from that cutover, corrections arrive only
  as source events or billing webhooks, never as warehouse edits.

## Legacy and provisional rules

Two retirement rules apply here, and they are not interchangeable.

(a) Closed-window restatements are governance. Once a close period is
closed, its published snapshot changes only when
`warehouse.factory_corrections` carries a controller sign-off naming that
period and the tracker ticket authorising the restatement. A draft or
unsigned entry never reopens a closed period.

(b) Pre-cutover feed adjustments are engineering. The billing platform
extract once delivered gross, pre-split or pre-fee amounts. While it did,
the fold layer applied temporary bridge adjustments (pre-cutover split
multipliers and fee retention haircuts) so the warehouse saw net figures.
Those adjustments retire on log evidence, not on tickets. Once
`var/logs/ingest_daemon.log` records that the billing extract cutover is
active, the feed now delivers net `amount_cents` directly. Any leftover
multiplier or haircut in the fold script double-adjusts every affected row.
It must be removed so warehouse facts match the landed net cents. Before
changing a rate, check `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log` for the delivery; afterwards, reconcile the
affected day against the raw landed rows.

Treating a closed-period restatement as an engineering fix is a governance
breach; treating a dead feed bridge as governance is how double-counted and
double-discounted amounts survive in the close ledger.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to the billing platform when the seal is late or a partition is
short; escalate internally when our ingest, fold or close broke on healthy
partitions. Post every SLA breach (including ones the next night's rebuild
silently heals) with the log timestamps that bracket it.
