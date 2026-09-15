# Three tracks — build and validate

**Created 2026-09-14. Rewritten 2026-09-14 after R003.** The first version described a
system that no longer exists; treat anything you remember from it as stale.

Three agents build in parallel: **Kimi (A)**, **Astra (B)**, **Claude (C)**. Each owns the
code it already built. **Read §0–§2 before touching your own track**, then read only your own
section.

Background: [`agent.md`](agent.md) — §9 is the ground rules and frozen contracts, §10 the
repository architecture. Findings: `implementation_structure/rounds/R002/` and `R003/`.

---

## 0. Where the project actually is

### What changed in R003

R001 and R002 were research. **R003 built the system and put real models in front of it.**

| | before R003 | now |
|---|---|---|
| tasks built | 1 (GT001) | **3** — GT001, GT002, GT003, all TASK VALID |
| world shapes | 1 | **4** themes: `revenue_daily`, `sftp_vendor`, `mainframe_batch`, `excel_close` |
| gateway tools | 17 | **48** — +metrics, alarms, deploy, logs, S3, ingest |
| gateway tests | 12 | **78 passed, 9 skipped** |
| world tests | 7 | **37** |
| the scheduler | appended `"status": "success"` as a literal | **executes real dbt, real loaders, real SQL**; status derives from what happened |
| episode topology | host subprocess, `/grading` one `..` away | **runs in the container**; `cat /grading/…` → `No such file or directory` |
| the agent's terminal | none | **a real shell**, shipped only after containment was enforced |
| frontier rollouts | zero | **measured, two model families** |

### The measurement, and it is not what we hoped

GT001, K=5, opus, 0 quarantined: **Pass@1 = 1.0.** Scores 18, 17, 17, 17, 17 out of 19.
Kimi ran K2 independently and got the same shape.

Per-check, both families: **D1, D2, D3, D5a, D5b at 100%.** The only discriminating check is
**D6** — secondary causes minus the false-cause penalty — at **0%**.

`agent.md` §8.3 rests on *"five sub-results at 70% → 17% all-green."* Measured, they are not
at 70%. **Conjunction multiplies ones.** Difficulty-by-conjunction does not survive a frontier
model when the conjuncts are individually easy.

The one axis that discriminated grades **judgment under ambiguity**, not state repair. That is
the same split Polymath reports: *"the ability to plan over long horizons and coordinate across
tools, rather than code generation alone, is a key differentiator."*

This outcome was planned for — *"This phase falsifies the thesis cheaply — by design. A failing
prototype is a feature, not a bug."* **Task difficulty is deliberately deferred.** The current
priority is the system, not the score.

### Reference point: Polymath's Horizon-SWE

Their best model passes **25.5%**. Their environment is a running app with live traffic, 73
MCP tools, and grading split **feature correctness 60% / deployment & DevOps 30% /
engineering quality 10%**, with a pass-fail and a partial-credit framework over one rubric.

Where we still differ: **no running application serving traffic**, no issue tracker or comms,
no engineering-quality axis wired, and our world evolves but cannot yet *misbehave* on its own.

---

## 1. Ground rules — all tracks

Each was learned by having a red-team agent destroy an earlier design. Full list, with failure
IDs, in `agent.md` §9.1.

1. **The grader does not exist during the episode.** Now enforced by container topology, not
   by trust.
2. **The agent sees only `agent_visible_brief`.** All 41 archetypes now carry one.
3. **No network egress from the episode zone** — proven per-service from inside each network
   namespace, not inferred from `internal: true`.
4. **Official score is deterministic for v1.** The judge is shadow-mode; process signals are
   report-only.
5. **Destructive operations are flagged, not blocked.** Whether the agent gates itself is the
   measurement.
6. **Adversarial baselines are CI.**
7. **Never fabricate** — no invented URL, quote, image tag, version, metric or run status. Mark
   what you *executed* apart from what you *read*.
8. **Measure, don't extrapolate.** R002's best correction came from running `dbt parse` instead
   of scaling a number from a bug report; the estimate was wrong by 4–6×.
9. **Report your own weakest output.** Every R003 agent did, and those sections found more
   defects than the reviews did.

### The rule R003 paid the most to learn

**A check that never ran against the thing it ships against proves nothing.** GT001 printed
TASK VALID for a round while its oracle aborted on its first statement — because the baselines
staged the warehouse under a *convenient* filename instead of the contract one. Two more
instances of the same defect surfaced the moment that was fixed.

Corollaries, all of which cost real time this round:
- A registered tool with no test is indistinguishable from a dead one.
- A metric that does not read real state is a lie the grader may believe.
- A skipped test that names why is honest; a passing test that never exercised the surface is not.
- `s3mock` ignored `initialBuckets` on its pinned digest, so the S3 half of the acceptance
  script **had never once passed** while looking green.

---

## 2. Frozen contracts

Full text in `agent.md` §9.2. Do not change these without telling the other tracks.

