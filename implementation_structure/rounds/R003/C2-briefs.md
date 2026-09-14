# C2-briefs — closing F513 corpus-wide

**Round R003 · agent C2-BRIEFS · Track C · 2026-09-13**

Everything marked **[V]** I ran on this machine today. Everything marked **[R]** is a claim I
read and attributed but did not execute. Ground rule 7.

---

## 0. What shipped

`agent_visible_brief` now exists on all 26 R001-era archetypes. **[V]**

```
A001 A002 A003 A004 A005 A006 A009 A010 A011 A012 A013 A017 A018
A019 A020 A021 A022 A025 A026 A027 A028 A029 A030 A031 A032 A033
```

- All 26 land in the 60–150 word band (min 125, max 150, median 136.5). **[V]**
- Every file parses identically under the gate's mini-YAML reader and under real `pyyaml`,
  and the two agree token-for-token on the brief text. **[V]**
- `python implementation_structure/gates/run_gates.py --gate G0` → **PASS, 565 checked, 0
  failures, 0 warnings.** G3 and G4 also PASS with their pre-existing warnings unchanged.
  **[V]**
- `git diff --numstat` over `state/archetypes/` shows **zero deleted lines** in all 26 files.
  One field added, nothing else touched. **[V]**

Two failure records filed and cross-linked (§3): **F600** (A017) and **F601** (A026, scoped
to six siblings). G7's open-ruling count went 78 → 80 as a direct result. **[V]** That is
correct behaviour, not a regression: both are live rulings I have no authority to execute, and
§9.2's own rule is that silence is not closure.

**F513 is now closed as a corpus-wide defect.** The consequence the red team drew about its
own work — *"no attack in this round has tested the task the agent actually sees, because that
text does not exist"* — no longer holds. The text exists and can be attacked. It has not been
attacked yet, including by me; see §5.

---

## 1. Method

Read `3_track.md` §0–§2 and §5, `agent.md` §9.1 rule 2 and §9.2.3, `F513.yaml`,
`tasks/GT001-aov-halving/instruction.md`, and the four R002 briefs that already meet the
standard (A040, A043, A060, A063), plus the other ten R002 briefs for the range of registers
in use. Then read all 26 target archetypes in full, including `repair_note`, because several
repair notes invalidate legs that a naive brief would have written a ticket around (A009's
materialisation leg, A019's rename boundary, A026's frequency language).

Every brief is a ticket from a named person with a role and a ticket id, reporting an
observable and asking for something. None names a defect, a mechanism, a decoy, a cause count,
or an exact damage window.

Two mechanical checks, both run:

**Leak lint.** F513's mitigation proposed *"a lint should fail any brief containing a token
that appears in the archetype's `failure_modes_expected`."* I implemented that (widened to
`why_hard` + `failure_modes_expected` + `deterministic_checks`, stopworded, ≥4 chars) and ran
it over all 26. **[V]** Median overlap 6 tokens, max 11. Reviewing every hit by hand found
three real leaks and I fixed all three:

| Archetype | Leaked | Fix |
|---|---|---|
| A005 | brief said *"the serving path fixed"* — pre-empts FM "chases model quality instead of the data path"; and *"do not retrain it"* pre-empts FM 1 verbatim | both cut |
| A030 | *"tell me what each one actually computes rather than what it is called"* — that is FM "trusts variant names and misses hidden filters", handed over | clause cut |
| A022 | *"I do not need a strategy view"* — that is FM "declares a real business decline and writes a strategy memo" | clause cut |

The honest finding about the lint is that **it is weak**. All three leaks it surfaced were
lexical. The leaks I am most worried about (§5) are semantic and every one of them passes the
lint clean. Ship the lint — it costs nothing and it caught three — but do not treat a green
lint as evidence.

**Consistency check against grading.** Diffed each brief against its archetype's
`deterministic_checks`. Found two defects in my own drafts and fixed both:

