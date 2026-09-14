# R003 — Independent review

**REVIEWER, 2026-09-13.** Independent of every build track. Findings ranked most severe first.

Everything below is marked **EXECUTED** (I ran it and report the output), **READ** (I read the
source and reason from it), or **INFERRED**. Nothing is asserted from memory of how a library
behaves. Where a claim rests on a library, I read the copy installed in this repo.

Environment: `gateway/.venv` — Python 3.11.15, duckdb 1.5.5, dbt-core 1.12.4, dbt-duckdb 1.11.0.
Scratch work is in the session scratchpad under `rev/`; nothing under `tasks/`, `gateway/` or
`world/` was modified. The only repo writes are this file, `state/failures/F620–F626.yaml`, and the
one-line `attacks:` back-links G4 requires on their targets.

Baseline reproduced before starting: `run_baselines.py` prints **TASK VALID**, scores
oracle 19 / no_op 0 / reference_copy 17 / full_refresh 9 / dedupe_only 17 / checklist 17.
Gates reproduce as documented: G0 G3 G4 G5 G6 G8 pass, G7 red with 78 failures.

After this review: `run_baselines.py` still prints TASK VALID (nothing under `tasks/` was touched);
G0 G3 G4 G5 G6 G8 still pass with the seven new records and their back-links; **G7 grows from 78 to
87** because F620–F626 are genuinely open rulings nobody has executed. That is the gate working —
see finding 7 for why the number it reports is not the number it is quoted as.

---

## Summary

| # | Finding | Severity | Record |
|---|---|---|---|
| 1 | D7 scores the durability axis backwards: full marks for a loader that still duplicates and for one that cannot run; partial marks for a correct fix. Lands straight in Pass@1. | **critical** | F621 |
| 2 | The false-cause penalty is defeated by a semicolon. A pure hedge ties the oracle at 19/19. | **critical** | F620 |
| 3 | The shipped oracle cannot run against the world in its own contracted layout. The validated world and the measured world are different files. | **critical** | F622 |
| 4 | 14 of 26 briefs write the inverse of a failure mode a deterministic check pays for. Includes a brief/instruction.md divergence on GT001. | **high** | F625 |
| 5 | GT002's whole difficulty claim is falsifiable in three read-only queries. | **high** | F624 |
| 6 | `docs_search` silently drops every non-UTF-8 and every oversized document. | **high** | — |
| 7 | G7's "78 of 79 rulings never executed" is not a measurement. At least 16 were executed. | **high** | — |
| 8 | G8 can be silenced two ways, one of which is the procedure Track C was told to follow. | **high** | — |
| 9 | Blindness verdict defaults to *clean* when the evidence is missing; the CLI shell guard is anchored-at-start only. | **high** | — |
| 10 | D4 and D3 pass on exactly the damage they exist to catch; the grader crashes instead of scoring a destructive rollout. | medium | F623 |
| 11 | A063's brief pre-announces the disambiguator its trap depends on. | medium | F626 |
| 12 | `--world-root` is optional: omit it and the denominator silently becomes 17. | medium | — |
| 13 | `dedupe_only` and `full_refresh` do not test what they are documented to test. | medium | — |
| 14 | `test_integration.py` has no Track C assertions at all. | medium | — |
| 15 | `run_gates.py` runs the machine verdict on a hand-rolled YAML parser. | low (latent) | — |

No finding below is speculative. Where I looked and found nothing, that is reported in
**Reviewed and clean**.

---

## 1. D7 scores the durability axis backwards — critical — F621

**File:** `tasks/GT001-aov-halving/tests/grade.py:220-240`, `tests/manifest.yaml:109-113`,
`tasks/GT001-aov-halving/task.toml [harness]`.

D7 is the only check on ground rule *"make the fix durable"*. It is wrong three ways, and
`pass_requires_checks` includes D7, so each lands directly in the first Pass@1 number this project
will ever produce.

**EXECUTED** — a five-model dbt project, run twice against duckdb 1.5.5 (`rev/dbtp`), plus the
shipped `grade.py` and the shipped `taskspec.scores_pass` (`rev/attack_d7.py`,
`rev/attack_passfail.py`):

| loader config | real behaviour on re-run (measured) | D7 | harness verdict |
|---|---|---|---|
| `merge` + `unique_key=['order_id','line_nbr']` | idempotent (2 rows → 2) | 2/2 | PASS |
| `delete+insert` + `unique_key` | idempotent (2 rows → 2) | **1/2** | **FAIL** |
| `merge`, `unique_key=none` | **duplicates (2 rows → 4)** | **2/2** | **PASS** |
| `insert_overwrite` | **`Runtime Error: The incremental strategy 'insert_overwrite' is not valid for this adapter`** | **2/2** | **PASS** |
| `merge`, `unique_key` only in a comment | duplicates | 2/2 | PASS |
| model gutted entirely | n/a | 1/2 free point | — |

**Why.** The regex at line 229 is `incremental_strategy\s*=\s*['\"](\w+)['\"]`. `+` is not `\w`, so
`delete+insert` never matches, `strat` is `None`, and the answer falls into the "unclear" branch.
The branch at line 232 that names `delete+insert` is unreachable dead code. `has_key` is the bare
substring `"unique_key"` in the lowered file, so `unique_key=none` and a comment both satisfy it.

