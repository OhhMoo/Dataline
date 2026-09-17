# Can you look at the commercial numbers?

**Direct message to the data team's on-call engineer** - Nadia Brennan
(Head of Commercial Reporting), 08:52

> Morning. I think you're the one on call for data this week.
>
> Audit want the commercial marts certified next month, and I'm the one who signs the pack
> that goes out with them. I'm not comfortable doing that. I can't tell you what
> specifically is wrong - I'm not close enough to how any of this is put together - I only
> know I'm being asked to put my name to numbers I can't defend, and that when I've said so,
> nobody has been able to tell me they're fine either.
>
> So: can you go and look properly? If the marts are right, say so and show me how you know,
> and I'll sign. If they aren't, I need to know what is wrong, which days it touched, and
> that it has actually been fixed rather than patched for the pack.
>
> Either way - the nightly schedule keeps running while you're working, so please don't
> leave things in a worse state overnight than you found them.

---

## What you have

A working data stack and an on-call engineer's usual access to it. The warehouse is DuckDB,
transformations are dbt, and the repo is at `/workspace`. There is a scheduler with run
history, the monitoring the team has, the BI layer the business reads, a documentation wiki
of varying freshness, and the team's issue tracker and chat.

Nothing is currently alerting.

## What we need

Answer Nadia. That is the whole ask; what it takes is yours to work out.

If the numbers are right, say so, and show what convinced you. If they are not, find out why,
put it right, and make sure it stays right - including after the pipeline runs again, and
including for data that is still in flight now. If you change something, leave behind a check
that would have caught this.

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
