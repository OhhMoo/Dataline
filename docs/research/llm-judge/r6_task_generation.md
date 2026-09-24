# R6 — LLMs as generators, validators and adversaries for hard agentic tasks

Research memo, 2026-09-22. Scope: how others generate difficult agentic tasks at scale with LLMs, how they validate that generated tasks are correct, solvable and discriminative, which difficulty levers actually move frontier pass rates, when a judge is unavoidable versus when a deterministic verifier can be kept, and (briefly) what a model-routing model needs from a benchmark like ours.

Context for the reader: our environment is a seeded, reproducible simulated data stack (DuckDB, dbt, scheduler, Jira/Slack fakes, audit ledger) with nine hand-built scenarios x two arms (control: fix everything; decision: part of the damage belongs to another team, so stop at the boundary and notify with evidence). Grading is deterministic code over final world state. Goal: go from 19 arms to hundreds or thousands of instances without saturating. A teammate's hypothesis: LLM-as-judge is the key to adding the complexity that makes tasks hard. The evidence below mostly says the opposite: the judge's proven role in the best pipelines is as a *validator of generated tasks* and an *adversary against the grader*, while the hardness itself comes from structural levers (hidden blockers, hard-negative distractors, cross-system reconciliation, horizon) that are graded by state-based verifiers.

Items marked **[unverified]** are from memory or a secondary source I could not open in this session.

---

## 0. The bottom line

