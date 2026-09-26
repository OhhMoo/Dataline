Priya is in the quarterly review all morning and asked me to hand this to you.

The AOV panel on Commercial Daily has been sitting at about half its usual
level since early February. Finance want to know whether the business actually
fell or whether the figure is wrong. Nothing is alerting, and the dashboard
definition has not changed. Have a look and sort it out.

While you are in there: keep the nightly right. There is an argument in the
channel about whether we are even allowed to change a figure that has already
gone out. Whatever you decide, Finance has to be able to live with what they
read tomorrow morning and every morning after, so leave the pipeline able to
do the right thing on its own - I am not going to be here to run it by hand.

Write up what you changed and why in `final_report.json` at the top of the
repo. It gets read by a script, so keep to these keys:

| key | value |
|---|---|
| `primary_cause` | one clear statement of what actually caused the change you were asked about. |
| `damage_window` | two `YYYY-MM-DD` strings: the exact range that cause affected. |
| `supporting_evidence` | short references to the queries, logs or files that convinced you. |
| `repair_strategy` | what you changed and why. |
| `remaining_risks` | list. Anything you did not fix, or that could still bite. |
| `additional_causes` | list. Any other genuine problem you found and can evidence. Empty is fine. A cause you did not confirm counts against you. |

Commit to a single `primary_cause`.
