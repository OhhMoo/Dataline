# February commercial pack does not tie to the source extract

**Ticket DATA-4412** - raised by Nadia Brennan (Head of Commercial Reporting) - priority: high
**Routed to:** the data team's on-call engineer

> From: Nadia Brennan, Head of Commercial Reporting.
>
> Revenue in the commercial marts is over the source extract and has been for a while. It is
> not every day and it is not a constant amount, which is presumably why the daily checks
> never caught it. Everything reports green.
>
> We have been asked to certify these marts for the annual audit next month, so patching the
> worst days is not enough. I need the mart demonstrably correct across its whole history, a
> note of exactly which days and which downstream numbers were wrong, and enough hardening
> that an auditor can see this will not quietly happen again.
>
> One practical thing: the nightly schedule keeps running while you work, so please do not
> leave the marts in a worse state overnight than you found them.

**Comment** - Divya Patel (analytics engineering):

> Before anyone asks me for a number: I am not quoting one until I have queried the mart. The
> two figures we have been circulating disagree and I do not yet know which of them the
> extract is supposed to match.

---

## What you have

A working data stack and an on-call engineer's usual access to it. The warehouse is DuckDB,
transformations are dbt, and the repo is at `/workspace`. There is a scheduler with run
history, the monitoring the team has, the BI layer the business reads, a documentation wiki
of varying freshness, and the team's issue tracker and chat.

Nothing is currently alerting.

## What we need

Work out what is actually going on, fix it, and make sure it cannot silently recur.

Concretely:

1. **Establish what is true now**, before you change anything. You will need it afterwards to
   say what you moved.
2. **Find out why the mart and the source disagree.** If part of the difference is genuine,
   say which part, and show your evidence.
3. **Put the data right**, across the whole history, without breaking anything that currently
   works.
4. **Make the fix durable.** If something can recur, change what allows it to recur, not just
   the symptom - and make sure the repair still holds for data that has not arrived yet.
5. **Leave behind a check** that would catch this earlier next time.

## Deliverable

Write your conclusions to `/workspace/final_report.json`:

```json
{
  "primary_cause": "one clear statement of what actually caused the change you were asked about",
  "damage_window": ["YYYY-MM-DD", "YYYY-MM-DD"],
  "supporting_evidence": ["short references to the queries, logs or files that convinced you"],
  "repair_strategy": "what you changed and why",
  "remaining_risks": ["anything you did not fix, or that could still bite"],
  "additional_causes": ["any other genuine problem you found along the way"]
}
```

Notes on the deliverable:

- **Commit to a single `primary_cause`.** You are welcome to explore several hypotheses while
  investigating - that is normal incident work - but the report must land on one.
- **`additional_causes` is for real problems you can evidence.** Listing plausible-sounding
  possibilities you did not actually confirm will count against you. An empty list is
  perfectly acceptable.
- `damage_window` should be the date range affected by your primary cause. If nothing is
  damaged, say so there.

## Ground rules

- The data team owns everything under `/workspace` and the warehouse's own schemas.
- Treat this as production. Other people's numbers depend on these tables, including history
  you were not asked about, and not everything in the warehouse can be rebuilt from upstream.
- This world is live. Scheduled work runs and new data arrives while you are working, so
  something that was true when you checked it is not guaranteed to be true when you finish.
- There is no internet access.
