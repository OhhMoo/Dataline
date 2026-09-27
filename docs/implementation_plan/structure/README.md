# implementation_structure/ — the research record

Everything the task designs rest on, kept where a later round can check it: the
archetype corpus (what practitioners actually hit), its sources and the audit that damaged
some of them, the failure rulings (what a round killed so the next does not rediscover
it), and the design notes that became code. Read `agent.md` §9.5 before trusting any count
in here.

```
TASK_DESIGN_2026-09-15.md   the four-layer framework + the interference / 2x2 experiment (built: agent.md §8.11)
state/                      the blackboard — typed files, no chat history
  problem.yaml              frozen problem spec; every agent read it
  knowledge/  claims/       literature findings (source schema); assertions (claim schema)
  inventory/  archetypes/   apps and mocks (tool schema); task archetypes (archetype schema)
  failures/                 what died and why
  verified/                 passed all gates
schemas/schemas.json        the four artifact schemas, enforced at G0
gates/run_gates.py          the machine verdict — stdlib only
roles/                      the agent prompts; roster in roles/README.md
examples/                   one worked example per artifact type
rounds/R001..R005/          per-round reports, append-only (incl. the citation-audit verdict, FAULTS, LIVE-WORLD, the R005 trajectory analysis)
```

Sections below: how the swarm ran (the method), the gates, and round 1's plan as it was
frozen — kept as the record of what the corpus was produced under.

## The method

> **Agents generate; machines judge.**

Not: agents generate and other agents say whether they like it. Every artifact must
survive a check that does not consult a language model. An agent's confidence is never
evidence; a resolving URL with a matching quote is.

| failure mode | machine check |
|---|---|
| hallucinated citation | URL resolves **and** the quoted span appears in the fetched text |
| "this repo is MIT, we can copy it" | `LICENSE` fetched and identified via the GitHub API |
| dead or abandoned project | last-commit date, stars, archived flag |
| "this connector is easy to mock" | `pip install` / `docker pull` succeeds in a clean container |
| invented numbers | every numeric claim carries a source + exact quote |
| duplicate / non-covering archetype list | schema validation, near-duplicate detection, category coverage |

**Topology: blind generation, then attack, then machine verdict.** Agents do not share a
chat log. Free-form multi-agent discussion converges and hallucinates consensus; eight
strategy agents reading each other would collapse onto the same four archetypes.

```
COORDINATOR   allocates budget; does no research; reads state/problem.yaml
  Phase 1  LITERATURE  x6  blind      what is known?            -> state/knowledge/     G0 G1
  Phase 2  FORMALIZE   x2             question -> schema + graph                         G0
  Phase 3  STRATEGY    x8  BLIND      each a different derivation path                   G0 G3
  Phase 4  BUILD       x4             artifacts, not prose: inventories, probes, rubrics  G0 G2 G5
  Phase 5  ATTACK      x4  blind      "assume it is wrong"                               G4
  Phase 6  VERDICT     python gates/run_gates.py  -> PASS -> state/verified/ -> rounds/RNNN
                                                 -> FAIL -> state/failures/, repair, back to 5
```

Three rules: strategy agents never see each other's output (diversity is the product of
phase 3); critics see the candidate but not each other (two independent critics finding
the same hole is signal; one reading the other is noise); the final verifier has *less*
context, not more (the artifact and the gate script, not the transcript or the producing
agent's confidence).

**The blackboard.** No agent reads another's transcript; coordination is typed files in
`state/`, so context stays bounded however many rounds accumulate. `failures/` is
load-bearing: a connector that turned out AGPL, or an archetype a red-teamer showed a
frontier model solves in three steps, is recorded so the next round does not spend the
budget again.

**Budgeting** is a tournament, not equal spend: round 1 wide and shallow over every
candidate; later rounds concentrate on survivors. The roster is 28 roles, ~24 dispatched
per round in phases (30–100 model calls per full round); prompts in `roles/`.

## The gates

`gates/run_gates.py` implements G0–G3 and G5 mechanically; G4 and G6 are logged verdicts.

| gate | checks | mechanical |
|---|---|---|
| G0 | schema conformance | yes |
| G1 | `source.url` returns 200 **and** `quote` string-matches the body | yes |
| G2 | license identified (not AGPL/SSPL/BSL), repo alive, not archived | yes |
| G3 | archetype count, discipline coverage, near-duplicates, `derived_from` resolves | yes |
| G4 | ≥ 2 independent red-teamers failed to break it | logged |
| G5 | install probe actually ran and passed | yes |
| G6 | human sign-off | logged |

Nothing reaches `state/verified/` below G3; nothing goes out as a recommendation below G4.

```bash
python gates/run_gates.py                  # offline: G0, G3, G5
python gates/run_gates.py --online         # adds G1 citations, G2 license/liveness
python gates/run_gates.py --gate G1
python gates/run_gates.py --online --json  # for the coordinator loop
```

Stdlib only. Set `GITHUB_TOKEN` before G2 at volume (60 req/hr unauthenticated). Run the
gates after every phase: a phase that fails G0 has produced nothing usable.

## Round 1 as frozen (2026-09-11; report 2026-09-15) — research only

Four deliverables in priority order: (1) 10–30 task archetypes derived from cited
evidence (RQ2, floor G3); (2) a connector/mock verdict per app — what can be replicated
offline, proven (RQ3, G2 + G5); (3) a 20–30 app inventory ranked by frequency ×
replication ease (RQ4, G0); (4) pricing and sizing anchors (RQ6, G1). No environment code,
Dockerfiles, dbt projects, MCP server, generator design or LLM judge.

Seeds verified via the GitHub API on 2026-09-11 (permissive, maintained):
`laude-institute/harbor` (Apache-2.0; the harness), `tekumara/fakesnow` (Apache-2.0;
Snowflake via SDK reshape — breaks if the agent uses a CLI or REST), `buremba/universql`
(Apache-2.0; Snowflake via wire protocol — more robust, less active),
`Snowflake-Labs/data-eng-bench` (Apache-2.0; 103 tasks, hidden pytest verifier),
`dbt-labs/ade-bench` (Apache-2.0; approximate-equality validators, "sage agent" validity
check), `tobymao/sqlglot` (MIT). Open items carried in: "Emily Bench" as a likely
transcription of MLE-Bench; the "hundreds to low thousands per task" anecdote needing an
independent source; the "toy environment with mocked APIs" action item being *building*,
which the round excluded.

**What happened.** Rounds R001–R005 ran (`rounds/`); the corpus is what `state/` holds.
Do not read its counts as validated (`agent.md` §9.5): 11 archetypes rest on sources the
citation audit damaged (`rounds/R001/CITATION-AUDIT-VERDICT.md`); of the practitioner
sources sampled for semantic review, roughly 47% failed despite a perfect verbatim quote
match — mechanical verification is not quality; **nothing has been promoted to
`state/verified/`**; G4 verifies that attack records are cross-linked, not that an
archetype survived two attacks, and G5 confirms a probe was recorded, not executed. The
build decisions were taken from the round reports; the four-layer task framework
(`TASK_DESIGN_2026-09-15.md`) is what the later rounds and the task packages under `tasks/`
were built against.
