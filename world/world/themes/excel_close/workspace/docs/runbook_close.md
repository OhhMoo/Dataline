# Runbook: monthly close

Calendar (all times UTC):

- Day 1 08:00 — scheduler job `close_import` runs `loader/import_close.py`
  over `finance/workbooks/`, refreshing `raw.gl_lines` and `raw.manual_adj`
  from the CLOSE_YYYYMM workbooks.
- Day 1 08:10 — `dbt_build` rebuilds staging and `warehouse.fct_gl_monthly`.
- Day 1 EOD — the controller reviews dashboard `close_summary` against the
  workbook totals before signing off the close.

Workbook ownership: one workbook per period, maintained by the GL team; the
controller is the final owner. Old periods are never edited after sign-off —
a late correction goes into the current period's workbook as a new line.

MANUAL_ADJ discipline: manual adjustments are entered only in the workbook's
MANUAL_ADJ sheet — never directly in the warehouse, never in a side file.
Every adjustment needs an owner and a note; the sheet is the audit trail.

If `close_import` is red in `sched_state`, do not hand-load tables: fix the
workbook and re-run the import, so `raw` always matches the workbook exactly.