- **§9.2.1 world layout** — `/workspace`, `/var/warehouse/warehouse.duckdb`, `/var/sched`,
  `/var/bi/dashboards`, and now `/var/obs/`. `/grading` is never mounted in-episode.
- **§9.2.2 action log** — one row per tool call, byte-identical from MCP and CLI, and now
  across warehouse backends. `op_class` lives in the log, never the response.
- **§9.2.3 task package**, **§9.2.4 `grade.py`**, **§9.2.5 the final artifact**.
- **`gateway/CONNECTOR_CONTRACT.md`** — the warehouse connector interface. `sql.py` carries
  zero DuckDB couplings; a world declares `warehouse_backend`.
- **`gateway/OBSERVABILITY_CONTRACT.md`** and **`OBS_STORE_CONTRACT.md`** — the metrics/alarms
  surface and the append-only sample store.

**Never open the warehouse file directly.** DuckDB names the catalog after the file stem, so
`warehouse.duckdb` holding a schema named `warehouse` makes every two-part reference ambiguous:

```python
con = duckdb.connect(":memory:", config={"threads": 1})
con.execute("ATTACH '<path>' AS wh (READ_ONLY)")
con.execute("USE wh.warehouse")
```

---

## 3. Track A — Kimi — Worlds

**Done:** theme-lifted builder (the shape was hardcoded in `build.py`); four themes; the
executing scheduler; K2 rollouts on GT001.

**Open:**
- **Faults** — in flight. The world evolves but cannot misbehave: no late/missing/duplicate
  partition, no timeout, no retry, no overlapping jobs, no read-during-write, no partial write.
  Those are the pathologies the corpus is made of.
- **Cross-host determinism** — `acceptance.sh` gate 1 still builds twice on one host.
- Themes 5+ from `rounds/R002/T3-environment-themes.md`; 19 of 30 have no archetype.

## 4. Track B — Astra — Gateway and stack

**Done:** warehouse connector (DuckDB + Postgres, parametrised test ids per backend); 48 tools;
multi-container compose with containment measured per service; fakesnow socket with TLS trust;
docs/BI/S3 surfaces tested.

**Open:**
- **Issue tracker, comms, knowledge base** — in flight. Horizon-SWE seeds all three.
- Postgres is proven for four SQL statements; `render_dashboard`, `BIStore.refresh`, watermark
  queries and dbt are DuckDB-only.
- `core.py:23` guards `from world.fingerprint import semantic_hash` — a Track A rename breaks
  Track B at runtime and **neither suite goes red**.
- `bi_list` returns `invalid_arguments` when a stray `.yml` sits beside the dashboards — the
  agent's args were `{}`. In the classifier that misattributes as `reasoning`.

## 5. Track C — Claude — Tasks, harness, grading

**Done:** GT001/2/3 TASK VALID; the rollout harness with enforced blindness, failure
classification and quarantine; container episodes plus the terminal; 41 briefs; gates G0–G8.

**Open:**
- **GT001 re-measured with the terminal** — in flight, and the most important open number. Every
  Pass@1 we quote was taken under `tool_surface_revoked`, which no longer describes how
  episodes run.
- **Observability as a record, not a recomputation** — in flight.
- **Engineering-quality axis.** Process signals (doom-loop, thrash, ungated destructive,
  time-to-first-write) exist and are report-only. Horizon-SWE weights this 10% and penalises
  *"unproductive loops in the agent's trajectory"* — which is exactly what we already compute.
- **PF / PC split.** They report pass-fail and partial-credit over one rubric; we have the same
  idea in §8.3 and have not named the two frameworks.
- **G7: 89 failure records, ~87 with no execution record.** The gate overstates — ≥16 targets
  carry a `repair_note` saying otherwise — but the backlog is real.

---

## 6. Verify

```bash
cd gateway  && .venv/Scripts/python -m pytest tests            # 78 passed, 9 skipped
cd world    && PYTHONPATH=. ../gateway/.venv/Scripts/python -m pytest tests   # 37
cd tasks/GT001-aov-halving/tests && python run_baselines.py    # TASK VALID
python tasks/harness/selftest.py                               # 51
python implementation_structure/gates/run_gates.py             # G0-G8
```

G7 is red on purpose: it lists every red-team ruling nobody has executed. Do not silence it.

---

## 7. What is still not true

- **Nothing has been promoted to `state/verified/`.** Zero of 41. Nine are `redesign_required`.
- **6 of the original 162 sources are a practitioner describing their own incident** — 3.7%,
  and that is the thing this project sells. *"Roughly half needs redoing, and it is the half
  that matters."*
- **Reddit is unreachable** from this environment, confirmed by five agents independently.
- **A063's stated mechanism does not close** — building GT002 showed it assumed a consequence
  its own premise forbids. Designs fail on contact with implementation, which is an argument
  for building sooner.
- **The difficulty thesis is falsified as written.** Deferred deliberately; do not quietly
  re-adopt conjunction-counting as a difficulty argument in the meantime.
