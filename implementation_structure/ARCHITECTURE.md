# Research swarm — architecture

*Created 2026-09-11. Applies the verifier-first multi-agent research pattern to the open
research questions in `agent.md` §6. Round 1 is **research only** — no environment code.*

---

## 1. The one principle

> **Agents generate; machines judge.**

Not: agents generate, and other agents say whether they like it. Every artifact this swarm
produces must survive a check that does not involve asking a language model for its
opinion.

For a *research* swarm the machine checks are different from a proof swarm's Lean kernel,
but they are just as mechanical:

| Failure mode we are defending against | Machine check |
|---|---|
| Hallucinated citation | URL resolves **and** the quoted span appears in the fetched text |
| "This repo is MIT, we can copy it" | `LICENSE` file fetched and identified via the GitHub API |
| Dead or abandoned project | last-commit date, stars, archived flag |
| "This connector is easy to mock" | `pip install` / `docker pull` actually succeeds in a clean container |
| Invented numbers | every numeric claim carries a source + exact quote |
| Duplicate / non-covering archetype list | schema validation, near-duplicate detection, category coverage count |

An agent's confidence is never evidence. A resolving URL with a matching quote is.

## 2. Topology — blind generation, then attack, then machine verdict

Agents do **not** share a chat log. The documented failure mode of free-form multi-agent
discussion is convergence plus hallucinated consensus, and it is exactly what we would get
if eight strategy agents could read each other while proposing task archetypes: they would
collapse onto the same four obvious ones.

```
                        ┌──────────────┐
                        │ COORDINATOR  │  allocates budget; does not do research
                        └──────┬───────┘
                               │ reads state/problem.yaml
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        PHASE 1  LITERATURE  (6 agents, blind to each other)
        what is already known? -> state/knowledge/*.yaml
                               │
                               ▼
        PHASE 2  FORMALIZE  (2 agents)
        fuzzy question -> exact schema + dependency graph
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        PHASE 3  STRATEGY  (8 agents, BLIND — the diversity phase)
        each assigned a DIFFERENT derivation path, never the same prompt
                               │
                               ▼
        PHASE 4  BUILD  (4 agents)
        produce artifacts, not prose: inventories, probes, rubric drafts
                               │
                               ▼
        PHASE 5  ATTACK  (4 red-team agents)
        see the candidate; do NOT see each other. "Assume it is wrong."
                               │
                               ▼
        PHASE 6  MACHINE VERDICT   python gates/run_gates.py
                          ┌────────┴────────┐
                          │   PASS / FAIL   │
                          └────────┬────────┘
                               │ FAIL -> state/failures/, repair, back to PHASE 5
                               ▼
                          state/verified/
                               │
                               ▼
                          WRITER -> rounds/RNNN-report.md
```

Three rules that make this work:

1. **Strategy agents are blind during generation.** They never receive another strategy
   agent's output. Diversity is the product of phase 3; sharing destroys it.
2. **Critics see the candidate but not each other.** Two critics who independently find
   the same hole is strong signal. Two critics who agree because one read the other is
   noise.
3. **The final verifier has *less* context, not more.** It does not know which agent
   produced a claim or how confident that agent was. It gets the artifact and the gate
   script.

## 3. The blackboard — `state/`

No agent reads another agent's transcript. All coordination happens through typed files on
disk, so that context stays bounded no matter how many rounds accumulate.

```
state/
  problem.yaml            the research questions, frozen per round
  knowledge/              literature findings        -> source.schema.json
  claims/                 assertions with provenance -> claim.schema.json
  inventory/              tools/connectors           -> tool.schema.json
  archetypes/             task archetypes            -> archetype.schema.json
  failures/               what was tried and died, and why
  verified/               artifacts that passed all gates
```

`failures/` is load-bearing and easy to skip. A connector that turned out to be
AGPL-licensed, or an archetype that a red-teamer showed a frontier model solves in three
steps, must be recorded — otherwise round 3 rediscovers it and spends the budget again.

## 4. What the coordinator does

It allocates compute. It does not do research.

```python
while budget_remaining:
    open_qs   = state.unresolved()
    ranked    = rank(open_qs, by=(importance, uncertainty, expected_info_gain))
    results   = dispatch_parallel(ranked[0], n_agents=k, blind=True)
    verdict   = run_gates(results)          # machine, not model
    state.update(results, verdict)
    if verdict.failed:  spawn_repair(results, verdict.reasons)
    if verdict.passed:  promote_to_verified()
```

## 5. Tournament budgeting

Do not spend equally across hypotheses. Round 1 goes wide and cheap; later rounds
concentrate on what survived.

```
ROUND 1   all candidates, shallow      H1 H2 H3 H4 H5 H6 H7 H8   (1 unit each)
ROUND 2   survivors, deeper            H2 ####  H5 ####  H7 ####
ROUND 3   finalists, adversarial       H5 ########  H7 ########
```

For us, "hypotheses" in round 1 are the eight independent derivation paths toward the task
archetype list (§ roles/strategy) and the candidate mock-ability strategies.

## 6. Roster — 28 roles, ~24 dispatched per round

Phased rounds, not 28 concurrent processes. Expect **30–100 model calls per full round.**

| Phase | Count | Roles |
|---|---|---|
| Literature | 6 | practitioner-workflow DE / DS / analytics, prior-art benchmarks, competitive + pricing, tooling inventory |
| Formalize | 2 | archetype-taxonomy formalizer, environment-spec formalizer |
| Strategy | 8 | archetypes from post-mortems / job specs / benchmark gaps / tool docs; mock-ability via HTTP interception / OSS substitution / SDK reshaping; synthetic-data strategy |
| Build | 4 | connector scout (GitHub), license + liveness auditor, rubric prototyper, feasibility prober |
| Red-team | 4 | leakage hunter, replication skeptic, difficulty skeptic, citation auditor |
| Verify | 2 | gate runner, clean-room re-checker |
| Coordinate | 1 | coordinator |
| Write | 1 | report writer |

Full prompts in `roles/`. The roster and phase map live in `roles/README.md`.

## 7. Gates

Adapted from the G0–G6 ladder to a research deliverable. `gates/run_gates.py` implements
G0–G3 and G5 mechanically; G4 and G6 are logged human/agent verdicts.

| Gate | Name | Mechanical? | Check |
|---|---|---|---|
| G0 | Schema | yes | every artifact validates against its schema |
| G1 | Citation resolves | yes | `source.url` returns 200 **and** `quote` appears in the body |
| G2 | License + liveness | yes | GitHub API: LICENSE identified, last commit, archived flag, stars |
| G3 | Coverage + dedup | yes | category coverage met, count target met, no near-duplicates |
| G4 | Adversarial | logged | >= 2 independent red-teamers failed to break it |
| G5 | Replication probe | yes | `pip install` / `docker pull` succeeds in a clean container |
| G6 | Human review | logged | Michael / Andrew sign-off |

Nothing reaches `state/verified/` below G3. Nothing goes to Andrew as a recommendation
below G4.

## 8. What this swarm is explicitly not for

Not for writing environment code, Dockerfiles, dbt projects, or the MCP gateway. Round 1
produces **research artifacts only**: inventories, archetypes, sourced claims, and a
feasibility verdict per connector. Build decisions come after Andrew reviews.
