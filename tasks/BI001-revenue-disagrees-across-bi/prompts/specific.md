# ANA-241 - Executive Overview shows three different Net Revenue numbers

From: Hanna Kowalski (Analytics engineering lead)
To: whoever is on call

The Executive Overview exists three times - Power BI (leadership's, the one
Finance closes from), Tableau (Sales' rebuild) and Sigma (Finance's rebuild) -
and the three Net Revenue KPI cards disagree. This is the state I need things
in when you finish. It is a floor, not a walkthrough.

1. **The published dashboards are right.** What Tableau Server and Sigma serve
   for the Executive Overview shows the dbt metric on every tile - Net
   Revenue, Gross Revenue, Completed Orders, AOV, Segment Share, Units -
   exactly as `dbt_project/models/semantic/metrics.yml` defines them. Check
   the published numbers against the warehouse, not your local files.

2. **The fix is the definition.** Each build that is wrong is fixed where it
   is defined, in its project under `bi/`, so that it stays right when the
   data changes tomorrow. A number typed into a calculation, or a filter added
   to a tile so that today's rows happen to add up, is not a fix. Every tile
   keeps the question it asks.

3. **The repo is the source of truth.** The fix is committed. CI republishes
   main, so a change that is published and not committed will be overwritten.
   Publish with the tools `ae/runbooks/bi-release-process` names.

4. **Nothing else moves.** Power BI is right and is not touched. Nothing in
   the warehouse, the dbt models or `models/semantic` changes.

5. **Finance hears it from us.** Mia gets the right Net Revenue figure and
   what was wrong in Tableau and in Sigma, on ANA-241.

---

When you are done, write `/workspace/final_report.json`:

```json
{
  "summary": "<what was wrong and what you did, in a few sentences>",
  "root_cause": "<what was wrong in each tool - the mechanism, not the symptom>",
  "net_revenue": "<the Net Revenue the KPI card should show, as a number>",
  "deployed": ["<each BI tool whose published content you changed: powerbi | tableau | sigma>"],
  "changes": ["<each file you changed, and why>"]
}
```
