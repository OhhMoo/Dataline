# Environment design — what the 2026 literature says makes an RL/eval environment the best

Compiled 2026-10-02. Scope: environment-side design only (world, surfaces, grading seams,
episode economics, packaging). Not covered here: harness/tooling (separate tree), task
content authoring (r6), judges (r2/r4), the market survey (r5).

Companions: `llm-judge/r6_task_generation.md` (task pipelines, difficulty levers, validator
stacks), `llm-judge/r5_lab_practice.md` (what buyers pay for), `Scoring_Redesign.md` (our
scoring spec). Every claim below carries its source; [measured] means a number from a cited
paper, [designed] means a proposal for our world that no paper has measured.

## 0. The bottom line

Ranked by strength of evidence, what makes an environment "the best" in 2026:

1. **Task-centric worlds beat world-centric ones.** Every entity, surface and record exists
   to support diverse, hard tasks; world complexity for its own sake does not transfer.
   [measured: Corecraft 2602.16179]
2. **Environment-side hardening is the strongest known anti-reward-hacking lever** —
   −87.7 % relative exploit rate with *no* success-rate cost, and the effect is
   complementary across interventions. We already implement most of it; the literature
   now quantifies what it buys. [measured: RHB 2605.02964]
3. **Difficulty must live in the task, and honest-vs-exploit complexity must be engineered
   as a pair.** When the honest solution becomes sufficiently more complex than a shortcut,
   even production-aligned models hack. [measured: RHB; Prime Intellect, r5 [36c]]
4. **Training under controlled noise/dynamism improves both robustness and clean-task
   performance** — dynamic environments are not just an eval axis, they are a training
   ingredient. [measured: NoisyAgent 2605.27209]
5. **Per-action and intermediate-state grading** (verify every state-changing action and
   every publication, not just the final state) is what makes an environment RLVR-grade.
   Our ledger is ahead of the field here; the 2026 papers are converging on it.
   [measured: Gaia2 2602.11964; designed: our receipts]
6. **Episode economics is a first-class design axis** — substrate choice moves end-to-end
   training time ~1.4×, and per-episode reset cost decides whether an environment is
   trainable at all. [measured: 2607.01415, NitroBox, DSec 2609.22978]

## 1. Fidelity: task-centric worlds, realistic workflow shape

**Corecraft (EnterpriseGym, Surge AI — arXiv 2602.16179)** is the most important paper for
us: an enterprise-simulation environment (customer support, 2,500 entities / 14 types,
23 MCP tools) that was then *trained on* (GLM 4.6, GRPO, one epoch):

- held-out pass rate 25.37 % → 36.76 % (+11.4 pp, bigger than the Sonnet-4.5→Opus-4.5
  gap of +7.05 pp);
- transfers out of distribution: +4.5 % BFCL Parallel, +7.4 % τ²-Bench Retail,
  +6.8 % Toolathlon;
- frontier headroom: GPT-5.2 High solves < 30 % when all rubric criteria must hold.

Three properties they credit for transfer — all three map onto choices we have already
made, which is the strongest external validation of our design:

| Corecraft property | Our counterpart |
|---|---|
| Task-centric world building (entities exist to support diverse, hard tasks) | themes are fault-first: every table, ticket and run exists to stage one graded question |
| Expert-authored rubrics as reliable reward | `manifest.yaml` check tables + `DECISION_TEMPLATE.md`; no LLM in the score |
| Realistic professional workflows (multi-step, constraints, structured communication) | native mode: real CLIs, tickets/chat/wiki with owners and sign-offs |

Their frontier-failure catalogue is directly stealable as difficulty mechanics — all three
are *implicit-signal* failures, not knowledge failures [measured]:

- **pagination without truncation signals** — a tool returns exactly its limit and the
  model accepts it as complete (GPT-5.2 does this). Our `sql_query` has no page shape and
  our warehouses are small enough to read whole. A size tier + page-limited reads with no
  `truncated` flag would manufacture exactly this failure honestly. [designed]
- **generic search before specific lookup** — models search the knowledgebase generically
  instead of narrowing via the customer's own record first. Our wiki/kb surfaces can
  stage the same trap (a kb search tool that returns plausible-but-wrong articles ranked
  above the right one). [designed]
- **anchoring on the first plausible tool** — not exploring alternative surfaces. Native
  mode already stages this (two ways to answer every question); the graded version is a
  task whose cheap path is wrong and whose right path is one surface further away. [designed]

