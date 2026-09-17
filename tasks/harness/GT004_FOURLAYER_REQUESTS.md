# GT004 four-layer retrofit — requests to the harness

Written 2026-09-16 by the GT004 four-layer build. **Nothing under
`tasks/harness/` was changed by that build**; everything it needed already
existed. This file records the one thing it could not do from inside the task
package, so that whoever owns the harness can decide it on evidence rather than
discovering it in a report.

## Request 1 — a rollout path that advances the clock, so G7 can ever decide

**Status: not blocking. GT004 ships with G7 `not_evaluable`, worth 0, in the
pass criterion of nothing, and every baseline scores exactly what it scored
before G7 existed (`tasks/GT004-doubling-window/tests/VALIDITY.md`).**

What already works, and was measured on this host:

- `world/world/scheduler.py` calls `world/world/ledger.py` at the three sites
  where a job's command actually ran, so an advance over a world whose jobs
  declare `publishes:` writes real receipts.
- `tasks/harness/episode.py:316-324` already stages `<world_root>/var/ledger`
  into the grading zone after teardown and before grading, and already
  substitutes `{ledger}` into `grade_argv`.
- `tasks/harness/ledger.py` reads the staged copy and refuses malformed
  evidence.
- GT004's `tests/ledger_check.py` stages a publishing world
  (`make_fixture_world.stage_world_root(..., ledger=True)`), advances four
  nights one 24h step at a time through `world.scheduler.advance`, and audits
  the 8 receipts that result: the oracle's are clean, the un-repaired world's
  carry 6 offences, and **the two states are separated by the ledger alone**.

What does not: **no rollout path moves the clock.** `trigger()` writes no
receipts by contract (`world/LEDGER_CONTRACT.md`: a manual trigger runs inside
the episode container, where `var/ledger` is an empty read-only tmpfs), and
GT004's G3 probe advances `vnow` by editing `var/sched/state.json` on a *copy*
of the world rather than by calling `advance`. So a real episode ends with an
empty ledger and G7 correctly says so.

The request, if and when the framework wants layer 4 to carry weight in a
rollout: a declared post-episode advance — `[harness] advance_nights`, or the
GT008 shape where the grader advances internally — run on the world root
**before** `ledgerlib.stage()`. GT004 would then need
`stage_world_root(..., ledger=True)` in `tests/build_world.py`, and that is the
cost, stated plainly:

> **It moves the world fingerprint.** `publishes:` lives in
> `var/world/live.json` and the schedule an advance needs lives in
> `var/sched/state.json`, and both are sections of
> `world.fingerprint.semantic()`. Measured, both ways:
> `semantic_hash 8e7919ff84f23e63040ef06b144ffae07824e9c7b8c33fa23f708472dbc0c474`
> with `ledger=False` (identical before and after this build) and
> `b48d9872449b10682923f9a11767f4193526466f4415c4cfa9d4b201fa36c21d` with
> `ledger=True`. Every recorded GT004 rollout ran in the first world. Switching
> the default would invalidate the comparison the 46 measured episodes rest on,
> which is why this build did not do it.

So the decision is not "wire it up", it is "start a new world hash for GT004,
and accept that numbers either side of it are not comparable". That is a
task-design call, not a builder's.

## Not a request — recorded so it is not rediscovered

`probes/collateral.py`'s blast-radius clause (nothing the task asked to change
actually changed ⇒ no offences) combined with `grade.py`'s `idle_agent_policy`
(that case is never converted into a penalty) means an agent that touches ONLY a
scored-but-unrelated workspace path, and nothing in the warehouse, is not
charged for it. GT004's new `disable_the_rule` baseline falls in exactly that
gap and scores 4.0 of 33 anyway. It is recorded as GT004's weakest finding in
its `tests/VALIDITY.md`; it needs no harness change, and closing it would
require re-deciding a policy that exists to keep the wrecker and the idler
apart.
