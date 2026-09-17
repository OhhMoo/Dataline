# Alkera-DE Benchmark

A training and evaluation substrate for AI data agents: a data engineer's working
computer — warehouse, dbt, scheduler, BI, issues, chat, wiki — reproducible from a seed,
contained, and graded deterministically on the state the agent leaves behind.

## Tasks and pass rates

Opus 5, K=5, MCP driver, host topology. K=5 gives ±0.18 at p=0.5; treat every number as a
screen.

| task | type | Pass@1 |
|---|---|---|
| GT001 aov-halving | ratio KPI halved by a non-idempotent retry | 1.0 |
| GT002 orders-flatline | stalled stream consumer, append-only sink | 0.8 |
| GT003 tuesday-figure | torn read during a mart publish; snapshot is a record | **0.2** (D5/D7 regrade of `mcp-20260917-k5`; was 0.0) |
| GT004 doubling-window | doubled window + historical rows under an old layout | **0.0** default; **0.2** no-ticket / no-refresh; `authorized` **no number** (5/5 `harness_error`, spend limit; 2/3 completed episodes repaired G1c) |
| GT005 assertion-never-ran | `.dbtignore`d assertion, green builds | 1.0 |
| GT006 quality-gate-drops-rows | NULL-unsafe quality rule | 1.0 |
| GT007 ran-long-or-started-late | SLA miss with two opposite fixes, canaried deploy | 1.0 |
| GT008 warehouse-cutover | legacy→nova migration while the feed keeps delivering | 1.0 |
| GT009 settlement-cutoff | recover a gap while publishing to a bank at a nightly cutoff; restatement decision | **0.2** (`clean-20260917-k5`, valid; 8/8/8/8/18; D1–D3 at 1.0 ×5, wall is R1–R4) |

Native mode (bash only, no tool list): GT004 0.25 on host, n=4, confounded; no container
number yet. Second family (Kimi): no valid Pass@1. GT003 Kimi K=5 died on quota
(`kimi-20260917-k5`, 5/5 `harness_error`); two reports regraded under D5/D7 to 17/16 of 20.

## Structure

```
world/        world factory: themes, seeds, scheduler, faults, fingerprint, ledger
gateway/      63 MCP tools, native surfaces, containment, docker
tasks/        GT001-GT009, contracts/, stressors/, harness/
scripts/      track_status.py
```

Full guide: [`agent.md`](agent.md). Current state, findings and run plan: `STATUS.md`
(local, not committed).
