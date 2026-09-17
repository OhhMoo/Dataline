# Merchant statements pack is missing merchants

**Ticket DATA-1755** - raised by Marguerite Bissett (Head of Payments Operations) - priority: high
**Routed to:** the data team's on-call engineer

> From: Marguerite Bissett, Payments Operations.
>
> The February statements went out yesterday and a block of merchants are not in the pack
> at all. They are absent from the merchant monthly report for every month, and they settle
> with us every day. Their statements went out blank and I have had three of them on the
> phone this morning.
>
> Please find out why they are missing, put the pack right across every month it covers, and
> make sure it stays right - it is rebuilt every night and I do not want to be back here
> next month.
>
> Before it goes back out I want the statements to tie to the processor's own settlement
> file. My monthly reconciliation against it has been a little off for a while, and I have
> been assuming this is why.

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

The statements pack is `reporting.rpt_merchant_monthly`, a dbt model over
`warehouse.fct_payments`. `warehouse.fct_payments` is built from `raw.payments_landing` by
`pipelines/build_payments.sql`. Marguerite reconciles the statements against the processor's
own settlement file, which is the same population as the landing stream. So a statement that
does not tie is wrong between the feed and the pack, or it is genuinely right.

That is as far as anyone has got.

## What we need

1. **Establish what is true now**, before you change anything. You will need it afterwards
   to say what you moved.
2. **Work out why the merchants are missing**, and whether the pack ties to the feed once
   they are back. If part of any difference is genuine, say which part, and show your
   evidence.
3. **Put the data right**, across the whole affected range, without breaking anything that
   currently works.
4. **Make the fix durable.** If something can recur, change what allows it to recur, not
   just the symptom - the pack is rebuilt tonight and the load runs again tonight.
5. **Leave behind a check** that would catch this earlier next time.

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
