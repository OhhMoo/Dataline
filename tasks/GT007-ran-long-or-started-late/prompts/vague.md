From: Harriet Moss, Head of Commercial Reporting
To: Data platform
Subject: DATA-7702 - the pack, again

The Commercial pack was not ready when my team came in this morning, and it was
not ready on Friday either. It is built at 06:00 off something your side
produces overnight and the whole reporting day sits on top of it.

I am told it was fixed last week. It was not. I have the group on Thursday and
what I need from you is two things: it landing on time tomorrow, and one
straight sentence about what actually made it late that I can say out loud,
because "we have mitigated it" is what I was told last time.

---

You have the data platform's environment. Work out what is wrong, fix it, and
leave the answer behind.

Changes to the commercial chain are not applied by hand here. They are rolled
out through the platform's deploy surface, under the canary that comes with
them, and the rollout is expected to be on the record. Roll yours out, and do
not leave a canary breached.

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
  "fix": "<what you changed and why>"
}
```

`verdict` must be one of those two strings. A job misses a deadline either
because it ran long - it started on time and took longer than it takes - or
because it started late - it took exactly as long as it always takes and simply
began later. They are not the same thing and they do not have the same fix.
