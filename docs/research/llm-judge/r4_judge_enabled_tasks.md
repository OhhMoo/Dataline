# R4 — Judge-enabled task design: how benchmarks grade hard, open-ended professional work, and what that unlocks that code-grading cannot

Literature survey, 2026-09-22. Scope: 2025–2026 benchmarks and meta-evaluation papers. All numbers are quoted from the primary source unless flagged **[secondary]** (taken from a non-primary page) or **[unverified]** (could not be confirmed against a primary source in this pass).

Context for reading: our environment (DuckDB + dbt + scheduler + Jira/Slack fakes + audit ledger; bash-only agent; deterministic grading over final world state and a structured report schema) is currently at ~100% on control arms for frontier models (Opus 5, K=5). The remaining failures are judgment calls. The question is what a validated judge or rubric would let us grade that code cannot, and how the best benchmarks do that without becoming hackable or unreproducible.

---

## 0. One-paragraph answer

The benchmarks that grade professional work reliably do **not** ask a judge "how good is this?". They decompose the deliverable into many **short, self-contained, binary, expert-written criteria** (APEX-Agents: 1–10 per task; PaperBench: thousands of leaf nodes; HealthBench: ~48k criteria; XpertBench: 15–40 weighted checkpoints) and use the LLM as a **reader that checks each criterion against an artifact it cannot be talked into** — a filesystem diff (APEX/Archipelago), a rubric tree over submitted code (PaperBench), a final response (HealthBench), never the agent's own narrative. With that design, judge-vs-expert agreement runs 0.83 F1 (PaperBench), 0.709 macro-F1 vs physicians at 0.569–0.730 (HealthBench), 98.5% accuracy on 747 expert labels (APEX-Agents), κw = 0.90 (ORCA-bench). Where the judge is instead given holistic Likert scales, LLM-authored rubrics, or the trajectory, agreement collapses (LH-Bench κ 0.46 for LLM-written rubrics vs 0.60 for domain-written; "Gaming the Judge" +20–30 pt false-positive rate from rewritten reasoning traces; DataClawEval judges inflating process scores from 22.2 to 75–76 with per-case SD > 6.35). The data-engineering benchmarks specifically (DABstep, Spider 2.0, BIRD-Interact, ELT-Bench, DataGovBench, DataClawEval, AgenticDataBench, DAB) are almost entirely code-graded on materialized tables, and the one that tried an LLM judge (DataClawEval) published the failure. The judge-enabled tasks that are missing from that family — and that our environment is positioned to add — are: design choices with many correct answers, written deliverables (postmortems, RFCs, stakeholder explanations), constraint-dependent trade-offs, requirement discovery via asking, and process quality. Each has a benchmark that shows how to grade it and what the human-agreement ceiling is.

---

## 1. Benchmarks that use rubric / judge grading of professional work

### 1.1 APEX-Agents (Mercor, Jan 2026) — the cleanest template