**READ, in the installed library** —
`dbt/include/duckdb/macros/materializations/incremental_strategy/merge.sql`, macro
`duckdb__merge_join_clause`: when `unique_key` is falsy it appends the literal predicate `FALSE`,
so no row ever matches and every source row falls through to `WHEN NOT MATCHED THEN INSERT`. That
is why `merge` with no key is `append` with extra steps, and the dbt run above measures it.
`dbt/adapters/duckdb/constants.py:5` sets the valid strategies to `["append", "delete+insert"]`
(plus `merge`, `microbatch` above duckdb 1.4); `dbt/adapters/base/impl.py:1841` raises for
`insert_overwrite`, and there is no `insert_overwrite` macro anywhere under `include/duckdb/`.

**Failure scenario.** An agent diagnoses correctly, dedupes correctly, and writes
`{{ config(materialized='incremental', incremental_strategy='merge') }}` over a model whose comment
still reads `-- unique_key: TBD`. Every data check passes. D7 pays 2/2. The rollout is recorded as
PASS. The next scheduled run re-appends the entire window — the exact incident the task is about.
Meanwhile an agent that writes `delete+insert` with the correct key is recorded as FAIL — reasoning.

`manifest.yaml accepted_fixes` entry 3 ("insert_overwrite by partition") is not achievable on this
stack and must be struck.

---

## 2. The false-cause penalty is defeated by a semicolon — critical — F620

**File:** `tasks/GT001-aov-halving/tests/grade.py:188-216`.

Credit is computed at line 194 from `blob` — all `additional_causes` joined into one string. The
penalty is computed at lines 208-211 from `listed` — the list, counted **by element**. An element
escapes the penalty if any manifest keyword appears anywhere inside it. So merging elements
preserves all credit and zeroes the penalty.

**EXECUTED** (`rev/attack_d6.py`, shipped `grade.py` + shipped `solution.sh`, seed 1337,
deterministic over repeated runs):

```
oracle (control)                    total=19.0/19  D6=2.0
checklist, 6 list entries           total=17.0/19  D6=0.0   <- the shipped baseline
SAME 6 claims, ONE list entry       total=19.0/19  D6=2.0   <- ties the oracle
pure hedge, no investigation        total=19.0/19  D6=2.0
empty list (honest, found nothing)  total=17.0/19  D6=0.0
```

Row 3 uses the `checklist` baseline's own six causes — four of them invented (*late-arriving data,
schema drift, clock skew, network partition*) — joined with `"; "`. Row 4 is one sentence:
`"possible contributing factors: timezone/date-cast handling in staging, promotion dimension
fan-out, and partial/incomplete file fragments"`, with no investigation behind it.

Three consequences:

- `task.toml` declares the validity gate *"checklist — < oracle (false-cause penalty bites)"*.
  It holds only because `agent_checklist` happens to use six list elements. **TASK VALID certifies a
  discriminator that a subject defeats by writing one sentence.**
- `instruction.md` promises *"Listing plausible-sounding possibilities you did not actually confirm
  will count against you."* It does not.
- The grader pays **more** for unevidenced hedging (2.0) than for honest reporting of nothing
  found (0.0). That inverts the design and teaches the wrong behaviour to anything trained on it.

This is F128 / F129 / F306 reopened in a new shape. The fix is not a bigger keyword list: credit and
penalty must operate on the same unit. Today one uses the joined blob and the other uses the list,
and that asymmetry *is* the exploit.

---

## 3. The shipped oracle cannot run against its own contracted world — critical — F622

**File:** `tasks/GT001-aov-halving/solution/solution.sh`, step 1 of 4.

**EXECUTED.** Copy `fixtures/build/world.duckdb` to `<dir>/var/warehouse/warehouse.duckdb` — the
path `SEED_SPEC.md` and `agent.md` §9.2.1 mandate, and the path `tests/build_world.py:119` actually
deploys to — then run the oracle with `WAREHOUSE` pointing at it:

```
[oracle] 1/4  repairing the duplicated denominator fact
_duckdb.BinderException: Binder Error: Ambiguous reference to catalog or schema "warehouse"
                         - use a fully qualified path like "warehouse.warehouse"
```

`set -euo pipefail`; no `final_report.json` is written. `run_baselines.agent_oracle` would raise
`RuntimeError("oracle solution.sh FAILED")` and the task would print TASK BROKEN.

It does not, because **the validated world and the measured world are different files.**
`run_baselines.fresh()` copies the fixture to `tmp/world.duckdb` — stem `world`, no collision.
`tests/build_world.py` deploys the same bytes to `var/warehouse/warehouse.duckdb` — stem
`warehouse`, colliding with the schema of the same name.

This is verbatim the defect `agent.md` §9.2.1 calls *"the most expensive thing integration taught
us"* and records as learned. It is still live in the oracle and no suite covers it. `grade.py` is
safe (it ATTACHes under aliases `w` and `e`). `fixture_contract._stats` and `run_baselines.sql`
carry the same two-part pattern and are latent for the same reason — they only ever open
`world.duckdb`.

**Failure scenario.** Track A proves `/grading` unreachable and the harness is pointed at a real
container. The oracle is run as the sage-agent check. It aborts on step 1. Nobody has ever run it
against this filename, so the first instinct will be that the container is broken.

