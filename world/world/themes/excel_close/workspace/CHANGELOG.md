# Changelog

## 2026-02-09
- `run.sh workbooks` added after the January close was imported while two
  workbooks were open in Excel. It now refuses when a `~$` lock file exists.

## 2026-01-15
- Materiality threshold raised to 10000 for FY26 in `.envrc`.
  `config/close.yml` was not updated and the BI variance panel reads that
  file. Open.

## 2025-11-20
- `adj_total` split out of `net_amount` in `fct_gl_monthly` at the
  controller's request. Adjustments are now visible beside the number they
  adjust rather than inside it.

## 2025-10-14
- ADR 0002: the workbook is the source of record. Import rewritten as a full
  re-read of every workbook.

## 2025-09-30
- First close run through this pipeline. Previously the controller maintained
  the summary by hand in a separate workbook.