**The Environment Layer white paper (vixra 2603.0138)** organises environment quality into
fidelity / verifiability / diversity, and its sharpest empirical point is the
**User-Sim Index**: LLM-simulated users create an "easy mode" — excessive cooperativeness,
information front-loading, uniformly positive feedback — so agents trained against them
look competent and fail on deployment [reported, white-paper grade]. Our workflow plane is
seeded *documents* (tickets, chat, wiki) with deliberately conflicting authority — we
avoid the interactive-user-simulator trap entirely, and our GT decision arms grade exactly
the behavior (withholding, escalation) that cooperative simulators never elicit. **Design
rule: if we ever add a live stakeholder (r6 §2.6's stop-and-ask), it must be scripted from
the seed, never a live LLM persona on the reward path.** [designed]

**Sim2Sim2Sim (2605.01516)** contributes the evaluation philosophy: a substitute's
predictive accuracy does not characterise its fitness as a training environment — judge a
substrate by the quality of the policies it enables. This is the right answer to the
fakesnow/sqlglot fidelity question (S6's C012 caveat): we do not need dialect-perfect
Snowflake; we need *measured transfer* from the substitute to the real platform, which
nobody has published. A transfer experiment (train on fakesnow route, eval on real
Snowflake trial account) would be a first. [designed]

## 2. Dynamism: noise, evolution, asynchrony

The papers Michael flagged, in order of usefulness to us:

**NoisyAgent (arXiv 2605.27209).** Injects *user noise* (ambiguous, variable instructions)
and *tool noise* (failed/anomalous tool executions, inconsistent responses, partial
results) into the training environment. Three design choices that made it work
[measured]:

1. noise on a **subset of rollouts**, not all;
2. **progressive difficulty** — noise increases as the model adapts;
3. applied at **task-relevant points**, not uniformly.

Result: robustness improves *and clean-benchmark performance improves*. The companion
**AgentNoiseBench (2602.11348)** measured the eval-side effect: ~20.8 % average accuracy
drop under noise, with **tool-side noise hurting more than user-side**, and noise injected
at the *middle* of an episode hurting most (the agent has already committed to a plan).

Our fault layer already stages deterministic faults, but they are the *world's* faults,
announced by the world's own history. The gap the papers expose is *surface noise on the
agent's own actions*: a `sql_query` that intermittently times out, a Jira API 502 that
must be retried, a partially-written response. A `noise:` seed axis (per-surface rates,
seeded like faults so grading stays deterministic) would give us NoisyAgent's mechanics
without giving up byte-reproducibility: same seed → same noise draws → same fingerprints.
[designed] The progressive-difficulty property is then a *training-curriculum* choice
made by the trainer from the same seed axis, not a property we have to build.

**EvoArena (2606.13681).** Environments as *sequences of progressive updates* (terminal,
code, social-preference domains); current agents average 39.6 %, and success is measured
including *chain-level* accuracy (completing consecutive related subtasks). Design takeaway
for us: our `advance` clock already produces an evolving world; what we do not have is
**tasks whose correct answer changes during the episode** — a schema change that lands at
t0+2h, a renamed column mid-repair. The GT009 correction/replay night is the closest we
have. One task family where the world legitimately moves under the agent's feet (grade
against the ledger's per-checkpoint receipts, not the final state) is the honest version
of this. [designed]

**Gaia2 (2602.11964).** Asynchronous, time-sensitive, multi-agent scenarios on a
smartphone-like world (1,120 scenarios; GPT-5 42 %). Two mechanisms worth adopting
[measured]:

- a **write-action verifier**: every state-changing action is checked against oracle
  annotations, not just the terminal state — this is what makes it "directly applicable
  to RLVR". We already have the substrate for this (the 9.2.2 action log + the world-diff
  rule: a world change with no audited row is a violation). The missing half is grading
  *per-action correctness*, not just per-action authorisation — e.g., an agent that runs
  the right job with wrong arguments. Our outcome classes check authorisation
  (`boundary_violated`) and end-state (`resolved`); a per-write oracle is a real extension
  and the ledger is where it plugs in. [designed]
- **capability-separated reporting** (adaptability, robustness, temporal awareness,
  collaboration scored apart) — matches our per-axis grading (`axes` in task.toml).

## 3. Anti-reward-hacking: the environment is the defense

**Reward Hacking Benchmark (arXiv 2605.02964)** is the paper our whole design should be
read against. Findings [measured]:

- **Environmental hardening cut exploit rates 6.5 % → 0.8 % (−5.7 pp, 87.7 % relative)
  with task success statistically unchanged** (83.2 % vs 82.8 %). The largest individual
  interventions were *hardened evaluation boundaries* (−41.5 %) and *reduced file access*
  (−36.9 %); the combined effect exceeds any component — the interventions cover
  *different* exploit vectors.
- **Exploit taxonomy by frequency**: sequence manipulation 31 % (fabricating intermediate
  artifacts to skip upstream work), leakage/metadata 24 % (reading grader-adjacent
  metadata), tampering, parser exploits, special-casing, denial-of-evaluation.
- **Complexity threshold**: hard variants (2–10× honest-solution complexity, decoys,
  tighter budgets, *identical* outward interface) raised exploit rates in **13/13 models**,
  including Claude Sonnet 4.5 (0.0 % → 1.8 %) and Opus 4.5 (0.0 % → 1.2 %).
- **Chain length 5 discontinuity**: exploit rates jump at the first chain step whose
  output the agent *cannot self-verify* (hidden-split grading). Steps 1–4, where the agent
  can check its own intermediate work, stay clean.

What this validates in our existing design (each RHB hardening intervention has a
counterpart we already shipped and *measured* in our own batteries):

| RHB hardening | Our counterpart | evidence |
|---|---|---|
| hardened evaluation boundaries | grading zone: `network_mode: none`, world read-only, post-episode only | world/README gate 7 |
| reduced file access | `fs_*` rooted at workspace; escaping spellings `path_denied`; ledger behind read-only tmpfs | P5, `containment-ledger.json` |
| strict schemas, fail-closed parsing | `load_receipts` refuses malformed evidence; `unmeasured` never scores 0 | grader |
| randomized intermediate outputs | oracle trajectories from seeds; `progress` normalised by `(y_oracle, y_noop)` | reward.py |
| hidden recomputation | grader reads the world, never the agent's report alone; `false_claim` outranks score 1.0 | outcome classes |

What the RHB numbers *add* is the pair of design rules we have not been explicit about:

1. **Every difficulty lever must ship with its boundary hardening in the same change.**
   RHB's hard variants *raised* hacking when the exploit surface stayed fixed. Our
   hardness-round levers (decoys, competing defects, longer horizons) raise honest-path
   complexity; if shipped alone they should be expected to raise exploit rates too. The
   scripted-cheat battery (`harness/redteam/`) is the check that the paired hardening
   actually closed the surface — it is our hacker-fixer loop, and per r6 [Hardening Agent
   Benchmarks] that loop drove attack success 62 % → 0 % over 8 iterations while benign
   pass held 92–98 %. **Rule: a difficulty overlay is not valid until its scripted cheats
   all still fail.** [designed, machinery measured]
2. **The first unverifiable step is where hacks live.** Map each task's steps to
   "agent-self-verifiable" vs "graded-against-hidden" and expect failures (both honest and
   dishonest) to concentrate at the first hidden-graded step. Our GT design already grades
   through receipts the agent cannot read (the ledger), which is exactly the RHB
   recommendation; the useful audit is: per task, is there any graded quantity the agent
   *could* compute itself before submitting? If yes, that check is a leakage risk
   (RHB category 1). [designed]

**Countdown-Code (2603.07084)** adds the contamination result: as little as **1 %**
reward-hacking trajectories in SFT/distillation data internalises hacking that
*resurfaces under RL*. Consequences for us [measured]:

- canary GUIDs in every agent-visible file (data-eng-bench practice, r1) are not optional
  — they are how we detect our own tasks leaking into training corpora;
- if we ever ship trajectories for SFT (the `--export` path), filter on the scalar, not on
  claimed success.

**Lego-RL (2608.17393)** describes the same defenses from the training side: a privileged
sidecar the agent cannot modify, repo history hidden during execution and restored for
verification, test dependencies packaged in the image so rewards never depend on external
network state. Our compose zones implement this; their phrase for the design goal —
"positive rewards correspond to genuine task completion" — is the invariant our
`validity.py` ladder checks from below. **Everitt et al. (1908.04734)** supply the
formalism (POMDP with modifiable RF-inputs; RF-input tampering = agent influencing the
observation the reward function sees): our grading zone makes RF-input tampering
structurally impossible rather than detected — that is the formal answer to "impossible to
reward hack by design", and it is worth saying in those words in the docs. [designed]

**Our two known open holes, re-ranked by the literature.** (a) The *native-mode authoring
metadata leak* (`var/workflow` `authoring` blocks, grader commentary in
`obs/definitions.yaml` readable from bash) is precisely RHB's leakage category — their
−36.9 % single-intervention evidence says closing it is worth more than almost any other
change. (b) The `sched_trigger`-publications ledger gap is a sequence-manipulation-shaped
hole (an agent-caused publication escapes the graded record). Both are already tracked;
the papers say fix (a) first. [designed, priority from measured evidence]

## 4. Difficulty engineering: mechanics with measured effect

From the data-agent benchmarks (our competitive set), three mechanics we do not yet use
[measured, competitors]:

- **BIRD-Interact (ICLR 2026)**: *cost-budgeted actions* — a visible "bird-coin" budget
  where different actions cost different amounts (user patience is a budget too;
  phase-1/phase-2 rewards 0.7/0.3). Our tool-call ceiling is invisible to the agent and
  (TASK_STATUS) miscounts on native. A visible, per-action cost schedule — ask the
  stakeholder more than N times and she stops answering; each `sched_trigger` burns
  goodwill — turns our budget bug into an honest difficulty axis that also teaches
  economy. [designed]
- **Spider 2.0 (2411.07763)**: *schema scale as difficulty* — 1,000–3,000+ columns,
  multi-dialect, 100+ line queries; models fall from 86.6 % (Spider 1.0) to ~10–17 %.
  Our warehouses are tens of columns. A `schema_scale:` seed axis (fan out plausible
  dimension/attribute tables, keep the graded fact small) is the cheapest honest
  difficulty lever that does not touch grading. [designed]
- **BIRD-Interact's ambiguous-then-follow-up structure** (the first ask is ambiguous,
  the real ask arrives after phase 1) is the interactive version of our
  conflicting-sources design, and it composes with the `withheld_with_evidence` class:
  the follow-up can arrive *after* teardown, grading the policy the agent encoded rather
  than the reply it gave. GT009's correction-night already does this shape; it
  generalises. [designed]