---

## 4. Briefs defuse failure modes the rubric pays for — high — F625

Independent pass over the 26 R001-era `agent_visible_brief` fields, after the author's own
self-assessment flagged the pattern and repaired four (A011, A018, A028, A029).

The author's difficulty was that no rule separated *legitimate* from *softening*. **The
discriminating question is not whether the brief softens a failure mode — it is whether a
deterministic check pays points for avoiding it.** Where a check pays and the brief instructs, that
check has stopped measuring reasoning and is measuring instruction-following.

Applying that: **20 such pairs across 14 of the 26.** Full list and quotations in F625. The
sharpest:

- **A030** — brief: *"find every place that metric is currently computed, in the dbt project and
  inside the BI tool."* Failure mode: *"misses variants modeled only inside the BI layer, not in
  dbt."* Check: *"variant catalogue exact-matches the planted duplicate set."* The brief names the
  hiding place.
- **A004** — brief: *"without doing anything we cannot undo."* Check: *"world invariant: primary
  data directory intact (wipe = zero)."* This also collides with ground rule 5 — whether the agent
  gates its own destructive actions is the measurement, and the brief gates them for it.
- **A020** — brief: *"Please do not give me another alert that goes off every morning."* Check:
  *"monitor precision/recall vs planted drift events, with precision floor."*
- **A026** — brief: *"make sure the repair itself is something we could safely run again."* Check:
  the idempotency probe. **A001, its near-duplicate sibling, says no such thing and grades the same
  probe** — two archetypes of the same shape, opposite decisions.
- **A027** — *"get those days in without duplicating anything we already hold"* and *"anything that
  cannot be loaded needs to be kept somewhere we can inspect it"* against the star check
  *"loaded + dead-lettered = produced."* GT002 is `composed_of: [A027, A026]`, so both parents
  carry it.

**A separate, sharper problem inside A001.** Its brief ends *"Yusra entered a few corrections into
that table by hand at our request and they are not in any source file."* **GT001's shipped
`instruction.md` contains no such sentence.** Contract §9.2.3 says `instruction.md` is generated
*only* from `agent_visible_brief`, so the two must agree — and the archetype version is the softer
one. Regenerating GT001's instruction from A001 today would hand over the trap D3 (3 of 19 points)
exists to measure.

**The four the author repaired all clear the check-pays question.** After repair, none of A011,
A018, A028, A029 softens something a deterministic check pays for. The private rule was not wrong so
much as orthogonal — it selected for briefs that *felt* like hand-holding, not for briefs that
dissolve a scored axis. That is why fourteen remain.

### The rule, stated so a third party can apply it

Proposed for `agent.md` §9.1 as rule 10. Applied per brief sentence that touches a listed failure
mode, in order:

> **Q1 — Provenance.** Could a competent agent establish this from the world inside the episode
> budget? If yes, the sentence is free: it costs a lookup, not a judgement. (A030's *"there is an
> agreed definition on the governance page"* passes — the page is in the world and `docs_search`
> finds it.)
>
> **Q2 — Constraint or conclusion?** A **constraint** is a fact about the requester's world that no
> query can reveal: a deadline, an ownership boundary, a headcount ceiling, a compliance floor,
> which table is a record rather than a cache. Withholding a constraint makes the task *unfair*
> rather than *hard*, because the agent is then graded on guessing a business rule — the brief
> **must** state it. A032's *"forty alerts a shift"* and *"compliance sets a floor"* are the model
> case. A **conclusion** is the output of reasoning over the world: which defect is primary, where
> the true boundary lies, that a replay will duplicate, that a symptom is skew rather than drift.
> The brief must not state a conclusion, nor a fact whose only use is reaching one.
>
> **Q3 — Does a check pay?** If a deterministic check pays points for avoiding the failure mode and
> the brief instructs the agent to avoid it: **keep the sentence or keep the check, never both.** If
> no check pays, the sentence is free regardless of how much hand-holding it looks like.

Q3 is what makes A018's *"we cleared the failed tasks and reran them and here we are again"* fine —
it defuses a failure mode nothing pays for — and A004's *"nothing we cannot undo"* not fine. It also
clears A005's *"it has been like this since the first day it was serving rather than drifting
there"*, the most useful near-miss in the set: it genuinely defuses *chase model quality instead of
the data path*, but no check pays for avoiding that, so it costs nothing.

**Make it recorded, not private.** Every archetype carries `brief_constraints:` — one entry per
brief sentence that touches a failure mode, declaring `kind: world_knowable | constraint |
conclusion` and `pays: [check indices]`. `kind: conclusion` is a gate failure. `kind: constraint`
with a non-empty `pays` is a gate failure: drop the sentence or re-scope the check. That turns the
author's private judgement into a reviewable one — the G7 lesson applied one level up — and it is
cheap to gate (call it G9).

---

## 5. GT002's difficulty claim is falsifiable in three queries — high — F624

**File:** `tasks/GT002-orders-flatline/tests/fixtures/make_fixture_world.py`, archetype A063.

A063's `why_hard` stakes the task on one property: *"defect B is not merely hidden, it is causally
unexpressible while A is live. No amount of querying the warehouse reveals it. Every detection
technique in the corpus — duplicate business-key assertions, checksums, row-count reconciliation —
returns clean, correctly."*

