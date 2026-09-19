# The board pack: `finance.board_pack_daily` and closed reporting periods

Owner: Finance Systems (Hannah Vogt, `hvogt`). Ticket for anything touching
published figures: the incident ticket the work is done under, currently
DATA-5533. Channel: `#finance-systems`.

## What the job does

`board_pack_publish` runs at 06:00 every day (`pipelines/board_pack_publish.sql`)
and republishes `finance.board_pack_daily` — one row per region per day — from
the revenue mart (`warehouse.mart_revenue_fact` joined to
`warehouse.mart_revenue_dim`) for every reporting month that is **open** in
`finance.reporting_periods`. Closed months are not touched by the job.

## Open and closed months

| period_month | status | closed_at (pack run)   | pack issued |
|--------------|--------|------------------------|-------------|
| 2025-11      | closed | 2025-12-01 06:00       | 2025-12-01  |
| 2025-12      | closed | 2026-01-02 06:00       | 2026-01-02  |
| 2026-01      | closed | 2026-02-02 06:00       | 2026-02-02  |
| 2026-02      | closed | 2026-03-02 06:00       | 2026-03-02  |
| 2026-03      | open   |                        |             |

A month closes when its pack goes to the board, normally the first working day
of the following month.

## Restating a closed month

A closed month stays exactly as the board received it until the Finance Systems
lead has put a written restatement decision on the ticket. Writing
`board_pack_daily` directly, re-running the pack job over a closed month, or
reopening a period in `reporting_periods` are all restatements and all need
that decision first. A request for one states the impact per month.

The snapshot record (`warehouse.rev_snapshot`) is the data team's: a tick that
is wrong on its face is corrected in place and the correction noted on the
incident ticket. See `docs/runbook-revenue-mart.md` for the mart itself.