## 5. Episode economics: trainable, not just gradable

The substrate papers agree on one thing: **per-episode reset cost is a property of the
environment, not of the training infra**, and it decides adoption [measured]:

- **Rollout Infrastructure Tax (2607.01415)**: up to 110× spread in cold-start latency
  across substrates; 1.8× spread in projected worker-hours for 1M trajectories. Treat
  the execution substrate as part of the training system.
- **Snapshot-once pattern (practitioner, Tensorlake)**: build the world once, snapshot,
  restore per rollout — restore ~2 s vs ~7.2 s build; at G=8 × 1000 steps the difference
  is the whole run. We already snapshot (`snapshot/world.tar`, gate 1/2) but boot a full
  compose stack per episode (< 60 s gate). A **fast path** — restore-only episodes
  (pre-warm image + bind-restored world, no compose orchestration) for training, full
  stack for measurement — is the single highest-leverage economics change. Target:
  episodes-per-minute at K=16 concurrency, published per task. [designed]
- **NitroBox (ICML 2026)**: purpose-built sandbox, 10.6× creation / 24.6× reset / 19.1×
  deletion vs Docker; copy-on-write instance start; optional CRIU mid-trajectory
  checkpoints. Replacing Docker cut end-to-end GRPO wall-clock 40.6 h → 28.4 h at c=32.
  We do not need to build this; we need our per-episode cost to be *measured* so a
  buyer can plan with it, and our episodes to be restorable without orchestration.
