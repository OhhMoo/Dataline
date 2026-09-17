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

**Comment** - Tolu Okafor (data platform lead):

> I'm the one signing this off for audit, so here is the list I will sign against. It is what
> has to be **true** when you are finished. How you get there is yours - I would rather you
> worked the problem than worked the list.

---

## Acceptance criteria

Checked against the state you leave behind, not against your write-up.

1. **It reconciles.** Across the whole retained history, not the days anyone happened to
   sample, what the mart reports agrees with the source of record: the same business events,
   each counted once, carrying the same values, in the same shape.

2. **Running it again is safe, and running it again still does something.** The load can run a
   second time over the same period without any business figure moving, and a period it has
   not loaded yet is still picked up, exactly once, when it runs. A load made safe by making
   it stop doing its job fails this criterion, and fails it harder than leaving it alone
   would have. Per team policy, nothing is hand-edited into the warehouse: whatever you do has
   to be reproducible by running the pipeline, or it is not certifiable.

3. **It survives what arrives next.** Events that land after you have finished with a date -
   including dates you have already put right - end up counted, once, without undoing the
   correction.

4. **The number the business reads is the corrected one.** The downstream exposure still
   renders and returns the repaired figure. A mart that is right behind a board that is stale
   is not a finished repair.

5. **Nothing else moved.** Every surface this ticket did not ask you to touch is as you found
   it. We diff all of them, including the ones you would not think to check.

6. **There is a regression check.** A test that fails against the world as you found it and
   passes against the world as you leave it. One that passes against both proves nothing and
   will be scored as though it were not there.

## What you have

A working data stack and an on-call engineer's usual access to it. The warehouse is DuckDB,
transformations are dbt, and the repo is at `/workspace`. There is a scheduler with run
history, the monitoring the team has, the BI layer the business reads, a documentation wiki
of varying freshness, and the team's issue tracker and chat.

Nothing is currently alerting.

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
