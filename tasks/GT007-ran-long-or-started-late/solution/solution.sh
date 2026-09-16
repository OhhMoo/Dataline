#!/usr/bin/env bash
# GT007 reference solution - the oracle.
#
#   solution.sh <world-root>
#
# It does exactly two things, because there are exactly two things to do:
# state the diagnosis, and apply the repair the diagnosis implies. It touches
# no data, drops no table and runs no backfill, and that is the point of the
# unit - the warehouse was never wrong.
#
# ----------------------------------------------------------------------------
# THE DIAGNOSIS, and how it is reached from what the agent can see.
#
# 1. The service level is on export_commercial: warehouse.commercial_extract by
#    04:00 (orchestration/schedule.yml, the runbook, DATA-7702).
#
# 2. `sched_state` carries 45 runs over 15 nights. Line up export_commercial's
#    own runs:
#
#      2026-03-31 .. 2026-04-07   start 03:10   end 03:30   (1200s)
#      2026-04-08                 start 03:10   FAILED after 12s
#      2026-04-09 .. 2026-04-14   start 04:10   end 04:30   (1200s)
#
#    Its duration is 1200s on every night it completed. It has not moved once.
#    THE EXPORT DID NOT RUN LONG. Its START moved by 60 minutes on 2026-04-09
#    and has stayed there. IT STARTED LATE.
#
# 3. Why: `git log -p orchestration/schedule.yml` shows the 2026-04-09 commit
#    that widened export_commercial's wait after build_commercial from 95 to
#    155 minutes, in reaction to the 2026-04-08 incident (DATA-7710), where
#    build_commercial ran 7200s instead of its usual 3600s and the export
#    walked into a half-built mart. The build has run 3600s every night since -
#    that overrun happened once in fifteen nights - so the extra 60 minutes
#    buys nothing and costs the SLA.
#
# 4. Why the popular answer is wrong: DATA-7714, the runbook and #data-eng all
#    say the export has outgrown its window. Every figure in DATA-7714 is true -
#    end-to-end latency really did grow by an hour - but latency is not
#    duration, and the duration did not move. The timeout was already raised on
#    the 9th (1800 -> 2700) and the landing time did not shift by a second,
#    which is the world telling you the same thing a second time: a timeout is
#    when we give up on a job, not when it starts or how long it takes.
#
# ----------------------------------------------------------------------------
# THE REPAIR. Bring the wait back inside the feasible band.
#
#   lower bound 60   the export must not start before build_commercial (3600s)
#                    finishes, or its freshness guard fires - that is the
#                    2026-04-08 incident (ADR 0002).
#   upper bound 125  the export takes 1200s and must end by 04:00, so it must
#                    start by 03:40, which is 125 minutes after the build's
#                    01:35 start.
#
# 120 is chosen inside that band rather than the old 95: it puts the export at
# 03:35, which is the exact moment a DOUBLED build would finish, so the fix
# survives a repeat of the 8th, and still lands the extract at 03:55 with five
# minutes in hand. 95 would also pass the gate and would be more fragile.
# ----------------------------------------------------------------------------
set -euo pipefail

ROOT="${1:?usage: solution.sh <world-root>}"
WS="$ROOT/workspace"
SCHEDULE="$WS/orchestration/schedule.yml"

test -f "$SCHEDULE" || { echo "no schedule at $SCHEDULE" >&2; exit 1; }

python - "$SCHEDULE" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
old = "after: {job: build_commercial, minutes: 155}"
new = "after: {job: build_commercial, minutes: 120}"
if old not in text:
    raise SystemExit(f"{path} no longer carries {old!r}; refusing to guess")
path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="")
print("export_commercial: wait after build_commercial 155 -> 120 minutes")
PY

python - "$WS/final_report.json" <<'PY'
import json
import sys
from pathlib import Path

report = {
    "sla_job": "export_commercial",
    "sla_deadline": "04:00",
    "observed_finish": "04:30",
    "verdict": "started_late",
    "observed_runtime_s": 1200,
    "runtime_changed": False,
    "start_shift_minutes": 60,
    "regression_date": "2026-04-09",
    "blocking_job": "build_commercial",
    "evidence": [
        "export_commercial's own duration is 1200s on every one of the 14 "
        "nights it completed, before and after the regression - it did not "
        "run long",
        "its start moved 03:10 -> 04:10 on 2026-04-09 and has not moved since",
        "the 2026-04-09 commit to orchestration/schedule.yml widened its wait "
        "after build_commercial from 95 to 155 minutes",
        "that commit reacted to 2026-04-08, when build_commercial ran 7200s "
        "instead of 3600s; it has run 3600s on every other night in the "
        "history",
        "the same commit raised the export's timeout from 1800s to 2700s and "
        "the landing time did not change at all",
    ],
    "fix": (
        "orchestration/schedule.yml: export_commercial now waits 120 minutes "
        "after build_commercial instead of 155. The feasible band is 60 to "
        "125: at least the build's 3600s runtime so the freshness guard cannot "
        "fire, at most 125 so the 1200s export ends by 04:00. 120 puts the "
        "export at 03:35 - the moment a doubled build would finish - so it "
        "also survives a repeat of the 8th, and lands the extract at 03:55."
    ),
    "not_the_cause": (
        "The export has not got slower. DATA-7714 measures end-to-end latency, "
        "which did grow by an hour, and reads it as duration, which did not "
        "move at all. Raising the timeout again changes nothing: the timeout "
        "is not an input to the scheduler."
    ),
}
Path(sys.argv[1]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8", newline="")
print(f"wrote {sys.argv[1]}")
PY
