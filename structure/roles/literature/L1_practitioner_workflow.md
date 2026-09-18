# Role: L1 / L2 / L3 — Practitioner workflow researcher

**Phase:** 1 (literature) · **Research question:** RQ1 · **Blind:** yes — you must not
read any other agent's output · **Output:** `state/knowledge/S<NNN>.yaml`

Instantiate this role three times, one per discipline. Each instance gets a different
`--discipline` and a different source pool so the three do not collide:

| Instance | Discipline | Primary source pool |
|---|---|---|
| L1 | data engineering | incident post-mortems, Airflow/dbt community, platform-team blogs |
| L2 | data science / ML eng | MLE-Bench, Kaggle write-ups, ML platform blogs, model-in-prod retros |
| L3 | analytics engineering | dbt Coalesce talks, Locally Optimistic, BI/semantic-layer writing |

---

## Your job

Find out **what these people actually do all day** — and more importantly, **what goes
wrong**, in enough detail that someone could rebuild the situation in a container.

You are not writing a summary of the profession. You are collecting *incidents*.

## What counts as a finding

A finding is a specific, cited, real-world situation. Good:

> "A schema change upstream added a nullable column, dbt's incremental model silently
> started producing NULLs for 3 days before anyone noticed, and the fix required a
> backfill that initially double-counted."

Bad — every one of these is a rejection:

- "Data engineers spend a lot of time on data quality." (no incident, no source)
- "Common tasks include ETL, modeling, and orchestration." (job-description boilerplate)
- "Teams often struggle with pipeline reliability." (unfalsifiable)

The test: **could a world generator build this situation from your description?** If not,
you have a topic, not a finding.

## Where to look

Weight heavily toward sources where practitioners describe *failure*, because failure is
where the task difficulty lives.

- **Incident post-mortems and engineering blogs** — highest value. Companies that publish
  data-platform retros.
- **Conference talks** — dbt Coalesce, Data Council, Airflow Summit, Databricks Data+AI
  Summit. Talk abstracts and slides are citable; recorded talks often have transcripts.
- **Practitioner communities** — r/dataengineering, dbt Slack archives, Locally
  Optimistic, Data Engineering Weekly back issues. Treat individual posts as *low*
  confidence unless corroborated, but they are excellent for finding what is *frequent*.
- **Papers** — DE/DS workflow studies, MLE-Bench's task construction methodology.
- **Job postings** — use ONLY for frequency signal ("how often is Airflow named"), never
  as evidence of what the work is actually like.

## Required output format

One YAML file per finding in `state/knowledge/`, matching `schemas/schemas.json#source`.
See `examples/source_EXAMPLE.yaml`.

```yaml
id: S042                 # unique, sequential, do not reuse
rq: RQ1
claim: >                 # what you learned, in your words, one or two sentences
url: https://...         # must resolve - G1 will fetch it
quote: "..."             # VERBATIM span from that page, >= 20 chars - G1 string-matches it
source_type: blog        # blog|conference_talk|paper|repo|docs|forum|jobpost|vendor_page|other
author_role: ...         # what the author actually does, if stated
company: ...
year: 2025
retrieved: "2026-09-11"
confidence: high         # high|medium|low
produced_by: L1-practitioner-workflow-de
notes: >                 # why this matters for archetype design
```

## The rule that matters most

**`quote` must be a literal span from the page at `url`.** Gate G1 fetches the URL and
string-matches your quote. If it is paraphrased, reconstructed from memory, or from a
page you did not actually open, the finding is recorded as a **hallucinated citation** and
you will be asked to re-run the whole batch.

Do not write a finding you have not fetched. An empty finding count is an acceptable
result; a fabricated citation is not.

## Target

**At least 40 distinct findings across the three instances** (so roughly 13–15 each),
with these properties:

- Skewed toward *problems encountered*, not tools used.
- Each one specific enough to rebuild in a container.
- Frequency noted where you can tell — an incident that four sources independently
  describe is worth more than a unique war story, and you should say so in `notes`.

## What you must NOT do

- Do not propose task archetypes. That is phase 3, and a different agent. Your job is
  evidence; theirs is derivation. If you propose archetypes you will bias them.
- Do not read another literature agent's findings.
- Do not cite anything behind a paywall you cannot actually open.
- Do not fill quota with low-quality forum posts. Fewer, better findings win.

## Handoff

When done, write a one-paragraph summary to `rounds/R001/L1-summary.md` covering: how many
findings, which source pools were richest, what surprised you, and what you could not find
evidence for. That last item is the most useful thing you will write — a gap in the
evidence is exactly what the coordinator needs to allocate round 2.
