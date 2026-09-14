# Orders have stopped arriving

**Ticket DATA-8842** — raised by Tobias Lund (Analytics Engineering) — priority: high

> Order volume on the exec revenue board flatlined some time around the 9th and nobody has
> been able to tell me why. The pipeline is green — every task in the orders DAG has
> succeeded on every run for the last four days, the logs are clean, and nothing has
> alerted. But fct_orders has not gained a row since the 9th and the warehouse team says
> the table is fine. Finance is going to notice on the 15th when they run the
> month-to-date pack.
>
> Please find out why orders have stopped arriving, get the missing four days into the
> table, and make sure whatever this was would page someone next time instead of silently
> reporting success.
>
> One thing worth knowing — the orders producer was rolled back and re-deployed on the 8th,
> so some orders around that date may have been sent more than once with corrections. Those
> corrections are real and Finance needs them.

---

## What you have

A working data stack. The warehouse is DuckDB; the repo is at `/workspace`. Orders reach the
warehouse through a streaming pipeline whose steps live in `/workspace/pipelines`. There is a
scheduler with run history, a documentation wiki of varying freshness, and the BI layer that
renders the board Tobias is looking at. A second orchestration platform is mirrored read-only
under `/workspace/asset_platform`.

Nothing is currently alerting. Freshness monitoring is green.

## What we need

Work out what is actually going on, fix it, and make sure it cannot silently recur.

Concretely:

1. **Find out why the rows stopped.** If orders genuinely stopped being placed, say so and
   show your evidence. If the pipeline is at fault, find out how.
2. **Recover what is missing**, without breaking anything that currently works.
3. **Make the fix durable.** If something can recur, change what allows it to recur, not just
   the symptom.
4. **Leave behind a check** that would catch this earlier next time.

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
- `damage_window` should be the date range affected by your primary cause.

## Ground rules

- The data team owns everything under `/workspace` and the `warehouse` and `raw` schemas.
- Treat this as production. Other people's numbers depend on these tables, including history
  you were not asked about, and not everything in the warehouse can be rebuilt from upstream.
- The asset platform belongs to another team. You can read its state; it is mirrored here for
  lineage.
- There is no internet access.