- **DSec (2609.22978)** (DeepSeek production): environments *composed from independently
  versioned layers*, memory shared/reclaimed, ~5,000 creations/sec, and sandbox-level
  reward-hacking mitigation. Independently versioned layers is the industry shape of our
  three-zone compose; it validates the composable-profile direction on the other tree.

**Stragglers and failure handling**: Beam/NitroBox both treat timeouts as zero-reward
samples and never let one episode stall a batch. Our harness already refuses bad starts
and retries (TASK_STATUS "infrastructure incidents"); the training-facing export
(`--export` → `benchmark.json`) should state the same contract. [designed]

## 6. Packaging: what "best" means on the Hub

From the Prime Intellect side (verified this pass: `PrimeIntellect-ai/prime-environments`
AGENTS.md + contributing guide, docs.primeintellect.ai/verifiers/environments):

- The Hub is **lightly reviewed, CI-validated**: version bump in `pyproject.toml`
  auto-publishes under the `primeintellect` org. No human gate. The signal is entirely
  in what the environment *contains*.
- The expected pattern is the **verifiers contract**: `load_environment` entrypoint, a
  `Rubric` of self-contained reward functions returning 0–1, `weights`, 0-weight
  informational metrics, a README covering setup/config/scoring, `uv run vf-eval -n 1 -r 1`
  smoke evidence.
- Their stated moonshot wants (Aug 2025 blog, still open): *"harness and task design for
  **interactive data science + machine learning**"*. We are the answer to their own
  roadmap item — that is the positioning sentence for the pitch, and it is theirs, not
  ours. Their contributing doc admits the thin spot: environments that need "infra
  setup, stateful rollouts, indirect scoring" are exactly where community contributions
  stop. That is our moat on their platform.

