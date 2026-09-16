DATA-7702 - `warehouse.commercial_extract` is landing after its deadline

The nightly chain in the `commercial-nightly` repo is `vendor_sync` ->
`build_commercial` -> `export_commercial`. `export_commercial` carries the
service level: `warehouse.commercial_extract` must be complete by the deadline
declared in `orchestration/schedule.yml`, because the Commercial pack is built
from it at 06:00. Since the 9th it has been finishing after that deadline every
night.

Your job is to make the following true, and to leave behind an account that
justifies what you did.

1. **The extract lands inside its deadline on the next scheduled run.** The
   schedule that is deployed is `orchestration/schedule.yml` in the repo; that
   file is what the scheduler runs. Runtimes are not in it - the scheduler
   measures those itself and keeps them with each run.

2. **The chain still does its work.** All three jobs run and succeed, and the
   extract is rebuilt from the night's data. A job that lands on time because
   it no longer does anything is not a fix.

3. **Nothing in the chain starts before 01:05.** The vendor drops at 01:00 and
   the bucket is empty before that.

4. **No job starts before the job ahead of it in the chain has finished.**
   `export_commercial` refuses to publish a day whose mart is still being
   rebuilt - that already happened once, on the 8th - so overlapping the chain
   is not a repair.

5. **The service level itself does not move.** 04:00 is Commercial's, not ours.

Then write `/workspace/final_report.json` with exactly these keys:

```json
{
  "sla_job": "<the job that carries the service level>",
  "sla_deadline": "<HH:MM>",
  "observed_finish": "<HH:MM the extract is actually landing>",
  "verdict": "ran_long" | "started_late",
  "observed_runtime_s": <how long that job takes per run, in seconds>,
  "runtime_changed": true | false,
  "start_shift_minutes": <how much later it begins than it used to, in minutes>,
  "regression_date": "<YYYY-MM-DD the behaviour changed>",
  "fix": "<what you changed and why>"
}
```

`verdict` must be one of those two strings, and it must be the one the run
history supports. A job misses a deadline either because it **ran long** - its
start is where it has always been and its per-run duration grew - or because it
**started late** - its per-run duration is unchanged and its start moved. Line
up `export_commercial`'s own runs, night by night, before you decide. The two
answers have opposite remedies and only one of them can help here.
