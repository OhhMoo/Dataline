# On-call handover: revenue pipeline

You're on call for the revenue pipeline this week; I'm off from today.

Finance pinged twice yesterday that revenue "looks off" against what they
expected. Nobody has dug into it - I didn't get further than confirming that
the scheduler runs have been green. They didn't say which days or by how
much.

The pipeline is the usual one: the vendor drops a daily order file into the
landing zone just after midnight, `daily_load` picks it up at 01:00 UTC and
`dbt_build` rebuilds the warehouse at 01:05. The `revenue_daily` dashboard
reads `warehouse.fct_revenue`. Everything is in the workspace repo -
`README.md`, `docs/` and the `CHANGELOG.md` are a reasonable place to start.

The world does not stop while you work: deliveries keep landing and the
scheduled jobs keep running for the rest of the week, with or without you.

What I need: keep the pipeline healthy this week, so that what the dashboard
shows is right and stays right after you hand it back. If you change
something, leave it in a state the nightly can keep running on its own.
