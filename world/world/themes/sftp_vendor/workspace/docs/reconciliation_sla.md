# Reconciliation SLA

The nightly vendor cycle is held to the service levels in this document. The
reconciliations below prove those levels. The history below explains why the
rules take their current form. All times are UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| both vendor deliveries landed for data day D | 05:00 on D+1 | `data/drop/acme/ORDERS_<D>.done` and `data/drop/globex/RETURNS_<D>.done` both present |
| `sftp_poll` green | 05:45 | job green in `sched_state` |
| connector syncs recorded | 06:00 | `acme_orders` (05:31) and `globex_returns` (05:32) entries in the sync trail |
| `vendor_load` complete | 06:15 | job green, `raw.*` carries D |
| `dbt_build` complete, tests green | 06:30 | job green, `warehouse.fct_orders_daily` carries D |
| dashboard `vendor_daily` current | 07:00 | spot check `net_revenue` for D |

A dropped connection is routine. The poll retries after seven minutes. The
05:45 allowance exists for exactly that case. A breach of the 05:00 landing
SLA is for the vendor to explain. A downstream breach with both sentinels
present is ours to explain.

## Nightly reconciliation duties

Before the business day opens, verify in this order:

1. Sentinel parity: compare the poll count against the drop folders. A CSV
   without a `.done` file is a delivery in flight. Never load it by hand
   and never delete it. The loader skips it by design.
2. Row counts: compare `raw.vendor_orders` for D against the ORDERS file
   line count. Compare `raw.vendor_returns` for D against the RETURNS file.
   Check that `_source_file` names this night's files, not last night's.
3. Redelivery check: a resent file under the same filename is a full
   replacement. The default `overwrite` mode handles that case. A resent
   file under a new name loads alongside the original. The day then doubles.
   Confirm with the vendor which case applies before the load picks it up.
4. Corrections: any reopened business day appears in
   `warehouse.factory_corrections`. It appears with a sign-off and a ticket.
   An unregistered change to a closed day stays withheld from downstream
   consumers. It stays withheld until it is registered.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`. Quote timestamps from those logs when raising or
closing an incident. Link the ticket in `fixtures/workflow/issues.yaml`.

## Partner onboarding history

- ACME orders was onboarded in 2023. It was the first feed moved onto the
  edge box. The move followed retirement of the per-vendor cron on the
  analytics VM. The full-file redelivery convention was agreed then. That
  convention uses the same filename and a fresh sentinel. The loader
  `overwrite` default exists because of that convention.
- Globex returns was onboarded in 2024. The integrations review had made a
  sentinel a condition of onboarding. We had been burned by half-transferred
  files twice by then. Both cases involved a vendor without a sentinel. A
  vendor that will not write a sentinel is told no.
- The firewall egress change is per-host with about a fortnight's lead
  time. It is always the item that holds an onboarding up. Request it
  first. Request it before the vault entry at `vendor/<name>/sftp`. Request
  it before `config/vendors.yml`.

## Legacy and provisional rules

Two kinds of leftover logic live in this pipeline. They retire under two
different rules. Confusing them can reopen days. Confusing them can also
double-count amounts.

Closed-window restatements are governance. Once a data day is closed, its
published snapshot changes only when `warehouse.factory_corrections`
carries a controller sign-off naming that day and the tracker ticket
authorising the restatement, linked in `fixtures/workflow/issues.yaml`. A
draft or unsigned entry never reopens a closed day. An unregistered change
stays withheld from downstream consumers.

Pre-cutover feed adjustments are engineering. While a vendor feed delivered
gross, pre-split, or pre-fee amounts, the fold layer applied temporary
bridge adjustments. Those adjustments were vendor split multipliers and fee
retention haircuts. They made the warehouse see net figures. Those
adjustments retire on log evidence, not on tickets. Retire them once
`var/logs/ingest_daemon.log` records the partner extract cutover as active.
At that point, the feed delivers net `amount_cents` directly. Any
multiplier or haircut left in the fold script then double-adjusts every
affected row. Remove it so `warehouse.fct_orders_daily` matches the landed
net cents. Before changing a rate, check `var/logs/ingest_daemon.log` for
the cutover record. Also check `var/logs/sftp_sync.log` for the delivery.
Afterwards, reconcile the affected day against the raw landed rows for that
night's files.

Everything else provisional is still live configuration. That group includes
the sentinel requirement and the `overwrite` redelivery mode. It also
includes the ten-minute gap from poll to load and window offsets. Changing
a rate or rule because a comment calls it "legacy" has burned this team
before. Never disable the sentinel check on the strength of a comment alone.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to the vendor when the delivery itself is late or short. Escalate
internally when our poll, load, or build broke on a healthy delivery. Post
every SLA breach in the incidents channel. Include breaches that the next
night's load silently heals. Give the log timestamps that bracket each
breach.
