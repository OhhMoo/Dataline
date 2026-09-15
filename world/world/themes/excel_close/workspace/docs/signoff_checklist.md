# Close sign-off checklist

Worked through by the preparer, then by the controller. Nothing in this list
is automated and nothing on it should be: the point of the list is that a
person looked.

## Preparer (GL team)

1. Every workbook for the period is closed in Excel. `./run.sh workbooks`
   reports open workbooks; if it reports any, stop.
2. `close_import` is green in `sched_state` for the period. If it is red,
   fix the workbook and let the next scheduled import pick it up. **Do not
   hand-load `raw`** — `raw` must always be exactly what the workbook says.
3. Every row in MANUAL_ADJ has an owner and a note. The sheet is the audit
   trail; an adjustment without a note is not reviewable.
4. `warehouse.fct_gl_monthly` totals for the period tie to the workbook
   totals, account by account.

## Controller

5. Accounts on `config/close.yml` `review.always_review_accounts` reviewed
   line by line, regardless of variance.
6. Every variance above the materiality threshold explained in writing.

   **The threshold is inconsistent.** `config/close.yml` says 5000 and
   `.envrc` says 10000. The BI variance panel reads
   `config/close.yml`. Use 5000 until finance confirms which is right; the
   lower number cannot be wrong in the direction that matters.

7. Sign-off recorded in the workbook. Not here, not in a ticket, not in a
   chat message.

## After sign-off

A prior period is never edited. A late correction goes into the current
period's workbook as a new line, with a note naming the period it relates to.
The nightly import re-reads every workbook, including closed ones, so a prior
period that changes will change in the warehouse silently — which is exactly
why the rule exists.
