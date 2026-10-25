# Reconciliation SLA

The service levels the nightly cycle is held to, the reconciliations that
prove them, and the history that explains why the rules are the way they
are. All times UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| order partition landed for business date D | 00:30 on D+1 | `data/landing/daily_revenue/dt=D/revenue.csv` present |
| `daily_load` complete | 01:30 | job green in `sched_state` |
| `dbt_build` complete, tests green | 02:00 | job green, `warehouse.fct_revenue` carries D |
| dashboard `revenue_daily` current | 07:00 | spot check against `warehouse.fct_revenue` |

The partition lands at 00:00. The load starts at 01:00, and the build
follows five minutes after. On a normal night the mart carries D well
before 02:00. The rest of the window is slack for a rerun. A breach of
the 00:30 landing SLA is the platform's to explain; a breach of anything
downstream of it with a healthy partition is ours.

## Nightly reconciliation duties

Before the business day opens, verify in this order:

1. Row counts: `raw.daily_revenue` for D versus the partition on disk. The
   load is a full reload, so the table for D must equal the CSV for D,
   line for line.
2. Mart parity: `warehouse.fct_revenue` for D versus a hand-sum of
   `staging.stg_revenue` for the day (orders counted, revenue summed in
   decimal). The dbt tests enforce the grain, but on a manual rerun say so
   in the incident thread rather than assuming they held.
3. Corrections: any reopened business day appears in
   `warehouse.factory_corrections` with a sign-off and a ticket; an
   unregistered change to a closed day is withheld from downstream
   consumers until it is registered.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`; quote timestamps from those logs when raising or
closing an incident, and link the ticket in
`fixtures/workflow/issues.yaml`.

## Partner onboarding history

- The order platform's daily extract was onboarded in 2023.
  It uses a land-then-reload contract. The platform owns the partition
  until the load turns green. Resends are full-file replacements under
  the same `dt=` folder, never deltas.
- The partition layout (`dt=` folders, one `revenue.csv` per day) took
  its current shape in the 2024 cutover to the dbt mart; the loader's glob
  is deliberately permissive about anything older.
- A duplicated delivery is a known platform behaviour during their deploy
  windows. The reconciliation duty is to confirm the grain absorbed it
  (one row per `order_id` per `dt`), not to delete landed files.

## Legacy and provisional rules

Two kinds of leftover logic live in this pipeline, and they retire under
two different rules; confusing them is how days get reopened or rates get
double-counted.

Closed-window restatements are governance. Once a business day is closed,
its published snapshot changes only when `warehouse.factory_corrections`
carries a controller sign-off naming that day and the tracker ticket
authorising the restatement, linked in `fixtures/workflow/issues.yaml`. A
draft or unsigned entry never reopens a closed day, and an unregistered
change stays withheld from downstream consumers.

Pre-cutover feed adjustments are engineering. While the order platform's
extract delivered gross, pre-split, pre-fee amounts, the fold layer
applied temporary bridge adjustments (split multipliers and fee retention
haircuts) so the warehouse saw net figures. Those adjustments retire on
log evidence, not on tickets. Once `var/logs/ingest_daemon.log` records
that the partner extract cutover is active, the feed now delivers net
`amount_cents` directly. Any multiplier or haircut left in the fold script
double-adjusts every affected row. It must be removed so
`warehouse.fct_revenue` matches the landed net cents. Before changing a
rate, check `var/logs/ingest_daemon.log` (and `var/logs/sftp_sync.log`
for the delivery) for the cutover record; afterwards, reconcile the
affected day against the raw landed rows for that partition.

Everything else provisional is still live configuration. That covers
revenue rates, the five-minute gap between load and build, and window
offsets. Changing a rate or rule because a comment calls it "legacy" has
burned this team before.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to the platform when the extract itself is late or short;
escalate internally when our load or build broke on a healthy partition.
Post every SLA breach (including ones the next night's reload silently
heals) in the incidents channel with the log timestamps that bracket it.
