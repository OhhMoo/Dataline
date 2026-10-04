# BI build-out plan: semantic layer, Power BI, Tableau, Sigma

Written 2026-10-03. Owner: Michael (Power BI, Tableau / Sigma, semantic layer / BI-as-code).
Companion to `TOOL_INVENTORY.md` (§3.5, §1a). Sizes are rough: S ≈ days, M ≈ a week,
L ≈ multi-week.

## Status 2026-10-04

Phases 1-4 are built; phase 0's calibration access is not, so every skin is `partial`
(`alkera-gateway plugs matrix`: `da/snowflake` and `de/snowflake` are runnable;
`*/aws` and `*/databricks` wait only on Redshift and Databricks).

- **Phase 1:** `world/world/semantic/` (`model.py`, `expr.py`, `compile.py`,
  `render.py`, refusals in `errors.py`) and the dbt semantic-layer front-end
  (`formats/yaml_model.py`). `world/bi.py`'s SQL panels render through the core,
  byte-identical to the pre-engine output on the four themes that had panels. Not
  built: a `semantic_yaml` plug in the `bi` slot.
- **Phases 2-3:** Power BI (PBIP: TMDL + PBIR, a DAX subset, M source steps; fake
  Power BI REST, Fabric item definitions, Entra client credentials), Tableau
  (`.tds`/`.twb`, a calculated-field subset; fake REST 3.25 driven by tabcmd and
  tableauserverclient), Sigma (workbook and data-model JSON, a formula subset; fake
  API v2). Explicit-only plugs: served only under a profile that names them. Not
  built: refresh as a publication-ledger receipt (BI001 grades the services'
  published state, staged after teardown, instead).
- **Phase 4:** the `commerce_analytics` theme (the only `bi_projects` hook; `bi_faults:`
  plants a defect in a vendor's first commit) and BI001 on `da/laptop` with all three
  vendors: archetype 1 (a Tableau Custom SQL fan-out) plus a Sigma Net Revenue that
  keeps refunds. `TASK VALID` on both arms; the native oracle resolves through the
  harness. Archetypes 2 and 3 and the single-vendor stacks are not built.
- **Calibration: open.** No golden values from Power BI Desktop, Tableau or Sigma.

What the two differential checks showed:

- **MetricFlow.** A throwaway run of the real MetricFlow (metricflow 0.213.0,
  dbt-metricflow 0.15.0, dbt-core 1.12.5, dbt-duckdb 1.11.0) on the semantic fixture
  matched the engine on all 24 rows, both for a project written in MetricFlow's own
  idioms and for `yaml_model.write_project`'s output (whose semantic manifest
  MetricFlow validates); the numbers are pinned in `world/tests/test_semantic_yaml.py`.
  It also showed the Commerce model is not expressible in MetricFlow: CALCULATE's
  overwrite filter, REMOVEFILTERS, RELATED in a measure, metric_time reached through
  another table and an offset metric grouped by year/month are refused by name, while a
  dbt-native variant of the model reads and writes. It is the only comparison against an
  independent engine,
  and it covers the YAML front-end, not DAX, Tableau or Sigma semantics.
- **Cross-vendor agreement.** On the full `commerce_analytics` warehouse every visual
  the three editions share (KPIs, Revenue by Month, Revenue by Segment, Top Products,
  Revenue by Region 2026) renders identical rows in Power BI, Tableau and Sigma, equal
  to hand-written SQL; Segment Share agrees between Power BI and Tableau (Sigma has no
  per-column filter removal); Revenue LY and YoY Growth are Power BI only (time
  intelligence has no exact Tableau calculation or Sigma formula, so those front-ends
  refuse it). With `bi_faults` on, the three KPI cards disagree each for its declared
  reason (Power BI 33,733,790.00, Tableau 76,338,069.00, Sigma 35,084,324.00 at seed 11)
  (`world/tests/test_bi_projects.py`). The three front-ends share one compiler, so
  agreement shows the front-ends lower to the same model; it cannot catch a semantic
  error common to all three. That is what calibration is for.

