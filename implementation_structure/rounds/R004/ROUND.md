# R004 — environment fidelity

**Opened 2026-09-14.** Northstar restated by the user: *the sandbox mocks a real
**data engineer's** working computer as it actually is.* Data science is **out of
scope** — dropped explicitly this round. Task difficulty stays deferred.

R003 built the system and measured it. R004 asks a narrower question: **would a
working data engineer recognise this box?** Every item below is a place where we
answered "no".

## Builders — disjoint file ownership

Four agents in parallel. The ownership split is load-bearing: R003 lost hours to a
determinism "failure" that was one agent editing a fingerprinted theme input while
another's gate was mid-flight, and the two are indistinguishable from the outside.

| Agent | Owns | Must not touch |
|---|---|---|
| **WORKFLOW** | `themes/*/workflow/**`, `gen.py` workflow hook, `theme.yaml` **`workflow:` key only**, `build.py`, `fingerprint.py`, `test_workflow.py` | `workspace/`, `live:`, `faults.py`, `scheduler.py`, `gateway/docker/` |
| **IMAGE** | `gateway/docker/**`, the diagram comment at `container_episode.py:27` | `world/**`, `tools.py`, `build.py`, `fingerprint.py` |
| **HISTORY** | `faults.py`, `scheduler.py`, `theme.yaml` **`live:` subtree only**, GT baselines if forced | `build.py`, `fingerprint.py`, `workspace/`, `workflow:`, `gateway/docker/` |
| **REPO** | `themes/*/workspace/**` | every `theme.yaml`, all world `.py`, `gateway/**` |

`theme.yaml` has two owners over disjoint top-level keys. Both were told: targeted
edits only, never a whole-file rewrite.

**No agent re-records a golden fingerprint hash it does not own.** Each reports
which sections moved; integration re-records once. Three of the four move
fingerprints, so this is the round's main merge hazard.

## What is being closed

1. **Workflow plane seeded.** 15 tools (`issues_*`, `chat_*`, `kb_*`) are built,
   contracted and tested, and **no theme seeds them** — the ticket the work arrives
   in does not exist in any buildable world. Plus the missing `workflow` section in
   `fingerprint.py`, which `WORKFLOW_CONTRACT.md` already flagged as owed.
2. **Toolbelt.** The image installs `git` and `openssl`. That is the whole CLI
   surface behind the agent's shell. `container_episode.py:27` promises `psql`,
   which is not installed.
3. **Tool parity.** Image pins 36 tools; the gateway has 63. Container rollouts run
   a toolbelt a generation behind, missing the entire workflow surface.
4. **A past.** Every episode opens on 30 consecutive green nights that then start
   failing. `faults_in_history` exists, covers job faults only, and is off
   everywhere. `revenue_daily` stays clean — it is the determinism reference seed
   **and** GT001's ground truth.
5. **The repo.** No orchestrator the agent can read (DAG *code* is how engineers
   experience orchestration, not JSON), no config or credential archaeology, no CI,
   no `.gitignore`, no staleness.

## Researchers — the blocked list

The user's question: *"if we cannot directly call them, can we create a fake api or
mcp that let agent believe they are using those corresponding tools?"*

| Agent | Scope | Output |
|---|---|---|
| **RESEARCH-WAREHOUSE** | Snowflake / BigQuery / Databricks SQL surface | `RESEARCH-warehouse.md` |
| **RESEARCH-ORCHESTRATION** | Airflow (+ Dagster, Prefect) under a virtual clock | `RESEARCH-orchestration.md` |
| **RESEARCH-STREAMING** | Kafka / Redpanda, Spark / distributed compute | `RESEARCH-streaming.md` |

All three answer one criterion above the others:

> **Fidelity that survives contact.** A fake that is merely incomplete is fine —
> every mock is. A fake that is *wrong* teaches behaviour that fails on the real
> system, and this substrate is sold to labs to train and evaluate agents, so a
> wrong fake is a defect that propagates into models.

Each must name precisely where its proposal diverges from the real system and what
an agent would learn there that is false. That analysis is the deliverable; the
recommendation is secondary. "Not this round" is a legitimate top answer.

The sharpest form of the question, given to ORCHESTRATION and REPO both:
**if the agent edits a DAG file, what must happen?** If nothing happens, we have
taught it that editing DAGs is inert.

## Integration gates (mine)

```
cd gateway && .venv/Scripts/python -m pytest tests
cd world   && PYTHONPATH=. ../gateway/.venv/Scripts/python -m pytest tests
GT001 / GT002 / GT003                      -> TASK VALID   (hard gate)
python tasks/harness/selftest.py
bash world/scripts/acceptance.sh
python implementation_structure/gates/run_gates.py   # G7 red BY DESIGN
```

