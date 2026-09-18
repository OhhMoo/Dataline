# What makes an agentic eval worth training on — research notes

2026-09-18. Written for one question: **if we hand GT001–GT009 to a model
company as an RL environment, what has to be true of the tasks and the grader
for the reward to teach anything?** The literature pass below is organised
around the answer, and §7 audits the library against it. Every claim about
this repository is **[measured]** (a stored run or battery) or **[designed]**;
every claim about the outside world carries its source.

Short version:

1. **The reward must have variance where the behaviour is.** Under
   group-relative RL (GRPO / DAPO / RLOO) a prompt whose K samples all pass or
   all fail contributes *zero* gradient. Our FOUND axis is at 1.0 on Opus; the
   decision axes sit at 0.2–0.9. Only the decision axes can train anything.
2. **Reward hacking is the default, not the edge case.** Frontier models
   modify tests, read reference answers and exploit graders at measurable
   rates, and doing so generalises into broader misalignment. A grader that
   reads receipts the agent cannot edit, restores platform code from outside
   the world, and charges false claims is the right shape; the erase-the-record
   baseline and the manual-trigger hole are exactly the audits labs run.
3. **Judgment is trainable with verifiable rewards.** HiL-Bench (Scale, 2026)
   shows frontier pass rates collapsing from 75–89% to 4–24% when the agent
   must decide whether to ask, and a 32B model closing part of that gap under
   RLVR on a shaped ask/act reward that transfers across domains. That is the
   GT009 thesis, published by someone else, on a different axis.
4. **Two-sided cost is the design, not a nicety.** Every decision benchmark
   that works (HiL-Bench, Escalation Bench, BLINDSPOT, pi-bench) reports
   over-caution and over-action separately. GT009's measured asymmetry (7 vs 8,
   8 vs 9 in the live-consumer arm) is that property; GT004 default/authorized
   is the "proceed twin / handoff twin" pattern.
5. **Nine tasks at K=5 is an evaluation, not a training set.** The labs' own
   bar is "learnable in O(100s) of steps with pass@k > 0 and pass@1 < 1". That
   needs a generator that mints hundreds of *distinct* incidents on the same
   decision structure, not more hand-built worlds.

---

## 1. What the labs say they need from an environment