- **A029** — my brief asked for *"a corrected figure for each of the last twelve weeks by
  segment"*. The check is *"corrected metric matches reference in all 12 **day**-segment
  cells"*. A brief that pins the wrong grain would have contradicted the grader. Now
  unpinned.
- **A009** — my brief invented hand-adjusted rows that must survive. A009's description and
  checks contain no such element (A001's do). That is a brief specifying world content rather
  than describing it, which inverts the dependency. Replaced with a constraint A009's own
  design supports (the nightly schedule keeps running).

---

## 2. The nine written against designs I expect to change

`status: redesign_required`, so the brief will likely need rewriting when the design lands:

| | Why the brief is likely to move |
|---|---|
| **A002** | Adapter-pinning constraint (F454) may force a different defect; the loud-sibling leg may be re-cut from S753, which fails loudly rather than silently. My brief reports both symptoms. |
| **A010** | Two defects stapled together; F601 argues it should merge with A017 or A025. If it merges, half this ticket goes. |
| **A017** | F600 (mine) says a graded axis has to be rewritten. The brief survives; the rubric does not. |
| **A018** | F507/F508: three of four checks require Airflow, which is not in the frozen v1 contract. My brief mentions "the orchestrator", never Airflow, so it ports to `/var/sched/state.json` unchanged. Deliberate. |
| **A019** | F552 says the load-bearing mechanism cannot be re-grounded and a redesign should be built on S207 instead. If that happens the ticket changes from a rename blast radius to a silently-wrong KPI, and this brief is void. **Lowest-confidence of the nine.** |
| **A025** | Same adapter-pinning issue as A002; F601 proposes merging with A002. |
| **A031** | F300/F301/F250 — the outcome simulator is unbuildable. The brief is the *only* part of A031 that is not under attack, because the stakeholder ask is the premise rather than the machinery. I expect it to survive a redesign nearly intact. |
| **A032** | F302 (closed-form quantile), F304 (redundant with A020). My brief states the two policy constraints numerically (40 alerts/shift); a redesign that changes the budget invalidates that line. |
| **A033** | F254 says the bitwise gate is unachievable. My brief states the acceptance bar as *"the same answer every time it is run"* — if F254 is executed and the bar softens, that line has to soften with it. |

A note the redesigners should have: **write the brief first next time.** Three of these nine
(A018, A031, A032) became noticeably easier to reason about once I had to state what a human
would actually say, and A017's defect (§3) is only visible from the brief's side. The brief is
a cheap design review that this corpus has never run.

---

## 3. Two findings filed

### F600 — A017's detection axis cannot survive the existence of a brief

**This is the deliverable-3 case: an archetype where the honest ticket destroys something the
design depends on.**

A017 awards rubric credit for *"detection (did the agent find the loss without being told)"*,
and its `why_hard` rests on *"nothing in the world is red"*. The brief is what starts the
episode, and §9.2.3 makes it the only legal input to `instruction.md`. So the episode cannot
begin unless a named person states an observable. The only observable this world can produce
is *a count is short* — silent destination row loss is the entire planted defect and there is
nothing else for a human to have noticed.

The brief either says a number looks low (the agent has been told) or says nothing (no
`instruction.md`, the task cannot ship). There is no third option. I wrote it the first way
and withheld the second planted symptom — the collapsed per-day created-date distribution — so
localisation still has to be earned. That is the most a brief can preserve here.

Recommended action `redesign`, folded into A017's existing F506 redesign. The corpus-wide
corollary is in the record and is the part that matters: **no rubric axis may pay for noticing
that a problem exists, because the brief always states that a problem exists.** A003, A006,
A013 and A020 lean the same way and should be checked before any of them is built.

### F601 — the briefs reveal duplication that no gate can see

Writing all 26 in one pass showed something reading them one at a time cannot. G3 dedups on
title similarity and on `discipline+apps+rubric_points`; on 2026-09-13 it reports exactly one
structural duplicate in the whole corpus (A040 vs A050) and no near-duplicate titles. **[V]**
But the agent never sees a title. It sees the brief, and then it does the investigation the
brief sets off. Five shapes account for 15 of the 26:

| Shape | Archetypes |
|---|---|
| a month of revenue is overstated; everything green; fix it idempotently | A001, A009, A026 |
| an upstream change silently corrupted or lost data; every run green | A002, A010, A017, A025 |
| two reporting surfaces disagree; tell me which is right | A006, A021, A029 (+ R002's A062) |
| the orchestrator says X; the cause is elsewhere | A011, A018, A027 (+ R002's A063) |
| a model quietly stopped being useful; labels lag; alerts are noise | A013, A020 (+ R002's A064) |

A026 is close to a strict subset of A001. A009's only distinguishing leg is the one its own
`repair_note` says is unsourced and should be dropped. There is precedent for the call: A014
was withdrawn on 2026-09-12 as a strict subset of A006.

**The measured negative result matters more than the finding.** My first draft of F601
recommended extending G3's matcher to `agent_visible_brief`. I measured it before recommending
it — ground rule 8 — and it does not work. Using G3's own `difflib.SequenceMatcher`: **[V]**

| Pair | title sim | brief sim |
|---|---|---|
| A001 vs A026 | 0.51 | **0.06** |
| A001 vs A009 | 0.76 | **0.02** |
| A010 vs A017 | 0.27 | **0.05** |
| A002 vs A025 | 0.51 | **0.05** |

Brief similarity is *lower* than title similarity in every cluster, because the briefs
deliberately differ in voice, name, department and domain — and that variation is a
requirement of a good brief, not a defect. **The duplication is in investigative shape and no
lexical metric reaches it.** I rewrote the mitigation to say so and recorded the negative
result, so the next round does not spend a day rediscovering it. Formal scope is A026 plus the
two tightest clusters; the other three are named in the record for whoever owns disposition.

Nothing was judged unwritable. A017 is the closest, and it is a rubric defect rather than an
unwritable defect.

---

## 4. Notes for whoever builds these

- **Briefs are world specifications now, whether we like it or not.** A004's brief says the
  volume was grown by 200GB on Friday. A032's says forty alerts a shift. A002's says 400
  million rows. Those numbers came from me, not from the archetype, and the world builder is
  now obliged to honour them or the ticket is a lie. Pull them into `SEED_SPEC.md` at build
  time rather than letting the two drift.
- **Twenty-six distinct named senders, one per brief** **[V]**, plus two colleagues mentioned
  in passing (Yusra in A001, Sam in A029). None collides with the R002 briefs' cast (Priya
  Raghavan, Marco, Dana Whitlock, Ravi, Tobias Lund, Marcus Oyelaran, Renata Sousa). No real
  company, product or incident is named anywhere — F101's training-data-leakage rule.
- **A018 never says "Airflow".** It says "the orchestrator". F507 says the Airflow dependency
  is fatal against the frozen v1 contract; the brief was written so it survives the port to
  `/var/sched/state.json`. A011 and A027 are the same. This was deliberate and is the one
  place where writing the brief early bought something concrete.

---

## 5. The single biggest weakness in my own output

**I systematically defused graded failure modes by writing their inverse into the brief as a
deliverable constraint, and I did it without a rule for when that is fair.**

This is the leak channel I actually used. It is not lexical, so the lint in §1 passes it
clean, and it is invisible unless you read a brief against its own `failure_modes_expected` —
which, as of today, only I have done for all 26.

Reading every brief against its archetype's failure modes — **my hand count, not a machine
result, and the judgement in it is exactly what needs checking** — **roughly 21 of 26 contain
at least one constraint clause that inverts a listed failure mode.** Some of that is
legitimate and some of it is me softening a design, and the line is this:

- **Fair** — the constraint states a grading criterion the agent could not otherwise know, so
  omitting it would grade on an unstated rule. A027's *"anything that cannot be loaded needs
  to be kept somewhere we can inspect it"* (the dead-letter conservation check). A020's
  *"do not give me another alert that goes off every morning"* (the monitor precision floor).
  A033's reproducibility bar. A019's deprecation ordering. A030's no-break-on-deprecate.
  GT001 does exactly this with *"make the fix durable"*, so there is precedent.
- **Not fair** — the constraint defuses a *reasoning* trap the agent was meant to fall into.
  I found four and fixed all four rather than only confessing them: A018 (*"do not just
  restart things until they go green"* → now reports what they tried, as evidence, with no
  instruction), A011 (same shape), A028 (*"validate properly rather than checking the totals
  look about right"* → cut), A029 (*"'one of them is wrong' will not close this ticket"* →
  cut).

The weakness is not that four slipped through. It is that **I was the one who decided which
side of the line each of the other seventeen fell on, using a rule I invented while writing
them, and nobody has checked that judgement.** That is precisely the structure F202 punished:
an author assessing their own author-side prose. The first red-team pass on these briefs
should start here, with `failure_modes_expected` open next to each one.

### Which of my briefs I think still leak

Ordered by how much it worries me. All four are cases where I made a judgement call and could
defend the opposite.

1. **A025** — worst, and it is a deliberate compromise. A positional column shift makes a
   dashboard show a string where a number belongs. That is what the reporter would *actually*
   say, and saying it hands over the mechanism. I wrote *"wrong in a way you can see from
   across the room"* instead, which is vaguer than any real person filing this ticket would
   be. So A025's brief is the least realistic of the 26 **by choice**, and it is still the
   likeliest to leak if a builder "improves" it toward realism. Flag it in the task package.
2. **A021** — the symptom is close to diagnostic. Two executives on two boards disagreeing is
   not itself a pointer to row-level security (definitions, filters and grain all explain it),
   which is why I wrote it that way rather than "same board, different viewers". But in a
   world that *has* an access-control surface, a capable model will check grants early. I also
   cut the natural constraint *"don't fix this by widening access"* precisely because it
   leaks, which means A021's governance-regression failure mode is now graded on an unstated
   rule — the opposite error from §5's first half. I could not have it both ways and I chose
   the leak-free side. Somebody should decide which error is cheaper.
3. **A005** — *"it has been like this since the first day it was serving rather than drifting
   there"* is diagnostic: an immediate gap at deployment points at skew rather than drift. I
   kept it because it is a genuine observable and GT001's *"since early February"* is the same
   kind of thing. It is still a free hypothesis narrowed.
4. **A002** — reports two symptoms (wrong regional margins, a model failing since Tuesday).
   F513's own mitigation proposed exactly this shape, so I followed it, but it does hint that
   one event produced both. The mitigating factor is that Tuesday is a *trap* — the true data
   boundary is not the date of the loud failure — so the hint costs less than it looks.

### Two smaller admissions

- **A006's brief needs a scoping device to work at all.** Its third defect is a KPI reading
  0.0% that survived *because nobody questioned it* — so no realistic reporter would ever
  mention it, and without a reason to look the agent cannot reach a third of the rubric. I
  used a board-pack framing ("treat the whole page as in scope"). It is legitimate, but it is
  a device, and A006 is the only brief that needs one to be solvable. That is a fact about
  A006's design and I did not file it, because the device works.
- **I have not tested a single one of these.** Every claim above about what a brief does or
  does not give away is my judgement, not a measurement. The only real test is C1: put a
  blind agent in front of `instruction.md` and see what it looks at first. Until that runs,
  F513 is closed and the thing F513 was really complaining about — *that nobody has tested the
  text the agent sees* — is still true. It is now testable, which it was not this morning.
  That is the whole of what this work bought.
