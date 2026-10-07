"""sched: the team's own scheduler CLI, `./ops/sched`, which under the container topology
sends each subcommand to the scheduler plane at sched.internal:8120; the plane writes the
`sched.sched_<op>` row (gateway/src/alkera_gateway/native/shims/sched_cli.py).

Only a profile whose orchestrator is `ops_sched` gets the CLI (every de/* stack); no
da/* box has one, so this probe runs on de/laptop. Read-only: `trigger` would run a job,
and nothing an agent does may move the world's clock.
"""
from tool_tour import Probe

PROBES = [
    Probe(
        id="sched.ops_sched.dbt_build_successes",
        profile="de/laptop",
        plugs={},
        command=(
            "set -euo pipefail\n"
            "n=$(./ops/sched logs dbt_build | awk '$3 == \"success\"' | wc -l)\n"
            "printf '{\"value\": %s}\\n' \"$n\"\n"),
        expect_tools=("sched.sched_logs",),
        truth_sql=("SELECT count(*) FROM (SELECT unnest(runs) AS r FROM "
                   "read_json('var/sched/state.json')) "
                   "WHERE r.job = 'dbt_build' AND r.status = 'success'"),
        instruction=(
            "How many dbt_build runs has the team's scheduler recorded as successful? Use "
            "the scheduler CLI that ships in the repository."),
    ),
]
