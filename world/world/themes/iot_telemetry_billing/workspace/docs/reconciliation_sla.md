# Reconciliation SLA

This document sets the service levels for the nightly metering and close
cycle. It also lists the reconciliations that prove those levels. A short
history explains why the rules take their current form. All times are UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| gateway flush for business date D landed | 00:45 on D+1 | `data/landing/<entity>/dt=D/` present |
| `load_usage` complete | 01:15 | job green in `sched_state` |
| `finalize_billing` complete, tests green | 01:45 | job green, fact carries D |
| `invoice_run_handoff` current for locked periods | 02:00 | spot check against `warehouse.billing_period_lock` |

On a normal night, deliberate slack sits between the load and the
finalisation. A breach of the 00:45 landing SLA is for the collector
platform to explain. A downstream breach with a healthy flush is for this
team to explain.

## Nightly reconciliation duties

Before Billing Operations opens the day, verify the items below in this
order:

1. Row counts: new edge rollups by `rollup_id` and `window_start`, not by
   arrival partition alone, and `warehouse.fct_billable_usage` carrying the
   latest business day.
2. Counter integrity: no negative `reported_units` or `billable_units`; a
   lower counter after reboot is a reset row and contributes its new reading,
   never a forced zero. Investigate failures from the raw heartbeat sequence.
3. Close parity: during close, compare `invoice_run_handoff` with the locked
   rows in `billing_period_lock` and review `late_units_excluded`; a flush
   crossing a lock cutoff leaves the locked invoice unchanged.
4. Corrections: any reopened billing period appears in
   `warehouse.factory_corrections` with a Billing Operations sign-off and a
   ticket; an unregistered change to a locked period is withheld from
   downstream consumers until it is registered.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`. Quote timestamps from those logs when raising or
closing an incident. Link the ticket in `fixtures/workflow/issues.yaml`.

## Partner onboarding history

- The direct-device heartbeat feed was onboarded in 2023.
  It uses a land-then-reload contract. The collector owns the partition
  until the load turns green. Resends are always full-file replacements
  under the same `dt=` folder, never deltas.
- The store-and-forward gateway estate came on in late 2023 after a field
  trial proved multi-day flushes; the full-reload loader was chosen
  specifically so older usage-day partitions are absorbed without manual
  deduplication. Never copy a rollup into the arrival-day partition. Never
  subtract its units from a counter feed. Direct and edge devices are
  disjoint by contract.
- The contract commits feed gained its grace-day column in the 2024 billing
  cutover; older partitions keep their original layout and the loader's glob
  is deliberately permissive about them.
- A managed-service vendor's reseller estate was folded in during 2024 under
  the same partition contract; its commercial splits are settled downstream
  of the handoff, so no vendor-specific loader branch exists.

## Legacy and provisional rules

Legacy configuration in this pipeline falls under two retirement rules.
Confusing the two rules has burned this team before.

Closed-window restatements are governance. Once a billing period is locked,
its published snapshot changes only in one case. `warehouse.factory_corrections`
must carry a Billing Operations sign-off naming that period. The tracker
ticket in `fixtures/workflow/issues.yaml` must authorise the restatement. A
draft or unsigned entry never reopens a locked period. That rule holds
whatever a comment or a rate table suggests. Monthly minimum treatments live
under this rule. So do overage block rounding and grace-day and cutoff
offsets. Reset handling also lives under this rule.

Pre-cutover feed adjustments are engineering. While the collector platform's
extract delivered gross amounts, those amounts were pre-split and pre-fee.
The fold layer then applied temporary bridge adjustments so the warehouse
saw net figures. Those adjustments included pre-cutover split multipliers
and fee retention haircuts. Those adjustments retire on log evidence, not on
tickets. This point matters once `var/logs/ingest_daemon.log` records the
partner extract cutover as active. At that point, the feed delivers net
amount_cents directly. Any leftover multiplier or haircut in the fold script
then double-adjusts every affected row. It must be removed so warehouse
facts match the landed net cents. Before changing such a rate, check
`var/logs/ingest_daemon.log` for the cutover line. Also check
`var/logs/sftp_sync.log` for the delivery. Afterwards, reconcile the affected
usage day against the raw landed rows.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to the collector platform when the flush itself is late or short.
Escalate internally when the load or finalisation broke on a healthy flush.
Post every SLA breach in the incidents channel. Include breaches that the
next night's reload silently heals. Add the log timestamps that bracket each
breach.
