"""postgres: `psql` with the box's PG* variables, which only a profile that plugs the
postgres warehouse exports (de/postgres).

The server is the always-on compose `postgres`: today it holds `warehouse.items` (2 rows)
and nothing of the world, so there is no world truth to compare against (truth_sql None).
Task 9 seeds it from the world for profiles that plug postgres and wires the statement
tailer; until then no `psql.statement` row is written, and this probe FAILs on purpose.
"""
from tool_tour import Probe

PROBES = [
    Probe(
        id="postgres.psql.items_count",
        profile="de/postgres",
        plugs={},
        command=(
            "set -euo pipefail\n"
            "n=$(psql -tAc \"SELECT count(*) FROM warehouse.items\")\n"
            "printf '{\"value\": %s}\\n' \"$n\"\n"),
        expect_tools=("psql.statement",),
        truth_sql=None,
        instruction=(
            "How many rows are in warehouse.items on the team's Postgres warehouse? Use "
            "psql with the connection settings this box provides."),
    ),
]

KNOWN = {
    "postgres.psql.items_count": (
        "Task 9: native/tailers/postgres.py is not run by NativeContainerEpisode.down(), so "
        "a psql statement leaves no psql.statement row in actions.jsonl"),
}
