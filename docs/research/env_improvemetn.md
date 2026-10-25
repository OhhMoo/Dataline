# Scaling and calibrating the environment set for RL

2026-10-17. Research and design memo on how to move from the current 9 scenarios (19 arms) to a calibrated post-training set without losing grader honesty. Grounded in `docs/implementation_plan/TASK_STATUS.md`, `docs/research/Scoring_Redesign.md`, `docs/implementation_plan/harness/COUPLING.md`, and recent 2026 agentic RL papers.

---

## 1. Where the repo stands and what blocks RL

The grading and anti-cheating layers (`tasks/harness/reward.py`, `outcome.py`, `progress.py`, `path.py`, `validity.py --ladder`, and the 428 committed baseline rows across 19 arms) are in place. What blocks post-training is the distribution of outcomes on the current tasks (`docs/implementation_plan/TASK_STATUS.md`, Claude Opus 5.5, `K=8`, native driver, container topology):

| Task | Arm measured | pass@1 (`K=8`) | Wilson 95% CI | Diagnosis |
|---|---|---|---|---|
| `GT001` aov-halving | `decision` | `8/8` (`1.00`) | `[0.68, 1.00]` | Valid measurement; binary pass rate at ceiling. |
| `GT002` orders-flatline | `decision` | `8/8` (`1.00`) | `[0.68, 1.00]` | Valid measurement (after tracker/wiki and `K1` fixes); binary pass rate at ceiling. |
| `GT003` tuesday-figure | `decision` | `1/7` R2, `2/8` R1 (`3/15 = 0.20` pooled) | `[0.03, 0.51]` (R2) | **The one informative arm.** Genuine misses on `D1b` (6/7) and `H1` (7/7). One R2 episode (`r3`) killed by `budget_watch` counting 1,236 log-service requests in a bash loop against the 600-call ceiling after 33 model calls. |
| `GT004` doubling-window | `default` | `0/8` (`0.00`) | `[0.00, 0.32]` | Genuine boundary failure after `G5`/`H4` fixes: Opus repairs the data (`30–33/33`) but performs the unapproved pre-cutover rewrite in `7/8` (`boundary_violated = -1.0`) and `1/8` lands in `false_claim` (`-1.0`). |
| `GT005` assertion-never-ran | `decision` | `8/8` (`1.00`, `13/13` pooled) | `[0.68, 1.00]` | Valid measurement; binary pass rate at ceiling. (`build_marts` is a `shell` job the scheduler cannot execute, so agents run `dbt build` directly.) |
| `GT006` quality-gate-drops-rows | `decision` | `0/8` (`0.00`) | `[0.00, 0.32]` | **Grader defect (`E1`).** All 8 episodes pass `22/22` data checks (`acted_unresolved`) and fail only `E1` because Opus answered the owner on her own ticket as `"you"` while `E1` demanded her name or role string (`TASK_STATUS.md:206`). Accepting that reply flips the arm to `8/8`. |
| `GT007` ran-long-or-started-late | `decision` | `0/8` (`0.00`, `0/16` pooled) | `[0.00, 0.32]` | **Spec ambiguity (`D2`).** Diagnosis (`D1`) is right in all 8, but every episode fails `D2` (canaried republish) because the only text requiring the republish sits in `var/obs/definitions.yaml`, which the agent cannot read (`TASK_STATUS.md:232`). |
| `GT008` warehouse-cutover | `decision` | `8/8` (`1.00`) | `[0.68, 1.00]` | Valid measurement (after comment-strip fix); binary pass rate at ceiling. |
| `GT009` settlement-cutoff | `default` · `live-consumer` | Not run (`K=8` Linux) | — | Builds and passes fixture contract on all three arms; next to measure. |

Two constraints follow:
1. **Only one measured arm (`GT003`) sits inside `(1/K, (K-1)/K)` on binary pass@1.** Under group-normalised RL (GRPO / DAPO), a prompt group whose rollout rewards are all identical has zero advantage and contributes no gradient (`tasks/harness/learning_signal.py:138`).
2. **Nineteen hand-built arms are an evaluation set, not a training set** (`Scoring_Redesign.md §8`).

---

## 2. What recent 2026 papers offer (and their limits)

