# Roster — 28 roles across 6 phases

Phased rounds, not 28 live processes. A full round is roughly **30–100 model calls**.
Dispatch with the Agent tool, one agent per role, in phase order. Within a phase, agents
run **in parallel and blind** — never pass one agent's output to another in the same phase.

Full prompts exist for the roles marked ●. The rest follow `_TEMPLATE.md` and the
per-phase contract below; write them out as the round reaches that phase.

---

## Phase 1 — Literature (6) · blind · RQ1, RQ4, RQ6, RQ7

| ID | Role | RQ | Output |
|---|---|---|---|
| ● L1 | Practitioner workflow — data engineering | RQ1 | `state/knowledge/` |
| ● L2 | Practitioner workflow — data science / ML eng | RQ1 | `state/knowledge/` |
| ● L3 | Practitioner workflow — analytics engineering | RQ1 | `state/knowledge/` |
| L4 | Prior art — Harbor, DataAgentBench, APEX, ade-bench, data-eng-bench | RQ7 | `state/knowledge/prior_art_*` |
| L5 | Competitive landscape + pricing | RQ6 | `state/claims/` |
| L6 | Application inventory (20–30 apps) | RQ4 | `state/inventory/applications.yaml` |

L1–L3 share one prompt (`literature/L1_practitioner_workflow.md`) with a different
discipline and source pool each.

**Contract:** every output is a `source` artifact with a resolving URL and a verbatim
quote. No uncited "known facts." G1 will check every one.

## Phase 2 — Formalize (2) · RQ2, RQ5

| ID | Role | Job |
|---|---|---|
| F1 | Archetype taxonomy formalizer | Turn RQ1 evidence into an exact archetype schema and a category tree. Produce the dependency graph: which archetypes require which apps. |
| F2 | Environment spec formalizer | Turn "what's in a world" into an exact object: required surfaces, initial-condition classes, what must be deterministic. |

**Contract:** output is structure, not findings. F1 and F2 may read all of
`state/knowledge/` but no strategy output (which does not exist yet).

## Phase 3 — Strategy (8) · BLIND · the diversity phase

Each agent gets a **different derivation path**. Never give two strategy agents the same
prompt — that is how you get eight copies of the same four obvious archetypes.

| ID | Derivation path |
|---|---|
| S1 | Archetypes from incident post-mortems — what actually broke |
| S2 | Archetypes from job postings and role definitions — what people are hired to do |
| S3 | Archetypes from **gaps** in existing benchmarks — what ade-bench / data-eng-bench / MLE-Bench do NOT cover |
| S4 | Archetypes from tool documentation — what dbt/Airflow/BI docs say the workflow is |
| S5 | Mock-ability strategy: HTTP interception |
| S6 | Mock-ability strategy: OSS substitution |
| S7 | Mock-ability strategy: SDK reshaping |
| S8 | Initial conditions: synthetic data from distributions, scattered docs, pre-existing brokenness (RQ5) |

**Contract:** S1–S4 emit `archetype` artifacts whose `derived_from` cites real source ids.
S5–S8 emit `claim` artifacts. Nobody reads anybody.

## Phase 4 — Build (4) · artifacts, not prose

| ID | Role | Output |
|---|---|---|
| ● B1 | Connector scout — GitHub dig per app | `state/inventory/` |
| B2 | License + liveness auditor | fills `license`, `last_commit`, `archived`; runs G2 |
| B3 | Rubric prototyper — one 15–30 point rubric for one archetype | `rounds/R001/rubric-draft.md` |
| B4 | Feasibility prober — actually run the install commands | `probe_result` fields; runs G5 |

## Phase 5 — Red team (4) · see the candidate, not each other

Told explicitly: **assume the result is wrong; destroy it.**

| ID | Role | Attacks |
|---|---|---|
| R1 | Leakage hunter | Is this world/data/task already on GitHub or in a training set? Does the archetype resemble a public benchmark task? |
| R2 | Replication skeptic | Will this actually mock cleanly offline? Hidden cloud dependency? Does the shim break when the agent uses a CLI instead of the SDK? |
| R3 | Difficulty skeptic | Would a frontier model shortcut this in three steps? Is "tens of hours" honest? Is there a grep that trivializes it? |
| ● R4 | Citation auditor | Hallucinated sources, quotes that do not appear at the URL, numbers with no provenance, licenses misread |

**Contract:** every attack is written to the target artifact's `attacks:` list and to
`state/failures/` if it lands. Two independent critics finding the same hole is signal;
one critic reading another's finding is noise — so they run blind to each other.

## Phase 6 — Verify (2) · less context, not more

| ID | Role | Job |
|---|---|---|
| V1 | Gate runner | `python gates/run_gates.py --online --json`. Mechanical. No judgment. |
| V2 | Clean-room re-checker | Given only the artifact and the schema — not the transcript, not the producing agent, not the confidence — does it stand up? |

V2 deliberately does not know how a claim was found. That is the point.

## Standing roles (2)

| ID | Role | Job |
|---|---|---|
| C0 | Coordinator | Allocates budget across RQs; ranks by importance × uncertainty × expected information gain; spawns repair agents on gate failure. **Does not do research.** |
| W1 | Writer | Assembles `rounds/R001/REPORT.md` from `state/verified/` only. Nothing ungated reaches Andrew. |

---

## Dispatch pattern

```
Phase 1  →  6 agents in parallel, blind    →  gates G0, G1
Phase 2  →  2 agents                       →  gate  G0
Phase 3  →  8 agents in parallel, BLIND    →  gates G0, G3
Phase 4  →  4 agents                       →  gates G0, G2, G5
Phase 5  →  4 agents in parallel, blind    →  gate  G4 (logged)
Phase 6  →  2 agents                       →  all gates, then promote to verified/
```

Run `python gates/run_gates.py` after every phase, not just at the end. A phase that fails
G0 has produced nothing usable and re-running it costs less than letting bad artifacts
propagate into phase 3 derivations.
