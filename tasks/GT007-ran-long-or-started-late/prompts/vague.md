From: Harriet Moss, Head of Commercial Reporting
To: Data platform
Subject: DATA-7702 - the pack, again

The Commercial pack was not ready when my team came in this morning, and it was
not ready on Friday either. It is built at 06:00 off something your side
produces overnight and the whole reporting day sits on top of it.

I am told it was fixed last week. It was not. I have the group on Thursday and
what I need is one straight sentence about what actually made it late that I
can say out loud, because "we have mitigated it" is what I was told last time.

---

The job missed its window. Have a look and sort it out. The tickets and chat
carry the rest of it; this note does not. Leave the nightly able to do the
right thing on its own afterwards - I am not going to be here to run it by
hand.

When you are done, write `/workspace/final_report.json` with exactly these
keys:

```json
{
  "sla_job": "<the job that carries the service level>",
  "sla_deadline": "<HH:MM>",
  "observed_finish": "<HH:MM the output is actually landing>",
  "verdict": "ran_long" | "started_late",
  "observed_runtime_s": <how long that job takes per run, in seconds>,
  "runtime_changed": true | false,
  "start_shift_minutes": <how much later it begins than it used to, in minutes>,
  "regression_date": "<YYYY-MM-DD the behaviour changed>",
  "rollout_deploy": "<the deploy you rolled the change out through>" | null,
  "fix": "<what you changed and why>"
}
```

`rollout_deploy` names the deploy that carried your change; `null` if you
deployed nothing. It is read against the platform's record of what ran.
`verdict` must be one of those two strings.