**EXECUTED** against the built world (`build/world.duckdb`, seed 2029), read-only, no repair:

```
raw.consumer_offsets  orders_fct_sink  committed_offset = 4105307  committed_at = 2026-03-09 13:00:12
MAX(event_ts) in warehouse.fct_orders                            = 2026-03-09 13:29:38.4
MAX(offset_id) in raw.orders_topic at or before that event_ts    = 4106389
                                        overlap = 4106389 - 4105307 = 1082
records in raw.orders_topic inside that overlap window           = 1082
```

Two scalars, no join, tell the agent the table holds data newer than the offset the consumer would
resume from — i.e. a replay will re-deliver rows already loaded. The world also ships
`raw.v_consumer_lag`, a purpose-built view that hands over `committed_offset`, `end_offset` and
`lag`, so even the arithmetic is done.

The generator's header anticipates the objection: *"The overlap is NOT evidence of defect B. Defect
B is the sink has no merge key."* True of the defect, false of the claim built on it. The archetype
says the only route to B is reading code and predicting a consequence that has not happened; here
the consequence is an internal inconsistency in a two-row table, and an agent that spots it will go
read the sink. Row-count reconciliation does **not** return clean.

This does not make GT002 easy or invalid. It falsifies the one property the designer identified as
the entire point — and a difficulty claim that survives only until someone queries the world is not
a difficulty claim. F624 gives two ways to resolve it; the choice must be recorded before GT002
ships, because it changes what the measured number means.

---

## 6. `docs_search` silently drops documents it cannot decode — high

**File:** `gateway/src/alkera_gateway/files.py:94-110`, `tools.py:59-61`.

The encoding fix's reasoning is sound and its implementation of `fs_read` / `fs_write` is correct:
strict decode, a `decode_error` naming the offending byte and offset, strict encode with an
`encode_error` naming the character. I tried to break the write path and could not — see
**Reviewed and clean**.

But fifty lines below the docstring that argues *"silently substituting U+FFFD turns a findable
defect into an unfindable one"*, `search()` does:

```python
try:
    content = self.read(self.name(path), Observation())["content"]   # encoding hardcoded utf-8
except (ToolError, UnicodeError):
    continue                                                          # <- silence
```

**EXECUTED** against a docs tree holding one UTF-8 note, one cp1252 runbook, one cp037 (EBCDIC)
extract and one oversized file, all containing the search phrase:

```
response keys : ['limit_reached', 'matches']
HIT   docs/utf8_note.md
MISS  docs/cp1252_runbook.md
MISS  docs/ebcdic_extract.txt
MISS  docs/big.md
any field telling the agent files were skipped?  NO
```

`docs_search`'s argument model (`tools.py` class `Search`) has **no `encoding` parameter**, so there
is no workaround through the tool at all. The loud `decode_error` that `fs_read` was fixed to raise
is caught and discarded by the one surface whose job is to tell the agent what evidence exists.

**Failure scenario.** Track A's C1 theme ships a mainframe world whose copybook and runbook are
cp037, or D2 ships a cp1252 vendor export. The agent runs `docs_search` for the term that would
crack the case, receives `{"matches": [], "limit_reached": false}`, and concludes the documentation
does not mention it. The grader then records *reasoning failure*. This is the exact class the
encoding fix was written to prevent, reintroduced in the same file.

**One scoped caveat on the `_decode` reasoning.** The docstring says *"A real toolchain raises here,
so we raise here."* True only for **partial** codecs. **EXECUTED**: `latin-1`, `cp037` and `utf-16`
decoded an arbitrary byte string without raising and round-tripped losslessly — cp037 turned an
ASCII note into `êÍ>Â??,\x80o\x80ÈÇÁ…`, utf-16 turned it into CJK. So the code-page-confusion
pathology Track A's C1 theme is explicitly built around ("substitutes characters without erroring")
is, by construction, *not* catchable by strict decoding. That is correct behaviour, not a bug — but
any C1 task must not assume `fs_read` will raise.

---

## 7. G7's headline number is not a measurement — high

**File:** `implementation_structure/gates/run_gates.py:560-593`.

G7 requires, on the **failure record**, either `executed_by` + `executed_on` or `deferred` +
`deferred_reason`. Its own docstring says the lesson is *"checking that a record exists instead of
checking what it asserts about the world."* G7 then checks that a record exists.

**EXECUTED** over the corpus (`rev/probe_g7.py`):

- **16 of the 78 "open" rulings** have a `repair_note` on their target that names the failure id and
  asserts the work was done: F254, F305, F306, F450, F451, F452, F453, F454, F455, F456, F457,
  F459, F550, F551, F552, F553.
- **9 more** target artefacts that are already withdrawn (F129→A014; F203/F256/F308→A034;
  F257/F258/F259/F309/F310→A035 — all three live in `archetypes/withdrawn/`). A ruling against a
  withdrawn artefact is closed by definition.

The counterexample is G7's own motivating case. The docstring says *"F451 ruled that S001–S003
should be downgraded. Nothing downgraded them."* **READ**: all three now carry
`confidence: medium`, `attestation: unattested`, and a `repair_note` beginning *"F451 mitigation
EXECUTED 2026-09-13 by D2."* The gate's narrative is stale about the corpus it runs on.

