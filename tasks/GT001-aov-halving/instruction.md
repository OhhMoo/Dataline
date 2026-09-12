# Average order value looks wrong

**Ticket DATA-4471** — raised by Priya Raghavan (Commercial Analytics) — priority: high

> The AOV panel on the Commercial Daily dashboard has been sitting at roughly half its
> normal level since early February. Finance are using this number in the quarterly pack
> and they want to know whether the business genuinely fell off a cliff or whether the
> number is wrong. I've checked the dashboard definition and it hasn't changed.
>
> Can someone from the data team look? We need an answer before Thursday.

---

## What you have

A working data stack. The warehouse is DuckDB; transformations are dbt; the repo is at
`/workspace`. There is a scheduler with run history, a documentation wiki of varying
freshness, and the BI layer that renders the dashboard Priya is looking at.

Nothing is currently alerting. Freshness monitoring is green.

## What we need

Work out what is actually going on, fix it, and make sure it cannot silently recur.

Concretely:

1. **Find out why the number moved.** If the business genuinely declined, say so and show
   your evidence. If the data is wrong, find out how.
2. **Repair whatever is broken**, without breaking anything that currently works.
3. **Make the fix durable.** If something can recur, change what allows it to recur, not
   just the symptom.
4. **Leave behind a test** that would catch this earlier next time.

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

- **Commit to a single `primary_cause`.** You are welcome to explore several hypotheses
  while investigating — that is normal incident work — but the report must land on one.
- **`additional_causes` is for real problems you can evidence.** Listing plausible-sounding
  possibilities you did not actually confirm will count against you. An empty list is
  perfectly acceptable.
- `damage_window` should be the exact date range affected by your primary cause.

## Ground rules

- The data team owns everything under `/workspace` and the `warehouse` schemas.
- Treat this as production. Other people's numbers depend on these tables, including
  history you were not asked about.
- There is no internet access.
