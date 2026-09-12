# Round 1 — plan

**Frozen:** 2026-09-11 (Friday) · **Report to Andrew:** 2026-09-15 (Tuesday)
**Mode:** research only — no environment code

---

## What round 1 has to produce

Four artifacts, in priority order. Everything else is optional.

| # | Deliverable | RQ | Gate floor | Owner phase |
|---|---|---|---|---|
| 1 | **10–30 task archetypes**, derived from cited evidence | RQ2 | G3 | strategy |
| 2 | **Connector/mock verdict per app** — what we can replicate offline, proven | RQ3 | G2 + G5 | build |
| 3 | **20–30 app inventory**, ranked by frequency × replication ease | RQ4 | G0 | literature |
| 4 | **Pricing + sizing anchors**, independently sourced | RQ6 | G1 | literature |

Deliverables 1 and 2 are the ones Andrew asked for by name. If the round runs short,
sacrifice 3 and 4.

## Sequence

```
FRI 09-11   Phase 1  Literature x6, blind, parallel
            -> gates G0, G1 (--online)
            Target: >= 40 sourced findings. Below ~25, re-run before proceeding.

SAT 09-12   Phase 2  Formalize x2        -> G0
            Phase 3  Strategy x8, BLIND  -> G0, G3
            Target: first archetype set. Expect G3 dedup failures on the first pass.

SUN 09-13   Phase 4  Build x4            -> G0, G2, G5
            Target: every simulate_target app has a candidate row or an explicit
            none_found, with a real install probe behind every 'easy'.

MON 09-14   Phase 5  Red team x4, blind  -> G4
            Phase 6  Verify x2           -> all gates, promote to state/verified/
            Repair loop on anything that fails. Writer assembles the report.

TUE 09-15   Report to Andrew.
```

## Dispatch

Run gates after **every** phase, not just at the end:

```bash
python gates/run_gates.py                  # offline: G0, G3, G5
python gates/run_gates.py --online         # adds G1 citations, G2 license/liveness
python gates/run_gates.py --online --json  # for the coordinator loop
```

Set `GITHUB_TOKEN` before phase 4 or G2 will rate-limit at 60 requests/hour.

Within a phase, dispatch all agents in **one** parallel batch and do not let any of them
see another's output. Between phases, the blackboard (`state/`) is the only channel.

## Stopping rule

Nothing reaches `state/verified/` below G3. Nothing goes into the Tuesday report as a
recommendation below G4. A finding that fails a gate goes to `state/failures/` with the
reason — it is not deleted, because round 2 must not rediscover it.

## Known round-1 seeds

Already verified real, permissively licensed and actively maintained (checked via the
GitHub API, 2026-09-11 — do not re-verify, do extend):

| Repo | License | Note |
|---|---|---|
| `laude-institute/harbor` | Apache-2.0 | the harness; decided (E1) |
| `tekumara/fakesnow` | Apache-2.0 | Snowflake via **SDK reshape** — breaks if the agent uses a CLI or REST |
| `buremba/universql` | Apache-2.0 | Snowflake via **wire protocol** — more robust, less active (last push 2025-10) |
| `Snowflake-Labs/data-eng-bench` | Apache-2.0 | 103 tasks + synthetic retail dataset; hidden pytest verifier pattern |
| `dbt-labs/ade-bench` | Apache-2.0 | approximate-equality validators; the "sage agent" task-validity check |
| `tobymao/sqlglot` | MIT | dialect transpilation |

`fakesnow` vs `universql` is the SDK-shim-versus-wire-protocol tradeoff in miniature —
B1's summary should take a position on it.

## Open items carried into the round

- **E11 — "Emily Bench"** in the Granola summary is almost certainly a transcription
  artifact for **MLE-Bench**. L2 should confirm and correct. Do not cite the name until
  resolved.
- **E8 — "hundreds to low thousands per task"** is a single meeting anecdote. L5 must
  source it independently or the Tuesday report has to present it as unverified.
- Michael's meeting action item "**toy environment with mocked APIs, before Saturday**"
  is *building*, which this round explicitly excludes. It needs to run as a separate
  track, or be renegotiated with Andrew. Flagged, not resolved.

## What this round does not do

No Dockerfiles. No dbt projects. No MCP server. No generator design. No LLM judge. The
output is a research package that makes the build decisions obvious — not the build.