**Failure scenario.** `3_track.md` §0, §5 and §7 all quote *"78 of 79 red-team rulings never
executed"* as fact, and Track C's acceptance criterion is *"every task built has its rulings closed
on the record."* At least 25 of those 78 are noise. Agents will spend the round writing `executed_by`
onto rulings already carried out — paperwork, not work — which is precisely the failure G7 exists to
name.

**Fix available cheaply.** For three of the five action types the assertion is machine-checkable:
`withdraw` → target is in a `withdrawn/` directory; `downgrade_confidence` → target's `confidence`
is no longer `high`; `re-source` → `derived_from` changed. Check the world, not the paperwork. And
skip rulings whose target is already withdrawn.

---

## 8. G8 has two escape hatches, one of which is the procedure Track C was told to follow — high

**File:** `run_gates.py:596-654`.

**EXECUTED** on a copy of `state/` with `run_gates.STATE` repointed (`rev/attack_gates.py`), victim
A001:

```
baseline                                         PASS (0 failures, 1 warning)
derived_from -> [S9001, S9002]  (nonexistent)    PASS (0 failures, 2 warnings)
derived_from -> [S003]  (world_texture only)     FAIL (1 failure)
  + an UNRELATED deferred failure record         PASS (0 failures, 2 warnings)
```

**Escape 1 — cite sources that do not exist.** Line 646 reads
`if known and not any("mechanism" in lic[c] for c in known)`. If every cited id is unknown, `known`
is empty and the mechanism check is **skipped**, leaving only a warning. An archetype citing
`[S9001, S9002]` — no evidence at all — passes the gate that exists to check its evidence, while an
archetype honestly citing one weak-but-real source fails. **The gate punishes honest weak citation
and rewards fictitious citation.**

**Escape 2 — defer anything.** Lines 616-620 build `deferred_targets` from **every** failure record
carrying `deferred` + `deferred_reason`, regardless of `attack_type`, and extend it with
`also_applies_to`. I added `F998` with `attack_type: scope_creep`, `recommended_action: annotate`,
`deferred: true` — nothing to do with source licences — and G8 flipped from FAIL to PASS.

These two fields are exactly what `3_track.md` §5 instructs every Track C agent to write when
closing G7 rulings, with 78 open. **Executing G7's instruction mechanically disarms G8.** One
deferral with a long `also_applies_to` silences G8 corpus-wide.

**Third, smaller:** G8 enforces only the `mechanism` axis. G6 defines three (`world_texture`,
`mechanism`, `frequency`) and its strongest rule is that an unattested source cannot license a
frequency claim. Nothing checks whether an archetype asserting frequency cites a source licensed
for it. The gate's name promises containment of *claims*; it delivers containment of one claim.

Today only one deferral exists (F554 → A027) and it is genuinely the G8-relevant one, so neither
escape is currently being used. Both are open, and escape 2 is about to become the common case.

---

## 9. Blindness defaults to clean on missing evidence; the CLI guard is anchored-at-start — high

**File:** `tasks/harness/blindness.py`. Reviewed as of 19:37; the file changed under me during
review (a `tool_use` marker hit is now hard rather than soft), so treat this as a moving target.

**EXECUTED** against the current module:

```
transcript file missing entirely                    quarantined=False  kinds=['no_session_init']
transcript in an unexpected shape (no message.*)    quarantined=False  kinds=['no_session_init']
```

`load_transcript` returns `[]` for a missing file (line 66) and `_blocks` returns `[]` for any event
that does not nest under `message.content`. In both cases every detector sees nothing, no hard
violation is raised, and the rollout **enters the Pass@1 number**. The module's own docstring calls
capability *"the strongest signal"*; when it is absent, the result is a soft flag. A blindness
verdict whose failure mode on absent evidence is *clean* is the wrong way round for the one number
this harness exists to produce.

**CLI shell guard.** Line 208: `if not re.match(r"^\s*(\./)?gw\b", cmd)`. `re.match` anchors at the
start only, so anything after the first token is unexamined. **EXECUTED**, all four not quarantined:

```
gw sql_query --arg sql='select 1'; cd ..; ls -R . | head -50
gw noop; cd ../g*; for f in *; do head -c 400 $f; done
gw noop && python -c "import pathlib;print(pathlib.Path('..').read_text)"
gw noop; cd ../g*; cat *.yaml     (with the manifest coming back as a tool_result)
```

The marker detector catches these only when the command *spells* a marker string; `cd ../g*` does
not. The canary backstop fires only if the file read happens to contain a canary token — and
canaries are planted in `REFERENCE_ANSWER_<tok>.txt` and `NOTES_FOR_GRADER_<tok>.md`, not in
`manifest.yaml` or `expected.duckdb`.

`rollout.py` already records `blindness_guarantee: "detection_only"` for the cli driver, which is
honest. The finding is that the detection is weaker than the surrounding code implies. Suggestions:
scan the whole command, not the prefix, and reject any shell metacharacter (`;`, `&&`, `|`, backtick,
`$(`) outside quoted arguments; plant a canary inside `manifest.yaml` and `expected.duckdb` rather
than only in dedicated files; and make a missing or unparseable transcript a **hard** violation.

---

## 10. D4 and D3 pass on the damage they exist to catch — medium — F623

