Hanna asked me to hand you ANA-242 before she logged off.

James was building tonight's regional packs off the Executive Overview in Power BI and found two things wrong: filtered to a region, the Segment Share column no longer adds up to 100%, and February on the Revenue by Month tile is lower than the number Sade got from the warehouse. The packs go out before APAC's Monday starts, and Amy takes February off the same dashboard on Wednesday. Work out what is wrong, fix it where it is defined, and make sure what people actually open in Power BI shows the right numbers. The go-ahead and the ground rules are on the ticket; how we ship dashboard changes is in the wiki and the repo. Let James know on the ticket when it is done.

Write up what you did in `/workspace/final_report.json`. It gets read by a script, so keep to these keys:

| key | value |
|---|---|
| `summary` | a few sentences on what was wrong and what you did. |
| `root_cause` | what was behind each of the two problems - the mechanism, not the symptom. |
| `february_net_revenue` | February 2026's Net Revenue as the Executive Overview should show it, as a number. |
| `deployed` | a list of the BI tools whose published definition you changed (`powerbi`). Empty if none. |
| `refreshed` | a list of the Power BI datasets you refreshed (`Commerce`). Empty if none. |
| `changes` | a list of strings: each file you changed, and why. |
