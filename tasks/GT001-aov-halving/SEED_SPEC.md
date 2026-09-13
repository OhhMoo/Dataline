# GT001 — seed spec (FROZEN CONTRACT for Track A)

**Task:** "Average order value halved" — non-idempotent append backfill duplicating a
denominator fact.
**Frozen 2026-09-12.** Track A builds the world from this. Track B's `bi_render` and `sql_*`
tools address these names. Track C owns everything under `tests/`.

Amendments go through Track C. Do not rename anything below without telling me — the grader
addresses these identifiers literally.

---

## 1. The causal chain

```
scheduler retry re-runs fct_orders loader in APPEND mode over 30 partitions
        that already contained rows
                    │
                    ▼
        warehouse.fct_orders  DUPLICATED for dt ∈ [2026-02-01, 2026-03-02]
                    │   (fct_revenue is NOT duplicated — different model,
                    │    merge strategy, different schedule)
                    ▼
        warehouse.kpi_daily.aov = revenue / order_count
                    │   numerator unchanged, denominator doubled
                    ▼
        AOV falls ~50% across the window        ← the visible symptom
                    ▼
        BI dashboard "aov_daily"                ← where the agent starts
```

The asymmetry is the point: only the **denominator** doubled, so the symptom is a halving
rather than a doubling. An agent that assumes "numbers look wrong ⇒ revenue is wrong" goes the
wrong way.

## 2. Virtual clock

- World "now": **2026-03-05T09:00:00Z**
- History: **2025-09-05 → 2026-03-04** (181 days)
- Damage window: **2026-02-01 → 2026-03-02** (30 partitions)

## 3. Schema

Warehouse file `/var/warehouse/warehouse.duckdb`, schemas `raw`, `staging`, `warehouse`.

### `warehouse.fct_orders` — the duplicated denominator fact
| column | type | note |
|---|---|---|
| order_id | BIGINT | business key part 1 |
| line_nbr | INTEGER | business key part 2 |
| order_dt | DATE | partition column |
| order_ts | TIMESTAMP | UTC; feeds the timezone decoy |
| customer_id | BIGINT | |
| sku | VARCHAR | |
| qty | INTEGER | |
| unit_price | DECIMAL(10,2) | |
| is_manual_adjustment | BOOLEAN | **must survive repair** |
| adjustment_note | VARCHAR | null unless adjusted |
| load_ts | TIMESTAMP | audit col — EXCLUDED from all diffs |
| batch_id | VARCHAR | audit col — EXCLUDED from all diffs |

**Business key: `(order_id, line_nbr)`.** Uniqueness holds everywhere except the damage window.

### `warehouse.fct_revenue` — numerator, NOT duplicated
`order_id, line_nbr, order_dt, gross_amount DECIMAL(12,2), promo_id VARCHAR NULL,
net_amount DECIMAL(12,2), load_ts, batch_id`

### `warehouse.kpi_daily` — the symptom surface (dbt view)
`dt DATE, order_count BIGINT, revenue DECIMAL(14,2), aov DECIMAL(10,2)`
where `order_count = COUNT(*) FROM fct_orders`, `revenue = SUM(net_amount) FROM fct_revenue`,
`aov = revenue / order_count`.

### `warehouse.dim_promotions` — carries decoy 2
`promo_id VARCHAR, promo_name VARCHAR, discount_pct DECIMAL(5,2), valid_from DATE, valid_to DATE`

### `warehouse.pipeline_watermark`
`job VARCHAR, last_loaded_dt DATE, updated_at TIMESTAMP`
Rows for jobs `daily_orders` and `daily_revenue`.

### Staging / raw
`staging.stg_orders`, `staging.stg_revenue` (dbt models);
`raw.orders_landing`, `raw.revenue_landing` loaded from
`/workspace/data/landing/{orders,revenue}/dt=YYYY-MM-DD/*.csv`.

## 4. Scale

~2,000 order lines/day × 181 days ≈ **362,000 rows** before duplication; ~60,000 duplicated.
Small on purpose — must build in under 60s and sit inside ~2GB.

## 5. The primary defect

For each `dt` in **[2026-02-01, 2026-03-02]**, every `fct_orders` row appears **exactly twice**
with identical business columns and **differing `load_ts` / `batch_id`**. The second copy carries
`batch_id = 'retry-20260303-0412'`.

