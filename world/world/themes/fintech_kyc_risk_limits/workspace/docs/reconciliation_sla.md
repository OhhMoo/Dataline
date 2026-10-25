# Reconciliation SLA

This document sets the service levels for the nightly risk cycle. It also
lists the reconciliations that prove those levels. A short history explains
why the rules take their current form. All times are UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| risk feeds for business date D landed | 00:45 on D+1 | `data/landing/<feed>/dt=D/events.csv` present |
| `risk_feed_load` complete | 01:15 | job green in `sched_state` |
| `merchant_exposure_build` complete, tests green | 01:30 | job green, mart carries D |
| `reserve_handoff` complete | 01:45 | sign-off and handoff rows carry D |
| committee and treasury consumers current | 02:00 | spot check against `warehouse.fct_merchant_exposure` |

On a normal night, deliberate slack sits between the exposure build and the
handoff. A breach of the 00:45 landing SLA is for the acquiring platform to
explain. A downstream breach with healthy feeds is for this team to explain.

## Nightly reconciliation duties

Before the committee convenes, verify the items below in this order:

1. Row counts: each `raw` feed for D against the platform's nightly manifest
   (posted in the ops channel), and `warehouse.fct_merchant_exposure` for D
   against `raw.merchant_volume`.
2. Dispute lifecycle parity: represented disputes finish won or lost, and
   representment lags land within the configured window. The dbt tests
   enforce this. On a manual rerun, say so in the incident thread rather
   than assuming it held.
3. Release governance: every reserve release in
   `warehouse.treasury_reserve_handoff` carries a risk officer approval and a
   chargeback ratio below the policy threshold; a payout request alone is not
   an authorisation and must not appear as one.
4. Corrections: any reopened business date appears in
   `warehouse.factory_corrections` with a risk officer sign-off and a ticket;
   an unregistered change to a closed date is withheld from downstream
   consumers until it is registered.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`. Quote timestamps from those logs when raising or
closing an incident. Link the ticket in `fixtures/workflow/issues.yaml`.

## Partner onboarding history

- The acquiring platform's dispute and volume feeds were onboarded in 2023.
  They use a land-then-reload contract. The platform owns the partition until
  the load turns green. Resends are always full-file replacements under the
  same `dt=` folder, never deltas.
- The KYC review feed came on in early 2024 when review cadence moved to the
  configured interval; older partitions predate the current column layout and
  the loader's glob is deliberately permissive about them.
- The payout-holds feed was added later in 2024 alongside the treasury
  handoff, after the cutover review found hold status was being keyed by
  e-mail. Its contract is the same partition layout as the other feeds.
- A resent or duplicated events file is a known platform behaviour during
  their deploy windows. The reconciliation duty is to confirm the warehouse
  grain absorbed it (unique keys hold), not to delete landed files.

## Legacy and provisional rules

Legacy configuration in this pipeline falls under two retirement rules.
Confusing the two rules has burned this team before.

Closed-window restatements are governance. Once a business date is closed,
its published snapshot changes only in one case. `warehouse.factory_corrections`
must carry a risk officer sign-off naming that date. The tracker ticket in
`fixtures/workflow/issues.yaml` must authorise the restatement. A draft or
unsigned entry never reopens a closed date. That rule holds whatever a
comment or a rate table suggests. Chargeback warning and critical thresholds
live under this rule. So do base and elevated reserve basis points.
Coverage-day windows and hold-cycle offsets also live under this rule.

Pre-cutover feed adjustments are engineering. While the acquiring platform
delivered gross amounts, those amounts were pre-split and pre-fee. The fold
layer then applied temporary bridge adjustments so the warehouse saw net
figures. Those adjustments included pre-cutover split multipliers and fee
retention haircuts. Those adjustments retire on log evidence, not on tickets.
This point matters once `var/logs/ingest_daemon.log` records the partner
extract cutover as active. At that point, the feed delivers net amount_cents
directly. Any leftover multiplier or haircut in the fold script then
double-adjusts every affected row. It must be removed so warehouse facts
match the landed net cents. Before changing such a rate or threshold, check
`var/logs/ingest_daemon.log` for the cutover line. Also check
`var/logs/sftp_sync.log` for the delivery. Afterwards, reconcile the affected
date against the raw landed rows.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to the acquiring platform when a feed itself is late or short.
Escalate internally when the load, exposure build, or reserve handoff broke
on healthy feeds. Post every SLA breach in the incidents channel. Include
breaches that the next night's reload silently heals. Add the log timestamps
that bracket each breach.