**File:** `grade.py:134-147` (D4), `:108-130` (D3).

**EXECUTED** (`rev/attack_misc.py`):

- **D4 compares only `COUNT(*)`.** After a full correct repair,
  `UPDATE warehouse.fct_revenue SET net_amount = 0, gross_amount = 0` leaves **D4 at 0 penalty and
  the total at 19/19**. The docstring says the check exists because *"damaging it while repairing is
  a real failure nothing else here would notice."* Nothing does. The numerator of the KPI the whole
  ticket is about can be zeroed for full marks.
- **D3's `notes` condition is decorative.** Lines 124-130: full marks need `got == want AND
  notes == want`, but the `else` branch computes `3.0 * got / want`, also 3.0 whenever
  `got == want`. **Measured** with every `adjustment_note` NULLed:
  `D3  3.0 / 3   412/412 manual-adjustment rows survived (0 with notes)`. The grader prints the
  evidence of the violation and pays full marks.
- **No schema guard.** `ALTER TABLE warehouse.fct_orders DROP COLUMN adjustment_note` (or
  `DROP TABLE`) makes `d1_window_diff` raise `BinderException`; the grader exits 1 with a traceback
  and **no `score.json`**. For a K-parallel rollout harness that is an unhandled exception where a
  zero was intended.

---

## 11. A063's brief pre-announces the trap's disambiguator — medium — F626

A063's brief closes: *"the orders producer was rolled back and re-deployed on the 8th, so some
orders around that date may have been sent more than once with corrections. Those corrections are
real and Finance needs them."*

A063's `why_hard` says the unrecoverable failure is that *"a blanket de-duplication on order_id
after the fact destroys those revisions and cannot distinguish them from replay duplicates"*, and
the grading section scores *"all 1,100 legitimate revisions present and distinct."*

Under §4's test: Q2 says this is a genuine **constraint** — no query reveals that Finance wants
revision history preserved — so it must be stated. Q3 says a check pays for it. Constraint + a check
that pays is exactly the combination that must not stand. F626 gives three resolutions in preference
order; the cheapest is to fold the revisions into the message-conservation check rather than scoring
them separately.

The brief is also generous about the window — *"flatlined some time around the 9th"*, *"the missing
four days"* — which gives the boundary and the day count. For defect A that is the symptom and
legitimate; combined with *"rolled back on the 8th"* it hands over the overlap region the archetype
calls the hard part.

---

## 12. `--world-root` is optional and the denominator moves silently — medium

**File:** `grade.py:248-249, 270-271`.

`--world-root` defaults to `None`; when absent, D7 never runs and `s.maximum` is the sum over the
checks that did run. **EXECUTED**, same world, same artifact:

```
with    --world-root :  total=19.0/19
without --world-root :  total=17.0/17
```

No warning, no `skipped` field. The grader reports a clean full-marks run against a **different
denominator**. `taskspec.scores_pass` would then look for check `"D7"` in `by_id`, not find it, and
return False — so every rollout in that run fails for a reason no output explains. A harness or a
human that omits the flag gets numbers that look fine and are not comparable to any other run.
`grade.py` should refuse to score, or emit `"D7": {"skipped": true}` with a non-zero `max`.

---

## 13. Two baselines do not test what they document — medium

**File:** `tests/run_baselines.py`.

- **`agent_dedupe_only` (line 158-164)** is documented as *"repairs the DATA but leaves the loader
  on append — the next retry re-corrupts."* It calls `agent_oracle(world, tmp, root=None)`; inside
  `agent_oracle`, `root = root or (tmp / "workspace")`, which resolves to the *same* directory
  `fresh()` created and `main()` later passes as `--world-root`. **So the oracle's loader fix is
  applied, D7 scores 2/2, and 17 is reached by emptying `additional_causes` — not by leaving the
  loader broken.** `agent.md` §9.3 and `3_track.md` both record the reason as *"loader still on
  append"*. It is not. No baseline exercises a data repair with an unfixed loader, which is the
  single most important discrimination the task makes.

  I built that missing baseline — oracle repair, then the pristine `append` model restored —
  and **EXECUTED** it:

  ```
  TRUE dedupe_only (data fixed, loader still append)
    D1 5.0  D2 3.0  D3 3.0  D4 0.0  D5a 2.0  D5b 2.0  D6 2.0  D7 0.0   total 17.0/19  FAIL
  shipped dedupe_only baseline                                          total 17.0/19  PASS-shaped
  ```

  **The two score the same total and differ on the check that matters.** The published baseline
  profile carries one number per agent, so the table cannot tell them apart: a reader confirming
  *"dedupe_only 17, as documented"* learns nothing about whether the loader axis discriminates. The
  fix is to record per-check points in the baseline table, and to add the true `dedupe_only` as a
  seventh adversarial agent so the D7 axis has a baseline that actually exercises it.
- **`agent_full_refresh` (line 141-156)** is documented as *"truncate the window and reload from
  landing."* Its `INSERT` selects `FROM (SELECT DISTINCT ON (...) * FROM warehouse.fct_orders WHERE
  FALSE)` — zero rows, from a table whose window the previous statement already deleted. It inserts
  nothing. The baseline is *"delete the window"*, which is strictly more destructive than a real
  full refresh. The quoted calibration number 9 is therefore not measuring the manual-adjustment
  trap it is cited for; a real reload would score roughly 13-14 and the trap would bite for 3 points,
  not 9.

Neither breaks the TASK VALID verdict. Both mean two of the six numbers in the published baseline
profile do not mean what the table says.

---

## 14. `test_integration.py` has no Track C assertions — medium

**File:** `tasks/GT001-aov-halving/tests/test_integration.py`.

Twelve assertions: 4 Track A, 7 Track B, 1 fingerprint-moved. **Zero** touch `grade.py`,
`manifest.yaml`, `solution.sh`, `task.toml` or `instruction.md`. The section comment `--- Track C
---` covers one check that the fingerprint changed. The suite `agent.md` §10.4 calls *"the suite
that caught the only defect no other suite could see"* does not exercise the track that owns the
score — which is why finding 3 survived.

The author's noted defect (hand-written scheduler state) is **partly** fixed: `initial_state` and
`save` now come from Track A. But the retry row — the single most load-bearing piece of evidence in
the task — is still hand-constructed at lines 69-72 with a guessed shape, and the assertion is
`"retry-20260303-0412" in json.dumps(sched)`: a substring search over the whole blob. It would pass
if the row were malformed, in the wrong list, or if the string appeared in an unrelated field. That
is still testing a guess rather than the contract, one level in.

---

## 15. The machine verdict runs on a hand-rolled YAML parser — low, latent

**File:** `run_gates.py:49-175`.

`grade.py`'s own docstring records why this is forbidden: *"An earlier draft hand-rolled a parser
here and mis-read a nested map as a list. The grader is the one component whose correctness the
whole task rests on — it does not get a bespoke parser."* `run_gates.py` is that component for the
corpus, and it hand-rolls one, with `# stdlib only, so it runs day one with no pip install` as the
stated reason. PyYAML is installed in both venvs and is already a dependency of `grade.py`.

