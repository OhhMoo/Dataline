"""postgres: `psql` with the box's PG* variables, which only a profile that plugs the
postgres warehouse exports (de/postgres).

The server is the always-on compose `postgres`: today it holds `warehouse.items` (2 rows)
and nothing of the world, so there is no world truth to compare against (truth_sql None).
Its statements are audited by a TAILER that folds the server's log into the action log
in the stack's `down()`, so the probe is `rows_at="teardown"`: judged after teardown,
on a stack of its own.

OWNED BY TASK 9, which seeds the server from the world for profiles that plug postgres,
wires the tailer, and rewrites this probe as February 2026 Net Revenue through psql.
Until then no `psql.statement` row is written and this probe FAILs on purpose (KNOWN).
"""
from tool_tour import Probe

PROBES = [
    Probe(
        id="postgres.psql.items_count",
        profile="de/postgres",
        plugs={},
        command=(
            "cd /workspace\n"
            "set -euo pipefail\n"
            "n=$(psql -tAc \"SELECT count(*) FROM warehouse.items\")\n"
            "printf '{\"value\": %s}\\n' \"$n\"\n"),
        expect_tools=("psql.statement",),
        truth_sql=None,
        rows_at="teardown",
        instruction=(
            "How many rows are in warehouse.items on the team's Postgres warehouse? Use "
            "psql with the connection settings this box provides."),
    ),
]

KNOWN = {
    "postgres.psql.items_count": (
        "Task 9, two missing pieces: (1) the tailer is not wired - "
        "native/tailers/postgres.py is not run by NativeContainerEpisode.down(), so a psql "
        "statement leaves no psql.statement row even after teardown; (2) the server is not "
        "seeded - it holds warehouse.items (2 rows), not the world, so there is no world "
        "truth for a psql read"),
}
