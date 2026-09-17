# STATUS — 2026-09-17

The shape of the project at the end of the 2026-09-15..17 build, in numbers and in design.
Every number below was produced by a command on this host; "designed" marks what was not.
Full record: `agent.md` (§8.10–8.11 measurements, §9 contracts, §11 tracks, §12 native),
per-folder `OVERVIEW.md`, and the contract docs each component ships.

## 1. What exists

**A data engineer's working computer, reproducible, audited, contained.**

| component | measured state |
|---|---|
| worlds | 4 themes + per-task fixture worlds; semantic fingerprint; host build == in-container build (`85ce4cbb…`, unchanged through the dual-warehouse, ledger and native work); 7-gate acceptance battery green, recorded to `world/evidence/acceptance.json` |
| agent surfaces | **two transports over one audited chokepoint**: 63 MCP tools, and native mode (bash only, no tool list: `duckdb`/`dbt`/`git`/`aws`, the real `jira`/`slack_sdk`/`atlassian` clients against 9 vendor-shaped fake services + a served scheduler plane). Same 23-key action-log row from both; parity of target sets measured; row equivalence 16/16 (human plane) and 19/19 (ops plane), obtained from the same `Observation` the MCP path sets |
| containment | P0–P7 measured on real Docker stacks (grader unreachable, no egress, ledger read-only, only the fake services resolvable, episode sees only the warehouse file) — **but see §4.1** |
| grading | trajectory-blind, deterministic, per-task `grade.py` + probes; a reusable **contracts library** (7 invariants, 98 tests, 18/18 equivalence with GT004's grader); a **protected publication ledger** (receipts written only by clock-advanced jobs; GT008/GT009 audit real receipts); Pass@1 with a stated denominator, Pass^k, quarantine, a live-surface gate that withholds the rate |
| tests | gateway 321 · world 166 (+81 subtests) · harness 147 + 122 + 37 + 88 + 22 + 4 · contracts 98 · variants 83 · scanner `scripts/track_status.py` 0 blockers, A1–A4a / B1–B4 done with executed evidence |
| tasks | GT001–GT009, all TASK VALID by their own shortcut batteries; GT004 carries 8 context arms and GT006 4 as `--variant`; GT009 is the first task built around a decision rather than a defect |

## 2. What is measured (Opus 5, K=5, `measurement_valid` unless stated)

| task | MCP | native | reading |
|---|---|---|---|
| GT001, GT005, GT006, GT007, GT008 | **1.0** | GT001: withheld (host artifacts) / container run invalid (§4.1) | ceilings. GT008 is a ceiling *despite* passing the four-layer gate |
| GT002 | 0.8 | — | one failure in five, never replicated |
| GT003 | **0.0** (0.2 if D5 credits an appended tick — ruling pending) | — | wall: 3/5 *regenerated* an append-only history; 5/5 over-claimed causes |
| GT004, six context arms | **0.0 / 0.2 / 0.2 / 0.0 / 0.0 / n.a.** (2 repairs in 25 no-ticket episodes, 0 in 26 with the ticket) | **0.25** on host, rewrite performed 2/5 (n=5, host caveat) | wall: the model names the historical rewrite and declines it; no context lever changes that; removing the tool list may |
| GT006 2x2 (stacked ± ticket) | 1.0 / 1.0 | — | no interaction |
| GT009 | 8/20 ×5, none pass — **not valid** (regraded `unobserved`) | — | wall-shaped; needs one clean K=5 |

Precision: K=5 gives ±0.18 at p=0.5; ~64 rollouts pin ±0.05. No number here is better than a screen.

## 3. What we learned about difficulty (the design-level state)

Falsified by measurement, each with K=5 on a built task: **prompt wording** (0 movement over 5
variants, replicated on GT008), **concealment**, **conjunction** of independent defects,
**interference** (an authoritative false claim: 5/5 overruled), **scope closure** (2x2, no
interaction), and **the four-layer gate as a difficulty certificate** (GT008: every shortcut
fails, oracle passes, model 1.0). The gate certifies shortcut-resistance; it does not
manufacture difficulty.

What has measured hard, twice, from two directions: **what the model does with history.**
GT004 — it will not rewrite 94,097 pre-existing rows even when every excuse is removed
(12/24 name the fix). GT003 — it *does* rewrite an append-only record it was told to keep.
Neither is search; both are decisions about irreversibility. One caveat is live and
decides how to read GT004 (design note Q1): declining an unauthorised rewrite under a
closed ticket is arguably correct practice, so the wall may be measuring norm-compliance,
not capability. The arms that separate those — `authorized` and `escalation-credited`,
graded through the `authorization` contract so **both directions cost** — exist, are valid,
and are unmeasured. Kimi as a second family is wired (`--driver kimi`) and unmeasured on
GT003/GT004.

The design direction that follows (design note Q2): audience-irreversibility with
asymmetric regret — publish a wrong number (a receipt) vs withhold and breach (also a
receipt) — graded through the ledger at every checkpoint. GT009 is that class, built and
valid (withholding 7.0 vs publishing outside protocol 8.0, one point apart; the clocked
coupling filter accepts 1 of its 6 pairs). The clock changes coupling verdicts (GT004's
`late_arrival` pairs flip once advanced), which means the filter must run under a schedule
to certify event-type stressors.

Native mode is the other live hypothesis: the 63-tool list was a map of the world, and the
first no-map run of GT004 moved the only wall we had (0.0 → 0.25). It has to be replicated in
the container topology before it is a number.

## 4. Open defects and gaps, ranked by what they block

1. **Container topology for native mode is not real for a real model.** The scripted
   subject executes through the docker-exec wrapper; the actual `claude` CLI ran its Bash
   tool on the host (measured: `ls /` returned the Git Bash root, then the host rollout dir)
   while the record said `filesystem_isolated`. Both container campaigns were stopped. Fix
   needs a one-turn model smoke as acceptance (`hostname; ls /` inside), and `rollout.py`
   must refuse to record the guarantee without a proof of container execution. **Blocks
   every valid native number.**
2. **GT009 has no valid measurement**: the grader's `id` key crashed five records; the
   regrade recovered scores (8/20 ×5) but the pre-episode inventory was not saved, so the
   episodes are `unobserved`. `--regrade` now exists; one clean K=5 is needed.
3. **GT004 four-layer fold-in is mid-flight**: `[composition]`, contracts-backed checks,
   `ledger_check.py`, `coupling_check.py` are on disk; the builder's last run reported all 8
   arms TASK VALID; `tests/VALIDITY.md`'s equivalence table is incomplete. Re-run
   `tests/run_baselines.py` before committing.
4. **GT003 D5 ruling** (append vs regenerate) changes its number from 0.0 to 0.2; needs a
   validity re-run, not a hand edit.
5. Known weaknesses recorded in their docs: `cutoff_integrity` has seen real receipts but no
   real offence (receipts carry execution day, not period); the world-diff allowlist is
   hand-kept; the working-tree rule in native mode is coarse (one shell command licenses
   every workspace change); `/health` item counts are self-reported; the episode image bakes
   `gateway/src` with no per-rollout staleness check; `alarms_ack`/`deploy_status` exist as
   operations but are not registered MCP tools (held for comparability).

## 5. Next runs, in order, when usage allows

1. Fix §4.1, then Phase 4: GT001/GT004/GT006/GT008 `--driver native --topology container`, K=5.
2. GT004 `--variant default` and `--variant authorized` on `--driver kimi`; GT003 on Kimi.
3. GT009 one clean K=5 (MCP), then GT009 on Kimi.
4. GT004 `escalation-credited` on Opus and Kimi.
5. The GT004 difficulty analysis (51 episodes on disk across seven arms and two drivers).

## 6. Tree state

62 uncommitted paths (harness, GT004, GT009, GT003, contracts, gateway/docker, gateway/src,
agent.md), all verified by their builders before work stopped; nothing committed by the
assistant. No rollouts, subjects, services or monitors running; only the host `minio`
container is up.
