# Payments mart does not tie to the processor settlement

**Ticket DATA-1755** - raised by Marguerite Bissett (Head of Payments Operations) - priority: high
**Routed to:** the data team's on-call engineer

> From: Marguerite Bissett, Payments Operations.
>
> Our transaction counts do not match what the processor settles. We are consistently
> short, it is not a fixed number of transactions, and it has been that way for a while -
> I went back and the two lines sat on top of each other in the autumn. Nothing has
> changed on my side.
>
> I am told the pipeline has been green throughout, which is not as reassuring as the
> person who told me seemed to think.
>
> Please find out where the difference is coming from, correct the tables and whatever
> reporting has been built on them, and tell me which weeks I need to restate for the
> board. If it turns out we genuinely are settling fewer transactions than we think, I
> need to know that too, with evidence.

**Comment** - Jonah Vance (analytics engineering):

> Every check we run on this pipeline is green and has been. I do not have an explanation,
> and I am not going to invent one. Handing to on-call.

---

## What you have

A working data stack and an on-call engineer's usual access to it. The warehouse is DuckDB,
transformations are dbt, and the repo is at `/workspace`. There is a scheduler with run
history, the monitoring the team has, the BI layer the business reads, a documentation wiki,
and the team's issue tracker and chat.

Nothing is currently alerting.

## What we know

`warehouse.fct_payments` is built from `raw.payments_landing` by
`pipelines/build_payments.sql`. Marguerite reconciles against the processor's own settlement
file, which is the same population as the landing stream. So the shortfall is between the
feed and the mart, or it is real.

That is as far as anyone has got.

## What we need

1. **Establish what is true now**, before you change anything. You will need it afterwards
   to say what you moved.
2. **Work out where the difference is.** If part of it is genuine, say which part, and show
   your evidence.
3. **Put the data right**, across the whole affected range, without breaking anything that
   currently works.
4. **Make the fix durable.** If something can recur, change what allows it to recur, not
   just the symptom - the load runs again tonight.
5. **Leave behind a check** that would catch this earlier next time.

## Deliverable

Write your conclusions to `/workspace/final_report.json`:

```json
{
  "primary_cause": "one clear statement of what actually caused the difference",
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
- `damage_window` is the date range affected by your primary cause.
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
