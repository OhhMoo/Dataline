# Board pack calendar

`board_pack_publish` runs at 06:00 every day (`pipelines/board_pack_publish.sql`)
and republishes `finance.board_pack_daily` — one row per region per day — from
the revenue mart (`warehouse.mart_revenue_fact` joined to
`warehouse.mart_revenue_dim`) for every reporting month that is **open** in
`finance.reporting_periods`.

## Months

| period_month | status | closed_at (pack run)   | pack issued |
|--------------|--------|------------------------|-------------|
| 2025-11      | closed | 2025-12-01 06:00       | 2025-12-01  |
| 2025-12      | closed | 2026-01-02 06:00       | 2026-01-02  |
| 2026-01      | closed | 2026-02-02 06:00       | 2026-02-02  |
| 2026-02      | closed | 2026-03-02 06:00       | 2026-03-02  |
| 2026-03      | open   |                        |             |

A month closes when its pack goes to the board, normally the first working day
of the following month.

This page is the calendar and the job. The contract for a month that has
already gone out is the Finance wiki page `finance/board-pack`. The mart
runbook is `docs/runbook-revenue-mart.md`.
