# ADR 0002 — the workbook is the source of record, and raw mirrors it exactly

Status: accepted (2025-10-14)
Supersedes: ADR 0001 (never written up; the decision it recorded was "load the
GL export and have finance reconcile", and it was abandoned after two months)

## Context

There is no upstream system for the manual adjustments. They exist only in the
MANUAL_ADJ sheet of each month's workbook, keyed by the close team, and the
controller signs the workbook. That makes the workbook the source of record
whether or not anyone likes it.

Once that is true, the tempting shortcut — load the GL export from the ERP and
add the adjustments on top — produces a warehouse that no signed document
supports.

## Decision

1. `raw.gl_lines` and `raw.manual_adj` are a faithful mirror of the workbooks
   and nothing else. Every column arrives as a string; typing happens in
   staging.
2. The import is a full re-read of every workbook on every run. There is no
   incremental path, because "changed" is not a thing a spreadsheet tells you.
3. Nothing writes to `raw` except `loader/import_close.py`. If the import is
   red, the fix is in the workbook.
4. `adj_total` is reported beside `net_amount` in `fct_gl_monthly`, never
   folded into it. An adjustment must stay visible as an adjustment.
5. The xlsx parse uses the standard library only. The close laptop cannot
   install packages, and a pipeline that runs everywhere except the machine
   that matters is not a pipeline.

## Consequences

- Re-reading closed periods means a prior period can change in the warehouse
  without any run being red. The discipline in `docs/signoff_checklist.md`
  ("a prior period is never edited") is the only thing preventing it, and that
  discipline is not enforced by anything. This is the known weak point of this
  decision and has been raised twice.
- The import is O(all workbooks) every night. At the current volume that is
  seconds; it will not stay that way forever.
