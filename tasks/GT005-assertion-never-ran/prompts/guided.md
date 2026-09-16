# DATA-5301 - Q1 revenue in the close pack is under the ledger

From: Joe O'Donnell (Data Platform lead)
To: whoever is on shift

Finance closed Q1 and `mart_revenue_by_month` is under their number by about
four and a half per cent. It has been under by roughly the same proportion every
month since January. The shape is right - the daily curve tracks, the currencies
are right - it is just consistently light.

What Rhea has already ruled out:

* The nightly build is green. The last forty-odd scheduled runs are all success
  with zero test failures.
* Nobody has deployed anything to the revenue models since December.
* The gap is stable month on month, so it is not a lost load - a lost load would
  show as a notch, not a slope.

Please work out why the number is wrong, fix it, and make sure the pipeline
would catch this if it happened again. Work from `raw.billing_invoice_line`
rather than from the marts; the marts are the thing under suspicion.

Tamas needs the exact first and last affected invoice dates, not rounded months
- the audit pack quotes dates, and they are deciding whether to restate.

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