The native tool calls on a BI world are checked by `tasks/harness/bi_e2e_check.py`
(40 actions, every tool an analyst uses): host topology 0 FAIL, 0 FINDING
(`tasks/harness/evidence/bi_e2e_host.json`).

## The one decision that shapes everything

**One semantic core, three vendor skins.** Power BI's TMDL, Tableau's data sources and
Sigma's workbooks are all the same thing underneath: tables, relationships, measures,
filters, and visuals that ask for a measure by some dimensions. Build that once as a
semantic model that compiles to SQL against the warehouse slot, and each vendor becomes:

1. a **file-format adapter** (read/write the vendor's text project files),
2. an **expression-subset translator** (DAX / Tableau calc / Sigma formula -> the core's
   expression tree, with everything outside the subset refused by name), and
3. a **fake REST service** (refresh, publish, query), following the existing native-service
   rules (`gateway/NATIVE.md`: the real client library works, one 9.2.2 row per request).

The core is also the "semantic layer / BI-as-code" item on its own: a YAML front-end over
the same model is what a dbt shop's metrics layer looks like.

Why not one clone per vendor: three independent engines means three chances to be
silently wrong, three calibration efforts, and tasks that cannot be compared across stacks.
With one core, the same seed and the same broken measure can be planted in Power BI on
`da/databricks`, Tableau on `da/aws` and Sigma on `da/snowflake`. That is the
data-eng-bench trick (one task, two backends) applied to BI.

## What exists today (the base to build on)

- Dashboards are YAML files of named SQL panels: `world/world/themes/<theme>/bi/*.yml`,
  copied to `var/bi/dashboards/`.
- **Rendering:** `world/world/bi.py` computes the panels into canonical values. The values
  are the point, not images, so "the dashboard disagrees with the warehouse" is checkable.
- **Tools:** `bi_render`, `bi_list`, `bi_get`, `bi_cached`, `bi_update` and `bi_refresh`
  (`gateway/src/alkera_gateway/bi.py`). Cached tiles and refresh events live outside the
  world tree.
- **Native fake:** a Metabase-shaped service (`native/services/metabase.py`) over the same
  store.
- **Graders:** six task graders read dashboards, and the ledger records publications.
- The BI slot's plugs are `metabase_fake` (shipped) plus `powerbi`, `tableau` and `sigma`
  (all planned) in `gateway/src/alkera_gateway/plugs.py`.

## Architecture

```
world/world/semantic/          the core (world/ is the lowest layer; gateway and graders import it)
  model.py                     tables, columns, relationships (cardinality, filter direction),
                               measures, calculated columns, visuals (measure x dimensions x filters)
  expr.py                      the expression tree every front-end lowers into
  compile.py                   expression tree + visual -> SQL (sqlglot, warehouse-slot dialect)
  render.py                    visual -> canonical values (same canon as world/bi.py today)
  refuse.py                    the one place "unsupported: <feature>" errors are raised
  formats/
    yaml_model.py              BI-as-code front-end (the semantic-layer item)
    tmdl.py  pbir.py           Power BI project (PBIP): semantic model + report
    dax.py                     DAX subset -> expr tree
    tableau.py  tableau_calc.py   .tds/.twb XML + calculated-field subset
    sigma.py  sigma_formula.py    workbook JSON + formula subset
world/world/bi_files.py        build step: write each plugged vendor's project into the workspace
gateway/src/alkera_gateway/native/services/
  powerbi.py  tableau.py  sigma.py   fake REST planes over world.semantic
tasks/contracts/               (unchanged) graders call world.semantic.render on the FINAL files
```

Determinism rules carry over unchanged: integer cents, `ORDER BY` everything, threads=1,
no wall clock; rendered values go through `hashing.canon_value`.

## Fidelity rules (non-negotiable, from the repo's research)

- **Loud unsupported, never silent wrong.** Every construct outside the subset raises
  `unsupported: <feature>` with the vendor's own name for it. No approximations.
- **The grader never trusts a cache or a service.** Grading recomputes values from the
  agent's final project files through `world.semantic` in the grading zone. Cached tiles,
  REST responses and screenshots are evidence of process, never of outcome.
- **The engine is not reachable from the workspace.** The core lives in the image and
  grading zone, not in the agent's repo, so the agent cannot edit the evaluator.
- **Calibrated or labelled.** A subset feature is `calibrated` only when a golden corpus
  produced by the real product matches the core. Otherwise the plug stays `partial`, and
  no task may depend on an uncalibrated feature.

## Phases

### Phase 0: decisions and calibration access (S, 2–3 days)

- Pick 3 BI task archetypes to build toward (proposal below) and freeze the expression
  subset each needs. Nothing outside that list gets built in phases 1–3.
- **Get real-product access for golden values:**
  - Power BI Desktop is free (Windows only), and DAX evaluates locally in it.
  - Tableau Public or a Desktop trial covers Tableau.
  - Sigma needs a trial account.
  - Record who holds which licence; calibration is the critical path.
- Decide the semantic YAML schema, starting from dbt's semantic models / MetricFlow so a
  dbt shop recognises it.

**Gate:** archetypes, subset lists and calibration access written down.

### Phase 1: the semantic core and BI-as-code (M, about 1 week)

- **Core:** `model.py`, `expr.py`, `compile.py`, `render.py` and `refuse.py`, plus the YAML
  front-end.
- **Migration:** the existing SQL-panel dashboards become a degenerate case of the model
  (a panel is a visual whose measure is raw SQL). `world/bi.py` delegates to the core.
- **Wiring:** `bi_*` tools and the Metabase fake read through the core. A new plug,
  `semantic_yaml`, joins the bi slot, so a profile can plug `bi = ["semantic_yaml",
  "powerbi"]`.

**Gate:** all four themes render **byte-identical values** through the core, every recorded
world hash and grader baseline is unchanged, and refusal tests cover every unsupported
construct named in phase 0.

### Phase 2: Power BI (M–L, about 1.5–2 weeks; this is the hardest one)

- **Files:** read and write PBIP. The semantic model is TMDL (tables, columns,
  relationships, measures); the report is PBIR JSON (pages, visuals, fields). PBIP is GA
  and the default since September 2026, and both formats are documented text, so no
  Microsoft binary is involved.
- **DAX subset to the expression tree:**
  - aggregations: `SUM`, `COUNT`, `COUNTROWS`, `DISTINCTCOUNT`, `AVERAGE`, `MIN`, `MAX`, `DIVIDE`;
  - `CALCULATE` with simple column filters, `ALL`, `ALLEXCEPT`, `REMOVEFILTERS`;
  - `RELATED` across single-direction relationships;
  - `SAMEPERIODLASTYEAR` and `TOTALYTD` over a marked date table.

  Refused by name:
  - context transition inside iterators beyond `SUMX` over one table;
  - bidirectional cross-filtering in measures;
  - `USERELATIONSHIP` with inactive relationships, until calibrated;
  - variables over tables;
  - any M (Power Query) beyond a plain source step.
- **Fake service at `powerbi.internal`:** a REST subset (groups, datasets, reports,
  `datasets/{id}/refreshes`, `executeQueries` over the DAX subset). Auth is a fake bearer
  token. There's no official Python SDK, so the compatibility test drives it with the
  documented REST shapes via `requests`.
- **Refresh = publication:** a refresh writes a ledger receipt of the rendered values, so a
  wrong number shown on Tuesday and fixed on Thursday stays visible to the grader.
- **Calibration:** at least 50 golden measure-by-dimension results from Power BI Desktop on
  the shared seed, checked in as fixtures and run as tests.

**Gate:** golden corpus green; the plug moves `planned` -> `partial` (-> `shipped` once a
task uses it).

### Phase 3: Tableau and Sigma (M each, about 1 week each, can run in parallel)

- **Tableau:**
  - Files: `.tds`/`.twb` XML (data source, calculated fields, worksheets).
  - Calc subset: aggregates, `IF`/`CASE`, `ZN`, `DATETRUNC`, and simple fixed-level LOD
    (`{FIXED [dim]: SUM(...)}`). Table calculations are refused until calibrated.
  - Fake REST: sites, workbooks, datasources, refresh, view data, driven by
    `tableauserverclient` (the real client, per the NATIVE rule).
  - **No Hyper API:** it has no Linux arm64 build, so extracts point at a live connection
    to the warehouse slot.
- **Sigma:**
  - Files: workbook JSON (elements, columns with formulas, filters).
  - Formula subset: aggregates, `If`, `Coalesce`, `DateTrunc`, and `Lookup` across a
    relationship.
  - Fake REST v2: workbooks, datasets, connections, export.
  - Sigma's model is already "SQL on the warehouse", so it is the cheapest to keep honest.
- Calibration corpus for each, as in phase 2.

### Phase 4: tasks and profiles (M)

- **One shared BI task family, three skins:**
  - the same seed and the same planted defect;
  - grading through `world.semantic.render` on the final files;
  - the full validity ladder (oracle, no-op, edits the cached tile, edits the warehouse
    instead of the measure, claims a fix without one).
- **Profiles:**
  - the role × stack table already defaults `da/snowflake` to Sigma, `da/aws` to Tableau
    and `da/databricks` to Power BI;
  - `da/snowflake` becomes runnable as soon as Sigma is `partial`;
  - `da/aws` and `da/databricks` also wait on Redshift and Databricks.

**Proposed archetypes** (each has a real-world analogue, and each is checkable from values):
1. **Fan-out double count.** A new relationship makes a revenue measure double-count. The
   fix belongs in the model, not the warehouse.
2. **Filter-context bug.** A share-of-total measure ignores a slicer, or ignores it when it
   shouldn't (`ALL` vs `ALLEXCEPT` and the Tableau / Sigma equivalents).
3. **Stale publish.** The dashboard was refreshed before the late partition landed. The
   number is wrong only in the ledger's history, and the fix is a refresh at the right
   time, not a measure edit.

### Phase 5: UI clones (L+, deferred; decide after phase 4)

"Tableau / Sigma UI clones" means a web UI an agent drives by browser or computer use. It
can sit on the same core (the UI edits the same project files and calls the same
services), but UI fidelity is open-ended. Gate it on phase 4 tasks proving the file/API
path, and scope it as its own plan.

## Effort and sequencing

| phase | size | depends on | parallel with |
|---|---|---|---|
| 0 decisions + calibration access | S | – | – |
| 1 semantic core + BI-as-code | M | 0 | – |
| 2 Power BI | M–L | 1 | 3 |
| 3 Tableau, Sigma | M each | 1 | 2 |
| 4 tasks + profiles | M | 2 or 3 (per skin) | – |
| 5 UI clones | L+ | 4 | – |

About 4–6 weeks to three calibrated skins and one task family. The first runnable BI
profile beyond Metabase (`da/snowflake` on Sigma) is reachable in about 2.5 weeks.

## Risks

- **Calibration access** is the critical path, especially Power BI on Windows. Without
  goldens, every skin stays `partial` and no task may depend on it.
- **DAX semantics creep.** Every "just one more function" pulls in filter-context rules.
  The phase 0 subset list is the contract, and additions need a calibration case first.
- **Determinism of vendor files.** Generated PBIR, `.twb` and Sigma JSON must be canonical
  (sorted keys, stable ids from the seed), or the world fingerprint moves between builds.
- **Fake-service tells.** Each fake must answer the real client library's shapes, or the
  agent learns it is in a fixture. The compatibility tests use the real clients wherever
  one exists.
