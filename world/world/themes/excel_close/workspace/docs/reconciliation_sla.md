# Reconciliation SLA

The service levels the close cycle is held to, the reconciliations that
prove them, and the history that explains why the rules are the way they
are. All times UTC.

## SLA windows

| milestone | due | measured against |
|---|---|---|
| period workbook saved with day D's postings | 07:30 on D+1 | `finance/workbooks/CLOSE_YYYYMM.xlsx` rewritten; `./run.sh workbooks` reports nothing open |
| `close_import` complete | 08:30 | job green in `sched_state`, `raw` mirrors the workbooks |
| `dbt_build` complete, tests green | 08:45 | job green, `warehouse.fct_gl_monthly` carries D's postings |
| `close_summary` current for consumers | 09:30 | spot check against workbook totals |
| close-day controller review | end of Day 1 | sign-off recorded in the workbook |

The workbook is saved by 07:00. The import runs at 08:00, and the build
follows ten minutes after. The morning's slack exists for a fixed workbook
and a rerun. A breach of the 07:30 workbook SLA is the GL team's to
explain. A downstream breach with a healthy workbook is ours.

## Nightly reconciliation duties

Before the business day opens, verify in this order:

1. Workbook hygiene: `./run.sh workbooks` reports no open workbooks. An
   open workbook stops the close. If the import is red, fix the workbook.
   Let the next scheduled import pick it up. Never hand-load `raw`. `raw`
   must always be exactly what the workbook says.
2. Adjustment discipline: every row in MANUAL_ADJ has an owner and a note.
   The sheet is the audit trail. An adjustment without a note is not
   reviewable.
3. Tie-out: `warehouse.fct_gl_monthly` totals for the period tie to the
   workbook totals, account by account. Keep `adj_total` visible beside
   `net_amount` rather than folded into it.
4. Variances: every variance above the materiality threshold explained in
   writing. The threshold is inconsistent. `config/close.yml` says 5000
   and `.envrc` says 10000. The BI variance panel reads
   `config/close.yml`. Use 5000 until finance confirms which is right. The
   lower number cannot be wrong in the direction that matters.
5. Corrections: any reopened period appears in
   `warehouse.factory_corrections` with a controller sign-off and a
   ticket. An unregistered change to a closed period is withheld from
   downstream consumers. It stays withheld until it is registered.

Evidence for every step lives in `var/logs/ingest_daemon.log` and
`var/logs/sftp_sync.log`. Quote timestamps from those logs when raising or
closing an incident. Link the ticket in `fixtures/workflow/issues.yaml`.

## Partner onboarding history

- The workbook convention was onboarded in 2023. That followed the ERP GL
  export plus finance reconciliation. That approach was abandoned inside
  two months. There is no upstream system for the manual adjustments. The
  workbook therefore became the source of record, whether anyone liked it
  or not.
- The 2024 close-tooling review standardised how downstream consumers
  read. Consolidation reporting takes `raw.gl_lines` as-landed. External
  audit takes `raw.manual_adj` as the only adjustment trail. Entity and
  intercompany splits are computed in the mart. They are not keyed into
  the workbook as plug lines.
- Old periods are never edited after sign-off. A late correction goes into
  the current period's workbook as a new line. Its note names the period
  it relates to. The convention dates from the 2024 review. It exists
  because the import re-reads every workbook every night.

## Legacy and provisional rules

Two kinds of leftover logic live in this pipeline. They retire under two
different rules. Confusing them is how periods get reopened or amounts
get double-counted.

Closed-window restatements are governance. Once a period is closed, its
published snapshot changes only under a named entry in
`warehouse.factory_corrections`. That entry must carry a controller sign-off
naming that period. It must name the tracker ticket authorising the
restatement, linked in `fixtures/workflow/issues.yaml`. A ticket in
`waiting_on_finance` is a queue position, not an approval. A draft or
unsigned entry never reopens a closed period. An unregistered change stays
withheld from downstream consumers.

Pre-cutover feed adjustments are engineering. While the upstream postings
extract delivered gross, pre-split, pre-fee amounts, the fold layer applied
temporary bridge adjustments. Those adjustments included entity split
multipliers and fee retention haircuts. They let the warehouse see net
figures. Those adjustments retire on log evidence, not on tickets. Once
`var/logs/ingest_daemon.log` records that the partner extract cutover is
active, the feed delivers net `amount_cents` directly. Any multiplier or
haircut left in the fold script double-adjusts every affected row. It must
be removed so `warehouse.fct_gl_monthly` matches the landed net cents.
Before changing a rate, check `var/logs/ingest_daemon.log` for the cutover
record. Check `var/logs/sftp_sync.log` for the delivery. Afterwards,
reconcile the affected period against the raw landed rows.

Everything else provisional is still live configuration. That includes the
materiality threshold discrepancy and the ten-minute gap from import to
build. It also includes the staging casts. It includes the unenforced rule
that a signed-off period is never edited. Changing a threshold or rule
because a comment calls it "provisional" has burned this team before.

## Escalation

First failure first: investigate the earliest red job, not the last.
Escalate to the GL team when the workbook itself is late, open, or
internally inconsistent. Escalate internally when our import or build
broke on a healthy workbook. Post every SLA breach, including ones the
next morning's import silently heals. Include the log timestamps that
bracket it. Never let a silent re-read of a closed period substitute for
a registered correction.