Then: re-record the fingerprint goldens once, across all four builders' movement.

---

# Integration

## Fixed at integration

| Defect | Fix | Proof |
|---|---|---|
| `workflow` missing from the section-list golden (`test_world.py:41`) | added, docstring updated | test passes |
| **Cross-host determinism broken, `sftp_vendor`** — all 60 `.done` sentinels differ (`53\r\n` vs `53\n`); `Path.write_text` opens in text mode and the paired `.csv` already passed `newline=""` | `newline=""` on the sentinel write | built world: 60 sentinels, **0 containing CR** |
| **Cross-host determinism broken, `excel_close`** — workbooks differ at *identical byte length*; `ZipInfo.__init__` sets `create_system` 0 on win32, 3 elsewhere, one byte per central-directory entry | pinned `create_system = 3` | built world: workbooks report `{3}` |
| `acceptance.sh` gate 2c aborts under Git Bash — `mktemp -d` yields an MSYS path bash and a Windows python resolve to different directories | guarded `cygpath -m` conversion | battery re-run |

The two cross-host defects were found by REPO while doing unrelated work, and they
matter more than anything R004 set out to build: `world/README.md` opens with *"the
product here is not the tools; it is that the world is **provably the same world**
every time it is built."* For two of four themes that was false. They were invisible
because the sentinel CR does not change row counts and the `create_system` byte does
not change file length — no length check, row count or parse test could ever see
either. Only a byte-level cross-host diff finds them, and gate 3 is that check; it
had been aborting at gate 2c on this host, so it never ran.

**Three separate agents hit the gate 2c abort and each reported it as "not mine."**
Each was right individually. The cost was that gates 3-7 never ran in any of their
sessions, which is how two themes shipped a broken core guarantee through a round
whose stated rule is *a check that never ran against the thing it ships against
proves nothing*. The lesson is not about the bug; it is that **a skipped gate needs
an owner, or it is indistinguishable from a passing one.**

## Open — scoped, not started

### 1. `late_arrival` is inert (highest value)

`executor._kind_scan` globs the **cumulative** delivery folder with `min: 1`, so after
day one the count can never fall below the minimum:

- `sftp_vendor.sftp_poll` — `data/drop`, `*/*.done`: measured **60 older sentinels, succeeded**
- `mainframe_batch.nightly_extract` — `data/landing/claims_extract`, `dt=*/CLAIMS.DAT`: **30 older partitions, succeeded**

Both themes ship `late_arrival` **enabled** and both `theme.yaml`s assert the poll
*"FAILS for real - a genuine scan of a genuinely empty folder, not a planted status."*
It does not. A late night's entire signature is `effects: rows_after == rows_before`.

**Corrected estimate.** HISTORY called this a one-line `jobs:` change. It is not:
`{day}` templating exists for `detail` (`scheduler.py:276,566`) and for `script` args
(`executor.py:262`) but **not** for scan patterns, and `scheduler._execute` at line 646
passes `variables=None`, so `_kind_scan` cannot see the execution day at all. Three
parts: thread the day into `_execute`, template `pattern` in `_kind_scan`, then change
the two themes. Touches the execution path, so seeded history and `missed_by` must be
re-derived together. GT001 is on `revenue_daily` (no scan job) and GT002/GT003 build
their own fixture worlds, so no GT ground truth moves.

### 2. Authored claims can drift from world state — one gate, not two patches

Two builders independently opened the same hole:
- **WORKFLOW**: tickets quote warehouse figures measured against `seed.example.yaml`; change a seed and every number silently becomes fiction while structural validation still passes.
- **HISTORY**: `missed_by` declares which jobs a late arrival broke; `check_history` can verify the job *exists*, never that it *would have failed*.

Same shape as G8 (archetype claims stay inside source licences). Wants one containment
gate tying authored assertions to measured world state.

### 3. Tool parity needs a commit, not a rebuild

HEAD declares **36** tools; the working tree has **63**; the 27 extra are observability
(12) and workflow (15), uncommitted. The image was never stale against its own source
revision — my "36 vs 63" framing was wrong. `test_image_was_built_from_current_head`
now guards this and goes red on a commit without a rebuild.

### 4. Nobody has run a rollout against the new box

GT001 Pass@1 = 1.0 was measured against a two-binary image. The box now has 50
binaries, a seeded backlog, DAG code, and scarred run histories. That changes what an
agent attempts, and therefore the measurement.

