# Reconciliation SLA

The service levels the nightly cycle is held to, the reconciliations that
prove them, and the history that explains why the rules are the way they are.
All times UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| shop extract landed for business date D | 02:00 on D+1 | `data/landing/<entity>/dt=D/` present |
| `daily_load` complete | 03:00 | job green in `sched_state` |
| `dbt_build` complete, tests green | 04:00 | job green, marts carry D |
| `bi_refresh` complete | 06:30 | refresh watermark in `raw.bi_refresh_history` |
| executive dashboards current | 07:00 | spot check against `warehouse.fct_orders` |

There are three hours of slack between the build and the refresh on a normal
night. A breach of the 02:00 landing SLA is the platform's to explain. A
downstream breach with a healthy extract is ours.

## Nightly reconciliation duties

Before the business day opens, verify in this order:

1. Row counts: compare `raw.orders` for D with the platform's nightly manifest
   (posted in the ops channel). Compare `warehouse.fct_orders` with
   `raw.orders` for D.
2. Order/line parity: every order's amount equals the sum of its lines. The
   dbt reconciliation test enforces this. On a manual rerun, say so in
   the incident thread. Do not assume it held.
3. Refresh watermark: `raw.bi_refresh_history`'s newest entry for each
   executive dashboard names a data watermark at or beyond D.
4. Corrections: any reopened business day appears in
   `warehouse.factory_corrections` with a controller sign-off and a ticket.
   An unregistered change to a closed day is withheld from downstream
   consumers. It stays withheld until it is registered.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`. Quote timestamps from those logs when raising or
closing an incident. Link the ticket in `fixtures/workflow/issues.yaml`.

## Partner onboarding history

- The shop platform's CDC extract was onboarded in 2023 with a
  land-then-reload contract. The platform owns the partition until the load
  turns green. Resends are always full-file replacements under the same
  `dt=` folder, never deltas.
- The order_lines feed gained its current file naming in the 2024 cutover.
  Older partitions keep their original names. The loader's glob is
  deliberately permissive about them.
- A resent or duplicated file is a known platform behaviour during their
  deploy windows. The reconciliation duty is to confirm the warehouse grain
  absorbed it (unique keys hold), not to delete landed files.

## Legacy and provisional rules

Legacy configuration in this pipeline falls under two different retirement
rules, and confusing them has burned this team before.

Closed-window restatements are governance. Once a business day is closed,
its published snapshot changes only under a named entry in
`warehouse.factory_corrections`. That entry must carry a controller sign-off
naming that day. It must also name the tracker ticket in
`fixtures/workflow/issues.yaml` authorising the restatement. A draft or
unsigned entry never reopens a closed day, whatever a comment or a rate
table suggests. Standing split conventions, fee treatments, and window
offsets that are live business policy all live under this rule.

Pre-cutover feed adjustments are engineering. While the shop platform's
extract delivered gross, pre-split, pre-fee amounts, the fold layer applied
temporary bridge adjustments. Those adjustments included pre-cutover split
multipliers and fee retention haircuts. They let the warehouse see net
figures. Those adjustments retire on log evidence, not on tickets. Once
`var/logs/ingest_daemon.log` records that the partner extract cutover is
active, the feed delivers net amount_cents directly. Any leftover multiplier
or haircut in the fold script double-adjusts every affected row. It must be
removed so warehouse facts match the landed net cents. Before changing such
a rate, check `var/logs/ingest_daemon.log` for the cutover line. Check
`var/logs/sftp_sync.log` for the delivery. Afterwards, reconcile the affected
day against the raw landed rows.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to the platform when the extract itself is late or short. Escalate
internally when our load, build, or refresh broke on a healthy extract.
Post every SLA breach, including ones the next night's reload silently
heals. Post it in #data-incidents with the log timestamps that bracket it.
