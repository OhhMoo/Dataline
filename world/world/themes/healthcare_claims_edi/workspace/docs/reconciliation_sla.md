# Reconciliation SLA

This document sets the service levels for the nightly claims cycle. It also
lists the reconciliations that prove those levels. A short history explains
why the rules take their current form. All times are UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| EDI delivery for business date D landed | 00:45 on D+1 | `data/landing/<entity>/dt=D/` present |
| `nightly_edi_load` complete | 01:30 | job green in `sched_state` |
| `adjudication_build` complete, tests green | 01:45 | job green, marts carry D |
| `settlement_handoffs` complete | 02:00 | handoffs carry D's service dates |
| `cms_window_close` complete | 02:15 | window row published for the open month |
| downstream consumers current | 02:30 | spot check against `warehouse.fct_adjudicated_claims` |

On a normal night, deliberate slack sits between the adjudication build and
the window close. A breach of the 00:45 landing SLA is for the clearinghouse
to explain. A downstream breach with a healthy delivery is for this team to
explain.

## Nightly reconciliation duties

Before the business day opens, verify the items below in this order:

1. Row counts: `raw.edi_837_claims` versus `raw.edi_835_remits` by `dt`
   (every claim version needs a remit), and
   `warehouse.fct_adjudicated_claims` carrying one current row per `claim_id`.
2. Financial balance: billed equals payer paid plus member responsibility
   plus contractual and COB adjustments. The dbt tests enforce this. On a
   manual rerun, say so in the incident thread rather than assuming it held.
3. Handoff dates: the reserve handoff's data-through date and the CMS
   window's acceptance as-of date both bracket D before either output is
   distributed.
4. Corrections: any reopened service partition appears in
   `warehouse.factory_corrections` with a controller sign-off and a ticket;
   an unregistered change to a closed partition is withheld from downstream
   consumers until it is registered.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`. Quote timestamps from those logs when raising or
closing an incident. Link the ticket in `fixtures/workflow/issues.yaml`.

## Partner onboarding history

- The primary clearinghouse's 837/835 pair was onboarded in 2023.
  It uses a land-then-reload contract. The clearinghouse owns the partition
  until the load turns green. Corrected deliveries are always full-file
  replacements under the same `dt=` folder, never deltas. The loader's
  `CREATE OR REPLACE TABLE` absorbs them without row-level surgery.
- The provider roster feed gained its current capitation columns in the 2024
  cutover; older partitions keep their original layout and the loader's glob
  is deliberately permissive about them.
- A resent or duplicated transmission is a known clearinghouse behaviour
  during their deploy windows. The reconciliation duty is to confirm the
  warehouse grain absorbed it (latest-version precedence holds), not to
  delete landed files.
- A second trading-partner vendor came on during 2024 for behavioural-health
  claims; its splits are settled in the marts, so its files follow the same
  partition contract and no partner-specific loader branch exists.

## Legacy and provisional rules

Legacy configuration in this pipeline falls under two retirement rules.
Confusing the two rules has burned this team before.

Closed-window restatements are governance. Once a business day is closed,
its published snapshot changes only in one case. `warehouse.factory_corrections`
must carry a controller sign-off naming that day. The tracker ticket in
`fixtures/workflow/issues.yaml` must authorise the restatement. A draft or
unsigned entry never reopens a closed day. That rule holds whatever a
comment or a rate table suggests. Reserve basis-point rates live under this
rule. So do capitation withhold treatments and frequency-code precedence.
CMS cutoff offsets also live under this rule.

Pre-cutover feed adjustments are engineering. While the clearinghouse
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
day against the raw landed rows.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to the clearinghouse when the delivery itself is late or short.
Escalate internally when the load, build, handoffs, or window close broke on
a healthy delivery. Post every SLA breach in the incidents channel. Include
breaches that the next night's reload silently heals. Add the log timestamps
that bracket each breach.