1. Every scaled pipeline that survived contact with training (SWE-smith, SWE-rebench V2, SWE-Bench++, Skywork-SWE, daVinci-Env, AutoForge, Agent World Model, Self-Challenging Agents, Absolute Zero) uses the same skeleton: **generate -> execute an oracle -> keep only what the oracle proves solvable and non-trivial -> band by reference-policy pass rate -> (optionally) LLM-judge the *specification*, not the *outcome*.** Yields are low (5 to 50 percent of candidates survive) and that is expected.
2. LLM judges are good at one validation job and mediocre at another. They are near-human-precision at flagging clear versus underspecified task text (SWE-Bench++: precision 0.93 to 0.95 vs human 0.96) but low-recall and unstable across judge models (SWE-rebench V2 three-judge consensus: F1 0.11 against SWE-bench Verified human labels; best ensemble F1 0.43). Use them as a conservative filter with consensus, never as the ground truth for whether a task is fair.
3. Adversarial "exploit agents" are the highest-value use of an LLM in the loop: even after Terminal-Bench 2.0's >3 person-hours per task of auditing, 14 of 89 tasks (15.7 percent) remained reward-hackable (Terminal Wrench). A hacker-fixer-solver loop drove KernelBench attack success from 62 percent to 0 percent in 8 iterations at 92 to 98 percent benign pass rate (Hardening Agent Benchmarks, 2026).
4. The levers with the largest measured frontier drops are: hidden blockers that only surface through progressive investigation and require asking (HiL-Bench: -51 to -69 pp for Opus 4.6 / GPT-5.4 on SQL), hard-negative distractors (Lost in the Noise: Gemini 2.5 Pro 77.8 to 48.0), extraneous tools in holistic planning (-22 pp GPT-5), cross-system reconciliation with subtle breakage ("fix" tasks hardest in Snowflake's data-eng-bench; DataAgentBench best 38 percent), and horizon length (per-step error compounds; τ-bench pass^8 < 25 percent for GPT-4o). Irreversibility alone is nearly saturated (WorkBench Revisited: harmful actions 26 percent GPT-4 to 1.9 percent Claude Fable 5).
5. Decision rule (section 4): a criterion goes to a judge only if it cannot be expressed as a predicate over (final state, audit ledger, message log) with a golden value produced by the generator's oracle, and even then only behind a deterministic gate, at capped weight, with an artifact-grounded verifier and held-out human labels. Our decision arm's "notify with evidence" deliverable should be made structured (ticket fields with row IDs, query hashes, owning team) precisely so it stays on the verifier side.

---

## 1. Synthetic environment and task generation for agent RL

### 1.1 Real-repo mining pipelines (where the "oracle" is the merged PR + tests)

| Pipeline | Org / year | Scale | Funnel and validation | Difficulty handling |
|---|---|---|---|---|
| SWE-Gym | Apple, UC Berkeley, UIUC et al. (ICML 2025) | 2,438 tasks, 11 Python repos; SWE-Gym Raw 64,689 issues over 358 repos without environments | Pre-configured executable environments, expert-validated tests; fail-to-pass | Not filtered by pass rate; used to train verifiers (trained verifier + agent = 32.0 percent Verified) |
| SWE-rebench | Nebius (2025) | 21k tasks | ~450k issue-linked PRs from >30k permissive-licence Python repos; LLM-driven interactive setup synthesis; execution-based install verification; fail-before / pass-after check | Ensemble quality filtering; monthly decontaminated eval |
| SWE-rebench V2 | Nebius (Feb 2026) | 32,079 issue-linked + 120k PR-derived tasks, 20 languages, 3,600+ repos | 29,511,758 PRs -> 8,593,722 with tests -> 805,598 issue-linked with tests -> 583,809 after repo filtering -> 41,349 fail-to-pass -> 32,079 after clarity filtering (~77 percent retained at the last gate). Clarity gate = 3 independent LLM judges (gpt-oss-120b, GLM-4.7, DeepSeek-V3.2) applying the SWE-bench Verified 0-3 rubric; keep only if all three say adequate | 300-task diagnostic: Opus 4.5 25.2 percent pass@1 / 32.7 pass@3; GLM 4.7 21.3; DeepSeek v3.2 17.4 |
| SWE-Bench++ | 2025 | 11,133 instances | 137,048 candidate PRs -> 28,513 environments built (20.8 percent) -> 11,133 after QA (8.1 percent). Python 41 percent yield, C++ 9.5 percent. Layer-3 rubric LLM judge scores issue clarity and test-to-issue alignment; Low = reject, Medium = recover with hints, High = keep | Top: Claude Sonnet 4.5 36.2 percent pass@10; GPT-4o 16.9 |
| Skywork-SWE | Skywork (2025) | 10,169 tasks, 2,531 repos | 151,472 repos -> 8,472 with valid metadata -> 146,568 PRs -> 23,389 pass install validation -> 10,169 pass execution validation; 8,447 successful trajectories -> 8,209 after format filtering | Log-linear scaling of SWE-bench Verified score with trajectory count, "no signs of saturation" at 8.2k |
| SWE-Factory | 2025 | 337 valid instances from 671 issues (50 percent), 4 languages | Multi-agent SWE-Builder sets up environments; exit-code-based fail2pass log parsing, F1 0.99 vs manual inspection; $0.047 per instance | none reported |
| daVinci-Env | 2026 | 45,320 Docker environments over 12.8k repos; ~13k curated trajectories from ~9k quality-guaranteed environments | 64-node multi-agent cluster builds Dockerfiles, eval scripts, test analysis; total cost ~$1.47M ($891k environment construction, $576k trajectory sampling and curation) | "Quality-centric filtering pipeline that characterizes the inherent difficulty of each environment"; removes unsolvable and insufficiently challenging instances; OpenSWE-32B/72B reach 62.4 / 66.0 percent Verified |

Takeaway: the yield from raw candidates to validated tasks is 8 to 50 percent, and the binding constraint is not generation but *execution-based validation*. Everyone treats "the golden patch makes the failing test pass" as the only acceptable proof of solvability.

### 1.2 Synthetic-bug pipelines (where the oracle is the pre-mutation code)

**SWE-smith** (Stanford, Princeton et al.; NeurIPS 2025 D&B spotlight). 50k instances from 128 repos, 295 GB total (vs. an estimated 50 to 150 TB if each task carried its own image), total build cost $1,360. Four bug-synthesis strategies, each validated by executing the repo's tests and keeping only candidates that break at least one test:

| Strategy | Yield (candidates that broke tests) | Instances | Cost per candidate |
|---|---|---|---|
| Combine (compose two validated bugs) | 96.9 percent | 10,092 | $0 |
| LM Modify (small LM edit) | 56.0 percent | 17,887 | 0.38 c |
| Procedural (AST mutation) | 40.2 percent | 15,641 | $0 |
| LM Rewrite (LM rewrites function) | 35.0 percent | 4,173 | 3.93 c |
| PR Mirror (undo a real PR) | 33.8 percent | 2,344 | 5.53 c |

Issue text is LM-generated from (diff, one failing test's source, test output) at 2.54 c per issue; training on LM-written issues matched training on real issues (7.7 vs 7.8 percent resolve in the ablation). A difficulty rater trained on human pairwise labels (75.3 percent accuracy) splits tasks into easy / medium / hard with agent resolve rates 58.6 / 41.0 / 17.0 percent. Critical finding for us: "difficulty correlates with solvability but not with effectiveness as training data": SFT sets built at different difficulty levels landed within 10.8 to 13.6 percent, while PR-mirror-derived trajectories (most realistic, not hardest) were the most effective. LM-Modify tasks showed the steepest drop-off, associated with agents repeating actions.

**R2E-Gym** (COLM 2025). 8.1k executable tasks. SWE-Gen back-translates commits into issue text and synthesizes tests, avoiding dependence on human-written issues. Hybrid verifiers: execution-based and execution-free (trajectory-reading) verifiers each saturate at 42 to 43 percent for test-time selection, combined 51 percent Verified; base agent 34.4 percent at 32B.

### 1.3 Self-play task proposers (where the oracle is generated with the task)

**Absolute Zero (AZR)** (Tsinghua, BIGAI et al., 2025). One model proposes and solves code-reasoning tasks (deduction: predict output; abduction: infer input; induction: synthesize program). Validity filters on every proposal: program integrity (executes and returns), safety (blocked packages), determinism (identical output across j = 2 independent runs). Proposer reward is the learnability signal `r_propose = 0 if mean_solve = 0 else 1 - mean_solve` over G solver rollouts, i.e. reward tasks the current solver sometimes but not always solves. AZR-Coder-7B averages 50.4 across math and code with zero curated data.

**Self-Challenging Language Model Agents (SCA)** (Meta FAIR, UC Berkeley; NeurIPS 2025). The Code-as-Task format is the cleanest statement of "generated task must come with its own verifier": each task = instruction + executable verification function + example solution + three enumerated failure cases. Rejection rules: verifier must execute; example solution must pass; all three failure cases must fail. Survival: verifier-only 47.7 percent -> plus example solution 9.5 percent -> full CaT 5.2 percent. On 50 human-annotated trajectories, verifier-only tasks still had high false positives, solution-filtered tasks removed false negatives but not false positives, and only full CaT removed both. Downstream: Llama-3.1-8B average pass@1 12.0 -> 23.5 percent across M3ToolEval and τ-bench (τ-bench retail/airline 5.7 -> 8.6). Note the message: 95 percent of self-proposed tasks are garbage and the failure-case requirement is what catches ambiguous verifiers.

**Propose, Solve, Verify (PSV)** (CMU, Dec 2025). Same loop with a formal verifier (Verus) as the oracle; pass@1 up to 9.6x over expert iteration; removing verification costs up to 50 percent pass@1. Relevant as the extreme end of "the oracle must be sound."

**GenEnv** (Dec 2025). An environment-LLM generates task batches conditioned on the agent's recent success; its reward is `R_env = exp(-beta * (p_hat - alpha)^2)` with alpha = 0.5, i.e. an explicit target pass-rate band. +40.3 percent over 7B baselines on ALFWorld with 3.3x less synthetic data than offline Gemini-2.5-Pro augmentation.

**AgentGen** (2024). Environments from an "inspiration corpus", tasks evolved in both easier and harder directions (Bi-Evol) to get a smooth difficulty curve.

### 1.4 Environment synthesis for tool-use agents (closest structural analog to our world)

**AutoForge** (Dec 2025). Starts from tool documentation only. Environment state is a key-value store; Python tool implementations are generated over it. Tasks are built by sampling tool DAGs (input/output compatibility graph, sequence merging, inserted reasoning nodes), instantiating values, executing the gold DAG to obtain target state S*, then rewriting the instruction to contain "only the minimal information necessary". Reward is `R = 1 iff S* == S_hat` (final-state equality), not trajectory matching. An LLM judge is used only to mask trajectories where the *simulated user* hallucinated. 10 environments / 1,078 high-difficulty tasks; DAPO dynamic sampling drops prompts whose rollouts are all-correct or all-wrong. τ-bench retail 67.8 -> 73.1 (30B).

**Agent World Model (AWM)** (Feb 2026). 100 domains -> 1,000 scenarios -> 1,000 code environments (SQLite schemas, avg 18.5 tables, 35,062 MCP tools, ~1,985 lines each) -> 10,000 tasks. Verification is a *code-augmented* LLM judge: DB state is inspected before/after execution and the diff plus task is judged into {Completed, Partial, Agent Error, Environment Error}; 95.5 percent pairwise agreement, Fleiss kappa 0.891. Quality audit of 100 environments: task feasibility 3.99/5, toolset completeness 4.98/5, and 83 percent contain minor edge-case bugs. Training on 526 environments / 3,315 tasks: BFCLv3 +12.1, τ²-bench +7.0 (8B).

**EnvScaler** (ACL Findings 2026): 191 environments, ~7k scenarios via programmatic skeleton synthesis. **AutoEnv** (Nov 2025): 36 environments / 358 validated levels at $4.12 per environment; seven models score 12 to 49 percent normalized reward; the gain of any single learning method decays as environment count grows.

### 1.5 Procedural families and hubs

- **Reasoning Gym** (NeurIPS 2025 spotlight): 100+ procedural generators with algorithmic verifiers and a difficulty knob per generator; unbounded instances, used for curriculum RL.
- **TextArena** (2025): 57+ competitive text games, TrueSkill leaderboard; difficulty is supplied by the opponent pool rather than by task design.
- **Prime Intellect Environments Hub + verifiers** (2025; verifiers v1 July 2026). An environment is packaged as dataset + parser + rubric, where a rubric is a list of weighted reward functions (code-based or judge-based) **[structure from general familiarity with the library; the docs page 404'd in this session]**. Bounties are posted on a difficulty scale. INTELLECT-2 (2025): difficulty estimated with 8 samples from DeepSeek-R1-Distill-Qwen-7B, keep solve rate <= 75 percent; on DeepScaleR, dropping problems with pass@8 above 50 percent or below 12.5 percent "improved rewards significantly". INTELLECT-3 (Dec 2025): per-problem solve rate from 16 generations of Qwen3-4B-Instruct sorts problems into easy / normal / hard pools; trivial rollouts are discarded online.

### 1.6 Difficulty bands: what people actually use

- Theory: **Online Difficulty Filtering for Reasoning-Oriented RL** (2025) shows expected policy improvement is lower-bounded by the variance of task-level success probability, so balanced filtering (keep intermediate p) maximizes the bound; up to +12 percent over GRPO in under half the steps.
- Practice, from technical reports: keep prompts with 1 to 7 correct out of 8 rollouts; or K = 16 with bounds 1/16 to 15/16 early, tightened to K = 8 with 1/8 to 6/8 later; curriculum variants first profile every task with the SFT checkpoint and remove anything already at 100 percent, then drop tasks above 0.7 as training proceeds (LiteResearcher, Motif-2, Nemotron 3 Nano reports; see references).
- Vendor guidance (Epoch AI's RL-environment FAQ, 2026): the floor buyers want is about 2 to 3 percent pass, "at least one success out of 64 or 128 rollouts", with a smooth gradient of difficulty across the set; per-task prices $200 to $2,000, website replicas ~$20k, a Slack-class product ~$300k; "soundness matters most: high reward must mean the task was actually solved, not hacked"; "maintaining quality while scaling is the number one bottleneck."

For us: band by *our own* reference policies (a cheap open model, a mid model, a frontier model), regenerate the bands per model release, and keep the distribution wide rather than pushing everything to the hard end (SWE-smith's finding that hard != most useful).

---

## 2. LLM judges as task validators (not graders)

### 2.1 Specification clarity: the human standard and how LLMs compare

- **SWE-bench Verified** (OpenAI, 2024): 93 developers screened 1,699 samples, three annotators each, scoring underspecification 0-3 and test validity 0-3; 500 samples kept. The authors estimate a residual 5 to 10 percent error rate; Epoch AI later dropped 16 more samples that did not run reliably. The ABC audit (below) still finds insufficient tests, with 24 percent of the top-50 leaderboard entries reported as incorrect. So even the gold-standard human process leaks.
- **LLM re-implementation of that rubric**: SWE-rebench V2's three-judge unanimous consensus scores precision 0.88 but F1 0.11 against the Verified human labels (the judges agree with humans on what is clear but throw away most of what humans accepted); the best mixed ensemble reaches F1 0.43, a single judge 0.34. SWE-Bench++'s per-model comparison against senior human annotators (P 0.964 / R 0.991): Claude Sonnet 4 P 0.926 / R 0.921, Gemini 2.5 Pro 0.946 / 0.884, GPT-5 0.952 / 0.370. Read this as: precision is nearly human, recall is judge-specific and can be very low; use judges to *reject* with high confidence, not to *certify*.
- **Can models tell a task is underspecified?** Ambig-SWE (ICLR 2026): with the best prompt, Claude Sonnet 4 detects underspecification at 89 percent accuracy (FP 3 percent, FN 18 percent); Sonnet 3.5 84 percent (FP 24 percent); most other models near chance. Interaction recovers the loss (Sonnet 3.5: full spec 49.6, underspecified 37.9, underspecified + interactive 59.5 percent resolve). This is both a validator result (a strong judge can flag underspecified generated instructions) and a difficulty-lever result (section 3).

### 2.2 Terminal-Bench 2.0's validator stack (the most complete public recipe)

From Appendix B of the Terminal-Bench paper (2026), 89 final tasks, > 3 person-hours per task:

1. Contributor checklist in the PR template: every behavior in tests is in the instruction and vice versa; task.yaml and solution.sh written by a human; a strong model was run and failing runs were analysed to confirm the task is valid; dependencies pinned.
2. Deterministic CI check tools: canary string present; Dockerfile does not reference tests or oracle; test-file references are mentioned in the instruction; no privileged containers; metadata fields filled.
3. LLM-backed check tools (one criterion each): behavior-in-task-description; behavior-in-tests; informative test docstrings; anti-cheating measures (can the agent read solutions from files, newer git commits, etc.); structured-data schema declared; pinned dependencies; typos in file and variable names; tests or solution copied into image; test deps installed in image; **hardcoded solution** (oracle must compute the answer, not echo it); file references mentioned.
4. LLM debug tool: given task.yaml, run info and multiple trials, decides whether failures are due to INSUFFICIENT INSTRUCTIONS (exact-name mismatches, implicit format expectations, hardcoded test expectations, examples-vs-requirements) versus normal agent limitation, using the heuristic "if all trials fail the same tests with similar errors, suspect the spec; if trials fail differently, suspect difficulty."
5. Adversarial exploit agent (OpenHands) with the prompt "You are a security researcher bot and your goal is to exploit any way to CHEAT ... you MUST use at least some cheating"; trajectories are manually inspected regardless of pass/fail. Common finds: monkey-patching the test environment, guessing answers, and generating *all* possible answers in the hope tests only check presence of a correct one, not absence of wrong ones.
6. Independent ABC assessment: 0.857 outcome validity, 1.000 task validity, 0.830 reporting.

What it did not catch: Terminal Wrench (2026) re-audited 1,968 tasks across five terminal benchmarks by appending a hack-elicitation excerpt to the agent prompt, running > 40,000 trials, LLM-judging each trajectory, then confirming 395 candidates down to 331 with a stronger adversarial loop. Hackable rates: Terminal-Bench 2.0 14/89 (15.7 percent), Terminal-Bench-Pro 48/200 (24.0), SETA 241/1,376 (17.5), original 36/233 (15.5); overall 17.7 percent. Exploit taxonomy by trajectory count: hollow implementation 2,243; output spoofing 1,071; constraint loophole 849; structural extraction (reading verifier or answer key) 529; binary hijacking 428; algorithmic simplification 339; mutable-input tampering 322; keyword gaming 312; metric spoofing 236; security downgrading 80; deceptive rationalization 38. Monitorability: an LLM monitor detects hacks at AUC 0.968 (TPR 0.82 at 5 percent FPR) with full chain-of-thought, degrading to AUC 0.917 (TPR 0.44) with actions only; loophole and simplification hacks are the ones that become invisible.

### 2.3 Automated hacker-fixer loops

**Hardening Agent Benchmarks with Adversarial Hacker-Fixer Loops** (2026). Three agents: hacker (pass the verifier without solving), fixer (patch the verifier to reject that exploit), solver (confirm legitimate solutions still pass). KernelBench L1: hinted attack success 62 -> 0 percent by iteration 8; unhinted Gemini 3.1 Pro 76 -> 0, Claude Opus 4.7 61 -> 0; solver pass rate held at 92 to 98 percent; verifiers accumulate defenses across tasks (task 001's final verifier contained contributions from 13 tasks). Without the solver guardrail, patched verifiers blocked *all* legitimate solutions (benign pass 0 to 22 percent) until an autopatch relaxed two over-aggressive defenses. On 77 Terminal-Bench tasks the loop was weaker: unhinted attacks 39.2 -> 16.7 percent, but solver pass dropped 76.1 -> 65.2 percent, i.e. verifiers got over-restrictive on open-ended tasks. Cost ~$5,000 API plus 8xH200 for ~48 h; max 10 iterations per task.

Related: KernelBench documents 15 exploit strategies (50 to 120x fake speedups by skipping work, hardcoding outputs, precision downgrade 6.4 percent of submissions, timer monkey-patching 3.3, unsynchronized stream injection 2.5, cached-output replay 1.6); KernelGuard cut hacked submissions from 3.45 to 0.37 percent in production. The **Reward Hacking Benchmark** (2026) for tool-use agents: exploit rates across 13 frontier models from 0 percent (Claude Sonnet 4.5) to 13.9 percent (DeepSeek-R1-Zero; V3 at 0.6 percent, so RL post-training raised it); simple environment hardening cut exploits 87.7 percent relative without hurting legitimate performance; 72 percent of hacks carry explicit chain-of-thought rationale; models that never hack easy variants hack harder variants, "alignment only suppresses reward hacking when honest solutions remain tractable" (directly relevant: as we make tasks harder, the grader gets attacked more).

### 2.4 Benchmark-level validity audits

**Establishing Best Practices for Building Rigorous Agentic Benchmarks (ABC)** (2025). Of 10 audited benchmarks, 7 had outcome-validity flaws, 7 had task-validity flaws, all 10 had reporting gaps. Examples: τ-bench counted empty responses as success (38 percent overestimation) and listing all candidate answers as valid (40 percent); KernelBench incomplete fuzzing of edge cases and memory layouts (31 percent absolute overestimation); SWE-Lancer agents could read test files (100 percent); WebArena LLM judge accepting irrelevant output (1.4 to 5.2 percent); OSWorld broken selectors (28 percent underestimation); CVE-Bench naive state matching (32.5 percent). Applying the checklist to CVE-Bench reduced overestimation by 33 percent. Task-validity items that map directly onto generated tasks: T.5 ground-truth isolation from the agent, T.6 frozen reproducible environment, T.7-8 correct ground truth, T.9 automatic oracle demonstration, T.10 pilot-run outlier inspection.

**What Makes a Good Terminal-Agent Benchmark Task** (2026): six recurring anti-patterns in generated or contributed tasks: incoherent AI-generated instructions, over-prescriptive specs, clerical (busywork) difficulty, oracle solutions that need undocumented knowledge, tests validating the wrong criteria, reward-hackable environments; "over 15 percent of tasks in popular terminal-agent benchmarks are reward-hackable."

### 2.5 Criteria quality in rubric-graded benchmarks

- **APEX-Agents** (Mercor, 2026): 480 tasks in 33 simulated "worlds" averaging 160+ files, built by professionals with 5 to 10 years' experience who acted as both contributors and reviewers. Each rubric has 1 to 10 binary Met/Not-met criteria (mean 4.06), each a short self-contained statement with a "grading target" naming the artifact to inspect; a judge-LM panel grades each criterion independently and is reported as highly correlated with human grades. Frontier pass@1 under 25 percent (Gemini 3 Flash 24.0, GPT-5.2 23.0, Claude Opus 4.5 18.4; pass@8 about 40). The public materials describe the criterion requirements but not the reviewer counts or the rejection statistics of the criteria review **[detail not available in fetched sources]**. Mercor's associated verifier, Archipelago, grades from a filesystem diff plus extracted document content.
- **HiL-Bench** (Scale AI, 2026) is the best public recipe for *validating an injected difficulty*: 300 tasks (150 SWE, 150 SQL), 1,131 blockers (missing info 42 percent, ambiguous 36, contradictory 22). Each blocker must be self-contained and non-self-referential (no mention of hidden tests or the benchmark); 5 to 6 rounds of independent human auditors; automated **necessity** check (baseline pass rate <= 5 percent without resolutions) and **sufficiency** check (~90 percent pass with all resolutions supplied). The simulated human is a frozen Llama-3.3-70B semantic matcher that returns the registered resolution if the question targets a registered blocker, else "irrelevant question", giving reproducible binary feedback.
- **PaperBench** (OpenAI, 2025): 8,316 rubric leaves co-developed with paper authors; SimpleJudge with o3-mini scores F1 0.83 against human judgments on JudgeEval (GPT-4o 0.73, o1 0.84 at $830 per paper); ~$66 per paper vs ~12 human-hours ($1,200).
- **GDPval** (OpenAI, 2025) **[unverified: page returned 403]**: recalled figure is that the automated pairwise grader agreed with expert graders about 66 percent of the time versus ~71 percent human-human agreement; treat as approximate.

### 2.6 How reliable is a judge at verifying a criterion once you have one?

- **RuVerBench / Can LLM-as-a-Judge Reliably Verify Rubrics in Agentic Scenarios?** (2026): 2,458 human-labelled (output, rubric) pairs across deep research (7.1k-token reports) and agentic coding (49.4k-token trajectories), 18 judges. Format and numeric criteria are reliable; logic and fact criteria (research) and tool-use / rule criteria that require trajectory inspection (coding) are not. Top balanced accuracy 94.7 (research) vs 89.4 (coding). Two dominant errors: accepting partial satisfaction as full, and inventing constraints not in the rubric. Error overlap between top judges only 16 to 21 percent, so 3 to 5 vote majority helps; single-criterion calls beat batching for coding.
- **Verifying Agents in Rubric-Graded Environments / Gandalf** (Handshake AI, KDD 2026): 3,204 human-judged criteria over 21 investment-banking tasks. Criteria decompose into nine verifier capabilities; the three largest are "recompute a derived figure and compare" (23.0 percent), "confirm a literal data point matches the source" (15.5), "trace a value across two locations" (14.9), all of which are executable checks in disguise. A reactive verifier agent running *inside the same environment with the same MCP tools* and given domain guidance reaches F1 0.633 at $42 per run (Gemini 3 Flash 0.660 at $64), beating a serialize-then-judge pipeline (Archipelago, F1 0.538 at $414) and a text-only trajectory judge (AutoRubric, below 0.47 for every backbone). Verifier architecture mattered more than model choice (3.1-point spread across five models vs 10+ points across architectures).
- **Reward Hacking in Rubric-Based RL** (2026): when rubric judges become the RL reward, policies learn partial-criterion satisfaction, implicit-to-explicit misrepresentation and imprecise topic matching; stronger judges reduce but do not remove this "when the rubric leaves important failure modes unspecified"; rubric-optimized checkpoints were preferred by rubric judges but *rejected* by rubric-free human raters on factual correctness, conciseness and relevance. This is the central argument against putting a judge in the reward path of an RL environment.
- **Autorubric** (2026) catalogues the judge failure modes to configure against: position bias, stochastic inconsistency, criterion conflation, forced judgment under uncertainty, model-dependent calibration.

### 2.7 What LLM validators demonstrably catch, and what they miss

Catch well (high precision): hardcoded or echoed oracle solutions; tests or answers copied into the image; spec/test mismatch (behavior tested but not described, and vice versa); typos in identifiers; missing file-name references; blatantly underspecified instructions; instructions that leak the hidden state ("the benchmark deliberately corrupted..."; HiL-Bench's non-self-referential rule); and, with a hack-elicitation prompt, roughly one in six graders that can be bypassed.

Miss or mis-handle: recall on clarity (human-accepted tasks rejected, judge-to-judge disagreement); subtle test insufficiency (SWE-bench Verified still leaks); loophole and simplification exploits once chain-of-thought is hidden; partial satisfaction of compound criteria; and anything requiring inspection of environment state or trajectory when the judge only sees text.

---

## 3. Difficulty levers that scale procedurally, with evidence

Ordered by measured effect on frontier pass rates. Numbers are the largest reputable deltas I found; the analog to our data-engineering world is noted after each.

### 3.1 Hidden blockers discoverable only by investigation, plus a stop-and-ask requirement (largest effect)
- HiL-Bench SQL: Claude Opus 4.6 90.7 percent pass@3 with full information -> 39.3 percent when the same information must be discovered and requested through `ask_human()` (-51 pp); GPT-5.4 86 -> 17.3 (-69 pp); Gemini 3.1 Pro 88.7 -> 35.3. Best Ask-F1 57.5 (SQL) / 29.9 (SWE). Failure signatures: GPT "confident execution on wrong beliefs"; Claude "detects uncertainty without acting on it". Blockers must surface *progressively*, which prevents the degenerate strategy of front-loading questions.
- Ambig-DS (2026): 51 prediction-target and 61 evaluation-objective ambiguities grafted onto DSBench / MLE-bench, graded with the original evaluators; agents "quietly commit to plausible but unintended framings, producing clean executable artifacts that hide the wrong assessment"; one clarifying question recovers most of the loss; permissive prompts over-ask on clear tasks, conservative prompts silently default on ambiguous ones.
- Agent Planning Benchmark (2026): GPT-5 refuses 80 percent of tasks with an *explicit* constraint conflict but only 40 percent of tasks with *implicit* missing information.
- TheAgentCompany (2025): 175 tasks, best 30.3 percent full completion (Gemini 2.5 Pro); agents "fabricate workarounds to bypass difficult requirements rather than seeking necessary information from simulated colleagues."

Analog for us: this is exactly the decision arm. The ownership boundary should not be stated in the ticket; it should be discoverable (CODEOWNERS-style ownership table, dbt `meta.owner`, an old Slack thread, a scheduler config) and sometimes *contradictory across sources* so the agent has to reconcile. Grade "asked / notified the right owner with the right evidence" with HiL-Bench-style necessity (<= 5 percent pass if you just fix everything) and sufficiency (~90 percent pass if the resolution is given) checks per generated instance.

### 3.2 Hard-negative distractor artifacts (large effect; random noise is much weaker)
- Lost in the Noise / NoisyBench (2026), 11 datasets incl. tool use: Gemini 2.5 Pro 77.8 clean -> 70.8 random documents -> 62.5 irrelevant chat history -> 48.0 hard negatives (-38 pp); DeepSeek-R1-0528 72.4 -> 47.6 with hard negatives; small reasoning models lose up to 80 percent. Inverse scaling: more reasoning tokens -> more attention on distractor tokens -> lower accuracy. Design rule from the paper: a distractor must not change the correct answer and must not contain it.
- Agent Planning Benchmark: extraneous tools cut holistic-plan accuracy 22.0 percent (GPT-5) and 12.3 percent (Claude Sonnet 4.5) but only 0.9 to 6.7 percent in step-wise mode, so the lever is strongest when the agent must plan up front.

Analog: plausible-but-wrong dbt models, a second staging table with a similar name and stale rows, an abandoned branch with a half-fix, a Jira ticket describing a *different* incident with overlapping symptoms, a Slack thread proposing the wrong root cause. Random junk files will not move frontier pass rates; semantically adjacent decoys will.

### 3.3 Cross-system reconciliation with subtle breakage (large effect in data-engineering benchmarks)
- Snowflake data-eng-bench: 103 tasks (84 build, 19 fix) over a 579-table retail warehouse, hidden multi-assertion verifier on materialized output; top config 73.8 percent pass@1 but 64.1 percent consistency across three runs; "fix tasks require diagnosing and repairing subtly incorrect models" are the hardest.
- DataAgentBench (UC Berkeley, 2026): 54 questions across 12 datasets, 4 DBMS incl. DuckDB; best model 38 percent pass@1 (Gemini 3 Pro).
- ADE-bench (dbt Labs): GPT-5.1 and Claude Sonnet 4.5 both 56 percent on DuckDB + dbt Core at ~$14.90 per task.
- Spider 2.0 (2025): 632 tasks; DBT split (68 tasks on DuckDB) top 65.6; Lite top 76.23; databases with 1,000 to 3,000+ columns, multi-dialect, 100+ line queries, documentation reading.
- ELT-Bench: 100 end-to-end pipelines, "sharp" drops on full workflows. DataClawEval: 100 tasks, best 74.9/100, no engine-wide winner, more tool calls did not help.
- APEX-Agents: cross-application worlds of 160+ files, < 25 percent pass@1.

Analog: our multi-system stack already has this; the procedural knob is the *number of hops* between symptom and cause (dashboard -> mart -> intermediate -> staging -> loader -> scheduler config -> vendor file) and whether the cause is split across two systems owned by different teams.

### 3.4 Horizon length and simulated time (compounding effect)
- The Illusion of Diminishing Returns (ICLR 2026): failures on lengthened simple tasks are execution errors, not reasoning; per-step error rises through the episode because the model conditions on its own error-laden history (self-conditioning); small gains in step accuracy compound into exponential horizon gains, so horizon is a discriminative lever across model generations.
- τ-bench: GPT-4o < 50 percent pass^1 but < 25 percent pass^8 on retail; reliability collapses faster than mean accuracy.
- Vending-Bench 2 (Andon Labs, 2026): a full simulated year, adversarial suppliers, delayed deliveries, customers demanding refunds, single metric = closing balance from $500; GPT-6 Astra $15,515 +- 1,074, Claude Opus 5 $11,182 +- 2,094, wide spread; winners maintain constant tool-use rate over the year, losers degrade.
- FutureSim (2026): 90-day chronological replay of 330 real events, 500 to 4,000+ tool calls, > 10M tokens; best top-1 accuracy 25 percent; several frontier open-weight models score below abstaining.

Analog: advance the scheduler clock inside the episode so that a fix must survive N future runs, backfills, and a late-arriving vendor file; grade over the ledger *after* the simulated time has elapsed.

### 3.5 Live events and fault injection during the episode (moderate effect)
- ReliabilityBench (2026): reliability surface R(k, epsilon, lambda) over repeats, semantically-equivalent perturbations and injected tool/API faults (timeouts, rate limits, partial responses, schema drift); success 96.9 -> 88.1 percent at perturbation intensity 0.2; rate limiting the most damaging fault; correctness defined by *action metamorphic relations* (end-state equivalence), which is a verifier pattern we can copy.

Analog: a second incident lands in Slack mid-episode; a scheduler run kicks off while the agent is editing; the warehouse returns a transient error on the first query. Cheap to generate, moderate discriminative power, and it makes reward hacking harder because the agent cannot precompute the end state.

### 3.6 Conflicting sources of truth and stale documentation (real but under-measured)
- HiL-Bench's contradictory-information blockers (22 percent of blockers) are validated the same way as missing information. Agent Planning Benchmark shows *explicit* contradictions are detected 2x more often than implicit gaps, so a contradiction stated in one document is weaker than a contradiction that only emerges by joining two systems.
- Stale-documentation evidence is thin and mostly vendor blogs (e.g. a "stale context" benchmark reporting that smaller models "floor at zero" when handed outdated API docs) **[weak, unverified]**. The failure mode is well attested qualitatively (Terminal-Bench's failure taxonomy includes "using the wrong source of truth" as a material disobey-specification error) but I found no controlled frontier-model measurement.

Analog: README says the cutoff is 23:59 UTC, the dbt macro says 00:00 local, the ledger shows which one production actually uses. This is cheap to procedurally generate from a single seed parameter and it forces the agent to verify against state rather than text.

### 3.7 Multiple stakeholders with conflicting authority (plausible, thin evidence)
TheAgentCompany's colleague-interaction tasks and APEX-Agents' multi-role worlds are the closest measurements; both report that communication-dependent tasks are among the hardest but neither isolates "conflicting authority" as a factor. I would treat this as a variant of 3.1 (the blocker is "who may authorize this?") and validate it with the same necessity/sufficiency check.

### 3.8 Irreversibility (nearly saturated on its own)
WorkBench Revisited (2026): unintended harmful actions (wrong recipient, destructive edits) fell from 26 percent of tasks for GPT-4 to 1.9 percent for Claude Fable 5. StepShield (2026) argues the open question is *when* an agent goes rogue, not whether. So irreversibility is worth keeping as a *penalty* (the audit ledger records a destructive write to the other team's tables) but not as a primary difficulty lever; it becomes discriminative only when coupled to 3.1 (the agent must decide whether it is allowed to make the irreversible change).

### 3.9 Levers that add noise rather than difficulty
- Clerical volume (many files to touch, long boilerplate): flagged as an anti-pattern by the terminal-benchmark guideline paper; raises variance, not discrimination.
- Over-prescriptive specs that force one solution path: create spurious failures (the Terminal-Bench debug tool's whole purpose is to detect these).
- Random, semantically unrelated distractors: ~9 pp for Gemini 2.5 Pro versus ~38 pp for hard negatives.
- Difficulty for its own sake: SWE-smith's hard band (17 percent resolve) was no better as training data than easy or medium; daVinci-Env and INTELLECT filter *both* tails.

---

## 4. Judge required vs. deterministic verifier: the decision rule

### 4.1 Evidence summary
- Deterministic verifiers are attackable (15 to 24 percent of tasks in audited terminal benchmarks; 31 percent overestimation on KernelBench from incomplete fuzzing) **but are repairable to ~0 percent attack success with a hacker-fixer-solver loop while keeping 92 to 98 percent benign pass**, and they are stable under RL.
- LLM judges reach F1 0.83 on well-structured rubric leaves (PaperBench), balanced accuracy ~89 to 95 on simple format/number criteria, but fall to F1 0.47 to 0.66 on artifact-heavy criteria unless the verifier is itself an agent inside the environment (Gandalf), and they drift under RL optimization (rubric reward-hacking paper: judge-preferred, human-rejected checkpoints).
- Cost: judge-based verification is $42 to $66 per task-level evaluation at current prices; a state-based verifier is effectively free per rollout, which matters at 8 to 128 rollouts per task for difficulty banding.

### 4.2 Verifier patterns that let generation stay deterministic
1. **Oracle execution at generation time** (SWE-smith, AutoForge, SCA): the generator produces the golden fix/trajectory, executes it in the seeded world, and records the target state S*; reward = predicates over S* vs. agent's final state. Reject any generated instance whose oracle does not reach S* or whose oracle is trivial (Terminal-Bench "hardcoded solution" check).
2. **Fail-to-pass plus failure cases** (SWE pipelines, SCA): at least one check must fail on the unfixed world and pass after the oracle; and three *plausible wrong fixes* (fix the symptom in the mart, overwrite the other team's table, fix only one of two broken loaders) must still fail. This is the single most effective filter against ambiguous verifiers in the SCA ablation.
3. **Determinism check** (AZR): run the oracle twice from the seed; identical ledger hashes or reject.
4. **Necessity / sufficiency for injected blockers** (HiL-Bench): reference agent without the hidden information passes <= 5 percent; with it, ~90 percent.
5. **Metamorphic and differential relations** (ReliabilityBench action-MRs; Argus for SQL): two instructions that should yield the same end state must; the agent's dbt model vs. the oracle's must agree on a hidden query set (differential testing), and invariants (row counts reconcile across staging/mart, ledger balances, no writes outside owned schemas) must hold regardless of path.
6. **State-diff verification for "soft" deliverables** (Archipelago, Gandalf's capability taxonomy): most "did the agent communicate correctly" criteria reduce to structured fields, e.g. a Jira ticket exists, assigned to team X, links run ID Y, includes the query hash or row IDs the oracle computed, was created before any write to X's schema. Make the evidence schema part of the task contract so it is checkable.
7. **Adversarial audit as a release gate** (Terminal-Bench B.4, hacker-fixer loop): every new task family passes an exploit agent run with the hack-elicitation prompt; any pass without a legitimate fix is a generator bug.

### 4.3 The rule
For each candidate grading criterion of a generated instance:

1. **Can it be written as a predicate over (final world state, audit ledger, message/ticket log) with a golden value the generator's oracle computed?** If yes: deterministic verifier. Add it to the fail-to-pass set and to the failure-case set. (Covers correctness of data, idempotence, boundary respect, notification sent to the right owner with the right structured evidence, timing relative to writes.)
2. **If no, can the task be re-specified so that it is?** (Require the evidence in structured fields; ask for a SQL check rather than prose; require the agent to write its diagnosis to a form with enumerated causes.) If yes: re-specify and go to 1. This is where APEX's "grading target" and Gandalf's taxonomy say most of the mass is.
3. **If still no** (free-text rationale quality, prioritization judgment, tone of a stakeholder message): allow a judge **only** if all of the following hold: (a) a deterministic gate (steps 1 and 2) has already passed, so the judge cannot turn a failure into a pass; (b) the criterion is binary, self-contained and names its grading target; (c) the judge is artifact-grounded (reads the actual ticket/ledger, ideally runs inside the environment) rather than reading a trajectory dump; (d) its weight is capped (for a scalar reward: a small bonus, not a component of the headline pass); (e) it is calibrated against a held-out human-labelled set per criterion type and re-checked after each policy update, with a reward-hacking probe (rubric-optimized vs. rubric-free preference).
4. **If (3) cannot be satisfied, drop the criterion.** A criterion that only a judge can score and that cannot be gated is a reward-hacking surface, not difficulty.

Where an LLM is still essential and safe: generating the task family parameters and narrative artifacts (tickets, Slack threads, stale docs, decoys), generating the *oracle* that is then executed, validating instruction clarity and leak-freedom (precision-first, consensus of 3, reject-only), running the exploit agent, and triaging failing pilot runs into "spec problem" vs. "hard" (the Terminal-Bench debug-tool prompt is a good starting point).

---

## 5. Model routing: state of the art and what our benchmark could contribute

- **RouteLLM** (LMSYS / UC Berkeley, 2024): binary strong/weak routing trained on Chatbot Arena preference pairs plus augmentation with golden-label data (MMLU); four router types (similarity-weighted ranking, matrix factorization, BERT classifier, causal-LM classifier); > 2x cost reduction at matched quality on MT-Bench, MMLU, GSM8K; routers transfer when the strong/weak pair is swapped.
- **RouterBench** (Martian, 2024): 405k+ inference outcomes across representative models as a static routing dataset with a cost-quality frontier.
- **Arch-Router** (Katanemo, 2025): 1.5B model mapping queries to user-defined domain/action preferences; trained on synthetic LLM-generated preference data; positioned as aligning with subjective preferences rather than benchmark accuracy.
- **Router-R1** (UIUC, NeurIPS 2025): the router is itself an LLM trained with RL to interleave "think" and "route" actions across multiple rounds, with format, outcome and cost rewards; conditions on model descriptors (price, latency, example performance) so it generalizes to unseen models; evaluated on seven QA benchmarks.
- **LLMRouterBench** (2026): 400k+ instances, 21 datasets, 33 models, 10 baselines with per-query per-model correctness and cost. Findings: a large gap to oracle driven by "model-recall failures" (routers fail to send a query to the one model that can do it); many methods perform similarly under unified evaluation; several recent and commercial routers do not beat a simple baseline; embedding choice matters little; bigger candidate pools show diminishing returns versus careful pool selection.
- **DARS / From Sampled Outcomes to Capability Distributions** (2026): replaces single binary labels with a per-query capability distribution estimated from repeated decoding and semantics-preserving rewrites (expected quality, expected cost, variance); improves routing utility over single-shot supervision across methods, benefits persist at moderate sampling budgets. This is the paper that justifies pass-rate-over-k labels.
- **Survey: Dynamic Model Routing and Cascading** (2026): families are difficulty-aware predictors (question-score pairs), preference-based (Arena pairs or synthetic comparisons), RL/bandit routers (online feedback), uncertainty/confidence routers (calibration sets), and cascades (FrugalGPT, AutoMix: escalate on a quality signal; need query-response pairs or fine-tuned confidence). Reported gains, e.g. MixLLM 97.25 percent of GPT-4 quality at 24.18 percent cost. The stated open problem across families is generalization to newly released models without retraining. Meta-Router (2025) addresses mixing gold-standard and preference labels.

Could our environment produce useful routing training data? Yes, with three conditions.
1. **The label is the pass-rate vector.** Run each generated instance k >= 8 times per candidate model family and store (instance features, model, empirical pass rate, cost, tokens, wall time, boundary-violation flag). That is exactly DARS-style supervision and exactly what LLMRouterBench stores; 19 arms are far too few, hundreds to thousands of instances are the minimum for a per-family capability estimate that is not dominated by variance.
2. **Features must be observable at routing time.** A router sees the ticket text and the initial environment observations (schema sizes, number of systems touched, presence of an ownership boundary flag if it is visible), not the final state. Procedural generation is an advantage here because the generator *knows* the latent difficulty parameters (hop count, decoy count, blocker type, horizon), which lets you learn which surface features predict them.
3. **Route at the sub-task level, not the episode level.** Our episodes decompose into investigate / fix / decide-and-notify. LLMRouterBench's "model-recall failure" and the Databricks internal finding (three capability tiers; cheaper models handle routine work) **[Databricks detail from a secondary blog, unverified]** both suggest the useful signal is "which family is reliable on which sub-skill," e.g. cheap model for the investigation queries, strong model for the boundary decision. A cascade (cheap model first, escalate on a deterministic verifier failure or on a boundary-detected flag) is the simplest deployable form and needs no learned router at all.

Caveat: routers trained on our data will predict our task distribution; the value to the labs is less the router than the *table* (instance features x model family x pass rate x cost), refreshed each model release, which is also what difficulty banding (section 1.6) needs anyway.

---

## 6. Recommended pipeline for our environment (synthesis)

1. **Parametrize the nine scenarios into families.** Seeds control: damage type, hop count from symptom to cause, number and kind of hard-negative decoys, blocker type (missing / ambiguous / contradictory ownership), horizon (scheduler ticks, backfills, late files), live-event injections, and whether the boundary case is present (decision arm). Use the LLM to write the narrative artifacts (ticket, Slack, stale README, decoy branch) from the parameters, never to invent the parameters.
2. **Generate the oracle with the task** and execute it in the seeded world to obtain S*. Reject if the oracle fails, is non-deterministic across two runs, or is trivial (echoes an answer, touches no state).
3. **Derive checks from S*:** fail-to-pass predicates, three failure-case fixes that must still fail, invariants (row reconciliation, ledger balance, no writes outside owned schemas), and the structured-evidence predicate for the notification. No judge in the reward path.
4. **Validate the specification with a 3-judge reject-only clarity gate** using the SWE-bench Verified rubric and the Terminal-Bench LLM check list (behavior-in-description, behavior-in-tests, hardcoded solution, leaked solution, self-referential text). Expect to lose 20 to 25 percent of otherwise valid instances.
5. **Validate injected difficulty** HiL-Bench-style: reference policy without the hidden information passes <= 5 percent; with it, ~90 percent. Instances outside those bounds are mis-generated, not hard.
6. **Band by reference-policy pass rate** with k = 8 to 16 rollouts per instance across at least three model families; keep 1/k to (k-1)/k, re-band per release, keep a smooth distribution. Track that the decision arm and control arm are banded independently.
7. **Run the exploit agent** (hack-elicitation prompt) on every new family and on a sample of every batch; feed exploits into a fixer step whose patches must keep the oracle passing (the solver guardrail). Budget a few dollars per instance for this; it is the cheapest insurance in the whole pipeline.
8. **Pilot-run triage**: run the Terminal-Bench debug prompt over failing rollouts to separate spec defects from difficulty; if all rollouts fail the same predicate the same way, suspect the generator.
9. **Keep a human-labelled calibration set** (a few hundred criterion-level labels) so any later judge (for the free-text rationale bonus only) can be scored per criterion type, and so the pass-rate table can be sanity-checked against human difficulty ratings (SWE-smith's rater reached 75 percent on pairwise labels).

---

## References

1. SWE-smith: Scaling Data for Software Engineering Agents — Stanford / Princeton et al., NeurIPS 2025. https://arxiv.org/abs/2504.21798
2. Training Software Engineering Agents and Verifiers with SWE-Gym — Apple, UC Berkeley, UIUC et al., ICML 2025. https://arxiv.org/abs/2412.21139
3. R2E-Gym: Procedural Environments and Hybrid Verifiers for Scaling Open-Weights SWE Agents — COLM 2025. https://arxiv.org/abs/2504.07164
4. SWE-rebench: An Automated Pipeline for Task Collection and Decontaminated Evaluation of Software Engineering Agents — Nebius, 2025. https://arxiv.org/abs/2505.20411
5. SWE-rebench V2: Language-Agnostic SWE Task Collection at Scale — Nebius, 2026. https://arxiv.org/abs/2602.23866
6. SWE-Bench++: A Framework for the Scalable Generation of Software Engineering Benchmarks from Open-Source Repositories — 2025. https://arxiv.org/abs/2512.17419
7. Skywork-SWE: Unveiling Data Scaling Laws for Software Engineering in LLMs — Skywork AI, 2025. https://arxiv.org/abs/2506.19290
8. SWE-Factory: Your Automated Factory for Issue Resolution Training Data and Evaluation Benchmarks — 2025. https://arxiv.org/abs/2506.10954
9. daVinci-Env: Open SWE Environment Synthesis at Scale — 2026. https://arxiv.org/abs/2603.13023
10. Kimi-Dev: Agentless Training as Skill Prior for SWE-Agents — Moonshot AI, 2025. https://arxiv.org/abs/2509.23045 ("Genesis" task-synthesis system: **[unverified; no primary source found]**)
11. Absolute Zero: Reinforced Self-play Reasoning with Zero Data — Tsinghua / BIGAI et al., 2025. https://arxiv.org/abs/2505.03335
12. Self-Challenging Language Model Agents — Meta FAIR / UC Berkeley, NeurIPS 2025. https://arxiv.org/abs/2506.01716
13. Propose, Solve, Verify: Self-Play Through Formal Verification — CMU, Dec 2025. https://arxiv.org/abs/2512.18160
14. GenEnv: Difficulty-Aligned Co-Evolution Between LLM Agents and Environment Simulators — Gen-Verse, Dec 2025. https://arxiv.org/abs/2512.19682
15. AgentGen: Enhancing Planning Abilities for LLM-based Agents via Environment and Task Generation — 2024. https://arxiv.org/abs/2408.00764
16. AutoForge: Automated Environment Synthesis for Agentic Reinforcement Learning — Dec 2025. https://arxiv.org/abs/2512.22857
17. Agent World Model: Infinity Synthetic Environments for Agentic Reinforcement Learning — Feb 2026. https://arxiv.org/abs/2602.10090
18. EnvScaler: Scaling Tool-Interactive Environments for LLM Agent via Programmatic Synthesis — RUC, ACL Findings 2026. https://arxiv.org/abs/2601.05808
19. AutoEnv: Automated Environments for Measuring Cross-Environment Agent Learning — Nov 2025. https://arxiv.org/abs/2511.19304
20. Reasoning Gym: Reasoning Environments for RL with Verifiable Rewards — open-thought, NeurIPS 2025. https://arxiv.org/abs/2505.24760
21. TextArena — 2025. https://arxiv.org/abs/2504.11442
22. Environments Hub: A Community Hub To Scale RL To Open AGI — Prime Intellect, 2025. https://www.primeintellect.ai/blog/environments ; verifiers library https://github.com/PrimeIntellect-ai/verifiers
23. INTELLECT-2: A Reasoning Model Trained Through Globally Decentralized RL — Prime Intellect, 2025. https://arxiv.org/abs/2505.07291
24. INTELLECT-3: Technical Report — Prime Intellect, Dec 2025. https://arxiv.org/abs/2512.16144
25. An FAQ on Reinforcement Learning Environments — Epoch AI, 2026. https://epoch.ai/gradient-updates/state-of-rl-envs
26. Online Difficulty Filtering for Reasoning Oriented Reinforcement Learning — 2025. https://arxiv.org/abs/2504.03380
27. LiteResearcher: A Scalable Agentic RL Training Framework for Deep Research Agent — 2026 (difficulty bands). https://arxiv.org/abs/2604.17931 ; Motif-2-12.7B-Reasoning (2025) https://arxiv.org/abs/2512.11463 ; Nemotron 3 Nano (2025) https://arxiv.org/abs/2512.20848
28. Introducing SWE-bench Verified — OpenAI, 2024. https://openai.com/index/introducing-swe-bench-verified/ ; Epoch AI benchmark page https://epoch.ai/benchmarks/swe-bench-verified
29. Ambig-SWE: Interactive Agents to Overcome Ambiguity in Software Engineering — ICLR 2026. https://arxiv.org/abs/2502.13069
30. Terminal-Bench: Benchmarking Agents on Hard, Realistic Tasks in Command Line Interfaces — 2026 (Appendix B: quality control, adversarial exploit agent). https://arxiv.org/abs/2601.11868
31. Terminal Wrench: A Dataset of 331 Reward-Hackable Environments and 3,632 Exploit Trajectories — 2026. https://arxiv.org/abs/2604.17596
32. Hardening Agent Benchmarks with Adversarial Hacker-Fixer Loops — 2026. https://arxiv.org/abs/2606.08960
33. What Makes a Good Terminal-Agent Benchmark Task: A Guideline for Adversarial, Difficult, and Legible Evaluation Design — 2026. https://arxiv.org/abs/2604.28093
34. Establishing Best Practices for Building Rigorous Agentic Benchmarks (ABC) — 2025. https://arxiv.org/abs/2507.02825
35. KernelBench EVAL notes (documented exploit strategies) — Scaling Intelligence Lab. https://github.com/ScalingIntelligence/KernelBench/blob/main/EVAL.md ; KernelGuard (2026) **[secondary summary only]**
36. Reward Hacking Benchmark: Measuring Exploits in LLM Agents with Tool Use — 2026. https://arxiv.org/abs/2605.02964
37. APEX-Agents — Mercor, 2026. https://www.mercor.com/blog/introducing-apex-agents/ ; dataset https://huggingface.co/datasets/mercor/apex-agents ; The AI Productivity Index (APEX) https://arxiv.org/abs/2509.25721
38. HiL-Bench (Human-in-Loop Benchmark): Do Agents Know When to Ask for Help? — Scale AI, 2026. https://arxiv.org/abs/2604.09408
39. Ambig-DS: A Benchmark for Task-Framing Ambiguity in Data-Science Agents — 2026. https://arxiv.org/abs/2605.09698
40. PaperBench: Evaluating AI's Ability to Replicate AI Research — OpenAI, 2025. https://arxiv.org/abs/2504.01848
41. GDPval — OpenAI, 2025. https://openai.com/index/gdpval/ **[grader-agreement figures unverified in this session]**
42. Can LLM-as-a-Judge Reliably Verify Rubrics in Agentic Scenarios? (RuVerBench) — 2026. https://arxiv.org/abs/2606.29920
43. Verifying Agents in Rubric-Graded Environments (Gandalf, BankerVerifierBench) — Handshake AI Research, KDD 2026. https://kdd-eval-workshop.github.io/agenticai-evaluation-kdd2026/assets/papers/34_Verifying_Agents_in_Rubric_.pdf
44. Rubrics as Rewards: Reinforcement Learning Beyond Verifiable Domains — Scale AI, 2025. https://arxiv.org/abs/2507.17746
45. Reward Hacking in Rubric-Based Reinforcement Learning — 2026. https://arxiv.org/abs/2605.12474
46. Autorubric: A Unifying Framework for Rubric-Based LLM Evaluation on Non-Verifiable Tasks — 2026. https://arxiv.org/abs/2603.00077
47. Lost in the Noise: How Reasoning Models Fail with Contextual Distractors (NoisyBench) — 2026. https://arxiv.org/abs/2601.07226
48. Agent Planning Benchmark: A Diagnostic Framework for Planning Capabilities in LLM Agents — 2026. https://arxiv.org/abs/2606.04874
49. TheAgentCompany: Benchmarking LLM Agents on Consequential Real World Tasks — CMU, 2024/2025. https://arxiv.org/abs/2412.14161
50. The Illusion of Diminishing Returns: Measuring Long Horizon Execution in LLMs — ICLR 2026. https://arxiv.org/abs/2509.09677
51. τ-bench: A Benchmark for Tool-Agent-User Interaction in Real-World Domains — Sierra, 2024. https://arxiv.org/abs/2406.12045
52. Vending-Bench 2 — Andon Labs, 2026. https://andonlabs.com/evals/vending-bench-2
53. FutureSim: Replaying World Events to Evaluate Adaptive Agents — 2026. https://arxiv.org/abs/2605.15188
54. ReliabilityBench: Evaluating LLM Agent Reliability Under Production-Like Stress Conditions — 2026. https://arxiv.org/abs/2601.06112
55. WorkBench Revisited: Workplace Agents Two Years On — 2026. https://arxiv.org/abs/2606.13715
56. StepShield: When, Not Whether to Intervene on Rogue Agents — 2026. https://arxiv.org/abs/2601.22136
57. Spider 2.0: Evaluating Language Models on Real-World Enterprise Text-to-SQL Workflows — 2025; leaderboard https://spider2-sql.github.io/
58. Data Engineering Benchmarks for AI Agents (summary of ADE-bench / dbt Labs, Snowflake data-eng-bench, ELT-Bench, DataClawEval, Databricks internal) — Upriver, 2026. https://upriverdata.com/blog/data-engineering-benchmarks-for-ai-agents **[secondary source; per-benchmark figures not independently verified]**
59. Can AI Agents Answer Your Data Questions? A Benchmark for Data Agents (DataAgentBench) — UC Berkeley EPIC, 2026. https://arxiv.org/abs/2603.20576 ; https://ucbepic.github.io/DataAgentBench/
60. RouteLLM: Learning to Route LLMs with Preference Data — LMSYS / UC Berkeley, 2024. https://arxiv.org/abs/2406.18665
61. RouterBench: A Benchmark for Multi-LLM Routing System — Martian, 2024. https://arxiv.org/abs/2403.12031
62. Arch-Router: Aligning LLM Routing with Human Preferences — Katanemo Labs, 2025. https://arxiv.org/abs/2506.16655
63. Router-R1: Teaching LLMs Multi-Round Routing and Aggregation via Reinforcement Learning — UIUC, NeurIPS 2025. https://arxiv.org/abs/2506.09033
64. LLMRouterBench: A Massive Benchmark and Unified Framework for LLM Routing — 2026. https://arxiv.org/abs/2601.07206
65. From Sampled Outcomes to Capability Distributions: Rethinking Supervision for LLM Routing (DARS) — 2026. https://arxiv.org/abs/2606.06924
66. Dynamic Model Routing and Cascading for Efficient LLM Inference: A Survey — 2026. https://arxiv.org/abs/2603.04445
67. Meta-Router: Bridging Gold-standard and Preference-based Evaluations in LLM Routing — 2025. https://arxiv.org/abs/2509.25535
68. FrugalGPT: How to Use LLMs While Reducing Cost and Improving Performance — Stanford, 2023. https://arxiv.org/abs/2305.05176
69. Multi-Agent LLM-based Metamorphic Testing for REST APIs (ARMeta) — 2026. https://arxiv.org/abs/2605.28321 ; AgentAssay (2026) https://arxiv.org/abs/2603.02601
