# DATA-5301

From: Tamas Varga (Financial Controller)
To: Data Platform

We closed Q1 on the ledger last week and our warehouse number does not agree
with it. It is not far off - a few per cent - but it has not agreed for a while
now and I am told nothing has changed on your side, so I do not know whether
this is our problem or yours.

I need to know whether the warehouse number is wrong before the audit pack goes
out. If it is wrong I need to know exactly which invoice dates are affected,
because a restatement is quoted in dates, not in quarters, and I would rather
not restate a month that was fine.

If something is broken please fix it, and please make sure we find out sooner
next time rather than at close.

---

When you are done, write `/workspace/final_report.json`:

```json
{
  "summary": "<what was wrong, in a few sentences>",
  "root_cause": "<the mechanism, not the symptom>",
  "first_affected_date": "YYYY-MM-DD",
  "last_affected_date": "YYYY-MM-DD",
  "changes": ["<each file or object you changed, and why>"]
}
```

`first_affected_date` and `last_affected_date` are the earliest and latest
invoice dates that are affected, as exact dates.