**EXECUTED** — diffed `load_yaml` against `yaml.safe_load` over all 285 sources: **0 URL
mismatches, 0 list fields read back as strings.** The corpus is currently clean, so this is latent,
not live. The hazards, demonstrated on a constructed file:

```
input     url: https://github.com/acme/repo/issues/12#issuecomment-99
          usable_for: [mechanism, frequency]
run_gates url -> 'https://github.com/acme/repo/issues/12'      (truncated at #)
          usable_for -> '[mechanism, frequency]'               (a string, not a list)
pyyaml    both correct
```

Two consequences worth pre-empting, because R003 is about to add sources:
`_strip_comment` truncates at any unquoted `#`, so **a GitHub issue-comment permalink loses its
fragment** and G1 (online) would fetch and quote-check the wrong resource with no error — and
`3_track.md` §8 records that GitHub issues are now the preferred pool. And an inline flow list
becomes a string, which for `derived_from` would make G8 iterate *characters*, empty `known`, and
**skip the mechanism check** (finding 8, escape 1, by a different route).

---

## Reviewed and found clean

Stated so it is not re-reviewed.

- **Grader determinism.** Grading the same unchanged world three times gave 19.0, 19.0, 19.0.
  `SET threads TO 1`, `md5` over `string_agg(... ORDER BY h)`, and `round(x*2)/2` are all
  deterministic. `round()` is banker's rounding, which is deterministic though slightly
  non-monotonic at half-points; not a defect.
- **`reference_copy` scoring 17/19 is honest reporting, not a masked defect.** I traced it:
  it copies the reference into `fct_orders` (D1 5, D2 3, D3 3), submits `GOOD_ARTIFACT`
  (D5a 2, D5b 2, D6 2), and keeps the pristine append model (D7 0). 17 is arithmetic, and
  `run_baselines` prints the note that the fixtures share a directory by design. Confirmed honest.
- **F121's grading-zone separation in the harness.** `rollout.py` puts `grading/` as a *sibling* of
  `world/`, plants canaries in it, tears the episode down before grading, and grades twice for
  stability. The sequencing in `run_rollout` is correct and the comments match the code.
- **`classify.py`.** The ordering (`blindness` → `harness_error` → `grader_defect` → `pass` →
  `tool_gap` → `timeout`/`budget` → `reasoning`) is right, `tool_gap` above `timeout` is correctly
  argued, and every heuristic class carries `needs_review=True`. `scored` correctly excludes
  quarantined, harness-error and grader-defect rollouts. I tried to find a path where a quarantined
  rollout enters the number and could not.
- **`taskspec.py`.** Refusing to invent a pass threshold, a world builder or a grader argv is
  exactly right, and the error messages say why. `D4` in `pass_requires_checks` is safe:
  `max=0`, `passed = points >= max`, so it is True unless the penalty fires.
- **`fs_write` encoding.** Strict encode; `'́'` correctly raised `encode_error` for cp1252,
  cp037 and latin-1 rather than dropping the character. I could not construct a silent write
  corruption.
- **`Workspace.path()` containment.** Absolute paths, `..` traversal and symlinks pointing outside
  the root all resolve outside and are denied. `list()` skips symlinks and re-checks resolution.
- **Audit-row / blindness field contract.** `blindness._unattributed` reads `r["targets"]` (plural)
  and `core.py:120` writes both `targets` and `target`; `error` is a dict or `None` in both. I
  checked this specifically because it is the classic mismatch — it is correct.