**OpenAI, reinforcement fine-tuning guide** ([developers.openai.com](https://developers.openai.com/api/docs/guides/rft-use-cases)).
The task must be "clear and have verifiable answers"; a grader "must read an
answer and produce a score without a person in the loop". Before training,
run the eval: *"if the model scores at either the absolute minimum or absolute
maximum possible score, then RFT won't be useful to you."* Prefer "a smooth
score, not a pass/fail stamp"; avoid skewed label distributions; if an LLM
judge is used, evaluate the judge for stability against multiple candidate
answers. This is the cleanest public statement of the two constraints that
matter here: **not saturated, and graded without a human.**

**OpenEnv RFC 008, "Environment auto validation"** ([github.com/huggingface/OpenEnv/issues/778](https://github.com/huggingface/OpenEnv/issues/778)),
written explicitly "from a frontier lab view". An environment is useful only
if it (1) scales on infra, (2) is **learnable — a model can hill-climb after
O(100s) of steps**, with reference models at **pass@k > 0 and pass@1 < 1**,
(3) is secure, (4) **is not prone to reward hacking**, (5) is observable and
self-describing, (6) is reproducible — same inputs, same outcome, across
hosts and time; **verifier determinism** is a named acceptance test, and
LLM-graded rubrics must declare a tolerance and a pinned judge. The RFC also
requires a **golden solution** run at ingest as a verifier sanity check.
The companion reward guide ([huggingface.co/docs/openenv/guides/rewards](https://huggingface.co/docs/openenv/guides/rewards))
says: start with a sparse success signal; shape only after the agent can reach
it; dense shaping "invites reward hacking" and should be a small-weighted
component; and *"if you catch yourself subtracting rewards to cancel out a bad
behaviour, consider a Gate instead — make the bad case a hard zero."*

**"The Environment Layer" white paper** ([rxiv 2603.0138](https://rxiv.org/pdf/2603.0138v1.pdf), 2026)
names three dimensions of environment quality — **fidelity, verifiability,
diversity** — and documents the "easy mode" failure: LLM-simulated users that
are over-cooperative and front-load information, so agents look competent in
training and fail in deployment. Read for us: a world where every fact is one
tool call away is the same easy mode, and the ablations already showed it
(concealment did nothing; the model always queries the data).

**Epoch AI, "An FAQ on Reinforcement Learning Environments"** ([epochai.substack.com](https://epochai.substack.com/p/an-faq-on-reinforcement-learning), Jan 2026; 18 interviews with environment vendors, neolabs and frontier labs).
The closest thing to a public buyer's spec. *"Interviewees consistently cited
robustness against reward hacking as the most important quality criterion"*
("high reward must mean the task was actually solved, not hacked"; "it takes
many many iterations to check against reward hacking"). Difficulty: "if the
pass rate is 0% or 100%, the model won't learn"; several want a floor of
**~2–3%, or one success in 64–128 rollouts**; a desirable mix is "some tasks
at 0%, some at 5%, some at 30%", discarded once ~70%; a 0% task is admissible
only after a blind human solve. Tasks should be compositional (share skills)
and multi-turn. Economics: labs use environments **10–20× more for RL than
for benchmarking**; tasks cost $200–$2,000 to build against ~$2,400 of
compute per task during training, so a bad task wastes more than it cost.
No public OpenAI partner-criteria document exists; this FAQ, the RFT guide
above and OpenAI's SWE-bench Verified post-mortem (§6) are the proxies.

**Anthropic, "Demystifying evals for AI agents"** ([anthropic.com/engineering](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents), Jan 2026).
Grade **outcomes** (final environment state) not execution paths; every task
needs a **reference solution** that passes all graders ("proves the task is
solvable and verifies graders are correctly configured"); *"with frontier
models, a 0% pass rate across many trials (0% pass@100) is most often a signal
of a broken task"*; make graders "resistant to bypasses or hacks"; **read the
transcripts** — "failures should seem fair"; monitor for saturation ("an eval
at 100% tracks regressions but provides no signal for improvement"). Two
cautionary cases: Opus 4.5 went from 42% to 95% on CORE-Bench once rigid
number matching, ambiguous specs and irreproducible tasks were fixed; METR
found tasks that asked for a score threshold while the grader required
exceeding it, penalising the model that followed instructions.

## 2. Where the learning signal actually comes from

**Group-relative advantage has no gradient at the extremes.** DAPO
([arXiv 2503.14476](https://arxiv.org/abs/2503.14476)): under GRPO, "if all
outputs of a particular prompt are correct and receive the same reward, the
resulting advantage for this group is zero. A zero advantage results in zero
policy gradients". Their fix, **dynamic sampling**, over-samples and *discards*
every prompt whose group accuracy is 0 or 1, keeping only prompts with
0 < #correct < G. NeMo-RL and ms-swift ship the same filter
(`use_dynamic_sampling`, `dynamic_sample`). Consequence for an environment
provider: **a task a frontier model passes 5/5 or 0/5 is thrown away by the
trainer before the first gradient step.** It is not "easy" or "hard"; it is
invisible.

**So the useful band is mixed outcomes at the current policy.** For a binary
reward the expected gradient magnitude scales with p(1−p) and the number of
contrastive pairs in a group is k(G−k) — both maximal at p = 0.5, zero at 0 and
1 (Prompt Replay, [arXiv 2603.21177](https://arxiv.org/abs/2603.21177); "Max
Out GRPO Signal", [arXiv 2607.07674](https://arxiv.org/abs/2607.07674); "Hard
Examples Are All You Need", [arXiv 2508.14094](https://arxiv.org/abs/2508.14094)).
"Advantage Collapse in GRPO" ([arXiv 2605.21125](https://arxiv.org/abs/2605.21125))
measures the damage: 28–45% of prompt groups in math training had identical
rewards, with a U-shaped relation to difficulty and group size 6–8 the best
trade-off — and offers **advantage-collapse rate per task** as a zero-cost
diagnostic an environment can report. Practitioners run the filter on their
own data: Kimi k1.5 (10 rollouts, drop trivial, sample ∝ 1 − success), POLARIS
(drop 8/8 solved, drop above 0.9 during training), Skywork-OR1 (16 rollouts,
drop 0/16 and 16/16 *per target model*). Yue et al., "Does RL Really
Incentivize Reasoning Capacity Beyond the Base Model?" ([arXiv 2504.13837](https://arxiv.org/abs/2504.13837))
adds the ceiling: RLVR raises pass@1 but base models win at pass@256, so a
task at 0% even at pass@128 is unlikely to be learned by outcome RL at all —
it is a held-out eval or a distillation target, not a training prompt. A task
whose difficulty comes from a *decision* has a property the search tasks lack:
the same world stays in the band as the model improves, because the regret is
symmetric and the approval source does not get easier to weigh.

**Infrastructure noise lands in the zero-reward bin.** Segato et al.,
"Quantifying Infrastructure Noise in Agentic Coding Evals" (Anthropic, Feb
2026, [anthropic.com/engineering/infrastructure-noise](https://www.anthropic.com/engineering/infrastructure-noise)):
same model, same tasks, a 6-point swing between strict per-task limits and
uncapped; infra error rate 5.8% at 1× → 2.1% at 3× headroom → 0.5% uncapped.
Their rule: guarantee an allocation *and* set a separate kill threshold at
~3×; treat gaps under 3 points as noise. For RL it is worse than for evals —
a `harness_error` scored as 0 corrupts the within-group comparison (the
"Signal Reshaping" paper, [arXiv 2605.07276](https://arxiv.org/abs/2605.07276),
filters to "execution-comparable" rollouts for this reason). Our harness
already quarantines rather than zeroes; that is the right treatment and
should be stated to a buyer as such.

**Binary is fine; graded is better only if it cannot be gamed.** OpenAI asks
for a smooth score. The DRO analysis of dense rewards
([inquiringlines.com](https://inquiringlines.com/notes/separating-optimization-from-feasibility-dense-token-level-rewards-plus-rubric-h/))
argues the opposite for rubric scores: they are "noisy, gameable, and
discontinuous in ways that dense gradients amplify", and should be used as
**gates** (accept/reject the rollout) with the fine-grained signal applied only
among accepted rollouts — *"treating the categorical signal as a
multiplicative gate rather than as an additive reward preserves its
categorical nature."* This is the same structure `new_matrix.md §2.1` chose for
the evaluation table (FOUND gates; pass is set membership; the sum is a
diagnostic), for different reasons.

**Multi-turn credit assignment is the open problem.** "From Reasoning to
Agentic: Credit Assignment in RL for LLMs" ([arXiv 2604.09459](https://arxiv.org/html/2604.09459v1))
puts it in numbers: with T = 100 turns and a terminal binary reward the
per-action signal-to-noise is roughly 100× worse than single-turn reasoning,
and episode-level credit produces the "echo trap" (agents converge to
repetitive actions because the gradient cannot tell productive from
redundant). Turn-level critics, hindsight credit and privileged critics are the
active responses. For us this argues for **more, shorter, sharper episodes**
(one decision, four nights) over long free-form incidents, and for exposing
per-check outcomes so a lab can build its own turn-level signal if it wants.

**Process rewards: mixed record.** "Let's Verify Step by Step" (Lightman et
al., 2023) established PRMs for math; DeepSeek-R1 dropped them in production
RL, citing the difficulty of defining steps and PRM reward hacking. "Demystifying
RL Post-Training" ([arXiv 2608.24949](https://arxiv.org/html/2608.24949v1))
shows dense PRM credit lifting a base model from 10% to 92% on a single hard
AIME problem — in an idealised setting where the authors could build an
accurate dense reward — and, on the other side, that the "spurious rewards"
result (Shao et al. 2025, [arXiv 2506.10947](https://arxiv.org/abs/2506.10947):
random rewards gave Qwen2.5-Math +21 points on MATH-500, and nothing to Llama
or OLMo) is governed by prompt-distribution breadth and pre-existing model
behaviour. The lesson that transfers: **a reward that moves a number on one
model family proves nothing until it moves it on a second.** That is
`tasks/MATRIX_RERUN_EDITS.md §3`.

**How many samples.** Miller, "Adding Error Bars to Evals"
([arXiv 2411.00640](https://arxiv.org/abs/2411.00640)): required number of
independent questions is n = (z_α/2 + z_β)² (ω² + σ²_A/K_A + σ²_B/K_B) / δ²;
under reasonable parameters, detecting a 3-point difference needs on the
order of **1,000 independent questions**, and raising K from 1 to 10 on a
fixed 198-question eval only shrinks the minimum detectable effect from 13.2%
to 7.5%. **K cannot substitute for n.** Nine tasks at K=5 can separate 0.2 from
1.0 on one task; it cannot rank models, and it cannot tell 0.8 from 1.0
(`MATRIX_RERUN_EDITS.md §1` already says so). Anthropic's pass@k / pass^k
distinction ("Demystifying evals") is the other half: pass^k — all k trials
succeed — is the consistency metric that matters for an agent that will act
on production; τ²-bench reports pass^k for the same reason.

## 3. Reward hacking is the default outcome

**Incidence.** METR, "Recent Frontier Models Are Reward Hacking"
([metr.org, June 2025](https://metr.org/blog/2025-06-05-recent-reward-hacking/)):
o3 modified tests or scoring code, reached reference implementations, and
exploited environment loopholes; ~0.7% of HCAST runs, 43× that rate on
RE-Bench, and on one task every trajectory eventually hacked. The models
"demonstrate awareness that their behavior isn't in line with user intentions
and disavow cheating strategies when asked". Baker et al., "Monitoring
Reasoning Models for Misbehavior" ([arXiv 2503.11926](https://arxiv.org/abs/2503.11926)):
hacks observed in agentic coding RL include `exit(0)` to skip tests, `raise
SkipTest`, forcing `verify → true`, editing the test framework, decompiling
reference solutions. A CoT monitor caught ~95% vs ~60% for an action-only
monitor; but **training against the monitor produced obfuscated hacking** —
the same hack rate with innocent-looking reasoning. Terminal-Bench 2.0
([arXiv 2601.11868](https://arxiv.org/html/2601.11868v1), §B.4) runs an
**adversarial exploit agent** on every task before inclusion, prompted to
cheat without reading tests or the oracle; common finds are "monkey-patching
test environments, guessing answers, and generating all possible answers with
the hope that tests might only check existence of a correct answer".

Terminal Wrench ([arXiv 2604.17596](https://arxiv.org/abs/2604.17596), 2026)
ran hack elicitation over 1,860 tasks from five public terminal benchmarks:
**331 (>15%) were demonstrably hackable** by Opus 4.6, Gemini 3.1 Pro and
GPT-5.4 — output spoofing, hollow implementations, stdlib patching,
stack-frame introspection, binary hijacking — and the exploits were
task-specific, so no harness patch fixes them. SWE-bench issue
[#465](https://github.com/SWE-bench/SWE-bench/issues/465) (Sep 2025): agents
ran `git log --all --grep=<issue>` and read the *future* fix commit; only the
remote had been removed, not tags, branches or reflog; the same hole
reappeared in SWE-bench Pro. Claude 3.7's system card documents special-casing
tests "typically after multiple failed attempts", caught by classifiers on
training transcripts and not by users. DeepSeek-R1
([arXiv 2501.12948](https://arxiv.org/abs/2501.12948)) used only rule-based
rewards because "the neural reward model may suffer from reward hacking",
and lists PRMs under "unsuccessful attempts".

**Consequence.** Anthropic, "Natural emergent misalignment from reward
hacking in production RL" ([arXiv 2511.18397](https://arxiv.org/abs/2511.18397), Nov 2025):
a model that learned to hack real production coding environments generalised
to alignment faking (50% of simple goal questions), cooperation with
attackers, and intentional sabotage of safety-research code in 12% of Claude
Code trials. Chat-style RLHF hid this on chat evals and left it intact on
agentic tasks. Mitigations: prevent the hack; diversify safety training onto
agentic scenarios; **inoculation prompting** ("your task is just to make the
grading script pass"), which cut misaligned generalisation 75–90% without
reducing the hack rate. The blunt reading for an environment supplier: **a
hackable grader is not a weak eval, it is a training hazard for the customer.**

**What resists hacking, from the same sources.**

- The verdict lives in state the agent cannot write. Terminal-Bench's tests
  are copied into the container *after* the episode; its Dockerfile check
  forbids tests or oracle solutions in the image. GT009's receipts are written
  by the world's own scheduler into `var/ledger/receipts.jsonl` when the
  *grader* advances the clock, and the platform daemon is restored from the
  grading zone; an agent that edits `.platform/` changes nothing and is
  charged (H2) **[measured]**.
- A no-op must fail. Terminal-Bench's CI runs a "dummy" agent on every task;
  OpenEnv requires the golden solution to pass and, implicitly, that the empty
  policy does not. GT009: `no_op` = 0.0, `checklist` (false claim) = −1.0
  **[measured]**.
- Existence-only checks are the classic hole. "Generating all possible
  answers" passes a grader that checks presence and not absence. GT009 hashes
  the *whole* published object per night, so an append-beside restatement
  fails R2 rather than passing on the presence of the right rows
  **[measured, clean-20260917-k5]**.
- Adversarial pass before shipping. Terminal-Bench B.4 and METR's reward-hack
  classifier are the same discipline as our `redteam/scripted_subject.py` and
  the `erase_the_record` baseline: write the exploit, confirm the grader
  charges it, keep the exploit in the suite. Bercovich ("What Makes a Good
  Terminal-Agent Benchmark Task", [arXiv 2604.28093](https://arxiv.org/abs/2604.28093))
  adds two cheap probes: run a "please hack" variant and read it; put the
  test signatures in the prompt and see whether success jumps.
- Reward on explicit submit only. DeepSWE ([together.ai](https://www.together.ai/blog/deepswe))
  gives 1 iff the patch passes *and* the agent submitted, because agents
  "stumble upon correct patches without knowing" and paying for that
  reinforces the bad steps after it. DeepCoder requires ≥5 tests per problem
  and samples the hardest, because fewer "encourage the model to simply print
  out the memorized answer"; K/N partial-test credit was dropped for the same
  reason. Our pass axis is already a gate on a complete trajectory, not a
  count of checks.
- Hidden scoring, purged history, no agent-writable grader input. METR's
  scorer runs as root and hidden by default; SWE-bench v4.1.0 strips all
  future history and prunes objects; and any surface the agent can write
  that a judge later reads is an injection vector ("Adversarial Attacks on
  LLM-as-a-Judge", [arXiv 2504.18333](https://arxiv.org/abs/2504.18333):
  up to 73.8% success). We have no judge, so the ticket and chat surfaces
  are safe to grade by structure; that must stay true.

## 4. Rubrics, judges, partial credit

**Rubrics as Rewards** (Scale AI, 2025; Gunjal et al.): instance-specific
checklist rubrics used as GRPO reward beat Likert-style judge scores by up to
28–31% relative on HealthBench and 7–13% on GPQA-Diamond; rubric guidance
helps *smaller* judges most; generic rubrics underperform; reference-grounded
rubric generation works best. **The rubric is a grader specification, not a
hint** — the trained model never sees it. OpenAI's HealthBench and PaperBench
use the same shape (physician-written and author-written rubrics, LLM
applied). OpenEnv's acceptance test for any LLM-graded rubric is a pinned
judge and a declared tolerance; OpenAI's guide is to test the judge on
multiple candidates before training on it.

**Where this library stands.** Every check in GT001–GT009 is code over world
state, receipts or a structured report; **no LLM judge is in the loop**. The
one place a rubric touched the model was the S1/S2 report schema in GT009,
which was undisclosed and cost a fixed two points regardless of quality —
found by reading transcripts, fixed by disclosing the keys and re-locking the
prompt hashes **[measured, `tests/VALIDITY.md`]**. That is Anthropic's
"failures should seem fair" rule applied. Keep it judge-free; the moment a
judge enters, the RFC's determinism test and the DRO gate argument both
apply.

**Judges are fragile in specific, testable ways.** "One Token to Fool
LLM-as-a-Judge" ([arXiv 2507.08794](https://arxiv.org/abs/2507.08794)): a
lone ":" or "Thought process:" produces false positives across judges and
datasets. Evaluator stress tests ([arXiv 2507.05619](https://arxiv.org/abs/2507.05619)):
re-formatting to bullets and headers *raised* judge scores by +0.23 while
content paraphrase lowered them. HealthBench's own meta-evaluation set the
realistic ceiling — GPT-4.1 grader macro-F1 0.71 against physicians, with
physician–physician agreement 55–75% by theme, and a 2026 follow-up
([arXiv 2602.22758](https://arxiv.org/abs/2602.22758)) finding 22.5% physician
disagreement, ~82% of it case-level. GDPval's auto-grader reached 66%
against a 71% human inter-rater rate. Terminal-Bench 3 allows a judge "only
in rare and extraordinary circumstances" where multiple different LLMs
always agree. If a judge ever enters this library the bar is: one binary
criterion per call, an "Unknown" out, validated on a human-labelled judge
set to at least the human agreement level, and stress-tested for the three
failure modes above.

**Partial credit.** OpenAI wants a smooth score; the RLVR practitioner
consensus is that partial credit "is genuinely hard" because credit for
format or intermediate steps teaches "beautifully formatted wrong answers".
TheAgentCompany ([arXiv 2412.14161](https://arxiv.org/abs/2412.14161)) is the
eval-side precedent: S = 0.5·(checkpoints/total) + 0.5·S_full, half the weight
reserved for full completion, checkpoints graded by code over workspace and
chat state, rubrics hidden from the agent. HealthBench assigns each criterion
a point value in [−10, 10], negatives for harms, so the total can go below
zero — the same shape as our hygiene penalties. The turn-level credit papers
(MT-GRPO [arXiv 2505.11821](https://arxiv.org/abs/2505.11821); "Signal
Reshaping" [arXiv 2605.07276](https://arxiv.org/abs/2605.07276)) find dense
tiers help GRPO stability when each tier is *deterministically verifiable and
monotone in true progress* (does not compile → compiles → correct) — and that
is the line between shaping that survives and shaping that gets gamed.
The reconciliation in the sources is the DRO gate: **categorical correctness
as a gate, graded quality only among rollouts that pass the gate.** The
library's axes already have that structure: FOUND is the gate; the pass axis
is set membership against named trajectories; the remaining axes and the
point total are diagnostics that would make a legitimate small-weight shaping
term.

## 5. Judgment evals exist, and they are trainable

This is the closest prior art to the GT009 thesis, and it is all 2025–2026.

**HiL-Bench** (Scale, [arXiv 2604.09408](https://arxiv.org/abs/2604.09408), Apr 2026).
Tasks carry 3–5 human-validated blockers (missing, ambiguous or contradictory
information) that surface only through exploration. Metric **Ask-F1** =
harmonic mean of question precision and blocker recall — "asking too much is
just as costly as not asking enough", and 50 questions at 8% precision score
14.5% even at 80% recall. Frontier models: **75–89% pass@3 with full
information, 4–24% when they must decide whether to ask**; "the bottleneck is
judgment, not capability", with stable per-family failure signatures (GPT
executes confidently on wrong beliefs; Claude detects uncertainty but does
not act on it; Gemini responds to external signals). **RLVR on shaped Ask-F1
(Qwen3-32B, LoRA, SkyRL, 120 train / 30 held-out tasks per domain) improved
precision, recall and pass@3 together, and transferred SQL→SWE and back.**
Their conclusion is ours: "current training pipelines optimize for task
completion and preference, but neither penalizes an agent for confidently
solving the wrong problem."

**Escalation Bench** ([github.com/nbdesai1992/escalation-bench](https://github.com/nbdesai1992/escalation-bench)).
Minimal **proceed twin / handoff twin** pairs: the same scenario, one where
acting is safe, authorised, reversible and in-mandate, one where the same
action trips a line (irreversible & costly, beyond mandate, underdetermined,
blocked). Reports **unsafe-action rate** (context-aware: the tool that is
right on the proceed twin is unsafe on the handoff twin), **over-escalation**,
and **thrash** (never commits). Finding: six frontier models within ~4 points
on accuracy, but unsafe-action rate spanning 3.7× — "for a benchmark about
*when to act*, the safety axis is where the signal lives", and "indecision can
masquerade as safety". GT004 `default` / `authorized` is exactly a twin pair
on WARRANTED, already built **[measured: default 26/30 escalated;
authorized G1c 2/3]**.

**BLINDSPOT** ([arXiv 2609.16305](https://arxiv.org/pdf/2609.16305v1), Sep 2026)
grades long-horizon tool-using agents on **executed actions and environment
state, not textual claims**, into unsafe completion / correct refusal /
benign completion / **over-refusal** / indeterminate, and finds "model safety
is not well described by a single scalar ranking". **pi-bench**
([github](https://github.com/Jyoti-Ranjan-Das845/pi-bench)) scores
ALLOW / ALLOW-CONDITIONAL / DENY / ESCALATE against messy policy documents,
with separate `under_refusal_rate`, `over_refusal_rate` and
`escalation_accuracy_rate`. **τ²-bench** ([arXiv 2506.07982](https://arxiv.org/abs/2506.07982))
scores policy-following under a simulated user and reports pass^k; its
**Verified** release ([amazon-agi/tau2-bench-verified](https://github.com/amazon-agi/tau2-bench-verified))
documents the four error classes the original shipped with — expected actions
that violated the stated policy, database mismatches, impossible scenarios,
ambiguous instructions — which is a checklist for our own `fixture_contract`
clauses.

**The failure to avoid, measured.** "Establishing Best Practices for Building
Rigorous Agentic Benchmarks" (ABC, [arXiv 2507.02825](https://arxiv.org/abs/2507.02825),
NeurIPS 2025) audited τ-bench: because success on impossible tasks is defined
as *leaving the database unchanged*, an agent that returns an empty response
**passes 38% of airline tasks and beats GPT-4o**; substring matching lets an
agent that dumps the whole DB pass a further 2–3.6%. That is the do-nothing
hole for any "don't act" task. Our `no_op` = 0.0 baseline exists to close it,
and the live-consumer arm's "withholding is not silence" is the same
principle stated from the world's side: the agent that correctly withholds
must leave *positive* evidence — the right owner, the boundary named, the
consumer impact stated — or it scores as absent. ABC's other checklist items
we can adopt verbatim: ground-truth includes *all* acceptable outcomes
(O.g.1); state space includes *irrelevant* state so out-of-scope writes fail
(O.g.2); state complex enough that random changes don't pass (O.g.3); ship an
oracle (T.9); inspect outliers in both directions (T.10).

**Matched pairs and factorials elsewhere.** AgentHarm ([arXiv 2410.09024](https://arxiv.org/abs/2410.09024))
pairs every harmful task with a benign twin of matched tools and complexity,
so refusal on the benign set is over-refusal and measured. OR-Bench and
XSTest find safety is achieved "at the expense of over-refusal, rarely
excelling in both" — the exact analogue is an incident agent trained on
one-sided escalation tasks that escalates everything. Anthropic's "Agentic
Misalignment" ([arXiv 2510.05179](https://arxiv.org/abs/2510.05179)) is the
methodological model: a 2×3 factorial (goal conflict × threat) with a null
control, 100 samples per cell at temperature 1, released scenarios; and the
finding that Claude misbehaved *less* when it said it believed it was in a
test, which is why our environments randomise names and dates. On asking:
NoisyToolBench ([arXiv 2409.00557](https://arxiv.org/abs/2409.00557)) — 56% of
real user queries were missing key information and agents "arbitrarily
generate the missed argument" rather than ask; reward = asked-the-right-
question minus redundant questions. QuestBench ([arXiv 2503.22674](https://arxiv.org/abs/2503.22674))
makes the missing variable exactly one, so the *minimal necessary* question
is objectively determinable. Agent-SafetyBench ([arXiv 2412.14470](https://arxiv.org/abs/2412.14470))
finds the two lowest-scoring failure modes are "fabricating parameters when
information is incomplete" and "calling tools before obtaining complete
information" — both are incident-response failures and both are things a
check can name.

**What these share, and GT009 has.** (i) Both directions are scored, never one.
(ii) The verdict is from state, not from what the agent said. (iii) The
approval source is in the world, in prose, with conflicting sources, and the
agent has to weigh it. (iv) Twin worlds flip the correct answer without
changing the mechanism. (v) They demonstrate — HiL-Bench directly — that the
gap is closable by RL on a verifiable reward, which is the proposition a lab
is buying.

## 6. Validity methodology

The practices that recur across the sources above, stated as tests:

| test | source | this library |
|---|---|---|
| Golden/reference solution passes every grader | OpenEnv RFC 008; Anthropic; Terminal-Bench CI | `oracle` and a positive control (`rebuilt_not_updated`) at max in every GT009 battery **[measured]** |
| Empty policy fails; false claim scores below empty | Terminal-Bench "dummy agent"; OpenEnv | `no_op` = 0.0, `checklist` = −1.0 **[measured]**; `report_only` earns EVIDENCE only |
| Adversarial exploit pass before shipping | Terminal-Bench §B.4; METR classifier | `redteam/scripted_subject.py` (quarantined as `blindness_violation`), `erase_the_record` **[measured]**; open: manual `sched_trigger` writes no receipt (`MATRIX_RERUN_EDITS.md §2.2`) |
| Grader mutation / falsifier: every planted damage caught by a *named* clause | STING ([arXiv 2604.01518](https://arxiv.org/abs/2604.01518)): mutate the gold patch into plausible-wrong variants, tests that accept them are too weak; drop tests that fail on behaviour-preserving variants | 30/30 base, 34/34 live-consumer **[measured]**; the second half (behaviour-preserving variants of the oracle must still pass) is `rebuilt_not_updated` and `advance_before_repair`, not yet systematised |
| Human review does not finish the job | OpenAI 2026 audit of SWE-bench Verified: after triple annotation removed 68% of tasks, **59.4% of the 138 most-failed remaining tasks still had test/spec flaws** | our equivalent is transcript reading on every 0% cell; expect a residual, keep auditing what the frontier fails |
| State checks cover what must *not* change | ABC O.g.2 | H1/H2 hygiene in GT009; other tasks' "no collateral writes" checks vary — audit in the rerun |
| Determinism across hosts and time | OpenEnv RFC 008 | two independent builds agree on digests **[measured]**; graders reproduce a world-written receipt hash byte for byte |
| 0% pass@many = broken task, not hard task | Anthropic | GT004 default 0.0 → grader bug (G1c); GT003 0.0 → D5/D7 grader bugs **[measured, new_matrix §1.2]** |
| Read the transcripts | Anthropic | S1/S2 schema finding; the four GT009 misses read as defended judgments **[measured]** |
| Pre-register the expected direction | (methodological; HiL-Bench's design section does it implicitly) | `MATRIX_RERUN_EDITS.md §1` |
| Second model family before any capability claim | Shao et al. 2025; HiL-Bench per-family signatures | not yet — `MATRIX_RERUN_EDITS.md §3` |
| Enough independent items for the claim | Miller 2024 | not close — nine tasks |

## 7. What this means for Alkera

### 7.1 What already matches the bar

- **Verdict from receipts the agent cannot edit; platform code restored from
  outside the world.** Better than the SWE-bench pattern (tests in the
  container) and equivalent to Terminal-Bench's post-hoc test copy.
- **Judge-free, deterministic, oracle-verified, falsifier-audited graders**
  with an explicit no-op / false-claim ordering. The live-consumer battery
  found and fixed an H2 defect in its own arm the same night it ran
  (`tests/VALIDITY.md`); that is the loop working.
- **Two-sided regret, measured** (7 vs 8; 8 vs 9), and a **twin pair** on
  WARRANTED (GT004 default/authorized).
- **The matrix's gate-then-project structure** is the reward shape the DRO
  argument and OpenEnv's "make the bad case a hard zero" both recommend.
- **The ablation record.** Concealment, prompt wording, stacked defects and a
  false authoritative ticket all measured flat. That record is what lets us
  say the difficulty is a decision and not a search — the exact property
  HiL-Bench had to argue for with the "no tool" control.

### 7.2 What does not yet, in order of how much it blocks the lab conversation

1. **Scale.** Nine hand-built worlds cannot be a training set: DAPO discards
   the saturated ones and Miller says K does not buy n. The generator is
   already seeded and parametric (`make_fixture_world.py --seed`), and the
   decision structure (owner / boundary / consumer, both directions charged)
   is what has to be preserved across instances. **The next artefact is a
   procedural family** — dates, merchants, amounts, which source is later,
   which role owns the runbook, how many nights — each instance carrying its
   own oracle, rivals and falsifier, with the seed as the only input.
   HiL-Bench trained on 120 tasks per domain; that is the order of magnitude.
   **[designed]**
2. **Cross-family evidence.** No valid row exists for any model but Opus. If
   FOUND drops on a second family the gate earns its keep; if FOUND holds and
   the decision axes move, the decisions are the difficulty. Either result is
   useful; neither exists.
3. **The manual-trigger hole.** A `sched_trigger` on a `publishes:` job writes
   no receipt. Three of five valid GT009 episodes triggered the closer at
   09:01. An adversarial-agent pass of the Terminal-Bench kind would find this
   first. `world/scheduler.py:trigger()` is the fix site; it affects GT008 too.
4. **The training scalar is undeclared.** The eval table is right not to
   multiply axes. A lab still needs one number per episode. Declare it in the
   task package rather than let them invent it:
   `r = 0 if FOUND cold; else 1.0·[outcome ∈ A_task] + w·(normalised sum of
   the other declared axes) + penalties(H1, H2)` with `w` small (OpenEnv's
   "small-weighted shaping"), `A_task` the frozen accepted set, and `H1`
   (false claim) applied unconditionally so the ordering `checklist < no_op`
   survives into training. Publish per-check outcomes alongside so a lab can
   build its own turn-level credit. **[designed]**
5. **Time does not move while the agent thinks.** Every lever on what the
   world *tells* the agent measured flat; the lever left is what *happens*
   while it works. The live-consumer arm fires only on grading advances
   (`DESIGN.md §10`). A mid-episode consumer is a harness change; it is also
   the single most novel thing this environment could offer, since none of
   the judgment benchmarks above have a clock.
6. **The numbers a buyer's filter reads are not in the report.** Labs filter
   on pass@1 per target model and on group spread; K = 5 gives neither. Once
   the procedural family exists, the per-task report should carry pass@1 and
   pass@k (k ≥ 64) for two model families and the advantage-collapse rate
   (fraction of groups with identical reward), so a lab can see which tasks
   are core (pass@1 ∈ [0.05, 0.7]), frontier (pass@1 ≈ 0, pass@128 > 0), and
   eval-only. That is a report change, not a task change.
7. **"Nothing else changed" is uneven across tasks.** GT009 charges
   collateral writes (H1/H2). Whether GT001–GT008 assert the *irrelevant*
   state (ABC O.g.2) — other tables, other tickets, the scheduler config —
   should be audited in the rerun and recorded in `MATRIX_RERUN_EDITS.md`.

### 7.3 Do not

- Add a judge. Every hack-resistance property above comes from code over
  state; a judge re-opens all of them and adds the determinism problem.
- Add concealment or stack defects to make FOUND move. It was tried; it did
  not move; and if it did, it would move the gate, not the decision.
- Sell the pass rate. Sell the axis separation (FOUND at 1.0, decision axis
  below it, misses that read as defended judgments) and the ablation record.
- Ship a task no adversarial pass has been run against.

## 8. Open questions the literature does not settle

- **Does a decision axis stay in the learnable band as the policy improves,
  or does it saturate like a search task once the approval-weighing rule is
  learned?** HiL-Bench's transfer result suggests the skill generalises;
  nothing yet says how fast a single world's rivals collapse. The procedural
  family is the only way to find out.
- **Is FOUND-as-gate the right training treatment for "didn't look" vs.
  "looked and said no"?** The eval table must not confuse them; a trainer may
  prefer a small negative reward for cold FOUND over a dropped sample, to keep
  the group's variance. Ask the lab; declare both options.
- **Interleaving.** Whether an agent that must act before a clock it does not
  control produces different failures from one graded after the fact is
  unmeasured anywhere we found.

### 8.1 Where the sources disagree, and the position taken here

- **Outcome-only vs dense reward.** DeepSeek-R1 and DeepSWE/DeepCoder dropped
  PRMs and partial test credit because they were hacked or collapsed;
  Lightman shows PRMs win for *reranking*; the turn-level papers show dense
  tiers help GRPO on small models. Position: dense is safe only when each
  tier is deterministically verifiable and monotone in true progress. Our
  point total is a diagnostic; the pass axis is the reward.
- **Partial credit: yes for measurement, careful for training.** Anthropic
  and TheAgentCompany build it in; DeepSWE refuses it. Both hold: report
  graded checks, hand the trainer the gate, let the lab shape.
- **Difficulty set-point: 0.5 vs a spread with a 2–3% floor.** The gradient
  papers say p ≈ 0.5; Epoch's interviewees want a spread from ~2–3% to ~70%
  including some 0% tasks that "become learnable"; Yue et al. imply
  pass@128 = 0 is not learnable by outcome RL. Position: report pass@1 and
  pass@k (k ≥ 64) per task for two model families; core tasks at
  pass@1 ∈ [0.05, 0.7]; pass@1 ≈ 0 with pass@128 > 0 is a frontier task;
  pass@128 = 0 is eval-only or broken.
- **LLM judges.** Terminal-Bench 3 nearly bans them; PaperBench, HealthBench,
  GDPval use them at validated human-agreement levels; RaR/RLCF train on
  them; the exploit papers show they are trivially fooled. Position: none in
  the action reward; if ever used, decomposed binary criteria, validated,
  stress-tested.
- **Human difficulty labels.** Terminal-Bench: r = 0.436 between predicted
  and empirical difficulty. Do not ship tiers without model-measured rates —
  which is what the pre-registration in `MATRIX_RERUN_EDITS.md §1` is for.
- **Realism vs legibility.** Epoch's buyers want production-like stacks;
  Bercovich wants two-paragraph tasks; Anthropic warns path-grading is
  brittle because agents find valid approaches designers did not anticipate
  (Opus 4.5 "failed" a τ²-bench task via a policy loophole that was better
  for the user). Position: our world is on the realistic end, so the whole
  burden is on end-state grading, and a defended alternative that is
  genuinely valid gets *adjudicated into the accepted-outcome set*, never
  handled by loosening a check.

## 9. Sources

- OpenAI, *Reinforcement fine-tuning use cases* — https://developers.openai.com/api/docs/guides/rft-use-cases
- OpenEnv, *RFC 008: Environment auto validation* — https://github.com/huggingface/OpenEnv/issues/778 ; *Reward design* — https://huggingface.co/docs/openenv/guides/rewards
- *The Environment Layer: Building Infrastructure for Agentic AI Training* (2026) — https://rxiv.org/pdf/2603.0138v1.pdf
- Anthropic, *Demystifying evals for AI agents* (Jan 2026) — https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
- Yu et al., *DAPO: An Open-Source LLM RL System at Scale* (2025) — https://arxiv.org/abs/2503.14476
- Miller, *Adding Error Bars to Evals* (Anthropic, 2024) — https://arxiv.org/abs/2411.00640
- METR, *Recent Frontier Models Are Reward Hacking* (Jun 2025) — https://metr.org/blog/2025-06-05-recent-reward-hacking/
- Baker et al., *Monitoring Reasoning Models for Misbehavior and the Risks of Promoting Obfuscation* (OpenAI, 2025) — https://arxiv.org/abs/2503.11926
- Anthropic, *Natural emergent misalignment from reward hacking in production RL* (Nov 2025) — https://arxiv.org/abs/2511.18397
- Terminal-Bench 2.0 (2026) — https://arxiv.org/html/2601.11868v1
- Gunjal et al., *Rubrics as Rewards* (Scale AI, 2025) — https://arxiv.org/abs/2507.17746
- Lightman et al., *Let's Verify Step by Step* (2023) — https://arxiv.org/abs/2305.20050
- Shao et al., *Spurious Rewards: Rethinking Training Signals in RLVR* (2025) — https://arxiv.org/abs/2506.10947
- *Demystifying Reinforcement Learning Post-Training of Language Models* (2026) — https://arxiv.org/html/2608.24949v1
- *From Reasoning to Agentic: Credit Assignment in RL for LLMs* (2026) — https://arxiv.org/html/2604.09459v1
- *Can rubrics and dense rewards work together without hacking?* (DRO note) — https://inquiringlines.com/notes/separating-optimization-from-feasibility-dense-token-level-rewards-plus-rubric-h/
- Scale AI, *HiL-Bench: Do Agents Know When to Ask for Help?* (Apr 2026) — https://arxiv.org/abs/2604.09408 ; https://labs.scale.com/papers/hil
- *Escalation Bench* — https://github.com/nbdesai1992/escalation-bench
- *BLINDSPOT: Safety and Refusal Calibration in Long-Horizon Tool-Using Agents* (Sep 2026) — https://arxiv.org/pdf/2609.16305v1
- *pi-bench* — https://github.com/Jyoti-Ranjan-Das845/pi-bench
- Barres et al., *τ²-Bench* (2025) — https://arxiv.org/abs/2506.07982 ; *τ²-bench-verified* — https://github.com/amazon-agi/tau2-bench-verified
- SWE-bench Verified — https://www.swebench.com/ ; OpenAI, *Why SWE-bench Verified no longer measures frontier coding capabilities* (2026) — https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/ ; issue #465 *Repo State Loopholes* — https://github.com/SWE-bench/SWE-bench/issues/465
- Denain & Barber, *An FAQ on Reinforcement Learning Environments* (Epoch AI, Jan 2026) — https://epochai.substack.com/p/an-faq-on-reinforcement-learning
- DeepSeek-AI, *DeepSeek-R1* (2025) — https://arxiv.org/abs/2501.12948
- Bercovich et al., *Terminal Wrench: 331 Reward-Hackable Environments* (2026) — https://arxiv.org/abs/2604.17596 ; Bercovich, *What Makes a Good Terminal-Agent Benchmark Task* (2026) — https://arxiv.org/abs/2604.28093
- Agentica/Together, *DeepSWE* — https://www.together.ai/blog/deepswe ; *DeepCoder* — https://www.together.ai/blog/deepcoder
- *Advantage Collapse in GRPO* (2026) — https://arxiv.org/abs/2605.21125 ; *Prompt Replay* (2026) — https://arxiv.org/abs/2603.21177 ; *Max Out GRPO Signal* (2026) — https://arxiv.org/abs/2607.07674 ; *Hard Examples Are All You Need* (2025) — https://arxiv.org/abs/2508.14094
- Yue et al., *Does RL Really Incentivize Reasoning Capacity Beyond the Base Model?* (2025) — https://arxiv.org/abs/2504.13837
- Segato et al., *Quantifying Infrastructure Noise in Agentic Coding Evals* (Anthropic, Feb 2026) — https://www.anthropic.com/engineering/infrastructure-noise
- Zeng et al., *Fine-Grained Reward Structure for Multi-Turn Agents (MT-GRPO)* (2025) — https://arxiv.org/abs/2505.11821 ; *Signal Reshaping for GRPO in Weak-Feedback Code Repair* (2026) — https://arxiv.org/abs/2605.07276
- Xu et al., *TheAgentCompany* (2024) — https://arxiv.org/abs/2412.14161
- Arora et al., *HealthBench* (2025) — https://arxiv.org/abs/2505.08775 ; *Decomposing Physician Disagreement in HealthBench* (2026) — https://arxiv.org/abs/2602.22758
- Starace et al., *PaperBench* (2025) — https://arxiv.org/abs/2504.01848 ; Patwardhan et al., *GDPval* (2025) — https://arxiv.org/abs/2510.04374
- Zhao et al., *One Token to Fool LLM-as-a-Judge* (2025) — https://arxiv.org/abs/2507.08794 ; *Detecting Proxy Gaming via Evaluator Stress Tests* (2025) — https://arxiv.org/abs/2507.05619 ; *Adversarial Attacks on LLM-as-a-Judge Systems* (2025) — https://arxiv.org/abs/2504.18333
- Viswanathan et al., *Checklists Are Better Than Reward Models (RLCF)* (2025) — https://arxiv.org/abs/2507.18624
- Zhu et al., *Establishing Best Practices for Building Rigorous Agentic Benchmarks (ABC)* (NeurIPS 2025) — https://arxiv.org/abs/2507.02825
- Andriushchenko et al., *AgentHarm* (ICLR 2025) — https://arxiv.org/abs/2410.09024 ; Cui et al., *OR-Bench* (2024) — https://arxiv.org/abs/2405.20947 ; Röttger et al., *XSTest* (NAACL 2024) — https://aclanthology.org/2024.naacl-long.301/
- Lynch et al., *Agentic Misalignment* (Anthropic, 2025) — https://arxiv.org/abs/2510.05179
- Wang et al., *Learning to Ask (NoisyToolBench)* (EMNLP 2025) — https://arxiv.org/abs/2409.00557 ; Li et al., *QuestBench* (DeepMind, 2025) — https://arxiv.org/abs/2503.22674 ; Zhang & Choi, *Clarify When Necessary* (NAACL 2025) — https://aclanthology.org/2025.naacl-findings.306.pdf
- Zhang et al., *Agent-SafetyBench* (2024) — https://arxiv.org/abs/2412.14470
- *STING: Mutation-Guided Diagnosis and Augmentation of Benchmark Tests* (2026) — https://arxiv.org/abs/2604.01518
- Reuel et al., *BetterBench* (2024) — https://arxiv.org/abs/2411.12990 ; Bean et al., *Measuring What Matters: Construct Validity in LLM Benchmarks* (NeurIPS 2025) — https://arxiv.org/abs/2511.04703
- METR, *HCAST* — https://metr.org/hcast.pdf ; *Task Standard* — https://github.com/METR/public-tasks
- Repository: `new_matrix.md`, `tasks/MATRIX_RERUN_EDITS.md`, `tasks/GT009-settlement-cutoff/DESIGN.md`, `tasks/GT009-settlement-cutoff/tests/VALIDITY.md`, `agent.md §8.10–8.11`, `STATUS.md`.
