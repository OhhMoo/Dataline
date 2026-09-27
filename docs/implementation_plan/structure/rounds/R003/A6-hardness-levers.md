# A6 — why GT001 was easy, and the hardness levers that respect the northstar

**2026-09-14 · Track A.** Measured basis: `A-rollout-results.md` (claude-k5 Pass@1 1.0,
kimi 4/6 scored pass on GT001). Constraint from the user: difficulty must come from
what a *real* data engineer's working computer is like — not from puzzle construction.

## Why GT001 fell at first attempt

1. **The defect is the only anomaly in the world.** Everything else is clean, so any
   anomaly scan finds the injected fault. Search is trivial when signal-to-noise is 1:0.
2. **The world is static.** All evidence exists at t0; nothing moves while the agent
   thinks. Real incidents unfold — rows land, jobs run, people commit — mid-diagnosis.
3. **The pattern is overrepresented in training data.** "Duplicates from a retried
   backfill" is a textbook incident. Both models pattern-matched it in <80 calls.
4. **Outcome grading only on data-state** invites brute force (recompute-and-overwrite),
   and there was no live clock to make that unsafe.

GT004–GT007 (Track C, uncommitted WIP) already attack 2–4: stacked defects, clock
advance at grading, absent-rows direction, folk-theory-vs-evidence, live-fire gates.
**GT006 and GT007 are the only fully validity-measured tasks; GT004/GT005 are still
placeholder-validated. None has faced a model.** Measure before building more.

## Levers, ranked by (northstar fidelity × expected discrimination × cost)

**L1 — Distractor base rate (highest value, cheap).** Seed every world with benign
anomalies and red herrings: already-remediated duplicates *with paper trail*, a naming
violation that doesn't touch data, an intentional-but-odd dashboard filter, a second
real-but-out-of-scope incident. Real DE machines are messy; the mess is the difficulty.
Grading already has the instrument: no-collateral penalty checks (GT004 G5, GT006 G1b/G3)
punish "fixing" distractors. This creates needle-in-haystack pressure without 500 tables.

**L2 — Live world during the episode (not just at grading).** We now have `vclock.py`
and `airflow_bridge.py` (untracked in world/). Drive a mid-episode event schedule keyed
to virtual time or action count: late arrivals land, a colleague-bot commits a
conflicting change, an unrelated alert fires. This is the DA-Comp finding — models are
worst at concurrent/interleaved data+event logic — and GT004's G2/G3 prove we can grade
"repair survives liveness." Deterministic if the event script is seeded.

**L3 — Interactive requester (deterministic persona).** Some fact exists only in a
human's head ("we ran a manual backfill on the 3rd"). A scripted state machine behind
an `ask_requester`-style surface answers with partial, sometimes contradictory info.
Real DE work is exactly this; grading stays deterministic. Northstar-faithful: real
computers have Slack/email with humans behind them (Slack transcript fake is already
on the A5 residual list).

**L4 — Scale the haystack only together with L1.** The builder is seed-driven, so 200
extra tables/dashboards are cheap — but models grep well. Scale alone costs tokens,
not capability. Use scale to hide the *distractors*, not the defect.

**L5 — Enumerate and gate the cheap paths, systematically.** GT006's adversary ("just
disable the rule") is the template: for every task, write down the top-3 brute-force
repairs and make the rubric punish each (liveness on the agent's own rule, idempotency
under clock advance, downstream re-resolution). This converts outcome grading into
process grading, which is where models actually fail.

**L6 — Epistemic honesty as a scored axis.** D6 (honest secondary-cause reporting) was
the *only* check with pass rate 0.0 on claude-k5 — and it's excluded from
`pass_requires_checks`. Rewarding calibrated uncertainty and penalizing overclaiming is
both realistic (incident reports) and our best measured discriminator. Decision needed:
include an honesty check in pass requirements on new tasks.

**Anti-pattern (do not do):** artificial obscurity — weird renames, hidden evidence,
undocumented hops. A real DE computer is not an escape room; labs will (rightly) call
it contrived.

## Recommended sequence

1. **Measure what exists first**: opus + kimi on GT006 and GT007 (validity-measured),
   then GT004 once its builder/fixtures land. Don't build hardness on an unmeasured base.
2. **Build L1 into the theme layer** (distractor seeding is a `gen.py` hook — zero
   builder changes) and L2 on top of `airflow_bridge` (event script in theme.yaml).
3. **Prototype L3 on one task** (GT007's folk-theory premise is the natural host).
4. Re-run the falsification loop: target Pass@1 < 0.2 on at least one task for both
   model families before claiming difficulty to a lab.