`fct_revenue` is untouched. `pipeline_watermark.daily_orders.last_loaded_dt` = 2026-03-04, so
freshness monitoring reads green — nothing alerts.

The dbt model `warehouse/fct_orders.sql` uses
`materialized='incremental', incremental_strategy='append'` with no unique key. **That is the
bug to fix**, not merely the data.

## 6. Decoys — real defects, present across ALL history, each explaining only a small delta

**Decoy 1 — timezone boundary.** `stg_orders` casts `order_ts` to date with a naive local cast,
so orders between 23:00–00:00 UTC land on the wrong `order_dt`. Affects **~1.4%** of rows
uniformly across all 181 days. A real defect worth fixing; it does not explain a 50% move and it
did not start at the window boundary.

**Decoy 2 — promotion fan-out.** `dim_promotions` holds duplicate rows for **7 promo_ids**
(same promo, overlapping validity ranges). `stg_revenue` joins on `promo_id`, so those orders'
revenue is inflated ~2–3%. Again uniform across history.

Both survive a shallow check. Neither is the answer. **Fixing either without finding the
duplication earns partial credit and fails the task.**

## 7. Repair hazard — manual adjustments

**412 rows** across **4 partitions inside the window** carry `is_manual_adjustment = true` with a
populated `adjustment_note` (finance corrections for a pricing error). **These rows do not exist
in the landing CSVs.**

So the obvious repair — truncate the window and reload from landing — produces a table that
passes a naive row-count check while silently destroying 412 finance-corrected rows. The grader
checks for them explicitly.

## 8. Window-boundary obfuscation

A crashed run left **partial CSV fragments** in 2 partitions adjacent to the window
(**2026-03-03** and **2026-03-04**). The next scheduled run consumed them, so those two dates
have *incomplete* rather than duplicated data. The true damage window is therefore not simply
"where the duplicates are" — the agent must distinguish the two failure modes and state the
duplication window exactly.

## 9. What the agent must do

1. Diagnose the duplication as primary cause, with the exact window.
2. Repair `fct_orders`, preserving all 412 manual-adjustment rows.
3. Fix the **loader** so a re-run is idempotent (merge / insert-overwrite / delete+insert in one
   transaction — all accepted).
4. Add an executable regression test that **fails on the pre-repair world and passes after**.
5. Submit the structured final artifact (`agent.md` §9.2.5).

## 10. Hidden — grading zone only, never mounted in the episode

`/grading/expected.duckdb` (correct `fct_orders`, `kpi_daily`), `/grading/manifest.yaml`
(planted defects for per-cause credit), `/grading/solution.sh`, `/grading/grade.py`.

**Track A must verify** that a reference-copy agent cannot `ATTACH` anything under `/grading`
from inside the episode. That check is a build gate, not a test.

## 11. Determinism

Everything above generates from `seed`. Same seed ⇒ identical semantic fingerprint: row values,
the 412 adjustment rows, which 7 promo_ids fan out, which rows fall in the timezone band, and
the fragment contents. No wall-clock, no RNG without the seed, no hostname or core-count
dependence.

## 12. Evidence grounding — read this

**Re-grounded 2026-09-12 after the citation audit.** A001's original derivation (S001, S005,
S006) is **withdrawn as evidence**: S005/S006 belong to the vendor-survey cluster with no
auditable denominator (failure F450), and S001's backfill-doubling story is the essay's narrated
**opening scene**, not an attested incident — the record's claim that "the author states this is
a real incident" was false (failure F451).

Replacement grounding:

- **S008** *(audit-clean, high confidence, conference talk)* — Astronomer field engineers
  cataloguing recurring production failures across customers, explicitly naming **backfills
  imploding** and **local-timezone scheduling**. Supports both the primary mechanism and decoy 1.
- **S015** *(audit-clean, high confidence, repo)* — a long-open dbt issue on incremental-model
  semantics; supports that incremental materialisation edge cases are a live, real class.

**Still needed:** a primary citation for dbt's `append` vs `merge` incremental semantics, from
dbt's own documentation. That is verifiable tool behaviour rather than a vendor claim, and it
must be **fetched and quote-verified**, not asserted. Tracked as an open item for Track C.

**Honest status:** the *mechanism* is sound and now rests on audit-clean sources. The original
frequency weighting ("how often does this happen in the wild") does **not** — that came from the
failed vendor survey and is not re-derived. Claim the mechanism, not the frequency.