### 5. Smaller, owned

- `revenue_daily.daily_load` should become `{kind: script, path: loader/load_daily_revenue.py}` — it is the only theme whose loader is duplicated between `gen.py` and the workspace (REPO).
- `dag_check.py` handles `>>`/`<<` but not `chain()` / `set_upstream()` (REPO).
- Seeded history contains runs dated **after `vnow`** — 2 in `revenue_daily`, 3 in `sftp_vendor`. Pre-existing; fixing `vnow` moves GT001's byte-identical history (HISTORY).
- Stale docs: `WORKFLOW_CONTRACT.md:38-50,214`; `world/README.md:163,~304`; `themes/__init__.py:7`; `build.py:~102`.
- `build_episode.py --overlay` should be deleted or default-off once the docker change lands (IMAGE).

## G7: three rulings closed, and what checking them found

G7 **83 -> 80**. F620, F621, F622 now carry execution records. Two of the three were
**not** as executed as memory said, which is the gate working exactly as its docstring
intends: *"'someone probably did it' is exactly the assumption that produced this gate."*

- **F620 — was half done.** The credit/penalty unit fix shipped in R003. The mitigation
  also asked for *"a concatenated checklist added as a seventh adversarial agent so the
  regression cannot return silently"*, and that agent did not exist: the fix had been
  shipped with nothing guarding it. `run_baselines.agent_checklist_joined` now submits
  the six decoys as one semicolon-joined string - the literal exploit - with two
  verdicts. **Measured: joined 17.0 == listed 17.0, both < oracle 19.0.** The form that
  once scored 19.0 and tied the oracle now scores 17.0. Now fully executed.
- **F621 — partial, recorded as partial.** The static stand-in (strategy regex, real
  `unique_key`) and the manifest correction are done. The mitigation's *preferred* fix -
  stop parsing config text, run the model twice and diff business columns, or resolve
  through dbt and validate against `adapter.valid_incremental_strategies()` - is not.
  **The publication caveat therefore still binds:** no Pass@1 figure including D7 in
  `pass_requires_checks` should be published while D7 is a regex over source text.
- **F622 — four of five parts.** `fixture_contract.py` was the latent case the finding
  named by name, and it was still live. Both connections now use aliased ATTACH.
  **Measured at the contract path AND the convenient path: identical stats.** `_stats`
  takes its path from its caller, so it was one caller away from firing. The fifth part
  (a `test_integration.py` assertion) is deferred because `run_baselines` already
  executes the shipped `solution.sh` at the contract filename and grades it.

The pattern: **all three fixes were real and all three were narrower than their
rulings.** Recording execution is not bookkeeping - it is the step that re-reads the
ruling and notices the remainder.

## Post-integration: the determinism defect that enters at clone time

Found while cleaning the working tree for commit, and it belongs with the two
cross-host defects above because it is the same class one layer further out.

`.gitattributes` declares `* text=auto eol=lf`, so **every** clone - Windows or
Linux - checks out LF. The working tree did not: **465 files carried CRLF**, 17 of
them under `themes/*/workspace`, which `build.py:98` copies **byte-for-byte**
(`shutil.copytree`) and `fingerprint.py:72` hashes as the `workspace` section.

Measured on `revenue_daily` rather than argued:

```
files differing CRLF vs LF :  3
workspace hash, as-is      :  eefdae93e1fee17e7b8ca9972addf2bcf49f4c23e9a17b8aa4de00f609ab530b
workspace hash, as cloned  :  48eda34a24daea605a105d268cbcfc5a73f917c3a1e2c5f70e31544cf640e389
```

**Gate 3 cannot see this, by construction.** It builds twice from one source tree
and compares; the CR is present in both, so both agree. No amount of cross-host
building finds a defect that enters at checkout. The two earlier defects were
invisible to every *content* check and needed a byte check; this one is invisible
to the byte check too, and needs the tree to equal what a clone produces.

Fixed by normalising all 465 files to LF (0 remain) and re-verifying the full
battery: world 120/66 subtests, gateway 136+1s, harness 73/0, GT001/2/3 TASK
VALID, ALL ACCEPTANCE GATES PASSED, G0/G3/G4/G5/G6/G8 pass, G7 80 (red by design).
No golden moved - the goldens compare two live builds, which is why this survived.

**Standing rule this earns:** the fingerprint is only as good as the bytes the
build is handed. `.gitattributes` is therefore a fingerprint input, and a tree
that disagrees with its own checkout rules is a determinism defect, not untidiness.
