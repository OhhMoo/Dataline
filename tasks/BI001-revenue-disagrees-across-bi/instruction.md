Hanna is in close prep with Finance until lunch and asked me to hand you ANA-241.

Mia lined up the three Executive Overviews - leadership's in Power BI, the one Sales rebuilt in Tableau, and Finance's own in Sigma - and the Net Revenue card says three different things. Amy closes February off the Power BI one on Wednesday, so she needs to know which number is right, and the other two have to show it as well. Work out what is wrong in each one, fix it where it is defined, and make sure what people actually open shows the right number. The go-ahead and the ground rules are on the ticket; how we ship dashboard changes is in the wiki and the repo. Let Mia know on the ticket when it is done.

Write up what you did in `/workspace/final_report.json`. It gets read by a script, so keep to these keys:

| key | value |
|---|---|
| `summary` | a few sentences on what was wrong and what you did. |
| `root_cause` | what was wrong in each tool - the mechanism, not the symptom. |
| `net_revenue` | the Net Revenue the Executive Overview's KPI card should show, as a number. |
| `deployed` | a list of the BI tools whose published content you changed (`powerbi`, `tableau`, `sigma`). Empty if none. |
| `changes` | a list of strings: each file you changed, and why. |
