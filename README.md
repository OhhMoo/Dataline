# alkera — data-agent benchmark

A training and evaluation substrate for AI data agents, delivered to frontier labs through
Alkera's services. Each task runs inside a **live, containerized data stack** — warehouse,
dbt, scheduler, BI, scattered docs. The agent reaches it only through an **audited tool
gateway**. Grading is **trajectory-blind and deterministic**, and the world **rebuilds
identically from a seed**.

The target: **Pass@1 below 20%** for frontier models genuinely trying. Difficulty comes from
*conjunction* — five or more independently graded sub-results — never from obscurity. The
design key, from the research: *the system reports green while being wrong.*

Full project guide: [`agent.md`](agent.md). Architecture detail: §10 there.

## Layout

```
world/                    the world factory: seed -> warehouse + dbt + scheduler + BI,
                          semantic fingerprint, snapshot/restore, 3-zone Docker topology
gateway/                  the only way an agent touches the world: 17 tools, MCP + CLI over
                          one audited core, destructive ops flagged not blocked
tasks/GT001-aov-halving/  the golden task: seed spec, generator, oracle, hidden grader,
                          six adversarial baselines, shadow judge
implementation_structure/ the research swarm and its record: archetypes, sources, tool
                          inventory, failure records, and the gates that judge them
```

## The one rule

```
tasks/ ──▶ gateway/ ──▶ world/
```

Dependencies point one way. `world/` knows nothing of the gateway; the gateway knows nothing
of any task. `implementation_structure/` is upstream of everything and imports nothing.

## Three trust zones

**Harness** builds the world and runs the agent. **Episode** has no network egress and no
grader. **Grading** starts only after teardown and mounts the world read-only. The grader does
not exist during the episode — with the reference reachable, an earlier design scored 11 of 14
points by copying it.

## Verify

```
cd gateway  && .venv/Scripts/python -m pytest tests               # 12 tests
cd world    && PYTHONPATH=. ../gateway/.venv/Scripts/python -m pytest tests   # 7 tests
cd tasks/GT001-aov-halving/tests && python run_baselines.py        # TASK VALID
                                    python test_integration.py     # 12 assertions
python implementation_structure/gates/run_gates.py                 # G0-G8
```

G7 fails on purpose: it lists every red-team ruling nobody has executed yet.

## Where things stand

41 archetypes (32 shippable), 285 sources, 132 tool records, 80 failure records, 0 promoted
to `verified/`. One task runs end to end. The corpus is honest about itself; that is not the
same as strong. See `agent.md` §9.5 and `implementation_structure/rounds/`.
