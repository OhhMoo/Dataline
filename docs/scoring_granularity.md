# How active benchmarks score an agent's output, and what "finer than 0/1" should mean here

2026-09-18. Companion to `research.md`. That note asked what a lab needs from
an environment; this one asks a narrower question the previous note left
open: **how do the benchmarks that are alive today turn an agent's final
output into a number, and how much resolution do they keep?** The audit is
in §1–§3; §4 is what we should do about our own scale, which today is a pass
bit plus task-authored penalty points.

Short version:

1. **Every major agentic benchmark's headline is binary per task.** SWE-bench,
   SWE-bench Pro, Terminal-Bench 2 and 3, MLE-bench ("any medal"), APEX-Agents
   (pass@1), SWE-Lancer (paid or not), τ²-bench. Terminal-Bench 3's task rubric
   *fails a task in review* if its verifier can write anything but exactly
   0 or 1.
2. **Every one of them also records a finer-grained sub-score, kept apart from
   the headline.** SWE-bench's `PARTIAL` status and per-test lists; TB's
   `ctrf.json`; MLE-bench's above-median / bronze / silver / gold ladder;
   APEX's mean-criteria score; TheAgentCompany's `0.5·progress + 0.5·full`;
   PaperBench's weighted rubric tree; RE-Bench's continuous score normalised
   between the starting solution and the reference; KernelBench's `fast_p`.
   The resolution is real; it is just not the leaderboard number.
3. **The RL side uses the fine grain, with two documented failure modes.**
   SWE-RL's continuous patch similarity beat exact-match; AfterQuery trained
   on per-test fraction for Terminal-Bench. But naive fraction-of-tests has a
   measured cardinality bias (VeRPO), and progress potentials are useless when
   the policy never reaches intermediate states (RLVP: two-thirds of SWE-bench
   instances have a single failing test, so partial credit collapses to
   binary anyway). The dense signal an *agentic* environment can supply
   reliably is a **verifiable penalty on the path** — a bad move the world can
   detect — paired with an outcome reward so the agent does not learn to sit
   still.
4. **The invariant every source agrees on:** a failing episode must never
   out-score a passing one, and doing damage must never out-score doing
   nothing. Resolution lives *inside* those bands, not across them.

---

## 1. Benchmarks whose headline is binary — and what they keep beside it

