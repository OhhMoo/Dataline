# alkera — data-agent benchmark

A training and evaluation substrate for AI data agents.

## Northstar

**Mock a real data engineer's working computer — as it actually is.** Messy, moving, and
honest: a surface never reports success for work it did not do. Difficulty must come from
the job, not from contrived obscurity. Data science is out of scope; this is data
engineering.

## Built

- [x] **The world**: deterministic from a seed, byte-identical rebuilds, verified
      host-vs-container. Virtual clock (`advance(1)×N == advance(N)`).
- [x] **The stack**: DuckDB warehouse, Postgres, S3, fakesnow, a real dbt project that
      compiles/runs/tests, a scheduler that really executes jobs.
- [x] **The surfaces**: 63 MCP tools + a real bash shell over a 50-binary toolbelt
      (psql, aws, sqlfluff, csvkit, dbt). Issues, chat, knowledge base, BI, metrics,
      alarms, deploys, ingest, docs, git.
- [x] **Containment**: grader unreachable from the episode, no egress, proven per service.
- [x] **The harness**: per-tool-call audit log, transcripts, Pass@1 **and Pass^k**,
      blindness detection, and a gate that *refuses to publish a rate* if a required
      surface was dead.
- [x] **8 tasks** (GT001–GT008) + 7 ablation/2×2 arms, all TASK VALID, calibrated
      against Opus 5 at K=5.
- [x] **The four-layer task framework**: contracts (`tasks/contracts/`, 75 tests,
      18/18 equivalence with GT004's grader), stressor registry, a mechanical coupling
      filter (GT004: 2 accepted / 10 rejected, no model tokens), and a protected
      publication ledger graded at every checkpoint (containment P5).
- [x] **Native surfaces**: the agent works through `duckdb`/`dbt`/`git`/`aws`, the real
      `jira`/`slack_sdk`/`atlassian` clients and vendor-shaped fake APIs, with no tool
      list; the same audited row on both transports (parity measured, P6 measured).
      Scripted episodes run end to end on GT001/GT004/GT006/GT008.
- [x] **Airflow**: real, under the virtual clock; editing a DAG changes execution. (~4 of
      ~20 days)
- [x] **Snowflake**: real dbt runs against fakesnow, `PASS=36 ERROR=0`. (~4 of 8–12 days)
- [ ] **Difficulty**: the open problem. See below.
- [ ] **Native-mode model numbers** (Phase 4): GT001/GT004/GT006/GT008 K=5 vs MCP.
- [ ] Container-topology numbers for GT002–GT008 (all current numbers are host).
- [ ] A second model family (Kimi) on the new tasks.

## Where we are

| task | Pass@1 | |
|---|---|---|
| GT002 | **0.8** | the only unit |
| GT004 | **0.0** | a wall — and still ≤0.2 with every context lever removed |
| GT001, GT005, GT006, GT007, **GT008** | **1.0** | ceilings (GT008 is the first task certified through all four layers) |

Ruled out by measurement (Opus 5, K=5): **prompt wording**, **concealment**,
**conjunction**, **interference** (a closed ticket excusing the defect: 5/5 overruled it),
and **scope closure** (the 2×2 — easy defect stacked on the ticket — came out 1.0 / 1.0).

What is measured hard: **a correct action that looks irreversible.** GT004's second defect
needs 94,097 pre-existing fact rows rewritten; across six context arms (46 episodes) the
model names it in half of them and repairs it in two. Nothing the world *says* changes
that — removing the ticket, the refresh claim and the stale definition together still
gives 0.0. The four-layer gate certifies shortcut-resistance, not difficulty.

## Next

1. **Native-mode comparison**: the same four tasks, no tool list, K=5, against the MCP
   numbers. The tool list was a map; find out whether the map was part of the score.
2. **Build the next instance around the measured lever**: the correct action is the one
   that looks dangerous (withhold a publication past a cutoff, rewrite history, roll back a
   live consumer), with the ledger grading what was published when; give the coupling
   filter a clock so event-type stressors can be certified. Design together first.
3. Register the native-only writes (`alarms_ack`, `deploy_status`) as MCP tools once the
   comparison is measured against the current 63.

## Structure

Every top-level folder carries an `OVERVIEW.md`: purpose, layout, what to read next.

```
world/        world factory: themes, seeds, scheduler, faults, fingerprint, ledger, Airflow bridge
gateway/      every way an agent touches the world: 63 MCP tools, native/ (9 vendor-shaped
              services, tailers, shims), containment P0-P6, docker; NATIVE_SURFACES.md is the spec
tasks/        GT001-GT008 + arms; contracts/ (layer 1), stressors/ (layer 2), harness/ (rollouts,
              grading, blindness, coupling.py = layer 3, ledger.py, native_stack.py)
implementation_structure/   research record: archetypes, sources, failure rulings, TASK_DESIGN_2026-09-15.md
verifier/     append-only verification history
scripts/      track_status.py: re-derives the track table from the tree (0 blockers)
```

Full guide: [`agent.md`](agent.md) — §8.10–8.11 difficulty and the four-layer framework,
§9 contracts and ground rules, §10 architecture, §11 track status, §12 native surfaces.
