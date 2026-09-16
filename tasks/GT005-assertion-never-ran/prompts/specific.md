# DATA-5301 - Q1 revenue in the close pack is under the ledger

From: Joe O'Donnell (Data Platform lead)
To: whoever is on shift

Finance closed Q1 and `mart_revenue_by_month` is under the ledger by about four
and a half per cent, every month since January. The nightly build has been green
throughout - forty-odd consecutive successes, zero test failures - and nobody
has deployed to the revenue models since December.

This is the state I need the repo and the warehouse in when you finish. It is a
floor, not a walkthrough; how you get there is yours.

1. **The number is right.** `warehouse.fct_revenue_daily` reconciles to
   `raw.billing_invoice_line` for every invoice date in the extract, counting
   only `line_status = 'posted'`. Reconcile the chain one step at a time -
   raw, staging, fact, daily, monthly - and find the step that loses rows.

2. **The fix is in the transformation, not in the data.** Do not rewrite rows in
   `raw.*` and do not hand-patch a mart. Tomorrow's extract lands the same
   content it landed today, so a repair that does not survive one more day of
   the same input is not a repair. Assume I will test that.

3. **The pipeline catches it next time.** After your change, a source system key
   that nobody has conformed must make `dbt build` exit non-zero the first night
   it appears - **on any billing feed that carries one**, not only on the one
   that went wrong. ADR-0006 is the standard; hold the whole project to it, not
   the model you happened to open. The build must still be green on correct
   input, including one more day of the input we already have. A control that
   cannot fail is not a control; a control that always fails is not one either.

4. **The dates are exact.** Finance quotes dates in the audit pack. Give the
   earliest and latest affected invoice dates, measured, not rounded to a month.

Constraints: the extract (`raw.*`) and the operational history (`ops.*`) are
records of what happened and are not yours to edit. Everything under
`/workspace` is.

The warehouse is duckdb. Attach it, do not open it directly:

    ATTACH '<path>' AS wh (READ_ONLY); USE wh.warehouse;

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
