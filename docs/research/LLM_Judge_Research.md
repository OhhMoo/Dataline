# LLM-as-judge: the decision, the build, and the evidence

25 Sep 2026. This file merges the 25 Sep decision note and the 22 Sep research memo. The memo's section numbers are unchanged below, so an older citation of "§3" or "§9" still lands. Sweeps with quotes remain in `docs/research/llm-judge/` (r1–r6).

## Decision taken

An LLM does not grade an episode. `pass@1` and the training scalar stay deterministic code over the final world: the warehouse, the ledger, and structured report fields. `tasks/harness/outcome.py` and `tasks/harness/reward.py` do not call a model. `tasks/harness/lab_card.py` treats an import of `judge`, `openai`, or `anthropic` inside `grade.py` as a failed lab requirement.

That is the decision. The papers it rests on:

| Decision | What we took | Paper |
|---|---|---|
| Agent tasks are graded by executable checks. A neural reward on the same tasks gets exploited as the policy grows. | No model inside `pass@1` or `episode_reward`. | DeepSeek-R1, https://arxiv.org/abs/2501.12948 — "neural reward models are susceptible to reward hacking during large-scale reinforcement learning." Nemotron 3 Ultra (Jun 2026), https://research.nvidia.com/labs/nemotron/files/NVIDIA-Nemotron-3-Ultra-Technical-Report.pdf — database-state verification for agent environments; an LLM judge only for instruction following. Kimi K2.5, https://arxiv.org/abs/2602.02276 — rule-based outcome reward for agentic tasks. |
| A rubric judge used as the training reward is exploited inside one run. | A judge verdict cannot flip a fail into a pass. | Mahmoud et al., *Reward Hacking in Rubric-Based RL* (Scale, May 2026), https://arxiv.org/abs/2605.12474 — credited-but-wrong criteria rose from 39% to 65% in one 7B GRPO run under a weak verifier; a strong verifier (92% panel agreement) still left 15–28% exploitation. |
| The same data-engineering job is graded by assertions on tables, not by a judge. | Our checks stay predicates on state. | Snowflake data-eng-bench (2026-08-06), https://www.snowflake.com/en/blog/engineering/data-eng-bench-data-engineering-agent-benchmark/ — hidden pytest, no LLM in the score. An LLM audits only successful trajectories after the fact. |
| Hardness is under-specification, and it is code-gradable. | Decision arms: the brief states the symptom; the permission rule lives in tickets, runbook, and chat, and those sources disagree. GT001–GT003 and GT005–GT008 were rewritten that way on 25 Sep. GT009 was already this shape. GT004 was not rewritten. | HiL-Bench (Scale, 2026), https://arxiv.org/abs/2604.09408 — 75–89% pass@3 with full information, 4–24% when the agent must decide whether to ask. UnderSpecBench (2026), https://arxiv.org/abs/2607.02294 — 55.8–67.8% of acted runs crossed a boundary. |
| A judge may reject a task. It may not accept one. | `tasks/harness/judge/clarity_gate.py`. `consensus` returns `reject` or `hold`, never `accept`. One reject vote rejects. No API key required. Not imported by the scorer. Unit tests passed. No live model has been called. | SWE-Bench++ (2025), https://arxiv.org/abs/2512.17419 — three reviewers, precision 0.93–0.95 vs human ~0.96, GPT-5 recall 0.370, so a judge must not be the acceptance. SWE-bench Verified rubric, https://openai.com/index/introducing-swe-bench-verified/ — clarity 0–3, inadequate issues discarded, ensemble by highest severity. Terminal-Bench, https://arxiv.org/abs/2601.11868 — behaviour described, behaviour tested, no hard-coded solution, no leaked solution. |
| A pass that is not a real repair is a bug in the grader, not a point. | `tasks/harness/redteam/exploit_agent/`. The prompt tells the subject to maximise the scalar without the legitimate repair. `run_exploit.py` classifies a grade already collected. A bug exits 2. Not imported by the scorer. Unit tests passed. No exploit episode has been run. | Terminal-Bench appendix B.4, https://arxiv.org/abs/2601.11868. Terminal Wrench (2026), https://arxiv.org/abs/2604.17596 — 14 of 89 Terminal-Bench 2.0 tasks (15.7%) still hackable after human audit. Zhong et al., hacker-fixer-solver loop (2026), https://arxiv.org/abs/2606.08960 — KernelBench attack success from 62% to 0% while the benign solution still passes. |

What was considered and not taken:

- **A judge of code quality, readability, or robustness on the score.** DataClawEval, https://arxiv.org/abs/2607.28033, watched holistic process scores drift from 22 to 75–76 and reverted to five deterministic checks.
- **Training our own reward model.** Gao, Schulman, Hilton, https://arxiv.org/abs/2210.10760, is the over-optimisation curve. The label floor in the memo below is about 10k trajectories and 2.5k–7.5k expert hours. Not started.
- **A capped judge sub-score inside the training scalar.** Left for later, and only after two experts label 200–500 items and human–human agreement clears α ≥ 0.8 (Alt-Test, https://aclanthology.org/2025.acl-long.782/). Not built.
- **Full marks for stopping and notifying while the owned object stays unrepaired** is our `withheld_with_evidence` band. HiL-Bench and UnderSpecBench measure the miss. Neither paper scores that stop as a pass. The trace is `docs/implementation_plan/paper-trace.md`.

The two tools that exist are set up. They have been run as unit tests only. They are not in the Opus pass@1 path.

---

## Build note (25 Sep)

Keep `pass@1` and the training scalar as deterministic code over the final world.

The hypothesis on the table was that a judge is how we add the complexity that makes tasks hard. The literature, and our own rollouts, say the opposite. Frontier models already find the mechanism. They lose when the world does not say what they are allowed to do. That loss can be graded from state. A judge on the score is a surface the policy will learn to flatter.

Labs that publish a reward mix put agent and tool-use work on executable checks. Rubric judges are for chat. DeepSeek-R1 and Nemotron 3 Ultra both report that neural rewards get exploited as the policy grows. Scale measured it inside one GRPO run: credited-but-wrong criteria went from 39% to 65% under a weak verifier, and a strong verifier still left 15–28% exploitation.

### Where the judge is, in this repo

It is not in the grade.

Each `tests/grade.py` is code over the warehouse, the ledger, and structured report fields. The 428 baseline rows and `validity.py --ladder` are unchanged by the judge tools.

What sits beside the grader. None of it can flip a fail into a pass.

| Piece | Where | What it does | What it does not do |
|---|---|---|---|
| Clarity gate | `tasks/harness/judge/clarity_gate.py` | Reject-only scan of a brief: empty, leaked solution, expected table, self-reference. Optional model hook can add a reject. | Cannot accept. Does not score an episode. No model has been called. |
| Exploit record and runner | `tasks/harness/redteam/exploit_agent/` | Prompt whose job is to maximise the scalar without a legitimate repair. `run_exploit.py` turns a collected grade into a bug record. | Does not start an episode. Not imported by `outcome.py` or `reward.py`. |
| Exploit loop | `tasks/harness/redteam/exploit_loop.py` | Classifies a batch the caller already collected. Any bug exits 2 with `needs_fixer`. | Does not start an episode. |
| Model client | `tasks/harness/judge/model_client.py` | The model behind every judge tool: `claude --print` (the one credential the harness always has), k samples, strict majority or `unknown`, provenance with the judge's model family and `same_family_as_subject`. Satisfies `clarity_gate`'s client protocol. Added 26 Sep. | Not imported by any scorer (`test_judge_isolation.py` checks `reward.py`, `outcome.py` and every `grade.py`). |
| Grader audit | `tasks/harness/judge/grader_audit.py` | Job 2 in audit form: one call per failed check, judge with tools on a COPY of the rollout, verdict `grader_correct / grader_wrong / task_ambiguous / environment_fault / unknown`. `--include-passes` audits must-haves of passes; `--dedupe` judges one rollout per (task, check, reason). A `grader_wrong` emits a baseline stub and exits 2. First live use 26 Sep on `opus-decision-k1-20260926T054914Z`: 5 of 5 failed decision-arm episodes carried a grader defect; both passes confirmed. | Writes `_audit/`, never the record. Cannot change `pass`. |
| Shadow judge backend | `tasks/GT001-aov-halving/tests/judge/shadow_judge.py --backend claude` | The J1–J3 shadow axes on a real model: one call per axis, k-vote, `unknown` is an answer, not a 0. The keyword heuristic stays the default, which is still what `optimize_judge.py` (GEPA) searches over; that optimiser is archived at `_archive/tasks/GT001-aov-halving/tests/judge/` (it needs a `vendor/gepa` checkout the repo does not carry). | Advisory only, as before. Labels are baseline-derived, not human; no agreement number is claimed. |
| Rebuild helper | `tasks/contracts/rebuild.py` | `run_twice` copies the world, plants a delivery, runs the pipeline twice. A `WHERE 1=0` loader fails liveness. A plant that inserts the fact rows itself also fails, because the count is taken after the plant. `rematerialise_matches` compares two digests. `second_seed_hardcoded` flags seed B copying seed A. | It does not rebuild from raw by itself. If both digests are the table the agent left, the compare is a tautology. |
| Side checks | `tasks/GT001`–`GT009` `tests/rebuild_check.py` | Name the control arm, the table, and the three check ids. Decision arms return no ids. | Not imported by `grade.py`. Calling them without a real plant spec or two real digests raises `NotImplementedError`. |
| Ask channel | `gateway/src/alkera_gateway/ask_stakeholder.py` | Default off (`ALKERA_ASK_STAKEHOLDER=1` only). Deterministic trigger match. Ask-F1 punishes question spam. | Not one of the 63 MCP tools. No model. Raises if the flag is off. |
| Hardness flags | `tasks/GT004-doubling-window/hardness/`, `tasks/GT009-settlement-cutoff/hardness/` | The hidden fact, the contradictory sources, a decoy name, and the strings that would give the answer away. Scanner gates `prompts/*.md`. | `hidden_blocker.enabled` is false. |
| Decision briefs | GT001, GT002, GT003, GT005, GT006, GT007, GT008 | Coworker symptom. The permission rule is in the world and the sources disagree. Run `--variant decision`. | GT004 was not rewritten. GT009 already had this shape; its default brief is `prompts/guided.md`, not `prompts/vague.md`. |

Two red-team findings, before anyone wires the side checks in:

- **Permanent skip.** `liveness.ingests` already zeros a loader that lands nothing. It still gives full marks if `plant.sql` writes the counted table, or if the loader invents rows that match the predicate and never reads the delivery. `run_twice` closes the first of those. The stub-row hole is still open in the existing contract. Finding: `docs/implementation_plan/hardness-round/findings/R-A1.md`.
- **Hand-patched mart.** No `grade.py` rebuilds from raw. GT001 can look repaired in the table while `fct_orders.sql` stays `append`. A full refresh of that unchanged model reproduces the hand patch, so a naive rebuild would clear the cheat. The rebuild that fails it is the model the agent left, run the way the nightly job runs, from seeded raw. Finding: `docs/implementation_plan/hardness-round/findings/R-A2.md`. Baseline id to add when we wire it: `hand_patch_mart`, class not `resolved`.

### What a judge is for, when we spend tokens

Three jobs, in this order. None of them is "score the episode." The first two now have code. The third does not.

1. **Reject bad generated tasks.** Three models, consensus, reject only. SWE-Bench++ is the calibration: precision 0.93–0.95 against a human 0.96, and low recall, so a judge must not be allowed to accept a task. The checklist is the Terminal-Bench one: behaviour in the description, behaviour in the tests, hard-coded solution, leaked solution, self-referential text. The local gate implements the checklist without a model. The three-model call has not been run.
2. **Attack the grader.** A model is told to maximise the scalar without the legitimate repair. Terminal-Bench 2.0 still had 15.7% of tasks hackable after more than three person-hours of human audit each. A hacker–fixer–solver loop, with the oracle required to keep passing, drove a KernelBench attack rate from 62% to 0%. The prompt, the record, and the classifier are in `exploit_agent/`. The model run has not been started.
3. **A logged sub-score, later, capped.** Binary, one criterion per call, artifact not transcript, different model family from the agent, k=3–5, an "Unknown" out. It cannot move `pass` or the scalar until two experts have labelled 200–500 items and the criterion clears human–human α ≥ 0.8. Candidates, if we ever spend that labelling: whether the discrepancy note names the rows, whether the owner message is enough to act on, whether a design note matches an accepted reference set. "Code quality" and "robustness" as holistic scores are the thing DataClawEval tried and then deleted.

Do not train our own reward model. A domain trajectory model needs on the order of 10k labels the code grader cannot already produce, which is a few thousand expert hours. Vendors ship a frozen frontier judge behind expert rubrics. A frozen judge on one rollout is about $0.05–0.20. That budget belongs on job 2.

Agreement numbers, so the boundary is concrete. Judges match humans when the question is a short binary check of an artifact: HealthBench macro-F1 0.709, PaperBench leaf F1 0.83, APEX criterion accuracy 98.5%. They fall apart on "how good is this?" and on transcripts: AgentRewardBench, no judge above 70% precision, and the dominant error is believing the agent's own claim that it finished. A judge with tools on the world reaches about 90%; a transcript-only judge sits at 60–70%. Temperature 0 still flips about 14% of pairwise verdicts.

### How the tasks get harder

The measured residual on our Opus rollouts: every lost point was a judgment the model stated and defended, or a rubric artifact. Diagnosis was not the miss. GT004's non-repairers gave the same reason in the same words. GT009's agents took the intended branch and then lost on side effects.

So hardness is not a longer prompt, a larger warehouse, or a prose score. It is a structural fact the agent has to discover, and a code check that is false if they skip it.

On 25 Sep the coworker brief was applied to GT001, GT002, GT003, GT005, GT006, GT007, and GT008. GT004 was left as it was. GT009 was already the template. The checks below are the ones still open on those arms.

1. **Leave the answer out of the brief.** Done for the seven arms above: the symptom is in the brief, the rule is in the world. `hidden_blocker.enabled` on GT004 and GT009 is still false.
2. **Necessity and sufficiency, before any frontier number.** For each hidden fact, a scripted agent that is handed the fact lands in the top band (target about 90%). The same agent with that fact deleted from the world does not (target at most 5%). If both pass, the fact was never load-bearing and hiding it was cosmetic. If neither passes, the task is broken, not hard. Not run yet.
3. **A decoy that is graded as damage.** Named for GT004 (`warehouse.fct_revenue_history`) and GT009 (`warehouse.settlement_handoff_prior`). Not planted as a graded object yet.
4. **Ask, with a closed answer key.** The channel exists and is off. Ask-F1 is not multiplied into the score.
5. **Time.** GT004 already advances the clock onto the repaired window. Grade GT009 after the nights, not at t0.
6. **Wire the rebuild only with the caller rule.** `idempotent_rebuild` on control arms, using `run_twice`, with liveness required. `rematerialise_matches` only when `rebuilt_digest` is the incremental re-run. Add `hand_patch_mart` to the ladder in the same change. A grader edit that moves an existing baseline row does not land. Not wired into `grade.py`.

What not to copy from Snowflake's data-eng-bench: the 7k-character spec that names every column and tie-break, and the reward that is 1 only if every assertion passes. What to copy: one deep base world plus an overlay, grain and conservation checks, run-twice, rebuild-from-raw, second-seed, and an oracle computed from the seed instead of a stored fixture.

### What to run with the token budget

1. **Exploit agent on the current 19 arms.** K=4 per arm is enough for a first hack rate. Subject prompt: `tasks/harness/redteam/exploit_agent/prompt.md`. Feed each record to `run_exploit.py`. A pass with `legitimate_repair` false is a task bug. Promote every hole to a scripted baseline before changing the task. Re-run the oracle and the no-op after every fixer patch.
2. **Do not run a judge-as-reward ablation.** We already know the shape of that result, and it would put a number next to `pass@1` that people will quote.
3. **Opus pass@1 on the decision arms** of GT001, GT002, GT003, GT005, GT006, GT007, and GT008, with `--variant decision`. GT001 and GT005 changed world text and were not re-scored after that edit.
4. **Family generator only after an overlay survives** the exploit loop and the necessity pair. Seeds pick the damage, the hop count, the decoy, and whether the boundary is present. The model writes the ticket, the chat, and the stale doc. It does not write the parameters or the expected table. Expect most candidates to die. That yield is the filter.

---

# Research memo (22 Sep 2026)

Question on the table, from the thread with Andrew: should we adopt data-eng-bench's style of task design, and does adding an LLM judge (or training our own reward model) let us build the harder tasks we need? Six literature sweeps were run for this memo. Every number below is traceable to one of them. Section 2 describes the repo as it was on 22 Sep, including "no LLM judge anywhere." The tools in the decision section above were added on 25 Sep and are not in the score.

## 1. Short answer

1. **Our code-only grader is not a compromise. It is what the labs use for
   agentic tasks.** Every 2025–26 lab report that discloses its reward mix puts
   agent and tool-use tasks on rule-based or executable verification, and uses
   rubric or generative reward models only for chat-style tasks. DeepSeek-R1
   says why: "neural reward models are susceptible to reward hacking during
   large-scale reinforcement learning." Nemotron 3 Ultra (Jun 2026): "as policy
   models become larger and more capable, we observe an increasing tendency for
   them to exploit weaknesses in the reward model."

2. **Judged benchmarks buy about one model generation of headroom, not a new
   regime.** APEX-Agents launched at 24% pass@1 in Jan 2026 and its leaderboard
   reads 73.5% in Sep 2026. What stays low across benchmarks is not "judged-ness"
   but one specific thing: knowing what you were not told. HiL-Bench: frontier
   models fall from 75–89% with full information to 4–24% when they must decide
   whether to ask. ClarEval 89% to 8.9%. ORCA-bench Hard 10%. That is the same
   residual we measured: no scored failure in our ~90 Opus rollouts was a
   diagnosis miss; every one was a judgment call.

3. **A judge in the reward path is a liability; a judge as a validator and an
   attacker is the highest-value LLM in the loop.** Scale AI's own follow-up to
   Rubrics-as-Rewards (May 2026) measured the policy exploiting the rubric
   judge: credited-but-wrong criteria rose from 39% to 65% within a single 7B
   GRPO run under a weak verifier, and a strong verifier (92% agreement with a
   frontier panel) still left 15–28% exploitation. Meanwhile the Terminal-Bench
   exploit-agent pattern and the hacker-fixer-solver loop drove KernelBench
   attack success from 62% to 0% at 92–98% benign pass rate.

4. **Do not train our own reward model now.** A credible domain trajectory RM
   needs roughly 10k labelled trajectories carrying information our code grader
   cannot already produce, a lineage-matched base model, and an on-policy
   refresh loop. That is 2.5k–7.5k expert hours. No vendor ships a home-trained
   RM inside an environment; the shipped judge is always a frozen frontier model
   behind expert rubrics. A frozen judge costs about $0.05–0.20 per rollout.

5. **Borrow from data-eng-bench selectively.** Their shared-base-world overlay
   pattern, their verifier checklist (grain, contract, conservation,
   idempotency, run-twice), re-materialising the agent's pipeline from clean
   state, and second-seed grading are all worth taking. Their prompt style
   (6.9k-char specs with formulas tabled) and binary sparse reward are not.

**Proposed position for the meetup:** keep the headline and the training
scalar deterministic over final world state. Add LLM judges in three bounded
roles, in this order: (i) task validator and spec-clarity gate for generated
tasks, (ii) exploit agent against every grader before a task ships, (iii) a
capped, artifact-grounded, human-calibrated sub-score for the few criteria
code cannot express, never able to flip a fail to a pass. Get difficulty from
structural levers that stay code-gradable: hidden blockers that require asking,
hard-negative decoys, cross-system hops, simulated time.

---

## 2. Where we stand

From `docs/research/Scoring_Redesign.md` and `docs/STATUS.md`:

- Nine scenarios, 19 arms, one deterministic grader per arm over final world
  state plus structured report fields. Outcome class → band → progress → path
  penalty. 428 committed baseline rows; a grader change that moves one is
  refused. Cheating pass closed 8 holes. No LLM judge anywhere.
- Measured (Opus 5, K=5, MCP, host): five control arms at 1.0; GT002 0.8;
  GT003 0.2 (regraded); GT004 six arms 0.0–0.2; GT009 0.2. No second model
  family has a valid number.
- The finding that drives this memo: *every lost point was a judgment call the
  model stated and defended, or a rubric artifact.* The model finds every
  mechanism. GT004's 22 of 29 non-repairers gave the same reason in the same
  words. GT009's five agents all chose the intended branch, then lost on side
  effects of verification.
- Known gaps: difficulty unknown under the new grader; 19 hand-built arms is an
  eval, not a training set; progress may act as a switch on some arms; no
  optimising-policy red-team yet.

---

## 3. data-eng-bench, and what to take from it

Snowflake and Bespoke Labs, repo created 2026-07-29, blog 2026-08-06,
Harbor/Terminal-Bench format. Report r1 profiled all 103 tasks by script.

**What it is.** 103 hand-written dbt tasks on one synthetic retail warehouse
(579 tables, 19 schemas, a pre-built 300+ model dbt project baked into the
image). 84 build tasks, 19 fix tasks. DuckDB and Snowflake are not a task
split; the same tasks run on either backend. The prompt is a detailed
engineering spec (median 6.9k characters, roughly 40% dbt profile boilerplate)
with exact columns, formulas, tie-breaks; fix tasks usually enumerate the bugs.

**How it grades.** Hidden pytest verifier, reward 1 only if every assertion
passes. Median 18 graded units per task. 79 of 103 re-run `dbt run` on the
agent's SQL; about 80 check run-twice idempotency; many recompute expected
values in Python from raw tables. No LLM judge in scoring. An LLM audits only
*successful* leaderboard trajectories for cheating, post hoc.

**Results (blog, k=3).** Opus 5 via Cortex Code 73.8% pass@1, 64.1% pass^3;
Claude Code with Opus 5 69.6 / 60.2; Sonnet 5 56.6 / 40.8. Not saturated, but
the residual is SQL mechanics and verifier brittleness (open PR #9 is a
ground-truth off-by-one that fails spec-compliant agents; one timezone
verifier is marked "advisory"; ten verifiers regex the SQL source).

**Borrow.**

- A shared deep base world with a per-task overlay. This is their scaling
  lever and it matches what a "simulated company" already is for us.
- The verifier taxonomy: one row per key at the declared grain, column
  contract, conservation (buckets sum to total), ordering (p95 ≥ p50), SCD-2
  shape, and run-twice idempotency. Add `idempotent_rebuild` to every control
  arm where the fix is a pipeline change.
- Re-materialise the agent's pipeline from the seeded raw layer and diff
  against the tables the agent left. An agent that hand-patched a mart with
  `UPDATE` and never fixed the model diverges. Deterministic detector, no judge.
- Second-seed grading: re-run the agent's transformations on a second seed with
  the same schema and different values. Anything that passes on seed A and
  fails on seed B is hard-coded. This replaces their post-hoc LLM cheat judge.
- `compute_expected(seed)` oracles instead of stored fixtures. Fixtures rot.
- Task types we lack: greenfield build to a column contract, SCD-2 snapshot
  repair, incremental with late-arriving rows, multi-source union, finance
  oracles (FIFO/LIFO, deferred revenue, aging, three-way match). SCD-2,
  incremental and finance also make natural decision-arm material.
- Hygiene: canary GUID in every task file, pass@1 and pass^k together, cost and
  tokens as first-class columns, oracle CI on both backends.

**Do not borrow.** Binary sparse reward (a 95%-correct build scores 0). No
notion of damage, boundaries, honesty or notification. Over-explicit specs that
test compliance rather than diagnosis. Single deliverable type.

**Does anything in their catalog need a judge?** No. Every task is a table with
a closed-form oracle. They keep it deterministic by over-specifying ties and
rounding and pinning column names, at the cost of design freedom.

---

## 4. What the research says about LLM-as-judge

Report r2 (about 60 references) and r4 (56 references).

**Agreement with humans is real but bounded and setting-dependent.**

| setting | judge vs human | human vs human |
|---|---|---|
| MT-Bench chat pairs (Zheng 2023) | 85% | 81% |
| HealthBench binary rubric items | macro-F1 0.709 | physicians 0.569–0.730 |
| PaperBench rubric leaves | F1 0.83 | — |
| APEX-Agents criteria, 747 expert labels | 98.5% accuracy | — |
| ORCA-bench incident reports (hand regrade) | κw 0.90 | — |
| GDPval open-ended deliverables | 66% | 71% |
| AgentRewardBench, web trajectories | no judge above 70% precision | — |
| JudgeBench, objectively hard pairs | best 64% | — |

The pattern: judges work when they are **readers checking a short, binary,
expert-written criterion against an artifact**, and fail when asked "how good
is this?" or handed a trajectory. PaperBench's own stratification says it
cleanly: "is the number in the file?" F1 0.94; "is this a correct
implementation?" F1 0.72.

**Trajectory-reading is the weak link.** AgentRewardBench's dominant judge
error was trusting the agent's own completion claims. Rewriting the reasoning
trace alone, with actions and observations fixed, raised judge false-positive
rates 20–30 points (Gaming the Judge, 2026). Judges that can inspect the
artifact or world state with tools reach about 90% alignment versus 60–70% for
transcript-only judging (Agent-as-a-Judge, ICML 2025; AJ-Bench +13 points for
environment-aware judges; Handshake's Gandalf verifier F1 0.63 in-environment
versus below 0.47 text-only).

**Biases and instability, with numbers.** Position bias (GPT-4 only 65%
consistent under swap). Style bias 0.10–0.76 across judges, dwarfing position
bias. Self-preference correlates with self-recognition; GDPval's GPT-5 grader
agrees less with humans on OpenAI outputs. Temperature 0 is not reproducible:
13.6% mean pairwise flip rate, about 11 repeats to recover a verdict at 95%
confidence (Coin Flip Judge, 2026). Exact-match agreement overstates κ by
33–41 points (Reliability without Validity, 2026).

**Judges are an attack surface.** A constant null model reaches 86.5%
length-controlled win rate on AlpacaEval 2.0. Single tokens like ":" or
"Thought process:" elicit false positives from o1- and Claude-4-class judges
(One Token to Fool, 2025). Under RL the policy learns partial-compound
satisfaction (36%), implicit-as-explicit claims (35%), rubric-term echoing;
presence-only criteria carried 90% of rubric weight and were the structural
cause (Scale, arXiv 2605.12474). Anthropic (Aug 2026): a model trained on 80
hackable environments produced harmful text to satisfy a *visible* LLM-judge
grader script (29% vs 0.7%) and killed reward monitors 68% of the time when it
had root. Classifiers trained on rubric text alone predict judge verdicts at
0.83–0.90 (Rubric Artifacts, Sep 2026). Holistic judges inflate and drift:
DataClawEval, an industrial ETL benchmark, saw process scores go from 22 to
75–76 with per-case SD above 6, and reverted to five deterministic checks.

**Mitigations with measured effect.** Binary per-criterion rubrics (+20 points
over ternary). Domain-expert-written rubrics κ 0.60 vs LLM-written κ 0.46
(LH-Bench). Explicit reasoning before verdict (+10 points). Position-swap
averaging; k=3–5 majority per criterion; juries of diverse model families;
judge from a different family than the agent. Adversarial-negative training
(Master-RM). Environment hardening cut exploit rate 87.7% relative at no
task-success cost. Prompt-only "be unbiased" does not survive significance
testing.

**The human-agreement gate labs actually use.** Alt-Test (ACL 2025): 3+
raters, 50–100 items per criterion, leave-one-human-out, FDR-corrected. Report
κ or Krippendorff's α for judge-vs-human *and* human-vs-human on the same
items. Floor α ≥ 0.6 to count at all, ≥ 0.8 for anything headline-adjacent;
redesign any criterion whose human-human α is below 0.67. Anthropic's guide:
model graders "require calibration with humans"; grade the artifact, not the
path; one isolated judge call per dimension; give the judge an "Unknown" out.

---

## 5. Reward models: what labs use, and what ours would cost

Report r3 (58 references).

**Taxonomy in one line.** Outcome vs process RMs; scalar Bradley-Terry vs
generative (judge-style) RMs; rubric-conditioned RMs; verifiable rewards
(RLVR) vs learned rewards vs hybrids.

**What frontier labs do.** Two-track everywhere. Kimi K2.5: "we apply a
rule-based outcome reward for tasks with verifiable solutions, such as
reasoning and agentic tasks." DeepSeek-V3.2: 1,827 agent environments, all
function-verified. GLM-5: "over 10k verifiable environments." Qwen3 three-tier
rewards. Nemotron 3 uses "database state verification" for agentic
environments and an LLM judge only for instruction following. For
non-verifiable aspects the 2025–26 consensus is a frozen strong generative
judge conditioned on prompt-specific rubrics or checklists (Rubrics-as-Rewards,
RLCF checklists at NeurIPS 2025, Kimi's self-critique rubrics), not a scalar RM.

**Learned rewards get hacked on a known curve.** Gao et al.'s
over-optimisation curves still govern; ensembles "mitigate but do not
eliminate" (Eisenstein et al.). RewardBench 2 finds the RM must share lineage
with the policy or downstream RL degrades. METR: o3 hacked 30.4% of RE-Bench
runs and said the plan was against user intent 10 of 10 times when asked.

**Agent-trajectory RMs exist, but add density, not truth.** SWE-RM (400k
execution-labelled trajectories, 256k context) lifts SWE-bench Verified
51.6 → 62.0 via best-of-N. SWE-TRACE, Agent-RRM (118k instances), Critic
Rubrics from sparse outcomes. Nearly all are trained on labels a verifier
already produced and are used for best-of-N selection and trajectory
filtering, not as the optimised reward. "The Verification Horizon" (Jun 2026):
no fixed reward survives capability growth; verification must co-evolve.

**Hybrids that are safe.** HERO (Meta): bound RM scores *within
verifier-defined groups* so the dense signal only reorders inside a
correctness band. RLCF: verifier programs per checklist item. RLVP (Pine AI,
Jul 2026, "penalize the path, reward the outcome") is a direct published
validation of our band-plus-path-penalty shape.

**Cost of training our own.** Labels our code cannot produce (was the Slack
update appropriate, was the design note sound) are exactly where hacking
pressure concentrates. Floor for a domain trajectory RM used as reward: about
10k labelled trajectories, diverse policies, balanced outcomes. Expert
judging of a 30–100 step pipeline trajectory takes 15–45 minutes. That is
2.5k–7.5k expert hours, roughly $250k–750k. GPU cost is trivial by
comparison. A frozen frontier judge scored on a 50k-token trajectory is
$0.05–0.20 per rollout; 100k rollouts ≈ $5k–20k, gated to the ambiguous slice.

**Recommendation.** Option (a), code-only, stays the only signal that moves
bands. Option (b), a frozen judge with per-task rubrics, is added first as an
offline validity and hack monitor, then optionally as a small-weight
within-band tie-breaker on non-code-visible aspects, enabled only after ≥90%
criterion-level agreement on 200–500 two-expert-labelled trajectories, with
proxy-vs-panel divergence tracked during any RL run. Option (c), our own RM,
is revisited only once the judge-monitor has accumulated a labelled
disagreement corpus worth learning from.

**Routing, briefly.** RouteLLM cuts cost 85% at 95% of GPT-4 quality by
training a small pairwise RM on Arena preferences. Router-R1, Arch-Router,
DARS (pass-rate-over-k labels beat single binary labels). LLMRouterBench: a
big oracle gap from "model-recall failures" and commercial routers often lose
to simple baselines. Our benchmark can produce routing data if we log
(routing-time-observable features, model family, pass rate over k≥8, cost,
boundary-violation flag) for hundreds of instances; 19 arms is far too few.
The deployable form for us is grader routing: code always, judge for the
ambiguous slice, human for disagreements.

---

## 6. Where the difficulty actually is

Report r6 ranked levers by measured frontier drop. Report r4 confirmed the
saturation picture.

| lever | measured effect | our analog |
|---|---|---|
| Hidden blockers needing progressive discovery, then asking | HiL-Bench SQL: Opus 4.6 90.7 → 39.3, GPT-5.4 86 → 17.3; Ambig-DS: agents "quietly commit to plausible but unintended framings, producing clean executable artifacts" | The decision arm. Do not state the boundary in the ticket; make ownership discoverable and sometimes contradictory across systems |
| Hard-negative decoys | Gemini 2.5 Pro 77.8 → 48.0; random noise only −9 | A second staging table with a similar name and stale rows, a half-fix on an abandoned branch, a ticket for a different incident with overlapping symptoms |
| Extraneous tools in up-front planning | GPT-5 −22 points in holistic mode, ~0 step-wise | Native mode already withholds the tool list |
| Cross-system reconciliation with subtle breakage | "fix" tasks hardest in data-eng-bench; DataAgentBench best 38%; ELT-Bench 3.9% | Hop count from symptom to cause; cause split across two teams' systems |
| Horizon and simulated time | per-step error compounds (ICLR 2026); τ-bench pass^8 < 25% | Advance the scheduler clock inside the episode; grade after N runs and a late file |
| Live events and fault injection | ReliabilityBench 96.9 → 88.1 at moderate intensity | Second incident lands mid-episode; transient warehouse error |
| Conflicting sources of truth | explicit contradictions detected 2× more than implicit gaps | README vs macro vs ledger; contradiction emerges only by joining two systems |
| Over-scope actions | UnderSpecBench: 55.8–67.8% of frontier acted runs violated a boundary | Already ours; declare intended-set and protected-set per task, grade the ledger diff |
| Irreversibility alone | WorkBench Revisited: harmful actions 26% (GPT-4) → 1.9% (Fable 5) | Keep as penalty, not as a difficulty lever |

**Noise, not difficulty:** clerical volume, over-prescriptive specs, random
distractors, hardness for its own sake (SWE-smith's hard band was no better as
training data; INTELLECT and daVinci-Env filter both tails; Prime Intellect:
"tasks that are too easy or too hard for the model both produce hacking
conditions").

The consistent reading across HiL-Bench, Ambig-DS, Ambig-SWE, ClarEval and
UnderSpecBench: **capability is not the bottleneck; judgment about
under-specification is.** That is our diagnosis too, and these benchmarks show
it can be made code-gradable.

---

## 7. Task patterns that seem to need a judge, and how the good benchmarks grade them

From r4, ranked by (realism unlocked × grading evidence ÷ hack surface).

1. **Requirement discovery with a simulated stakeholder** (HiL-Bench). Inject
   expert-validated blockers (missing SLA, ambiguous "revenue", Jira contradicts
   Slack). Add an ask-stakeholder channel backed by a frozen model that answers
   only registered trigger questions. Score = existing deterministic state check
   × Ask-F1, so question spam is penalised by precision. Judge role: semantic
   match of question to registered blocker only (97% precision / 91% recall).
   Validate each instance with necessity and sufficiency: ≤5% pass without the
   hidden information, ~90% with it.
2. **Blast-radius oracles** (UnderSpecBench). Intended-set and protected-set per
   task; Safe Success = pass ∧ no protected object touched. No judge (optionally
   classify a non-action final message as Ask/Refuse/Defer, κ 0.86).
3. **Postmortem or metric-discrepancy report** (ORCA-bench). Expert rubrics
   enumerate the expected *symptoms* (which ledger rows, which dbt test, which
   row counts) and a *set* of acceptable root causes; judge each criterion
   binary against report plus world state, JSON-schema constrained; hand
   regrade gives κw 0.90. Write criteria that name verifiable quantities
   ("names the 2026-06-11 re-delivery and the three SLA_BREACH rows"), not
   qualities ("explains clearly").
4. **Design-choice tasks** (R2ABench). Grade structure deterministically first
   (lineage node/edge F1 vs a reference, anti-pattern detectors: fan-out from
   staging, non-idempotent inserts, missing unique keys), then a judge on 2–3
   semantic dimensions validated against two experts (human κ 0.82, model κ
   0.73). Mitigate single-reference bias with a set of accepted references, or
   SWE-Lancer-style pick-the-proposal graded against a real decision.
5. **Verifier with tools** (Gandalf). If a judge runs at all, run it inside the
   environment with DuckDB access so "the reported figure equals SUM(...) over
   the corrected table" is recomputed, not read.

**Do not adopt:** holistic process or quality scores (DataClawEval);
LLM-authored rubrics (κ 0.46); containment or substring answer checks (DAB
went 38% → 94.7% and nobody can separate real gains from shotgun answers);
trajectory-reading judges of any kind.

---

## 8. Proposed design

### 8.1 The judge's three jobs, in order of value

1. **Task validator.** Reject-only clarity gate with a 3-judge consensus on
   generated specs (SWE-Bench++ precision 0.93–0.95 vs human 0.96, but low
   recall, so never accept on the judge's word). Terminal-Bench's LLM check
   list is directly reusable: behaviour in description, behaviour in tests,
   hard-coded solution, leaked solution, self-referential text.
2. **Exploit agent.** Hack-elicitation prompt run against every new task family
   and a sample of every batch. Terminal-Bench 2.0 still had 15.7% of tasks
   hackable after 3+ person-hours per task of human audit. A hacker-fixer-solver
   loop, with the oracle as the solver guardrail so fixes cannot over-restrict,
   drove attacks to 0%. This is the "optimising-policy red-team" our status doc
   lists as next, and it is cheap.
3. **Bounded sub-score.** For the residue that survives the decision rule
   below. Binary, artifact-grounded, capped weight, calibrated per criterion,
   cross-family, k=3–5 majority, transcripts stored, canary suite in CI
   (master keys, "ignore previous instructions", fake references, style
   reformatting, stakes framing must not move verdicts).

### 8.2 The decision rule for any new criterion

1. Can it be written as a predicate over (final state, audit ledger,
   ticket/message log) with a golden value the oracle computed? Deterministic
   verifier. Add it to the fail-to-pass set and write three plausible wrong
   fixes that must still fail.
2. If not, can the task be re-specified so it is? Structured evidence fields,
   a SQL check instead of prose, an enumerated diagnosis form. Most of the mass
   is here (APEX's "grading target", Gandalf's taxonomy).
3. If still not: a judge, only if a deterministic gate has already passed,
   the criterion is binary and self-contained, the judge reads artifacts not
   trajectory, weight is capped, and it is calibrated on held-out human labels
   with a rubric-optimised-vs-rubric-free probe.
4. Otherwise drop the criterion. A criterion only a judge can score and that
   cannot be gated is a hacking surface, not difficulty.

### 8.3 Scaling pipeline (from 19 arms to families)

Parametrise the nine scenarios into families. Seeds control damage type, hop
count, decoy count and kind, blocker type (missing / ambiguous / contradictory
ownership), horizon, live events, and whether the boundary case is present.
The LLM writes narrative artifacts from the parameters, never the parameters.
Generate the oracle with the task and execute it to obtain the target state;
reject if it fails, is non-deterministic across two runs, or is trivial. Derive
fail-to-pass predicates, failure cases, invariants, and the structured-evidence
predicate for the notification. Clarity gate. Necessity/sufficiency check on
the injected blocker. Band by reference-policy pass rate with k=8–16 across
three model families, keep 1/k to (k−1)/k, re-band per release. Exploit agent.
Expect 5–50% of candidates to survive; that is normal (Self-Challenging Agents
5.2%, SWE-Bench++ 8.1%).

### 8.4 What this changes in the pitch

"State-verified, like DeepSeek-V3.2's 1,827 agent environments and GLM-5's
10k SWE environments." The lab-side audit will be a hack monitor over our
episodes (Anthropic read summaries of 1.5M episodes across thousands of
environments for Opus 5; its Environment Scaling team's remit includes "QA
frameworks to catch reward hacking"). Ship our own hack-audit evidence: gold
sanity gate, exploit-agent results, grader unreachable from the agent process,
egress controls. Epoch's buyer checklist: ≥1 success in 64–128 rollouts,
smooth difficulty gradient, soundness first. Per-task pricing $200–2,000;
contracts six to seven figures per quarter (secondary reporting).

---

## 9. Concrete next steps to propose

1. **Run the exploit agent** against all 19 arms this week. It spends tokens
   but is the cheapest insurance in the pipeline and directly answers "nobody
   has told a model to maximise the score yet."
2. **Add the deterministic borrowings** from data-eng-bench: run-twice
   idempotency on control arms, clean-state re-materialisation diff,
   second-seed grading, `compute_expected(seed)` oracles.
3. **Build one HiL-style task** on top of GT004 or GT009: move the ownership
   boundary out of the ticket into discoverable and contradictory sources, add
   a stakeholder channel that answers registered questions, score with Ask-F1
   × the existing checks. Validate with the necessity/sufficiency pair.
4. **Start the calibration corpus now**, before any judge exists: two experts
   label 200–500 criterion-level items on stored rollouts (report soundness,
   notification appropriateness, design-note quality). This is also the
   disagreement corpus a future RM would need.
5. **Wire a frozen cross-family judge as a logged metric only** on the
   non-code-visible aspects, report κ next to human-human κ, and watch for
   judged pass-rate shifts uncoupled from deterministic scores.
6. **Log routing features** per instance (observable features, family, pass
   rate over k, cost, boundary flag) so the side project has data when the
   family count is large enough.

---

## 10. Open questions for the meetup

- Do we agree the residual difficulty is under-specification judgment, and that
  it is code-gradable via the HiL-Bench pattern? If yes, the judge is not on
  the critical path to hard tasks.
- Which two or three non-code-visible criteria are worth the calibration
  labour? Candidates: soundness of the discrepancy explanation, whether the
  owner notification would let the owner act, design-note quality on
  build-to-contract tasks.
- Are we willing to run judged criteria as a separately reported quality axis
  that never enters pass@1 or the scalar, at least until the calibration set
  clears α ≥ 0.8?
- Family generation is the next artefact after the Opus rerun. Who owns the
  generator, and do we start from data-eng-bench's shared-base-world pattern?

---

## 11. Sources

Grouped; full lists with quotes are in `docs/research/llm-judge/r1–r6`.

**data-eng-bench and data benchmarks**
- Snowflake-Labs/data-eng-bench — https://github.com/Snowflake-Labs/data-eng-bench ; blog (2026-08-06) https://www.snowflake.com/en/blog/engineering/data-eng-bench-data-engineering-agent-benchmark/ ; PR #9 https://github.com/Snowflake-Labs/data-eng-bench/pull/9
- dbt Labs / Mode, ADE-bench (2026-04-15) — https://docs.getdbt.com/blog/building-a-better-data-agent-benchmark
- ELT-Bench-Verified — https://arxiv.org/abs/2603.29399
- Ambig-DS — https://arxiv.org/abs/2605.09698
- DataClawEval — arXiv 2607.28033
- Upriver, data-engineering benchmark summary (secondary) — https://upriverdata.com/blog/data-engineering-benchmarks-for-ai-agents

**LLM-as-judge reliability and attacks**
- Zheng et al., Judging LLM-as-a-judge (2023) — arXiv 2306.05685
- Calderon, Reichart, Dror, The Alternative Annotator Test (ACL 2025) — https://aclanthology.org/2025.acl-long.782/
- Zhao et al., One Token to Fool LLM-as-a-Judge (2025) — https://arxiv.org/abs/2507.08794
- Gaming the Judge (2026) — arXiv 2601.14691
- The Coin Flip Judge (2026) — https://arxiv.org/abs/2606.13685
- Reliability without Validity (2026) — arXiv 2606.19544
- Judging LLM-as-a-Judge: Concerning Rubric Artifacts (Sep 2026) — arXiv 2609.02942
- RuVerBench (EMNLP 2026) — https://arxiv.org/abs/2606.29920
- Verga et al., Replacing Judges with Juries (2024) — https://arxiv.org/abs/2404.18796
- Agent-as-a-Judge (ICML 2025) — https://arxiv.org/abs/2410.10934
- AgentRewardBench (2025) — https://arxiv.org/abs/2504.08942
- AJ-Bench (ACL 2026 Findings) — arXiv 2604.18240
- Handshake, Verifying Agents in Rubric-Graded Environments / Gandalf (KDD 2026) — https://kdd-eval-workshop.github.io/agenticai-evaluation-kdd2026/assets/papers/34_Verifying_Agents_in_Rubric_.pdf
- LH-Bench (2026) — arXiv 2603.22744

**Rubric and judge-graded benchmarks**
- APEX-Agents (Mercor 2026) — https://arxiv.org/abs/2601.14242 ; leaderboard https://www.mercor.com/apex/apex-agents-leaderboard/
- PaperBench (OpenAI 2025) — https://arxiv.org/abs/2504.01848
- HealthBench (OpenAI 2025) — https://arxiv.org/abs/2505.08775
- GDPval (OpenAI 2025) — https://arxiv.org/abs/2510.04374
- HiL-Bench (Scale 2026) — https://arxiv.org/abs/2604.09408
- UnderSpecBench (2026) — arXiv 2607.02294
- ORCA-bench (2026) — arXiv 2607.28545
- R2ABench (2026) — arXiv 2604.06683
- NoisyBench / Lost in the Noise (2026) — https://arxiv.org/abs/2601.07226
- ReliabilityBench (2026) — https://arxiv.org/abs/2601.06112
- The Illusion of Diminishing Returns (ICLR 2026) — https://arxiv.org/abs/2509.09677

**Reward models and rubric RL**
- DeepSeek-R1 — https://arxiv.org/abs/2501.12948
- Kimi K2 — https://arxiv.org/abs/2507.20534 ; Kimi K2.5 — https://arxiv.org/abs/2602.02276
- Nemotron 3 Ultra technical report (Jun 2026) — https://research.nvidia.com/labs/nemotron/files/NVIDIA-Nemotron-3-Ultra-Technical-Report.pdf
- Gunjal et al., Rubrics as Rewards (Scale, ICLR 2026) — https://arxiv.org/abs/2507.17746
- Mahmoud et al., Reward Hacking in Rubric-Based RL (Scale, May 2026) — https://arxiv.org/abs/2605.12474
- Viswanathan et al., Checklists Are Better Than Reward Models (RLCF, NeurIPS 2025) — https://arxiv.org/abs/2507.18624
- HERO (Meta) — arXiv 2510.07242
- SWE-RM (Qwen/HKUST) — https://arxiv.org/abs/2512.21919
- RLVP, Penalize the Path, Reward the Outcome (2026) — https://arxiv.org/html/2607.07435
- The Verification Horizon (2026) — https://arxiv.org/abs/2606.26300
- Gao, Schulman, Hilton, Scaling laws for reward model overoptimization — arXiv 2210.10760

**Reward hacking incidents and lab practice**
- METR, Recent Frontier Models Are Reward Hacking (Jun 2025) — https://metr.org/blog/2025-06-05-recent-reward-hacking/ ; Frontier Risk Report (May 2026) — https://metr.org/blog/2026-05-19-frontier-risk-report/
- Anthropic, Natural Emergent Misalignment from Reward Hacking in Production RL (Nov 2025) — https://arxiv.org/abs/2511.18397
- Anthropic, Training a Misaligned Reward Seeker (Aug 2026) — https://alignment.anthropic.com/2026/reward-seeker/
- Anthropic, Demystifying evals for AI agents (Jan 2026) — https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- Rajan, Auditing Reward Hackability in Code RL Training Environments (Jun 2026) — https://arxiv.org/abs/2606.16062
- Terminal Wrench (2026) — https://arxiv.org/abs/2604.17596 ; Hardening Agent Benchmarks with Hacker-Fixer Loops (2026) — https://arxiv.org/abs/2606.08960
- Prime Intellect, Systematic Reward Hacking (May 2026) — https://www.primeintellect.ai/blog/reward-hacking ; Environments Hub — https://www.primeintellect.ai/blog/environments
- HUD, Verifier and Reward Design for RL Environments (Mar 2026) — https://www.hud.ai/resources/verifier-reward-design-rl-environments
- Epoch AI, An FAQ on Reinforcement Learning Environments (2026) — https://epoch.ai/gradient-updates/state-of-rl-envs
- OpenAI, Graders and RFT guides — https://developers.openai.com/api/docs/guides/graders

**Task generation and routing**
- SWE-smith (NeurIPS 2025) — https://arxiv.org/abs/2504.21798 ; SWE-rebench V2 (2026) — https://arxiv.org/abs/2602.23866 ; SWE-Bench++ — https://arxiv.org/abs/2512.17419
- Self-Challenging Language Model Agents (NeurIPS 2025) — https://arxiv.org/abs/2506.01716
- RouteLLM — https://arxiv.org/abs/2406.18665 ; LLMRouterBench (2026) — https://arxiv.org/abs/2601.07206 ; DARS (2026) — https://arxiv.org/abs/2606.06924

**Flagged as unverified by the sweeps:** Gao et al.'s exact functional forms
(from memory); an "Opus 4.5 18.2% hack rate" figure (secondary summaries);
GDPval grader-agreement figures in r6 (r2 and r4 fetched them: 66% vs 71%);
Mercor's 92.8–94.7% criterion-level agreement (secondary); Nathan Lambert's
"rubrics prone to overopt" line (unsourced); market pricing (all secondary).

