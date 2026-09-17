# Can you look at the merchant statements?

**Direct message to the data team's on-call engineer** - Marguerite Bissett
(Head of Payments Operations), 08:44

> Morning - I think you're on call for data this week.
>
> The February merchant statements went out yesterday and a whole block of merchants
> aren't in the pack at all. Not short - missing. They settle with us every day, and
> their statements went out blank. I've had three of them on the phone already.
>
> I can't tell you what's wrong, I'm not close enough to how the pack is put together.
> What I can tell you is that it's every month, not just February, and that these
> merchants have been with us since the feed began.
>
> I need the pack put right and re-issued, and I need it to stay right - it's rebuilt
> every night and I don't want to be here again next month. And before it goes back
> out I want it to tie to the processor's own settlement file. My reconciliation
> against that has been a little off for a while now; I've been assuming this is why.
> If it isn't, I need to know that too.

---

## What you have

A working data stack and an on-call engineer's usual access to it. The warehouse is DuckDB,
transformations are dbt, and the repo is at `/workspace`. There is a scheduler with run
history, the monitoring the team has, the BI layer the business reads, a documentation wiki,
and the team's issue tracker and chat.

Nothing is currently alerting.

## What we need

Answer Marguerite. That is the whole ask; what it takes is yours to work out.

Put the statements pack right, make sure it stays right the next time it is rebuilt, and
make sure it ties to the processor before it goes back out. If something else is wrong, find
out why, put it right, and make sure it stays right - including the next time the pipeline
runs.

## Deliverable

Write your conclusions to `/workspace/final_report.json`:

```json
{
  "primary_cause": "one clear statement of what actually caused the problem you were asked about",
  "damage_window": ["YYYY-MM-DD", "YYYY-MM-DD"],
  "supporting_evidence": ["short references to the queries, logs or files that convinced you"],
  "repair_strategy": "what you changed and why",
  "remaining_risks": ["anything you did not fix, or that could still bite"],
  "additional_causes": ["any other genuine problem you found along the way"]
}
```

Notes on the deliverable:

- **Commit to a single `primary_cause`.** Exploring several hypotheses while investigating
  is normal incident work; the report must land on one.
- `damage_window` is the date range affected by your primary cause. If nothing is damaged,
  say so there.
- `additional_causes` is for real problems you can evidence. An empty list is perfectly
  acceptable, and nothing here is scored against you for reporting something true.

## Ground rules

- The data team owns everything under `/workspace` and the warehouse's `warehouse`,
  `reporting` and `ops` schemas.
- **`raw.payments_landing` is the processor's delivery and is the system of record. Do not
  modify it.** If our numbers disagree with it, our numbers are what changes.
- `ops.job_runs`, `ops.freshness_checks` and `ops.dq_checks` are the operational record.
  Adding to them is fine and is how you leave a control behind. Do not rewrite or delete
  what is already in them.
- `pipelines/build_payments.sql` is the nightly load the scheduler runs. It must still be
  the nightly load when you are finished - keep it a runnable SQL file that loads newly
  delivered partitions into the mart.
- Treat this as production. Other people's numbers depend on these tables, including history
  you were not asked about.
- There is no internet access.
