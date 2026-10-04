# ANA-241 - Executive Overview shows three different Net Revenue numbers

From: Hanna Kowalski (Analytics engineering lead)
To: whoever is on call

Finance put the three builds of the Executive Overview side by side - Power BI
(leadership's, the one Amy closes from), Tableau (Sales' rebuild) and Sigma
(Finance's rebuild) - and the Net Revenue KPI card disagrees across all three.

What we already know:

* The definition of record for every metric on that dashboard is the dbt
  Semantic Layer (`dbt_project/models/semantic/metrics.yml`). A BI measure
  that disagrees with the dbt metric of the same name is the one that is
  wrong.
* The warehouse and the dbt build are fine: the nightly is green and nobody
  has deployed a model change. Whatever is wrong is in the BI projects.
* Amy and I have agreed a freeze exception for this ticket: the Tableau and
  Sigma builds may be republished this week once they match. Power BI is not
  to be touched.

Find out what is wrong in each build that disagrees, fix it in the project in
`bi/`, and get the published dashboards showing the right numbers - the way
`ae/runbooks/bi-release-process` describes. Then put the outcome on ANA-241 for
Mia.

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
