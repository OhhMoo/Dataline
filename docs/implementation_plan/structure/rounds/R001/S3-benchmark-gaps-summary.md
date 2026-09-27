# S3 summary — archetypes derived from gaps in existing benchmarks

Derivation path: read the prior-art evidence (S300-S318: Harbor, ade-bench,
data-eng-bench, MLE-Bench, DataAgentBench, APEX/SkyRL grading patterns) first,
identified surfaces no benchmark simulates (live ingestion connectors, an
orchestrator boundary, a BI layer downstream of dbt, access-control/policy state,
and any single trace crossing 4+ surfaces), then anchored each archetype in RQ1
incident evidence.

## Artifacts written (state/archetypes/)

- **A017** Ingestion-connector schema drift with silent row loss (DE) — S022, S002,
  S013, S019. Benchmark gap: data-eng-bench/ade-bench have no connector surface.
- **A018** Cross-system orchestration triage, ran-long vs started-late (DE) — S009,
  S008, S018, S011, S012. Gap: no benchmark has a live scheduler; failure behind
  the orchestrator is the largest practitioner pain per S018.
- **A019** dbt refactor with invisible BI blast radius (DA) — S010, S216, S217,
  S218, S211. Gap: existing graders stop at warehouse tables; rename blast radius
  into BI is structurally untestable there.
- **A020** Demand-shift model rescue under delayed labels and alert fatigue (DS) —
  S114, S117, S123, S125, S118. Gap: MLE-Bench is static offline with full labels;
  nothing grades the detect-decide-remediate-monitor loop.
- **A021** Row-level security / masking misconfiguration behind conflicting exec
  numbers (DA) — S205, S215, S525, S206. Gap: no benchmark has an access-control
  surface at all.
- **A022** Long-horizon cross-surface triage — broken metric vs broken business
  (DA) — S207, S208, S203, S205, S010. Gap: single verdict-first trace crossing
  ingestion+warehouse+dbt+BI+docs is uncovered everywhere; the differentiating
  format.

Discipline coverage: 2 DE, 1 DS, 3 DA (requirement of >=1 DS and >=1 DA met).

## Richest evidence

- **S008/S009/S018** (orchestration): a failure-mode catalogue, a first-party
  platform account (Pinterest), and a peer-reviewed taxonomy — A018 is the most
  defensible archetype in this batch.
- **S207/S208** (Thicket's own incidents): exact failure mechanics (failure-to-zero,
  varying-valid-day windows) that translate directly into planted broken-world
  initial conditions and deterministic ground truth — A022's grading keys come
  almost verbatim from these.
- **S010/S011/S012**: first-party, mechanically precise, and already
  grading-shaped (hidden answer key on bottleneck + verdict).

## Least confident

- **A021 (access control)** — flagged in-file. The only practitioner source naming
  row-level security as a discrepancy cause is one enumerated item in S205; S525 is
  a substrate, not an incident. The archetype is derived from a real benchmark gap
  but thin incident literature. If a future RQ1 pass finds no first-party
  governance incidents, merge its reconciliation core into A022.
- **A019** is second-least confident: DA-6 sources are practitioner blogs/forums
  with zero first-party BI-vendor incident reports (per L3), though the S010
  rename-to-board-meeting incident and S211's duplicate-BI-report validation
  pattern carry most of the weight.

## Verification

`python gates/run_gates.py --json` (offline): G0, G3, G5 all pass. G0 warnings are
pre-existing `repair_note` fields on knowledge files, not on these artifacts.