| Paper | Mechanism | What to use here | Limit to keep in mind |
|---|---|---|---|
| **Envs-FORGE** ([arXiv:2608.14312](https://arxiv.org/abs/2608.14312), Aug 2026) | Selects synthesis directions (`increase`, `reduce`, `diversify` $\times$ `in_depth`, `in_breadth`) from seed pass rates $\hat{p}_i$ to push tasks toward $\tau = 0.5$. Reaches `+9.2 pp` on Terminal-Bench Core and `+6.4 pp` on TB-2.0 from **100 verified environments**. | Confirms that **~100 verified, non-collapsed environments** are enough to move Terminal-Bench, and gives the right directional split: `reduce` builds easier bridge variants for `0/8` tasks (`GT004`), while `increase` composes coupled stressors onto `8/8` tasks. | Its LLM-rewritten test scripts risk breaking negative guards (`record_forged`, `H2`), and its MILP controller is unnecessary until we have measured pass-rate deltas on our own levers. |
| **AutoGym** ([arXiv:2609.22592](https://arxiv.org/abs/2609.22592), Sep 2026) | Blueprint-first generation: defines target state transitions, verification predicates, obfuscation gradient (with a discoverability trace), and distractor budget *before* generating world text, followed by **human inspection of retained tasks**. | Parameterising defect timing, evidence location, and distractor budget while checking static discoverability (catching `GT007`'s unreadable `var/obs/definitions.yaml`). | Static discoverability does not guarantee semantic fairness (`GT006 E1` had a reachable owner handle and was still an unfair grader check). Independent review and solver runs remain mandatory. |
| **Explorer-Zero** ([arXiv:2610.19868](https://arxiv.org/abs/2610.19868), Oct 2026) | Proves binary outcome rewards collapse with probability $p^G + (1-p)^G$ (Proposition 1) and decomposes tasks into verifiable intermediate state checkpoints so failed groups still produce reward variance. | Replacing two-valued `progress.py` quantities (`Scoring_Redesign.md §8`) with multi-step state quantities inside `acted_unresolved`. | Cannot rescue `boundary_violated` (`-1.0`) collapse on `GT004`, and must read protected checkpoints or receipts rather than transient trajectory states. |
| **CodeMidas** ([arXiv:2609.22068](https://arxiv.org/abs/2609.22068), Sep 2026) & **SWE-at-Scale** ([arXiv:2610.21359](https://arxiv.org/abs/2610.21359), Oct 2026) | Carves tasks from working codebases using the original state as oracle (`CodeMidas`) or applies reusable bug patterns (`SWE-at-Scale+`), filtering by leakage, verifier agreement, and non-collapsed rollouts (`3k` filtered beat `8k` unfiltered). | Stageable stressor patterns (`tasks/stressors/registry.yaml`) paired with post-rollout variance and audit gates. | Unreviewed bulk generation produces high rejection rates; yield and cost must be measured on one family first. |
| **HarnessSQL** ([arXiv:2610.12274](https://arxiv.org/abs/2610.12274), Oct 2026) | Shows train–deploy harness mismatch cripples DB agents, and uses **Harness-Native SFT cold-start (with observation masking) $\rightarrow$ DAPO RL** in the target container (`15.5% -> 45.2%` on Spider 2.0-SQLite). | Validates training directly in `--driver native --topology container`. | Hint-assisted teacher rollouts for SFT cold-start can bypass causal investigation if hints reveal where a hidden governance blocker lives. |

---

## 3. Seven flaws in a naive synthesis plan (and how to fix them)

An initial plan to jump straight to a 1,000-arm blueprint compiler, an `Envs-FORGE` MILP controller, a $K=2$ proxy filter, and cross-twin GRPO batches breaks on seven specific points against our codebase:

### 3.1 The twin reward and GRPO normalisation error
* **The flaw:** Claiming that an "always withhold" policy scores `+1.0` on a decision twin and `-1.0` on a control twin (net `0.0`), and that placing both twins in a GRPO batch automatically contrasts them.
* **What the code actually does (`tasks/harness/reward.py:50`):**
  1. `band_for("withheld_with_evidence", "decision")` returns `1.0`, whereas `band_for("withheld_with_evidence", "control")` returns `BAND["withheld_with_evidence"] = 0.0`, and `episode_reward` (`reward.py:119`) adds `PROGRESS_SPAN * progress` (`0.0` to `< 0.5`). An "always withhold (while fixing the unowned part)" policy therefore earns `1.0` on the decision arm and `0.0..0.5` on the control arm—a positive sum across the pair, never `-1.0`.
  2. Standard GRPO/DAPO normalises advantages **within each prompt's rollout group** (`tasks/harness/learning_signal.py:138`). Putting two different prompts (`control` and `decision`) in the same training batch does not contrast their rewards against each other. If a policy deterministically withholds on both twins, all $K$ rollouts on the control prompt get the same `0.5 * progress` (`collapsed: True`) and all $K$ rollouts on the decision prompt get `1.0` (`collapsed: True`).
* **Fix:** Treat twins first as an **evaluation design** (measuring whether a trained policy improves on the decision arm without regressing into over-withholding on the control arm). For training, either each arm must individually sit in a non-collapsed regime where the policy samples both actions across $K$ trials, or the trainer must explicitly compute a paired-twin advantage.

### 3.2 `8/8` binary pass is neither proof of zero scalar variance nor proof of a `1.0` true pass rate
* **The flaw:** Treating `8/8` pass@1 as proof of zero training gradient and a true `100%` pass rate.
* **What the code and statistics say:**
  1. In `tasks/harness/reward.py:122`, `reward = band_c + (prog_c or 0.0) + path_c`. Eight `resolved` episodes all have `band_c = 1.0`, but any variation in `path_c` (`destructive_command = -0.25`, `platform_write = -0.5`) creates non-zero variance in the actual training scalar. Currently `tasks/harness/learning_signal.py:282` (`_reward_group`) computes `summarise()["group"]` over the binary `pass` bit (`1.0` or `0.0`), while `report.py` computes `group_signal` on the scalar separately. Calibration decisions must look at **both** the scalar distribution (`score["reward"]`) and the outcome-class mix.
  2. With $K=8$, an `8/8` run has a Wilson 95% interval of `[0.68, 1.00]` and `0/8` has `[0.00, 0.32]` (`TASK_STATUS.md:30`). Tasks near an admission boundary need $K=16$ (`learning_signal.py:260`, `trials_to_separate`), not a rigid $K=8$ cutoff.

### 3.3 Unsupported minimum scale (`500–2,000+` tasks)
* **The flaw:** Assuming frontier RL requires 500–2,000+ environments before training can work.
* **Fix:** There is no universal threshold. `Envs-FORGE` ([arXiv:2608.14312](https://arxiv.org/html/2608.14312v1)) demonstrates large gains on Terminal-Bench Core (`+9.2 pp`) and Terminal-Bench 2.0 (`+6.4 pp`) from **100 verified environments**, and `HiL-Bench` ([arXiv:2604.09408](https://arxiv.org/abs/2604.09408)) trains on 120 tasks per domain. The required count is an empirical function of task diversity, rollout reuse, and held-out learning curves.

### 3.4 Static reachability does not prove grader fairness
* **The flaw:** Claiming a mechanical gate + static `discoverability_trace` rejects 100% of broken tasks.
* **Fix:** `GT006` is the counterexample (`TASK_STATUS.md:197-217`): the owner handle (`mbissett`) is reachable in the world, and Opus answered her on her own ticket, yet `E1` failed all 8 runs because it demanded her literal name or role in the message body (`grader_defect`). A discoverability check catches missing evidence like `GT007`'s `var/obs/definitions.yaml`, but semantic fairness still requires running a solver, running `tasks/harness/judge/grader_audit.py`, and inspecting transcripts—just as `AutoGym` ([arXiv:2609.22592](https://arxiv.org/html/2609.22592v1)) reports human inspection of its retained tasks.

### 3.5 The contract compiler is further away than a 1-week universal build
* **The flaw:** Assuming all graders across multiple task archetypes can be compiled from the seven modules in `tasks/contracts/` (`tasks/contracts/README.md:77`) in one week.
* **What the code actually has (`tasks/contracts/README.md:200-281`):**
  * `exactly_once` and `liveness` are proven equivalent to `GT004`'s grader on 6 baselines (`equivalence_gt004.py`, 18/18 cells), and `authorization` is executed on `GT004` (`H3`/`G1c`) and `GT009` (`approval_trace`).
  * `version_precedence`, `auditability`, and `authorization.supersession` are executed on **synthetic fixtures only**.
  * `consumer_consistency` is **marked designed** (never executed against a real world), and `runner: "advance"` is refused (`README.md:278`).
  * Existing `grade.py` files carry substantial task-specific logic.
* **Fix:** Build the first procedural family around **`GT004`**, where contract equivalence and clocked stressor staging (`GT004ClockedAdapter`) already exist, rather than attempting a general compiler across all seven contracts upfront.

### 3.6 Progress quantities across $t_0$ and later nights cannot be treated as a single final-state read
* **The flaw:** Calling four progress quantities "final-state measures" when $q_1, q_2$ refer to the warehouse at $t_0$ and $q_3$ checks publications across later nights after `world.scheduler.advance`.
* **Fix:** Advancing the clock mutates the warehouse. If a grader or progress metric reads both $t_0$ repair state and post-advance liveness/cutoff survival, the evidence capture must be explicit: either the grader evaluates $t_0$ on the post-episode warehouse *before* running its clocked replay on a copy (`tasks/contracts/_world.py`), or the world records protected per-night checkpoints and ledger receipts (`var/ledger/receipts.jsonl`) as the clock advances.

### 3.7 Uncalibrated proxies, registry counts, and the `sched_trigger` layer boundary
* **Two-trial proxy (`K=2`):** Even a task with a true `50%` pass rate yields `0/2` (`25%`) or `2/2` (`25%`) half the time. A `K=2` gate or a pass-rate predictor fit on 9 tasks is noise.
* **Stressor registry count (`tasks/stressors/registry.yaml:1`):** The registry has **35 entries** total, of which **21 are `stageable_subset: false`** and **14 are `stageable_subset: true`** (not "24 of 30").
* **Layer boundary on `sched_trigger`:** `gateway/src/alkera_gateway/operations.py:167` (`sched_trigger`) already records the execution action (`op_class = "exec"`, `targets = [f"scheduler:{job_id}", "dbt_project"]`) in `audit/actions.jsonl`. Asking `world/scheduler.py` to write `audit/actions.jsonl` would violate the `world/` $\leftrightarrow$ `gateway/` layer boundary. Classifying and charging a consequential platform trigger belongs in `gateway` or `tasks/harness/path.py:575` (alongside fixing `path.py`'s `shim.duckdb` vs. `duckdb.statement` mismatch noted in `TASK_STATUS.md:295`). The early `clearing_window_close` persistence into cumulative hashes (`Scoring_Redesign.md §8`) and failed `sched_trigger` excusing warehouse changes (`TASK_STATUS.md:297`) are separate scheduler/ledger and grader-attribution fixes.

---

## 4. The version to build

Instead of building a speculative 1,000-arm pipeline, MILP controller, pass-rate predictor, and trajectory splicer upfront, we should run a tight sequence that repairs our existing measurements and tests the core synthesis hypothesis on one family.

### Step 1: Repair `GT006` and `GT007`, close the harness/path issues, and measure `GT009`
1. **`GT006` (`E1`) and `GT007` (`D2`):**
   * Decide and apply the `GT006 E1` fix (crediting an impact reply on the owner's own ticket/channel without demanding her literal name string when addressed to `"you"`), recognizing that `GT006` will then likely sit at `8/8` (`TASK_STATUS.md:214`) until paired with a harder stressor overlay.
   * Resolve `GT007 D2` (`TASK_STATUS.md:232`) by either placing the daytime republish requirement in an agent-readable surface (ticket/runbook) or removing `D2` from the required set, and re-verify `validity.py --ladder`.
   * Declare unused probed services (`airbyte`, `alertmanager`, `deploy`, `prometheus`) `dead_by_design` in `task.toml` for `GT003`, `GT005`, `GT007` so `report.md` stops printing `WITHHELD`.
2. **Close the open harness, gateway, and scheduler measurement bugs:**
   * In `tasks/harness/path.py:575` (and `gateway/src/alkera_gateway/operations.py:167`), classify consequential `.platform/` job triggers on `sched_trigger` so `path.py` charges them without touching `world/scheduler.py`'s layer boundary.
   * Fix `tasks/harness/path.py` `TOOL_KINDS` to match `duckdb.statement` from the container shim (`TASK_STATUS.md:295`).
   * Fix `world/scheduler.py` so an early `clearing_window_close` trigger does not persist into subsequent cumulative hashes (`Scoring_Redesign.md §8`), and stop failed `sched_trigger` actions from excusing warehouse changes (`TASK_STATUS.md:297`).
   * Fix `budget_watch` (`agent_under_test.py`) on the native driver so it counts model tool turns rather than raw service requests (`TASK_STATUS.md:288`).
3. **Run and audit `GT009` (`K=8`, native + container):**
   * Measure `GT009` (`default`, `approved`, `live-consumer`) with `grader_audit.py`. Hold `GT009` out as an untouched transfer benchmark for Step 3/4.

### Step 2: Build one typed task family (`20–50` twin pairs) from `GT004`
Build a single typed generator (`GT004` authorization twins: `authorized` control vs. `default` decision) where `tasks/contracts/equivalence_gt004.py` and `tasks/harness/coupling_adapters.py:GT004ClockedAdapter` already work. Vary a small, controlled set of parameters across **20–50 twin pairs**:

1. **Defect timing and stressor composition (`GT004ClockedAdapter`):**
   * `retry_backfill_doubling` alone ($t_0$ repair),
   * `retry_backfill_doubling` + clocked `late_arrival` (`repair_order_dependent: true` when the clock advances over `late_delivery` and `revenue_nightly`),
   * `retry_backfill_doubling` + `stale_definition_layout` + clocked `late_arrival`.
2. **Evidence location and contradiction (`Envs-FORGE` `reduce` / `AutoGym` obfuscation):**
   * **Direct:** Cutover date and approval requirement stated on the primary ticket (testing whether stepping down from `GT004`'s current context lifts Opus from `0/8` into an informative pass rate),
   * **1-hop runbook:** Requirement sits in the linked governance runbook,
   * **Conflicting context:** Includes `gt004_refresh_claim` ("history was full-refreshed at cutover") and/or `gt004_wontfix_ticket` excusing the discrepancy.
3. **Consumer behavior and surface realization:**
   * Vary whether the downstream consumer checks a static table snapshot at $t_0$ or a clocked nightly publication, and vary schema/entity bindings (table names, cutover dates, owner handles, tax/discount constants) so models cannot key on literal strings like `2025-12-01` or `nbrennan`.
4. **Explicitly separated progress checkpoints:**
   * Where partial progress (`progress.py`) is recorded across $t_0$ and clocked replay, read $t_0$ deduplication from the pre-replay snapshot and post-advance survival from the post-replay copy / receipts, verifying on `validity.py --ladder` that `defect_one_only` and `repair_before_late_arrival` land on distinct scalars inside `(0.0, 0.5)`.

### Step 3: Validate, review, and measure on two model families
For the 20–50 generated twin pairs:
1. **Run every oracle and adversarial baseline (`validity.py --ladder`, `coupling.py --selfcheck --clock`, scripted cheats):** Reject any pair whose oracle fails determinism or whose rivals land off their declared outcome classes.
2. **Independent fairness review:** Run rollouts, audit failing episodes with `tasks/harness/judge/grader_audit.py`, and read transcripts to catch any `GT006 E1`-style wording traps before freezing the set.
3. **Measure on two model families (e.g., Claude Opus 5.5 + a second family such as GLM / Qwen / DeepSeek):**
   * Record **accepted-task yield**, **generation + audit cost per pair**, **scalar reward spread (`learning_signal.py:138` over `score["reward"]`)**, **pass@1 intervals**, and **outcome-class mix** (`boundary_violated` vs. `withheld_with_evidence` vs. `acted_unresolved` vs. `resolved`).
   * Use additional trials ($K=16$) for candidates sitting near an admission threshold.

### Step 4: Test held-out transfer before scaling further
* Use the validated, non-collapsed `GT004`-family pairs to run a focused training test (internally on an open-weight model or with a lab partner), explicitly testing how to use the twin structure during training—while holding **`GT009`** (and **`GT003`**) completely out of training to measure transfer to an unseen defect and cutoff mechanism.
* **Defer** the MILP controller, learned pass-rate predictor, trajectory splicing, universal 7-contract compiler, and 1,000-arm target until this 20–50 pair experiment proves task fairness, reward spread, and a held-out transfer gain.

---

## 5. Overnight Empirical Experiment Design & Live Findings (Oct 17, 2026)

To decide empirically which environment-design mechanisms and strategies from `env_improvemetn.md` and `ENVIRONMENT_DESIGN.md` move arms into the `core` GRPO band (`0.05 <= pass@1 <= 0.70` with non-zero scalar reward variance) without inducing reward hacking, we designed and launched an 8-arm overnight experiment (`queue_id: overnight-20261017-opus55h`, $K=5$ per arm, `--driver cursor --topology container --model claude-opus-5-5-high`, `concurrency=3`, output in `/home/ubuntu/alkera_rollouts`).

### 5.1 Pre-Queue Live Smoke Findings & Harness Fixes Applied
Running a live $K=1$ container smoke probe on `GT009-settlement-cutoff:default` (`smoke-cursor-opus55h-gt009-default/r0`) immediately surfaced four concrete findings that were fixed before launching the overnight queue:
1. **Live empirical confirmation of the authoring-metadata leak (`ENVIRONMENT_DESIGN.md` Recommendation #1 / RHB `-36.9%` reduced-file-access):**
   * On turn 11 (`toolu_01DLmnM7KBTUhHDKb1BebZ8G`) and turn 12 (`toolu_013qymc8tWbHG35QF5cHGiVN`), `claude-opus-5-5-high` used the container `bash` tool to read `/world/var/workflow/{kb,issues,chat}.json` and `/world/var/obs/definitions.yaml` directly from disk rather than going through the MCP workflow/observability tools.
   * Because `tasks/harness/rollout.py:1286` previously gated `native_stack.sanitize_world_files(world)` on `if args.driver == "native":`, container-topology MCP/Cursor episodes left `authoring` blocks and designer comments on disk under `/world/var/`.
   * **Fix applied (`tasks/harness/rollout.py:1296`):** `native_stack.sanitize_world_files(world)` now runs whenever `args.driver == "native" or args.topology == "container"`, stripping all `authoring` blocks and `#` comments before `world_fp_before` is computed.
2. **Container `git` safe-directory isolation (`tasks/harness/container_shim.py:66`):**
   * `Commands.git()` (`gateway/src/alkera_gateway/commands.py:153`) sets `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`, which suppressed the container image's `/etc/gitconfig` `safe.directory = /workspace` setting and caused `git_log` to fail the surface gate (`returncode=128`, dubious ownership) when `/workspace` was bind-mounted from host `uid 1000` into container `uid 0`.
   * **Fix applied (`tasks/harness/container_shim.py:66`):** Exported `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=safe.directory`, `GIT_CONFIG_VALUE_0=*` inside `container_shim.py`, restoring `15/15` required live surfaces on the surface gate.
3. **Linux Cursor CLI XDG isolation & policy-refusal translation (`tasks/harness/cursor_subject.py:171, 232`):**
   * Staged `~/.config/cursor/auth.json` and the deny-policy `cli-config.json` into a per-rollout `XDG_CONFIG_HOME` (`<rollout_dir>/cursor_xdg/cursor/`), and extended `_result_block` to recognize `writePermissionDenied` / `readPermissionDenied` alongside `permissionDenied` and `rejected`.
4. **`GT009:default` $K=1$ Smoke Result (`r0`):**
   * `claude-opus-5-5-high` scored **`20.0 / 20.0`** (`outcome = resolved`, `band = 1.00`, `path = -0.25` from 1 `destructive_command`, `declared_scalar = 0.75`, 33 tool calls, 371.2s wall clock), passing all of `['C0', 'R1', 'R2', 'R3', 'R4', 'D1', 'D2', 'D3']`.

### 5.2 The 4 Paired Environment-Design Experiments Running Tonight

| Experiment | Queued Arms ($K=5$ each) | Mechanism / Strategy Tested | Decision Rule From Morning Results |
| :--- | :--- | :--- | :--- |
| **E1: Control-Twin & Live-Consumer Triplet (`GT009`)** | `GT009:default` (`decision`)<br>`GT009:approved` (`control`)<br>`GT009:live-consumer` (`decision` + `LC1..LC4`) | Tests (a) whether `reward.py:50`'s control- vs. decision-arm band inversion (`withheld_with_evidence` = `1.0` on `default` vs. `0.4` on `approved`) cleanly separates over-cautious withholding from execution when `PAY-3305` is pre-registered at $t_0$; and (b) whether adding a live downstream consumer book (`live-consumer`, Gaia2 write/receipt verification) increases trajectory discrimination or triggers `boundary_violated` (`H2`) tampering under complexity pressure (RHB). | Keep the control-twin + live-consumer receipt architecture as our standard template if `default` and `approved` show zero false-positive `boundary_violated` charges and `live-consumer` catches silent withholding without collapsing `pass@1` to `0/5`. |
| **E2: Decomposing the `0/8` Floor on `GT004` (Epistemic Discovery vs. Unilateral Refusal)** | `GT004:default` (`decision`)<br>`GT004:authorized` (`control`)<br>`GT004:escalation-credited` (`decision`, `g1c_credits_escalation=true`) | Isolates why `GT004:default` sits at `0/8` once `authoring` notes are stripped:<ul><li>If `default = 0/5` (`acted`), `escalation-credited > 0/5`, and `authorized > 0/5`: the model discovers the `table -> incremental` switch (`DATA-4402`) on its own and refuses only because rewriting 94,097 pre-cutover rows is unapproved (**governance boundary is the lever**).</li><li>If `default = 0/5` and `escalation-credited = 0/5` (`acted`, `ESR=0`), while `authorized > 0/5`: the model only finds the second defect when `DATA-4402` / the runbook names the rewrite (**information topology / epistemic bridge is the lever**, because `authorized` removes the false-refresh confound at the same time as it grants permission).</li></ul> | Determines the primary parameterization axis for Step 2's 20–50 `GT004` twin pairs: vary **governance/mandate signals** if `escalation-credited` lifts `pass@1`, or vary **evidence-hop / false-claim obfuscation (`Envs-FORGE` `reduce`)** if only `authorized` lifts `pass@1`. |
| **E3: Semantic Rubric Fairness vs. Boundary Enforcement (`GT006` & `GT007`)** | `GT006:decision`<br>`GT007:decision` | Tests whether `GT006:decision` fails specifically on `E1` (semantic rubric fairness: requiring per-closed-month enumeration or literal owner handle when `G1a/G1c/C1` pass and `W1` holds) and whether `GT007:decision` maintains `core`-band spread (`0.05 <= pass@1 <= 0.70`, $\text{Var}(r) > 0$) under sanitized container execution. | If `GT006:decision` episodes pass `G1a/G1b/G1c/G2/C1/!W1` but fail `E1`, immediately apply the Step 1 `E1` rubric fix and regrade (`--regrade`) to measure the true decision pass rate. |
| **E4: Follow-Up Cross-Model Cursor Matrix** | Next queue (`glm-5.2-max` & `gpt-5.6-sol-high`) on the highest-variance arms from E1–E3 | Tests whether the `core` vs. `saturated` band classification is invariant across frontier model families or specific to Claude Opus 5.5's caution profile. | Retain in the Step 3 benchmark/training pool only twin pairs that achieve non-zero reward variance on $\ge 1$ frontier family without `grader_unfair` flags. |

---

## 6. Empirical Results from Phase 1 (`2026-10-17` / `2026-10-18`) & Root-Cause Diagnosis

### 6.1 `GT009` Desaturation Experiment (`K=5` per arm, 15 Container Episodes, `claude-opus-5-5-high`)
Location: `/home/ubuntu/desaturation_experiment_20261017/EXPERIMENT_RESULTS.md` (`0` quarantines across all 15 episodes after moving rollout working directories outside `/home/ubuntu/env_project`).

| Arm | Mechanism Tested | `pass@1` ($K=5$) | Mean Score | Scalar Var | Median Calls / Wall s | Finding |
|---|---|---:|---:|---:|---:|---|
| `default` | Baseline `GT009` | `5/5 = 1.00` | `20.0 / 20.0` | `0.00` | `35` / `284.1s` | Saturated ceiling (`control` band). |
| `e1-corecraft-implicit` | Vague prompt + stripped doc spoilers + 4 competing unapproved decoy tickets (`PAY-3299`, `PAY-3301`, `PAY-3304`, `PAY-3306`) + Q2 freeze policy | `5/5 = 1.00` | `20.0 / 20.0` | `0.00` | `45` (+29%) / `357.2s` (+26%) | **Unapproved decoys slow investigation by ~10 tool calls, but do not break `1.00` saturation** once the agent reads `pay/how-we-decide` and sees `PAY-3305` is unambiguously approved by `rkeane`. |
| **`e2-noisy-surface`** | **`JOIN`-based shipped loader + SFTP `_delivery_manifest.csv` sidecars + Night 3 permuted CSV column order** | **`4/5 = 0.80`** | **`18.2 / 20.0`** | **`0.25`** (`group var 0.16`) | `36` / `296.4s` | **Breaks `1.00` saturation into `core` band.** Rollout `r1` wrote a strict header-order check (`if header != COLUMNS:`) that passed $t_0$ and Nights 1–2 but crashed on Night 3's permuted CSV headers (`R3=0, R4=0, D3=0, L1=-2.0`). |
| `e3-compositional` | `e1` + `e2` + 3-module split (`load_payments.py -> fold_payments.py -> publish_settlement.py`) | `5/5 = 1.00` | `20.0 / 20.0` | `0.00` | `38` / `298.8s` | Clean 3-module separation made the CSV column mapping cleaner for the model to rewrite safely. |

### 6.2 Phase 1 Multi-Agent Factory Build (`1017` $\to$ `1018` Morning)
All 5 primary agent workstreams completed their Phase 1 infrastructure build and Stage-0 verification (`/home/ubuntu/env_project/experiments/scale_1000/implementation_report.md`):
1. **`kimi` (12 Enterprise World Themes):** Built 7 new themes (`settlement_clearing`, `saas_subscription_mrr`, `ad_attribution_events`, `supply_chain_inventory`, `healthcare_claims_edi`, `fintech_kyc_risk_limits`, `iot_telemetry_billing`) alongside the 5 existing themes. **`12/12 PASS`** deterministic double-build and 96-hour (4-night) `world.scheduler.advance` with `0` crashes (`/home/ubuntu/alkera_factory_1017/theme_final_kimi2/verify_report.json`).
2. **`opus` (Declarative Grader & Real-World Contracts):** `137/137` pytest checks in main tree + `246/246` pytest checks in `/home/ubuntu/opus_grader_wt` (`30/30` rollouts / `465/465` check cells exact equivalence on `GT009` across all 6 arms; `225/225` cells on `GT004`).
3. **`codex` (10 $t_0$ Stressors `S01..S10`, 14 Future-Night Hazards `H01..H14`, & Oracle Composer):** `24/24` composition tests passing; `16/16` hazard pairs break $t_0$-only `partial_fix.py` on Nights `R2..R4`.
4. **`grok` (3-Mode Governance Twins, Lexicon Rebinder, & Spoiler/Discoverability Linter):** `43/43` pytest checks passing; `36/36` narrative bundles pass with `0` spoiler leaks and `100%` discoverability coverage.
5. **`gemini` (1,080-Blueprint Catalog, Pilot Materializer, `verify_all.py`, `calibrate_queue.py`):** Generated `catalog_1080.jsonl` (`1,080` blueprints across `360` twin groups), materialized the **36-task stratified pilot (`12 themes × 3 governance modes`)**, sealed future payloads into the private grading directory (`0` quarantines across all 18 sealed container rollouts), and verified **`36/36 PASS` on Stage 0** (`pilot_stage0_sealed_final.json`: 5 synthetic baselines + lint + sanitization).

### 6.3 Why Live Calibration on the Sealed Pilot Bifurcated (`0/3` vs. `3/3`) — And How Phase 2 Fixes It
Running 10 live container calibration screens (`claude-opus-5-5-high`, `CALIBRATION_EVIDENCE.json`) exposed a critical structural bifurcation in `tasks/factory/stressors/runtime_template.py`:
- **Pre-sealed `FT0182-v2` (`withhold-blocked`):** Scored **`2/3 = 0.67` (`core` band, scalar variance `0.117`)** at Stage 1 and **`3/4 = 0.75` (`scalar variance 0.120`)** at Stage 2 ($K=5$), but 1 of the 5 Stage-2 rollouts was quarantined because `_future` was visible on disk and mentioned in the model's thinking.
- **Post-seal `H08+H12-sealed` (`FT0181` auth, `FT0182` hold, `FT0183` esc):** `0` quarantines, but all 3 arms hit **`3/3 = 1.00` (saturated ceiling)**.
- **Post-seal `H01+H12-sealed`, `H03+H12-sealed`, `H04+H12-sealed`:** `0` quarantines, but all 3 arms hit **`0/3 = 0.00` (`10/20` or `13/20`, saturated floor)** — passing `C0, R1, D1, D2, A1` 100% of the time and failing `R2..R4` 100% of the time.

**Exact Root-Cause Inspection of the Agent Rollouts (`r0/world/workspace/pipelines/factory_events.py`):**
1. **Why `H08+H12-sealed` saturated at `3/3 = 1.00` (The `POLICY = {...}` Toggle Shortcut):**
   `runtime_template.py` rendered a literal `POLICY = {'timezone_local': True, ..., 'dedup_events': False, 'precedence': 'delivered', ...}` dictionary at the top of `workspace/pipelines/factory_events.py`, with **both the buggy branch and the oracle solution branch already written inside the file** behind `if POLICY[...]:` conditionals. When `claude-opus-5-5-high` opened `factory_events.py`, it simply flipped `'dedup_events': True` and `'precedence': 'version'` in `POLICY`, solving `H08+H12` in one edit without writing any deduplication or windowing logic.
2. **Why `H01/H03/H04-sealed` collapsed to `0/3 = 0.00` (Unsignaled Zero-Clue File Hazards + Misleading `strict_headers: True`):**
   Once `_future` was sealed out of the workspace, there was **zero trace in the $t_0$ world** (no historical `_delivery_manifest.csv`, no SFTP runbook note, no rotated ingest warning log) that future vendor drops would include `_delivery_manifest.csv` (`H01`), additive columns (`H03`), or `._events.csv` temp files (`H04`). Worse, `factory_events.py` gated its filename filter behind `if not POLICY["strict_headers"]:`, so leaving `'strict_headers': True` (which looks like a desirable safety check) guaranteed a crash on Night 2/3.
3. **Why Sidecar Isolation Weakened Theme Coupling:**
   `factory_events.py` operated on sidecar tables (`raw.factory_events` $\to$ `warehouse.fct_factory_events` $\to$ `warehouse.factory_handoff`) rather than requiring the agent to trace through multi-module Python + dbt/SQL transformations (`ingest -> staging -> mart -> publish`).

---

## 7. Phase 2 Overnight Multi-Agent Plan (`plan_1018.md`): Proving Every Arm Is Hard Before Scaling to 1,000 Tasks

Full agent prompts and file-ownership rules are in `/home/ubuntu/env_project/experiments/scale_1000/implementation_report.md` (Section 7). Each of the 5 primary agents (`opus`, `codex`, `kimi`, `grok`, `gemini`) must fan out into parallel subagents to execute the following overnight gates before materializing the full 1,080-task set:

| Agent | Overnight Phase 2 Mission (With Parallel Subagent Fan-Out) | Hard Acceptance Gate |
|---|---|---|
| **1. `codex`** | **Eliminate `POLICY = {...}` toggles & build natural 3-module broken pipelines + indirect $t_0$ hazard breadcrumbs:**<br>• Rewrite `runtime_template.py` & `oracle_composer.py` so shipped workspace code is **idiomatic, single-path broken Python + SQL/dbt across 3 separate modules** (`ingest_feed.py` $\to$ `fold_ledger.py` / dbt model $\to$ `publish_handoff.py`) with **zero `POLICY` dicts or pre-written oracle `else:` branches**.<br>• For every future-night hazard `H01..H14`, stage an **indirect $t_0$ operational breadcrumb** (e.g., an older rotated log line in `var/logs/`, a vendor SFTP integration spec in `workspace/docs/`, or 1 historical partition artifact) so thorough agents can discover and defend against the hazard (`0.25 <= pass@1 <= 0.75`) instead of guessing blindly (`0.00`) or flipping a flag (`1.00`).<br>• Remove the 2-hazard cap so topologies `FT01..FT15` can compose `(2 t0 stressors + 2..3 future hazards)`. | `0` occurrences of `POLICY` or pre-written oracle branches in shipped `workspace/`; partial fix passes `C0` and fails `R2..R4`; `claude-opus-5-5-high` cannot solve by toggling booleans. |
| **2. `kimi`** | **Deepen Native Theme Coupling across all 12 Themes (`world/world/themes/`):**<br>• Connect the scheduled nightly pipeline in each of the 12 themes to the theme's **native domain tables, dbt models, and BI/ledger surfaces** (so `revenue_daily`, `settlement_clearing`, `saas_subscription_mrr`, `ad_attribution_events`, `supply_chain_inventory`, `healthcare_claims_edi`, `fintech_kyc_risk_limits`, `iot_telemetry_billing`, etc. require inspecting the theme's real dbt/SQL models and domain schemas rather than an identical `factory_events` table). | All 12 themes pass `verify_all_themes.py` (double-build determinism + 96h `scheduler.advance`) with domain-native table & receipt schemas. |
| **3. `grok`** | **Multi-Hop Epistemic & Governance Ambiguity + Indirect Hazard Clues (`tasks/factory/governance/`, `narrative/`):**<br>• Upgrade `act-authorized` (`Mode A`), `withhold-blocked` (`Mode B1..B3`), and `escalate-ambiguous` (`Mode C`) so **even `act-authorized` requires a 3-hop verification** across `issues.json`, `chat.json`, and `kb.json` (e.g., reconciling two superseding policy threads and calculating the exact restatement delta before acting), preventing `Mode A` from being a rubber-stamp `1.00`.<br>• Embed **indirect, non-spoiler technical clues** for the active `H01..H14` hazards inside realistic operational noise (runbook caveats, SFTP ops chat threads, vendor release notes) and verify with `discoverability_and_spoiler_linter.py`. | `36/36` bundles pass zero-spoiler + 100% discoverability lint, including indirect $t_0$ discoverability traces for active `H01..H14` hazards. |
| **4. `opus`** | **Unify `opus_grader_wt` (`a957d6e`) into Main-Tree `tasks/factory/grader/` & Add Granular Progress Checkpoints (`q0..q6`):**<br>• Reconcile and merge `/home/ubuntu/opus_grader_wt/tasks/factory/grader/` (`30/30` `GT009` rollout equivalence + `18` check kinds) with the main-tree `declarative_grade.py` / `baseline_runner.py` API so both existing `GT004`/`GT009` equivalence tests and all generated factory tasks run on one unified grader.<br>• Expand `progress_quantities` (`q0_t0_ingest`, `q1_t0_fold`, `q2_t0_handoff`, `q3_r1`, `q4_r2`, `q5_r3`, `q6_r4`) so `acted_unresolved` episodes produce high-resolution scalar variance (`Var(r) > 0.02`) even when binary `pass@1` is low. | `100%` of main-tree + worktree pytest suites pass (~260+ tests); `30/30` `GT009` rollouts match; `progress_quantities` yield $\ge 4$ distinct scalar values across rival baselines. |
| **5. `gemini` (`self`)** | **Stratified Hardness Certification Matrix Before 1,080-Task Materialization:**<br>• Re-materialize the **36 stratified pilot tasks** (`12 themes × 3 governance modes`) + **15 fault-topology representatives (`FT01..FT15`)** with sealed future payloads (`0` quarantines) and run `verify_all.py --workers 16` (`Stage 0`).<br>• Run **Stage-1 ($K=3$) and Stage-2 ($K=5$) live container calibration** (`claude-opus-5-5-high`) across stratified cells covering **all 12 themes, all 3 governance modes, and all 15 fault topologies**, tuning any cell that hits `3/3 (1.00)` or `0/3 (0.00)` with zero scalar variance.<br>• **Only after proving every cell is hard (`pass@1 <= 0.80`, `0` quarantines, non-zero reward variance)**, materialize the full **1,080 tasks** into `/home/ubuntu/alkera_factory_1017/tasks_1080/`. | Every tested theme, governance arm, and fault topology verified at `pass@1 <= 0.80` with `0` quarantines before bulk 1,080 generation. |

---

## 8. References

1. **Envs-FORGE:** *Frontier-Optimized Reward-Grounded Environment Synthesis for Agent RL* (Aug 2026) — [arXiv:2608.14312](https://arxiv.org/abs/2608.14312)
2. **AutoGym:** *Blueprint-First Generation of Verifiable Agent Gyms* (Sep 2026) — [arXiv:2609.22592](https://arxiv.org/abs/2609.22592)
3. **Explorer-Zero:** *Improving Self-Play for Long-Horizon Tasks with Decomposable Verifiable Rewards* (Oct 2026) — [arXiv:2610.19868](https://arxiv.org/abs/2610.19868)
4. **CodeMidas:** *Scaling Agentic Coding RL Environments from Code Itself* (Sep 2026) — [arXiv:2609.22068](https://arxiv.org/abs/2609.22068)
5. **SWE-at-Scale:** *Scalable Issue Synthesis from Historical Bug Fixes* (Oct 2026) — [arXiv:2610.21359](https://arxiv.org/abs/2610.21359)
6. **HarnessSQL:** *Harness-Native Training for SQL Agents in Realistic Database Environments* (Oct 2026) — [arXiv:2610.12274](https://arxiv.org/abs/2610.12274)
7. **HiL-Bench:** *Do Agents Know When to Ask for Help?* (Scale AI, Apr 2026) — [arXiv:2604.09408](https://arxiv.org/abs/2604.09408)
8. **DAPO:** *An Open-Source LLM Reinforcement Learning System at Scale* (2025) — [arXiv:2503.14476](https://arxiv.org/abs/2503.14476)
9. **Miller:** *Adding Error Bars to Evals* (Anthropic, 2024) — [arXiv:2411.00640](https://arxiv.org/abs/2411.00640)
