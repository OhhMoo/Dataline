# dbt parse budget at scale — measured, not extrapolated

**2026-09-13, lead agent.** Source record: `S950`.

## Why

E2 (scale archetypes) named its parse-time budget as the weakest number in its own report
and said plainly that nothing had been built or measured. Its estimate extrapolated linearly
from S650 (~3 min at 1,599 models) down to **~40–70s at 570 models** — at or over Track A's
60s boot budget — and noted dbt parse is "probably superlinear in test count, so the true
figure is likely worse."

If that were right, the entire scale-archetype family (A050–A054) would be undeployable, and
we would not want to discover that after building the generator.

## Method

Generated four synthetic dbt projects at 100 / 300 / 570 / 900 models in a
`stg_ → int_ → rpt_` shape at the prefix ratios E2 measured from a real public monorepo
(S664), each model carrying two generic tests via a single `schema.yml`. Timed
`dbt parse` against the dbt in `gateway/.venv` (dbt-core + dbt-duckdb), `threads: 1`,
in-memory DuckDB target. Cold runs delete `target/` first.

## Results

| Models | Manifest nodes | Cold parse | Warm parse (`partial_parse.msgpack`) |
|---:|---:|---:|---:|
| 0 (empty project) | 0 | **4.08s** | — |
| 100 | 300 | 10.31s | — |
| 300 | 900 | 9.93s | — |
| 570 | 1,710 | **11.12s** | **4.55s** |
| 900 | 2,700 | 12.81s | 5.02s |

Re-parse after an edit, 570 models:

| Edit | Re-parse |
|---|---:|
| one model `.sql` file | 4.96s |
| `schema.yml` carrying 1,140 tests | 4.57s |

## Findings

1. **Parse is dominated by fixed startup, not model count.** An empty project costs 4.08s.
   Going 100 → 900 models (300 → 2,700 nodes) adds only ~2.5s on top.

2. **E2's extrapolation was wrong by roughly 4–6×, in the safe direction.** 570 models
   cold-parse in ~11s, not 40–70s. Scaling across this range is mild and sublinear in
   wall-clock; the suspected superlinearity in test count does not appear here.

3. **Partial parsing survives edits, and is per-file.** This was E2's specific operational
   worry — the agent *will* edit models mid-episode, and E2 expected that to invalidate the
   prebuilt parse. It does not: editing one model re-parses in 4.96s, and editing the
   `schema.yml` holding 1,140 tests in 4.57s. Both are essentially the startup floor.

**The scale-archetype family is within budget.** Shipping a prebuilt `partial_parse.msgpack`
is still worth doing (it takes the in-episode cost to ~4.6s) but it is an optimisation, not
a rescue.

## Honest limit

**This is a floor, not a prediction.** The generated models are one-line selects with no
packages, no macro layer, no snapshots, no seeds, and trivial Jinja. S650's three-minute
project is presumably far heavier per node, and macro-heavy projects parse much more slowly
at the same node count — which is the likeliest explanation for the gap, rather than S650
being wrong.

What this establishes: **node count alone does not blow the budget in this range.** What it
does not establish: that any particular realistic 570-model project parses in 11s. When the
generator exists, re-measure against its actual output.

Not measured here: `dbt run`/`dbt test` execution time (parse only), memory footprint, or
the cost of the DuckDB bulk data itself — all three matter for the 2GB ceiling and none are
addressed by this note.
