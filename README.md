# alkera — data-agent benchmark

A training and evaluation substrate for AI data agents, sold to frontier labs.

## Northstar

**Mock a real data engineer's working computer — as it actually is.** Messy, moving, and
honest: a surface never reports success for work it did not do. Difficulty must come from
the job, not from contrived obscurity. Data science is out of scope; this is data
engineering.

## Built

- [x] **The world** — deterministic from a seed, byte-identical rebuilds, verified
      host-vs-container. Virtual clock (`advance(1)×N == advance(N)`).
- [x] **The stack** — DuckDB warehouse, Postgres, S3, fakesnow, a real dbt project that
      compiles/runs/tests, a scheduler that really executes jobs.
- [x] **The surfaces** — 63 MCP tools + a real bash shell over a 50-binary toolbelt
      (psql, aws, sqlfluff, csvkit, dbt). Issues, chat, knowledge base, BI, metrics,
      alarms, deploys, ingest, docs, git.
- [x] **Containment** — grader unreachable from the episode, no egress, proven per service.
- [x] **The harness** — per-tool-call audit log, transcripts, Pass@1 **and Pass^k**,
      blindness detection, and a gate that *refuses to publish a rate* if a required
      surface was dead.
- [x] **7 tasks** (GT001–GT007), 6 calibrated against Opus 5 at K=5.
- [x] **Airflow** — real, under the virtual clock; editing a DAG changes execution. (~4 of
      ~20 days)
- [x] **Snowflake** — real dbt runs against fakesnow, `PASS=36 ERROR=0`. (~4 of 8–12 days)
- [ ] **Difficulty** — the open problem. See below.
- [ ] Container-topology numbers for GT002–GT007 (all current numbers are host).
- [ ] A second model family (Kimi) on the new tasks.

## Where we are

| task | Pass@1 | |
|---|---|---|
| GT002 | **0.8** | the only unit |
| GT004 | **0.0** | a wall |
| GT001, GT005, GT006, GT007 | **1.0** | ceilings |

GT005/GT006/GT007 were built *specifically* to land near 0.8, by builders holding this
table. All three came out at 1.0.

Ruled out by measurement: **prompt wording** (5 variant runs, zero movement) and
**concealment** (GT006 hides a 6.4%/day loss inside a 14.75% swing and still scores 1.0).

What worked, reproducibly: **GT004's closed `wontfix` ticket.** The model *names* the second
defect and never repairs it — 21 episodes, 3 prompt framings, 0 repairs. The world hands it
an authoritative reason to leave the thing alone, and it defers to that over the data.

## Next

1. **The interference experiment.** Take GT006 (clean 1.0), add nothing but a closed ticket
   asserting the behaviour is intentional. Re-run K=5. One change, one run, tests the only
   lever we have measured.
2. **Filter the archetype corpus for depth** — keep the ones needing repeated edit/verify
   cycles inside one episode; drop the one-shot diagnoses.
3. Distractors and live mid-episode events in the theme layer.

## Structure

```
world/        world factory: themes, seeds, scheduler, faults, fingerprint, Airflow bridge
gateway/      the only way an agent touches the world — 63 MCP tools, containment, docker
tasks/        GT001-GT007 task packages + harness/ (rollouts, grading, blindness, reports)
implementation_structure/   research record: 41 archetypes, sources, failure rulings, gates
verifier/     append-only verification history
scripts/      status tooling
```

Full guide: [`agent.md`](agent.md) — §8.10 difficulty, §9 contracts and ground rules,
§10 architecture, §11 track status.