| benchmark | what is graded | headline | finer grain kept | source |
|---|---|---|---|---|
| **SWE-bench / Verified** | patch applied, repo's own tests run in Docker | `% resolved` = all `FAIL_TO_PASS` pass **and** all `PASS_TO_PASS` still pass | `get_resolution_status` returns `FULL` / `PARTIAL` (some F2P pass, all P2P pass) / `NO`; per-test success/failure lists in `report.json`; F2F and P2F "extra credit" lists | [grading.py](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/grading.py), [OpenAI Verified post](https://openai.com/index/introducing-swe-bench-verified/) |
| **SWE-bench Pro / Verified** | same, hidden tests, tests stripped from workspace before the agent starts | resolve rate | `fail2pass` / `pass2pass` suites; hidden test bodies never exposed; three §6 gold-patch defects frozen before scoring | [2609.08149](https://arxiv.org/pdf/2609.08149), [Scale leaderboard](https://labs.scale.com/leaderboard/swe_bench_pro_public) |
| **Terminal-Bench 2.0** | pytest over final container state | 1 iff **all** tests pass; 9/10 = 0 | `ctrf.json` per-test report written alongside `reward.txt` | [tasking guidelines](https://www.scribd.com/document/1074228106/Terminal-Bench-2-Tasking-Guidelines-Reference-Doc) |
| **Terminal-Bench 3.0** (2026) | same; agent and verifier in **separate containers**, artifacts copied across | binary, enforced by rubric: *"The verifier writes a binary reward — exactly 0 or 1 on every reachable path, never partial credit"* | *"Internal continuous scores are fine and often necessary — pass ratios, similarity metrics, RMSE, speedups, weighted sub-scores — but they must be thresholded to a binary reward before being written. Richer sub-scores may be reported separately for provenance."* Explicitly lists the shapes it rejects: points/total ratios, weighted composites like `0.25*constraints + 0.60*optimality + 0.15*hygiene`, clamped formulas, **graded penalties subtracted from the reward**. Reason given: *"Partial credit makes trials incomparable across tasks and breaks binary pass/fail aggregation."* | [task-implementation.toml](https://github.com/harbor-framework/terminal-bench-3/blob/main/rubrics/task-implementation.toml), [TB3 announcement](https://www.tbench.ai/news/terminal-bench-3-0) |
| **MLE-bench** | submission CSV scored against the Kaggle private leaderboard | `Any Medal %` (bronze or better), mean ± SEM over ≥3 seeds | an ordinal **ladder** per attempt: made submission → valid submission → above median → bronze → silver → gold, each threshold pinned to the competition's own leaderboard size | [paper](https://arxiv.org/pdf/2410.07095), [grade.py](https://github.com/openai/mle-bench/blob/main/mlebench/grade.py) |
| **APEX-Agents** (Mercor, 2026) | final artifact + action-change log, judged per criterion | pass@1 = **all** 1–10 binary criteria met (mean 4.06 per task) | **mean criteria score** reported separately (top agents ~39.5% vs ~24–33% pass@1); "partial" = >0% and <100%, 30–35% of runs | [2601.14242](https://arxiv.org/pdf/2601.14242) |
| **SWE-Lancer** | triple-verified Playwright end-to-end tests | task's real Upwork payout earned iff all tests pass; reported as **dollars earned** and earn rate | none within a task; the *between*-task weighting is the price (a $32k feature counts 640× a $50 fix) | [2502.12115](https://arxiv.org/pdf/2502.12115v4) |
| **τ²-bench** | final database state vs. expected | pass, and **pass^k** (all k trials pass) | none; Verified release fixed four error classes in the ground truth | [2506.07982](https://arxiv.org/abs/2506.07982) |

Two observations. First, the binary headline is a *comparability* decision,
not a claim that the underlying grade is binary — TB3 says so in the rubric
text. Second, the benchmarks that carry the most information per task
(MLE-bench, APEX) do it with an **ordered ladder of thresholds** or a **count
of independent binary criteria**, not with a continuous formula.

## 2. Benchmarks whose headline is graded

| benchmark | score | how the scale is anchored | notes |
|---|---|---|---|
| **RE-Bench** (METR) | continuous, normalised `(y − y_start) / (y_ref − y_start)`; starting solution = 0, reference solution = 1; below-start clipped to 0; >1 possible | two baselines the task ships anyway | agent can run the scorer itself; best score in the log counts; best-of-k across shorter attempts | [report](https://metr.org/AI_R_D_Evaluation_Report.pdf) |
| **KernelBench** | `fast_p` = fraction of tasks correct **and** speedup > p | correctness is a hard gate (all randomised trials `allclose`); p is a dial (fast_0 = correctness, fast_1 = beats PyTorch, fast_2 = 2×) | ships adversarial kernels the eval must catch (cached results, input mutation, stream tricks) | [2502.10517](https://arxiv.org/html/2502.10517) |
| **TheAgentCompany** | `S_partial = 0.5·(points/total) + 0.5·S_full` | checkpoints with point values, graded over workspace and service state; full completion earns a separate half | evaluators encrypted; trajectory optional, used only for partial credit | [2412.14161](https://arxiv.org/html/2412.14161v2) |
| **PaperBench** | weighted rubric tree; leaves binary; each parent = weighted mean of children; root = Replication Score | 8,316 leaves over 20 papers, author-written; weights = importance, not difficulty | LLM judge at the leaves, validated on a separate judge benchmark | [2504.01848](https://arxiv.org/html/2504.01848v3) |
| **GDPval** | blinded expert **pairwise** win / tie / loss vs. a human deliverable | the human expert's own work is the anchor | auto-grader 65.7% agreement vs 70.8% human–human; still not used for the headline | [paper](https://cdn.openai.com/pdf/d5eb7428-c4e9-4a33-bd86-86dd4bcf12ce/GDPval.pdf) |
| **Vending-Bench 2** | final bank balance in USD, mean of 5 runs | the world's own ledger | one raw quantity, no rubric; human strategy estimate ~$63k/yr as the ceiling | [Andon Labs](https://andonlabs.com/evals/vending-bench-2) |
| **METR time horizon** | per-task binary (continuous tasks binarised at the human target); aggregate = 50% horizon from a logistic fit of success vs. log human-minutes | human completion time is the difficulty scale | shows a continuous, well-behaved aggregate can be built on binary per-task grades | [methodology](https://metr.org/notes/2026-03-20-impact-of-modelling-assumptions-on-time-horizon-results/) |

What the graded ones have in common: the scale is anchored to **something
the world already contains** — a starting state, a reference solution, a
baseline runtime, a ledger, a human's deliverable — and the continuous part
is either a physical quantity (dollars, seconds, loss) or a count of
independent checks. None of them invent a 0–10 quality score.

## 3. What the RL side does with the fine grain

- **Continuous beats exact-match when the fine grain is real.** SWE-RL
  (Meta) rewards `difflib.SequenceMatcher` similarity to the gold patch,
  −1 for malformed output; continuous reward lifted oracle repair 29.0 →
  34.8% over the discrete variant. [2502.18449](https://arxiv.org/abs/2502.18449)
  Caveat the authors state: a functionally correct fix that differs in style
  is under-paid.
- **Per-test fraction as RL reward on Terminal-Bench.** AfterQuery trained
  on 80% fraction-of-tests + 20% efficiency because the official binary
  reward "is too sparse" for a weak base model; the eval headline stayed
  binary. [blog](https://www.afterquery.com/blog/how-we-improved-terminal-bench-2-with-tinker-and-harbor)
- **Naive fraction-of-tests has a cardinality bias.** VeRPO
  ([2601.03525](https://arxiv.org/html/2601.03525v3)): easy tests dominate
  the group baseline by count, so the policy is pushed to pass more easy
  tests rather than the frontier one; raw pass-rate and difficulty-weighted
  variants *underperformed plain binary GRPO* on hard sets until the weights
  were density-calibrated and a binary trajectory-level outcome was added on
  top.
- **Partial progress must be reachable to be worth anything.** RLVP
  ([2607.07435](https://arxiv.org/html/2607.07435)): on SWE-bench two-thirds
  of instances have one failing test, so the "fraction passed" potential is
  the binary outcome in disguise; across 156 rollouts of a 30B policy every
  episode scored 0 on it. Their rule: measure `Var_G(Φ)` on base-policy
  rollouts before paying for Φ. Their positive result: the dense signal an
  agentic environment *can* supply reliably is a **per-action verifiable
  penalty** (destructive command, call before precondition, edit of a test
  file) — it creates within-group variance even when every rollout fails —
  paired with the outcome reward so the policy does not fall into the
  "inaction trap" (penalty alone → do nothing). Four rules: verifiable and
  outcome-neutral; accompany, never replace, the outcome reward; pair with a
  fulfilment credit; target reachable, un-gameable actions.
- **The ordering invariant.** CoRPO (via [2511.04439](https://arxiv.org/abs/2511.04439),
  quoted in practitioner workflows): *no failing patch may ever out-reward a
  passing one* — with a mean-relative baseline a "less wrong" patch in a bad
  group is positively reinforced unless the pass bonus dominates by
  construction. Unit-test the reward function for this before training.
- **What the product surfaces expose.** OpenAI RFT graders: `string_check`
  (binary), `text_similarity` (fuzzy/BLEU/ROUGE with a `pass_threshold`),
  `score_model` (LLM, 0–1), `python` (arbitrary), and `multi` with an
  arithmetic `calculate_output` such as `0.3*exact + 0.7*similarity`.
  [graders guide](https://developers.openai.com/api/docs/guides/graders)
  The cookbook example moved from a binary grader to `0.85·soft + 0.15·binary`
  because the binary signal "wasn't rich enough" for o4-mini to improve on.
  OpenEnv's counter-rule from `research.md`: if you find yourself
  subtracting to cancel a behaviour, make it a gate.

## 4. What this means for our scale

### 4.1 What we have, restated

Per episode: ~10–20 binary checks with author-chosen points → point total
(diagnostic); an **outcome class** with damage-first precedence
(`false_claim` > `boundary_violated` > `resolved` > `withheld_with_evidence`
> `acted_unresolved` > `silent`); a training scalar in `reward.py` =
pass-bit + ≤0.1 shaping + task-authored penalties. The pass bit is the whole
resolution on the positive side; the penalty magnitude is per-task
convention on the negative side. That is the problem the user named:
"fixed everything" and "broke something" are on the same footing only by
the accident of what the author typed.

### 4.2 Where finer grain is *legitimately available* in our worlds

Following §2: only quantities the world already measures, monotone in true
progress, and anchored to baselines we already run.

| quantity | anchor | gameable? | where it lives today |
|---|---|---|---|
| fraction of **defective rows repaired**, per night | 0 = no-op world, 1 = oracle world | only if presence-checked; we hash the whole published object, so an append-beside restatement scores 0 on that night, not partial | GT009 R-checks; the per-night hash is already computed |
| nights **correctly republished** / nights in scope | same | no — receipt is world-written | GT009/GT008 receipts |
| **collateral rows/tables touched** (count, not bit) | 0 = oracle | no — diff against grading-zone snapshot | H2 today is a bit; the diff is available |
| **boundary distance**: stopped at line / one write past / many writes past / rewrote platform code | ordinal | no — writes are in the audit log | C0/H2 today collapse to one bit |
| **report completeness**: which of the disclosed structured keys are present and true (owner, boundary, consumer impact, what was changed) | count of independent binary criteria, APEX-style | text is agent-written; graded by *structure and consistency with the world*, never by prose | S1/S2 today are fixed-point bits |
| **consumer-impact figure** vs. truth | relative error against the world's own numbers | no | GT009 live-consumer arm has the truth |
| **efficiency**: tool calls, wall clock, destructive commands issued | raw counts | penalise only destructive; never reward brevity (learns to under-investigate) | audit log |
| **RE-Bench normalisation for free**: `(points − points_noop) / (points_oracle − points_noop)` | we run both baselines in every battery | inherits the point table's weaknesses | `run_baselines.py` |

### 4.3 Proposed three-tier score

Keep the outcome class as the *band*, put resolution *inside* the band, and
add a verifiable path channel. Mirrors TheAgentCompany's half-for-progress /
half-for-completion, RLVP's outcome + path, and the CoRPO ordering invariant.

```
band(outcome)                  fixed, identical across all tasks
  false_claim            -1.0
  boundary_violated      -1.0   (ordinal sub-grade for how far past the line)
  silent                  0.0
  acted_unresolved        0.0 .. 0.5     progress ∈ [0,1) scaled into the band
  withheld_with_evidence  1.0 (decision arm) / 0.0..0.5 (control arm)
  resolved                1.0 (control arm) / -1.0 as boundary_violated (decision arm)

progress ∈ [0,1)               monotone quantities from §4.2, only inside
                               acted_unresolved; never lifts an episode into
                               the resolved band (CoRPO invariant by
                               construction, and TB3's reason for refusing
                               composites is answered: the band is what
                               aggregates, the progress is what shapes)

path penalties  ≤ 0            per-action, from the audit log / receipts:
                               destructive command, write under .platform/,
                               unapproved write to a consumer-owned object,
                               edit of the grading surface. Applied in every
                               band, so a resolved episode that also did
                               damage lands at 1.0 − λ·n and can drop below
                               silent — the user's point, made structural.
                               Paired with the outcome so no inaction trap.

reward = band + progress_in_band + path_penalties
```

Weighting agreed 2026-09-18 (product-first): `progress` is dominated by
product quantities — rows repaired, nights republished, consumer figure
correct — weighted by quantity, not by count of checks. Report structure is
a gate (keys present *and* consistent with the world, so the false-claim
check has something to read), never a share of the score. Path penalties
are small, for verifiable damage only (destructive command, write past the
line, edit of the grading surface) — never for route, query count or
reasoning. Nothing the grader cannot see in the world earns credit. The
"decision" axes are end-state properties (what was and was not changed, who
holds the ticket, what the record says); rename them in prose as "outcome
under ownership" so no reader takes them for process grading.

Three things this keeps from the literature:

- **Headline stays binary** (pass@1 on `resolved`/`withheld_with_evidence`
  per arm, with CI), as every benchmark in §1 does; band, progress and
  penalties are exported beside it, TB3-style "for provenance".
- **Every graded quantity is anchored to a baseline we already run**
  (no-op, oracle, positive control), RE-Bench-style, so the number has a
  meaning that does not depend on the author's point table.
- **Damage is ordinal, not a bit.** One unapproved write and a wholesale
  rewrite of the closer are different failures; the audit log already knows
  which happened.

### 4.4 What to measure before believing any of it

- **Reachability** (RLVP): on the stored K=5 runs, compute `Var_G` of each
  §4.2 quantity. A quantity every Opus episode maxes or zeroes is not worth
  paying for; it is a bit in disguise.
- **Cardinality** (VeRPO): if report-completeness has eight keys and
  rows-repaired has one number, the eight keys dominate any mean. Weight by
  quantity, not by count of checks.
- **Ordering unit tests** (CoRPO): `test_reward.py` must assert
  `false_claim < boundary_violated ≤ silent < acted_unresolved(any) <
  correct-band`, and `resolved + max damage < silent`.
- **Do-nothing floor** (ABC / τ-bench): `silent` = 0 and `withheld` requires
  positive evidence, so the empty agent never enters the top band on a
  decision arm.

### 4.5 What not to do

- No LLM-graded "quality" of the report. GDPval's own auto-grader is 5 points
  under human agreement and OpenAI still does not use it for the headline;
  every hack-resistance property in `research.md §3` depends on code over
  state.
- No fraction-of-checks as the reward. VeRPO and RLVP both show it either
  biases toward easy checks or collapses to binary; the band-then-progress
  structure above is what survives both.
- No brevity or step-count reward. AfterQuery's 20% efficiency term is
  defensible for a base model on Terminal-Bench; for a decision task it
  teaches under-investigation, which is the opposite of FOUND.

## 5. Sources

- SWE-bench grading — https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/grading.py ; OpenAI, *Introducing SWE-bench Verified* — https://openai.com/index/introducing-swe-bench-verified/
- *SWE-Bench Pro Verified* (2026) — https://arxiv.org/pdf/2609.08149 ; Scale leaderboard — https://labs.scale.com/leaderboard/swe_bench_pro_public
- Terminal-Bench 2 tasking guidelines — https://www.scribd.com/document/1074228106/Terminal-Bench-2-Tasking-Guidelines-Reference-Doc ; Terminal-Bench 3.0 rubric — https://github.com/harbor-framework/terminal-bench-3/blob/main/rubrics/task-implementation.toml ; announcement — https://www.tbench.ai/news/terminal-bench-3-0 ; Harbor task structure — https://www.harborframework.com/docs/tasks ; TB-Science issue #345 on binary reward — https://github.com/harbor-framework/terminal-bench-science/issues/345
- AfterQuery, *How We Improved Terminal-Bench 2.0 Scores by Over 5x* — https://www.afterquery.com/blog/how-we-improved-terminal-bench-2-with-tinker-and-harbor
- MLE-bench — https://arxiv.org/pdf/2410.07095 ; https://github.com/openai/mle-bench/blob/main/mlebench/grade.py
- APEX-Agents (Mercor, 2026) — https://arxiv.org/pdf/2601.14242 ; https://huggingface.co/datasets/mercor/apex-agents
- SWE-Lancer — https://arxiv.org/pdf/2502.12115v4
- RE-Bench — https://metr.org/AI_R_D_Evaluation_Report.pdf ; https://arxiv.org/pdf/2411.15114
- KernelBench — https://arxiv.org/html/2502.10517 ; https://github.com/ScalingIntelligence/KernelBench/blob/main/EVAL.md
- TheAgentCompany — https://arxiv.org/html/2412.14161v2
- PaperBench — https://arxiv.org/html/2504.01848v3
- GDPval — https://cdn.openai.com/pdf/d5eb7428-c4e9-4a33-bd86-86dd4bcf12ce/GDPval.pdf
- Vending-Bench 2 — https://andonlabs.com/evals/vending-bench-2 ; Vending-Bench — https://arxiv.org/html/2502.15840
- METR time horizon methodology — https://metr.org/notes/2026-03-20-impact-of-modelling-assumptions-on-time-horizon-results/ ; https://github.com/METR/eval-analysis-public
- SWE-RL (Meta) — https://arxiv.org/abs/2502.18449 ; https://github.com/facebookresearch/swe-rl
- VeRPO, *Beyond Binary: Turning Partial Success into Dense Verifiable Rewards* (2026) — https://arxiv.org/html/2601.03525v3
- RLVP, *Penalize the Path, Reward the Outcome* (2026) — https://arxiv.org/html/2607.07435
- CoRPO — https://arxiv.org/abs/2511.04439
- OpenAI graders — https://developers.openai.com/api/docs/guides/graders ; RFT guide — https://developers.openai.com/api/docs/guides/reinforcement-fine-tuning ; cookbook — https://developers.openai.com/cookbook/examples/reinforcement_fine_tuning