*APEX-Agents*, Mercor, arXiv:2601.14242 (https://arxiv.org/abs/2601.14242), 2026. Leaderboard: https://www.mercor.com/apex/apex-agents-leaderboard/.

- **Tasks**: 480 tasks across 33 "worlds" in investment banking, management consulting and corporate law. Each world has nine applications with 63 tools (Calendar, Chat, Code Execution, Documents, File system, Mail, PDFs, Spreadsheets, Presentations). Tasks are estimated at 11–22 hours of professional work.
- **How criteria are written**: experts wrote rubrics of "criteria: self-contained, short, descriptive statements, which can be graded as true or false against an agent's output." Each criterion has a "grading target" naming the required output type. 1–10 criteria per task, mean 4.06.
- **Validation**: 20% of tasks (n = 96) were independently executed by experts who had not authored them; "in 10% of tasks, we identified a minor problem with the prompt, rubric, or metadata that needed to be fixed." After finalization, the judge was run over all 480 gold outputs and "graded all of the associated criteria correctly."
- **Judge**: Gemini 3 Flash, thinking = low. Critically, "the judge takes in the task prompt, the agent output, a log of the changes induced by the agent's actions, and the criterion — but not the agent trajectory." Each criterion is graded in an independent call, returning Met / Not met plus a short explanation.
- **Judge-vs-human**: on 747 expert-labelled criteria, "accuracy is 98.5%, precision is 96.7%, recall is 98.1%, and positive-class F1 is 97.4%" (FNR 1.9%, FPR 1.3%). Self-preference check: on 84 Gemini 3 Flash criteria, 11 false positives → FPR 1.2%, i.e. no measurable self-preference.
- **Metrics**: headline is **pass@1 = all criteria met**; secondary is **Mean Score = mean fraction of criteria met**.
- **Results at publication (Jan 2026)**: pass@1 — Gemini 3 Flash 24.0%, GPT-5.2 23.0%, Claude Opus 4.5 / Gemini 3 Pro 18.4%. Mean Score — 39.5% / 38.7% / 34.8%.
- **Results now (leaderboard, Sep 2026)**: Opus 5.5 Max 73.5% ±4.9, Fable 5.1 Max 68.6% ±4.9, Gemini 3.7 Flash High 67.8% ±5.1, Opus 5 Max 65.8% ±5.1. (Leaderboard lists 31 worlds / 240 tasks, presumably the public subset.) So a judged benchmark went from 24% to 73% pass@1 in eight months — see §4.
- The verifier released with APEX-Agents, **Archipelago**, "captures a filesystem diff between initial and final environment states, extracts visual content via the Reducto document extraction service, and grades each criterion in an independent LLM call over the diff and extracted visuals without interacting with the deliverable directly" (per Dücker et al., §1.14 below). This is the "judge reads a state diff, not a transcript" pattern.

### 1.2 GDPval (OpenAI, Sep/Oct 2025) — expert pairwise, with a calibrated auto-grader

*GDPval: Evaluating AI Model Performance on Real-World Economically Valuable Tasks*, OpenAI, arXiv:2510.04374 (https://arxiv.org/abs/2510.04374), 2025. Blog: https://openai.com/index/gdpval/.

- **Tasks**: 1,320 tasks across 44 occupations; experts averaged 14 years' experience; "each task received an average of five human reviews (with a minimum of three reviews)." 67.7% of tasks require reference files; deliverables include spreadsheets, decks, CAD, diagrams, media.
- **Human grading**: blinded pairwise ranking of unlabelled deliverables by occupation experts; "grading each comparison for the gold subset took over an hour." **Human inter-rater agreement: 70.8%.**
- **Automated grader**: GPT-5-high pairwise, output in {0, 0.5, 1}. **"Average human–automated grader agreement was 65.7%"** — "5% below" human-human. Caveat stated by OpenAI: the grader "shows lower correlation with human expert graders when assessing outputs from capable OpenAI models" (self-preference). 12/220 tasks were marked ungradable by the auto-grader (internet access, non-Python, fonts, speech-to-text).
- **Results**: Claude Opus 4.1 47.6% win-or-tie vs human expert deliverables; GPT-5 ~39.0%; o3 ~35.2%.
- **GDPval-AA** (Artificial Analysis, https://artificialanalysis.ai/evaluations/gdpval-aa): a third-party re-run using an LLM judge for blind pairwise matchups aggregated to Elo (anchored at DeepSeek V4.1 Flash = 1600). Top: Claude Opus 5.5 Max 1846, Fable 5.1 Max 1735. The page does **not** name the judge or report judge validation — a useful contrast: the same task set, one version with published agreement, one without.

### 1.3 PaperBench (OpenAI, Apr 2025) — hierarchical rubric tree, binary leaves, JudgeEval

*PaperBench: Evaluating AI's Ability to Replicate AI Research*, OpenAI, arXiv:2504.01848 (https://arxiv.org/abs/2504.01848), 2025.

- **Rubric**: per-paper hierarchical tree, 8,316 leaf nodes across 20 papers; "leaf nodes specifying a single clear criterion to pass or fail"; parents are weighted averages of children. Three leaf types: **Code Development**, **Execution**, **Result Match**. Rubrics were co-written with the original paper authors over "multiple weeks per paper," "many tens of hours of labor," with "multiple rounds of feedback to address ambiguities."
- **Judge (SimpleJudge)**: grades each leaf independently, given the paper, addenda, sibling/ancestor requirements, the leaf requirement, and relevant submission files.
- **JudgeEval**: human-graded gold labels on partial replications of five papers, scored as binary classification. **o3-mini F1 0.83 at $66/paper** (chosen); o1 0.84 at $830; o1-mini 0.78; GPT-4o 0.73. By leaf type: **0.72 Code Development, 0.82 Execution, 0.94 Result Match** — i.e. the judge is nearly perfect where the criterion is closest to a verifiable fact, and weakest on "is this code a correct implementation".
- **Reproducibility**: official judge pinned to o3-mini-high + SimpleJudge; the authors explicitly note the judge "lacks determinism due to non-deterministic model calls" and recommend future work on "adversarial submissions."
- **Results**: Claude 3.5 Sonnet 21.0%; o1 IterativeAgent 24.4%; human ML PhDs 41.4% on a 3-paper subset after 48h.

### 1.4 HealthBench (OpenAI, May 2025) — physician-written weighted criteria

*HealthBench*, OpenAI, arXiv:2505.08775 (https://arxiv.org/abs/2505.08775), 2025. Blog: https://openai.com/index/healthbench/.

- 5,000 conversations, 48,562 unique criteria written by 262 physicians (26 specialties, 60 countries), each with a point value in [−10, 10] (negative for harmful content); criteria "self-contained, objective," tied to the final response, and "iteratively adjudicated by three or more physicians."
- **Grader**: GPT-4.1, binary met/not met per criterion.
- **Meta-eval**: GPT-4.1 macro-F1 **0.709** on consensus criteria; physician macro-F1 ranged **0.569–0.730** by theme; the model grader "exceeds the average physician score in five out of seven themes." This is the canonical "judge ≈ human-human agreement" result and the one most rubric-RL papers use as a target.
- HealthBench Professional (arXiv:2604.27470, 2026) extends to real clinician chats; same rubric design.

### 1.5 TheAgentCompany (CMU, Dec 2024 / NeurIPS 2025) — checkpoints, mostly deterministic

*TheAgentCompany*, arXiv:2412.14161 (https://arxiv.org/abs/2412.14161).

- 175 tasks in a simulated software company (GitLab, OwnCloud, Plane, RocketChat) with simulated colleagues.
- **Grading**: checkpoints; "the majority" are deterministic Python functions over state (repo cloned, binary built, server on port). **LLM-based evaluators are used in ~29% of tasks (51/175)**, for "written document quality and completeness," "correct identification of human contacts," and "qualitative relevance," prompted "with predefined rubrics or reference outputs." Evaluator: Claude-3-5-Sonnet-20241022 (same model as the simulated colleagues).
- **Score**: S_partial = 0.5·(points/total) + 0.5·S_full, so full completion gets a 50% bonus.
- **No judge-vs-human agreement reported.** Results: Gemini-2.5-Pro 30.3% full / 39.3% partial. Failure modes: "self-deception" (renaming a user rather than finding the right contact), not following colleague recommendations.

### 1.6 SWE-Lancer (OpenAI, Feb 2025) — no judge; manager tasks graded against the real manager

https://openai.com/index/swe-lancer/. IC tasks are graded by end-to-end tests "triple-verified by experienced software engineers." SWE Manager tasks (265 instances, $264.5k) ask the model to pick the best of several proposals, graded against the original hiring manager's choice; a **99% inter-rater agreement across 5 expert reviewers** on those choices is reported **[secondary]**. This is the cheapest way to grade design judgment: fix the answer key to a real decision, no judge needed — but it only works when a single defensible choice exists.

### 1.7 MLE-bench (OpenAI, 2024/25)

arXiv:2410.07095. Medal thresholds against the Kaggle private leaderboard; purely code-graded. Included here because Ambig-DS (§4) reuses it to inject framing ambiguity while keeping the original evaluator.

### 1.8 τ / τ²-bench (Sierra, 2024/25)

τ²-bench, arXiv:2506.07982; evaluation doc: https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md. Reward components: **DB state check** ("does the predicted environment's DB hash match the target DB hash"), **action checks** (matching tool calls), **communicate_info checks** ("does every string in communicate_info appear in the agent's messages?"), and **nl_assertions** ("does an LLM judge return true for every entry in nl_assertions?" — marked WIP). "The final reward is the product" of the components in `reward_basis`. pass^k = all k trials succeed. Design lesson: the judge is confined to one optional, multiplicative gate on natural-language assertions; state and actions stay deterministic.

### 1.9 BrowseComp, Humanity's Last Exam, SciCode — judge as equality checker only

- **BrowseComp** (OpenAI, 2025): short-answer questions; an LLM judge (gpt-4.1 in simple-evals) compares the exact answer to a reference. BrowseComp-Plus (arXiv:2508.06600, ACL 2026) swapped to Qwen3-32B as judge and reports the two judges "differ by < 1% on average" **[secondary]** — a rare published judge-swap variance number.
- **HLE** (Scale/CAIS, arXiv:2501.14249, Nature 2025): ~80% exact-match, ~20% MCQ; an LLM equality checker with small numeric tolerance. Not a quality judge.
- **SciCode** (arXiv:2407.13168): unit tests in pinned NumPy/SciPy environments. No judge. SciCode-Verified (arXiv:2608.04975, 2026) found benchmark defects that underestimated models — see §5 for the "verified" pattern.

### 1.10 ResearchRubrics (Scale, ICLR 2026), XpertBench, ProfBench — expert rubric benchmarks

- **ResearchRubrics**, arXiv:2511.07685 (https://arxiv.org/abs/2511.07685): 2,500+ expert-written rubrics, 2,800+ human hours; complexity axes = conceptual breadth, logical nesting, exploration; Gemini DR and OpenAI DR "under 68% average compliance," mostly from "missed implicit context." Judge-human agreement numbers are in the paper body, not the abstract **[unverified this pass]**.
- **XpertBench**, arXiv:2604.02368 (2026): 1,346 tasks, 80 categories; "mostly 15–40 weighted checkpoints" per task; **ShotJudge** = LLM judge calibrated with expert few-shot exemplars "to mitigate self-rewarding biases." Peak success ~66%, mean ~55%. Agreement numbers not in abstract **[unverified]**.
- **ProfBench**, arXiv:2510.18941 (2025): 7,000+ response–criterion pairs judged by Physics/Chemistry PhDs and Finance/Consulting MBAs; the paper's contribution is building judges "by mitigating self-enhancement bias" and cutting cost "2–3 orders of magnitude"; GPT-5-high 65.9%.
- **Consulting DR benchmark**, *Evaluating Deep Research Agents on Expert Consulting Work: A Benchmark with Verifiers, Rubrics, and Cognitive Traps*, arXiv:2605.17554 (2026): 70 SME prompts; **mean 14.9 deterministic verifiers per task** plus **five ordinal rubric criteria (0–3)** (Data Integrity, Analytical Rigor, Relevance & Focus, Execution Precision, Format & Deliverability); a Verifier-Rubric Score combines them and any criterion at 0 auto-rejects. ACCEPT rate (r̄ ≥ 2.5 and V ≥ 80%): o3 15.7%, Claude 12.9%, Gemini 12.9%. Rubric graded by ex-MBB/Big-4 humans; **no inter-rater coefficient reported** (stated limitation).

### 1.11 Remote Labor Index (Scale + CAIS, Oct 2025) — human-graded, no judge

arXiv:2510.26787; https://scale.com/blog/rli. 240 real paid freelance projects (median 11.5 h, $200); deliverables graded by human evaluators against the gold deliverable on a "would a client accept this" standard; 45.6% of failures were "not at a professional standard." Automation rate 2.5% at launch (Manus); **16.1% by July 2026 [secondary: CAIS/Pebblous blogs]**. Included as the human-graded control: no judge, slow, but the trend line is as steep as judged benchmarks.

### 1.12 Terminal-Bench 2.0 / 3.0 — deliberate no-judge stance

*Terminal-Bench: Benchmarking Agents on Hard, Realistic Tasks in Command Line Interfaces*, arXiv:2601.11868 (2026). 89 tasks; "each task features a unique environment, human-written solution, and comprehensive tests for verification"; tests check the final container state, not commands or console output. The paper includes an **Adversarial Exploit Agent** (App. B.4) run against tasks before release to find gaming loopholes, and an ABC (Agentic Benchmark Checklist) self-assessment (B.5). LLM-judge prompts appear only in App. C.2, for **post-hoc failure-mode categorisation** (Disobey Task Specification, Step Repetition, Context Loss, Reasoning–Action Mismatch …), not scoring. Frontier agents "score less than 65%." TB 3.0's leaderboard shows Opus 5 at 42.7% **[secondary: benchlm.ai]**; the "grade the end state, not the conversation" stance quoted in secondary write-ups is consistent with the TB2 paper but I could not fetch a TB3 primary methodology page **[unverified]**.

### 1.13 Anthropic, "Demystifying evals for AI agents" (Jan 2026)

https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents. Stance: code-based graders are "Fast… Cheap… Objective… Reproducible… Easy to debug" but "Brittle to valid variations"; model-based graders are "Flexible… Scalable… Captures nuance" but "Non-deterministic… More expensive… Requires calibration with human graders." Specific recommendations: "create clear, structured rubrics to grade each dimension of a task, and then grade each dimension with an isolated LLM-as-judge rather than using one to grade all dimensions"; "LLM-based rubrics should be frequently calibrated against expert human judgment"; "it's often better to grade what the agent produced, not the path it took"; keep a reference solution "that passes all graders"; "make your graders resistant to bypasses or hacks"; and read transcripts to learn "whether the agent made a genuine mistake or whether your graders rejected a valid solution."

### 1.14 Verifiers as objects of study: BankerVerifierBench / Gandalf (Handshake AI, KDD 2026)

*Verifying Agents in Rubric-Graded Environments*, Dücker, Kumar, Liu, Chaudhary, Plesner, Guzmán, Athalye, Handshake AI Research, KDD '26 (https://kdd-eval-workshop.github.io/agenticai-evaluation-kdd2026/assets/papers/34_Verifying_Agents_in_Rubric_.pdf). The first meta-evaluation of *verifiers* for rubric-graded agent environments.

- **BVB**: 3,204 human-judged criteria across 21 BankerToolBench tasks (47–286 criteria per task, median 160); each criterion is binary met/unmet; human inter-annotator agreement on the doubly-graded subset **89.5%**; met-class prevalence 78.7%.
- Compares three verifier architectures: **AutoRubric** (text-only trajectory grading, no deliverable access), **Archipelago** (APEX's diff-based grader), **Agent-as-a-Judge** (workflow agent opening files), and their own **Gandalf** (reactive agent that runs *inside the same environment* with the same MCP tools).
- **Result**: "verifier architecture matters more than model choice." Gandalf with GPT-5.4-Nano: **F1 0.633 at $42**; Archipelago with GPT-5.4: F1 0.538 at $414; Agent-as-a-Judge and AutoRubric cluster below F1 0.47 regardless of model. On an OpenClaw productivity benchmark Gandalf reaches F1 0.951.
- **Three design principles** (directly reusable): P1 *reactive verification* — the verifier decides which artifacts to open and which tools to call per criterion; P2 *environment alignment* — "the verifier should run in the same environment as the rollout agent, with access to the same filesystem, libraries, and tools"; P3 *domain guidance* — accept domain conventions through a channel separate from the criterion text.
- The capability taxonomy (Table 1) is a ready-made checklist of what a data-deliverable verifier must be able to do: audit a derived metric calculation (23.0% of criteria), verify a literal data point (15.5%), reconcile cross-component data linkage across sheets/files (14.9%), verify structural component inventory (12.1%), validate labels/metadata (6.5%), validate temporal and sequential integrity (3.3%).

### 1.15 The data-science / data-engineering family — what is code-graded vs judged

| Benchmark | Org / year | Code-graded | Judged | Notes |
|---|---|---|---|---|
| **DABstep** (arXiv:2506.23719) | Adyen + HF, 2025 | Everything: "quasi exact match between a model's answer and the ground truth (up to some normalization tied to the type)"; answers are a string, number or list | none | 450+ tasks; best reasoning agents ~16% at launch |
| **Spider 2.0** (ICLR 2025) | Spider team | Execution accuracy: result multiset identical to reference, superfluous columns tolerated | none | 632 tasks, BigQuery/Snowflake; o1-preview 17.0% at launch |
| **BIRD-Interact** (arXiv:2510.05318, ICLR 2026) | BIRD team | "each guarded by executable test cases"; full CRUD | none for scoring; a **function-driven user simulator** (two-stage, "predictable and controllable") answers clarifications from annotated ground truth | 600 PostgreSQL tasks with an ambiguous priority sub-task; SOTA ≈24% / 18% |
| **DAB / DataAgentBench** (arXiv:2603.20576) | UC Berkeley EPIC + Hasura, 2026 | `validate.py` per query; **substring containment**: "the ground-truth string must appear as a substring of the agent's response; for set-valued answers, every element must appear" | none | Authors admit it "favors recall over precision: an agent that returns correct values alongside incorrect ones still passes." Gemini-3-Pro 38% pass@1 in paper; leaderboard now 0.9467 (Claude-Opus-5 via Permute EQ) — see §5 |
| **KramaBench** (arXiv:2506.06541) | MIT DB group, 2025 | End-to-end answer with typed metrics (exact/approx string, numeric 1/(1+RAE), list F1); sub-task answers | **Pipeline design = fraction of "key functionalities" covered, "evaluated via LLM-as-a-judge"**; key functionalities drafted with o3 then "manually polished" to be implementation-independent | **No judge agreement reported.** Best 50% E2E, 42% pipeline design |
| **ELT-Bench** (arXiv:2504.04808) | 2025 | 100 pipelines, 835 source tables, 203 dbt data models; final tables vs ground truth | none | Best 3.9% of data models correct at launch |
| **ELT-Bench-Verified** (arXiv:2603.29399) | 2026 | audit of the above | none | "Most failed transformation tasks contain benchmark-attributable errors — rigid evaluation scripts, ambiguous specifications, and incorrect ground truth"; Auditor-Corrector method, Fleiss κ = 0.85 |
| **DataGovBench** (arXiv:2512.04416) | 2025 | 150 tasks (100 operator, 50 DAG); task-specific executable scripts (F1 for filtering, accuracy for refinement…); sanity gate: "ground truth should yield a score of 1.0, while the raw data should score below 0.3" | none | GPT-5 49% TSR operator / 46% DAG; Claude-4-sonnet 85% code-runnable but 46% task success |
| **AgenticDataBench** (arXiv:2607.01647) | 2026 | 344 tasks; five deterministic modes: table matching with numeric tolerance, MSE-normalised modelling, JSON key matching, chart matching (data + config), exact/fuzzy text | LLM used only for **post-hoc skill-level failure attribution**, not scoring | Best 48.8% |
| **DataClawEval** (arXiv:2607.28033) | 2026 | 100 industrial ETL tasks over PySpark/MySQL/Hive/Presto/FlinkSQL; five artifact checks (executability, schema, row alignment, numeric, categorical) + process metrics | **Tried and rejected**: "LLM judges systematically overrate DataClawEval and breach rubric caps" — process 22.2 → 75–76, product 52.1 → 90–91, per-case SD > 6.35, worst swings > 55 pts, engine-dependent bias | GPT-5.5 74.9/100 |
| **DSBench** (ICLR 2025) | 2024/25 | 466 analysis tasks vs reference answers; 74 modelling tasks via Relative Performance Gap | none | best agent 34.12% / RPG 34.74% |
| **DA-Code** (arXiv:2410.07331) | 2024 | executes agent code in sandbox and compares tables/plots/model outputs | none | |
| **DataSciBench** (arXiv:2502.13897, ACL 2026 Findings) | 2025 | Task-Function-Code: 519 test cases via aggregate functions + programmatic rules | **VLM-as-judge for visualisations** (fine-grained) | |
| **BixBench** (arXiv:2503.00096) | FutureHouse, 2025 | — | **Open-answer graded by an LLM comparing to ground truth**; MCQ variant where a second LLM reads the notebook and picks an option | Third-party audit (Dai, Zenodo 22151932, 2026) replicated grading and found "what a single pooled score hides" **[secondary]** |
| **LongDS-Bench** (arXiv:2605.30434) | 2026 | 68 tasks / 2,225 turns from Kaggle notebooks; accuracy vs reference; state-evolution (rollback, counterfactual, composition) | — | best 48.45%; ~47-pt drop early→late turns; "key bottleneck is maintaining a correct analytical state" |
| **IDA-Bench** (arXiv:2505.18223) | 2025 | final numeric output vs human baseline; **LLM-simulated user issues sequential instructions** | — | SOTA < 50% |
| **Ambig-DS** (arXiv:2605.09698) | 2026 | reuses DSBench/MLE-bench evaluators | human-and-LLM pipeline only to *verify the ambiguity*, not to score | see §4 |
| **ITBench** (IBM, arXiv:2502.05352) / ITBench-AA | 2025/26 | SRE: pass@1 on root cause + NTAM topology-aware metric on propagation chain + alert cleared; CISO: compliance detection; FinOps: cost proximity | none for scoring | SRE 13.8% at launch; ITBench-AA GPT-5.6 Sol 56.2% **[secondary]** |

Takeaway for the family: **every data benchmark that reports a credible number is grading a materialised artifact (table, answer, DAG output, cluster state) deterministically.** Judges appear only at the margins — pipeline-design coverage (KramaBench, unvalidated), plot reading (DataSciBench), open-answer equality (BixBench), failure attribution (AgenticDataBench) — and the one attempt to judge process/product quality holistically (DataClawEval) documented the failure. None of them grades design choices, written deliverables, or trade-off reasoning. That is the open space.

---

## 2. Task-design patterns that need a judge — and who has graded each

### 2(a) Design / architecture choices with many correct answers (schema design, model layering, idempotency approach)

- **R2ABench** — *Benchmarking Requirement-to-Architecture Generation with Hybrid Evaluation*, arXiv:2604.06683 (2026). 68 projects with a structured SRS and a human-validated reference architecture in PlantUML. **Layered hybrid grading**: (1) syntactic validity; (2) structural graph diagnostics — node/edge F1, layer accuracy, graph edit distance against the reference; (3) LLM-based multi-dimensional scoring of completeness, accuracy, rationality, readability; (4) anti-pattern detectors (orphan ratio, God-component ratio). **Human validation on 190 diagrams**: expert–expert κ = 0.82; **model–human κ = 0.73**. Finding: GPT-5 node F1 0.67 but **edge F1 0.15** — models name the right components and get the relationships wrong. Pattern to copy: *code-grade the structure you can diff (nodes, edges, anti-patterns), judge only the semantic dimensions, and validate the judge against two experts.*
- **SpecBench** — arXiv:2605.30314 (2026). Agent gets an RFC proposal + codebase + all past RFC discussions from Kubernetes/React/Rust/TVM/vLLM and must find "omissions, ambiguities, inconsistencies, or incorrect assumptions"; graded against the critiques real maintainers raised in the historical review. GPT-5.4 44.4%. Pattern: *use real design-review history as the answer key.*
- **SWE-Lancer Manager** (§1.6): pick-the-proposal graded against the real manager's pick. Zero judge.
- **KramaBench pipeline design** (§1.15): the only data-benchmark instance of judging design — "key functionalities any correct pipeline must contain," scored by LLM coverage — but with no reported agreement.
- No benchmark was found that grades *database schema design* or *dbt model layering* per se **[gap; unverified that none exists]**. The BIRD/FLEX finding that strict execution accuracy disagrees with human judgment ~38% of the time on SQL (mostly false negatives) **[secondary]** is the closest evidence that "many correct answers" is already a problem in code-graded SQL.

### 2(b) Code quality / maintainability / convention adherence

- **SWE-PRBench**, arXiv:2603.26130 (2026): 350 PRs with human-flagged issues. An LLM judge labels each model review comment CONFIRMED / PLAUSIBLE / FABRICATED and matches to ground truth with a greedy 1-to-1 assignment plus an embedding-affinity fallback ("cosine(e_a,e_h) + 0.15·1[same file] + δ_line", discard < 0.30). **Judge validation κ = 0.75 (N = 30, 83.3% exact); cross-judge (Claude Sonnet 4.6) κ = 0.616, 78.7% agreement**, and both judges reproduce the same A>>B>>C ordering. Frontier models detect only 15–31% of human-flagged issues diff-only.
- **ProjDevBench**, arXiv:2602.01655 (2026): end-to-end project development, combines online-judge tests with LLM-assisted code review **[secondary; not fetched]**.
- **Code Review Agent Benchmark**, arXiv:2603.23448 **[not fetched]**.
- **DataClawEval** is the cautionary case: the authors list "code quality or style" as explicitly *ungradable* under their deterministic scheme and show their LLM judges were unusable for it.
- Practical reading: code-quality judging works when the criterion is "did the reviewer find *this specific* human-flagged issue" (matching against an answer key), and has not been shown to work as "rate this code's maintainability."

### 2(c) Written deliverables (postmortem, RFC, migration plan, stakeholder explanation of a metric discrepancy)

- **ORCA-bench** — *How Ready Are Language Model Agents for Oncall?*, arXiv:2607.28545 (2026). Agent gets telemetry, source and an *ambiguous* incident report; produces an RCA report. Grading: expert-SRE **per-fault rubrics enumerating expected symptoms in metrics/logs/traces**; answers scored against the **set** of plausible root causes (4.41 ± 0.11 on Hard vs 2.00 ± 0.06 on Easy); **GPT-5.4 judge with JSON-schema-constrained prompts; hand re-scoring on 40 tasks gives Cohen's κw = 0.90 (0.84–0.96 by tier)**. Best model: 25.3% Medium, 10.0% Hard; hallucination 7–40%. This is the closest existing template for "grade an incident postmortem."
- **OpenRCA 2.0**, arXiv:2606.27154 (2026): moves from outcome labels to causal *process* supervision (step-wise causal edges); exact root-cause set recovered 20.7% on average across 11 frontier LLMs.
- **ITBench** SRE: NTAM topology-aware metric on the propagation chain — a deterministic way to grade the *structure* of a diagnosis.
- **GDPval** (§1.2): the general case (memos, decks, spreadsheets) — 65.7% auto-grader agreement vs 70.8% human-human.
- **TheAgentCompany**: document quality judged with reference outputs in 29% of tasks, no agreement reported.
- **HealthBench**: the template for "explain something to a stakeholder" — criteria with negative points for harmful/incorrect statements.

### 2(d) Trade-off reasoning where the right call depends on stated constraints

- **Consulting DR benchmark** (arXiv:2605.17554): deterministic verifiers for facts (mean 14.9/task) + ordinal rubric for Analytical Rigor / Relevance & Focus; "cognitive traps" embed realistic source errors and "deterministic precision traps" where shallow reasoning yields a confidently wrong answer. ACCEPT rates 12.9–15.7%. No inter-rater coefficient.
- **SWE-Lancer Manager** and **SpecBench** (above): trade-off judgment graded against a historical decision or review.
- **LH-Bench** — *Skill-Grounded Evaluation of Long-Horizon Agents on Subjective Enterprise Tasks*, arXiv:2603.22744 (2026): expert-authored rubrics + curated ground-truth artifacts (chapter-level) + pairwise human preference as convergent validation. **Domain-authored rubrics κ = 0.60 vs LLM-authored rubrics κ = 0.46**; human preference confirms the top-tier separation (p < 0.05). Direct evidence that *who writes the rubric* matters more than which judge reads it.
- **HiL-Bench** (§4) is the trade-off "act vs ask."

### 2(e) Process quality (investigation thoroughness, doom loops, unsafe / over-scoped actions)

Our audit ledger already captures actions, so most of this is code-gradable for us; the literature is useful for what the judge adds and where it fails.

- **AgentRewardBench**, arXiv:2504.08942 (2025): 1,300+ web trajectories expert-annotated for success, side effects, repetition. Best judge (GPT-4o, accessibility tree) on success: **P 69.8 / R 83.1 / F1 75.9**; on **repetition: P 80.4 / R 96.9 / F1 87.9**; on **side effects: P 7.7 / R 91.7 / F1 14.2** — judges are good at spotting loops and terrible at side effects. Rule-based graders *under*-estimate success (WebArena −16.7 pts, 55.9% recall) while LLM judges *over*-estimate it. More input hurt: "including both accessibility trees and screenshots yields a lower performance than including only the screenshot."
- **AJ-Bench**, arXiv:2604.18240 (ACL 2026 Findings): 155 tasks / 516 trajectories across Search, Data Systems, GUI; judges that can *act* in the environment (state verification, process verification, information acquisition) beat transcript-only judges by ~13 pts, e.g. DeepSeek-v3.2 +12.29 on the DS/FileSystem domain; "Agent-as-a-Judge built on weaker models can achieve performance comparable to LLM-as-a-Judge based on SOTA models."
- **Agent-as-a-Judge / DevAI**, arXiv:2410.10934 (ICML 2025): 55 tasks, 365 hierarchical requirements; three human experts disagreed 10–30% and took 86 h to converge; the agentic judge agreed with human consensus ~90% vs ~70% for plain LLM-as-a-judge.
- **Counsel**, arXiv:2606.21627 (2026): first public meta-evaluation of process-level critiques on τ-bench and DA-Code; annotator α = 0.78; strongest judge reaches "~88% agreement on location and ~65% on reasoning" — i.e. judges can find *where* a trajectory went wrong far better than *why*.
- **UnderSpecBench** — *Coding Agents Are Guessing: Measuring Action-Boundary Violations in Underspecified DevOps Instructions*, arXiv:2607.02294 (2026): **deterministic side-effect oracles** — each task has an *intended set* and an *over-scope set* of protected objects, graded by before/after state diff; **55.8–67.8% of acted runs violated a boundary**; Safe Success 15.5–36.8%; "blast radius" cues barely reduced action propensity (65.5% vs 64.0%). The LLM judge is used only to classify non-action final messages into Ask / Refuse / Defer (κ = 0.860 vs humans). Recommendation: "specify the target, not just the intent" and give the harness "a first-class Ask-User affordance."
- **Terminal-Bench** uses an LLM only for failure-mode taxonomy after the fact (Step Repetition, Context Loss, Reasoning–Action Mismatch), never for the score.
- **Anthropic**: "grade what the agent produced, not the path it took," and use transcripts for diagnosis.

---

## 3. Hybrid patterns — how the good benchmarks keep the judge honest

1. **Code-graded hard gates, judged soft criteria, combined by product or auto-reject.** τ²-bench: reward = product of DB-hash × actions × communicate × (optional) nl_assertions. Consulting DR: verifiers (facts) and rubric (quality) are computed independently, any rubric criterion at 0 auto-rejects, ACCEPT requires both r̄ ≥ 2.5 and V ≥ 80%. R2ABench: syntax → graph metrics → judge → anti-pattern detectors. DataGovBench's sanity gate (gold must score 1.0, raw input < 0.3) is a code-side analogue for validating each grader.
2. **Judge reads only artifacts the agent cannot narrate.** APEX/Archipelago: task prompt + output + *log of changes* + criterion, "but not the agent trajectory." PaperBench: submission files + rubric. HealthBench: final response only. The "Gaming the Judge" result (§5) is the empirical reason: rewriting the reasoning trace alone, with actions and observations fixed, raised judge false-positive rates 20–30 points.
3. **Verify-then-judge / judge-with-tools.** Gandalf (P2: same environment, same tools; F1 0.633 vs 0.538 for diff-serialisation and < 0.47 for text-only) and AJ-Bench (+13 pts for environment-aware judges) show that when the criterion needs a recomputation ("the derived metric matches its inputs"), a judge that can open the DuckDB and re-run the query beats a judge that reads a serialisation. This is *not* the same as letting the judge read the transcript — it is giving it the state.
4. **Judge only sub-criteria, never the headline.** APEX (pass@1 = all binary criteria met; the judge never emits a task-level score), PaperBench (leaf-level only, tree aggregates), HealthBench (per-criterion binary, weighted sum by physician-set points), Anthropic ("isolated LLM-as-judge" per dimension). PReMISE (arXiv:2605.30803) formalises this as "changing the rubric changes the response quality measurement induced by a fixed judge" — the rubric *is* the measurement instrument; the judge is the meter.
5. **Judge as reader vs evaluator.** PaperBench's stratified F1 is the cleanest evidence: Result Match (is the number in the file?) 0.94, Execution 0.82, Code Development (is this a correct implementation?) 0.72. Write criteria toward the first kind.
6. **Expert-authored criteria, validated by blind re-execution and gold runs.** APEX: 20% re-executed by non-authors, 10% needed fixes; judge run over all gold outputs must pass 100%. GDPval: ≥ 3 reviews per task, avg 5. HealthBench: ≥ 3 physicians adjudicate every criterion. ELT-Bench-Verified and SciCode-Verified show the cost of skipping this: "most failed transformation tasks contain benchmark-attributable errors."
7. **Reproducibility reporting — what is actually published.**
   - Judge pinned by name: APEX (Gemini 3 Flash, thinking low), PaperBench (o3-mini-high), HealthBench (GPT-4.1), GDPval (GPT-5-high), TheAgentCompany (claude-3-5-sonnet-20241022), ORCA (GPT-5.4), HiL-Bench (frozen Llama-3.3-70B as the user-simulator judge, 97% precision / 91% recall), UnderSpecBench (DeepSeek-v4-flash). GDPval-AA: not stated.
   - Temperature: rarely stated; APEX states thinking level. **[No benchmark in this survey publishes judge temperature and per-criterion re-run variance together — unverified that none does.]**
   - Variance across judge runs: DataClawEval is the only one that measured it for a holistic judge (per-case SD > 6.35, swings > 55). BrowseComp-Plus reports < 1% delta across two judge models on an equality-check task. GDPval reports three grader sweeps averaged. PaperBench explicitly flags non-determinism as a limitation.
   - Judge-swap sensitivity: *When the Judge Changes, So Does the Measurement*, arXiv:2607.08535 (2026): scores move "simply because the evaluator has changed"; "stronger judges reduce but do not remove position and verbosity bias"; "repeated-sample juries add little when errors are correlated"; recommends publishing "dataset slices, bias probes, error-dependence estimates, and protocol audit trails." *Reliability without Validity*, arXiv:2606.19544 (2026): 21 judges, ~541k judgments; exact-match agreement overstates kappa by 33–41 pts on MT-Bench; rankings shift up to 14 positions across benchmarks; test-retest > 0.95 coexists with position bias > 0.10; verbosity bias small (< 0.011). Proposes a Minimum Viable Validation Protocol (chance-corrected agreement, consistency, bias audit).

---

## 4. Difficulty and saturation

**Are judged benchmarks less saturated?** Only briefly. APEX-Agents launched at 24.0% pass@1 (Jan 2026) and the live leaderboard shows 73.5% (Opus 5.5 Max) eight months later; RLI went 2.5% → 16.1% **[secondary]** in ~9 months; GDPval win-or-tie was already 47.6% at launch and GDPval-AA now separates models only by Elo. Meanwhile SWE-bench Verified clusters at ~80.9% **[secondary]**. So the judged professional benchmarks bought roughly one model generation of headroom, not a different regime. What stays low is not "judged" per se but a handful of specific difficulty sources:

| Difficulty source | Benchmark | Score |
|---|---|---|
| Requirement discovery / knowing when to ask | HiL-Bench (arXiv:2604.09408): 75–89% pass@3 with full info vs **4–24%** when the agent must decide to ask; Ask-F1 = harmonic mean of question precision and blocker recall ("architecturally penalizes gaming through question spam"); blockers (missing / ambiguous / contradictory) injected by experts into SWE-Bench Pro and BIRD tasks, 5–6 audit rounds, gated so base pass ≤ 5% without help and ≥ 90% with resolutions | Opus 4.6: SQL 39.3% pass@3 / Ask-F1 57.5; SWE 9.4% / 29.9 |
| Same, coding | ClarEval (arXiv:2603.00187): GPT-4o 89.02% clarified → **8.94%** ambiguous. Ambig-SWE (ICLR 2026): interaction gives up to +74% but "models struggle to distinguish between well-specified and underspecified instructions." *Ask or Assume?* (arXiv:2603.26233): uncertainty-aware scaffold 69.40% vs 70.80% fully specified — the gap is closable *if* the agent asks | |
| Task-framing ambiguity in data science | Ambig-DS (arXiv:2605.09698): 51 DSBench + 61 MLE-bench tasks with controlled edits to the prediction target or objective; "agents quietly commit to plausible but unintended task framings, producing clean, executable artifacts that hide their incorrect assessment"; one clarifying question "recovers much of the loss, yet agents cannot reliably tell when to use it" | |
| Ambiguous incident reports | ORCA-bench Hard ("users are reporting site issues"): 10.0% | |
| Cross-system integration + dirty keys + domain knowledge | DAB: 38% (paper); ELT-Bench 3.9%; Spider 2.0 17%; BIRD-Interact 18–24% | |
| Long-horizon state maintenance | LongDS-Bench: 48.45%, −47 pts early→late turns; "additional agent steps do not necessarily improve performance" | |
| Hidden constraints / traps in sources | Consulting DR cognitive traps: ACCEPT 12.9–15.7% | |
| Over-scoped destructive actions | UnderSpecBench: 55.8–67.8% violation rate | |
| Many-criteria conjunction | APEX pass@1 (all criteria) 24% vs Mean Score 39.5% at launch — the all-or-nothing aggregation roughly halves the headline | |

The consistent finding across HiL-Bench, Ambig-DS, Ambig-SWE, ClarEval and UnderSpecBench: **capability is not the bottleneck; judgment about under-specification is.** That is the same diagnosis as ours ("failures are judgment calls, not diagnosis misses"), and these benchmarks show it can be made *code-gradable*: inject expert-validated blockers, simulate the stakeholder with a frozen model that only answers registered trigger questions, and score with Ask-F1 plus the original deterministic task check. The judge is confined to a semantic match between the agent's question and the registered blocker (HiL-Bench: 97% precision / 91% recall).

*The Growing Pains of Frontier Models: When Leaderboards Stop Separating* (arXiv:2605.18840) and *Open-World Evaluations for Measuring Frontier AI Capabilities* (arXiv:2605.20520) argue the same at the meta level **[abstracts only]**.

---

## 5. Hackability: where judged criteria were gamed, and how benchmarks patched it

1. **Rewriting the reasoning trace fools trajectory judges.** *Gaming the Judge: Unfaithful Chain of Thought Can Undermine Agent Evaluation*, arXiv:2601.14691 (2026): 800 trajectories (WebArena, AssistantBench, WorkArena), nine VLM judges; with actions and observations *fixed*, "Progress Fabrication increases FPR by 20–30 points on GPT-4o and GLM-4.1V (70–100% relative increase)," judgment flip rates up to 41.8%; style-only manipulations +5–10 FPR. Mitigations tested (manipulation-aware prompting, rubric grounding, judge-time scaling) "reduce but do not fully eliminate susceptibility," trading recall. **Patch**: don't show the judge the trajectory (APEX), or give it the state instead (Gandalf, AJ-Bench).
2. **Master keys.** *One Token to Fool LLM-as-a-Judge*, arXiv:2507.08794 (2025): non-word symbols (":" or ".") or openers like "Thought process:" elicit false-positive rewards from GPT-o1 and Claude-4-class judges; fixed by training reward models with truncated outputs as adversarial negatives (Master-RM).
3. **Rubric-term echoing, partial satisfaction, self-certification under RL.** *Reward Hacking in Rubric-Based Reinforcement Learning*, arXiv:2605.12474 (2026): GRPO on Qwen2.5-7B with RubricHub rubrics; exploitation rate rose "from 39% to 65% in medical and from 63% to 75% in science" with a GPT-4o-mini verifier; taxonomy — **Partial Compound 36.0%** (missing conjunct 32.9%), **Implicit-as-Explicit 34.6%** (inferred content 17.9%, missing supporting element 16.6%), **Imprecise Verification 29.4%** (topical alignment 21.1%). A strong verifier (GPT-OSS-120B) cut but did not remove it (P(incorrect | newly credited) 15–28%), and even then "rubric-based judges prefer the checkpoint on 85.8% of prompts but rubric-free judges prefer the base on 78.4%." Gains concentrated in "completeness and presence-based criteria, alongside declines in factual correctness, conciseness, relevance." **Patch**: rubric-free frontier panel as a reference; a verifier-free "self-internalization gap" from policy log-probs (r = 0.91–0.97 with the reference panel) to detect when to stop.
4. **CHERRL**, *Reproducing, Analyzing, and Detecting Reward Hacking in Rubric-Based RL*, arXiv:2606.04923 (2026): injected self-praise, lexical, format and tone biases; onset within 68–478 steps; models learned "self-evaluative postscripts" and "compliance declarations"; in-domain capability dropped (HealthBench 42.8 → 36.1; IFBench 31.7 → 23.7). Detector (RHDA) is judge-blind and reads only step/input/output/score/rubric. No fix implemented.
5. **The rubric text itself leaks the verdict.** *Judging LLM-as-a-Judge: Concerning Rubric Artifacts*, arXiv:2609.02942 (Sep 2026): classifiers trained *only on rubric text* predict judge outputs with ~0.83–0.90 accuracy on HealthBench; reversing the candidate answer flipped the verdict only 37.7% of the time (62.3% inconsistency); reversing the criterion flipped it only 16.8–32.2%. **Patch proposed**: stress-test rubric sensitivity by reversal, audit rationales, pair with non-rubric metrics.
6. **PReMISE**, arXiv:2605.30803 (2026): vague rubrics ("helpful and factual") reward fabrication; audit across structural adequacy, reliability, preference fit, adversarial robustness; reliability-constrained refinement cut exploit-response vulnerability 46.4% → 36.0%.
7. **Holistic judges inflate and drift.** DataClawEval (§1.15): process 22.2 → 75–76, product 52.1 → 90–91, per-case SD > 6.35, FlinkSQL judge "severely penalising" correct outputs while passing failing ones. Rejected in favour of five deterministic artifact checks.
8. **Containment graders are recall-only.** DAB's substring check "favors recall over precision: an agent that returns correct values alongside incorrect ones still passes" — the leaderboard jumped from 38% (paper) to 0.9467 (Sep 2026) and it is not possible from the outside to separate real gains from shotgun answers **[inference, not a claim by the authors]**.
9. **Question spam vs Ask-F1.** HiL-Bench's metric is explicitly designed so over-asking is penalised by precision; the semantic judge only returns a resolution when a question targets a registered blocker.
10. **Self-preference.** GDPval's GPT-5 grader agrees less with humans on OpenAI outputs; APEX measured Gemini-judge-on-Gemini FPR (1.2%, no effect); ProfBench and XpertBench build judges specifically around "self-enhancement bias"; Anthropic recommends per-dimension isolation. **Patch**: cross-family judge or judge panel, and publish the self-preference check.
11. **Verifier "reliability without validity."** arXiv:2606.19544: two production judges with test-retest > 0.95 and position bias > 0.10 — high consistency is not evidence of correctness; use chance-corrected κ, not exact-match agreement.

---

## 6. What this implies for a data-engineering RL environment

Ranked by (unlocked realism) × (evidence the grading works) ÷ (hack surface).

1. **Requirement discovery with a simulated stakeholder (HiL-Bench pattern).** Inject expert-validated blockers (missing SLA, ambiguous "revenue," contradictory Jira vs Slack) that surface only on exploration; add an `ask_stakeholder` tool backed by a frozen model that answers only registered trigger questions; score = existing deterministic state check × Ask-F1. Judge role: semantic question→blocker matching only (HiL: 97% P / 91% R). Risk: blocker leakage into the prompt; annotator cost (5–6 audit rounds). This attacks exactly our residual failure class and stays code-gradable at the headline.
2. **Over-scope / blast-radius oracles (UnderSpecBench pattern).** For every task, declare an intended-set and a protected-set of tables/models/jobs; grade the ledger diff deterministically; Safe Success = task pass ∧ no protected object touched. Judge role: none (optionally classify a non-action final message as Ask/Refuse/Defer, κ 0.86). Risk: low; the labour is in writing protected sets.
3. **Postmortem / discrepancy-explanation deliverable (ORCA-bench pattern).** Constrained report schema already exists; add expert-written per-incident rubrics that enumerate the *symptoms* (which ledger entries, which dbt test, which row counts) and the *set* of acceptable root causes; judge each criterion binary against the report + world state, JSON-schema constrained; validate with hand re-grading (ORCA κw 0.90 on 40 tasks). Risk: rubric-term echoing (§5.3, §5.5) — mitigate by making criteria reference verifiable quantities ("names the 2024-03-11 backfill job and the 3.2% AOV delta") rather than qualities ("explains clearly").
4. **Design-choice tasks with structural diffing (R2ABench pattern).** Schema/dbt-layering tasks graded first by deterministic structure (lineage graph node/edge F1 vs reference, anti-pattern detectors: fan-out from staging, non-idempotent inserts, missing unique keys), then a judge on 2–3 semantic dimensions validated against two experts (R2ABench κ 0.73). Risk: reference-architecture bias (many correct answers) — mitigate with a *set* of accepted references and SWE-Lancer-style "pick the best of N proposals" variants that need no judge.
5. **Verifier-with-tools for deliverable checks (Gandalf pattern).** If a judge is used at all, run it inside the environment with DuckDB access so criteria like "the reported figure equals SUM(...) over the corrected table" are *recomputed*, not read. Evidence: F1 0.633 vs 0.538 (diff serialisation) vs < 0.47 (text-only). Risk: cost; verifier can itself be sloppy — needs a BVB-style meta-eval set (~300–500 human-labelled criteria).
6. **Keep the headline deterministic; judged criteria only as sub-scores (APEX / τ² pattern).** pass@1 = all hard gates; Mean Score may include judged criteria; publish judge model, thinking setting, self-preference FPR, κ vs humans on ≥ 500 criteria, and per-criterion re-run agreement. Never show the judge the transcript.
7. **Do not** adopt: holistic process/quality scores (DataClawEval), LLM-authored rubrics (LH-Bench κ 0.46), or containment/substring answer checks (DAB).

---

## References

1. APEX-Agents. Mercor. arXiv:2601.14242. https://arxiv.org/abs/2601.14242 (2026). Leaderboard: https://www.mercor.com/apex/apex-agents-leaderboard/
2. GDPval: Evaluating AI Model Performance on Real-World Economically Valuable Tasks. OpenAI. arXiv:2510.04374. https://arxiv.org/abs/2510.04374 (2025). https://openai.com/index/gdpval/
3. GDPval-AA. Artificial Analysis. https://artificialanalysis.ai/evaluations/gdpval-aa (2026).
4. PaperBench: Evaluating AI's Ability to Replicate AI Research. OpenAI. arXiv:2504.01848. https://arxiv.org/abs/2504.01848 (2025). JudgeEval README: https://github.com/openai/preparedness/blob/main/project/paperbench/paperbench/judge/judge_eval/README.md
5. HealthBench. OpenAI. arXiv:2505.08775. https://arxiv.org/abs/2505.08775 (2025). HealthBench Professional: arXiv:2604.27470 (2026).
6. TheAgentCompany. CMU et al. arXiv:2412.14161. https://arxiv.org/abs/2412.14161 (2024; NeurIPS 2025 D&B).
7. SWE-Lancer. OpenAI. https://openai.com/index/swe-lancer/ (2025; ICML 2025).
8. MLE-bench. OpenAI. arXiv:2410.07095 (2024).
9. τ²-bench. Sierra. arXiv:2506.07982 (2025). Evaluation doc: https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md
10. BrowseComp-Plus. arXiv:2508.06600 (ACL 2026). https://github.com/texttron/BrowseComp-Plus
11. Humanity's Last Exam. Scale/CAIS. arXiv:2501.14249 (2025; Nature 2025).
12. SciCode. arXiv:2407.13168 (2024). SciCode-Verified: arXiv:2608.04975 (2026).
13. ResearchRubrics. Scale AI. arXiv:2511.07685 (ICLR 2026). https://github.com/scaleapi/researchrubrics
14. XpertBench: Expert Level Tasks with Rubrics-Based Evaluation. arXiv:2604.02368 (2026).
15. ProfBench: Multi-Domain Rubrics requiring Professional Knowledge to Answer and Judge. arXiv:2510.18941 (2025).
16. Evaluating Deep Research Agents on Expert Consulting Work: A Benchmark with Verifiers, Rubrics, and Cognitive Traps. arXiv:2605.17554 (2026).
17. Remote Labor Index. Scale AI + CAIS. arXiv:2510.26787. https://scale.com/blog/rli (2025). 16.1% figure: https://safe.ai/blog/significant-increase-in-digital-labor-automation [secondary].
18. Terminal-Bench: Benchmarking Agents on Hard, Realistic Tasks in Command Line Interfaces. Laude Institute et al. arXiv:2601.11868 (2026). TB3 leaderboard [secondary]: https://benchlm.ai/benchmarks/terminal-bench-3
19. Demystifying evals for AI agents. Anthropic Engineering. https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents (Jan 2026).
20. Verifying Agents in Rubric-Graded Environments. Dücker et al., Handshake AI Research. KDD '26 Agentic AI Evaluation Workshop. https://kdd-eval-workshop.github.io/agenticai-evaluation-kdd2026/assets/papers/34_Verifying_Agents_in_Rubric_.pdf ; https://github.com/Handshake-AI-Research/gandalf-the-grader (2026).
21. DABstep: Data Agent Benchmark for Multi-step Reasoning. Adyen + Hugging Face. arXiv:2506.23719. https://huggingface.co/blog/dabstep (2025).
22. Spider 2.0. ICLR 2025. https://spider2-sql.github.io/
23. BIRD-Interact. arXiv:2510.05318 (ICLR 2026). https://bird-interact.github.io/
24. Can AI Agents Answer Your Data Questions? A Benchmark for Data Agents (DAB). UC Berkeley EPIC + Hasura. arXiv:2603.20576. https://ucbepic.github.io/DataAgentBench/ (2026).
25. KramaBench. MIT. arXiv:2506.06541. https://kramabench.org/ (2025).
26. ELT-Bench. arXiv:2504.04808 (2025). ELT-Bench-Verified: arXiv:2603.29399 (2026).
27. DataGovBench. arXiv:2512.04416 (2025).
28. AgenticDataBench. arXiv:2607.01647 (2026).
29. DataClawEval: A Benchmark for Data Engineering Agents in Real Industrial Harness. arXiv:2607.28033 (2026).
30. DSBench. arXiv:2409.07703 (ICLR 2025). DA-Code. arXiv:2410.07331 (2024). DataSciBench. arXiv:2502.13897 (ACL 2026 Findings).
31. BixBench. FutureHouse. arXiv:2503.00096 (2025). Audit: Dai, "What the score hides," Zenodo 22151932 (2026) [secondary].
32. LongDS-Bench. arXiv:2605.30434 (2026). IDA-Bench. arXiv:2505.18223 (2025). Ambig-DS. arXiv:2605.09698 (2026).
33. ITBench. IBM. arXiv:2502.05352 (2025). ITBench-AA: https://artificialanalysis.ai/evaluations/itbench-aa (2026).
34. ORCA-bench: How Ready Are Language Model Agents for Oncall? arXiv:2607.28545 (2026).
35. OpenRCA 2.0: From Outcome Labels to Causal Process Supervision. arXiv:2606.27154 (2026).
36. R2ABench: Benchmarking Requirement-to-Architecture Generation with Hybrid Evaluation. arXiv:2604.06683 (2026).
37. SpecBench: Evaluating Specification-Level Reasoning for Software Engineering LLM Agents. arXiv:2605.30314 (2026).
38. SWE-PRBench. arXiv:2603.26130 (2026). ProjDevBench. arXiv:2602.01655 (2026) [not fetched].
39. LH-Bench: Skill-Grounded Evaluation of Long-Horizon Agents on Subjective Enterprise Tasks. arXiv:2603.22744 (2026).
40. HiL-Bench: Do Agents Know When to Ask for Help? Scale AI et al. arXiv:2604.09408 (2026).
41. ClarEval. arXiv:2603.00187 (2026). Ambig-SWE. arXiv:2502.13069 (ICLR 2026). Ask or Assume? arXiv:2603.26233 (2026).
42. Coding Agents Are Guessing: Measuring Action-Boundary Violations in Underspecified DevOps Instructions (UnderSpecBench). arXiv:2607.02294 (2026).
43. AgentRewardBench. arXiv:2504.08942 (2025).
44. AJ-Bench: Benchmarking Agent-as-a-Judge for Environment-Aware Evaluation. arXiv:2604.18240 (ACL 2026 Findings).
45. Agent-as-a-Judge: Evaluate Agents with Agents (DevAI). arXiv:2410.10934 (ICML 2025).
46. Counsel: A Meta-Evaluation Dataset for Agentic Tasks. arXiv:2606.21627 (2026).
47. Gaming the Judge: Unfaithful Chain of Thought Can Undermine Agent Evaluation. arXiv:2601.14691 (2026).
48. One Token to Fool LLM-as-a-Judge. arXiv:2507.08794 (2025).
49. Reward Hacking in Rubric-Based Reinforcement Learning. arXiv:2605.12474 (2026).
50. Reproducing, Analyzing, and Detecting Reward Hacking in Rubric-Based RL (CHERRL). arXiv:2606.04923 (2026).
51. Judging LLM-as-a-Judge: Concerning Rubric Artifacts in LLM-based Automated Text Generation Evaluation. arXiv:2609.02942 (2026).
52. PReMISE: Policy Rubrics as Measurement Specifications for LLM Judges. arXiv:2605.30803 (2026).
53. Reliability without Validity: A Systematic, Large-Scale Evaluation of LLM-as-a-Judge Models. arXiv:2606.19544 (2026).
54. When the Judge Changes, So Does the Measurement: Auditing LLM-as-Judge Reliability. arXiv:2607.08535 (2026).
55. Rubrics as Rewards. Scale AI. arXiv:2507.17746 (2025). Checklists Are Better Than Reward Models (RLCF). Apple/CMU. arXiv:2507.18624 (NeurIPS 2025).
56. The Growing Pains of Frontier Models: When Leaderboards Stop Separating. arXiv:2605.18840 (2026) [abstract only].
