# implementation_structure

A verifier-first research swarm for the open questions in `agent.md` §6.

**Round 1 is research only.** No environment code, no Dockerfiles, no MCP server. The
output is a research package: task archetypes, a connector feasibility verdict, an app
inventory, and pricing anchors — each one gated by a machine, not by another model's
opinion.

---

## The principle

> **Agents generate; machines judge.**

Every artifact must survive a check that does not consult a language model:

- a cited URL resolves **and** the quoted span literally appears on the page
- a repo's license comes from the GitHub API, not from a README badge
- "easy to replicate" requires an install command that actually ran
- an archetype must cite the source ids it was derived from, or it is an invention

An agent's confidence is never evidence.

## Layout

```
ARCHITECTURE.md      topology, roster, gates, why blind generation
ROUND_PLAN.md        round 1: what ships, when, in what order   <- start here
state/               the blackboard - typed files, no chat history
  problem.yaml       frozen problem spec; every agent reads it
  knowledge/         literature findings   (source schema)
  claims/            assertions            (claim schema)
  inventory/         apps and mocks        (tool schema)
  archetypes/        task archetypes       (archetype schema)
  failures/          what died and why - so round 2 doesn't redo it
  verified/          passed all gates
schemas/schemas.json the four artifact schemas, enforced at G0
gates/run_gates.py   the machine verdict - stdlib only, no install
roles/               agent prompts; roster in roles/README.md
examples/            one worked example per artifact type
rounds/              per-round reports, append-only
```

## Running the gates

Stdlib only — no `pip install` required.

```bash
python gates/run_gates.py                  # offline: G0 schema, G3 coverage, G5 probes
python gates/run_gates.py --online         # adds G1 citations, G2 license/liveness
python gates/run_gates.py --gate G1        # one gate
python gates/run_gates.py --online --json  # machine-readable, for the coordinator
```

Exit 0 = passed. Set `GITHUB_TOKEN` before running G2 at volume (60 req/hr unauthenticated).

| Gate | Checks | Mechanical |
|---|---|---|
| G0 | schema conformance | yes |
| G1 | URL resolves **and** quote string-matches | yes |
| G2 | license identified, not AGPL/SSPL/BSL, repo alive, not archived | yes |
| G3 | archetype count, discipline coverage, near-duplicates, derived_from resolves | yes |
| G4 | ≥ 2 independent red-teamers failed to break it | logged |
| G5 | install probe actually ran and passed | yes |
| G6 | human sign-off | logged |

Nothing reaches `state/verified/` below G3. Nothing reaches Andrew as a recommendation
below G4.

## Dispatching a round

See `ROUND_PLAN.md` for the schedule and `roles/README.md` for the roster. The shape:

```
Phase 1  Literature  x6   blind, parallel   -> G0, G1
Phase 2  Formalize   x2                     -> G0
Phase 3  Strategy    x8   BLIND, parallel   -> G0, G3
Phase 4  Build       x4                     -> G0, G2, G5
Phase 5  Red team    x4   blind, parallel   -> G4
Phase 6  Verify      x2                     -> all, promote to verified/
```

Two rules that do the real work:

1. **Strategy agents never see each other.** Eight agents that can read each other produce
   one answer eight times. Diversity is the entire product of phase 3.
2. **The verifier gets less context, not more.** V2 sees the artifact and the schema — not
   the transcript, not which agent produced it, not how confident that agent was.

Run the gates after every phase. A phase that fails G0 has produced nothing usable, and
re-running it is cheaper than letting bad artifacts propagate into phase-3 derivations.

## Status

Scaffolding complete and tested; `state/` is empty pending round 1.
Round 1 is planned but **not yet dispatched**.