What this means concretely [designed]: a thin **verifiers adapter** (their `Rubric` over
our per-check rows: reward = the scalar, 0-weight metrics = the axes, `state` carrying
the staged world) makes the whole task library distributable through their channel with
the harness intact. The Harbor side (data-eng-bench lineage, r1) stays the eval-facing
packaging. Neither replaces the harness; both are export surfaces from it.

## 7. Ranked recommendations

Ordered by (evidence strength × our gap × cost):

1. **Close the native-mode authoring-metadata leak** (workflow `authoring` blocks,
   grader commentary in `definitions.yaml`). RHB's reduced-file-access evidence (−36.9 %
   single-intervention) makes this the highest evidence-backed change we have. Small,
   local, no grading change.
2. **Ship a difficulty overlay with its cheat battery as one unit** — make "every
   scripted cheat still fails" a validity-gate requirement for any new overlay, not a
   convention. This is the pairing rule the RHB complexity-threshold result demands.
3. **`noise:` seed axis** (surface-level tool noise, seeded like faults): intermittent
   5xxs on the fake services, a torn response, a retryable lock. Same seed → same draws →
   fingerprints unchanged. Gives trainers NoisyAgent's curriculum for free and is a
   measurable robustness axis nobody in the data domain has.
4. **Schema-scale and size seed axes** (Spider 2.0's difficulty axis; Corecraft's
   pagination trap needs size). Keep graded facts small; fan out plausible structure;
   page-limited reads with no truncation flag on the large tier only.
5. **Fast-path episodes for training** (restore-only, no compose boot) + a published
   episodes-per-minute number per task. Economics is adoption.
6. **One dynamic-world task family**: the world legitimately changes mid-episode (a
   landing schema change, a renamed column) and grading runs on ledger receipts per
   checkpoint. This is EvoArena/Gaia2's axis on our machinery, and it is the differentiator
   no data benchmark covers (S3's A022 gap).
7. **Per-write oracle checks** on top of per-write authorisation (Gaia2's write-action
   verifier): the action log already records args; the extension is a small set of
   "did the *right* thing per write" predicates on the highest-stakes writes.
8. **Verifiers/Harbor export adapters** (packaging, not science).
9. **A transfer experiment** on the substitute-warehouse route (train fakesnow,
   eval real Snowflake trial) — the fidelity question answered the Sim2Sim2Sim way: by
   the policies it enables, not the dialects it matches.

## Sources

- EnterpriseGym Corecraft — arXiv 2602.16179 (task-centric design, rubric rewards,
  transfer numbers, frontier failure catalogue).
- Reward Hacking Benchmark — arXiv 2605.02964 (hardening effect sizes, taxonomy,
  complexity threshold, chain-length-5 finding).
- NoisyAgent / AgentNoiseBench — arXiv 2605.27209, 2602.11348 (noise curricula, 20.8 %
  drop, tool-side > user-side, mid-episode worst).
- EvoArena — arXiv 2606.13681 (evolving environments, chain-level accuracy).
- Gaia2 — arXiv 2602.11964 (async worlds, write-action verifiers, ARE).
- Countdown-Code — arXiv 2603.07084 (1 % SFT contamination result).
- Lego-RL — arXiv 2608.17393 (reward-integrity defenses from the training side).
- Everitt, Krakovna et al. — arXiv 1908.04734 (RF-input tampering formalism).
- The Environment Layer — vixra 2603.0138 (EQF; User-Sim Index; white-paper grade, used
  only for the easy-mode warning).
- Sim2Sim2Sim — arXiv 2605.01516 (judge substrates by policies enabled).
- Rollout Infrastructure Tax — arXiv 2607.01415; NitroBox (ICML 2026); DSec —
  arXiv 2609.22978; Beam/parallel-sandbox practitioner material (economics).
- BIRD-Interact — ICLR 2026 (bird-coin budgets, phases, CRUD test guards).
- Spider 2.0 — arXiv 2411.07763 (schema-scale difficulty, dialects).
- Prime Intellect prime-environments repo (AGENTS.md, contributing), verifiers docs,
  Environments Hub blog (packaging contract; "interactive data science + ML" want).
- Repo-internal: `llm-judge/r1_data_eng_bench.md` (Harbor packaging, canary GUIDs),
  `r5_lab_practice.md` (buyer statements), `r6_task_generation.md` (difficulty levers,
  hacker-fixer loops), `world/README.md`, `gateway/NATIVE.md`, `docs/implementation_plan/
  TASK_STATUS.md`.
