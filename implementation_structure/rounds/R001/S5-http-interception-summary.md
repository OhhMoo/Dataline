# S5-http-interception — Phase 3 summary (R001)

> **CORRECTION (post red-team, F110):** the "S3/MinIO (T109)" reference predates the MinIO rejection. Read it as S3 via adobe/s3mock (T154), which is http_intercept.


Role: derive CLAIMS (RQ3) for the E3 derivation path — for which of the 30 inventory
apps can we intercept HTTP and replicate the vendor REST format at a fake local URL.

## What I derived (state/claims/C001-C006.yaml)

- **C001** — Snowflake (T107) is mock-able today via fakesnow's standalone HTTP server:
  real connector/JDBC/CLI against a fake local URL, per-connection isolated DBs for
  parallel RL rollouts. Adoption decision, not build decision. (S316, S500)
- **C002** — fakesnow cannot be the sole fidelity axis: row-ordering non-determinism +
  liberal dialect + sqlglot "transpiler not validator" force grading rules (fully
  specified ORDER BY, execute-against-ground-truth per DAB validate.py, deterministic
  multi-seed per MLE-bench). (S316, S318, S315, S307)
- **C003** — Negative/boundary claim: mocks routed through a live cloud account are
  disqualified by E2; this kills universql (needs a real Snowflake account for catalog
  and ACLs). fakesnow-over-DuckDB is the only hermetic Snowflake substrate. (S317, S316)
- **C004** — BigQuery (T108) / Redshift (T119): no evidenced HTTP-intercept mock in the
  corpus; E3 path is sqlglot dialect substitution onto DuckDB, weaker than Snowflake.
  (S318, S503, S502, S507)
- **C005** — Confluence (T110) / Jira (T102): easiest interception class after
  Snowflake — the environment needs information-shape semantics (spaces/pages, work
  items/boards), not Atlassian fidelity; a fake REST server over synthetic content
  satisfies E3. No mock repo evidenced yet. (S522, S524)
- **C006** — BI REST APIs (T113/T114/T115/T128): weakest interception candidates; the
  evidenced path is OSS substitution (Superset locally), vendor REST replication is
  plausible but unevidenced and should be deferred. (S519, S515, S516)

## Richest evidence

S316 (fakesnow) is the single most load-bearing id — it grounds C001-C003 and carries
the grading caveats verbatim. S317/S318 complete the warehouse triangle
(adopt / cannot-use / adopt-with-rules). S315 and S307 supplied the concrete grading
patterns that turn the fakesnow caveats into claim C002. The L6 ids (S500-S526) are
docs-only: they establish *what surfaces exist* but contain zero mock-repo findings.

## Least confident

- **C004** (least): asserts a sqlglot-substitution path for BigQuery/Redshift with no
  emulator finding behind it; the claim itself says the dedicated-emulator probe is open
  RQ3 work. Would be strengthened by a BigQuery-emulator (e.g. goccy/bigquery-emulator
  class) source in a future pass.
- **C005** (second least): feasibility assertion for a fake Confluence/Jira REST server
  rests on docs-shape evidence (S522/S524) only; no implementation exists in the corpus.
- Not covered: Slack (T101) has no S5xx knowledge id at all — no claim was made for it;
  flag for a literature pass. S3/MinIO (T109) needs oss_equivalent, not interception
  (S514 notes say local S3-compatible surface), so no interception claim was written.
