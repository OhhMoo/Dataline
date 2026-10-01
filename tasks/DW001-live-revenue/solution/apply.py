"""DW-1 oracle patch: apply the fix for any subset of the three defects.

    python tasks/DW001-live-revenue/solution/apply.py <world-root> [defect ...]

`apply` copies the fixed file for each named defect over the world's
workspace (solution/workspace/ mirrors workspace/ path for path); `restate`
then re-runs the world's own daily_load exec block and `dbt build` once, so
the warehouse reflects the fixed code immediately rather than at the next
scheduled 01:00. Neither touches var/sched/state.json or var/world/live.json:
`restate` runs the work the way an engineer re-running a load by hand does
(executor.execute / executor.dbt_invoke), so the scheduler's clock and run
history stay exactly the replayed ones.

Defect names are the contracts they damage (damage.yaml keys).
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

# contract -> the one workspace file whose fixed copy repairs it
FIXES = {
    "exactly_once": "dbt_project/models/warehouse/fct_revenue_lines.sql",
    "liveness": "loader/load_daily_revenue.py",
    "consumer_consistency": "dbt_project/models/staging/stg_revenue.sql",
}
ALL = tuple(FIXES)


def apply(root: Path, defects=ALL) -> list[str]:
    root = Path(root)
    done = []
    for name in defects:
        if name not in FIXES:
            raise SystemExit(f"unknown defect {name!r} (known: {', '.join(FIXES)})")
        rel = FIXES[name]
        shutil.copyfile(HERE / "workspace" / rel, root / "workspace" / rel)
        done.append(f"fix {name}: workspace/{rel}")
    return done


def restate(root: Path) -> list[str]:
    """Re-run the load and the dbt build now, outside the schedule."""
    if str(REPO / "world") not in sys.path:
        sys.path.insert(0, str(REPO / "world"))
    from world import executor, scheduler

    root = Path(root)
    live = scheduler.load_live(root)
    spec = live["jobs"]["daily_load"]["exec"]
    load = executor.execute(root, "daily_load", spec)
    if not load["ok"]:
        raise SystemExit(f"restate: daily_load failed: {load['error']}")
    rc, output = executor.dbt_invoke(root, "build")
    if rc != 0:
        raise SystemExit(f"restate: dbt build failed:\n{output}")
    return ["restate: daily_load", "restate: dbt build"]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    root, defects = Path(argv[0]), tuple(argv[1:]) or ALL
    for line in apply(root, defects) + restate(root):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
