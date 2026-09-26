Joe is on a call all morning and asked me to hand this to you.

Finance says our warehouse number does not match theirs. It is not far off, a few percent, and it has looked like that for a while rather than like we lost a day. I checked the nightly before I left. The builds are green, the recent runs all succeeded, and nobody has shipped a change on that side since December. Have a look and sort it out.

While you are in there: there is an argument about whether we are even allowed to change a figure that has already gone out. Whatever you decide, what they read has to be something they can live with, and the pipeline has to catch a quiet miss like this on its own the next time. I am not going to be here to run it by hand.

Write up what you changed and why in `/workspace/final_report.json`. It gets read by a script, so keep to these keys:

| key | value |
|---|---|
| `summary` | a few sentences on what was wrong. |
| `root_cause` | the mechanism, not the symptom. |
| `first_affected_date` | `YYYY-MM-DD`. The earliest affected invoice date, measured, not rounded. |
| `last_affected_date` | `YYYY-MM-DD`. The latest affected invoice date, the same way. |
| `changes` | a list of strings: each file or object you changed, and why. |
