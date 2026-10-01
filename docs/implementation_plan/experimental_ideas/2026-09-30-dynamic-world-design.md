# Dynamic worlds: a data-engineering benchmark as a controlled stochastic system

Status: design, for review. Branch `experimental_ideas`. Target: NeurIPS Datasets & Benchmarks.
Scope of this spec: ideas A + B + C from the 2026-09-30 brainstorm (controller view, Hawkes
shocks, counterfactual twins). Fault algebra (D), observability (E) and compositional IRT (F)
are later sections of the paper and are out of scope here.

## 1. The research question

Static agent benchmarks (SWE-bench, Terminal-Bench, Spider 2.0, ELT-Bench, our GT001–GT009)
grade the **final state** of a **frozen world**. Two consequences we measured ourselves:

- They saturate. Six of nine Alkera tasks sit at 8/8 for Opus; GT009 finishes in ~4 minutes an
  episode. METR's result predicts this: success collapses only once tasks reach hours.
- Stacking static faults does not make tasks harder. `tasks/harness/coupling.py` records the
  GT006c/GT006d experiment: an easy defect stacked on another moved pass@1 by zero (28/28 ×5).

Real data engineering is neither frozen nor final-state. Pipelines keep running while you
repair them; a symptom patch holds until the next retry, a root-cause fix holds forever. **We
ask: can a benchmark treat the world as a controlled stochastic dynamical system, so that
difficulty is a continuous, theory-grounded dial, every fault is accounted for exactly, and the
agent is scored as a controller against exact counterfactuals?**

## 2. Formal model

**State and clock.** World state $W_t$ (warehouse, dbt project, scheduler state, landing zone,
workflow plane). Virtual time $t \in [0, T]$, advanced only by the world clock (§4.1).

**Latent defects** $D = \{d_1, \dots, d_n\}$: code or configuration defects planted at $t=0$,
ported from the stressor registry (`tasks/stressors/registry.yaml`) across worlds. A defect does
nothing on its own; it turns shocks into damage.

**Shocks** $\xi$: operational events drawn from the existing fault catalogue
(`world/world/faults.py`: `job_retry`, `late_arrival`, `duplicate_arrival`, `job_overrun`,
`partial_write`, ...), arriving as a marked, discrete-time **multivariate Hawkes process** over
the scheduler's occurrence lattice:

$$\lambda_k(o) = \mu_k + \sum_{j} \sum_{o' < o,\ o' \in \xi_j} \alpha_{kj}\, e^{-\beta\,(t(o) - t(o'))},
\qquad P(\text{shock } k \text{ at occurrence } o) = 1 - e^{-\lambda_k(o)\,\Delta}.$$

The **branching ratio** $\rho = \operatorname{sr}(\alpha)/\beta$ (spectral radius) is the
difficulty dial: $\rho = 0$ isolated shocks, $\rho \to 1$ critical cascades. The excitation
structure $A$ is derived, not tuned: $A_{kj}$ = overlap of shock kinds $k, j$ on the registry's
declared state (shared jobs, tables, checkpoints, windows); $\alpha = \rho^\star \beta\, A / \operatorname{sr}(A)$
sets any target $\rho^\star < 1$.

**Dynamics.** $W_{t+1} = \Phi(W_t, a_t, \xi_t)$, where $\Phi$ is the real scheduler executing
real dbt / loaders (`world.scheduler.advance`), $a_t$ the agent's commands, $\xi_t$ the shocks.
Damage arises from execution, never from assertion: a retry against a non-idempotent model
duplicates rows because dbt really ran twice.

**Damage.** One measure per contract in the registry's closed set
$c \in \{$exactly_once, liveness, version_precedence, cutoff_integrity, consumer_consistency,
auditability$\}$, computed exactly from $W_t$, normalized to $[0,1]$:

| contract | $V_c(W_t)$ (pilot definitions) |
|---|---|
| exactly_once | duplicated business keys / total keys in the governed fact |
| liveness | partitions due by $t$ and not landed / partitions due |
| version_precedence | keys whose served version ≠ max delivered version / keys |
| cutoff_integrity | rows in a closed period that differ from the published snapshot / rows in it |
| consumer_consistency | consumer-facing rows ≠ reference derivation of source / rows |
| auditability | ledger / scheduler-history / published-record rows altered / rows |

**Objective.** $J(\pi) = \int_0^{T} w^\top V(W_t)\,dt + \gamma\, w^\top V(W_T)$, over a horizon
that continues **after the agent stops** (the world keeps running to $T$; a symptom patch pays
here, a root fix does not).

**Score.** With the no-op twin and the oracle twin run on the **same shock realisation**:

$$S = \frac{J_{\text{noop}} - J_{\text{agent}}}{J_{\text{noop}} - J_{\text{oracle}}} \in (-\infty, 1].$$

$S = 1$ matches the oracle, $0$ is doing nothing, $<0$ made it worse. Continuous, so it does
not saturate the way pass@1 does.

## 3. Why this is more than "a harder task": the falsifiable claims

- **H1 (the dial works).** For every fixed policy, $\mathbb{E}[S]$ is monotone non-increasing in
  $\rho^\star$, and Opus's $\mathbb{E}[S]$ crosses 0.5 inside $\rho^\star \in [0, 0.9]$.
- **H2 (dynamics separate root cause from symptom).** Let $\pi_\text{root}$ fix defects and
  $\pi_\text{sym}$ repair their damage without fixing them. The gap
  $S(\pi_\text{root}) - S(\pi_\text{sym})$ is $\approx 0$ at $\rho^\star = 0, T = t_\text{end}$
  (the static benchmark) and grows with $\rho^\star$ and with the post-episode horizon. The
  first-order prediction: gap $\approx \sum_d \bar\lambda_d\, p_d\, \delta_d\,(T - t_\text{fix})$,
  whose parameters are fit from twins and then tested on held-out seeds.
- **H3 (paired twins cut variance).** Because the twins share the shock realisation (common
  random numbers), $\operatorname{Var}(S)$ is at least 2× smaller than the unpaired estimator (the same
  formula with $J_\text{noop}, J_\text{oracle}$ averaged over other seeds); the K needed for a
  given interval width drops by the same factor. The ratio is reported as measured.
- **H4 (every unit of damage is owned).** Damage is exactly attributable to faults: the Shapley
  decomposition of $J_\text{noop}$ over the fault set ($2^{|D|+|\text{shock kinds}|}$ twins,
  cheap because twins run no model) closes to the total within tolerance. Unattributed damage
  is, by definition, a world bug, and fails the task's validity gate.

H4 is the answer to "it will be hard to clear up what happened": the benchmark proves, per
world, that it can say which fault caused which damage.

## 4. Components

Five units, each with one job and a file-level interface, so they can be built in parallel.

### 4.1 World clock driver (`world/world/clock.py`, hook in `gateway/docker/subject/rexec.py`)

- The clock ticks on **agent command boundaries**, not wall time: before executing command
  number $n$, if $n \bmod m = 0$ the driver runs `advance(root, h)` synchronously, then the
  command. Deterministic given the command sequence; no race with the agent (the world never
  writes while an agent command runs, preserving faults.py rule 3).
- After the agent exits, the harness advances the world to $T$ (the post-episode horizon).
- Writes `tick_schedule.json`: `[{tick, after_command, vnow_before, vnow_after}]`. The twins
  replay this schedule, so they see the same virtual times.
- Counts **model tool calls** (commands through rexec), not gateway audit rows: that is the
  budget miscount we measured on GT003 r3 (33 calls read as 1,320).

### 4.2 Shock generator (`world/world/hawkes.py`)

- Discrete-time multivariate Hawkes on the occurrence lattice (jobs × schedule, arrivals × day).
- Sampling by thinning with **keyed draws**, the faults.py convention:
  `draw(fault_seed, "hawkes/<kind>", occurrence_key)`. Same seed ⇒ same realisation.
- **Compiles to the existing catalogue**: output is `live.faults` entries with `only:` lists of
  exact occurrences, so `advance` executes shocks through code that already exists and is
  already tested. No new execution path.
- Inputs: `shock_spec.yaml` (kinds, targets, $\mu$, $\beta$, $\rho^\star$); registry for $A$.
  Output: `shock_schedule.json` `[{kind, target, occurrence, vts, parent|null}]`. `parent` records
  the excitation ancestry (sampled via the branching / cluster representation), so every
  cascade is visible as a tree.

### 4.3 Damage monitor (`world/world/damage.py`)

- `sample(root, spec) -> {vnow, V: {contract: float}, detail}`: pure SQL + file reads over the
  world, one function per contract, parametrised by a world's `damage.yaml` (governed fact,
  business key, cutoff snapshot, due schedule, reference derivation).
- Called at every tick (4.1) and at $T$. Appends to `damage.jsonl`. The integral $J$ is the
  Riemann sum over the tick schedule; twins sample on the same schedule, so sums are paired.

### 4.4 Pilot world DW-1 (`tasks/DW001-live-revenue/`)

- Base: the `revenue_daily` theme (live faults declared, used by the acceptance battery).
- Latent defects, ported from three worlds by re-implementing each in DW-1's dbt project, with
  the registry id recorded in the ledger (one per contract family):
  1. non-idempotent append on the revenue fact (GT001/GT004 family; exactly_once) — turns
     `job_retry` / `duplicate_arrival` into duplicates;
  2. checkpoint advanced past an unread partition (GT009; liveness, cutoff_integrity) — turns
     `late_arrival` into a silently skipped day;
  3. NULL-unsafe `NOT IN` quality gate (GT006; consumer_consistency) — drops rows continuously,
     no shock needed (a control: damage without dynamics).
- Oracle policy (fix all three at $t=0$), and scripted policies for the ladder (§6).
- Prompt: an on-call handover ("revenue looks off; keep the pipeline healthy this week"). The
  agent sees a world that keeps running.

### 4.5 Twin runner and scoring (`tasks/harness/twins.py`, report integration)

- After an episode: rebuild the world from its seed, apply the same `shock_schedule.json` and
  `tick_schedule.json`, and run (a) no agent, (b) the oracle policy, (c) for H4, every subset of
  faults (only for the attribution study, not per episode).
- Twins run host-side, no model, no containers beyond what `advance` needs.
- Outputs per episode: $J_\text{agent}, J_\text{noop}, J_\text{oracle}, S$; per fault: onset,
  time-to-detect (first agent command touching the fault's footprint after onset, from the
  audit log), time-to-mitigate (first time the owning $V_c$ returns to 0 and stays), and whether
  it was fixed (no recurrence under later shocks) or patched (recurrence).
- `report.md` gains a damage-curve section: $V(t)$ for agent / noop / oracle, and the fault
  ledger aligned under it.

## 5. Monitorability: what we can always see

- **Fault ledger** (author side, unreachable by the agent): latent defects at $t=0$ with their
  registry ids; every shock with occurrence, virtual time, and Hawkes parent; `live.json`'s
  existing `fault_log` extended with `parent`.
- **Damage ledger**: `damage.jsonl`, one row per tick.
- **Tick ledger**: `tick_schedule.json`.
- All three live outside anything the agent can read (a root-owned `0700` directory in the
  episode, copied out by the harness). The containment probe gains a check that the agent user
  cannot read them. The agent meets only symptoms: failed runs, gaps, wrong numbers.
- **Closure (H4) as a validity gate**: a world ships only if its Shapley attribution closes.

## 6. Validation before any model token

A scripted-policy ladder, run at $\rho^\star \in \{0, 0.3, 0.6, 0.9\}$ over ≥ 10 seeds each:

| policy | does | required |
|---|---|---|
| oracle | fixes all defects at t=0 | $S = 1$ by construction |
| root_late | fixes all defects at $T/2$ | $0 < S < 1$ |
| symptom | deletes duplicates / backfills gaps each tick, never fixes code | $S <$ root_late, gap grows with $\rho^\star$ (H2) |
| partial | fixes one defect | between no-op and oracle |
| noop | nothing | $S = 0$ |
| overreach | disables the loads to stop duplicates | $S < 0$ (liveness damage) |
| vandal | truncates the fact | $S \ll 0$ |

Plus: determinism (two runs, same seed ⇒ identical ledgers and $J$), `advance(1)×n ≡ advance(n)`
preserved with shocks, and the empirical branching ratio of the sampled schedules within
tolerance of $\rho^\star$.

## 7. Experiments for the paper (after validation)

1. The dial: Opus, K=8, at four $\rho^\star$ values: $S$ vs $\rho^\star$ (H1).
2. Root vs symptom: classify each episode's handling of each defect (fixed / patched / missed)
   from recurrence; show the static-world blind spot (H2).
3. Variance: paired vs unpaired estimators from the same episodes (H3).
4. Attribution closure on DW-1 and one more world (H4).
5. If budget allows, a second model family for the dial curve.

## 8. Non-goals (v1)

- Decision arms / ownership boundaries (they fold in later as a `cutoff_integrity` or
  auditability damage term).
- LLM-simulated stakeholders; any LLM in the score.
- Closed-loop (state-dependent) shock intensity. v1 shocks are exogenous so twins share them
  exactly; the causal link to the world runs through the defects, which the agent can fix.
- Fault algebra (D), observability (E), IRT (F).

## 9. Risks

- **Tick cost.** `advance` runs real dbt; a tick every $m$ commands must stay under a few
  seconds or episodes crawl. Mitigation: measure on DW-1 first; pick $m$ and $h$ from it.
- **Agent writes vs clock.** The tick runs between commands, but a command can start a
  background process that writes during a tick. Mitigation: the tick waits for the episode's
  process group to be idle, or records the overlap in the ledger as a known race.
- **Damage-measure validity.** Each $V_c$ must be exact on the oracle (0) and on planted damage
  (the known amount). The registry's falsifier pattern (`fixture_contract.falsify`) is reused.
- **MCP driver.** v1 ticks only under the native driver (rexec is the choke point).
