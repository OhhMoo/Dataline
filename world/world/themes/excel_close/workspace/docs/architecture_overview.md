# Architecture overview

How a folder of close workbooks becomes the period number, and which pieces
are load-bearing when something disagrees.

## System at a glance

Excel is the integration layer. There is one workbook per period,
`finance/workbooks/CLOSE_YYYYMM.xlsx`. It is maintained by the GL team and
owned finally by the controller. Each posting day is saved into the month's
workbook. The workbook is rewritten from every day landed so far. It is in
place by 07:00 UTC. The cycle for posting day D runs on calendar day D+1,
UTC:

1. `close_import` (08:00) runs `loader/import_close.py`. It is a full
   re-read of every workbook. There is no incremental path, because
   "changed" is not a thing a spreadsheet tells you. It refreshes
   `raw.gl_lines` and `raw.manual_adj`. The parse uses the standard library
   only, because the close laptop cannot install packages.
2. `dbt_build` (08:10, ten minutes after the import) rebuilds staging and
   `warehouse.fct_gl_monthly` and runs the data tests.

The dashboard `close_summary` reads the mart. `raw.gl_lines` is consumed by
consolidation reporting. `raw.manual_adj` is consumed by external audit. The
deployed job order is recorded in `orchestration/schedule.yml`. Treat that
file, not memory, as the schedule of record. On Day 1, the same morning runs
feed the controller's end-of-day review. That review is against the workbook
totals.

## DuckDB schema overview

One database file, two schemas that matter here.

`raw` is a faithful mirror of the workbooks and nothing else. Every column
arrives as a string. Typing happens in staging (ADR 0002):

| table | grain | source |
|---|---|---|
| `raw.gl_lines` | one row per GL_LINES sheet row per period workbook | `finance/workbooks/CLOSE_YYYYMM.xlsx` |
| `raw.manual_adj` | one row per MANUAL_ADJ sheet row per period workbook | `finance/workbooks/CLOSE_YYYYMM.xlsx` |

`raw.manual_adj` lives only in the workbook. There is no upstream system
for it. `warehouse` is the mart the dashboard reads:

| table | grain |
|---|---|
| `warehouse.fct_gl_monthly` | one row per `period` + `account` (`lines`, `net_amount`, `adj_total`) |

`staging.stg_gl_lines` types the GL rows (`amount` decimal(12,2),
`post_date` date; `line_id` stays varchar). `adj_total` is reported beside
`net_amount`, never folded into it. An adjustment must stay visible as an
adjustment.

## Change control

The workbook is the source of record, and `raw` mirrors it exactly.
Nothing writes to `raw` except `loader/import_close.py`. If the import is
red, the fix is in the workbook, never a hand-load. Before altering any
pipeline parameter, confirm the live state rather than the intention.
Those parameters include the 08:00 import and the ten-minute gap. They
also include the staging casts and the materiality threshold the variance
panel reads. They include any rate or split convention encoded in job code:

- active tickets in `fixtures/workflow/issues.yaml` (a ticket in
  `waiting_on_finance` is a queue position, not an approval),
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`,
- the corrections register `warehouse.factory_corrections`, which records
  which periods were reopened, by whom, and under which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

The close ran briefly on a GL export from the ERP, with finance reconciling
on top. That decision was abandoned inside two months in 2023. It produced
a warehouse that no signed document supports. ADR 0002 settled the workbook
as the source of record. The 2024 close-tooling review followed. It
standardised the conventions still load-bearing today:

- Entity and intercompany splits are computed in the mart from
  `net_amount`. They are never keyed into the workbook as plug lines. They
  are never netted into raw GL rows. A deviation between
  `warehouse.fct_gl_monthly` and the workbook totals is evidence to
  reconcile, not to copy forward.
- Manual adjustments are entered only in the MANUAL_ADJ sheet. They are
  never entered directly in the warehouse. They are never entered in a
  side file. Any job-code path that synthesises adjustments is a legacy
  placeholder. It stays so until a ticket and the logs confirm it was
  retired. Do not delete or "fix" such logic on the strength of a comment
  alone. Check `fixtures/workflow/issues.yaml` and the audit lines in
  `var/logs/` first.
- The nightly import re-reads every workbook, including closed ones. A
  prior period that changes will therefore change in the warehouse
  silently. The discipline that a signed-off period is never edited is the
  only thing preventing it. That discipline is not enforced by anything.
  It is the known weak point of ADR 0002, raised twice.

Retirement itself follows two rules, not one. A closed period waits for a
controller sign-off naming that period in
`warehouse.factory_corrections`. Pre-cutover entity split multipliers or
fee retention haircuts in the fold layer retire on log evidence. That
evidence is in `var/logs/ingest_daemon.log`, not a ticket. The log must
record the partner extract cutover delivering net `amount_cents` directly.
Leaving them in past that point double-adjusts every affected row.

Historical review notes live with the decision records under `docs/adr/`.
When a discrepancy smells like a leftover, read those before touching the
pipeline.
