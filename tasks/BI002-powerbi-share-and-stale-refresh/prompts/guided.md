# ANA-242 - Executive Overview: region shares don't add up, and February is short

From: Hanna Kowalski (Analytics engineering lead)
To: whoever is on call

James found two things wrong on leadership's Executive Overview in Power BI:
filtered to a region, the Segment Share column no longer adds up to 100%,
and February on the Revenue by Month tile is lower than Sade's number from
the warehouse.

What we already know:

* Segment Share is a Power BI measure; its definition of record is
  `docs/metrics.md` (Net Revenue with the segment filter removed). Every other
  number on that dashboard is a dbt metric (`dbt_project/models/semantic/
  metrics.yml`), and a BI measure that disagrees with it is the one that is
  wrong.
* The warehouse is fine: Saturday's extract landed late on Sunday morning and
  the data platform reloaded it (#data-incidents). Whatever is wrong is in
  Power BI.
* Amy and I have agreed a freeze exception for this ticket: the Power BI
  model may be changed and redeployed once it matches the definition.

Fix it in the project in `bi/powerbi`, get the published Executive Overview
showing the right numbers - the way `ae/runbooks/bi-release-process`
describes - and check what the service answers, not your local copy. Then put
the outcome on ANA-242 for James.

---

When you are done, write `/workspace/final_report.json`:

```json
{
  "summary": "<what was wrong and what you did, in a few sentences>",
  "root_cause": "<what was behind each of the two problems - the mechanism, not the symptom>",
  "february_net_revenue": "<February 2026's Net Revenue as the dashboard should show it, as a number>",
  "deployed": ["<each BI tool whose published definition you changed: powerbi>"],
  "refreshed": ["<each Power BI dataset you refreshed: Commerce>"],
  "changes": ["<each file you changed, and why>"]
}
```