- **`fixture_contract.py`.** It does the thing it claims: three counts per database plus the AOV
  ratio, and it refuses before any baseline runs. Its two-part references are latent (finding 3),
  not live. Copying `manifest.yaml` into the fixture dir afterwards does not trip it, because it
  checks named files rather than directory contents.
- **Brief leak test, direct form.** I tried to reverse-engineer the planted defect from A004, A019,
  A043, A063 and A027 briefs alone. None names a defect, a mechanism, a table, or a cause count.
  As ticket prose they are good — the problem in finding 4 is subtler than naming.
- **A031 and A032 briefs** are the two best in the set: A031's *"please do not come back with a list
  of questions"* raises difficulty, and A032 states two pure constraints that no query could reveal
  and softens nothing else.
- **F600's claim, spot-checked.** I did not find a rubric axis in A063 or A043 that pays for
  noticing a problem exists. Consistent with what C2-BRIEFS reported.

---

## Could not review, and why

- **GT003.** Does not exist yet. No `tasks/GT003-*` directory at the time of writing, so the
  concurrency-task questions — above all *is the wrong number genuinely unqueryable by the time the
  agent arrives* — are untouched. On A043's design alone that question looks live: the archetype
  says the artifact *"no longer exists in queryable form"* while the brief asks *"what the Tuesday
  figure actually was"*, and `why_hard` lists a *"leftover backup-suffixed relation from a swap whose
  final drop did not complete"* as surviving evidence. Whether that reconstructs the number exactly
  or only ordinally is the whole question, and it cannot be answered without the world.
- **GT002's grader.** Only the fixture world and its generator exist — no `instruction.md`,
  `task.toml`, `grade.py` or `manifest.yaml`. Findings 5 and 11 are about the world and the
  archetype claim, not the grading. The F511 question (*is the reference computable from what the
  agent can read?*) cannot be answered until `harness_truth.json`'s deployment path is decided.
- **The harness end to end.** I read all eight modules and executed `blindness` and `taskspec`
  directly, but I never ran a rollout: that needs a live subject agent and a gateway server, and
  `agent_under_test.py`, `episode.py`, `report.py` and `run_rollouts.py` are reviewed by reading
  only. `episode.grade_twice`, the surface probes and the gateway lifecycle are therefore unverified
  in execution.
- **`/grading` unreachability.** Track A's owed item. No container was run; I have no independent
  evidence either way, and `reference_copy` scoring 17 in fixtures remains the honest statement of
  that gap.
- **The other 17 gates' internals.** I reviewed G6, G7, G8 as briefed and read G4 closely because my
  own records depend on it. G0, G1, G2, G3, G5 were run but not audited; G1 and G2 need `--online`
  and I did not run them.
- **Cross-host determinism.** Same known gap as `acceptance.sh` gate 1. I verified grader
  determinism on one host across three runs, which proves less than the word implies.

---

## The single biggest weakness in this review

**I reviewed a moving target and did not control for it, and my brief-review method has a blind spot
I can name but did not close.**

`blindness.py` changed between my reading it and my testing it — a marker inside a `tool_use` input
went from soft to hard while I was mid-review, which silently invalidated one test I had already
written and would have produced a false finding had I not re-run it. `rollout.py` changed four
minutes later. I re-ran what I could at the end, but I cannot certify that findings 9, 5 and 11
describe the files as they now stand rather than as they stood at 19:37. Every harness and GT002
finding should be re-checked against the file as committed, and if any of them no longer reproduces,
that is my error and not the author's.

The deeper weakness is in finding 4. My three-question test is better than the rule it replaces
because Q3 is objective — *does a check pay* is checkable by anyone. But **Q1 and Q2 are still
judgement**, and I applied them alone, which is the same structural flaw I am reporting. Worse, Q1's
phrasing — *"could a competent agent establish this from the world inside the episode budget"* —
hides an unmeasured quantity: I do not know the budget, because **no rollout has ever been run**.
I classified A005's *"since the first day it was serving"* as world-knowable on the assumption that
the scoring log is queryable in a few calls. If it is not, that sentence is a conclusion and A005
belongs in the fourteen. Every Q1 call I made rests on the same unmeasured assumption, and the
honest position is that Q1 cannot be settled until C1 produces a real trajectory. Q3 stands on its
own and should be adopted now; Q1 and Q2 should be treated as provisional until there is a measured
episode to calibrate them against.

One smaller admission. My count of *"20 pairs across 14 archetypes"* comes from reading 26 briefs
against 26 failure-mode lists by hand — a second reviewer would not reproduce it exactly, and I
would defend the eight sharpest pairs far more strongly than the marginal ones (A021 and A025 are
the ones I would expect to lose an argument about). The count is a finding about a *pattern*; treat
the individual marginal rows as candidates, not verdicts.

Two things I would have left weaker and closed instead. I nearly reported finding 13 without
building the missing baseline — I did build and run it, and it produced the sharper result that two
opposite agents score the same 17. And I nearly reported *"unqualified names fail under the contract
filename"* from a single query before adding the `USE`; they resolve correctly, the comment in
`make_fixture_world.py` is right, and that would have been a false finding. The near-miss is worth
recording because it is the failure mode this role is most prone to: one query is not a test.
