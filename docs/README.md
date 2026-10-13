# docs/ — research, implementation history, private notes

Current operating docs live next to the code, in each folder's `README.md` (start at the
root `README.md`). This folder holds what explains *why* and *how we got here*.

```
environments/                   what the composable environments need
  TOOL_INVENTORY.md             every tool (Snowflake, Databricks, Redshift, BigQuery, Airflow,
                                dbt, Jupyter, Power BI, Tableau, Sigma, ...): status, local plug,
                                licence, arm64; the slot model; DE/DS/DA x vendor-stack profiles
  BI_BUILD_PLAN.md              semantic core + Power BI / Tableau / Sigma skins, phased

research/                       design research with sources
  Scoring_Redesign.md           the eval and scoring design and its provenance
                                (normative spec: tasks/contracts/DECISION_TEMPLATE.md §9)
  LLM_Judge_Research.md         why no LLM grades an episode; what the off-score judge does
  llm-judge/r1..r6_*.md         the source sweeps behind it, with quotes
  ENVIRONMENT_DESIGN.md         the 2026 environment-design sweep: fidelity, dynamism,
                                reward-hacking defense, difficulty mechanics, episode
                                economics, packaging — ranked recommendations (Oct 2)

implementation_plan/            plans, round logs, findings and status: history, kept unchanged
  TASK_STATUS.md                per-task results under the current grader and whether to use each
  agent.md                      the original project guide (2026-09-08 to 09-16): idea, roadmap,
                                sandbox plan, problem-set design, frozen contracts (§9.2),
                                repo architecture (§10), native surfaces (§12). Code comments
                                cite it as "agent.md §N".
  structure/                    the R001-R005 research-swarm record: roles, rounds, the typed
                                blackboard (state/: archetypes, sources, claims, failure rulings),
                                its gates, and TASK_DESIGN_2026-09-15.md (the four-layer
                                framework). Formerly implementation_structure/, then structure/.
                                Guide: structure/README.md
  hardness-round/               the hardness round's plan, invariants, ownership and findings
  paper-trace.md                2026-09-25 trace of design choices to the pages that support them

private/                        gitignored: notes not meant for the shared repo
```

Anything named `*.private.md`, `*for_andrew*.md` or `FOR_*.md` is also gitignored anywhere
in the tree.
